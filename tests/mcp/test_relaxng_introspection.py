"""RELAX NG introspection, stubs, route guidance, models, and capabilities."""

import hashlib
import io
from collections import Counter
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_exploration import harness, upload

from pyxsd import __version__
from pyxsd.mcp import worker_main
from pyxsd.mcp.cache import SchemaCache
from pyxsd.mcp.captures import CaptureFile, InputCapture
from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import (
    FileRef,
    FindComponentsRequest,
    InspectSchemaRequest,
    Page,
    SchemaRef,
)
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.worker_protocol import (
    DescribeOperation,
    FindOperation,
)
from pyxsd.mcp.workers import capture_to_wire
from pyxsd.relaxng import RelaxNGSchema
from pyxsd.relaxng.introspection import describe_schema, find_components

NS = "http://relaxng.org/ns/structure/1.0"
XSD_LIBRARY = "http://www.w3.org/2001/XMLSchema-datatypes"

RNG = f"""<grammar xmlns="{NS}" datatypeLibrary="{XSD_LIBRARY}">
  <start><ref name="doc"/></start>
  <define name="doc">
    <element name="doc" ns="urn:test">
      <attribute name="id"><data type="integer"/></attribute>
      <interleave>
        <element name="leaf"><text/></element>
        <element name="num"><data type="integer"><param name="minInclusive">1</param></data></element>
      </interleave>
      <zeroOrMore>
        <element name="item">
          <attribute name="ref"><text/></attribute>
          <text/>
        </element>
      </zeroOrMore>
    </element>
  </define>
</grammar>"""

XSD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="Item"><xs:sequence><xs:element name="value" type="xs:int"/></xs:sequence></xs:complexType>
  <xs:element name="root"><xs:complexType><xs:sequence><xs:element name="item" type="Item"/></xs:sequence></xs:complexType></xs:element>
</xs:schema>"""

AMBIGUOUS_RNG = f"""<grammar xmlns="{NS}"><start><choice>
  <element name="doc" ns="urn:a"><empty/></element>
  <element name="doc" ns="urn:b"><empty/></element>
</choice></start></grammar>"""

OPEN_ROOT_RNG = (
    f"""<grammar xmlns="{NS}"><start><element><anyName/><empty/></element></start></grammar>"""
)

NESTED_LOCAL_RNG = f"""<grammar xmlns="{NS}"><start><element name="doc" ns="urn:a">
  <optional><element name="doc" ns="urn:b"><empty/></element></optional>
</element></start></grammar>"""

OPEN_PLUS_NAMESPACED_RNG = f"""<grammar xmlns="{NS}"><start><choice>
  <element><anyName/><empty/></element>
  <element name="doc" ns="urn:test"><empty/></element>
</choice></start></grammar>"""

OPEN_WITH_NAMED_CHILD_RNG = f"""<grammar xmlns="{NS}"><start><element>
  <anyName/><element name="inner" ns="urn:test"><text/></element>
</element></start></grammar>"""

NO_NAMESPACE_RNG = f"""<grammar xmlns="{NS}"><start><element name="doc">
  <element name="child"><element name="leaf"><text/></element></element>
</element></start></grammar>"""

RNG_COMPONENT_FIELDS = {
    "id",
    "kind",
    "name",
    "namespace",
    "location",
    "origins",
    "owner_id",
    "pattern_kind",
    "name_class",
    "datatype_library",
    "datatype_name",
    "datatype_parameters",
    "synthetic",
}


def _compile(text: str) -> RelaxNGSchema:
    return RelaxNGSchema.compile(io.StringIO(text))


def _worker_run(
    tmp_path: Path,
    schema_text: str,
    operation_type,
    *,
    filename: str = "schema.rng",
    schema_format: str = "relax-ng",
    limits: Limits | None = None,
    **kwargs,
):
    path = tmp_path / filename
    path.write_text(schema_text, encoding="utf-8")
    encoded = schema_text.encode()
    capture = InputCapture(
        "workspace",
        1,
        tmp_path,
        (CaptureFile(filename, hashlib.sha256(encoded).hexdigest(), len(encoded)),),
    )
    ref = SchemaRef(source=FileRef(path=filename), format=schema_format)
    cache = SchemaCache(limits if limits is not None else Limits(enforced=True))
    try:
        operation = operation_type(
            request_id="test", capture=capture_to_wire(capture), schema_ref=ref, **kwargs
        )
        return worker_main._execute(operation, cache, __version__)
    finally:
        cache.close()


def _ids(schema: RelaxNGSchema, kind: str) -> list[str]:
    return [item.id for item in find_components(schema, kind=kind)]


def _route_named(schema: RelaxNGSchema, name: str) -> str:
    return next(
        route.route_id for route in schema.projection.routes.values() if route.expanded_name == name
    )


# ---------------------------------------------------------------------------
# Existing XSD contracts and additive capabilities
# ---------------------------------------------------------------------------


def test_xsd_introspection_shapes_are_unchanged(tmp_path):
    overview = _worker_run(
        tmp_path, XSD, DescribeOperation, filename="schema.xsd", schema_format="xsd"
    )
    assert overview == {
        "view": "overview",
        "target_namespace": None,
        "roots": ["element:root"],
        "entry_roots": ["element:root"],
        "counts": {"attribute": 18, "element": 3, "type": 2},
    }
    constraints = _worker_run(
        tmp_path,
        XSD,
        DescribeOperation,
        filename="schema.xsd",
        schema_format="xsd",
        view="constraints",
    )
    assert set(constraints) == {
        "view",
        "target_namespace",
        "roots",
        "entry_roots",
        "counts",
        "components",
        "page",
    }
    component = constraints["components"][0]
    assert component["global_declaration"] is True
    assert "particle" in component and "python_name" in component
    assert "pattern_kind" not in component and "starts" not in constraints
    assert constraints["page"] == {"returned": 1, "truncated": False, "total": 1}


def test_capabilities_add_schema_languages_without_changing_old_keys(tmp_path):
    from pyxsd.mcp.config import Settings
    from pyxsd.mcp.server import Services
    from pyxsd.mcp.tools import capability_data

    async def scenario():
        async with Services(Settings(project_root=tmp_path)) as services:
            data = capability_data(services)
            settings = services.settings
            expected = {
                "version": __version__,
                "transport": "stdio",
                "execution_backend": settings.execution_backend,
                "enabled_groups": sorted(settings.enabled_groups),
                "source_root": None,
                "project_root": str(settings.project_root),
                "state_dir": str(settings.state_dir),
                "persist": settings.persist,
                "state_creation": "first_artifact",
                "limits": settings.limits.model_dump(mode="json"),
                "adapters": {
                    "pydantic": {
                        "enabled": settings.pydantic,
                        "available": True,
                    },
                    "arrow": {
                        "mode": settings.arrow,
                        "available": services.arrow_available,
                    },
                },
            }
            assert {key: value for key, value in data.items() if key != "schema_languages"} == (
                expected
            )
            assert list(data) == [*expected, "schema_languages"]

            languages = data["schema_languages"]
            assert set(languages) == {"xsd", "relax-ng"}
            assert languages["xsd"] == {
                "syntax": {"xml": True, "compact": False, "dtd": False},
                "library": {
                    "datatypes": {
                        "builtin": True,
                        "xsd": True,
                        "custom_python_factories": False,
                    },
                    "typed": True,
                    "capture": {"pattern_annotations": False, "xdm": True},
                    "conversion": {
                        "formats": [
                            "pyxsd",
                            "pyxsd-json",
                            "parker",
                            "badgerfish",
                            "jsonml",
                        ],
                        "fidelities": ["strict", "best-effort"],
                        "json_schema": True,
                    },
                },
                "mcp": {
                    "datatypes": {"python_factories": False, "custom_wire": False},
                    "validation": {"typed": True, "capture": False},
                    "diagnostics": {
                        "completeness": "not-reported",
                        "suppressed_count": True,
                    },
                    "comparison": {"structural": True, "corpus": True},
                    "conversion": {
                        "tools": True,
                        "export_data": True,
                        "import_data": True,
                        "document_json_schema": True,
                        "formats": [
                            "pyxsd",
                            "pyxsd-json",
                            "parker",
                            "badgerfish",
                            "jsonml",
                        ],
                        "fidelities": ["strict", "best-effort"],
                    },
                    "projection": {"models": True, "records": True},
                },
            }
            assert languages["relax-ng"] == {
                "syntax": {"xml": True, "compact": False, "dtd": False},
                "library": {
                    "datatypes": {
                        "builtin": True,
                        "xsd": True,
                        "custom_python_factories": True,
                    },
                    "typed": True,
                    "capture": {"pattern_annotations": True, "xdm": True},
                    "conversion": {
                        "formats": [
                            "pyxsd",
                            "pyxsd-json",
                            "parker",
                            "badgerfish",
                            "jsonml",
                        ],
                        "fidelities": ["strict", "best-effort"],
                        "json_schema": True,
                    },
                },
                "mcp": {
                    "datatypes": {"python_factories": False, "custom_wire": False},
                    "validation": {"typed": False, "capture": False},
                    "diagnostics": {
                        "completeness": "boolean",
                        "suppressed_count": True,
                    },
                    "comparison": {"structural": True, "corpus": True},
                    "conversion": {
                        "tools": True,
                        "export_data": True,
                        "import_data": True,
                        "document_json_schema": True,
                        "formats": [
                            "pyxsd",
                            "pyxsd-json",
                            "parker",
                            "badgerfish",
                            "jsonml",
                        ],
                        "fidelities": ["strict", "best-effort"],
                    },
                    "projection": {"models": True, "records": True},
                },
            }
            assert languages["relax-ng"]["mcp"]["comparison"] == {
                "structural": True,
                "corpus": True,
            }

    anyio.run(scenario)


# ---------------------------------------------------------------------------
# RELAX NG overview, components, filters, and stubs
# ---------------------------------------------------------------------------


def test_relaxng_overview_reports_starts_patterns_and_dependencies(tmp_path):
    result = _worker_run(tmp_path, RNG, DescribeOperation)
    schema = _compile(RNG)
    description = describe_schema(schema)
    assert set(result) == {
        "view",
        "starts",
        "entry_starts",
        "start_patterns",
        "counts",
        "dependencies",
    }
    assert result["view"] == "overview"
    assert result["starts"] == list(schema.projection.roots)
    assert result["entry_starts"] == result["starts"]
    assert result["start_patterns"] == list(description.start_ids)
    assert result["counts"] == dict(
        sorted(Counter(item.kind for item in description.components).items())
    )
    assert "target_namespace" not in result
    assert "roots" not in result
    (dependency,) = result["dependencies"]
    assert dependency["path"] == "/workspace/schema.rng"
    assert dependency["digest"] == description.dependencies[0].content_sha256
    assert dependency["purpose"] == "schema"
    assert dependency["references"] == 0


def test_relaxng_entry_starts_exclude_included_start_routes(tmp_path):
    included = f'<grammar xmlns="{NS}"><define name="doc"><element name="doc"><empty/></element></define></grammar>'
    root = f'<grammar xmlns="{NS}"><include href="part.rng"/><start><ref name="doc"/></start></grammar>'

    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(inputs, workspace, {"schema.rng": root, "part.rng": included})
            result = await service.inspect_schema(
                InspectSchemaRequest(
                    workspace_id=workspace,
                    schema_ref=SchemaRef(source=FileRef(path="schema.rng"), format="relax-ng"),
                )
            )
            assert result.data["starts"]
            assert result.data["entry_starts"] == []
            assert any(item["path"].endswith("part.rng") for item in result.data["dependencies"])

    anyio.run(scenario)


def test_relaxng_invalid_grammar_has_no_starts_but_keeps_components(tmp_path):
    invalid = RNG.replace('<ref name="doc"/>', '<ref name="missing"/>')
    overview = _worker_run(tmp_path, invalid, DescribeOperation)
    assert overview["starts"] == []
    assert overview["entry_starts"] == []
    assert overview["start_patterns"]
    assert overview["counts"]["pattern"]
    selected = _worker_run(tmp_path, invalid, DescribeOperation, component_ids=("grammar",))
    assert selected["starts"] == []
    assert [item["id"] for item in selected["components"]] == ["grammar"]
    assert selected["page"] == {"returned": 1, "truncated": False, "total": 1}


def test_relaxng_constraints_page_rng_components_without_xsd_stand_ins(tmp_path):
    schema = _compile(RNG)
    description = describe_schema(schema)
    selected = (
        "grammar",
        _ids(schema, "define")[0],
        _ids(schema, "pattern")[0],
        _ids(schema, "name-class")[0],
        next(
            item.id
            for item in description.components
            if item.kind == "datatype" and item.datatype_parameters
        ),
        _ids(schema, "route")[0],
        _ids(schema, "resource")[0],
    )
    result = _worker_run(
        tmp_path,
        RNG,
        DescribeOperation,
        view="constraints",
        component_ids=selected,
        offset=1,
        limit=3,
    )
    assert [item["id"] for item in result["components"]] == list(selected[1:4])
    assert result["page"] == {"returned": 3, "truncated": True, "total": 7}
    for component in result["components"]:
        assert set(component) == RNG_COMPONENT_FIELDS
        assert "global_declaration" not in component
        assert "particle" not in component
        assert "python_name" not in component

    full = _worker_run(tmp_path, RNG, DescribeOperation, view="constraints", component_ids=selected)
    by_id = {item["id"]: item for item in full["components"]}
    assert by_id["grammar"]["owner_id"] is None
    definition = by_id[_ids(schema, "define")[0]]
    assert definition["kind"] == "define"
    assert definition["synthetic"] is True
    assert definition["pattern_kind"] == "element"
    assert definition["name_class"].startswith("name(")
    route = by_id[_route_named(schema, "{urn:test}doc")]
    assert route["kind"] == "route"
    assert route["name"] == "{urn:test}doc"
    assert route["namespace"] == "urn:test"
    assert route["pattern_kind"] is None
    datatype = next(
        item
        for item in full["components"]
        if item["kind"] == "datatype" and item["datatype_parameters"]
    )
    assert datatype["datatype_library"] == XSD_LIBRARY
    assert datatype["datatype_name"] == "integer"
    assert datatype["datatype_parameters"] == [["minInclusive", "1"]]
    assert datatype["origins"]
    assert datatype["location"]["source"] == "/workspace/schema.rng"
    resource = next(item for item in full["components"] if item["kind"] == "resource")
    assert resource["name"] == "/workspace/schema.rng"
    assert resource["synthetic"] is False


def test_relaxng_find_filters_relations_and_pagination(tmp_path):
    schema = _compile(RNG)
    description = describe_schema(schema)
    everything = _worker_run(tmp_path, RNG, FindOperation)
    assert everything["page"]["total"] == len(description.components)
    assert len(everything["components"]) == len(description.components)

    for kind in (
        "grammar",
        "define",
        "pattern",
        "name-class",
        "datatype",
        "route",
        "resource",
    ):
        found = _worker_run(tmp_path, RNG, FindOperation, component_kind=kind)
        assert found["components"]
        assert {item["kind"] for item in found["components"]} == {kind}
        assert found["page"]["total"] == len(found["components"])

    integer = _worker_run(tmp_path, RNG, FindOperation, text="integer")
    assert integer["components"]
    assert {item["kind"] for item in integer["components"]} == {"datatype"}
    qualified = _worker_run(tmp_path, RNG, FindOperation, namespace="urn:test")
    assert qualified["components"]
    assert {item["namespace"] for item in qualified["components"]} == {"urn:test"}
    named = _worker_run(tmp_path, RNG, FindOperation, name="doc")
    assert {item["kind"] for item in named["components"]} == {
        "pattern",
        "name-class",
        "route",
    }
    assert all(item["name"].endswith("doc") for item in named["components"])

    first = _worker_run(tmp_path, RNG, FindOperation, component_kind="pattern", offset=1, limit=2)
    assert first["page"]["total"] == len(_ids(schema, "pattern"))
    assert first["page"] == {
        "returned": 2,
        "truncated": True,
        "total": len(_ids(schema, "pattern")),
    }
    assert [item["id"] for item in first["components"]] == _ids(schema, "pattern")[1:3]

    route_id = _route_named(schema, "{urn:test}doc")
    uses = _worker_run(
        tmp_path, RNG, FindOperation, component_id=route_id, direction="uses", depth=1
    )
    assert uses["components"]
    assert route_id in {item["id"] for item in uses["components"]}
    assert _route_named(schema, "{urn:test}item") in {item["id"] for item in uses["components"]}
    used_by = _worker_run(
        tmp_path,
        RNG,
        FindOperation,
        component_id=_route_named(schema, "{urn:test}item"),
        direction="used_by",
        depth=2,
    )
    assert route_id in {item["id"] for item in used_by["components"]}

    with pytest.raises(ToolError) as raised:
        _worker_run(tmp_path, RNG, FindOperation, component_kind="element")
    assert raised.value.fault.code == "invalid_argument"
    assert "RELAX NG component kind" in raised.value.fault.message
    with pytest.raises(ToolError) as missing:
        _worker_run(tmp_path, RNG, FindOperation, component_id="route:absent")
    assert missing.value.fault.code == "not_found"


def test_relaxng_stub_views_filter_depth_and_invalid_grammar(tmp_path):
    schema = _compile(RNG)
    root_id = schema.projection.roots[0]
    item_id = _route_named(schema, "{urn:test}item")
    datatype_id = _ids(schema, "datatype")[0]

    compact = _worker_run(tmp_path, RNG, DescribeOperation, view="compact", depth=1)
    assert "class Doc(RNG)" in compact["stub"]
    assert "class Item(RNG)" in compact["stub"]
    assert "start route" not in compact["stub"]

    full = _worker_run(tmp_path, RNG, DescribeOperation, view="full", depth=1)
    assert "from __future__ import annotations" in full["stub"]
    assert "class Doc" in full["stub"]

    filtered = _worker_run(
        tmp_path,
        RNG,
        DescribeOperation,
        view="compact",
        component_ids=(item_id,),
        depth=0,
    )
    assert "class Item(RNG)" in filtered["stub"]
    assert "class Doc(RNG)" not in filtered["stub"]

    shallow = _worker_run(
        tmp_path,
        RNG,
        DescribeOperation,
        view="compact",
        component_ids=(root_id,),
        depth=0,
    )
    assert "class Doc(RNG)" in shallow["stub"]
    assert "class Item(RNG)" not in shallow["stub"]
    assert "outside the component selection (depth=0)" in shallow["stub"]
    deeper = _worker_run(
        tmp_path,
        RNG,
        DescribeOperation,
        view="compact",
        component_ids=(root_id,),
        depth=1,
    )
    assert "class Item(RNG)" in deeper["stub"]
    assert "outside the component selection" not in deeper["stub"]

    no_routes = _worker_run(
        tmp_path,
        RNG,
        DescribeOperation,
        view="compact",
        component_ids=(datatype_id,),
        depth=1,
    )
    assert "no projection routes" in no_routes["stub"]
    assert "class " not in no_routes["stub"]

    invalid = RNG.replace('<ref name="doc"/>', '<ref name="missing"/>')
    unavailable = _worker_run(tmp_path, invalid, DescribeOperation, view="full")
    assert "schema report has errors" in unavailable["stub"]
    with pytest.raises(ToolError) as unknown:
        _worker_run(
            tmp_path,
            RNG,
            DescribeOperation,
            view="compact",
            component_ids=("route:absent",),
        )
    assert unknown.value.fault.code == "not_found"


def test_relaxng_introspection_unknown_components_and_exact_names(tmp_path):
    with pytest.raises(ToolError) as unknown:
        _worker_run(
            tmp_path,
            RNG,
            DescribeOperation,
            view="constraints",
            component_ids=("route:absent",),
        )
    assert unknown.value.fault.code == "not_found"
    exact = _worker_run(tmp_path, RNG, FindOperation, name="{urn:test}doc")
    assert [item["kind"] for item in exact["components"]] == ["route"]
    assert exact["components"][0]["name"] == "{urn:test}doc"

    from types import SimpleNamespace

    assert worker_main._relaxng_origin_order(SimpleNamespace(source_id="anonymous")) == (
        "anonymous",
        "",
        (),
        "",
    )


def test_relaxng_diagnostics_use_the_compiled_report(tmp_path):
    result = _worker_run(tmp_path, RNG, DescribeOperation, view="diagnostics")
    assert result["view"] == "diagnostics"
    assert result["report"]["counts"] == {"error": 0, "warning": 0}
    assert result["report"]["diagnostics_complete"] is True
    assert result["namespace_probe"] is None
    assert result["recommendations"] == []


def test_relaxng_introspection_never_reads_xsd_components(tmp_path, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("XSD introspection was called for RELAX NG")

    monkeypatch.setattr(worker_main, "describe_schema", refuse)
    monkeypatch.setattr(worker_main, "describe_component", refuse)
    monkeypatch.setattr(worker_main, "find_components", refuse)
    monkeypatch.setattr(worker_main, "related_components", refuse)
    monkeypatch.setattr(worker_main, "require_xsd_schema", refuse)

    schema = _compile(RNG)
    route_id = schema.projection.roots[0]
    overview = _worker_run(tmp_path, RNG, DescribeOperation)
    assert overview["starts"] == [route_id]
    _worker_run(tmp_path, RNG, DescribeOperation, view="constraints", component_ids=(route_id,))
    _worker_run(tmp_path, RNG, DescribeOperation, view="compact")
    found = _worker_run(tmp_path, RNG, FindOperation, component_kind="route")
    assert found["components"]
    related = _worker_run(
        tmp_path, RNG, FindOperation, component_id=route_id, direction="uses", depth=1
    )
    assert related["components"]


# ---------------------------------------------------------------------------
# Projection element and route guidance
# ---------------------------------------------------------------------------


def test_resolve_projection_element_keeps_xsd_semantics_and_resolves_rng_routes():
    from pyxsd.mcp.projection_guidance import resolve_projection_element
    from pyxsd.schema import Schema

    xsd = Schema.compile(io.StringIO(XSD))
    assert resolve_projection_element(xsd, "root") == "root"
    assert resolve_projection_element(xsd, "element:root") == "root"
    assert resolve_projection_element(xsd, "element:{urn:missing}root") == "{urn:missing}root"

    schema = _compile(RNG)
    root_id = schema.projection.roots[0]
    assert resolve_projection_element(schema, root_id) == root_id
    assert resolve_projection_element(schema, "{urn:test}doc") == root_id
    assert resolve_projection_element(schema, "Q{urn:test}doc") == root_id
    assert resolve_projection_element(schema, "doc") == root_id

    open_schema = _compile(OPEN_ROOT_RNG)
    open_id = open_schema.projection.roots[0]
    assert resolve_projection_element(open_schema, "anything") == open_id
    assert resolve_projection_element(open_schema, "{urn:any}anything") == open_id

    with pytest.raises(TypeError):
        resolve_projection_element(object(), "root")  # type: ignore[arg-type]


def test_relaxng_route_resolution_reports_ambiguity_missing_and_invalid():
    from pyxsd.mcp.projection_guidance import resolve_projection_element

    ambiguous = _compile(AMBIGUOUS_RNG)
    with pytest.raises(ToolError) as ambiguous_error:
        resolve_projection_element(ambiguous, "doc")
    assert ambiguous_error.value.fault.code == "invalid_argument"
    assert ambiguous_error.value.fault.details["candidates"] == sorted(ambiguous.projection.roots)

    schema = _compile(RNG)
    with pytest.raises(ToolError) as missing:
        resolve_projection_element(schema, "absent")
    assert missing.value.fault.code == "not_found"
    assert missing.value.fault.details["candidates"] == sorted(schema.projection.roots)
    with pytest.raises(ToolError) as malformed:
        resolve_projection_element(schema, "{oops")
    assert malformed.value.fault.code == "not_found"

    invalid = _compile(RNG.replace('<ref name="doc"/>', '<ref name="missing"/>'))
    with pytest.raises(ToolError) as invalid_error:
        resolve_projection_element(invalid, "doc")
    assert invalid_error.value.fault.code == "invalid_schema"
    with pytest.raises(ToolError) as invalid_start:
        resolve_projection_element(invalid, "doc", scope="start")
    assert invalid_start.value.fault.code == "invalid_schema"


def test_relaxng_start_resolution_matches_model_backend_fallbacks():
    from pyxsd.mcp.projection_guidance import resolve_projection_element

    nested = _compile(NESTED_LOCAL_RNG)
    root_id = nested.projection.roots[0]
    assert resolve_projection_element(nested, "doc", scope="start") == root_id
    with pytest.raises(ToolError) as all_routes:
        resolve_projection_element(nested, "doc")
    assert all_routes.value.fault.code == "invalid_argument"
    assert set(all_routes.value.fault.details["candidates"]) == {
        root_id,
        _route_named(nested, "{urn:b}doc"),
    }

    open_named = _compile(OPEN_PLUS_NAMESPACED_RNG)
    open_roots = [
        route_id
        for route_id in open_named.projection.roots
        if open_named.projection.routes[route_id].expanded_name is None
    ]
    assert len(open_roots) == 1
    assert resolve_projection_element(open_named, "doc", scope="start") == open_roots[0]

    open_child = _compile(OPEN_WITH_NAMED_CHILD_RNG)
    open_child_root = open_child.projection.roots[0]
    assert open_child.projection.routes[open_child_root].expanded_name is None
    assert resolve_projection_element(open_child, "inner", scope="start") == open_child_root

    local_id = _route_named(nested, "{urn:b}doc")
    with pytest.raises(ToolError) as local:
        resolve_projection_element(nested, local_id, scope="start")
    assert local.value.fault.code == "unsupported_projection"
    assert local.value.fault.details["scope"] == "local"
    assert local.value.fault.details["candidates"] == sorted(nested.projection.roots)


def test_relaxng_candidates_use_a_pre_resolved_start_route():
    from pyxsd.integrations.pydantic import models
    from pyxsd.mcp.projection_guidance import candidate_details

    schema = _compile(NESTED_LOCAL_RNG)
    root_id = schema.projection.roots[0]
    local_id = _route_named(schema, "{urn:b}doc")
    registry = models(schema)
    details = candidate_details(
        schema,
        element="doc",
        path=(),
        prepare=lambda element, path: registry.model_for(element=element, path=path),
        route_id=root_id,
    )
    assert details["element"] == "{urn:a}doc"
    assert [item["route_id"] for item in details["projection_candidates"]] == [local_id]
    with pytest.raises(ToolError) as without_anchor:
        candidate_details(
            schema,
            element="doc",
            path=(),
            prepare=lambda element, path: registry.model_for(element=element, path=path),
        )
    assert without_anchor.value.fault.code == "invalid_argument"


def test_relaxng_candidates_are_verified_by_the_model_backend():
    from pyxsd.integrations.pydantic import models
    from pyxsd.mcp.projection_guidance import candidate_details

    schema = _compile(RNG)
    registry = models(schema)
    details = candidate_details(
        schema,
        element="{urn:test}doc",
        path=(),
        prepare=lambda element, path: registry.model_for(element=element, path=path),
    )
    assert details["element"] == "{urn:test}doc"
    assert details["path"] == []
    assert details["search_truncated"] is False
    assert details["searched_declarations"] == 3
    assert [item["path"] for item in details["projection_candidates"]] == [
        ["{urn:test}item"],
        ["{urn:test}leaf"],
        ["{urn:test}num"],
    ]
    assert [item["repeated"] for item in details["projection_candidates"]] == [
        True,
        False,
        False,
    ]
    for candidate in details["projection_candidates"]:
        assert set(candidate) == {"element", "path", "repeated", "route_id"}
        model = registry.model_for(element=candidate["element"], path=tuple(candidate["path"]))
        assert model is not None

    below = candidate_details(
        schema,
        element="{urn:test}doc",
        path=("{urn:test}item",),
        prepare=lambda element, path: registry.model_for(element=element, path=path),
    )
    assert below["projection_candidates"] == []

    fallback = candidate_details(
        schema,
        element="{urn:test}doc",
        path=("{urn:test}missing",),
        prepare=lambda element, path: registry.model_for(element=element, path=path),
    )
    assert fallback["path"] == ["{urn:test}missing"]
    assert [item["path"] for item in fallback["projection_candidates"]] == [
        ["{urn:test}item"],
        ["{urn:test}leaf"],
        ["{urn:test}num"],
    ]
    unreachable = candidate_details(
        schema,
        element="{urn:test}doc",
        path=("{urn:test}missing", "{urn:test}deeper"),
        prepare=lambda element, path: registry.model_for(element=element, path=path),
    )
    assert unreachable["projection_candidates"] == []
    assert unreachable["search_truncated"] is False


def test_relaxng_candidates_skip_generic_routes_and_follow_wildcards():
    from pyxsd.integrations.pydantic import models
    from pyxsd.mcp.projection_guidance import candidate_details

    generic = f"""<grammar xmlns="{NS}"><start><element name="doc" ns="urn:test">
      <interleave>
        <element name="good"><text/></element>
        <element name="bad"><mixed><element name="x"><empty/></element></mixed></element>
      </interleave>
    </element></start></grammar>"""
    schema = _compile(generic)
    registry = models(schema)
    details = candidate_details(
        schema,
        element="{urn:test}doc",
        path=(),
        prepare=lambda element, path: registry.model_for(element=element, path=path),
    )
    direct = [item for item in details["projection_candidates"] if len(item["path"]) == 1]
    assert direct == [
        {
            "element": "{urn:test}doc",
            "path": ["{urn:test}good"],
            "repeated": False,
            "route_id": _route_named(schema, "{urn:test}good"),
        }
    ]
    assert all(item["path"] != ["{urn:test}bad"] for item in details["projection_candidates"])

    wildcard = f"""<grammar xmlns="{NS}"><start><element name="doc" ns="urn:test">
      <element><anyName/><element name="inner" ns="urn:test"><text/></element></element>
    </element></start></grammar>"""
    schema = _compile(wildcard)
    registry = models(schema)
    details = candidate_details(
        schema,
        element="{urn:test}doc",
        path=("{urn:test}anything",),
        prepare=lambda element, path: registry.model_for(element=element, path=path),
    )
    assert details["projection_candidates"] == [
        {
            "element": "{urn:test}doc",
            "path": ["{urn:test}anything", "{urn:test}inner"],
            "repeated": False,
            "route_id": _route_named(schema, "{urn:test}inner"),
        }
    ]


def test_relaxng_candidates_anchor_a_local_route_id_from_its_start():
    from pyxsd.integrations.pydantic import models
    from pyxsd.mcp.projection_guidance import candidate_details

    schema = _compile(RNG)
    item_id = _route_named(schema, "{urn:test}item")
    registry = models(schema)
    details = candidate_details(
        schema,
        element=item_id,
        path=(),
        prepare=lambda element, path: registry.model_for(element=element, path=path),
    )
    assert details["element"] == "{urn:test}doc"
    assert details["path"] == []
    assert details["projection_candidates"] == []
    assert details["search_truncated"] is False


def test_relaxng_route_resolution_handles_ambiguous_exact_expanded_names():
    from dataclasses import replace

    from pyxsd.mcp.projection_guidance import resolve_projection_element

    schema = _compile(RNG)
    plan = schema.projection
    route_id = plan.roots[0]
    duplicate = replace(plan.routes[route_id], route_id="duplicate-route")
    duplicate_plan = replace(plan, routes={**plan.routes, "duplicate-route": duplicate})
    schema.__dict__["projection"] = duplicate_plan
    with pytest.raises(ToolError) as ambiguous:
        resolve_projection_element(schema, "{urn:test}doc")
    assert ambiguous.value.fault.code == "invalid_argument"
    assert ambiguous.value.fault.details["candidates"] == sorted([route_id, "duplicate-route"])


def test_relaxng_empty_namespace_spellings_match_the_backend():
    from pyxsd.integrations.pydantic import models
    from pyxsd.mcp.projection_guidance import candidate_details, resolve_projection_element

    schema = _compile(NO_NAMESPACE_RNG)
    root_id = schema.projection.roots[0]
    child_id = _route_named(schema, "child")
    assert resolve_projection_element(schema, "{}doc") == root_id
    assert resolve_projection_element(schema, "Q{}doc") == root_id
    assert resolve_projection_element(schema, "{}doc", scope="start") == root_id
    assert resolve_projection_element(schema, "{}child") == child_id
    registry = models(schema)
    assert resolve_projection_element(schema, "{}doc") == _route_named(
        schema, registry.model_for(element="{}doc").model_json_schema()["x-pyxsd-element"]
    )
    details = candidate_details(
        schema,
        element="{}doc",
        path=("{}child",),
        prepare=lambda element, path: registry.model_for(element=element, path=path),
    )
    assert details["element"] == "doc"
    assert details["projection_candidates"] == [
        {
            "element": "doc",
            "path": ["child", "leaf"],
            "repeated": False,
            "route_id": _route_named(schema, "leaf"),
        }
    ]


def test_relaxng_candidate_caps_report_truncation_truthfully():
    from pyxsd.integrations.pydantic import models
    from pyxsd.mcp.projection_guidance import candidate_details

    def prepare_for(schema):
        registry = models(schema)
        return lambda element, path: registry.model_for(element=element, path=path)

    nested = '<element name="l9"><empty/></element>'
    for level in range(8, 0, -1):
        nested = f'<element name="l{level}">{nested}</element>'
    depth_schema = _compile(
        f'<grammar xmlns="{NS}"><start><element name="l0" ns="urn:test">'
        f"{nested}</element></start></grammar>"
    )
    depth = candidate_details(
        depth_schema,
        element="{urn:test}l0",
        path=(),
        prepare=prepare_for(depth_schema),
        limits=Limits(enforced=True),
    )
    assert len(depth["projection_candidates"]) == 8
    assert depth["search_truncated"] is True

    children = "".join(
        f'<element name="w{index:03d}"><zeroOrMore><attribute><anyName/><text/>'
        "</attribute></zeroOrMore><empty/></element>"
        for index in range(101)
    )
    visit_schema = _compile(
        f'<grammar xmlns="{NS}"><start><element name="doc">{children}</element></start></grammar>'
    )
    visit = candidate_details(
        visit_schema,
        element="doc",
        path=(),
        prepare=prepare_for(visit_schema),
        limits=Limits(enforced=True),
    )
    assert visit["projection_candidates"] == []
    assert visit["searched_declarations"] == 100
    assert visit["search_truncated"] is True

    candidates = "".join(
        f'<element name="line{index:02d}"><text/></element>' for index in range(11)
    )
    candidate_schema = _compile(
        f'<grammar xmlns="{NS}"><start><element name="doc">{candidates}</element></start></grammar>'
    )
    capped = candidate_details(
        candidate_schema,
        element="doc",
        path=(),
        prepare=prepare_for(candidate_schema),
        limits=Limits(enforced=True),
    )
    assert len(capped["projection_candidates"]) == 10
    assert capped["search_truncated"] is True


def test_relaxng_candidates_terminate_on_a_recursive_route():
    from pyxsd.integrations.pydantic import models
    from pyxsd.mcp.projection_guidance import candidate_details

    recursive = f"""<grammar xmlns="{NS}"><start><ref name="node"/></start>
      <define name="node"><element name="node"><zeroOrMore><ref name="node"/></zeroOrMore></element></define>
    </grammar>"""
    schema = _compile(recursive)
    route_id = schema.projection.roots[0]
    assert schema.projection.routes[route_id].children["node"].child_route_id == route_id
    registry = models(schema)
    details = candidate_details(
        schema,
        element="node",
        path=(),
        prepare=lambda element, path: registry.model_for(element=element, path=path),
    )
    assert details["searched_declarations"] == 1
    assert details["search_truncated"] is False
    assert [item["path"] for item in details["projection_candidates"]] == [["node"]]


def test_relaxng_route_anchoring_handles_multiple_roots_and_converging_paths():
    from pyxsd.mcp import projection_guidance
    from pyxsd.mcp.projection_guidance import candidate_details

    schema = _compile(
        f"""<grammar xmlns="{NS}"><start><choice>
          <element name="first"><empty/></element>
          <element name="root"><choice>
            <element name="b"><element name="d"><element name="e"><empty/></element></element></element>
            <element name="c"><element name="d"><element name="e"><empty/></element></element></element>
          </choice></element>
        </choice></start></grammar>"""
    )
    e_id = _route_named(schema, "e")
    details = candidate_details(
        schema,
        element=e_id,
        path=(),
        prepare=lambda element, path: None,
    )
    assert details["element"] == "root"
    assert details["projection_candidates"] == []
    assert details["search_truncated"] is False
    assert projection_guidance._relaxng_route_from_start(schema.projection, "absent") is None


def test_relaxng_candidate_guidance_never_reads_xsd_internals(monkeypatch):
    from pyxsd.integrations.pydantic import models
    from pyxsd.mcp import projection_guidance

    def refuse(*_args, **_kwargs):
        raise AssertionError("XSD guidance internals were read for RELAX NG")

    monkeypatch.setattr(projection_guidance, "particle_elements", refuse)
    monkeypatch.setattr(projection_guidance, "resolve_global_element", refuse)
    monkeypatch.setattr(projection_guidance, "resolve_route", refuse)
    schema = _compile(RNG)
    registry = models(schema)
    details = projection_guidance.candidate_details(
        schema,
        element="{urn:test}doc",
        path=(),
        prepare=lambda element, path: registry.model_for(element=element, path=path),
    )
    assert details["projection_candidates"]


def test_relaxng_open_name_and_disconnected_route_guidance_is_explicit(monkeypatch):
    from pyxsd.integrations.pydantic import models
    from pyxsd.mcp import projection_guidance

    open_schema = _compile(OPEN_ROOT_RNG)
    open_id = open_schema.projection.roots[0]
    registry = models(open_schema)
    details = projection_guidance.candidate_details(
        open_schema,
        element="anything",
        path=(),
        prepare=lambda element, path: registry.model_for(element=element, path=path),
    )
    assert details["element"] == "anything"
    assert details["projection_candidates"] == []
    with pytest.raises(ToolError) as open_error:
        projection_guidance.candidate_details(
            open_schema,
            element=open_id,
            path=(),
            prepare=lambda element, path: registry.model_for(element=element, path=path),
        )
    assert open_error.value.fault.code == "invalid_argument"
    assert open_error.value.fault.details["route_id"] == open_id

    schema = _compile(RNG)
    local_id = _route_named(schema, "{urn:test}item")
    monkeypatch.setattr(projection_guidance, "_relaxng_route_from_start", lambda *_args: None)
    with pytest.raises(ToolError) as disconnected:
        projection_guidance.candidate_details(
            schema,
            element=local_id,
            path=(),
            prepare=lambda element, path: None,
        )
    assert disconnected.value.fault.code == "invalid_argument"
    assert disconnected.value.fault.details["route_id"] == local_id


# ---------------------------------------------------------------------------
# Models and record scope
# ---------------------------------------------------------------------------


def test_relaxng_models_report_start_and_local_route_scope(tmp_path):
    from pyxsd.mcp.services.models import (
        InspectModelRequest,
        ModelService,
        ValidateDataRequest,
    )

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.rng": RNG})
            service = ModelService(inputs, pool)
            ref = SchemaRef(source=FileRef(path="schema.rng"), format="relax-ng")
            request = InspectModelRequest(
                workspace_id=workspace, schema_ref=ref, element="{urn:test}doc"
            )
            inspected = await service.inspect_model(request)
            assert inspected.data["validation_scope"] == "projected-data"
            assert inspected.data["route_scope"] == "start"
            assert inspected.data["whole_xml_validity"] is False
            assert inspected.data["route_element"] == "{urn:test}doc"
            assert "@id" in inspected.data["json_schema"]["properties"]
            assert "not whole-XML validity" in inspected.warnings[0]

            valid = await service.validate_data(
                ValidateDataRequest(
                    **request.model_dump(),
                    data={
                        "@id": "1",
                        "{urn:test}leaf": "leaf",
                        "{urn:test}num": 2,
                    },
                )
            )
            assert valid.data["valid"] is True
            assert valid.data["route_scope"] == "start"
            assert valid.data["whole_xml_validity"] is False

            local = request.model_copy(update={"path": ("{urn:test}item",)})
            local_inspect = await service.inspect_model(local)
            assert local_inspect.data["route_scope"] == "local"
            assert local_inspect.data["route_element"] == "{urn:test}item"
            local_valid = await service.validate_data(
                ValidateDataRequest(
                    **local.model_dump(),
                    data={"@ref": "a", "$": "text"},
                )
            )
            assert local_valid.data["valid"] is True
            assert local_valid.data["route_scope"] == "local"
            assert local_valid.data["whole_xml_validity"] is False

            bad = await service.validate_data(
                ValidateDataRequest(
                    **local.model_dump(),
                    data={"@ref": 3, "$": "text"},
                )
            )
            assert bad.data["valid"] is False
            assert bad.data["route_scope"] == "local"
            assert bad.data["whole_xml_validity"] is False
            assert bad.data["errors"]

    anyio.run(scenario)


def test_relaxng_model_start_selection_uses_start_only_semantics(tmp_path):
    from pyxsd.mcp.services.models import (
        InspectModelRequest,
        ModelService,
        ValidateDataRequest,
    )

    nested = _compile(NESTED_LOCAL_RNG)
    open_named = _compile(OPEN_PLUS_NAMESPACED_RNG)
    open_child = _compile(OPEN_WITH_NAMED_CHILD_RNG)

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "nested.rng": NESTED_LOCAL_RNG,
                    "open.rng": OPEN_PLUS_NAMESPACED_RNG,
                    "open-child.rng": OPEN_WITH_NAMED_CHILD_RNG,
                },
            )
            service = ModelService(inputs, pool)

            def request(path, element):
                return InspectModelRequest(
                    workspace_id=workspace,
                    schema_ref=SchemaRef(source=FileRef(path=path), format="relax-ng"),
                    element=element,
                )

            selected = await service.inspect_model(request("nested.rng", "doc"))
            assert selected.data["route_scope"] == "start"
            assert selected.data["route_element"] == "{urn:a}doc"
            assert selected.data["route_id"] == nested.projection.roots[0]
            valid = await service.validate_data(
                ValidateDataRequest(**request("nested.rng", "doc").model_dump(), data={})
            )
            assert valid.data["valid"] is True
            assert valid.data["route_element"] == "{urn:a}doc"

            open_selected = await service.inspect_model(request("open.rng", "doc"))
            assert open_selected.data["route_scope"] == "start"
            assert open_selected.data["route_element"] is None
            assert open_selected.data["route_id"] in open_named.projection.roots
            open_valid = await service.validate_data(
                ValidateDataRequest(**request("open.rng", "doc").model_dump(), data={})
            )
            assert open_valid.data["valid"] is True

            not_local = await service.inspect_model(request("open-child.rng", "inner"))
            assert not_local.data["route_scope"] == "start"
            assert not_local.data["route_element"] is None
            assert not_local.data["route_id"] == open_child.projection.roots[0]

    anyio.run(scenario)


def test_relaxng_model_route_selection_is_explicit_and_bounded(tmp_path):
    from pyxsd.mcp.services.models import InspectModelRequest, ModelService

    schema = _compile(RNG)
    item_id = _route_named(schema, "{urn:test}item")
    open_schema = _compile(OPEN_ROOT_RNG)
    open_id = open_schema.projection.roots[0]
    ambiguous = _compile(AMBIGUOUS_RNG)

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": RNG, "open.rng": OPEN_ROOT_RNG, "ambiguous.rng": AMBIGUOUS_RNG},
            )
            service = ModelService(inputs, pool)

            def request(path, element="{urn:test}doc"):
                return InspectModelRequest(
                    workspace_id=workspace,
                    schema_ref=SchemaRef(source=FileRef(path=path), format="relax-ng"),
                    element=element,
                )

            with pytest.raises(ToolError) as local:
                await service.inspect_model(request("schema.rng", item_id))
            assert local.value.fault.code == "unsupported_projection"
            assert local.value.fault.details["scope"] == "local"
            assert local.value.fault.details["candidates"] == sorted(schema.projection.roots)

            with pytest.raises(ToolError) as open_route:
                await service.inspect_model(request("open.rng", open_id))
            assert open_route.value.fault.code == "unsupported_projection"
            assert open_route.value.fault.details["scope"] == "start"
            opened = await service.inspect_model(request("open.rng", "anything"))
            assert opened.data["route_scope"] == "start"
            assert opened.data["route_element"] is None

            with pytest.raises(ToolError) as missing:
                await service.inspect_model(request("schema.rng", "absent"))
            assert missing.value.fault.code == "not_found"
            assert missing.value.fault.details["candidates"] == sorted(schema.projection.roots)

            with pytest.raises(ToolError) as ambiguous_error:
                await service.inspect_model(request("ambiguous.rng", "doc"))
            assert ambiguous_error.value.fault.code == "invalid_argument"
            assert ambiguous_error.value.fault.details["candidates"] == sorted(
                ambiguous.projection.roots
            )

    anyio.run(scenario)


def test_relaxng_model_failure_guidance_uses_real_route_models(tmp_path):
    from pyxsd.mcp.services.models import InspectModelRequest, ModelService

    generic = f"""<grammar xmlns="{NS}"><start><element name="doc" ns="urn:test">
      <interleave>
        <element name="good"><text/></element>
        <element name="bad"><mixed><element name="x"><empty/></element></mixed></element>
      </interleave>
    </element></start></grammar>"""

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"schema.rng": generic})
            service = ModelService(inputs, pool)
            request = InspectModelRequest(
                workspace_id=workspace,
                schema_ref=SchemaRef(source=FileRef(path="schema.rng"), format="relax-ng"),
                element="{urn:test}doc",
            )
            with pytest.raises(ToolError) as raised:
                await service.inspect_model(request)
            details = raised.value.fault.details
            assert details["element"] == "{urn:test}doc"
            direct = [item for item in details["projection_candidates"] if len(item["path"]) == 1]
            assert direct == [
                {
                    "element": "{urn:test}doc",
                    "path": ["{urn:test}good"],
                    "repeated": False,
                    "route_id": _route_named(_compile(generic), "{urn:test}good"),
                }
            ]
            assert details["search_truncated"] is False

    anyio.run(scenario)


def test_relaxng_record_preview_rejects_invalid_document(tmp_path):
    from pyxsd.mcp.services.records import PreviewRecordsRequest, RecordService

    pytest.importorskip("pyarrow")

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": RNG, "document.xml": "<doc/>"},
            )
            service = RecordService(inputs, pool, outputs=inputs.outputs)
            with pytest.raises(ToolError) as raised:
                await service.preview_records(
                    PreviewRecordsRequest(
                        workspace_id=workspace,
                        schema_ref=SchemaRef(source=FileRef(path="schema.rng"), format="relax-ng"),
                        document=FileRef(path="document.xml"),
                        element="{urn:test}doc",
                    )
                )
            assert raised.value.fault.code == "invalid_document"

    anyio.run(scenario)


def test_relaxng_record_start_selection_and_export_are_explicit():
    from types import SimpleNamespace

    from pyxsd.mcp.projection_guidance import resolve_projection_element
    from pyxsd.mcp.services.records import _prepare_records, _record_element

    pytest.importorskip("pyarrow")
    schema = _compile(RNG)
    root_id = schema.projection.roots[0]
    local_id = _route_named(schema, "{urn:test}item")
    assert _record_element(schema, "doc") == "{urn:test}doc"
    assert _record_element(schema, "{urn:test}doc") == "{urn:test}doc"
    with pytest.raises(ToolError) as local:
        _record_element(schema, local_id)
    assert local.value.fault.code == "unsupported_projection"
    assert local.value.fault.details["scope"] == "local"
    assert local.value.fault.details["candidates"] == sorted(schema.projection.roots)

    open_schema = _compile(OPEN_ROOT_RNG)
    open_id = open_schema.projection.roots[0]
    with pytest.raises(ToolError) as open_route:
        _record_element(open_schema, open_id)
    assert open_route.value.fault.code == "unsupported_projection"
    assert open_route.value.fault.details["scope"] == "start"
    assert _record_element(open_schema, "anything") == "anything"
    projection = _prepare_records(schema, SimpleNamespace(element="doc", path=()), None)
    assert type(projection).__name__ == "RelaxNGRecordProjection"
    assert resolve_projection_element(schema, "doc", scope="start") == root_id


def test_relaxng_record_export_rejects_invalid_document(tmp_path):
    from pyxsd.mcp.services.records import ExportRecordsRequest, RecordService

    pytest.importorskip("pyarrow")

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": RNG, "document.xml": "<doc/>"},
            )
            service = RecordService(inputs, pool, outputs=inputs.outputs)
            with pytest.raises(ToolError) as raised:
                await service.export_records(
                    ExportRecordsRequest(
                        workspace_id=workspace,
                        schema_ref=SchemaRef(source=FileRef(path="schema.rng"), format="relax-ng"),
                        documents=(FileRef(path="document.xml"),),
                        element="{urn:test}doc",
                    )
                )
            assert raised.value.fault.code == "invalid_document"

    anyio.run(scenario)


# ---------------------------------------------------------------------------
# Service paging
# ---------------------------------------------------------------------------


def test_relaxng_find_service_cursor_pages_components(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (service, inputs, _pool, workspace):
            await upload(inputs, workspace, {"schema.rng": RNG})
            request = FindComponentsRequest(
                workspace_id=workspace,
                schema_ref=SchemaRef(source=FileRef(path="schema.rng"), format="relax-ng"),
                kind="pattern",
                page=Page(limit=3),
            )
            first = await service.find_schema_components(request)
            assert first.page.returned == 3
            assert first.page.truncated is True
            assert first.page.next_cursor is not None
            second = await service.find_schema_components(
                request.model_copy(update={"page": Page(cursor=first.page.next_cursor, limit=3)})
            )
            assert second.page.returned == 3
            assert {item["id"] for item in first.data["components"]}.isdisjoint(
                {item["id"] for item in second.data["components"]}
            )

            overview = await service.inspect_schema(
                InspectSchemaRequest(
                    workspace_id=workspace,
                    schema_ref=SchemaRef(source=FileRef(path="schema.rng"), format="relax-ng"),
                )
            )
            assert overview.data["starts"]
            assert overview.data["dependencies"]

    anyio.run(scenario)
