"""Standalone conformance pass-rate report.

Usage::

    uv run python tests/report_conformance.py [--json]

Runs every corpus case through the real PyXSD pipeline and prints a
pass-rate table by feature area, followed by the explicit
unsupported-features table. Exits non-zero when any case fails.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from conformance_runner import load_cases, load_unsupported, run_case


def main(argv: list[str] | None = None) -> int:
    """Run the corpus and print the report. Returns an exit status."""
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument(
        "--json", action="store_true", help="emit JSON instead of a text table"
    )
    arguments = argument_parser.parse_args(argv)

    cases = load_cases()
    results_by_area: dict[str, list[tuple[str, bool, str]]] = defaultdict(list)
    with tempfile.TemporaryDirectory(prefix="pyxsd-conformance-") as scratch:
        for case in cases:
            area, _, name = case["id"].partition("/")
            passed, detail = run_case(case, Path(scratch) / name)
            results_by_area[area].append((name, passed, detail))

    rows = []
    for area, results in results_by_area.items():
        total = len(results)
        passed = sum(1 for _, ok, _ in results if ok)
        rows.append(
            {
                "area": area,
                "total": total,
                "passed": passed,
                "failed": total - passed,
                "rate": round(100.0 * passed / total, 1) if total else 0.0,
            }
        )
    grand_total = sum(row["total"] for row in rows)
    grand_passed = sum(row["passed"] for row in rows)
    overall = {
        "area": "overall",
        "total": grand_total,
        "passed": grand_passed,
        "failed": grand_total - grand_passed,
        "rate": round(100.0 * grand_passed / grand_total, 1) if grand_total else 0.0,
    }
    rows.append(overall)

    if arguments.json:
        print(
            json.dumps(
                {
                    "areas": rows[:-1],
                    "overall": overall,
                    "unsupported": load_unsupported(),
                    "failures": [
                        {"area": area, "name": name, "detail": detail}
                        for area, results in results_by_area.items()
                        for name, ok, detail in results
                        if not ok
                    ],
                },
                indent=2,
            )
        )
    else:
        _print_table(rows)
        _print_unsupported()
        failures = [
            (area, name, detail)
            for area, results in results_by_area.items()
            for name, ok, detail in results
            if not ok
        ]
        if failures:
            print(f"\n{len(failures)} failure(s):")
            for area, name, detail in failures:
                print(f"  {area}/{name}: {detail}")
        else:
            print("\nAll conformance cases passed.")

    return 1 if overall["failed"] else 0


def _print_table(rows: list[dict[str, object]]) -> None:
    """Print the pass-rate table to stdout."""
    header = f"{'area':<14}{'total':>6}{'passed':>8}{'failed':>8}{'rate':>9}"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['area']:<14}{row['total']:>6}{row['passed']:>8}"
            f"{row['failed']:>8}{row['rate']:>8}%"
        )


def _print_unsupported() -> None:
    """Print the unsupported-features table to stdout."""
    features = load_unsupported()
    print("\nUnsupported / partial features (explicit):")
    print("-" * 44)
    for feature in features:
        print(f"  [{feature['status']:<11}] {feature['name']}")
        print(f"    construct: {feature['construct']}")
        wrapped = _wrap(feature["note"], indent=6)
        print(f"    note: {wrapped}")


def _wrap(text: str, indent: int, width: int = 78) -> str:
    """Hard-wrap *text* to *width*, indenting continuation lines."""
    words, lines, current = text.split(), [], ""
    prefix = " " * indent
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(current) + len(word) + 1 > width - indent and current:
            lines.append(current)
            current = prefix + word
        else:
            current = candidate
    if current:
        lines.append(current)
    return ("\n" + prefix).join(lines)


if __name__ == "__main__":
    sys.exit(main())
