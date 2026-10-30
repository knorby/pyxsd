"""End-to-end MCP workflows from the integration-test matrix.

Each case exercises a complete user-facing workflow across the SDK client,
tool boundary, clean workers and provenance evidence. Assertions pin the
observable verdicts and the exact workspace fingerprint rather than internal
implementation details.
"""

import base64
import hashlib
import json

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_server import connected, structured

from pyxsd.mcp.contracts import FileRef, SchemaRef

EXPLORATION = {"exploration", "workspaces"}
FULL = {"exploration", "workspaces", "execution"}

UNFAMILIAR = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:complexType name="Item"><xs:sequence>
  <xs:element name="value" type="xs:int"/>
</xs:sequence><xs:attribute name="id" type="xs:ID" use="required"/></xs:complexType>
<xs:element name="root"><xs:complexType><xs:sequence>
  <xs:element name="item" type="Item" minOccurs="0" maxOccurs="unbounded"/>
</xs:sequence></xs:complexType></xs:element></xs:schema>"""

UNFAMILIAR_DOC = (
    '<root><item id="a"><value>1</value></item><item id="b"><value>2</value></item></root>'
)

NAMESPACED = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
targetNamespace="urn:example:catalog" xmlns:c="urn:example:catalog"
elementFormDefault="qualified">
<xs:complexType name="Item"><xs:sequence>
  <xs:element name="value" type="xs:int"/>
</xs:sequence><xs:attribute name="id" type="xs:ID" use="required"/></xs:complexType>
<xs:element name="catalog"><xs:complexType><xs:sequence>
  <xs:element name="item" type="c:Item" maxOccurs="unbounded"/>
</xs:sequence></xs:complexType></xs:element></xs:schema>"""

NAMESPACED_DOC = (
    '<c:catalog xmlns:c="urn:example:catalog">'
    '<c:item id="a"><c:value>1</c:value></c:item></c:catalog>'
)

REPEATED = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:complexType name="Item"><xs:sequence>
  <xs:element name="value" type="xs:int"/>
</xs:sequence><xs:attribute name="id" type="xs:string" use="required"/></xs:complexType>
<xs:element name="root"><xs:complexType><xs:sequence>
  <xs:element name="item" type="Item" minOccurs="0" maxOccurs="unbounded"/>
</xs:sequence></xs:complexType></xs:element></xs:schema>"""

REPEATED_DOC = (
    '<root><item id="a"><value>1</value></item>'
    "<item><value>bad</value></item>"
    '<item id="c"><value>3</value></item></root>'
)

REPAIR = """def transform(document, context):
    second = document.findall("./item")[1]
    second.id = "b"
    second.value = 2
    return document
"""

FACET = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
xmlns:cat="urn:example:catalog" targetNamespace="urn:example:catalog"
elementFormDefault="qualified">
<xs:simpleType name="Sku"><xs:restriction base="xs:string">
  <xs:pattern value="[A-Z]{3}-[0-9]{4}"/></xs:restriction></xs:simpleType>
<xs:element name="catalog"><xs:complexType><xs:sequence>
  <xs:element name="sku" type="cat:Sku"/>
</xs:sequence></xs:complexType></xs:element></xs:schema>"""

FACET_DOC = '<c:catalog xmlns:c="urn:example:catalog"><c:sku>nope</c:sku></c:catalog>'

MISSING_ATTRIBUTE_DOC = (
    '<root><item id="a"><value>1</value></item>'
    "<item><value>2</value></item>"
    '<item id="c"><value>3</value></item></root>'
)

IMPORT_ROOT = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:include schemaLocation="value.xsd"/>
<xs:element name="root" type="Value"/></xs:schema>"""

IMPORT_INT = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:simpleType name="Value"><xs:restriction base="xs:int"/></xs:simpleType></xs:schema>"""

IMPORT_STRING = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:simpleType name="Value"><xs:restriction base="xs:string"/></xs:simpleType></xs:schema>"""

SOURCE = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="root" type="xs:int"/></xs:schema>"""

TARGET = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="root" type="xs:string"/></xs:schema>"""

DECIMAL = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="order"><xs:complexType><xs:sequence>
<xs:element name="amount" type="xs:decimal"/>
<xs:element name="count" type="xs:integer"/>
</xs:sequence><xs:attribute name="id" type="xs:string" use="required"/>
</xs:complexType></xs:element></xs:schema>"""

RECORDS = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="root"><xs:complexType><xs:sequence>
<xs:element name="item" maxOccurs="unbounded"><xs:complexType><xs:sequence>
<xs:element name="amount" type="xs:decimal"/>
<xs:element name="payload" type="xs:base64Binary"/>
</xs:sequence><xs:attribute name="id" type="xs:string" use="required"/>
</xs:complexType></xs:element></xs:sequence>
<xs:attribute name="batch" type="xs:string"/>
</xs:complexType></xs:element></xs:schema>"""

RECORDS_DOC = (
    '<root batch="one">'
    + "".join(
        f'<item id="{index}"><amount>1.2300</amount><payload>AP8=</payload></item>'
        for index in range(3)
    )
    + "</root>"
)


async def _create(client):
    response = await client.call_tool("pyxsd_manage_workspace", {"action": "create"})
    assert response.is_error is False, structured(response)
    return structured(response)["data"]["id"]


async def _put(client, workspace, files):
    response = await client.call_tool(
        "pyxsd_put_files",
        {
            "workspace_id": workspace,
            "files": [{"path": path, "text": text} for path, text in files.items()],
        },
    )
    assert response.is_error is False, structured(response)
    return structured(response)["data"]["generation"]


async def _call(client, tool, arguments, *, error=False):
    response = await client.call_tool(tool, arguments)
    assert response.is_error is error, (tool, structured(response))
    return structured(response)


async def _expected_fingerprint(services, workspace, schema_path):
    from pyxsd.mcp.services.exploration import ExplorationService

    async with services.inputs.capture(workspace) as capture:
        schema = SchemaRef(source=FileRef(path=schema_path))
        return ExplorationService._fingerprint(capture, schema)


def test_unfamiliar_schema_overview_search_and_fingerprint(tmp_path):
    async def scenario():
        async with connected(tmp_path, enabled_groups=EXPLORATION) as (client, services):
            workspace = await _create(client)
            await _put(client, workspace, {"schema.xsd": UNFAMILIAR, "doc.xml": UNFAMILIAR_DOC})
            overview = await _call(
                client,
                "pyxsd_inspect_schema",
                {"workspace_id": workspace, "schema_ref": {"source": {"path": "schema.xsd"}}},
            )
            assert overview["data"]["roots"] == ["element:root"]
            assert overview["data"]["counts"]["type"] == 2
            assert "document" not in json.dumps(overview["data"])
            found = await _call(
                client,
                "pyxsd_find_schema_components",
                {
                    "workspace_id": workspace,
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                    "text": "value",
                },
            )
            components = found["data"]["components"]
            assert [item["kind"] for item in components] == ["element"]
            assert components[0]["name"] == "value"
            compact = await _call(
                client,
                "pyxsd_inspect_schema",
                {
                    "workspace_id": workspace,
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                    "view": "compact",
                },
            )
            full = await _call(
                client,
                "pyxsd_inspect_schema",
                {
                    "workspace_id": workspace,
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                    "view": "full",
                },
            )
            assert len(full["data"]["stub"]) > len(compact["data"]["stub"])
            validated = await _call(
                client,
                "pyxsd_validate_document",
                {
                    "workspace_id": workspace,
                    "document": {"path": "doc.xml"},
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                },
            )
            expected = await _expected_fingerprint(services, workspace, "schema.xsd")
            assert validated["data"]["valid"] is True
            assert validated["provenance"]["schema_fingerprint"] == expected
            again = await _call(
                client,
                "pyxsd_validate_document",
                {
                    "workspace_id": workspace,
                    "document": {"path": "doc.xml"},
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                },
            )
            assert again["provenance"] == validated["provenance"]

    anyio.run(scenario)


def test_namespace_collision_uses_expanded_names(tmp_path):
    async def scenario():
        async with connected(tmp_path, enabled_groups=EXPLORATION) as (client, _services):
            workspace = await _create(client)
            await _put(client, workspace, {"schema.xsd": NAMESPACED, "doc.xml": NAMESPACED_DOC})
            validated = await _call(
                client,
                "pyxsd_validate_document",
                {
                    "workspace_id": workspace,
                    "document": {"path": "doc.xml"},
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                },
            )
            assert validated["data"]["valid"] is True
            queried = await _call(
                client,
                "pyxsd_query_document",
                {
                    "workspace_id": workspace,
                    "document": {"path": "doc.xml"},
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                    "xpath": "/c:catalog/c:item/c:value",
                    "namespaces": {"c": "urn:example:catalog"},
                },
            )
            assert queried["data"]["results"] == [1]
            wrong_prefix = await client.call_tool(
                "pyxsd_query_document",
                {
                    "workspace_id": workspace,
                    "document": {"path": "doc.xml"},
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                    "xpath": "/q:catalog/q:item",
                },
            )
            assert wrong_prefix.is_error is True
            assert structured(wrong_prefix)["data"]["fault"]["code"] == "invalid_query"

    anyio.run(scenario)


def test_repeated_node_repair_returns_exact_candidate(tmp_path):
    async def scenario():
        async with connected(tmp_path, enabled_groups=FULL, execution_backend="trusted-local") as (
            client,
            _services,
        ):
            workspace = await _create(client)
            await _put(client, workspace, {"schema.xsd": REPEATED, "doc.xml": REPEATED_DOC})
            validated = await _call(
                client,
                "pyxsd_validate_document",
                {
                    "workspace_id": workspace,
                    "document": {"path": "doc.xml"},
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                },
            )
            assert validated["data"]["valid"] is False
            assert validated["data"]["report"]["counts"]["error"] == 2
            occurred = await _call(
                client,
                "pyxsd_query_document",
                {
                    "workspace_id": workspace,
                    "document": {"path": "doc.xml"},
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                    "xpath": "/root/item[2]",
                    "result": "subtree",
                },
            )
            assert occurred["data"]["results"][0]["location"]["path"][-1]["index"] == 2
            assert (
                base64.b64decode(occurred["data"]["results"][0]["data"])
                == b"<item><value>bad</value></item>"
            )
            submitted = await _call(
                client,
                "pyxsd_preview_transform",
                {
                    "workspace_id": workspace,
                    "document": {"path": "doc.xml"},
                    "source_schema": {"source": {"path": "schema.xsd"}},
                    "transform": {"kind": "python", "source": REPAIR},
                },
            )
            record = submitted["data"]
            assert record["valid"] is True
            content = await _call(
                client,
                "pyxsd_read_artifact",
                {
                    "artifact_id": record["candidate"]["id"],
                    "offset": 0,
                    "length": record["candidate"]["size"],
                },
            )
            assert (
                hashlib.sha256(base64.b64decode(content["data"]["data"])).hexdigest()
                == (record["candidate"]["digest"])
            )
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "pyxsd_commit_transform" not in names

    anyio.run(scenario)


def test_facet_failure_reports_actual_constraint_evidence(tmp_path):
    async def scenario():
        async with connected(tmp_path, enabled_groups=EXPLORATION) as (client, _services):
            workspace = await _create(client)
            await _put(client, workspace, {"schema.xsd": FACET, "doc.xml": FACET_DOC})
            validated = await _call(
                client,
                "pyxsd_validate_document",
                {
                    "workspace_id": workspace,
                    "document": {"path": "doc.xml"},
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                },
            )
            assert validated["data"]["valid"] is False
            issue = validated["data"]["report"]["issues"][0]
            assert issue["code"] == "value"
            assert issue["constraint"]["kind"] == "pattern"
            assert issue["constraint"]["actual"] == "nope"
            assert "[A-Z]{3}-[0-9]{4}" in issue["constraint"]["expected"][0]
            explained = await _call(
                client,
                "pyxsd_explain_issue",
                {"code": issue["code"]},
            )
            assert explained["data"]["known"] is True
            assert explained["data"]["known"] is True

    anyio.run(scenario)


def test_missing_attribute_points_at_the_one_occurrence(tmp_path):
    async def scenario():
        async with connected(tmp_path, enabled_groups=EXPLORATION) as (client, _services):
            workspace = await _create(client)
            await _put(
                client, workspace, {"schema.xsd": UNFAMILIAR, "doc.xml": MISSING_ATTRIBUTE_DOC}
            )
            validated = await _call(
                client,
                "pyxsd_validate_document",
                {
                    "workspace_id": workspace,
                    "document": {"path": "doc.xml"},
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                },
            )
            assert validated["data"]["valid"] is False
            issues = validated["data"]["report"]["issues"]
            assert {item["code"] for item in issues} == {"missing-attribute"}
            assert issues[0]["location"]["path"][-1]["index"] == 2
            located = await _call(
                client,
                "pyxsd_query_document",
                {
                    "workspace_id": workspace,
                    "document": {"path": "doc.xml"},
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                    "xpath": "/root/item[2]",
                },
            )
            assert located["data"]["results"][0]["location"]["path"][-1]["index"] == 2

    anyio.run(scenario)


def test_invalid_schema_never_yields_an_instance_verdict(tmp_path):
    async def scenario():
        async with connected(tmp_path, enabled_groups=EXPLORATION) as (client, _services):
            workspace = await _create(client)
            broken = (
                '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"><xs:element name="root"'
            )
            await _put(client, workspace, {"schema.xsd": broken, "doc.xml": "<root/>"})
            inspected = await _call(
                client,
                "pyxsd_inspect_schema",
                {"workspace_id": workspace, "schema_ref": {"source": {"path": "schema.xsd"}}},
                error=True,
            )
            assert inspected["status"] == "error"
            assert inspected["data"]["fault"]["code"] == "invalid_schema"
            failed = await _call(
                client,
                "pyxsd_validate_document",
                {
                    "workspace_id": workspace,
                    "document": {"path": "doc.xml"},
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                },
                error=True,
            )
            assert failed["status"] == "error"
            assert "valid" not in failed["data"]

    anyio.run(scenario)


def test_changed_import_recomputes_fingerprint_and_verdict(tmp_path):
    async def scenario():
        async with connected(tmp_path, enabled_groups=EXPLORATION) as (client, services):
            workspace = await _create(client)
            generation = await _put(
                client,
                workspace,
                {"schema.xsd": IMPORT_ROOT, "value.xsd": IMPORT_INT, "doc.xml": "<root>abc</root>"},
            )
            first = await _call(
                client,
                "pyxsd_validate_document",
                {
                    "workspace_id": workspace,
                    "document": {"path": "doc.xml"},
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                },
            )
            assert first["data"]["valid"] is False
            before = first["provenance"]["schema_fingerprint"]
            assert before == await _expected_fingerprint(services, workspace, "schema.xsd")
            changed = await _put(client, workspace, {"value.xsd": IMPORT_STRING})
            assert changed != generation
            second = await _call(
                client,
                "pyxsd_validate_document",
                {
                    "workspace_id": workspace,
                    "document": {"path": "doc.xml"},
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                },
            )
            assert second["data"]["valid"] is True
            assert second["provenance"]["generation"] == changed
            after = second["provenance"]["schema_fingerprint"]
            assert after == await _expected_fingerprint(services, workspace, "schema.xsd")
            assert after != before

    anyio.run(scenario)


def test_cross_schema_migration_previews_against_target(tmp_path):
    async def scenario():
        async with connected(tmp_path, enabled_groups=FULL, execution_backend="trusted-local") as (
            client,
            _services,
        ):
            workspace = await _create(client)
            await _put(
                client,
                workspace,
                {"old.xsd": SOURCE, "new.xsd": TARGET, "doc.xml": "<root>abc</root>"},
            )
            compared = await _call(
                client,
                "pyxsd_compare_schemas",
                {
                    "workspace_id": workspace,
                    "old_schema": {"source": {"path": "old.xsd"}},
                    "new_schema": {"source": {"path": "new.xsd"}},
                    "corpus": [{"path": "doc.xml"}],
                },
            )
            comparison = compared["data"]
            corpus = comparison["corpus"]
            assert corpus["resolutions"] == ["doc.xml"]
            assert corpus["totals"]["resolved"] == 1
            submitted = await _call(
                client,
                "pyxsd_preview_transform",
                {
                    "workspace_id": workspace,
                    "document": {"path": "doc.xml"},
                    "source_schema": {"source": {"path": "old.xsd"}},
                    "target_schema": {"source": {"path": "new.xsd"}},
                    "transform": {
                        "kind": "python",
                        "source": "def transform(document, context):\n    return document\n",
                    },
                },
            )
            assert submitted["data"]["valid"] is True
            assert comparison["conclusions"]["corpus_only"] is True

    anyio.run(scenario)


def test_pydantic_decimal_projection_preserves_exact_values(tmp_path):
    async def scenario():
        async with connected(tmp_path, enabled_groups=EXPLORATION) as (client, _services):
            workspace = await _create(client)
            await _put(client, workspace, {"schema.xsd": DECIMAL})
            inspected = await _call(
                client,
                "pyxsd_inspect_model",
                {
                    "workspace_id": workspace,
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                    "element": "order",
                },
            )
            contract = inspected["data"]["json_schema"]
            assert inspected["data"]["validation_scope"] == "projected-data"
            assert "amount" in contract["properties"]
            huge = 10**80 + 7
            validated = await _call(
                client,
                "pyxsd_validate_data",
                {
                    "workspace_id": workspace,
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                    "element": "order",
                    "data": {"@id": "one", "amount": "1.2300", "count": huge},
                },
            )
            assert validated["data"]["valid"] is True
            assert validated["data"]["value"]["amount"] == "1.2300"
            assert validated["data"]["value"]["count"] == str(huge)
            invalid = await _call(
                client,
                "pyxsd_validate_data",
                {
                    "workspace_id": workspace,
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                    "element": "order",
                    "data": {"@id": "one", "amount": "not-a-decimal", "count": 1},
                },
            )
            assert invalid["data"]["valid"] is False
            assert "XML" in invalid["warnings"][0]

    anyio.run(scenario)


def test_contextual_arrow_export_is_bounded_and_digest_verified(tmp_path):
    pytest.importorskip("pyarrow")

    async def scenario():
        async with connected(
            tmp_path,
            enabled_groups=FULL,
            arrow="enabled",
            execution_backend="trusted-local",
        ) as (client, _services):
            workspace = await _create(client)
            await _put(client, workspace, {"schema.xsd": RECORDS, "doc.xml": RECORDS_DOC})
            preview = await _call(
                client,
                "pyxsd_preview_records",
                {
                    "workspace_id": workspace,
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                    "document": {"path": "doc.xml"},
                    "element": "root",
                    "path": ["item"],
                    "selector": "./item",
                    "columns": {
                        "id": {"attribute": "id"},
                        "batch": {"scope": "root", "attribute": "batch"},
                    },
                },
            )
            assert preview["data"]["rows"] == [
                {"id": "0", "batch": "one"},
                {"id": "1", "batch": "one"},
                {"id": "2", "batch": "one"},
            ]
            assert preview["data"]["row_count"] == 3
            submitted = await _call(
                client,
                "pyxsd_export_records",
                {
                    "workspace_id": workspace,
                    "schema_ref": {"source": {"path": "schema.xsd"}},
                    "documents": [{"path": "doc.xml"}],
                    "element": "root",
                    "path": ["item"],
                    "selector": "./item",
                    "columns": {
                        "id": {"attribute": "id"},
                        "batch": {"scope": "root", "attribute": "batch"},
                    },
                },
            )
            export = submitted["data"]
            assert export["row_count"] == 3
            entry = next(item for item in export["artifacts"] if item["path"] == "export.parquet")
            downloaded = await _call(
                client,
                "pyxsd_read_artifact",
                {"artifact_id": entry["id"], "offset": 0, "length": entry["size"]},
            )
            content = base64.b64decode(downloaded["data"]["data"])
            assert content[:4] == b"PAR1"
            assert hashlib.sha256(content).hexdigest() == entry["digest"]

    anyio.run(scenario)
