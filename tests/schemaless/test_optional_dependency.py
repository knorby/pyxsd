"""Optional JSON Schema behavior when the evaluator is not installed."""

import os
import subprocess
import sys

import pytest

from pyxsd.schemaless import JsonSchemaError, from_data


class _BlockOptionalModules:
    """Refuse imports from the optional JSON Schema evaluator stack."""

    blocked = frozenset({"jsonschema", "referencing"})

    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition(".")[0] in self.blocked:
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


BLOCKED_IMPORT_SCRIPT = """
import io
import sys
from decimal import Decimal

class _BlockOptionalModules:
    blocked = frozenset({"jsonschema", "referencing"})

    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition(".")[0] in self.blocked:
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None

sys.meta_path.insert(0, _BlockOptionalModules())
assert "jsonschema" not in sys.modules
assert "referencing" not in sys.modules

from pyxsd.schemaless import JsonSchemaError, XmlMapping, from_data, from_json, parse_xml

xml = parse_xml(io.StringIO('<root id="1"><item>7</item><item>8</item></root>'))
assert xml.to_string() == '<root id="1"><item>7</item><item>8</item></root>'
assert xml.root.attribute_lexical("id") == "1"
assert [item.text_value() for item in xml.root.children] == ["7", "8"]
assert xml.to_xdm() is not None

data = from_json('{"n": 1.5, "items": [1, 2]}')
assert data.value_at("/n") == Decimal("1.5")
assert data.to_json() == '{"n": 1.5, "items": [1, 2]}'
assert from_data({"a": (1, 2)}).to_data() == {"a": [1, 2]}
assert data.to_xdm() is not None
explicit = data.to_xml(XmlMapping.elements(root_name="root"))
assert explicit.to_string() == (
    "<root><n>1.5</n><items><item>1</item><item>2</item></items></root>"
)

for operation in (
    lambda: from_data({}, json_schema={}),
    lambda: from_json("{}", json_schema={}),
    lambda: XmlMapping.from_json_schema({}, root_name="root"),
    lambda: XmlMapping.from_openapi({}, root_name="root"),
):
    try:
        operation()
    except JsonSchemaError as error:
        assert "pyxsd[jsonschema]" in str(error)
    else:
        raise AssertionError("schema guidance must require the pyxsd[jsonschema] extra")
"""


@pytest.fixture(autouse=True)
def _without_optional_schema_stack(monkeypatch):
    """Block the optional imports for the literal in-process assertion.

    ``test_blocked_optional_stack_in_a_fresh_interpreter`` repeats the contract
    in a clean process; this fixture supplies the same blocked environment for
    this module's process and restores every module and import hook afterwards.
    """
    blocker = _BlockOptionalModules()
    for name in [
        module
        for module in sys.modules
        if module.partition(".")[0] in _BlockOptionalModules.blocked
    ]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [blocker, *sys.meta_path])


def test_base_install_has_explicit_schema_capability_error():
    # in a subprocess that blocks jsonschema/referencing imports
    with pytest.raises(JsonSchemaError, match="pyxsd\\[jsonschema\\]"):
        from_data({}, json_schema={})


def test_blocked_optional_stack_in_a_fresh_interpreter():
    result = subprocess.run(
        [sys.executable, "-c", BLOCKED_IMPORT_SCRIPT],
        env={**os.environ, "PYTHONPATH": ""},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
