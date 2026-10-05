"""The dataset result contract must remain available without optional packages."""

import subprocess
import sys
from dataclasses import FrozenInstanceError

import pytest


def test_records_are_frozen_and_errors_carry_context(tmp_path):
    from pyxsd.integrations.dataset import DatasetExportError, DatasetResult

    result = DatasetResult(tmp_path, "partial", 2, 1, 1, 3, tmp_path / "manifest.json")
    with pytest.raises(FrozenInstanceError):
        result.rows = 4
    error = DatasetExportError("bad projection", source_id="A", stage="projection")
    assert error.source_id == "A"
    assert error.stage == "projection"
    assert "bad projection" in str(error)


def test_records_do_not_import_optional_dependencies():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
class Block:
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in ('pyarrow', 'pydantic'):
            raise AssertionError(fullname)
sys.meta_path.insert(0, Block())
from pyxsd.integrations.dataset import DatasetResult, DatasetExportError
assert 'pyarrow' not in sys.modules and 'pydantic' not in sys.modules
""",
        ],
        check=True,
    )
