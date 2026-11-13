"""Explicit, invocation-local document and output execution budgets."""

import io

import pytest
from tests.xslt.support import stylesheet

from pyxsd.resource_policy import ResourceData
from pyxsd.xdm import XdmAtomic
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltDynamicError
from pyxsd.xslt.results import _result_node_count
from pyxsd.xslt.runtime.frames import ExecutionBudget, ExecutionLimits
from pyxsd.xslt.runtime.resources import DocumentResources

BUDGET_CODE = "PYXSD-XSLT-001"


class _DocumentPolicy:
    def __init__(self, documents: dict[str, bytes]) -> None:
        self.documents = documents
        self.reads: list[tuple[str, str | None, str]] = []

    def read(self, source, *, base, purpose):
        stream = getattr(source, "read", None)
        if stream is not None:
            return ResourceData("<memory>", stream())
        self.reads.append((source, base, purpose))
        return ResourceData(source, self.documents[source])


def test_document_budget_counts_distinct_policy_reads() -> None:
    policy = _DocumentPolicy({"urn:doc:a": b"<a/>", "urn:doc:b": b"<b/>"})
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<out><xsl:sequence select=\"doc('urn:doc:a'), doc('urn:doc:b')\"/></out>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=policy).compile(io.BytesIO(source))
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(limits=ExecutionLimits(max_documents=1))
    assert caught.value.code == BUDGET_CODE
    assert "document budget" in str(caught.value)
    assert caught.value.catchable is False
    assert [read[0] for read in policy.reads] == ["urn:doc:a", "urn:doc:b"]


def test_cached_documents_are_counted_once_and_reset_per_invocation() -> None:
    policy = _DocumentPolicy({"urn:doc:a": b"<a/>"})
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<out><xsl:value-of select=\"count(doc('urn:doc:a')/a)"
        " + count(doc('urn:doc:a')/a)\"/></out>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=policy).compile(io.BytesIO(source))
    limits = ExecutionLimits(max_documents=1)
    assert compiled.transform(limits=limits).principal_tree.string_value == "2"
    assert compiled.transform(limits=limits).principal_tree.string_value == "2"
    assert [read[0] for read in policy.reads] == ["urn:doc:a", "urn:doc:a"]


def test_text_resources_do_not_consume_the_document_budget() -> None:
    policy = _DocumentPolicy({"urn:text": b"plain text", "urn:doc": b"<doc/>"})
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<out><xsl:value-of select=\"concat(unparsed-text('urn:text'), "
        "name(doc('urn:doc')/*))\"/></out>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=policy).compile(io.BytesIO(source))
    result = compiled.transform(limits=ExecutionLimits(max_documents=1))
    assert result.principal_tree.string_value == "plain textdoc"
    assert [read[0] for read in policy.reads] == ["urn:text", "urn:doc"]


def test_output_budget_counts_principal_result_nodes() -> None:
    source = stylesheet('<xsl:template name="xsl:initial-template"><out><a/></out></xsl:template>')
    compiled = XsltProcessor().compile(io.BytesIO(source))
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(limits=ExecutionLimits(max_output_nodes=1))
    assert caught.value.code == BUDGET_CODE
    assert "output budget" in str(caught.value)
    assert caught.value.catchable is False

    allowed = compiled.transform(limits=ExecutionLimits(max_output_nodes=2))
    assert allowed.principal_tree.root_element.local_name == "out"


def test_output_budget_counts_secondary_result_nodes() -> None:
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<principal/>"
        '<xsl:result-document href="urn:test:secondary"><secondary><inner/></secondary>'
        "</xsl:result-document>"
        "</xsl:template>"
    )
    compiled = XsltProcessor().compile(io.BytesIO(source))
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(limits=ExecutionLimits(max_output_nodes=2))
    assert "output budget" in str(caught.value)

    allowed = compiled.transform(limits=ExecutionLimits(max_output_nodes=3))
    assert set(allowed.secondary) == {"urn:test:secondary"}


def test_frame_and_step_budgets_keep_their_existing_messages() -> None:
    non_tail = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        '<xsl:call-template name="recurse"/></xsl:template>'
        '<xsl:template name="recurse"><xsl:call-template name="recurse"/><x/>'
        "</xsl:template>"
    )
    compiled = XsltProcessor().compile(io.BytesIO(non_tail))
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(limits=ExecutionLimits(max_frames=20))
    assert caught.value.code == BUDGET_CODE
    assert "frame budget" in str(caught.value)

    tail = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        '<xsl:call-template name="recurse"/></xsl:template>'
        '<xsl:template name="recurse"><xsl:call-template name="recurse"/></xsl:template>'
    )
    compiled = XsltProcessor().compile(io.BytesIO(tail))
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(limits=ExecutionLimits(max_steps=20))
    assert caught.value.code == BUDGET_CODE
    assert "step budget" in str(caught.value)


def test_budget_counters_are_invocation_local() -> None:
    budget = ExecutionBudget(
        ExecutionLimits(max_documents=1, max_output_nodes=2, max_output_bytes=2)
    )
    assert budget.documents == 0
    assert budget.take_document() is True
    assert budget.take_document() is False
    assert budget.take_output_nodes(1) is True
    assert budget.take_output_nodes(2) is False
    assert budget.output_nodes == 1
    assert budget.take_output_bytes(1) is True
    assert budget.take_output_bytes(2) is False
    assert budget.output_bytes == 1

    unlimited = ExecutionBudget()
    assert unlimited.take_document() is True
    assert unlimited.take_output_nodes(5) is True
    assert unlimited.take_output_bytes(5) is True
    assert unlimited.documents == 1
    assert unlimited.output_nodes == 5
    assert unlimited.output_bytes == 5


@pytest.mark.parametrize("field", ["max_documents", "max_output_nodes", "max_output_bytes"])
def test_optional_budget_fields_are_validated(field: str) -> None:
    with pytest.raises(TypeError, match=field):
        ExecutionLimits(**{field: True})
    with pytest.raises(TypeError, match=field):
        ExecutionLimits(**{field: "1"})
    with pytest.raises(ValueError, match=field):
        ExecutionLimits(**{field: 0})
    assert getattr(ExecutionLimits(**{field: 1}), field) == 1


def test_document_resources_validate_a_budget() -> None:
    with pytest.raises(TypeError, match="ExecutionBudget"):
        DocumentResources({}, budget=object())


def test_result_node_count_charges_attributes_and_deduplicates() -> None:
    document = XdmDocument.parse(b'<root attr="x"><a/></root>')
    root = document.root_element
    child = root.children[0]
    assert _result_node_count((root, child, XdmAtomic.string("not a node"))) == 3


# ---------------------------------------------------------------------------
# Serialized output byte budget


SECONDARY_URI = "urn:test:secondary"


def compile_principal_and_secondary_output():
    source = stylesheet(
        '<xsl:output method="xml" omit-xml-declaration="yes"/>'
        '<xsl:template name="xsl:initial-template">'
        "<principal/>"
        f'<xsl:result-document href="{SECONDARY_URI}">'
        "<secondary><inner/></secondary>"
        "</xsl:result-document>"
        "</xsl:template>"
    )
    return XsltProcessor().compile(io.BytesIO(source))


def compile_principal_only_output():
    source = stylesheet(
        '<xsl:output method="xml" omit-xml-declaration="yes"/>'
        '<xsl:template name="xsl:initial-template"><principal/></xsl:template>'
    )
    return XsltProcessor().compile(io.BytesIO(source))


def compile_text_only_stylesheet():
    source = stylesheet('<xsl:template name="xsl:initial-template">just text</xsl:template>')
    return XsltProcessor().compile(io.BytesIO(source))


def _serialized_bytes(entry) -> int:
    assert entry.serialized is not None
    return len(entry.serialized.encode(entry.serialization.get("encoding", "utf-8")))


def _delivered_sizes() -> tuple[int, int]:
    result = compile_principal_and_secondary_output().transform()
    principal = _serialized_bytes(result.principal_entry)
    secondary = sum(_serialized_bytes(entry) for entry in result.secondary.values())
    return principal, secondary


PRINCIPAL_BYTES, SECONDARY_BYTES = _delivered_sizes()
COMBINED_BYTES = PRINCIPAL_BYTES + SECONDARY_BYTES


def test_serialized_outputs_share_a_byte_budget() -> None:
    compiled = compile_principal_and_secondary_output()
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(limits=ExecutionLimits(max_output_bytes=COMBINED_BYTES - 1))
    assert caught.value.code == BUDGET_CODE
    assert "output byte budget" in str(caught.value)
    assert not caught.value.catchable


def test_exact_serialized_byte_budget_is_allowed() -> None:
    compiled = compile_principal_and_secondary_output()
    limits = ExecutionLimits(max_output_bytes=COMBINED_BYTES)
    first = compiled.transform(limits=limits)
    second = compiled.transform(limits=limits)
    assert first.principal_entry.serialized is not None
    assert second.principal_entry.serialized == first.principal_entry.serialized
    assert set(first.secondary) == {SECONDARY_URI}


def test_byte_budget_accumulates_principal_and_secondary_results() -> None:
    assert 0 < PRINCIPAL_BYTES < COMBINED_BYTES
    assert SECONDARY_BYTES > 0
    with pytest.raises(XsltDynamicError) as caught:
        compile_principal_and_secondary_output().transform(
            limits=ExecutionLimits(max_output_bytes=PRINCIPAL_BYTES)
        )
    assert "output byte budget" in str(caught.value)

    principal_only = compile_principal_only_output().transform(
        limits=ExecutionLimits(max_output_bytes=PRINCIPAL_BYTES)
    )
    assert _serialized_bytes(principal_only.principal_entry) == PRINCIPAL_BYTES


@pytest.mark.parametrize("delivery", ["raw", "document"])
def test_non_stylesheet_delivery_never_charges_serialized_bytes(delivery: str) -> None:
    result = compile_principal_and_secondary_output().transform(
        delivery=delivery,
        limits=ExecutionLimits(max_output_bytes=1),
    )
    assert result.principal_entry.serialized is None
    assert all(entry.serialized is None for entry in result.secondary.values())


def test_byte_charge_uses_the_entry_encoding() -> None:
    source = stylesheet(
        '<xsl:output method="text" encoding="UTF-16"/>'
        '<xsl:template name="xsl:initial-template">abc</xsl:template>'
    )
    compiled = XsltProcessor().compile(io.BytesIO(source))
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(limits=ExecutionLimits(max_output_bytes=5))
    assert "output byte budget" in str(caught.value)

    result = compiled.transform(limits=ExecutionLimits(max_output_bytes=6))
    assert result.principal_entry.serialized == "abc"


def test_node_and_byte_budgets_report_distinct_diagnostics() -> None:
    node_source = stylesheet(
        '<xsl:template name="xsl:initial-template"><out><a/></out></xsl:template>'
    )
    node_compiled = XsltProcessor().compile(io.BytesIO(node_source))
    with pytest.raises(XsltDynamicError) as node_error:
        node_compiled.transform(limits=ExecutionLimits(max_output_nodes=1))
    with pytest.raises(XsltDynamicError) as byte_error:
        compile_principal_and_secondary_output().transform(
            limits=ExecutionLimits(max_output_bytes=1)
        )
    node_text = str(node_error.value)
    byte_text = str(byte_error.value)
    assert node_text.endswith("the transformation exceeded its output budget")
    assert byte_text.endswith("the transformation exceeded its output byte budget")
    assert node_text != byte_text


def test_unlimited_node_budget_does_not_walk_results(monkeypatch) -> None:
    def fail_if_called(*args: object, **kwargs: object) -> int:
        raise AssertionError("an unlimited node budget must not count result nodes")

    monkeypatch.setattr("pyxsd.xslt.processor._result_node_count", fail_if_called)
    assert compile_text_only_stylesheet().transform().principal
