"""Cold import isolation and actionable missing-extra diagnostics."""

import importlib.util
import subprocess
import sys

import pytest


def cold(script, blocked):
    guard = f"""import sys
class Block:
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == x or fullname.startswith(x + '.') for x in {blocked!r}):
            raise ModuleNotFoundError('blocked ' + fullname, name=fullname)
sys.meta_path.insert(0, Block())
"""
    result = subprocess.run([sys.executable, "-c", guard + script], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_base_compile_parse_and_export_do_not_import_optional_backends():
    cold(
        """from io import StringIO
import pyxsd
import pyxsd.integrations
s = pyxsd.compile(StringIO('<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"><xs:element name="r" type="xs:int"/></xs:schema>'))
assert s.parse(StringIO('<r>7</r>')).to_dict() == {'$': 7}
assert 'pydantic' not in sys.modules and 'pyarrow' not in sys.modules
""",
        ["pydantic", "pyarrow"],
    )


@pytest.mark.parametrize(
    "module, missing, extra",
    [
        ("pydantic", "pydantic", "pydantic"),
        ("arrow", "pyarrow", "arrow"),
    ],
)
def test_missing_optional_dependency_names_the_extra(module, missing, extra):
    cold(
        f"""try:
    __import__('pyxsd.integrations.{module}')
except ImportError as exc:
    assert 'pyxsd[{extra}]' in str(exc), str(exc)
else:
    raise AssertionError('missing library was accepted')
""",
        [missing],
    )


@pytest.mark.parametrize(
    "module, available, blocked",
    [
        ("pydantic", "pydantic", "pydantic.errors"),
        ("arrow", "pyarrow", "pyarrow.parquet"),
    ],
)
def test_broken_optional_installation_is_not_mislabeled_missing(module, available, blocked):
    if importlib.util.find_spec(available) is None:
        pytest.skip(f"{available} not installed in this environment")
    cold(
        f"""try:
    __import__('pyxsd.integrations.{module}')
except ModuleNotFoundError as exc:
    assert exc.name == '{blocked}', exc.name
    assert 'Install pyxsd' not in str(exc)
else:
    raise AssertionError('broken library was accepted')
""",
        [blocked],
    )


@pytest.mark.parametrize(
    "module, available, blocked",
    [
        ("pydantic", "pydantic", "pyarrow"),
        ("arrow", "pyarrow", "pydantic"),
    ],
)
def test_independently_installed_extra_works_without_the_other(module, available, blocked):
    if importlib.util.find_spec(available) is None:
        pytest.skip(f"{available} not installed in this environment")
    cold(
        f"""from io import StringIO
import pyxsd
from pyxsd.integrations import {module} as adapter
s = pyxsd.compile(StringIO('<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"><xs:element name="r" type="xs:int"/></xs:schema>'))
d = s.parse(StringIO('<r>7</r>'))
if '{module}' == 'pydantic':
    assert adapter.models(s).from_document(d).root == 7
else:
    assert adapter.records(s, element='r').table(d).to_pylist() == [{{'value': 7}}]
assert '{blocked}' not in sys.modules
""",
        [blocked],
    )
