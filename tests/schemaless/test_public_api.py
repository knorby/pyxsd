"""The finalized ``pyxsd.schemaless`` package surface and its cold import."""

import os
import subprocess
import sys

import pytest

DESIGNED_PUBLIC_NAMES = frozenset(
    {
        "DocumentProfile",
        "JsonDocument",
        "JsonLimitError",
        "JsonLimits",
        "JsonParseError",
        "JsonSchemaError",
        "MappingRule",
        "NativeJsonValue",
        "NodeMapping",
        "PathProfile",
        "ProfileLimits",
        "ScalarAdapter",
        "ScalarTypeError",
        "SchemalessDocument",
        "SchemalessElement",
        "SchemalessParseError",
        "SchemalessRevision",
        "TypeCandidate",
        "TypePolicy",
        "XmlMapping",
        "XmlMappingError",
        "attribute",
        "child",
        "children",
        "from_data",
        "from_json",
        "parse_xml",
        "text",
    }
)

INTERNAL_NAMES = frozenset(
    {
        "SchemaGuidance",
        "SchemaOccurrence",
        "compile_openapi_mapping",
        "copy_native_data",
        "decode_json",
        "encode_json",
        "escape_pointer_segment",
        "json_to_xdm",
        "resolve_pointer",
    }
)

COLD_IMPORT_SCRIPT = """
import io
import sys

import pyxsd.schemaless as s

assert "jsonschema" not in sys.modules
assert "referencing" not in sys.modules
assert {"parse_xml", "from_json", "from_data", "TypePolicy", "XmlMapping"} <= set(s.__all__)
document = s.from_json('{"n": 1.5}')
assert document.to_json() == '{"n": 1.5}'
xml = s.parse_xml(io.StringIO("<r><x>1</x></r>"))
assert xml.to_string() == "<r><x>1</x></r>"
"""


@pytest.fixture(autouse=True)
def _hide_installed_jsonschema(monkeypatch):
    """Make the in-process export assertion independent of test collection.

    Other test modules import ``jsonschema`` for their own fixtures, so a
    session that has already collected them keeps it in ``sys.modules``.
    ``test_cold_import_in_a_fresh_interpreter`` proves the real guarantee;
    this fixture only removes the collection-order accident from the literal
    in-process assertion and restores the modules afterwards.
    """
    for name in [
        module
        for module in sys.modules
        if module == "jsonschema" or module.startswith("jsonschema.")
    ]:
        monkeypatch.delitem(sys.modules, name)


def test_public_schemaless_exports_and_cold_import():
    import pyxsd.schemaless as s

    assert {"parse_xml", "from_json", "from_data", "TypePolicy", "XmlMapping"} <= set(s.__all__)
    assert "jsonschema" not in sys.modules  # use a subprocess for reliable cold import


def test_every_designed_public_name_is_exported_exactly_once():
    import pyxsd.schemaless as s

    assert set(s.__all__) == DESIGNED_PUBLIC_NAMES
    assert len(s.__all__) == len(set(s.__all__))
    for name in DESIGNED_PUBLIC_NAMES:
        assert getattr(s, name) is not None


def test_guidance_and_projection_machinery_stays_private():
    import pyxsd.schemaless as s

    for name in INTERNAL_NAMES:
        assert name not in s.__all__
        assert not hasattr(s, name)
    assert not any(name.startswith("_") for name in s.__all__)


def test_cold_import_in_a_fresh_interpreter():
    result = subprocess.run(
        [sys.executable, "-c", COLD_IMPORT_SCRIPT],
        env={**os.environ, "PYTHONPATH": ""},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
