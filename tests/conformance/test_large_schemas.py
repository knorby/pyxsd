"""Compile smoke tests for the large-schema conformance corpus.

Each schema must compile with a pinned outcome: an exact class count and
an exact report-error total, derived from the per-error-code tallies in
:data:`tests.conformance.run.EXPECTED`. Those numbers are machine
independent, so they double as the correctness gate for compile-time
optimizations: a faster compile that changes any validation outcome fails
here. Per-code pins live in ``test_conformance_errors.py``.

Downloaded corpora (``fhir-single``, ``cda-r2``) skip when absent — run
``python -m tests.conformance.fetch`` to populate the cache.
"""

import pytest
from tests.conformance.fetch import SCHEMAS, entry_point
from tests.conformance.run import EXPECTED

from pyxsd import Schema


def _available(name: str) -> bool:
    try:
        return entry_point(name).is_file()
    except Exception:
        return False


@pytest.mark.parametrize("name", list(SCHEMAS))
def test_large_schema_compiles_with_pinned_outcome(name):
    if not _available(name):
        pytest.skip(
            f"conformance schema '{name}' not downloaded; run python -m tests.conformance.fetch"
        )
    schema = Schema.compile(str(entry_point(name)))
    expected = EXPECTED[name]
    if expected["classes"] is None:  # pragma: no cover - placeholder
        pytest.skip(f"conformance schema '{name}' outcome not yet pinned")
    assert len(schema.classes) == expected["classes"], "class count drifted"
    expected_errors = sum(expected["errors_by_code"].values())
    assert len(schema.report.errors) == expected_errors, "error count drifted"
