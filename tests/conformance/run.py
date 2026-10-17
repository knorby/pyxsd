"""Times schema compilation across the large-schema conformance corpus.

Usage::

    python -m tests.conformance.run [--repeats N] [--json] [--save-baseline]
                                    [--compare]

Wall-clock numbers are advisory: pytest never asserts on them (see
``test_large_schemas.py`` and ``test_conformance_errors.py`` for the
pinned outcomes). ``--save-baseline`` records this machine's timings to
``baseline.json``; ``--compare`` prints the per-schema ratio against
that baseline.
"""

from __future__ import annotations

import json
import logging
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from tests.conformance.fetch import SCHEMAS, entry_point

if TYPE_CHECKING:
    from pyxsd.validation import ValidationReport

# Machine-independent compile outcomes, pinned by the conformance and
# smoke tests. Each schema carries its full per-error-code tally, so a
# parse-correctness fix moves exactly its own lines. A ``None``
# ``classes`` value means "measured but not yet pinned" and the smoke
# test skips for that schema.
EXPECTED: dict[str, dict] = {
    "fhir-single": {
        "classes": 2941,
        "errors_by_code": {"unknown-attributeRef": 53, "unknown-elementRef": 1},
    },
    # CDA R2 normative: its datatypes.xsd circular include is skipped
    # (compose-cycle), cascading into unresolved HL7 datatypes — a known,
    # deterministic pre-existing outcome, pinned as-is.
    "cda-r2": {
        "classes": 987,
        "errors_by_code": {"unknown-type": 452, "declaration-attribute": 46},
    },
    "ooxml-wml": {
        "classes": 2359,
        "errors_by_code": {
            "unknown-elementRef": 57,
            "unknown-attributeRef": 26,
            "declaration-attribute": 26,
            "unknown-group": 6,
            "circular-derivation": 4,
            "unknown-type": 4,
            "upa": 2,
            "all-rule": 1,
        },
    },
    "ooxml-sml": {
        "classes": 2289,
        "errors_by_code": {
            "unknown-attributeRef": 37,
            "unknown-elementRef": 4,
            "unknown-group": 1,
            "declaration-attribute": 1,
        },
    },
    "musicxml": {
        "classes": 385,
        "errors_by_code": {"unknown-attributeRef": 27},
    },
}


def errors_by_code(name: str, report: ValidationReport) -> dict[str, int]:
    """Tally *report*'s errors by code for schema *name*.

    The manifest pins the exact tally per code, so a fix that resolves
    one error class shows up as exactly its own lines moving — not a
    total that hides which class changed.
    """
    return dict(Counter(error.code for error in report.errors))


BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"


def run_benchmark(name: str, repeats: int = 3) -> dict:
    """Compiles benchmark *name* *repeats* times; returns timing + outcome."""
    from pyxsd import Schema

    logging.disable(logging.CRITICAL)
    try:
        timings = []
        schema = None
        for _ in range(max(1, repeats)):
            start = time.perf_counter()
            schema = Schema.compile(str(entry_point(name)))
            timings.append(time.perf_counter() - start)
        assert schema is not None
        return {
            "name": name,
            "best_seconds": min(timings),
            "mean_seconds": statistics.mean(timings),
            "classes": len(schema.classes),
            "errors": len(schema.report.errors),
        }
    finally:
        logging.disable(logging.NOTSET)


def _load_baseline() -> dict:
    if BASELINE_PATH.is_file():
        return json.loads(BASELINE_PATH.read_text())
    return {}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    repeats = 3
    if "--repeats" in args:
        repeats = int(args[args.index("--repeats") + 1])
    as_json = "--json" in args
    save = "--save-baseline" in args
    compare = "--compare" in args

    results = []
    for name in SCHEMAS:
        if not entry_point(name).is_file():
            print(f"{name}: skipped (not downloaded)", file=sys.stderr)
            continue
        results.append(run_benchmark(name, repeats=repeats))

    if as_json:
        print(json.dumps(results, indent=2))
    else:
        header = f"{'schema':<14}{'best':>9}{'mean':>9}{'classes':>9}{'errors':>8}"
        print(header)
        for row in results:
            print(
                f"{row['name']:<14}{row['best_seconds']:>8.3f}s"
                f"{row['mean_seconds']:>8.3f}s{row['classes']:>9}"
                f"{row['errors']:>8}"
            )

    if save:
        BASELINE_PATH.write_text(json.dumps(results, indent=2) + "\n")
        print(f"baseline saved: {BASELINE_PATH}")

    if compare:
        baseline = _load_baseline()
        if not baseline:
            print("no baseline to compare against", file=sys.stderr)
        else:
            old = {row["name"]: row for row in baseline}
            print(f"\n{'schema':<14}{'baseline':>10}{'now':>9}{'ratio':>8}")
            for row in results:
                was = old.get(row["name"], {}).get("best_seconds")
                if was is None:
                    continue
                ratio = row["best_seconds"] / was
                print(f"{row['name']:<14}{was:>9.3f}s{row['best_seconds']:>8.3f}s{ratio:>8.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
