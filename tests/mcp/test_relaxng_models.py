"""Standalone route contracts cross the clean-worker boundary, never XML verdicts."""

from copy import deepcopy

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")

import anyio
from tests.mcp.test_exploration import harness, upload

from pyxsd.mcp.contracts import FileRef, SchemaRef
from pyxsd.mcp.services.models import InspectModelRequest, ModelService, ValidateDataRequest

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"


def element(body):
    return f'<element xmlns="{NS}" name="doc" datatypeLibrary="{XSD}">{body}</element>'


def model_request(workspace, **kwargs):
    return dict(
        workspace_id=workspace,
        schema_ref=SchemaRef(source=FileRef(path="schema.rng"), format="relax-ng"),
        element="doc",
        **kwargs,
    )


def test_inspect_before_xml_and_independent_alternative_json_snapshot(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {"schema.rng": element('<choice><data type="int"/><data type="string"/></choice>')},
            )
            service = ModelService(inputs, pool)
            request = model_request(workspace)
            inspected = await service.inspect_model(InspectModelRequest(**request))
            assert inspected.data["route_scope"] == "start"
            assert inspected.data["whole_xml_validity"] is False
            assert inspected.data["route_element"] == "doc"
            assert inspected.data["json_schema"]
            validated = await service.validate_data(ValidateDataRequest(**request, data="12"))
            assert validated.data["valid"] is True
            tag = validated.data["value"]
            assert tag["#value"]["lexical"] == "12"
            assert len(tag["#value"]["candidates"]) == 2
            assert validated.data["whole_xml_validity"] is False
            again = await service.validate_data(ValidateDataRequest(**request, data=tag))
            assert again.data["value"] == tag
            for key, bad in (("grammar", "foreign"), ("candidates", ["unknown"]), ("version", 9)):
                broken = deepcopy(tag)
                broken["#value"][key] = bad
                rejected = await service.validate_data(ValidateDataRequest(**request, data=broken))
                assert rejected.data["valid"] is False
                assert rejected.data["errors"][0]["type"] == "value_error"
                assert rejected.data["whole_xml_validity"] is False

    anyio.run(scenario)


@pytest.mark.parametrize(
    "context",
    [
        {},
        {"namespaces": {"p": "urn:x"}},
        {"base_uri": "urn:base"},
        {"namespaces": {"p": "urn:x"}, "base_uri": "urn:base"},
    ],
)
def test_scoped_qname_context_round_trip_and_no_leakage(tmp_path, context):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "schema.rng": element(
                        '<attribute name="ref"><choice><data type="QName"/>'
                        '<data type="string"/></choice></attribute>'
                    )
                },
            )
            service = ModelService(inputs, pool)
            request = model_request(workspace)
            before = deepcopy(context)
            result = await service.validate_data(
                ValidateDataRequest(**request, data={"@ref": "p:x"}, **context)
            )
            assert result.data["valid"] is True
            tag = result.data["value"]["@ref"]["#value"]
            assert len(tag["candidates"]) == (2 if context.get("namespaces") else 1)
            assert tag["context"]["base_uri"] == context.get("base_uri")
            assert context == before
            unscoped = await service.validate_data(
                ValidateDataRequest(**request, data={"@ref": "p:x"})
            )
            assert len(unscoped.data["value"]["@ref"]["#value"]["candidates"]) == 1

    anyio.run(scenario)


def test_local_route_scope_field_errors_joint_choices_and_list_shapes(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "schema.rng": element(
                        '<element name="leaf"><attribute name="n"><data type="int"/></attribute>'
                        '<attribute name="tags"><list><zeroOrMore><data type="int"/>'
                        "</zeroOrMore></list></attribute></element>"
                        '<choice><element name="a"><text/></element>'
                        '<element name="b"><text/></element></choice>'
                    )
                },
            )
            service = ModelService(inputs, pool)
            request = model_request(workspace)
            local = await service.validate_data(
                ValidateDataRequest(**request, path=("leaf",), data={"@n": 7, "@tags": [1, 2]})
            )
            assert local.data["route_scope"] == "local"
            assert local.data["whole_xml_validity"] is False
            assert local.data["route_element"] == "leaf"
            assert local.data["value"] == {"@n": 7, "@tags": [1, 2]}
            for data, location in (
                ({"@n": "bad", "@tags": [1]}, ["@n"]),
                ({"@n": 1, "@tags": "not-a-list"}, ["@tags"]),
            ):
                bad = await service.validate_data(
                    ValidateDataRequest(**request, path=("leaf",), data=data)
                )
                assert bad.data["valid"] is False
                assert bad.data["errors"][0]["location"] == location
            joint = await service.validate_data(
                ValidateDataRequest(
                    **request, data={"leaf": {"@n": 1, "@tags": []}, "a": "x", "b": "y"}
                )
            )
            assert joint.data["valid"] is False
            assert "alternative" in joint.data["errors"][0]["message"]

    anyio.run(scenario)


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"namespaces": {"p": "urn:x"}},
        {"base_uri": "urn:base"},
        {"namespaces": {"p": "urn:x"}, "base_uri": "urn:base"},
    ],
)
def test_worker_context_omits_absent_keys_and_owns_namespace_mapping(
    tmp_path, monkeypatch, options
):
    from tests.mcp.test_cache import request as compile_request

    from pydantic import BaseModel
    from pyxsd import __version__
    from pyxsd.mcp.cache import SchemaCache
    from pyxsd.mcp.config import Limits
    from pyxsd.mcp.services.models import execute_model
    from pyxsd.mcp.worker_protocol import ModelOperation, decode_operation, encode_frame
    from pyxsd.mcp.workers import capture_to_wire

    req = compile_request(tmp_path, files={"root.xsd": element("<text/>")})
    operation = ModelOperation(
        request_id="context",
        capture=capture_to_wire(req.capture),
        schema_ref=SchemaRef(source=FileRef(path="root.xsd"), format="relax-ng"),
        element="doc",
        action="validate",
        data="value",
        **options,
    )
    operation = decode_operation(
        encode_frame(operation, max_bytes=None), max_bytes=None, role="clean"
    )
    validate = BaseModel.model_validate.__func__
    contexts = []

    def observed(cls, value, **kwargs):
        context = kwargs.get("context")
        result = validate(cls, value, **kwargs)
        if hasattr(cls, "__relaxng_route__"):
            contexts.append(deepcopy(context))
            if "namespaces" in context["relaxng"]:
                context["relaxng"]["namespaces"]["changed"] = "urn:changed"
        return result

    monkeypatch.setattr(BaseModel, "model_validate", classmethod(observed))
    cache = SchemaCache(Limits())
    try:
        assert execute_model(operation, cache, __version__)["value"] == "value"
        assert contexts == [{"relaxng": options}]
        assert operation.namespaces == options.get("namespaces", {})
    finally:
        cache.close()


def test_mcp_validation_exposes_context_without_changing_inspection(tmp_path):
    from tests.mcp.test_server import connected, create_workspace, structured

    async def scenario():
        async with connected(tmp_path) as (client, _):
            workspace = await create_workspace(client)
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            validation = tools["pyxsd_validate_data"].input_schema["properties"]
            assert "namespaces" in validation and "base_uri" in validation
            inspection = tools["pyxsd_inspect_model"].input_schema["properties"]
            assert "namespaces" not in inspection and "base_uri" not in inspection
            await client.call_tool(
                "pyxsd_put_files",
                {
                    "workspace_id": workspace,
                    "files": [
                        {
                            "path": "schema.rng",
                            "text": element('<attribute name="q"><data type="QName"/></attribute>'),
                        }
                    ],
                },
            )
            result = await client.call_tool(
                "pyxsd_validate_data",
                {
                    "workspace_id": workspace,
                    "schema_ref": {"source": {"path": "schema.rng"}, "format": "relax-ng"},
                    "element": "doc",
                    "data": {"@q": "p:x"},
                    "namespaces": {"p": "urn:x"},
                    "base_uri": "urn:base",
                },
            )
            assert not result.is_error
            data = structured(result)["data"]
            assert data["valid"] is True
            assert data["value"] == {"@q": "Q{urn:x}x"}
            assert data["whole_xml_validity"] is False

    anyio.run(scenario)


def test_source_root_models_inspect_and_validate_without_workspace_uploads(tmp_path):
    from tests.mcp.test_source_root import harness as source_harness
    from tests.mcp.test_source_root import relaxng_ref

    async def scenario():
        async with source_harness(tmp_path) as (_, inputs, pool, workspace):
            service = ModelService(inputs, pool)
            request = InspectModelRequest(
                workspace_id=workspace, schema_ref=relaxng_ref(), element="note"
            )
            inspected = await service.inspect_model(request)
            assert inspected.data["route_scope"] == "start"
            assert inspected.data["whole_xml_validity"] is False
            result = await service.validate_data(
                ValidateDataRequest(**request.model_dump(), data="independent")
            )
            assert result.data["value"] == "independent"
            assert result.data["valid"] is True
            assert result.data["whole_xml_validity"] is False

    anyio.run(scenario)
