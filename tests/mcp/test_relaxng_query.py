"""RELAX NG XPath queries return lexical nodes without XSD assumptions."""

import base64
import hashlib
import io
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_exploration import harness, upload

from pyxsd.mcp import worker_main
from pyxsd.mcp.cache import SchemaCache
from pyxsd.mcp.captures import CaptureFile, InputCapture
from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import FileRef, Page, QueryDocumentRequest, SchemaRef
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.worker_protocol import QueryOperation
from pyxsd.mcp.workers import capture_to_wire
from pyxsd.relaxng import RelaxNGDocument, RelaxNGElement, RelaxNGSchema
from pyxsd.resource_policy import ResourcePolicyError
from pyxsd.source_spans import SourceIndex
from pyxsd.validation import ValidationReport

NS = "http://relaxng.org/ns/structure/1.0"
XSDT = "http://www.w3.org/2001/XMLSchema-datatypes"

RNG_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="root">
      <oneOrMore>
        <element name="item">
          <attribute name="id"><data type="string" datatypeLibrary="{XSDT}"/></attribute>
          <mixed>
            <zeroOrMore><element name="em"><text/></element></zeroOrMore>
          </mixed>
        </element>
      </oneOrMore>
    </element>
  </start>
</grammar>"""

DOCUMENT = (
    '<root><item id="a">lead<em>inner</em>tail</item>'
    '<item id="b">plain</item>'
    '<item id="c"><em>only</em>end</item></root>'
)

NS_SCHEMA = f"""<grammar xmlns="{NS}" ns="urn:example">
  <start>
    <element name="root">
      <oneOrMore>
        <element name="item">
          <attribute name="id"><text/></attribute>
          <text/>
        </element>
      </oneOrMore>
    </element>
  </start>
</grammar>"""
NS_DOCUMENT = '<root xmlns="urn:example"><item id="a">alpha</item></root>'

XSD_SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="Item">
    <xs:sequence><xs:element name="value" type="xs:int"/></xs:sequence>
    <xs:attribute name="id" type="xs:string" use="required"/>
  </xs:complexType>
  <xs:element name="root">
    <xs:complexType><xs:sequence>
      <xs:element name="item" type="Item"/>
    </xs:sequence></xs:complexType>
  </xs:element>
</xs:schema>"""
XSD_DOCUMENT = '<root><item id="a"><value>1</value></item></root>'


def relaxng_ref(path="schema.rng"):
    return SchemaRef(source=FileRef(path=path), format="relax-ng")


def xsd_ref(path="schema.xsd"):
    return SchemaRef(source=FileRef(path=path))


def capture_of(root: Path, files: dict[str, str]) -> InputCapture:
    root.mkdir(parents=True, exist_ok=True)
    entries = []
    for name in sorted(files):
        data = files[name].encode()
        (root / name).write_bytes(data)
        entries.append(CaptureFile(name, hashlib.sha256(data).hexdigest(), len(data)))
    return InputCapture("workspace", 1, root, tuple(entries))


async def ask(service, workspace, *, xpath, result="summary", namespaces=None, page=None):
    return await service.query_document(
        QueryDocumentRequest(
            workspace_id=workspace,
            document=FileRef(path="document.xml"),
            schema_ref=relaxng_ref(),
            xpath=xpath,
            result=result,
            namespaces=namespaces or {},
            page=page or Page(),
        )
    )


def test_summary_uses_expanded_names_locations_and_lexical_text(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": RNG_SCHEMA, "document.xml": DOCUMENT},
            )
            envelope = await ask(service, workspace, xpath="/root/item")
            results = envelope.data["results"]
            assert [set(item) for item in results] == [
                {"name", "location", "text"},
                {"name", "location", "text"},
                {"name", "location", "text"},
            ]
            assert [item["name"] for item in results] == ["item", "item", "item"]
            assert [item["text"] for item in results] == ["lead", "plain", None]
            assert [item["location"]["path"][-1] for item in results] == [
                {"name": "item", "index": 1},
                {"name": "item", "index": 2},
                {"name": "item", "index": 3},
            ]
            assert {item["location"]["source"] for item in results} == {"/workspace/document.xml"}
            assert all("source_faithful" not in item for item in results)
            assert all("schema" not in item and "type" not in item for item in results)
            assert envelope.page.returned == 3
            assert envelope.page.total == 3

    anyio.run(scenario)


def test_subtree_returns_exact_source_bytes_without_xsd_annotations(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": RNG_SCHEMA, "document.xml": DOCUMENT},
            )
            envelope = await ask(
                service,
                workspace,
                xpath="/root/item[2]",
                result="subtree",
            )
            item = envelope.data["results"][0]
            assert set(item) == {
                "name",
                "location",
                "encoding",
                "data",
                "byte_length",
                "span",
                "source_faithful",
                "fault",
            }
            expected = b'<item id="b">plain</item>'
            start = DOCUMENT.encode().index(expected)
            assert base64.b64decode(item["data"]) == expected
            assert item["byte_length"] == len(expected)
            assert item["span"] == {"start": start, "end": start + len(expected)}
            assert item["source_faithful"] is True
            assert item["fault"] is None
            assert item["location"]["source"] == "/workspace/document.xml"
            assert item["location"]["path"][-1] == {"name": "item", "index": 2}

            root = await ask(service, workspace, xpath="/root", result="subtree")
            whole = root.data["results"][0]
            assert base64.b64decode(whole["data"]) == DOCUMENT.encode()
            assert whole["span"] == {"start": 0, "end": len(DOCUMENT)}

    anyio.run(scenario)


def test_attributes_scalars_and_mixed_content_are_lexical(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": RNG_SCHEMA, "document.xml": DOCUMENT},
            )
            attributes = await ask(service, workspace, xpath="/root/item/@id")
            assert attributes.data["results"] == ["a", "b", "c"]
            all_attributes = await ask(service, workspace, xpath="/root/item[1]/@*")
            assert all_attributes.data["results"] == ["a"]

            texts = await ask(service, workspace, xpath="/root/item/text()")
            assert texts.data["results"] == ["lead", "tail", "plain", "end"]
            nested = await ask(service, workspace, xpath="/root/item/em/text()")
            assert nested.data["results"] == ["inner", "only"]

            count = await ask(service, workspace, xpath="count(/root/item)")
            assert count.data["results"] == 3
            first = await ask(service, workspace, xpath="string(/root/item[1])")
            assert first.data["results"] == "leadinnertail"
            third = await ask(service, workspace, xpath="string(/root/item[3])")
            assert third.data["results"] == "onlyend"

            child = await ask(service, workspace, xpath="/root/item[1]/em")
            child_summary = child.data["results"][0]
            assert child_summary["text"] == "inner"
            assert child_summary["name"] == "em"
            assert child_summary["location"]["path"][-1] == {"name": "em", "index": 1}

    anyio.run(scenario)


def test_namespaced_relaxng_query_uses_expanded_names(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": NS_SCHEMA, "document.xml": NS_DOCUMENT},
            )
            summary = await ask(
                service,
                workspace,
                xpath="/r:root/r:item",
                namespaces={"r": "urn:example"},
            )
            item = summary.data["results"][0]
            assert item["name"] == "{urn:example}item"
            assert item["location"]["path"] == [
                {"name": "{urn:example}root", "index": 1},
                {"name": "{urn:example}item", "index": 1},
            ]
            assert item["text"] == "alpha"
            subtree = await ask(
                service,
                workspace,
                xpath="/r:root/r:item",
                result="subtree",
                namespaces={"r": "urn:example"},
            )
            assert base64.b64decode(subtree.data["results"][0]["data"]) == (
                b'<item id="a">alpha</item>'
            )

    anyio.run(scenario)


def test_invalid_relaxng_document_remains_queryable(tmp_path):
    document = '<root><item id="a">lead<em>inner</em>tail</item><wrong/></root>'

    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": RNG_SCHEMA, "document.xml": document},
            )
            summary = await ask(service, workspace, xpath="/root/item")
            item = summary.data["results"][0]
            assert item["text"] == "lead"
            assert item["location"]["path"][-1] == {"name": "item", "index": 1}
            scalar = await ask(service, workspace, xpath="string(/root/item)")
            assert scalar.data["results"] == "leadinnertail"

    anyio.run(scenario)


def test_query_uses_validation_only_parsing_without_evidence(tmp_path):
    """A typed parse would exhaust this budget; queries must stay lexical."""
    document = '<root><item id="a">one</item><item id="b">two</item></root>'

    async def scenario():
        limits = Limits(enforced=True, workers=1, relaxng_evidence_states=1)
        async with harness(tmp_path, limits) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": RNG_SCHEMA, "document.xml": document},
            )
            envelope = await ask(service, workspace, xpath="/root/item")
            assert [item["text"] for item in envelope.data["results"]] == ["one", "two"]
            scalar = await ask(service, workspace, xpath="string(/root/item[1])")
            assert scalar.data["results"] == "one"

    anyio.run(scenario)


def test_xsd_projected_nodes_keep_existing_query_shapes(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.xsd": XSD_SCHEMA, "document.xml": XSD_DOCUMENT},
            )
            summary = await service.query_document(
                QueryDocumentRequest(
                    workspace_id=workspace,
                    document=FileRef(path="document.xml"),
                    schema_ref=xsd_ref(),
                    xpath="/root/item",
                )
            )
            item = summary.data["results"][0]
            assert set(item) == {"name", "location", "text"}
            assert item["name"] == "item"
            assert item["location"]["path"][-1] == {"name": "item", "index": 1}
            assert item["location"]["source"] == "/workspace/document.xml"

            subtree = await service.query_document(
                QueryDocumentRequest(
                    workspace_id=workspace,
                    document=FileRef(path="document.xml"),
                    schema_ref=xsd_ref(),
                    xpath="/root/item",
                    result="subtree",
                )
            )
            result = subtree.data["results"][0]
            assert base64.b64decode(result["data"]) == (b'<item id="a"><value>1</value></item>')
            assert result["source_faithful"] is True
            assert result["fault"] is None

    anyio.run(scenario)


def test_query_results_page_through_cursor_and_budget(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": RNG_SCHEMA, "document.xml": DOCUMENT},
            )
            first = await ask(service, workspace, xpath="/root/item", page=Page(limit=2))
            assert [item["text"] for item in first.data["results"]] == ["lead", "plain"]
            assert first.page.returned == 2
            assert first.page.total == 3
            assert first.page.truncated is True
            assert first.page.next_cursor is not None
            second = await ask(
                service,
                workspace,
                xpath="/root/item",
                page=Page(cursor=first.page.next_cursor, limit=2),
            )
            assert [item["text"] for item in second.data["results"]] == [None]
            assert second.page.returned == 1
            assert second.page.truncated is False
            assert second.page.next_cursor is None
            with pytest.raises(ToolError, match="invalid_argument"):
                await ask(service, workspace, xpath="/root/item", page=Page(limit=51))

    anyio.run(scenario)


def test_projected_relaxng_nodes_are_structural():
    schema = RelaxNGSchema.compile(io.StringIO(RNG_SCHEMA))
    document = schema.parse(io.StringIO(DOCUMENT), typed=True)
    item = document.root.children[0]
    assert type(item) is not RelaxNGElement
    assert worker_main._structural_node(item)
    summary = worker_main._node_summary(document, item, None)
    assert summary["name"] == "item"
    assert summary["text"] == "lead"
    index = SourceIndex("/workspace/document.xml", DOCUMENT.encode())
    subtree = worker_main._node_summary(document, item, index)
    assert base64.b64decode(subtree["data"]) == (b'<item id="a">lead<em>inner</em>tail</item>')
    assert subtree["source_faithful"] is True


def test_source_unavailable_when_no_addressable_occurrence():
    schema = RelaxNGSchema.compile(io.StringIO(RNG_SCHEMA))
    detached = RelaxNGElement.from_etree(ET.fromstring("<item>lead</item>"))
    document = RelaxNGDocument(
        schema=schema,
        root=detached,
        source=None,
        report=ValidationReport(),
        typed=False,
    )
    summary = worker_main._node_summary(document, detached, None)
    assert summary == {"name": "item", "location": None, "text": "lead"}

    index = SourceIndex("/workspace/document.xml", b"<other/>")
    subtree = worker_main._node_summary(document, detached, index)
    assert subtree["location"] is None
    assert subtree["data"] is None
    assert subtree["byte_length"] is None
    assert subtree["span"] is None
    assert subtree["source_faithful"] is False
    assert subtree["fault"]["code"] == "source_unavailable"

    parsed = schema.parse(io.StringIO("<root><item>lead</item></root>"), typed=False)
    item = parsed.root.children[0]
    missing = worker_main._node_summary(parsed, item, index)
    assert missing["location"]["path"] == [
        {"name": "root", "index": 1},
        {"name": "item", "index": 1},
    ]
    assert missing["span"] is None
    assert missing["source_faithful"] is False
    assert missing["fault"]["code"] == "source_unavailable"


def test_source_indexing_uses_the_worker_budgets(tmp_path):
    capture = capture_of(
        tmp_path,
        {"schema.rng": RNG_SCHEMA, "document.xml": DOCUMENT},
    )
    operation = QueryOperation(
        request_id="query",
        capture=capture_to_wire(capture),
        schema_ref=relaxng_ref(),
        document=FileRef(path="document.xml"),
        xpath="/root/item",
        result="subtree",
    )
    cache = SchemaCache(Limits(enforced=True, xml_elements=2))
    try:
        with pytest.raises(ResourcePolicyError, match="max_elements"):
            worker_main._source_index(operation, cache)
    finally:
        cache.close()
