"""Cumulative limits at the public transformation and result boundaries."""

import io
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from tests.xslt.support import stylesheet

from pyxsd.resource_policy import ResourceData
from pyxsd.xdm import XdmAtomic, XdmMap
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltDynamicError, XsltSerializationError
from pyxsd.xslt.results import delivered_entries
from pyxsd.xslt.runtime.frames import (
    Continuation,
    ExecutionBudget,
    ExecutionLimits,
    Frame,
    Return,
    Scheduler,
)
from pyxsd.xslt.serialization import Serializer


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


_CORPUS = Path(__file__).with_name("corpus") / "catalog.xml"


@pytest.mark.skipif(not _CORPUS.is_file(), reason="XSLT corpus not checked out")
@pytest.mark.parametrize("name", ["catalog/catalog-003", "variable/variable-0108"])
def test_pathological_corpus_case_has_a_controlled_verdict(name):
    run = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json
import sys
from tests.xslt.catalog import load_catalog
from tests.xslt.suite import SUITE_PROFILES, run_parallel

case = load_catalog(sys.argv[1]).cases[sys.argv[2]]
(result,) = run_parallel((case,), SUITE_PROFILES["xslt30"], jobs=1, timeout=5)
print(json.dumps([result.name, result.outcome.value, result.message]))
""",
            str(_CORPUS),
            name,
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=40,
        check=True,
    )
    assert "Traceback (most recent call last)" not in run.stderr
    actual_name, verdict, detail = json.loads(run.stdout)
    assert actual_name == name
    if name == "catalog/catalog-003" and verdict == "pass":
        return  # The corpus assertion already checked the result.
    assert verdict == "timeout"
    assert detail == "the case exceeded its 5 s budget"


def _deep_stylesheet(shape):
    function = """<xsl:function name="local:f">
      <xsl:param name="n"/>
      {body}
    </xsl:function>
    <xsl:template match="/"><xsl:sequence select="local:f(160)"/></xsl:template>"""
    source = b"<n/>"
    if shape == "push":
        source = b"<n>" * 160 + b"</n>" * 160
        body = """<xsl:template match="/"><xsl:apply-templates/></xsl:template>
        <xsl:template match="n">
          <out><xsl:apply-templates select="n"/></out>
        </xsl:template>"""
    elif shape == "sequence":
        body = function.format(
            body='<xsl:sequence select="if ($n eq 0) then () else ($n, local:f($n - 1))"/>'
        )
    elif shape == "function-constructor":
        body = function.format(
            body="""<xsl:if test="$n gt 0"><out>
              <xsl:sequence select="local:f($n - 1)"/>
            </out></xsl:if>"""
        )
    elif shape == "named-template":
        body = """<xsl:template match="/">
          <xsl:call-template name="nested"><xsl:with-param name="n" select="160"/></xsl:call-template>
        </xsl:template>
        <xsl:template name="nested">
          <xsl:param name="n"/>
          <xsl:if test="$n gt 0"><out><xsl:call-template name="nested">
            <xsl:with-param name="n" select="$n - 1"/>
          </xsl:call-template></out></xsl:if>
        </xsl:template>"""
    else:
        select = "/".join(["."] * 550)
        body = f'<xsl:template match="/"><xsl:sequence select="{select}"/></xsl:template>'
    compiled = XsltProcessor().compile(
        io.BytesIO(stylesheet(body, namespaces={"local": "urn:local"}))
    )
    return compiled, source


@pytest.mark.parametrize(
    ("shape", "cap"),
    [
        ("push", 8),
        ("sequence", 8),
        ("function-constructor", 8),
        ("named-template", 8),
        ("static-path", 1),
    ],
)
def test_deep_non_tail_execution_reports_the_bound_that_fired(shape, cap):
    compiled, source = _deep_stylesheet(shape)
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(source, delivery="raw")
    error = caught.value
    assert type(error) is XsltDynamicError
    assert error.code == "PYXSD-XSLT-001"
    assert error.catchable is False
    prefix = (
        f"the transformation exceeded the Python recursion limit: "
        f"Python recursion limit={sys.getrecursionlimit()}, max_frames=100000, frame depth="
    )
    message = error.args[0]
    assert message.startswith(prefix)
    assert 0 < int(message.removeprefix(prefix)) < 100_000

    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(source, delivery="raw", limits=ExecutionLimits(max_frames=cap))
    _assert_exhausted(caught.value, "frame", "max_frames", cap, cap + 1)


@pytest.mark.parametrize(
    "limits",
    [ExecutionLimits(), ExecutionLimits(max_frames=200_000), ExecutionLimits(max_frames=None)],
)
def test_python_stack_diagnostic_names_the_configured_frame_budget(limits):
    compiled, source = _deep_stylesheet("static-path")
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(source, delivery="raw", limits=limits)
    error = caught.value
    assert type(error) is XsltDynamicError
    assert error.code == "PYXSD-XSLT-001"
    assert error.catchable is False
    budget = (
        "max_frames=None (no frame budget)"
        if limits.max_frames is None
        else f"max_frames={limits.max_frames}"
    )
    assert error.args[0] == (
        f"the transformation exceeded the Python recursion limit: "
        f"Python recursion limit={sys.getrecursionlimit()}, {budget}, frame depth=2"
    )


def test_deep_result_serialization_reports_a_coded_error():
    depth = sys.getrecursionlimit() + 50
    document = XdmDocument.parse(b"<n>" * depth + b"</n>" * depth)
    with pytest.raises(XsltSerializationError) as caught:
        Serializer.serialize((document,))
    assert caught.value.code == "PYXSD-XSLT-001"
    assert caught.value.args[0] == (
        f"serialization exceeded the Python recursion limit: "
        f"Python recursion limit={sys.getrecursionlimit()}"
    )
    assert isinstance(caught.value.__cause__, RecursionError)


def test_python_stack_diagnostic_when_a_return_continuation_overflows():
    def overflow(value):
        raise RecursionError("continuation overflow")

    scheduler = Scheduler()
    entry = Frame(kind="entry", continuation=Continuation("convert", target=overflow))
    with pytest.raises(XsltDynamicError) as caught:
        scheduler.run(entry, lambda frame: Return(None))
    assert caught.value.code == "PYXSD-XSLT-001"
    assert caught.value.catchable is False
    assert caught.value.args[0] == (
        f"the transformation exceeded the Python recursion limit: "
        f"Python recursion limit={sys.getrecursionlimit()}, max_frames=100000, frame depth=0"
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


def test_node_budget_stops_before_the_rest_of_a_raw_result_is_constructed(monkeypatch):
    compiled = _compile(
        '<xsl:for-each select="1 to 10000">'
        '<xsl:if test=". eq 8"><xsl:sequence select="error()"/></xsl:if><item/>'
        "</xsl:for-each>"
    )
    serialized = []
    serialize = Serializer.serialize

    def record_serialization(*args, **kwargs):
        serialized.append(True)
        return serialize(*args, **kwargs)

    monkeypatch.setattr(Serializer, "serialize", record_serialization)
    for delivery in ("raw", "stylesheet"):
        with pytest.raises(XsltDynamicError) as caught:
            compiled.transform(delivery=delivery, limits=ExecutionLimits(max_output_nodes=3))
        _assert_exhausted(caught.value, "output", "max_output_nodes", 3, 4)
        assert not serialized
    assert _compile("<item/>").transform().principal_entry.serialized
    assert serialized == [True]


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
    with_sentinel = XsltProcessor().compile(
        io.BytesIO(
            stylesheet(
                '<xsl:template match="sentinel"><xsl:sequence select="error()"/></xsl:template>'
            )
        )
    )
    source = b"<a>first<b/>second<sentinel/></a>"
    with pytest.raises(XsltDynamicError) as sentinel:
        with_sentinel.transform(source, delivery="raw", limits=limits)
    assert sentinel.value.code == "FOER0000"
    with pytest.raises(XsltDynamicError) as caught:
        with_sentinel.transform(source, delivery="raw", limits=ExecutionLimits(max_output_nodes=1))
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


ANNOTATION_BODY = '<xsl:param name="values"/><xsl:sequence select="reverse($values)"/>'


def _annotation_values(count):
    return tuple(XdmAtomic.integer(n) for n in range(1000, 1000 + count))


def test_annotation_capacity_is_inclusive():
    compiled = _compile(ANNOTATION_BODY)
    values = _annotation_values(3)
    limits = ExecutionLimits(max_annotations=2)
    assert (
        compiled.transform(
            delivery="raw", template_parameters={"values": values[:2]}, limits=limits
        ).principal
        == values[:2][::-1]
    )
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(delivery="raw", template_parameters={"values": values}, limits=limits)
    _assert_exhausted(caught.value, "atomic type-annotation", "max_annotations", 2, 3)


def test_disabling_annotation_capacity_admits_more_than_the_default():
    compiled = _compile(ANNOTATION_BODY)
    values = _annotation_values(262_145)
    kwargs = {"delivery": "raw", "template_parameters": {"values": values}}
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(**kwargs)
    _assert_exhausted(caught.value, "atomic type-annotation", "max_annotations", 262_144, 262_145)
    assert (
        compiled.transform(**kwargs, limits=ExecutionLimits(max_annotations=None)).principal
        == values[::-1]
    )


@pytest.mark.parametrize(("parent_count", "count"), [(1, 3), (2, 262_145)])
def test_nested_transform_inherits_annotation_capacity(tmp_path, parent_count, count):
    child = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        f'<xsl:variable name="values" select="1000 to {999 + count}"/>'
        '<xsl:variable name="reversed" select="reverse($values)"/>'
        '<xsl:value-of select="count($reversed)"/>'
        "</xsl:template>"
    )
    path = tmp_path / "nested-annotation-limit.xsl"
    path.write_bytes(child)
    compiled = _compile(
        f'<xsl:variable name="parent" select="1000 to {999 + parent_count}"/>'
        '<xsl:sequence select="reverse($parent)"/>'
        '<xsl:sequence select="transform(map{'
        f"'stylesheet-location': '{path.as_uri()}', 'delivery-format': 'raw'"
        '})?output"/>'
    )
    cap = parent_count + count - 1
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(delivery="raw", limits=ExecutionLimits(max_annotations=cap))
    _assert_exhausted(caught.value, "atomic type-annotation", "max_annotations", cap, cap + 1)
    assert "nested-annotation-limit.xsl" in str(caught.value)
    assert compiled.transform(
        delivery="raw", limits=ExecutionLimits(max_annotations=cap + 1)
    ).principal[-1].string_value == str(count)
    assert compiled.transform(
        delivery="raw", limits=ExecutionLimits(max_annotations=None)
    ).principal[-1].string_value == str(count)


def test_stateful_encoding_preserves_one_shot_bytes_and_prefixes():
    text = "\u65e5" * 9000
    compiled = _compile(
        f"<xsl:text>{text}</xsl:text>",
        output='<xsl:output method="text" encoding="ISO-2022-JP"/>',
    )
    expected = text.encode("iso2022_jp")
    limit = 9000
    small = compiled.transform(
        delivery="raw", output_uri=URI, limits=ExecutionLimits(max_output_bytes=limit)
    )
    prefix = io.BytesIO()
    with pytest.raises(XsltDynamicError) as caught:
        small.write(prefix)
    assert caught.value.code == "PYXSD-XSLT-001"
    assert prefix.getvalue() == expected[:limit]
    for cap in (len(expected), None):
        complete = io.BytesIO()
        compiled.transform(
            delivery="raw", output_uri=URI, limits=ExecutionLimits(max_output_bytes=cap)
        ).write(complete)
        assert complete.getvalue() == expected
