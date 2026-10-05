"""The data workflow guide's commands are executable and stay in sync.

Every shell command shown in ``docs/data-workflows.md`` is listed here
verbatim, run as a subprocess from a temporary working directory, and must
produce the documented output fragments. Consumer-dependent commands skip
when their package is absent unless ``PYXSD_REQUIRE_EXAMPLES=1`` is set
(the dedicated consumer CI jobs set it).
"""

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "data-workflows.md"
PARQUET = "observations.parquet"

# (documented command, argv after the interpreter, required module, fragments)
COMMANDS = [
    (
        "python examples/analytics/duckdb_observations.py",
        ["examples/analytics/duckdb_observations.py"],
        "duckdb",
        [
            "Reading units: [(1, 'mg/L'), (2, 'mg/L'), (3, 'mg/L'), (4, 'mg/L')]",
            "Child row counts: {'tags': 4, 'replicates': 8}",
            "Station regions: [(1, 'river'), (2, 'river'), (3, 'river'), (4, 'coastal')]",
            "Passing quality sequences: [1, 4]",
            "dissolved oxygen [mg/L] readings=2 populated=2 total=12.000",
            "nitrate [mg/L] readings=2 populated=1 total=0.015",
        ],
    ),
    (
        "python examples/analytics/duckdb_observations.py --write-parquet observations.parquet",
        ["examples/analytics/duckdb_observations.py", "--write-parquet", PARQUET],
        "duckdb",
        ["dissolved oxygen [mg/L] readings=2 populated=2 total=12.000", "total=0.015"],
    ),
    (
        "python examples/analytics/duckdb_observations.py --parquet observations.parquet",
        ["examples/analytics/duckdb_observations.py", "--parquet", PARQUET],
        "duckdb",
        ["dissolved oxygen [mg/L] readings=2 populated=2 total=12.000", "total=0.015"],
    ),
    (
        "python examples/analytics/polars_observations.py",
        ["examples/analytics/polars_observations.py"],
        "polars",
        [
            "Reading units: [(1, 'mg/L'), (2, 'mg/L'), (3, 'mg/L'), (4, 'mg/L')]",
            "Child row counts: {'tags': 4, 'replicates': 8}",
            "Station regions: [(1, 'river'), (2, 'river'), (3, 'river'), (4, 'coastal')]",
            "Passing quality sequences: [1, 4]",
            "dissolved oxygen [mg/L] readings=2 populated=2 total=12.000",
            "nitrate [mg/L] readings=2 populated=1 total=0.015",
        ],
    ),
    (
        "python examples/analytics/polars_observations.py --write-parquet observations.parquet",
        ["examples/analytics/polars_observations.py", "--write-parquet", PARQUET],
        "polars",
        ["dissolved oxygen [mg/L] readings=2 populated=2 total=12.000", "total=0.015"],
    ),
    (
        "python examples/analytics/polars_observations.py --parquet observations.parquet",
        ["examples/analytics/polars_observations.py", "--parquet", PARQUET],
        "polars",
        ["dissolved oxygen [mg/L] readings=2 populated=2 total=12.000", "total=0.015"],
    ),
    (
        "python examples/pydantic/assess_orders.py",
        ["examples/pydantic/assess_orders.py"],
        "pydantic",
        [
            '"disposition": "approval_required"',
            '"subtotal": "681.90"',
            '"subtotal": "450.00"',
            '"subtotal": "100.00"',
        ],
    ),
    (
        "python examples/pydantic/assess_orders.py examples/pydantic/orders_review.xml",
        ["examples/pydantic/assess_orders.py", "examples/pydantic/orders_review.xml"],
        "pydantic",
        [
            '"unknown_account"',
            '"unknown_sku"',
            '"mixed_currencies"',
            '"subtotal": "600.00"',
        ],
    ),
]


def require(module: str):
    """Import a consumer, or skip; with PYXSD_REQUIRE_EXAMPLES a miss fails."""
    if os.environ.get("PYXSD_REQUIRE_EXAMPLES"):
        return importlib.import_module(module)
    return pytest.importorskip(module)


def _argv(args: list[str], tmp: Path) -> list[str]:
    """Translate documented relative paths into absolute subprocess argv."""
    translated = []
    for arg in args:
        if arg == PARQUET:
            translated.append(str(tmp / PARQUET))
        elif arg.endswith((".py", ".xml")):
            translated.append(str(REPO / arg))
        else:
            translated.append(arg)
    return [sys.executable, *translated]


def _ensure_parquet(script: str, tmp: Path) -> Path:
    target = tmp / PARQUET
    if not target.exists():
        result = subprocess.run(
            [sys.executable, str(REPO / script), "--write-parquet", str(target)],
            cwd=tmp,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
    return target


def test_all_documented_commands_appear_in_the_guide():
    text = DOC.read_text(encoding="utf-8")
    missing = [command for command, *_ in COMMANDS if command not in text]
    assert missing == []


@pytest.mark.parametrize(
    ("documented", "args", "module", "fragments"),
    COMMANDS,
    ids=[command for command, *_ in COMMANDS],
)
def test_documented_command_runs(documented, args, module, fragments, tmp_path):
    require(module)
    if "--parquet" in args:
        _ensure_parquet(args[0], tmp_path)
    result = subprocess.run(_argv(args, tmp_path), cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    output = " ".join((result.stdout + result.stderr).split())
    for fragment in fragments:
        assert " ".join(fragment.split()) in output, fragment
