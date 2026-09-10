"""Loader and executor for the pyxsd conformance corpus.

Shared by ``tests/test_conformance.py`` (pytest) and
``tests/report_conformance.py`` (standalone report).

Each case is materialized into a scratch directory as ``schema.xsd``,
``instance.xml`` (when present), and any auxiliary files, then run
through the real PyXSD pipeline. The module-level element
representative registry is cleared before every case so runs are
independent, matching the behavior of the pytest fixture.
"""

from __future__ import annotations

import io
import tomllib
from pathlib import Path
from typing import Any

from pyxsd.element_representatives.element_representative import registry
from pyxsd.parser import PyXSD

HERE = Path(__file__).parent
CASES_PATH = HERE / "conformance" / "cases.toml"
UNSUPPORTED_PATH = HERE / "conformance" / "unsupported.toml"


def load_cases(path: Path | None = None) -> list[dict[str, Any]]:
    """Load every conformance case from the corpus manifest."""
    with open(path or CASES_PATH, "rb") as handle:
        data = tomllib.load(handle)
    return data["case"]


def load_unsupported(path: Path | None = None) -> list[dict[str, Any]]:
    """Load the explicit unsupported-features table."""
    with open(path or UNSUPPORTED_PATH, "rb") as handle:
        data = tomllib.load(handle)
    return data["feature"]


def _materialize(case: dict[str, Any], directory: Path) -> Path:
    """Write the case files into *directory* and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "schema.xsd").write_text(case["schema"], encoding="utf-8")
    for name, content in case.get("files", {}).items():
        (directory / name).write_text(content, encoding="utf-8")
    if "instance" in case:
        (directory / "instance.xml").write_text(case["instance"], encoding="utf-8")
    return directory


def run_case(case: dict[str, Any], directory: Path) -> tuple[bool, str]:
    """Run one conformance case.

    Returns ``(passed, detail)``. *detail* explains the first failed
    expectation and is empty on success.

    ``PyXSD.__init__`` runs the whole pipeline eagerly, so a single
    construction executes the schema step and (when present) the
    instance step. Schema-only cases feed an in-memory dummy instance;
    the extra ``unknown-root`` report entry it produces is harmless
    because expectations match by subset of report codes.
    """
    directory = _materialize(case, directory)
    has_instance = "instance" in case
    registry.clear()

    if has_instance:
        instance_input: str | io.StringIO = str(directory / "instance.xml")
    else:
        instance_input = io.StringIO("<x/>")

    try:
        parser = PyXSD(
            instance_input,
            str(directory / "schema.xsd"),
            xmlFileOutput=False,
            transformOutputName=None,
        )
    except Exception as exc:
        return False, f"parse raised {type(exc).__name__}: {exc}"

    report = parser.report
    if not case.get("schema_valid", True):
        if not report.has_errors:
            return False, f"schema errors expected, report was clean: {report}"
        return _check_codes(case, report, "schema")

    if not has_instance:
        return True, ""

    if case.get("instance_valid", True):
        if report.issues:
            return False, f"clean parse expected, got report: {report}"
        return True, ""
    if not report.has_errors:
        return False, f"instance errors expected, report was clean: {report}"
    return _check_codes(case, report, "instance")


def _check_codes(case: dict[str, Any], report: Any, step: str) -> tuple[bool, str]:
    """Verify all expected_codes appear in the report of *step*."""
    expected = case.get("expected_codes") or []
    codes = {issue.code for issue in report.issues}
    missing = [code for code in expected if code not in codes]
    if missing:
        actual = sorted(codes)
        return False, f"{step} missing expected codes {missing}; got {actual}"
    return True, ""
