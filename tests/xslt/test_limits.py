"""Cumulative limits at the public transformation and result boundaries."""

import io
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from tests.xslt.support import stylesheet

from pyxsd.resource_policy import ResourceData
from pyxsd.xdm import XdmMap
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltDynamicError, XsltSerializationError
from pyxsd.xslt.results import delivered_entries
from pyxsd.xslt.runtime.frames import ExecutionBudget, ExecutionLimits


def _compile(body, *, output=""):
    return XsltProcessor().compile(
        io.BytesIO(
            stylesheet(f'{output}<xsl:template name="xsl:initial-template">{body}</xsl:template>')
        )
    )


def _assert_exhausted(error, label, field, limit, amount):
    assert type(error) is XsltDynamicError
    assert error.code == "PYXSD-XSLT-001"
    assert error.catchable is False
    assert error.args[0] == (
        f"the transformation exceeded its {label} budget: {field}={limit}, "
        f"cumulative amount={amount}"
    )


def test_resource_bytes_count_xml_and_shared_text_json_octets(tmp_path):
    document = tmp_path / "input.xml"
    document.write_bytes(b"<a/>")
    text = tmp_path / "input.json"
    text.write_bytes(b"[]")
    compiled = _compile(
        f"""<xsl:sequence select="name(doc('{document.as_uri()}')/*),
             array:size(json-doc('{text.as_uri()}')), unparsed-text('{text.as_uri()}')"
             xmlns:array="http://www.w3.org/2005/xpath-functions/array"/>"""
    )
    assert (
        len(
            compiled.transform(
                delivery="raw", limits=ExecutionLimits(max_resource_bytes=6)
            ).principal
        )
        == 3
    )
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(delivery="raw", limits=ExecutionLimits(max_resource_bytes=5))
    _assert_exhausted(caught.value, "resource byte", "max_resource_bytes", 5, 6)


def test_node_budget_stops_before_the_rest_of_a_raw_result_is_constructed(tmp_path):
    compiled = _compile(
        '<xsl:for-each select="1 to 10000">'
        '<xsl:if test=". eq 8"><xsl:sequence select="error()"/></xsl:if><item/>'
        "</xsl:for-each>"
    )
    output = tmp_path / "must-not-write.xml"
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(
            delivery="raw",
            output_uri=output.as_uri(),
            limits=ExecutionLimits(max_output_nodes=3),
        )
    _assert_exhausted(caught.value, "output", "max_output_nodes", 3, 4)
    assert not output.exists()


@pytest.mark.parametrize("delivery", ["raw", "document", "stylesheet"])
def test_node_budget_counts_raw_identity_and_pre_normalization_text(delivery):
    compiled = _compile(
        '<xsl:variable name="item" as="element()"><item attr="value"><child/></item></xsl:variable>'
        '<xsl:sequence select="$item, $item, $item/child"/>'
        "<xsl:text>first</xsl:text><xsl:text>second</xsl:text>"
    )
    assert (
        len(
            compiled.transform(
                delivery=delivery, limits=ExecutionLimits(max_output_nodes=5)
            ).principal
        )
        == 5
    )
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(delivery=delivery, limits=ExecutionLimits(max_output_nodes=4))
    _assert_exhausted(caught.value, "output", "max_output_nodes", 4, 5)


TEXT = '<xsl:output method="text" encoding="UTF-8"/>'
URI = "urn:test:output"


def test_write_stops_at_the_remaining_encoded_byte_budget():
    compiled = _compile("<xsl:text>\u00e9\u00e9\u00e9</xsl:text>", output=TEXT)
    result = compiled.transform(
        delivery="raw", output_uri=URI, limits=ExecutionLimits(max_output_bytes=3)
    )
    destination = io.BytesIO()
    assert result.serialize() == "\u00e9\u00e9\u00e9"
    with pytest.raises(XsltDynamicError) as caught:
        result.write(destination)
    _assert_exhausted(caught.value, "output byte", "max_output_bytes", 3, 6)
    assert destination.getvalue() == "\u00e9\u00e9\u00e9".encode()[:3]


def test_repeated_writes_share_the_ledger_and_charge_overridden_encoding():
    result = _compile("<xsl:text>ab</xsl:text>", output=TEXT).transform(
        delivery="raw", output_uri=URI, limits=ExecutionLimits(max_output_bytes=6)
    )
    first, second, third = (io.BytesIO() for _ in range(3))
    result.write(first)
    result.write(second, parameters={"encoding": "UTF-16"})
    assert first.getvalue() == b"ab"
    assert second.getvalue() == b"a\x00b\x00"
    with pytest.raises(XsltDynamicError) as caught:
        result.write(third)
    _assert_exhausted(caught.value, "output byte", "max_output_bytes", 6, 8)
    assert third.getvalue() == b""


def test_stylesheet_delivery_and_later_write_share_one_budget():
    result = _compile("<xsl:text>abc</xsl:text>", output=TEXT).transform(
        output_uri=URI, limits=ExecutionLimits(max_output_bytes=5)
    )
    destination = io.BytesIO()
    with pytest.raises(XsltDynamicError) as caught:
        result.write(destination)
    _assert_exhausted(caught.value, "output byte", "max_output_bytes", 5, 6)
    assert destination.getvalue() == b"ab"


def test_stylesheet_delivery_encodes_in_chunks():
    # A later encoding error must not hide exhaustion during an earlier chunk.
    compiled = _compile(
        "<xsl:value-of select=\"string-join((1 to 100000) ! 'a', '') || '&#233;'\"/>",
        output='<xsl:output method="text" encoding="US-ASCII"/>',
    )
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(limits=ExecutionLimits(max_output_bytes=1))
    error = caught.value
    assert error.code == "PYXSD-XSLT-001"
    assert "max_output_bytes=1" in error.args[0]
    assert error.catchable is False


def test_all_limits_can_be_disabled():
    result = _compile("<out><item/></out>").transform(
        output_uri=URI,
        limits=ExecutionLimits(
            max_frames=None,
            max_steps=None,
            max_documents=None,
            max_output_nodes=None,
            max_output_bytes=None,
            max_resource_bytes=None,
        ),
    )
    destination = io.BytesIO()
    result.write(destination)
    result.write(destination)
    assert destination.getvalue().count(b"<item/>") == 2


def test_nested_encoding_and_explicit_write_share_the_effective_encoding():
    child = stylesheet(
        f'{TEXT}<xsl:template name="xsl:initial-template">ab</xsl:template>'
    ).decode()
    options = XdmMap.from_mapping(
        {
            "stylesheet-text": child,
            "delivery-format": "serialized",
            "serialization-params": XdmMap.from_mapping({"encoding": "UTF-16"}),
        }
    )
    compiled = _compile(
        '<xsl:param name="options"/><xsl:sequence select="transform($options)?output"/>',
        output=TEXT,
    )
    result = compiled.transform(
        delivery="raw",
        output_uri=URI,
        template_parameters={"options": options},
        limits=ExecutionLimits(max_output_bytes=5),
    )
    destination = io.BytesIO()
    with pytest.raises(XsltDynamicError) as caught:
        result.write(destination)
    _assert_exhausted(caught.value, "output byte", "max_output_bytes", 5, 6)
    assert destination.getvalue() == b"a"


@pytest.mark.parametrize(
    ("field", "label", "cap", "amount"),
    [("max_documents", "document", 1, 2), ("max_resource_bytes", "resource byte", 7, 8)],
)
def test_parent_and_child_document_reads_share_budget(tmp_path, field, label, cap, amount):
    local = tmp_path / "input.xml"
    local.write_bytes(b"<a/>")
    expr = f"name(doc('{local.as_uri()}')/*)"
    child = stylesheet(
        f'<xsl:template name="xsl:initial-template"><xsl:sequence select="{expr}"/></xsl:template>'
    ).decode()
    options = XdmMap.from_mapping({"stylesheet-text": child, "delivery-format": "raw"})
    outer = _compile(
        f'<xsl:param name="options"/><xsl:sequence select="{expr}, transform($options)?output"/>'
    )
    compiled = XsltProcessor().compile(io.BytesIO(child.encode()))
    limits = ExecutionLimits(**{field: cap})
    assert len(compiled.transform(delivery="raw", limits=limits).principal) == 1
    with pytest.raises(XsltDynamicError) as caught:
        outer.transform(delivery="raw", template_parameters={"options": options}, limits=limits)
    _assert_exhausted(caught.value, label, field, cap, amount)


def test_parent_and_child_output_nodes_share_budget():
    expression = "if (exists(transform($options)?output)) then 'yes' else 'no'"
    compiled = _compile(f'<xsl:param name="options"/><a/><xsl:sequence select="{expression}"/>')
    child = stylesheet('<xsl:template name="xsl:initial-template"><b/></xsl:template>').decode()
    options = XdmMap.from_mapping({"stylesheet-text": child, "delivery-format": "raw"})
    assert (
        len(
            compiled.transform(
                delivery="raw",
                template_parameters={"options": options},
                limits=ExecutionLimits(max_output_nodes=2),
            ).principal
        )
        == 2
    )
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(
            delivery="raw",
            template_parameters={"options": options},
            limits=ExecutionLimits(max_output_nodes=1),
        )
    _assert_exhausted(caught.value, "output", "max_output_nodes", 1, 2)


@pytest.mark.parametrize(("field", "label"), [("max_steps", "step"), ("max_frames", "frame")])
def test_parent_and_child_scheduler_share_budget(field, label):
    child = stylesheet(
        '<xsl:template name="xsl:initial-template"><xsl:sequence select="1"/></xsl:template>'
    )
    options = XdmMap.from_mapping({"stylesheet-text": child.decode(), "delivery-format": "raw"})
    outer = _compile(
        '<xsl:param name="options"/><xsl:sequence select="transform($options)?output"/>'
    )
    # The child fits this limit on its own, but its scheduler runs inside
    # another invocation that has already consumed steps and holds a frame.
    cap = {"max_steps": 4, "max_frames": 2}[field]
    limits = ExecutionLimits(**{field: cap})
    assert (
        len(
            XsltProcessor()
            .compile(io.BytesIO(child))
            .transform(delivery="raw", limits=limits)
            .principal
        )
        == 1
    )
    with pytest.raises(XsltDynamicError) as caught:
        outer.transform(delivery="raw", template_parameters={"options": options}, limits=limits)
    _assert_exhausted(caught.value, label, field, cap, cap + 1)


def test_secondary_construction_stops_before_a_later_instruction():
    compiled = _compile(
        '<xsl:result-document href="urn:test:secondary">'
        '<xsl:for-each select="1 to 10000">'
        '<xsl:if test=". eq 8"><xsl:sequence select="error()"/></xsl:if><item/>'
        "</xsl:for-each></xsl:result-document>"
    )
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(delivery="raw", limits=ExecutionLimits(max_output_nodes=3))
    _assert_exhausted(caught.value, "output", "max_output_nodes", 3, 4)


def test_caught_temporary_output_contributes_no_nodes():
    compiled = _compile(
        '<xsl:try><discard/><discard/><xsl:sequence select="error()"/>'
        "<xsl:catch><delivered/></xsl:catch></xsl:try>"
    )
    assert (
        len(
            compiled.transform(delivery="raw", limits=ExecutionLimits(max_output_nodes=1)).principal
        )
        == 1
    )


def test_initial_source_reads_share_budget_with_doc(tmp_path):
    source = tmp_path / "initial.xml"
    source.write_bytes(b"<a/>")
    second = tmp_path / "second.xml"
    second.write_bytes(b"<b/>")
    compiled = XsltProcessor().compile(
        io.BytesIO(
            stylesheet(
                f'<xsl:template match="/"><xsl:sequence select="name(doc(\'{second.as_uri()}\')/*)"/>'
                "</xsl:template>"
            )
        )
    )
    assert (
        len(compiled.transform(source, limits=ExecutionLimits(max_resource_bytes=8)).principal) == 1
    )
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(source, limits=ExecutionLimits(max_resource_bytes=7))
    _assert_exhausted(caught.value, "resource byte", "max_resource_bytes", 7, 8)


def test_located_nested_stylesheet_and_its_include_share_budget(tmp_path):
    module = tmp_path / "child.xsl"
    body = stylesheet('<xsl:include href="include.xsl"/>')
    module.write_bytes(body)
    included = stylesheet('<xsl:template name="xsl:initial-template">ab</xsl:template>')
    (tmp_path / "include.xsl").write_bytes(included)
    compiled = _compile(
        '<xsl:param name="options"/><xsl:sequence select="transform($options)?output"/>'
    )
    options = XdmMap.from_mapping(
        {"stylesheet-location": module.as_uri(), "delivery-format": "raw", "cache": False}
    )
    size = len(body) + len(included)
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(
            delivery="raw",
            template_parameters={"options": options},
            limits=ExecutionLimits(max_resource_bytes=size - 1),
        )
    _assert_exhausted(caught.value, "resource byte", "max_resource_bytes", size - 1, size)
    assert (
        len(
            compiled.transform(
                delivery="raw",
                template_parameters={"options": options},
                limits=ExecutionLimits(max_resource_bytes=size),
            ).principal
        )
        == 1
    )


def test_policy_payload_is_charged_before_xml_parsing():
    class Policy:
        def read(self, source, *, base, purpose):
            if purpose == "schema":
                return ResourceData("<memory>", source.read())
            return ResourceData(source, b"not XML")

    compiled = XsltProcessor(resource_policy=Policy()).compile(
        io.BytesIO(
            stylesheet(
                '<xsl:template name="xsl:initial-template">'
                "<xsl:sequence select=\"doc('urn:test:xml')\"/>"
                "</xsl:template>"
            )
        )
    )
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(limits=ExecutionLimits(max_resource_bytes=1))
    _assert_exhausted(caught.value, "resource byte", "max_resource_bytes", 1, 7)


def test_path_retains_only_the_allowed_prefix_when_a_write_exhausts(tmp_path):
    result = _compile("<xsl:text>abc</xsl:text>", output=TEXT).transform(
        delivery="raw", output_uri=URI, limits=ExecutionLimits(max_output_bytes=2)
    )
    path = tmp_path / "output.txt"
    with pytest.raises(XsltDynamicError) as caught:
        result.write(path)
    _assert_exhausted(caught.value, "output byte", "max_output_bytes", 2, 3)
    assert path.read_bytes() == b"ab"


def test_parallel_writes_spend_only_one_remaining_budget():
    result = _compile("<xsl:text>abc</xsl:text>", output=TEXT).transform(
        delivery="raw", output_uri=URI, limits=ExecutionLimits(max_output_bytes=3)
    )
    barrier = Barrier(2)

    def write():
        try:
            destination = io.BytesIO()
            barrier.wait()
            try:
                result.write(destination)
            except XsltDynamicError as error:
                _assert_exhausted(error, "output byte", "max_output_bytes", 3, 6)
        except BaseException:
            barrier.abort()
            raise
        return destination.getvalue()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write)
        second = pool.submit(write)
        assert sorted((first.result(), second.result())) == [b"", b"abc"]


def test_byte_budget_does_not_replace_an_encoding_error_that_occurs_first():
    compiled = _compile(
        "<xsl:text>\u00e9</xsl:text>", output='<xsl:output method="text" encoding="US-ASCII"/>'
    )
    with pytest.raises(XsltSerializationError) as caught:
        compiled.transform(limits=ExecutionLimits(max_output_bytes=16))
    assert caught.value.code == "SERE0008"


def test_serialized_result_view_can_still_serialize_raw_and_override_serialized():
    compiled = _compile("<out/>")
    raw = compiled.transform(delivery="raw")
    assert delivered_entries(raw, delivery="serialized") == [("output", (raw.serialize(),))]
    default = compiled.transform()
    params = {"method": "text"}
    assert delivered_entries(default, delivery="serialized", principal_parameters=params) == [
        ("output", ("",))
    ]


def test_source_octets_are_not_charged_for_parsed_input():
    from pyxsd.xdm.nodes import XdmDocument

    compiled = XsltProcessor().compile(
        io.BytesIO(stylesheet('<xsl:template match="/"><out/></xsl:template>'))
    )
    limits = ExecutionLimits(max_resource_bytes=1)
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(b"<input/>", limits=limits)
    _assert_exhausted(caught.value, "resource byte", "max_resource_bytes", 1, 8)
    parsed = XdmDocument.parse(b"<input/>")
    assert compiled.transform(parsed, limits=limits).principal_tree.root_element.local_name == "out"


def test_disabled_resource_counter_is_inert():
    budget = ExecutionBudget(ExecutionLimits(max_resource_bytes=None))
    budget.charge_resource_bytes(10**100)
    assert budget.resource_bytes == 0


def test_lax_validation_charges_the_original_source_octets():
    compiled = XsltProcessor(validation="lax").compile(
        io.BytesIO(stylesheet('<xsl:template match="/"><out/></xsl:template>'))
    )
    source = b"<a/>"
    assert (
        compiled.transform(
            source, limits=ExecutionLimits(max_resource_bytes=4)
        ).principal_tree.root_element.local_name
        == "out"
    )
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(source, limits=ExecutionLimits(max_resource_bytes=3))
    _assert_exhausted(caught.value, "resource byte", "max_resource_bytes", 3, 4)


def test_located_nested_stylesheet_charges_its_policy_payload(tmp_path):
    child = stylesheet('<xsl:template name="xsl:initial-template">ab</xsl:template>')
    source = stylesheet(
        '<xsl:template name="xsl:initial-template"><xsl:param name="options"/>'
        '<xsl:sequence select="transform($options)?output"/></xsl:template>'
    )

    class Policy:
        def read(self, target, *, base, purpose):
            return (
                ResourceData("<memory>", target.read())
                if hasattr(target, "read")
                else ResourceData(target, child)
            )

    compiled = XsltProcessor(resource_policy=Policy()).compile(io.BytesIO(source))
    options = XdmMap.from_mapping(
        {
            "stylesheet-location": (tmp_path / "virtual.xsl").as_uri(),
            "delivery-format": "raw",
            "cache": False,
        }
    )
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(
            delivery="raw",
            template_parameters={"options": options},
            limits=ExecutionLimits(max_resource_bytes=1),
        )
    _assert_exhausted(caught.value, "resource byte", "max_resource_bytes", 1, len(child))


def test_built_in_templates_admit_text_nodes_during_application():
    compiled = XsltProcessor().compile(io.BytesIO(stylesheet("")))
    source = b"<a>first<b/>second</a>"
    limits = ExecutionLimits(max_output_nodes=2)
    result = compiled.transform(source, delivery="raw", limits=limits)
    assert [node.string_value for node in result.principal] == ["first", "second"]
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(source, delivery="raw", limits=ExecutionLimits(max_output_nodes=1))
    _assert_exhausted(caught.value, "output", "max_output_nodes", 1, 2)


def test_output_walk_deduplicates_owned_attributes_and_excludes_namespaces():
    compiled = XsltProcessor().compile(
        io.BytesIO(
            stylesheet(
                '<xsl:template match="/">'
                '<xsl:sequence select="/*, /*/@a, /*/namespace::*"/>'
                "</xsl:template>"
            )
        )
    )
    source = b'<out xmlns:t="urn:test" a="value"/>'
    result = compiled.transform(source, delivery="raw", limits=ExecutionLimits(max_output_nodes=2))
    assert [node.kind.value for node in result.principal] == [
        "element",
        "attribute",
        "namespace",
        "namespace",
    ]
    assert result.principal[0].attributes[0] is result.principal[1]
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(source, delivery="raw", limits=ExecutionLimits(max_output_nodes=1))
    _assert_exhausted(caught.value, "output", "max_output_nodes", 1, 2)
