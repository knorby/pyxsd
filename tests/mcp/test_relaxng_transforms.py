"""Ordinary transform previews over RELAX NG documents and root results.

The preview worker compiles an explicitly selected RELAX NG schema through the
public compiler, parses the source with the normal typed public mode, and
accepts lexical ``RelaxNGElement`` root results alongside XSD bound roots.
Candidate verdicts always come from a clean coordinator parse against the
target schema, never from a transformed document's stale source report.
"""

import hashlib
import io
import json
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio

from pyxsd import Schema
from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import FileRef, InlineFile, SchemaRef
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.execution_main import TransformContext, _apply
from pyxsd.mcp.inputs import SessionInputs
from pyxsd.mcp.output_store import OutputStore
from pyxsd.mcp.previews import PreviewService, PreviewTransformRequest
from pyxsd.mcp.storage import Storage
from pyxsd.mcp.transforms import normalize_transform
from pyxsd.mcp.workers import WorkerPool
from pyxsd.relaxng import RelaxNGDocument, RelaxNGElement, RelaxNGSchema

NS = "http://relaxng.org/ns/structure/1.0"
XSDT = "http://www.w3.org/2001/XMLSchema-datatypes"

STABLE_RNG = f"""<element xmlns="{NS}" name="doc">
  <attribute name="kind"><text/></attribute>
  <oneOrMore>
    <element name="item">
      <attribute name="id"><data type="integer" datatypeLibrary="{XSDT}"/></attribute>
      <text/>
    </element>
  </oneOrMore>
</element>"""

MIXED_RNG = f"""<grammar xmlns="{NS}">
  <start>
    <element name="doc">
      <mixed>
        <zeroOrMore>
          <element name="item"><text/></element>
        </zeroOrMore>
      </mixed>
    </element>
  </start>
</grammar>"""

GENERIC_RNG = f"""<element xmlns="{NS}">
  <anyName/>
  <zeroOrMore><attribute><anyName/></attribute></zeroOrMore>
  <text/>
</element>"""

XSD_SHAPED_RNG = f"""<element xmlns="{NS}" name="root">
  <zeroOrMore>
    <element name="item"><text/></element>
  </zeroOrMore>
</element>"""

XSD_TEXT = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
    '<xs:element name="root"><xs:complexType><xs:sequence>'
    '<xs:element name="item" type="xs:int" minOccurs="0" maxOccurs="unbounded"/>'
    "</xs:sequence></xs:complexType></xs:element></xs:schema>"
)

STABLE_XML = '<doc kind="k"><item id="1">one</item><item id="2">two</item></doc>'
MIXED_XML = "<doc>lead<item>one</item>trail<item>two</item>end</doc>"
XSD_SHAPED_XML = "<root><item>3</item></root>"

DEFAULT_FILES = {
    "schema.rng": STABLE_RNG,
    "schema.xsd": XSD_TEXT,
    "document.xml": STABLE_XML,
}


def rng_ref(path="schema.rng"):
    return SchemaRef(source=FileRef(path=path), format="relax-ng")


def xsd_ref(path="schema.xsd"):
    return SchemaRef(source=FileRef(path=path))


@asynccontextmanager
async def harness(tmp_path, files, *, limits=None):
    limits = limits or Limits(enforced=True, workers=1)
    with Storage(tmp_path / "state", persist=False) as storage:
        outputs = OutputStore(storage, limits)
        inputs = SessionInputs(storage.scratch, limits)
        pool = WorkerPool(limits)
        previews = PreviewService(inputs, pool, outputs, backend="trusted-local")
        workspace = await inputs.create()
        await inputs.put_files(
            workspace.id, [InlineFile(path=path, text=text) for path, text in files.items()]
        )
        try:
            yield previews, workspace.id
        finally:
            await pool.aclose()
            outputs.close()


async def preview(
    service,
    workspace,
    source,
    *,
    document="document.xml",
    source_schema=None,
    target_schema=None,
):
    request = PreviewTransformRequest(
        workspace_id=workspace,
        document=FileRef(path=document),
        source_schema=source_schema or rng_ref(),
        target_schema=target_schema,
        transform=normalize_transform(
            source if isinstance(source, dict) else {"kind": "python", "source": source},
            service.limits,
        ),
    )
    return await service.preview(workspace, request)


async def read(service, reference):
    return await service.outputs.read(reference.id, offset=0, length=1 << 20)


async def report_of(service, record):
    return json.loads(await read(service, record.report))


def test_relaxng_identity_transform_reports_the_candidate_bytes(tmp_path):
    async def scenario():
        async with harness(tmp_path, DEFAULT_FILES) as (service, workspace):
            record = await preview(
                service, workspace, "def transform(document, context):\n    return document\n"
            )

            assert record.valid is True
            assert record.well_formed is True
            assert await read(service, record.candidate) == STABLE_XML.encode()
            report = await report_of(service, record)
            assert report["counts"] == {"error": 0, "warning": 0}

    anyio.run(scenario)


def test_relaxng_xpath_transform_edits_original_typed_nodes(tmp_path):
    source = """def transform(document, context):
    for node in document.xpath('//item'):
        node.set_text(node.text_value().upper())
"""

    async def scenario():
        async with harness(tmp_path, DEFAULT_FILES) as (service, workspace):
            record = await preview(service, workspace, source)

            assert record.valid is True and record.well_formed is True
            candidate = await read(service, record.candidate)
            assert b">ONE<" in candidate and b">TWO<" in candidate
            assert (await report_of(service, record))["counts"]["error"] == 0

    anyio.run(scenario)


def test_relaxng_typed_descriptors_and_lexical_mutation_serialize(tmp_path):
    # Attribute descriptor assignment needs the normal public typed mode; a
    # validation-only parse would reject it before the candidate existed.
    source = """def transform(document, context):
    item = document.root.item[0]
    item.id = 9
    item.set_text('changed')
    return document.root
"""

    async def scenario():
        async with harness(tmp_path, DEFAULT_FILES) as (service, workspace):
            record = await preview(service, workspace, source)

            assert record.valid is True and record.well_formed is True
            assert await read(service, record.candidate) == (
                b'<doc kind="k"><item id="9">changed</item><item id="2">two</item></doc>'
            )

    anyio.run(scenario)


def test_relaxng_mixed_content_text_and_tail_mutation(tmp_path):
    files = {**DEFAULT_FILES, "mixed.rng": MIXED_RNG, "mixed.xml": MIXED_XML}
    source = """def transform(document, context):
    child = document.root.children[0]
    child.set_text('X')
    child.set_tail(' tail ')
"""

    async def scenario():
        async with harness(tmp_path, files) as (service, workspace):
            record = await preview(
                service,
                workspace,
                source,
                document="mixed.xml",
                source_schema=rng_ref("mixed.rng"),
            )

            assert record.valid is True and record.well_formed is True
            assert await read(service, record.candidate) == (
                b"<doc>lead<item>X</item> tail <item>two</item>end</doc>"
            )

    anyio.run(scenario)


def test_relaxng_root_return_is_wrapped_as_a_fresh_candidate(tmp_path):
    source = "def transform(document, context):\n    return document.root\n"

    async def scenario():
        async with harness(tmp_path, DEFAULT_FILES) as (service, workspace):
            record = await preview(service, workspace, source)

            assert record.valid is True and record.well_formed is True
            assert await read(service, record.candidate) == STABLE_XML.encode()

    anyio.run(scenario)


def test_relaxng_whole_document_return_replaces_the_candidate(tmp_path):
    source = """from io import StringIO
def transform(document, context):
    return document.schema.parse(
        StringIO('<doc kind="k"><item id="7">seven</item></doc>'), typed=True
    )
"""

    async def scenario():
        async with harness(tmp_path, DEFAULT_FILES) as (service, workspace):
            record = await preview(service, workspace, source)

            assert record.valid is True and record.well_formed is True
            assert await read(service, record.candidate) == (
                b'<doc kind="k"><item id="7">seven</item></doc>'
            )
            assert (await report_of(service, record))["counts"]["error"] == 0

    anyio.run(scenario)


@pytest.mark.parametrize("returned", ["{'value': 1}", "42"])
def test_relaxng_non_document_non_root_results_keep_the_existing_fault(tmp_path, returned):
    source = f"def transform(document, context):\n    return {returned}\n"

    async def scenario():
        async with harness(tmp_path, DEFAULT_FILES) as (service, workspace):
            with pytest.raises(ToolError) as raised:
                await preview(service, workspace, source)
            assert raised.value.fault.code == "invalid_transform_result"

    anyio.run(scenario)


def test_relaxng_xsd_root_result_is_not_a_root_for_an_xsd_document(tmp_path):
    files = {**DEFAULT_FILES, "xsd.xml": XSD_SHAPED_XML}
    source = """from xml.etree import ElementTree as ET
from pyxsd.relaxng import RelaxNGElement
def transform(document, context):
    return RelaxNGElement.from_etree(ET.fromstring('<root/>'))
"""

    async def scenario():
        async with harness(tmp_path, files) as (service, workspace):
            with pytest.raises(ToolError) as raised:
                await preview(
                    service,
                    workspace,
                    source,
                    document="xsd.xml",
                    source_schema=xsd_ref(),
                )
            assert raised.value.fault.code == "invalid_transform_result"

    anyio.run(scenario)


def test_relaxng_malformed_candidate_gets_no_structural_or_reused_report(tmp_path):
    source = """from pyxsd.relaxng.document import RelaxNGDocument
RelaxNGDocument.to_string = lambda self: '<doc><item>'
def transform(document, context):
    return document
"""

    async def scenario():
        async with harness(tmp_path, DEFAULT_FILES) as (service, workspace):
            record = await preview(service, workspace, source)

            assert record.valid is False and record.well_formed is False
            assert await read(service, record.candidate) == b"<doc><item>"
            report = await report_of(service, record)
            assert report["fault"]["code"] == "invalid_xml"
            assert "counts" not in report
            structural = json.loads(await read(service, record.structural_diff))
            assert structural == {
                "available": False,
                "reason": "Candidate is not well-formed XML",
            }

    anyio.run(scenario)


def test_relaxng_invalid_result_gets_a_fresh_report_not_the_source_verdict(tmp_path):
    # The source document is valid; renaming the child makes the candidate
    # invalid. Reusing any source-side report would report validity instead.
    source = """def transform(document, context):
    document.root.children[0].set_name('wrong')
"""

    async def scenario():
        async with harness(tmp_path, DEFAULT_FILES) as (service, workspace):
            record = await preview(service, workspace, source)

            assert record.valid is False and record.well_formed is True
            report = await report_of(service, record)
            assert report["counts"]["error"] >= 1
            assert any("wrong" in issue["message"] for issue in report["issues"])

    anyio.run(scenario)


def test_invalid_relaxng_source_can_be_repaired_with_a_fresh_report(tmp_path):
    files = {
        **DEFAULT_FILES,
        "document.xml": '<doc kind="k"><item id="bad">one</item></doc>',
    }
    source = """def transform(document, context):
    item = document.root.children[0]
    item.set_attribute('id', '4')
    item.set_text('fixed')
"""

    async def scenario():
        async with harness(tmp_path, files) as (service, workspace):
            record = await preview(service, workspace, source)

            assert record.valid is True and record.well_formed is True
            assert await read(service, record.candidate) == (
                b'<doc kind="k"><item id="4">fixed</item></doc>'
            )
            assert (await report_of(service, record))["counts"]["error"] == 0

    anyio.run(scenario)


def test_relaxng_checkpoint_revalidates_the_stale_root_result(tmp_path):
    recipe = {
        "kind": "pipeline",
        "steps": [{"name": "root", "checkpoint": True}, {"name": "edit"}],
        "definitions": {
            "root": "def transform(document, context):\n    return document.root\n",
            "edit": (
                "def transform(document, context):\n"
                "    document.root.children[0].set_text('checkpointed')\n"
            ),
        },
    }

    async def scenario():
        async with harness(tmp_path, DEFAULT_FILES) as (service, workspace):
            record = await preview(service, workspace, recipe)

            assert record.valid is True
            assert len(record.checkpoints) == 1
            checkpoint = record.checkpoints[0]
            assert checkpoint.valid is True and checkpoint.well_formed is True
            assert await read(service, checkpoint.candidate) == STABLE_XML.encode()
            checkpoint_report = json.loads(await read(service, checkpoint.report))
            assert checkpoint_report["counts"] == {"error": 0, "warning": 0}
            assert b"checkpointed" in await read(service, record.candidate)

    anyio.run(scenario)


def test_relaxng_source_to_xsd_target_uses_the_target_verdict(tmp_path):
    files = {
        **DEFAULT_FILES,
        "schema.rng": XSD_SHAPED_RNG,
        "document.xml": XSD_SHAPED_XML,
    }
    repair = "def transform(document, context):\n    document.root.children[0].set_text('7')\n"
    poison = "def transform(document, context):\n    document.root.children[0].set_text('nope')\n"

    async def scenario():
        async with harness(tmp_path, files) as (service, workspace):
            good = await preview(
                service,
                workspace,
                repair,
                source_schema=rng_ref(),
                target_schema=xsd_ref(),
            )
            assert good.valid is True and good.well_formed is True
            assert (await report_of(service, good))["counts"]["error"] == 0

            bad = await preview(
                service,
                workspace,
                poison,
                source_schema=rng_ref(),
                target_schema=xsd_ref(),
            )
            assert bad.valid is False and bad.well_formed is True
            bad_report = await report_of(service, bad)
            assert bad_report["counts"]["error"] >= 1
            assert any("integer" in issue["message"] for issue in bad_report["issues"]), bad_report

    anyio.run(scenario)


def test_xsd_source_to_relaxng_target_uses_the_target_verdict(tmp_path):
    files = {
        **DEFAULT_FILES,
        "schema.rng": XSD_SHAPED_RNG,
        "document.xml": "<root><item>3</item><item>4</item></root>",
    }

    async def scenario():
        async with harness(tmp_path, files) as (service, workspace):
            record = await preview(
                service,
                workspace,
                "def transform(document, context):\n    return document\n",
                source_schema=xsd_ref(),
                target_schema=rng_ref(),
            )

            assert record.valid is True and record.well_formed is True
            assert (await report_of(service, record))["counts"]["error"] == 0

    anyio.run(scenario)


def test_generic_relaxng_schema_preview_is_accepted(tmp_path):
    files = {
        **DEFAULT_FILES,
        "schema.rng": GENERIC_RNG,
        "document.xml": '<anything note="x">hello</anything>',
    }

    async def scenario():
        async with harness(tmp_path, files) as (service, workspace):
            record = await preview(
                service,
                workspace,
                "def transform(document, context):\n    return document.root\n",
            )

            assert record.valid is True and record.well_formed is True
            assert await read(service, record.candidate) == (b'<anything note="x">hello</anything>')

    anyio.run(scenario)


def test_apply_accepts_projected_root_and_returns_a_stale_document(tmp_path):
    schema = _compile_rng(STABLE_RNG)
    document = schema.parse(io.StringIO(STABLE_XML), typed=True)
    source_report = document.report

    assert type(document.root) is not RelaxNGElement
    assert isinstance(document.root, RelaxNGElement)

    result = _apply(
        document,
        "def transform(document, context):\n    return document.root\n",
        TransformContext("document.xml", 1, str(tmp_path)),
    )

    assert isinstance(result, RelaxNGDocument)
    assert result is not document
    assert result.root is document.root
    assert result.report is source_report
    assert result.interpretations_current is False

    fresh = result.revalidate()

    assert fresh.report is not source_report
    assert fresh.interpretations_current is True
    assert fresh.is_valid


def test_apply_accepts_a_generic_root_from_a_validation_only_document(tmp_path):
    schema = _compile_rng(GENERIC_RNG)
    document = schema.parse(io.StringIO('<anything note="x">hello</anything>'), typed=False)

    assert type(document.root) is RelaxNGElement

    result = _apply(
        document,
        "def transform(document, context):\n    return document.root\n",
        TransformContext("document.xml", 1, str(tmp_path)),
    )

    assert isinstance(result, RelaxNGDocument)
    assert result.root is document.root
    assert result.interpretations_current is False
    assert result.to_string() == '<anything note="x">hello</anything>'


def test_apply_rejects_non_root_results_on_relaxng_documents(tmp_path):
    schema = _compile_rng(STABLE_RNG)
    document = schema.parse(io.StringIO(STABLE_XML), typed=True)

    with pytest.raises(ToolError) as raised:
        _apply(
            document,
            "def transform(document, context):\n    return {'value': 1}",
            TransformContext("document.xml", 1, str(tmp_path)),
        )

    assert raised.value.fault.code == "invalid_transform_result"


def test_apply_rejects_an_xsd_root_on_a_relaxng_document(tmp_path):
    schema = _compile_rng(STABLE_RNG)
    document = schema.parse(io.StringIO(STABLE_XML), typed=True)
    xsd_schema = Schema.compile(io.StringIO(XSD_TEXT))
    xsd_root = xsd_schema.parse(io.StringIO(XSD_SHAPED_XML)).root

    with pytest.raises(ToolError) as raised:
        _apply(
            document,
            "def transform(document, context, root):\n    return root",
            TransformContext("document.xml", 1, str(tmp_path)),
            args=(xsd_root,),
        )

    assert raised.value.fault.code == "invalid_transform_result"


def test_apply_keeps_the_xsd_root_return_contract(tmp_path):
    xsd_schema = Schema.compile(io.StringIO(XSD_TEXT))
    document = xsd_schema.parse(io.StringIO(XSD_SHAPED_XML))
    root = document.root

    result = _apply(
        document,
        "def transform(document, context, root):\n    return root",
        TransformContext("document.xml", 1, str(tmp_path)),
        args=(root,),
    )

    assert result is document
    assert result.root is root


def test_apply_rejects_a_relaxng_root_on_an_xsd_document(tmp_path):
    xsd_schema = Schema.compile(io.StringIO(XSD_TEXT))
    document = xsd_schema.parse(io.StringIO(XSD_SHAPED_XML))
    schema = _compile_rng(GENERIC_RNG)
    relaxng_document = schema.parse(io.StringIO('<anything note="x">hello</anything>'), typed=False)

    with pytest.raises(ToolError) as raised:
        _apply(
            document,
            "def transform(document, context, root):\n    return root",
            TransformContext("document.xml", 1, str(tmp_path)),
            args=(relaxng_document.root,),
        )

    assert raised.value.fault.code == "invalid_transform_result"


def test_apply_allows_in_place_none_and_rejects_an_absent_root(tmp_path):
    schema = _compile_rng(STABLE_RNG)
    document = schema.parse(io.StringIO(STABLE_XML), typed=True)
    context = TransformContext("document.xml", 1, str(tmp_path))

    result = _apply(document, "def transform(document, context):\n    return None", context)

    assert result is document
    with pytest.raises(ToolError) as raised:
        _apply(document, "def transform(document, context):\n    document.root = None", context)
    assert raised.value.fault.code == "unbindable"


@pytest.mark.parametrize(
    "options, rejected",
    [
        ({"binding": "strict"}, "binding"),
        ({"namespaces": "legacy"}, "namespaces"),
        ({"xsd_version": "1.0"}, "xsd_version"),
        ({"namespace_schemas": {"urn:values": FileRef(path="extra.xsd")}}, "namespace_schemas"),
    ],
)
def test_relaxng_preview_rejects_nondefault_xsd_options(options, rejected):
    from pyxsd.mcp.contracts import CompileOptions
    from pyxsd.mcp.execution_main import _reject_relaxng_xsd_options

    reference = SchemaRef(
        source=FileRef(path="schema.rng"),
        options=CompileOptions(**options),
        format="relax-ng",
    )

    with pytest.raises(ToolError) as raised:
        _reject_relaxng_xsd_options(reference)

    assert raised.value.fault.code == "invalid_argument"
    assert raised.value.fault.details == {
        "format": "relax-ng",
        "rejected": [rejected],
    }


@pytest.mark.parametrize(
    "format, schema_file, schema_text, document_xml",
    [
        ("xsd", "schema.xsd", XSD_TEXT, XSD_SHAPED_XML),
        ("relax-ng", "schema.rng", XSD_SHAPED_RNG, XSD_SHAPED_XML),
    ],
)
def test_execution_main_dispatches_each_declared_schema_format(
    tmp_path, format, schema_file, schema_text, document_xml
):
    from pyxsd.mcp.captures import CaptureFile, InputCapture
    from pyxsd.mcp.execution import ExecutionRequest
    from pyxsd.mcp.execution_main import execute
    from pyxsd.mcp.transforms import PythonTransformSpec
    from pyxsd.mcp.workers import capture_to_wire

    capture_root = tmp_path / "capture"
    capture_root.mkdir()
    files = {schema_file: schema_text, "document.xml": document_xml}
    entries = []
    for name, text in files.items():
        data = text.encode()
        (capture_root / name).write_bytes(data)
        entries.append(CaptureFile(name, hashlib.sha256(data).hexdigest(), len(data)))
    capture = InputCapture("workspace", 1, capture_root, tuple(entries))
    output_directory = tmp_path / "output"
    output_directory.mkdir()
    request = ExecutionRequest(
        request_id="execution",
        capture=capture_to_wire(capture),
        schema_ref=SchemaRef(source=FileRef(path=schema_file), format=format),
        document=FileRef(path="document.xml"),
        transform=PythonTransformSpec(
            source="def transform(document, context):\n    return document\n"
        ),
        sources={},
        output_directory=str(output_directory),
        limits=Limits(enforced=True),
    )

    result = execute(request)

    assert result == {"checkpoints": []}
    candidate = (output_directory / "candidate.xml").read_bytes()
    assert b"<root" in candidate and b">3<" in candidate


def test_schema_aware_xslt_remains_xsd_only():
    from pyxsd.relaxng import compile as compile_relaxng
    from pyxsd.xslt.processor import XsltProcessor

    schema = compile_relaxng(io.StringIO(STABLE_RNG))

    with pytest.raises(TypeError, match="compiled Schema or SchemaPool"):
        XsltProcessor().compile(io.BytesIO(b"<not-a-stylesheet/>"), schema=schema)


def _compile_rng(source: str) -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(io.StringIO(source))
    assert not schema.report.has_errors, schema.report
    return schema
