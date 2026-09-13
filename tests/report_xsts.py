#!/usr/bin/env python3
"""Local runner for the W3C XML Schema Test Suite.

Examples::

    python tests/report_xsts.py --profile xsd11 --limit 500
    python tests/report_xsts.py --profile xsd11 --baseline tests/xsts/baseline-xsd11.toml
    python tests/report_xsts.py --profile xsd11 --write-baseline tests/xsts/baseline-xsd11.toml

This is intentionally not wired into CI: the suite is slow and PyXSD does not
yet pass it.  Run it locally when changing schema handling, and regenerate the
baseline only after reviewing the differences.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xsts import baseline, report
from xsts.drivers import PyXSDDriver, XmlSchemaDriver
from xsts.runner import (
    Runner,
    build_cases,
    configure_logging,
    corpus_available,
    load_default_catalog,
    run_parallel,
)
from xsts.selection import PROFILES


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="xsd11")
    parser.add_argument("--limit", type=int, default=None, help="run at most N cases")
    parser.add_argument(
        "--filter",
        default=None,
        help="only cases whose id contains this substring",
    )
    parser.add_argument(
        "--engine",
        choices=("pyxsd", "xmlschema", "both"),
        default="both",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="per-case seconds")
    parser.add_argument(
        "--jobs",
        type=int,
        default=os.cpu_count() or 1,
        help="worker processes (default: one per CPU)",
    )
    parser.add_argument("--baseline", type=Path, default=None, help="compare against a baseline")
    parser.add_argument(
        "--write-baseline",
        type=Path,
        default=None,
        help="write the observed outcomes to this baseline file",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--max-failures", type=int, default=20)
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="print how many cases would run, without running them",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not corpus_available():
        print(
            "the xsdtests corpus is not checked out; run "
            "`git submodule update --init tests/xsts/corpus`",
            file=sys.stderr,
        )
        return 2

    profile = PROFILES[args.profile]
    catalog = load_default_catalog()
    cases = build_cases(catalog, profile)
    if args.filter:
        cases = [case for case in cases if args.filter in case.test_id]
    if args.limit is not None:
        cases = cases[: args.limit]

    if args.list_only:
        print(f"{len(cases)} cases would run under profile {profile.name}")
        return 0

    configure_logging()
    oracle_enabled = args.engine in ("xmlschema", "both")

    with tempfile.TemporaryDirectory(prefix="pyxsd-xsts-") as workdir:
        if args.jobs > 1:

            def progress(done: int, total: int) -> None:
                if done % 500 == 0 or done == total:
                    print(f"  ...{done}/{total} cases", file=sys.stderr)

            results = run_parallel(
                cases,
                profile.name,
                oracle_enabled=oracle_enabled,
                timeout=args.timeout,
                jobs=args.jobs,
                temp_root=Path(workdir),
                on_progress=progress,
            )
        else:
            driver = PyXSDDriver(timeout=args.timeout)
            oracle = XmlSchemaDriver(profile.name, timeout=args.timeout) if oracle_enabled else None
            runner = Runner(profile=profile, driver=driver, oracle=oracle, workdir=Path(workdir))
            results = []
            for index, case in enumerate(cases, start=1):
                results.extend(runner.run_case(case))
                if index % 200 == 0:
                    print(f"  ...{index}/{len(cases)} cases", file=sys.stderr)

    if args.engine == "pyxsd":
        results = [r for r in results if r.engine == "pyxsd"]
    elif args.engine == "xmlschema":
        results = [r for r in results if r.engine == "xmlschema"]

    diff = None
    if args.baseline is not None:
        diff = baseline.diff(baseline.load(args.baseline), report.results_to_keys(results))
    summary = report.summarize(results, diff=diff)

    if args.write_baseline is not None:
        meta = {
            "profile": profile.name,
            "case_count": len(cases),
        }
        baseline.save(args.write_baseline, report.results_to_keys(results), meta)
        print(
            f"wrote baseline with {len(results)} entries to {args.write_baseline}", file=sys.stderr
        )

    if args.json:
        print(report.render_json(summary))
    else:
        print(report.render_text(summary, max_failures=args.max_failures))

    if diff is not None and diff.changes:
        print(f"{len(diff.changes)} baseline changes", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
