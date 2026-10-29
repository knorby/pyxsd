"""Schema comparison separates structural impact from observed corpus evidence."""

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_exploration import harness, upload

from pyxsd.mcp.contracts import FileRef, SchemaRef
from pyxsd.mcp.errors import ToolError

OLD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="root" type="Root"/>
<xs:element name="legacy" type="xs:string"/>
<xs:complexType name="Root"><xs:sequence>
<xs:element ref="legacy" minOccurs="0"/>
<xs:element name="item" type="Item" maxOccurs="unbounded"/>
</xs:sequence></xs:complexType>
<xs:complexType name="Item"><xs:sequence>
<xs:element name="amount" type="Amount"/>
</xs:sequence></xs:complexType>
<xs:simpleType name="Amount">
<xs:restriction base="xs:decimal"><xs:totalDigits value="8"/></xs:restriction>
</xs:simpleType>
</xs:schema>"""

NEW = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="root" type="Root"/>
<xs:element name="added" type="xs:string"/>
<xs:complexType name="Root"><xs:sequence>
<xs:element name="item" type="Item" maxOccurs="unbounded"/>
<xs:element name="fresh" type="xs:string" minOccurs="0"/>
</xs:sequence></xs:complexType>
<xs:complexType name="Item"><xs:sequence>
<xs:element name="amount" type="Amount"/>
</xs:sequence></xs:complexType>
<xs:simpleType name="Amount">
<xs:restriction base="xs:decimal"><xs:totalDigits value="6"/></xs:restriction>
</xs:simpleType>
</xs:schema>"""

OLD_ANON = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="anon"><xs:complexType><xs:sequence>
<xs:element name="a" type="xs:int"/>
</xs:sequence></xs:complexType></xs:element>
</xs:schema>"""

NEW_ANON = OLD_ANON.replace('type="xs:int"', 'type="xs:string"')

OLD_NS = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
    'targetNamespace="http://old.example" xmlns:o="http://old.example">'
    '<xs:element name="root" type="o:Root"/>'
    '<xs:complexType name="Root"><xs:sequence/></xs:complexType>'
    "</xs:schema>"
)
NEW_NS = OLD_NS.replace("old.example", "new.example")

GOOD = "<root><item><amount>123.45</amount></item></root>"
REGRESSED = "<root><item><amount>12345678</amount></item></root>"
RESOLVED = "<root><item><amount>123.45</amount></item><fresh>x</fresh></root>"
INVALID_BOTH = "<root><item><amount>abc</amount></item></root>"
MALFORMED = "<root><item>"
UNBINDABLE = "<other/>"


def _compare_request(workspace, **overrides):
    from pyxsd.mcp.services.comparison import CompareSchemasRequest

    fields = {
        "workspace_id": workspace,
        "old_schema": SchemaRef(source=FileRef(path="old.xsd")),
        "new_schema": SchemaRef(source=FileRef(path="new.xsd")),
    }
    fields.update(overrides)
    return CompareSchemasRequest(**fields)


def test_structural_diff_added_removed_changed_and_deterministic(tmp_path):
    from pyxsd.mcp.services.comparison import ComparisonService

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"old.xsd": OLD, "new.xsd": NEW})
            service = ComparisonService(inputs, pool)
            result = await service.compare(_compare_request(workspace))
            structural = result["structural"]
            assert "element:added" in structural["added"]
            assert "element:legacy" in structural["removed"]
            # Named local declarations keep owner-chain identities: only the
            # actually-added/removed locals churn, not their siblings.
            assert len(structural["added"]) == 2
            assert len(structural["removed"]) == 2
            assert any(str(item).startswith("element@") for item in structural["added"])
            assert any(str(item).startswith("element@") for item in structural["removed"])
            assert set(structural["changed"]) == {
                "element:root",
                "type:Amount",
                "type:Item",
                "type:Root",
                # The item/amount locals embed the changed facets via their
                # declaring types: conservative potential-impact propagation.
                *(i for i in structural["changed"] if str(i).startswith("element@")),
            }
            assert len([i for i in structural["changed"] if str(i).startswith("element@")]) == 2
            assert structural["namespace_changed"] is False
            assert structural["totals"] == {"added": 2, "removed": 2, "changed": 6}
            details = {entry["id"]: entry["changes"] for entry in structural["changed_details"]}
            facet_change = next(
                change for change in details["type:Amount"] if change["field"] == "facets"
            )
            assert facet_change["old"] != facet_change["new"]
            repeat = await service.compare(_compare_request(workspace))
            assert repeat["structural"] == structural

    anyio.run(scenario)


def test_anonymous_declarations_diff_by_content_addressed_identity(tmp_path):
    from pyxsd.mcp.services.comparison import ComparisonService

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"old.xsd": OLD_ANON, "new.xsd": NEW_ANON})
            service = ComparisonService(inputs, pool)
            result = await service.compare(
                _compare_request(
                    workspace,
                    old_schema=SchemaRef(source=FileRef(path="old.xsd")),
                    new_schema=SchemaRef(source=FileRef(path="new.xsd")),
                ),
            )
            structural = result["structural"]
            assert any(identifier.startswith("type@") for identifier in structural["removed"])
            assert any(identifier.startswith("type@") for identifier in structural["added"])

    anyio.run(scenario)


def test_namespace_change_reports_flag_and_reidentified_components(tmp_path):
    from pyxsd.mcp.services.comparison import ComparisonService

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"old.xsd": OLD_NS, "new.xsd": NEW_NS})
            service = ComparisonService(inputs, pool)
            result = await service.compare(
                _compare_request(
                    workspace,
                    old_schema=SchemaRef(source=FileRef(path="old.xsd")),
                    new_schema=SchemaRef(source=FileRef(path="new.xsd")),
                ),
            )
            structural = result["structural"]
            assert structural["namespace_changed"] is True
            assert "element:{http://new.example}root" in structural["added"]
            assert "element:{http://old.example}root" in structural["removed"]

    anyio.run(scenario)


def test_corpus_classifies_every_file_and_separates_impact_from_evidence(tmp_path):
    from pyxsd.mcp.services.comparison import ComparisonService

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "old.xsd": OLD,
                    "new.xsd": NEW,
                    "good.xml": GOOD,
                    "regressed.xml": REGRESSED,
                    "resolved.xml": RESOLVED,
                    "invalid.xml": INVALID_BOTH,
                    "malformed.xml": MALFORMED,
                    "unbindable.xml": UNBINDABLE,
                },
            )
            service = ComparisonService(inputs, pool)
            corpus = tuple(
                FileRef(path=name)
                for name in (
                    "good.xml",
                    "regressed.xml",
                    "resolved.xml",
                    "invalid.xml",
                    "malformed.xml",
                    "unbindable.xml",
                )
            )
            result = await service.compare(_compare_request(workspace, corpus=corpus))
            corpus_result = result["corpus"]
            statuses = {entry["path"]: entry["status"] for entry in corpus_result["files"]}
            assert statuses == {
                "good.xml": "valid",
                "regressed.xml": "regressed",
                "resolved.xml": "resolved",
                "invalid.xml": "invalid",
                "malformed.xml": "malformed",
                "unbindable.xml": "unbindable",
            }
            assert corpus_result["totals"] == {
                "valid": 1,
                "regressed": 1,
                "resolved": 1,
                "invalid": 1,
                "malformed": 1,
                "unbindable": 1,
                "skipped": 0,
            }
            assert corpus_result["regressions"] == ["regressed.xml"]
            assert corpus_result["resolutions"] == ["resolved.xml"]
            conclusions = result["conclusions"]
            assert conclusions["corpus_only"] is True
            assert "No universal compatibility" in conclusions["statement"]
            assert result["structural"]["totals"]["added"] == 2

    anyio.run(scenario)


def test_corpus_cap_is_enforced(tmp_path):
    from pyxsd.mcp.config import Limits
    from pyxsd.mcp.services.comparison import ComparisonService

    async def scenario():
        async with harness(tmp_path, Limits(workers=1, corpus_files=2)) as (
            _,
            inputs,
            pool,
            workspace,
        ):
            await upload(
                inputs,
                workspace,
                {
                    "old.xsd": OLD,
                    "new.xsd": NEW,
                    "a.xml": GOOD,
                    "b.xml": GOOD,
                    "c.xml": GOOD,
                },
            )
            service = ComparisonService(inputs, pool)
            with pytest.raises(ToolError, match="quota_exceeded"):
                await service.compare(
                    _compare_request(
                        workspace,
                        corpus=tuple(FileRef(path=name) for name in ("a.xml", "b.xml", "c.xml")),
                    ),
                )

    anyio.run(scenario)


def test_corpus_timeout_skips_remaining_files(tmp_path):
    from pyxsd.mcp.cache import SchemaCache
    from pyxsd.mcp.config import Limits
    from pyxsd.mcp.services import comparison as comparison_module
    from pyxsd.mcp.worker_protocol import ComparisonOperation
    from pyxsd.mcp.workers import capture_to_wire

    async def scenario():
        async with harness(tmp_path, Limits(workers=1, corpus_file_seconds=1)) as (
            _,
            inputs,
            _pool,
            workspace,
        ):
            await upload(
                inputs,
                workspace,
                {
                    "old.xsd": OLD,
                    "new.xsd": NEW,
                    "good.xml": GOOD,
                },
            )
            async with inputs.capture(workspace) as capture:
                operation = ComparisonOperation(
                    request_id="timeout",
                    capture=capture_to_wire(capture),
                    old_schema=SchemaRef(source=FileRef(path="old.xsd")),
                    new_schema=SchemaRef(source=FileRef(path="new.xsd")),
                    corpus=(FileRef(path="good.xml"),),
                )
                cache = SchemaCache(inputs.limits)
                try:
                    ticks = iter([0.0, 100.0, 100.0, 100.0])
                    result = comparison_module.execute_comparison(
                        operation, cache, "test", clock=lambda: next(ticks)
                    )
                finally:
                    cache.close()
            assert result["corpus"]["files"] == [
                {"path": "good.xml", "status": "skipped", "old": "skipped", "new": "skipped"}
            ]
            assert result["corpus"]["totals"]["skipped"] == 1

    anyio.run(scenario)


def test_comparison_rejects_worker_faults_and_non_object_results(tmp_path, monkeypatch):
    from pyxsd.mcp.contracts import ToolFault
    from pyxsd.mcp.services.comparison import ComparisonService
    from pyxsd.mcp.worker_protocol import WorkerReply

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"old.xsd": OLD, "new.xsd": NEW})
            service = ComparisonService(inputs, pool)

            async def failed(operation, *, deadline):
                return WorkerReply(
                    request_id=operation.request_id,
                    role="clean",
                    status="error",
                    fault=ToolFault(code="worker_died", message="No verdict"),
                )

            monkeypatch.setattr(service.pool, "run", failed)
            with pytest.raises(ToolError, match="worker_died"):
                await service.compare(_compare_request(workspace))

            async def not_an_object(operation, *, deadline):
                return WorkerReply(
                    request_id=operation.request_id, role="clean", status="ok", result=[]
                )

            monkeypatch.setattr(service.pool, "run", not_an_object)
            with pytest.raises(ToolError, match="ipc_protocol"):
                await service.compare(_compare_request(workspace))

    anyio.run(scenario)


def test_compare_tool_works_through_the_sdk_server(tmp_path):
    from tests.mcp.test_server import connected, create_workspace, structured

    async def scenario():
        async with connected(
            tmp_path, enabled_groups={"exploration", "workspaces", "execution"}
        ) as (client, _services):
            workspace = await create_workspace(client)
            await client.call_tool(
                "pyxsd_put_files",
                {
                    "workspace_id": workspace,
                    "files": [
                        {"path": "old.xsd", "text": OLD},
                        {"path": "new.xsd", "text": NEW},
                        {"path": "good.xml", "text": GOOD},
                    ],
                },
            )
            submitted = await client.call_tool(
                "pyxsd_compare_schemas",
                {
                    "workspace_id": workspace,
                    "old_schema": {"source": {"path": "old.xsd"}},
                    "new_schema": {"source": {"path": "new.xsd"}},
                    "corpus": [{"path": "good.xml"}],
                },
            )
            result = structured(submitted)["data"]
            assert "element:added" in result["structural"]["added"]
            assert result["corpus"]["files"][0]["status"] == "valid"
            assert result["conclusions"]["corpus_only"] is True

    anyio.run(scenario)


IDENTICAL = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="root" type="Root"/>
<xs:complexType name="Root"><xs:sequence>
<xs:element name="item" type="Item"/>
</xs:sequence></xs:complexType>
<xs:complexType name="Item">
<xs:attribute name="id" type="xs:ID" use="required"/>
</xs:complexType>
</xs:schema>"""

MOVED_MAIN = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:m">
<xs:include schemaLocation="inc.xsd"/>
<xs:element name="main"><xs:complexType><xs:sequence>
<xs:element ref="m:inc" xmlns:m="urn:m"/>
</xs:sequence></xs:complexType></xs:element>
</xs:schema>"""

MOVED_INC = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:m">
<xs:element name="inc"><xs:complexType><xs:sequence>
<xs:element name="a" type="xs:string"/>
</xs:sequence></xs:complexType></xs:element>
</xs:schema>"""

FACET_OLD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="rated" type="Rating"/>
<xs:simpleType name="Rating"><xs:restriction base="xs:int">
<xs:maxInclusive value="5"/>
</xs:restriction></xs:simpleType>
</xs:schema>"""
FACET_NEW = FACET_OLD.replace('value="5"', 'value="4"')

CHILD_OLD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="root" type="Root"/>
<xs:complexType name="Root"><xs:sequence>
<xs:element name="item" type="xs:int"/>
<xs:element name="note" type="xs:string"/>
</xs:sequence></xs:complexType>
</xs:schema>"""
CHILD_REMOVED = CHILD_OLD.replace('<xs:element name="note" type="xs:string"/>\n', "")
CHILD_INSERTED = CHILD_OLD.replace(
    '<xs:element name="item" type="xs:int"/>',
    '<xs:element name="item" type="xs:int"/>\n<xs:element name="extra" type="xs:string"/>',
)


def test_identical_bytes_different_paths_empty_diff(tmp_path):
    from pyxsd.mcp.services.comparison import ComparisonService

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {"old/a.xsd": IDENTICAL, "new/b.xsd": IDENTICAL},
            )
            service = ComparisonService(inputs, pool)
            result = await service.compare(
                _compare_request(
                    workspace,
                    old_schema=SchemaRef(source=FileRef(path="old/a.xsd")),
                    new_schema=SchemaRef(source=FileRef(path="new/b.xsd")),
                )
            )
            structural = result["structural"]
            assert structural["totals"] == {"added": 0, "removed": 0, "changed": 0}
            assert structural["changed_details"] == []

    anyio.run(scenario)


def test_identical_directory_with_includes_moves_cleanly(tmp_path):
    from pyxsd.mcp.services.comparison import ComparisonService

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "old/main.xsd": MOVED_MAIN,
                    "old/inc.xsd": MOVED_INC,
                    "new/main.xsd": MOVED_MAIN,
                    "new/inc.xsd": MOVED_INC,
                },
            )
            service = ComparisonService(inputs, pool)
            result = await service.compare(
                _compare_request(
                    workspace,
                    old_schema=SchemaRef(source=FileRef(path="old/main.xsd")),
                    new_schema=SchemaRef(source=FileRef(path="new/main.xsd")),
                )
            )
            assert result["structural"]["totals"] == {
                "added": 0,
                "removed": 0,
                "changed": 0,
            }

    anyio.run(scenario)


def test_facet_edit_reports_old_and_new_values(tmp_path):
    from pyxsd.mcp.services.comparison import ComparisonService

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"old.xsd": FACET_OLD, "new.xsd": FACET_NEW})
            service = ComparisonService(inputs, pool)
            result = await service.compare(_compare_request(workspace))
            structural = result["structural"]
            assert structural["totals"] == {"added": 0, "removed": 0, "changed": 2}
            # The type itself plus the element that embeds its facets.
            details = {entry["id"]: entry["changes"] for entry in structural["changed_details"]}
            (change,) = details["type:Rating"]
            assert change["field"] == "facets"
            assert change["old"] != change["new"]

    anyio.run(scenario)


def test_child_removal_names_only_the_removed_declaration(tmp_path):
    from pyxsd.mcp.services.comparison import ComparisonService

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"old.xsd": CHILD_OLD, "new.xsd": CHILD_REMOVED})
            service = ComparisonService(inputs, pool)
            result = await service.compare(_compare_request(workspace))
            structural = result["structural"]
            assert structural["added"] == []
            assert len(structural["removed"]) == 1
            (removed,) = structural["removed"]
            assert str(removed).startswith("element@")
            # Only the containing type and the root element embedding it change.
            assert set(structural["changed"]) == {"element:root", "type:Root"}

    anyio.run(scenario)


def test_named_sibling_insert_keeps_siblings_stable(tmp_path):
    from pyxsd.mcp.services.comparison import ComparisonService

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"old.xsd": CHILD_OLD, "new.xsd": CHILD_INSERTED})
            service = ComparisonService(inputs, pool)
            result = await service.compare(_compare_request(workspace))
            structural = result["structural"]
            assert structural["removed"] == []
            assert len(structural["added"]) == 1
            (added,) = structural["added"]
            assert str(added).startswith("element@")
            assert set(structural["changed"]) == {"element:root", "type:Root"}

    anyio.run(scenario)


def test_distinct_included_files_similar_locals_do_not_collide(tmp_path):
    from pyxsd.mcp.services.comparison import ComparisonService

    one = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:m">
<xs:element name="one"><xs:complexType><xs:sequence>
<xs:element name="shared" type="xs:int"/>
</xs:sequence></xs:complexType></xs:element>
</xs:schema>"""
    two = one.replace('name="one"', 'name="two"')
    main = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:m">
<xs:include schemaLocation="one.xsd"/>
<xs:include schemaLocation="two.xsd"/>
<xs:element name="main"><xs:complexType><xs:sequence/></xs:complexType></xs:element>
</xs:schema>"""

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "old/main.xsd": main,
                    "old/one.xsd": one,
                    "old/two.xsd": two,
                    "new/main.xsd": main,
                    "new/one.xsd": one.replace('type="xs:int"', 'type="xs:string"'),
                    "new/two.xsd": two,
                },
            )
            service = ComparisonService(inputs, pool)
            result = await service.compare(
                _compare_request(
                    workspace,
                    old_schema=SchemaRef(source=FileRef(path="old/main.xsd")),
                    new_schema=SchemaRef(source=FileRef(path="new/main.xsd")),
                )
            )
            structural = result["structural"]
            # Identical sides differ only in one.xsd's local: exactly that local
            # and its containing anonymous type change; two.xsd's local does not.
            locals_changed = [
                identifier
                for identifier in structural["changed"]
                + structural["added"]
                + structural["removed"]
                if str(identifier).startswith("element@")
            ]
            assert len(locals_changed) == 1

    anyio.run(scenario)


def test_comparison_identity_collision_discriminator_and_semantic_remap():
    from pyxsd.introspection import ComponentDescription
    from pyxsd.locations import IssueLocation, PathStep
    from pyxsd.mcp.services import comparison_identity as ci

    anonymous = ComponentDescription(
        id="type@original-1",
        kind="type",
        name=None,
        namespace=None,
        global_declaration=False,
        location=IssueLocation("/workspace/x/main.xsd", (PathStep("{xsd}schema", 0),)),
        owner_id="element:g",
        python_name=None,
        python_type=None,
        members=(),
        particle=None,
        facets=(),
        assertions=(),
        default=None,
        fixed=None,
        nillable=False,
        mixed=False,
        derivation=None,
        base_types=(),
        substitution_group=None,
        attribute_wildcard=None,
        documentation=None,
        identity_constraints=(),
    )
    twin = ComponentDescription(
        id="type@original-2",
        kind="type",
        name=None,
        namespace=None,
        global_declaration=False,
        location=IssueLocation("/workspace/x/main.xsd", (PathStep("{xsd}schema", 0),)),
        owner_id="element:g",
        python_name=None,
        python_type=None,
        members=(),
        particle=None,
        facets=(),
        assertions=(),
        default=None,
        fixed=None,
        nillable=False,
        mixed=False,
        derivation=None,
        base_types=(),
        substitution_group=None,
        attribute_wildcard=None,
        documentation=None,
        identity_constraints=(),
    )
    owner = ComponentDescription(
        id="element:g",
        kind="element",
        name="{urn}g",
        namespace="urn",
        global_declaration=True,
        location=None,
        owner_id=None,
        python_name=None,
        python_type=None,
        members=(),
        particle=None,
        facets=(),
        assertions=(),
        default=None,
        fixed=None,
        nillable=False,
        mixed=False,
        derivation=None,
        base_types=(),
        substitution_group=None,
        attribute_wildcard=None,
        documentation=None,
        identity_constraints=(),
    )
    from pyxsd.introspection import SchemaDescription

    description = SchemaDescription("urn", ("element:g",), (owner, anonymous, twin))
    keys = ci.comparison_keys(description, "/workspace/x/main.xsd")
    assert keys["element:g"] == "element:g"
    assert keys["type@original-1"] != keys["type@original-2"]
    assert keys["type@original-1"].startswith("type@")
    first = ci.semantic_dict(anonymous, keys)
    assert "location" not in first and "id" not in first
    assert first["owner_id"] == "element:g"
    from dataclasses import replace

    from pyxsd.introspection import MemberDescription

    missing_location = replace(anonymous, location=None)
    missing_source = replace(anonymous, location=IssueLocation(None, (PathStep("type", 1),)))
    assert ci.comparison_keys(SchemaDescription(None, (), (missing_location,)), "main.xsd")
    assert ci.comparison_keys(SchemaDescription(None, (), (missing_source,)), "main.xsd")
    primitive_member = MemberDescription(
        "attribute", "value", "value", "str", None, 0, 1, None, None, False, None, ()
    )
    without_link = replace(owner, members=(primitive_member,))
    assert ci.semantic_dict(without_link, keys)["members"][0]["component_id"] is None


def test_regressed_document_carries_issue_evidence(tmp_path):
    from pyxsd.mcp.services.comparison import ComparisonService

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {"old.xsd": OLD, "new.xsd": NEW, "regressed.xml": REGRESSED},
            )
            service = ComparisonService(inputs, pool)
            result = await service.compare(
                _compare_request(workspace, corpus=(FileRef(path="regressed.xml"),))
            )
            (entry,) = result["corpus"]["files"]
            assert entry["status"] == "regressed"
            evidence = entry["evidence"]
            assert evidence
            assert evidence[0]["code"]
            assert evidence[0]["message"]
            assert evidence[0]["location"] is not None

    anyio.run(scenario)


def test_resolved_document_carries_old_schema_evidence(tmp_path):
    from pyxsd.mcp.services.comparison import ComparisonService

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {"old.xsd": OLD, "new.xsd": NEW, "resolved.xml": RESOLVED},
            )
            service = ComparisonService(inputs, pool)
            result = await service.compare(
                _compare_request(workspace, corpus=(FileRef(path="resolved.xml"),))
            )
            (entry,) = result["corpus"]["files"]
            assert entry["status"] == "resolved"
            assert entry["evidence"]
            # Evidence is the old schema's issues against this document.
            assert entry["evidence"][0]["code"]

    anyio.run(scenario)


def test_valid_and_malformed_corpus_entries_omit_evidence(tmp_path):
    from pyxsd.mcp.services.comparison import ComparisonService

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {"old.xsd": OLD, "new.xsd": NEW, "good.xml": GOOD, "bad.xml": MALFORMED},
            )
            service = ComparisonService(inputs, pool)
            result = await service.compare(
                _compare_request(
                    workspace,
                    corpus=(FileRef(path="good.xml"), FileRef(path="bad.xml")),
                )
            )
            by_path = {entry["path"]: entry for entry in result["corpus"]["files"]}
            assert by_path["good.xml"]["status"] == "valid"
            assert "evidence" not in by_path["good.xml"]
            assert by_path["bad.xml"]["status"] == "malformed"
            assert "evidence" not in by_path["bad.xml"]

    anyio.run(scenario)
