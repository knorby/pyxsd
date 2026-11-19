"""Explicit schema-language dispatch, budgets, and cache identity for RELAX NG."""

import gc
import hashlib
import io
import weakref

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("defusedxml")

from pyxsd import Schema, __version__
from pyxsd.exceptions import PyXSDError
from pyxsd.mcp.cache import (
    CompileRequest,
    SchemaCache,
    compile_schema_ref,
    require_complete_schema,
    require_xsd_schema,
)
from pyxsd.mcp.captures import (
    CaptureFile,
    InputCapture,
    InputResourcePolicy,
)
from pyxsd.mcp.config import Limits, relaxng_limits_for_mcp
from pyxsd.mcp.contracts import CompileOptions, FileRef, SchemaRef
from pyxsd.mcp.errors import ToolError
from pyxsd.relaxng import RelaxNGLimitError, RelaxNGLimits, RelaxNGSchema
from pyxsd.relaxng.datatypes import UnsupportedDatatypeError

NS = "http://relaxng.org/ns/structure/1.0"
XSD_DATATYPES = "http://www.w3.org/2001/XMLSchema-datatypes"

XSD = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
    '<xs:element name="root" type="xs:int"/></xs:schema>'
)
XSD_DOCUMENT = "<root>3</root>"

LIBRARY_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="root">
      <attribute name="count"><data type="integer" datatypeLibrary="{XSD_DATATYPES}"/></attribute>
      <data type="string"/>
    </element>
  </start>
</grammar>"""
LIBRARY_DOCUMENT = '<root count="7">text</root>'

WORK_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="root">
      <optional>
        <attribute name="a"><data type="integer" datatypeLibrary="{XSD_DATATYPES}"/></attribute>
      </optional>
      <oneOrMore>
        <element name="item"><data type="integer" datatypeLibrary="{XSD_DATATYPES}"/></element>
      </oneOrMore>
    </element>
  </start>
</grammar>"""
WORK_DOCUMENT = '<root a="1"><item>1</item><item>2</item></root>'

INVALID_RELAXNG = f'<grammar xmlns="{NS}"><start><element/></start></grammar>'

#: ``Limits`` field -> mapped ``RelaxNGLimits`` field.
BUDGET_FIELDS = {
    "relaxng_pattern_nodes": "max_pattern_nodes",
    "relaxng_derivative_states": "max_derivative_states",
    "relaxng_memo_entries": "max_memo_entries",
    "relaxng_evidence_states": "max_evidence_states",
    "relaxng_evidence_edges": "max_evidence_edges",
    "relaxng_scalar_witnesses": "max_scalar_witnesses",
    "relaxng_binding_summaries": "max_binding_summaries",
    "relaxng_capture_entries": "max_capture_entries",
    "relaxng_projection_routes": "max_projection_routes",
    "relaxng_diagnostics": "max_diagnostics",
}

COMPILE_BUDGETS = ("relaxng_pattern_nodes", "relaxng_projection_routes")
PARSE_BUDGETS = (
    "relaxng_derivative_states",
    "relaxng_memo_entries",
    "relaxng_evidence_states",
    "relaxng_evidence_edges",
    "relaxng_scalar_witnesses",
    "relaxng_binding_summaries",
)


def capture_of(root, files, *, workspace="workspace", generation=1):
    entries = []
    for name in sorted(files):
        data = files[name].encode("utf-8")
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        entries.append(CaptureFile(name, hashlib.sha256(data).hexdigest(), len(data)))
    return InputCapture(workspace, generation, root, tuple(entries))


def request_of(
    root,
    *,
    schema=LIBRARY_SCHEMA,
    schema_format="relax-ng",
    source="root.rng",
    document=LIBRARY_DOCUMENT,
    document_name="document.xml",
    files=None,
    options=None,
    hints=(),
    processor_version=__version__,
):
    content = {source: schema, document_name: document}
    content.update(files or {})
    return CompileRequest(
        capture=capture_of(root, content),
        schema_ref=SchemaRef(
            source=FileRef(path=source),
            options=options or CompileOptions(),
            format=schema_format,
        ),
        hints=hints,
        processor_version=processor_version,
    )


def test_old_xsd_reference_keeps_xsd_defaults(tmp_path):
    reference = SchemaRef.model_validate({"source": {"path": "root.xsd"}})
    assert reference.format == "xsd"
    assert "format" not in reference.model_dump(exclude_unset=True)
    assert reference.model_dump(mode="json")["format"] == "xsd"

    expected = Schema.compile(io.StringIO(XSD))
    cache = SchemaCache(Limits(enforced=True))
    try:
        request = request_of(
            tmp_path,
            schema=XSD,
            schema_format="xsd",
            source="root.xsd",
            document=XSD_DOCUMENT,
        )
        with cache.acquire(request) as schema:
            assert type(schema) is Schema
            assert schema.report.to_dict() == expected.report.to_dict()
            assert schema.parse("/workspace/document.xml").root == 3
        with cache.acquire(request) as reused:
            assert reused is schema
        assert cache.info().hits == cache.info().misses == 1
    finally:
        cache.close()


def test_explicit_relaxng_compiles_only_through_the_public_relaxng_compiler(tmp_path, monkeypatch):
    import pyxsd.relaxng as relaxng

    calls = []
    original = relaxng.compile

    def spy(source, **kwargs):
        calls.append((source, kwargs))
        return original(source, **kwargs)

    monkeypatch.setattr(relaxng, "compile", spy)

    def forbidden(*args, **kwargs):
        raise AssertionError("the XSD compiler handled a RELAX NG reference")

    monkeypatch.setattr(Schema, "compile", forbidden)
    cache = SchemaCache(Limits(enforced=True))
    try:
        with cache.acquire(request_of(tmp_path)) as schema:
            assert type(schema) is RelaxNGSchema
            assert schema.report.has_errors is False
            assert schema.parse("/workspace/document.xml").is_valid
            policy = schema._resource_policy
            assert isinstance(policy, InputResourcePolicy)
        assert len(calls) == 1
        source, kwargs = calls[0]
        assert source == "/workspace/root.rng"
        assert set(kwargs) == {"resource_policy", "limits"}
        assert kwargs["resource_policy"] is policy
        assert kwargs["limits"] == relaxng_limits_for_mcp(cache.limits)
    finally:
        cache.close()


def test_schema_language_is_never_inferred_from_the_filename(tmp_path):
    cache = SchemaCache(Limits(enforced=True))
    try:
        rng_named = request_of(tmp_path, schema_format="xsd")
        with pytest.raises(PyXSDError), cache.acquire(rng_named):
            pytest.fail("a .rng filename selected the RELAX NG compiler")

        xsd_named = request_of(tmp_path / "second", source="schema.xsd")
        with cache.acquire(xsd_named) as schema:
            assert type(schema) is RelaxNGSchema
            assert schema.report.has_errors is False
    finally:
        cache.close()


def test_same_bytes_under_different_formats_are_distinct_entries(tmp_path):
    identical = '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"/>'
    cache = SchemaCache(Limits(enforced=True))
    try:
        xsd_request = request_of(tmp_path, schema=identical, schema_format="xsd", source="root.xsd")
        rng_request = request_of(tmp_path, schema=identical, source="root.xsd")
        with cache.acquire(xsd_request) as xsd_schema:
            assert type(xsd_schema) is Schema
            assert xsd_schema.report.has_errors is False
            with cache.acquire(rng_request) as rng_schema:
                assert type(rng_schema) is RelaxNGSchema
                assert rng_schema.report.has_errors
                assert rng_schema is not xsd_schema
        assert len(cache._entries) == 2
        assert cache.info().misses == 2
        with cache.acquire(xsd_request) as reused:
            assert reused is xsd_schema
        assert cache.info().hits == 1
    finally:
        cache.close()


def test_relaxng_dependency_bytes_invalidate_the_cached_schema(tmp_path):
    root_schema = (
        f'<grammar xmlns="{NS}"><include href="dep.rng"/>'
        '<start><element name="root"><ref name="content"/></element></start></grammar>'
    )
    first_dependency = f'<grammar xmlns="{NS}"><define name="content"><text/></define></grammar>'
    second_dependency = (
        f'<grammar xmlns="{NS}">'
        '<define name="content"><element name="value"><text/></element></define>'
        "</grammar>"
    )
    third_dependency = f'<grammar xmlns="{NS}"><define name="content"><empty/></define></grammar>'
    cache = SchemaCache(Limits(enforced=True))
    try:
        first = request_of(
            tmp_path,
            schema=root_schema,
            document="<root>hi</root>",
            files={"dep.rng": first_dependency},
        )
        with cache.acquire(first) as before:
            assert not before.report.has_errors
            assert [dep.source_id for dep in before.dependencies] == [
                "/workspace/dep.rng",
                "/workspace/root.rng",
            ]
        second = request_of(
            tmp_path,
            schema=root_schema,
            document="<root>hi</root>",
            files={"dep.rng": second_dependency},
        )
        with cache.acquire(second) as after:
            assert after is not before
            assert not after.report.has_errors
        assert cache.info().misses == 2

        (tmp_path / "dep.rng").write_text(third_dependency, encoding="utf-8")
        with cache.acquire(second) as replaced:
            assert replaced is not after
            assert replaced.report.has_errors
            assert "relaxng-resource-read" in {issue.code for issue in replaced.report.issues}
            with pytest.raises(ToolError) as raised:
                require_complete_schema(replaced)
        assert raised.value.fault.code == "invalid_schema"
        assert cache.info().stale == 1
        with cache.acquire(second) as confirmed:
            assert confirmed is replaced
        assert cache.info().stale == 1
        assert cache.info().hits == 1
    finally:
        cache.close()


def test_relaxng_missing_dependency_fails_closed(tmp_path):
    root_schema = (
        f'<grammar xmlns="{NS}"><include href="missing.rng"/>'
        '<start><element name="root"><text/></element></start></grammar>'
    )
    cache = SchemaCache(Limits(enforced=True))
    try:
        request = request_of(tmp_path, schema=root_schema, document="<root/>")
        with cache.acquire(request) as schema:
            assert schema.report.has_errors
            with pytest.raises(ToolError) as raised:
                require_complete_schema(schema)
        assert raised.value.fault.code == "schema_incomplete"
        assert raised.value.fault.details["missing"] == ["missing.rng"]
    finally:
        cache.close()


@pytest.mark.parametrize(
    ("field", "options", "hints"),
    [
        ("binding", CompileOptions(binding="strict"), ()),
        ("namespaces", CompileOptions(namespaces="legacy"), ()),
        ("xsd_version", CompileOptions(xsd_version="1.0"), ()),
        (
            "namespace_schemas",
            CompileOptions(namespace_schemas={"urn:extra": FileRef(path="extra.xsd")}),
            (),
        ),
        ("hints", CompileOptions(), (("urn:extra", "extra.xsd"),)),
    ],
)
def test_relaxng_rejects_nondefault_xsd_only_compile_inputs(
    tmp_path, monkeypatch, field, options, hints
):
    import pyxsd.relaxng as relaxng

    calls = []

    def reject(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("a rejected RELAX NG request reached the compiler")

    monkeypatch.setattr(relaxng, "compile", reject)
    cache = SchemaCache(Limits(enforced=True))
    try:
        request = request_of(tmp_path, options=options, hints=hints)
        with pytest.raises(ToolError) as raised, cache.acquire(request):
            pytest.fail("nondefault XSD compile input accepted for RELAX NG")
        assert raised.value.fault.code == "invalid_argument"
        assert raised.value.fault.details["rejected"] == [field]
        assert calls == []
        assert cache.info().entries == 0
    finally:
        cache.close()


@pytest.mark.parametrize("field", ["datatype_libraries", "libraries", "factory", "xsd_factory"])
def test_wire_contract_has_no_custom_or_executable_schema_factory(field):
    from pydantic import ValidationError

    assert field not in SchemaRef.model_fields
    with pytest.raises(ValidationError):
        SchemaRef.model_validate({"source": {"path": "root.rng"}, "format": "relax-ng", field: {}})
    with pytest.raises(ValidationError):
        SchemaRef.model_validate({"source": {"path": "root.rng"}, "format": "rng"})


def test_unsupported_relaxng_datatype_library_is_explicit(tmp_path):
    schema = (
        f'<grammar xmlns="{NS}"><start><element name="root">'
        '<data type="custom" datatypeLibrary="urn:example:custom"/>'
        "</element></start></grammar>"
    )
    cache = SchemaCache(Limits(enforced=True))
    try:
        request = request_of(tmp_path, schema=schema, document="<root/>")
        with pytest.raises(UnsupportedDatatypeError), cache.acquire(request):
            pytest.fail("an unsupported datatype library was ignored")
        assert cache.info().entries == 0
    finally:
        cache.close()


def test_require_xsd_schema_narrows_xsd_and_rejects_relaxng(tmp_path):
    cache = SchemaCache(Limits(enforced=True))
    try:
        with cache.acquire(request_of(tmp_path)) as schema:
            with pytest.raises(ToolError) as raised:
                require_xsd_schema(schema)
            assert raised.value.fault.code == "unsupported_capability"
            assert raised.value.fault.details == {"format": "relax-ng"}
        xsd_request = request_of(
            tmp_path / "xsd",
            schema=XSD,
            schema_format="xsd",
            source="root.xsd",
            document=XSD_DOCUMENT,
        )
        with cache.acquire(xsd_request) as xsd_schema:
            assert require_xsd_schema(xsd_schema) is xsd_schema
    finally:
        cache.close()


def test_cache_key_covers_the_fixed_limit_configuration(tmp_path):
    from pyxsd.mcp.cache import _key

    request = request_of(tmp_path)
    assert _key(request, Limits(enforced=True)) == _key(request, Limits(enforced=True))
    assert _key(request, Limits(enforced=True)) != _key(
        request, Limits(enforced=True, relaxng_pattern_nodes=2)
    )


def test_every_relaxng_budget_maps_independently(tmp_path):
    configured = {field: 100 + index for index, field in enumerate(BUDGET_FIELDS)}
    limits = Limits(enforced=True, **configured)
    mapped = relaxng_limits_for_mcp(limits)
    assert mapped == RelaxNGLimits(
        **{budget: configured[field] for field, budget in BUDGET_FIELDS.items()}
    )
    assert relaxng_limits_for_mcp(Limits()) == RelaxNGLimits()
    assert relaxng_limits_for_mcp(Limits(relaxng_pattern_nodes=5)).max_pattern_nodes is None

    cache = SchemaCache(limits)
    try:
        request = request_of(tmp_path)
        direct = compile_schema_ref(request, InputResourcePolicy(request.capture, limits))
        assert isinstance(direct, RelaxNGSchema)
        assert direct.limits == mapped
        with cache.acquire(request) as schema:
            assert schema.limits == mapped
    finally:
        cache.close()


@pytest.mark.parametrize("field", [*COMPILE_BUDGETS, *PARSE_BUDGETS])
def test_relaxng_work_budget_exhaustion_is_operational(tmp_path, field):
    budget = BUDGET_FIELDS[field]
    cache = SchemaCache(Limits(enforced=True, **{field: 1}))
    try:
        request = request_of(tmp_path, schema=WORK_SCHEMA, document=WORK_DOCUMENT)
        if field in COMPILE_BUDGETS:
            with pytest.raises(RelaxNGLimitError) as raised, cache.acquire(request):
                pytest.fail("an exhausted compilation produced a schema")
            assert cache.info().entries == 0
        else:
            with cache.acquire(request) as schema:
                require_complete_schema(schema)
                with pytest.raises(RelaxNGLimitError) as raised:
                    schema.parse("/workspace/document.xml")
        assert raised.value.budget == budget
        assert raised.value.configured == 1
        assert raised.value.observed >= 2
        assert not isinstance(raised.value, ToolError)
    finally:
        cache.close()


def test_relaxng_capture_and_diagnostic_budgets_truncate_without_raising(tmp_path):
    capture_cache = SchemaCache(Limits(enforced=True, relaxng_capture_entries=1))
    try:
        request = request_of(tmp_path / "capture", schema=WORK_SCHEMA, document=WORK_DOCUMENT)
        with capture_cache.acquire(request) as schema:
            document = schema.parse("/workspace/document.xml", capture="patterns")
            assert document.is_valid
            assert document.annotations is not None
            assert document.annotations.complete is False
    finally:
        capture_cache.close()

    diagnostics_cache = SchemaCache(Limits(enforced=True, relaxng_diagnostics=1))
    try:
        request = request_of(tmp_path / "diagnostics", schema=INVALID_RELAXNG, document="<root/>")
        with diagnostics_cache.acquire(request) as schema:
            assert schema.report.has_errors
            assert schema.report.error_count == 3
            assert schema.report.suppressed_count == 2
            assert schema.report.diagnostics_complete is False
            with pytest.raises(ToolError) as raised:
                require_complete_schema(schema)
        assert raised.value.fault.code == "invalid_schema"
        assert raised.value.fault.details == {"errors": 3}
    finally:
        diagnostics_cache.close()


def test_relaxng_leases_are_nonreentrant(tmp_path):
    cache = SchemaCache(Limits(enforced=True))
    try:
        request = request_of(tmp_path)
        with cache.acquire(request) as schema:
            with pytest.raises(ToolError, match="cache_busy"), cache.acquire(request):
                pytest.fail("RELAX NG leases cannot share one schema")
            assert cache.info().active == 1
            cache.close()
            assert cache.info().entries == 1
            assert schema.parse("/workspace/document.xml").is_valid
        assert cache.info().entries == cache.info().active == 0
    finally:
        cache.close()


def test_relaxng_parses_leave_no_documents_in_the_cache(tmp_path):
    cache = SchemaCache(Limits(enforced=True))
    try:
        with cache.acquire(request_of(tmp_path)) as schema:
            references = []
            for _ in range(50):
                document = schema.parse("/workspace/document.xml")
                assert document.is_valid
                references.append(weakref.ref(document))
            del document
            gc.collect()
            assert all(reference() is None for reference in references)
        assert cache.info().active == 0
    finally:
        cache.close()
