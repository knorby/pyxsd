"""Strict optional-server contracts and exact JSON scalar encodings."""

import datetime as dt
import json
from dataclasses import dataclass
from decimal import Decimal
from xml.etree.ElementTree import QName

import pytest

pytest.importorskip("pydantic")

DEFAULTS = {
    "xml_file_bytes": 64 * 2**20,
    "schema_file_bytes": 16 * 2**20,
    "schema_closure_files": 128,
    "schema_closure_bytes": 64 * 2**20,
    "xml_depth": 256,
    "xml_elements": 1_000_000,
    "inline_bytes": 2**20,
    "request_bytes": 2 * 2**20,
    "workspace_bytes": 512 * 2**20,
    "artifact_bytes_per_workspace": 512 * 2**20,
    "workspaces": 32,
    "snapshot_entries": 4096,
    "artifact_entries": 4096,
    "snapshot_revisions": 32,
    "managed_bytes": 4 * 2**30,
    "cache_entries": 8,
    "workers": 4,
    "active_jobs_per_owner": 1,
    "queued_jobs": 16,
    "compile_query_seconds": 30,
    "transform_seconds": 60,
    "batch_seconds": 300,
    "transform_source_bytes": 64 * 2**10,
    "pipeline_steps": 32,
    "result_bytes": 24 * 2**10,
    "page_items": 50,
    "preview_rows": 20,
    "collected_issues": 10_000,
    "message_bytes": 4 * 2**10,
    "output_bytes": 128 * 2**20,
    "cache_idle_seconds": 900,
    "workspace_idle_seconds": 3600,
    "preview_seconds": 1800,
    "job_result_seconds": 1800,
    "diagnostic_seconds": 3600,
    "worker_operations": 100,
    "corpus_files": 100,
    "corpus_max_files": 1000,
    "corpus_file_seconds": 10,
}


def test_limits_match_the_design():
    from pyxsd.mcp.config import Limits

    assert Limits().model_dump() == DEFAULTS


@pytest.mark.parametrize("field", DEFAULTS)
@pytest.mark.parametrize("bad", [0, -1, True, "1", 1.5])
def test_each_budget_is_a_strict_positive_integer(field, bad):
    from pydantic import ValidationError
    from pyxsd.mcp.config import Limits

    with pytest.raises(ValidationError):
        Limits.model_validate({field: bad})


def test_configuration_and_wire_models_reject_unknown_or_coerced_fields(tmp_path):
    from pydantic import ValidationError
    from pyxsd.mcp.config import Limits, Settings
    from pyxsd.mcp.contracts import CompileOptions, FileRef, Page, SchemaRef, ToolEnvelope

    settings = Settings(state_dir=tmp_path)
    assert settings.execution_backend == "disabled"
    assert settings.limits == Limits()
    assert CompileOptions().model_dump() == {
        "binding": "lax",
        "namespaces": "strict",
        "xsd_version": "1.1",
        "namespace_schemas": {},
    }
    schema = SchemaRef(source=FileRef(path="schema.xsd"))
    assert schema.options == CompileOptions()
    assert Page().limit == 50
    assert ToolEnvelope(data={"valid": False}).status == "ok"
    for cls, values in [
        (Limits, {"unbounded": True}),
        (Settings, {"state_dir": tmp_path, "unknown": True}),
        (FileRef, {"path": "x", "owner_id": "someone"}),
        (FileRef, {"path": 1}),
        (FileRef, {"path": ""}),
        (CompileOptions, {"namespaces": "lax"}),
        (CompileOptions, {"facets": "off"}),
        (Page, {"limit": 0}),
        (Settings, {"state_dir": tmp_path, "execution_backend": "unsafe"}),
    ]:
        with pytest.raises(ValidationError):
            cls.model_validate(values)


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        (True, True),
        (False, False),
        (1, 1),
        (2**53 - 1, 2**53 - 1),
        (2**53, str(2**53)),
        (-(2**53), str(-(2**53))),
        (1.25, 1.25),
        (float("nan"), "NaN"),
        (float("inf"), "INF"),
        (float("-inf"), "-INF"),
        (Decimal("0.10"), "0.10"),
        (Decimal("NaN"), "NaN"),
        (Decimal("Infinity"), "Infinity"),
        ("text", "text"),
        (b"\x00\xff", {"encoding": "base64", "data": "AP8="}),
        (dt.datetime(2026, 10, 26, 12, 30, tzinfo=dt.UTC), "2026-10-26T12:30:00+00:00"),
        (dt.date(2026, 10, 26), "2026-10-26"),
        (dt.time(12, 30), "12:30:00"),
        (QName("urn:a/b", "name"), "{urn:a/b}name"),
        ({"a": (Decimal("1.20"), 2**60)}, {"a": ["1.20", str(2**60)]}),
    ],
)
def test_exact_wire_values(value, expected):
    from pyxsd.mcp.encoding import encode_value

    result = encode_value(value)
    assert result == expected
    assert json.loads(json.dumps(result, allow_nan=False)) == expected
    if isinstance(expected, bool):
        assert result is expected


def test_core_descriptions_and_xsd_values_have_explicit_encodings():
    from pyxsd.locations import IssueLocation, PathStep
    from pyxsd.mcp.encoding import encode_value
    from pyxsd.xsd_data_types import Boolean, DateTime, QName

    assert encode_value(Boolean("true")) is True
    assert encode_value(Boolean("false")) is False
    assert (
        encode_value(DateTime("2000-01-01T00:00:00.123456789Z")) == "2000-01-01T00:00:00.123456789Z"
    )
    assert encode_value(QName("p:name")) == "p:name"
    location = IssueLocation("a.xml", (PathStep("{urn:x/y}r", 1),))
    assert encode_value(location) == {
        "source": "a.xml",
        "path": [{"name": "{urn:x/y}r", "index": 1}],
        "attribute": None,
    }


@pytest.mark.parametrize(
    "value",
    [object(), {1: "x"}, {"x"}],
)
def test_unsupported_values_fail_with_typed_fault_not_repr(value):
    from pyxsd.mcp.encoding import encode_value
    from pyxsd.mcp.errors import ToolError

    with pytest.raises(ToolError) as exc:
        encode_value(value)
    assert exc.value.fault.code == "unsupported_value"
    assert exc.value.fault.retryable is False
    assert "0x" not in exc.value.fault.message


def test_codec_rejects_cycles_and_excess_depth_without_recursion_errors():
    from pyxsd.mcp.encoding import encode_value
    from pyxsd.mcp.errors import ToolError

    cyclic = []
    cyclic.append(cyclic)
    for value in (cyclic, [[[0]]]):
        with pytest.raises(ToolError, match="unsupported_value"):
            encode_value(value, max_depth=2)
    with pytest.raises(ValueError, match="positive"):
        encode_value(1, max_depth=0)
    shared = [1]
    assert encode_value([shared, shared]) == [[1], [1]]


def test_unregistered_dataclass_is_not_an_implicit_wire_contract():
    from pyxsd.mcp.encoding import encode_value
    from pyxsd.mcp.errors import ToolError

    @dataclass
    class Secret:
        password: str

    with pytest.raises(ToolError, match="unsupported_value"):
        encode_value(Secret("not-for-the-wire"))


def test_wire_records_and_fault_details_are_explicit_json():
    from pydantic import ValidationError
    from pyxsd.mcp.contracts import ArtifactRef, PageResult, ToolEnvelope
    from pyxsd.mcp.encoding import encode_value
    from pyxsd.mcp.errors import ToolError

    artifact = ArtifactRef(
        id="opaque", digest="a" * 64, size=0, media_type="text/plain", expires_at=1.0
    )
    envelope = ToolEnvelope(artifacts=[artifact], page=PageResult(returned=0))
    assert encode_value(envelope) == json.loads(envelope.model_dump_json())
    fault = ToolError("busy", "Queue is full", retryable=True, details={"limit": 1}).fault
    assert encode_value(fault)["details"] == {"limit": 1}
    assert fault.retryable
    with pytest.raises(ValidationError):
        ToolEnvelope(data=float("nan"))


def test_unsafe_integer_encoding_does_not_depend_on_interpreter_digit_limit():
    from pyxsd.mcp.encoding import encode_value

    assert encode_value(10**5000) == "1" + "0" * 5000


@pytest.mark.parametrize("missing", ["pydantic", "pydantic._internal"])
def test_optional_dependency_errors_distinguish_absence_from_broken_installation(
    monkeypatch, missing
):
    import builtins
    import runpy

    from pyxsd.mcp import contracts

    original = builtins.__import__

    def intercepted(name, *args, **kwargs):
        if name == "pydantic":
            raise ModuleNotFoundError("simulated unavailable dependency", name=missing)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", intercepted)
    with pytest.raises(ImportError) as exc:
        runpy.run_path(contracts.__file__)
    if missing == "pydantic":
        assert "pyxsd[mcp]" in str(exc.value)
    else:
        assert exc.value.name == missing
        assert "Install" not in str(exc.value)
