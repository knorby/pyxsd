"""Compile-once benchmark tripwire for the large-schema corpus.

Timings are advisory. The only timing assertion anywhere is the
generous :data:`CEILING_SECONDS` ceiling: it trips on a pathological
regression (an accidental quadratic compile), never on ordinary machine
speed. Machine-independent outcomes are pinned in
``test_conformance_errors.py`` and ``test_large_schemas.py``.

Downloaded corpora (``fhir-single``, ``cda-r2``) skip when absent — run
``python -m tests.conformance.fetch`` to populate the cache.
"""

import pytest
from tests.conformance.fetch import SCHEMAS, entry_point
from tests.conformance.run import EXPECTED, run_benchmark

CEILING_SECONDS = 30


def _available(name: str) -> bool:
    try:
        return entry_point(name).is_file()
    except Exception:
        return False


@pytest.mark.parametrize("name", list(SCHEMAS))
def test_benchmark_compiles_under_the_tripwire_ceiling(name):
    if not _available(name):
        pytest.skip(
            f"benchmark schema '{name}' not downloaded; run python -m tests.conformance.fetch"
        )
    row = run_benchmark(name, repeats=1)
    assert row["best_seconds"] < CEILING_SECONDS, (
        f"{name} compile exceeded the {CEILING_SECONDS}s tripwire: {row['best_seconds']:.3f}s"
    )
    if EXPECTED[name]["classes"] is None:  # pragma: no cover - placeholder
        pytest.skip(f"benchmark schema '{name}' outcome not yet pinned")
    assert row["classes"] == EXPECTED[name]["classes"], "class count drifted"
