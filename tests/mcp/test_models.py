"""Projected Pydantic contracts run only in leased clean schema workers."""

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_exploration import harness, upload

from pyxsd.mcp.contracts import FileRef, SchemaRef
from pyxsd.mcp.errors import ToolError

XSD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="order"><xs:complexType><xs:sequence>
<xs:element name="amount" type="xs:decimal"/>
<xs:element name="count" type="xs:integer"/>
<xs:element name="optional" type="xs:string" minOccurs="0" nillable="true"/>
</xs:sequence><xs:attribute name="id" type="xs:string" use="required"/>
</xs:complexType></xs:element></xs:schema>"""


def test_inspect_and_validate_exact_projected_contract(tmp_path):
    from pyxsd.mcp.services.models import InspectModelRequest, ModelService, ValidateDataRequest

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            service = ModelService(inputs, pool)
            await upload(inputs, workspace, {"schema.xsd": XSD})
            request = InspectModelRequest(
                workspace_id=workspace,
                schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
                element="order",
            )
            inspected = await service.inspect_model(request)
            contract = inspected.data["json_schema"]
            assert "@id" in contract["properties"]
            assert "amount" in contract["required"]
            assert inspected.data["validation_scope"] == "projected-data"
            assert len(inspected.provenance["schema_fingerprint"]) == 64
            huge = 10**100 + 7
            validated = await service.validate_data(
                ValidateDataRequest(
                    **request.model_dump(),
                    data={
                        "@id": "one",
                        "amount": "1.2300",
                        "count": huge,
                    },
                ),
            )
            assert validated.data["valid"] is True
            assert validated.data["value"]["amount"] == "1.2300"
            assert validated.data["value"]["count"] == str(huge)
            assert "optional" not in validated.data["value"]
            nil = await service.validate_data(
                ValidateDataRequest(
                    **request.model_dump(),
                    data={
                        "@id": "one",
                        "amount": "1",
                        "count": 1,
                        "optional": None,
                    },
                ),
            )
            assert nil.data["value"]["optional"] is None
            bad = await service.validate_data(
                ValidateDataRequest(**request.model_dump(), data={"count": "wrong"}),
            )
            assert bad.data["valid"] is False
            assert bad.data["errors"]
            assert bad.data["validation_scope"] == "projected-data"
            assert "XML" in bad.warnings[0]

    anyio.run(scenario)


def test_recursive_shape_is_explicitly_unsupported(tmp_path):
    from pyxsd.mcp.services.models import InspectModelRequest, ModelService

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "schema.xsd": '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
                    '<xs:complexType name="Node"><xs:sequence>'
                    '<xs:element name="child" type="Node" minOccurs="0"/>'
                    '</xs:sequence></xs:complexType><xs:element name="root" type="Node"/>'
                    "</xs:schema>"
                },
            )
            with pytest.raises(ToolError, match="unsupported_projection"):
                await ModelService(inputs, pool).inspect_model(
                    InspectModelRequest(
                        workspace_id=workspace,
                        schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
                        element="root",
                    ),
                )

    anyio.run(scenario)


@pytest.mark.parametrize(
    "shape",
    [
        '<xs:complexType mixed="true"><xs:sequence><xs:element name="child" type="xs:string"/></xs:sequence></xs:complexType>',
        "<xs:complexType><xs:sequence><xs:any/></xs:sequence></xs:complexType>",
    ],
)
def test_unsupported_shapes_are_explicit(tmp_path, shape):
    from pyxsd.mcp.services.models import InspectModelRequest, ModelService

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "schema.xsd": '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
                    f'<xs:element name="root">{shape}</xs:element></xs:schema>'
                },
            )
            with pytest.raises(ToolError, match="unsupported_projection"):
                await ModelService(inputs, pool).inspect_model(
                    InspectModelRequest(
                        workspace_id=workspace,
                        schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
                        element="root",
                    ),
                )

    anyio.run(scenario)


def test_model_tools_are_available_in_mcp_only_install_and_can_be_disabled(tmp_path):
    from tests.mcp.test_server import connected, create_workspace, structured

    async def scenario():
        async with connected(tmp_path / "enabled") as (client, _services):
            workspace = await create_workspace(client)
            await client.call_tool(
                "pyxsd_put_files",
                {
                    "workspace_id": workspace,
                    "files": [{"path": "schema.xsd", "text": XSD}],
                },
            )
            request = {
                "workspace_id": workspace,
                "schema_ref": {"source": {"path": "schema.xsd"}},
                "element": "order",
            }
            inspected = await client.call_tool("pyxsd_inspect_model", request)
            assert not inspected.is_error
            assert structured(inspected)["data"]["validation_scope"] == "projected-data"
            validated = await client.call_tool(
                "pyxsd_validate_data",
                {
                    **request,
                    "data": {"@id": "one", "amount": "1.25", "count": 4},
                },
            )
            assert structured(validated)["data"]["valid"] is True
        async with connected(tmp_path / "disabled", pydantic=False) as (client, _services):
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "pyxsd_inspect_model" not in names
            assert "pyxsd_validate_data" not in names

    anyio.run(scenario)


def test_projected_models_reuse_and_expire_with_parent_cache(tmp_path):
    import gc
    import weakref

    from tests.mcp.test_cache import request

    from pyxsd import __version__
    from pyxsd.mcp.cache import SchemaCache
    from pyxsd.mcp.config import Limits
    from pyxsd.mcp.services.models import execute_model
    from pyxsd.mcp.worker_protocol import ModelOperation
    from pyxsd.mcp.workers import capture_to_wire

    req = request(tmp_path, files={"root.xsd": XSD})
    cache = SchemaCache(Limits())
    operation = ModelOperation(
        request_id="models",
        capture=capture_to_wire(req.capture),
        schema_ref=req.schema_ref,
        element="order",
        action="inspect",
    )
    execute_model(operation, cache, __version__)
    with cache.acquire(req) as schema:
        model = schema._mcp_projected_models.model_for(element="order")
        parent_ref, model_ref = weakref.ref(schema), weakref.ref(model)
    execute_model(operation, cache, __version__)
    with cache.acquire(req) as schema:
        assert schema._mcp_projected_models.model_for(element="order") is model
    assert cache.info().misses == 1
    del schema, model
    cache.close()
    gc.collect()
    assert parent_ref() is None
    assert model_ref() is None


def test_local_declaration_selection_and_bounded_error_details(tmp_path):
    from pyxsd.mcp.config import Limits
    from pyxsd.mcp.services.models import InspectModelRequest, ModelService, ValidateDataRequest

    async def scenario():
        async with harness(tmp_path, Limits(collected_issues=1)) as (
            _,
            inputs,
            pool,
            workspace,
        ):
            await upload(inputs, workspace, {"schema.xsd": XSD})
            service = ModelService(inputs, pool)
            selected = InspectModelRequest(
                workspace_id=workspace,
                schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
                element="order",
                path=("amount",),
            )
            good = await service.validate_data(
                ValidateDataRequest(**selected.model_dump(), data="2.500")
            )
            assert good.data["value"] == "2.500"
            root = selected.model_copy(update={"path": ()})
            bad = await service.validate_data(
                ValidateDataRequest(
                    **root.model_dump(),
                    data={
                        "@id": 1,
                        "amount": "bad",
                        "count": "bad",
                    },
                ),
            )
            assert len(bad.data["errors"]) == 1
            assert bad.data["error_count"] >= 3
            assert bad.data["suppressed_count"] == bad.data["error_count"] - 1
            missing = selected.model_copy(update={"element": "absent"})
            with pytest.raises(ToolError, match="unsupported_projection"):
                await service.inspect_model(missing)

    anyio.run(scenario)


AMBIGUOUS_XSD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
  xmlns:a="urn:a" xmlns:b="urn:b">
  <xs:import namespace="urn:b" schemaLocation="b.xsd"/>
  <xs:element name="ingredient" type="xs:string"/>
</xs:schema>"""
AMBIGUOUS_B = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
  targetNamespace="urn:b">
  <xs:element name="ingredient" type="xs:string"/>
</xs:schema>"""


NAMESPACED_XSD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
  targetNamespace="urn:oii:core">
  <xs:element name="ingredient"><xs:complexType><xs:sequence>
  <xs:element name="name" type="xs:string"/>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>"""


def test_inspect_model_accepts_bare_local_name(tmp_path):
    from pyxsd.mcp.services.models import InspectModelRequest, ModelService

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            service = ModelService(inputs, pool)
            await upload(inputs, workspace, {"schema.xsd": NAMESPACED_XSD})
            clark = await service.inspect_model(
                InspectModelRequest(
                    workspace_id=workspace,
                    schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
                    element="{urn:oii:core}ingredient",
                )
            )
            bare = await service.inspect_model(
                InspectModelRequest(
                    workspace_id=workspace,
                    schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
                    element="ingredient",
                )
            )
            assert bare.data == clark.data

    anyio.run(scenario)


def test_inspect_model_accepts_root_id_form(tmp_path):
    from pyxsd.mcp.services.models import InspectModelRequest, ModelService

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            service = ModelService(inputs, pool)
            await upload(inputs, workspace, {"schema.xsd": NAMESPACED_XSD})
            clark = await service.inspect_model(
                InspectModelRequest(
                    workspace_id=workspace,
                    schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
                    element="{urn:oii:core}ingredient",
                )
            )
            by_id = await service.inspect_model(
                InspectModelRequest(
                    workspace_id=workspace,
                    schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
                    element="element:{urn:oii:core}ingredient",
                )
            )
            assert by_id.data == clark.data

    anyio.run(scenario)


def test_inspect_model_ambiguous_bare_name_lists_candidates(tmp_path):
    from pyxsd.mcp.services.models import InspectModelRequest, ModelService

    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            service = ModelService(inputs, pool)
            await upload(
                inputs,
                workspace,
                {"schema.xsd": AMBIGUOUS_XSD, "b.xsd": AMBIGUOUS_B},
            )
            request = InspectModelRequest(
                workspace_id=workspace,
                schema_ref=SchemaRef(source=FileRef(path="schema.xsd")),
                element="ingredient",
            )
            with pytest.raises(ToolError) as exc:
                await service.inspect_model(request)
            assert exc.value.fault.code == "invalid_argument"
            assert sorted(exc.value.fault.details["candidates"]) == [
                "element:ingredient",
                "element:{urn:b}ingredient",
            ]

    anyio.run(scenario)
