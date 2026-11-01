#!/usr/bin/env python3
"""Local runner for the W3C XML Schema Test Suite.

Examples::

    python tests/report_xsts.py --profile xsd11 --limit 500
    python tests/report_xsts.py --profile xsd11 --baseline tests/xsts/baseline-xsd11.toml
    python tests/report_xsts.py --profile xsd11 --write-baseline tests/xsts/baseline-xsd11.toml

This local runner is not the required CI gate itself (CI runs the pytest
suite in ``tests/test_xsts_suite.py``); use it to explore slices, compare
both engines, regenerate baselines, and drive the dispatch-only
``.github/workflows/xsts.yml`` workflow, which covers the XSD 1.0 profile.
Against the checked-in baselines pyxsd passes 99.78% of the XSD 1.1 profile
and 99.61% of XSD 1.0.  Run it locally when changing schema handling.
``--write-baseline`` rewrites the whole file: never regenerate wholesale to
make a gate green -- move only the entries an intentional behavior change
actually flips, in that change's commit.

Every run also records provenance -- profile, engines, timeout, jobs, the
exact command, source and corpus revisions, versions, and a UTC completion
timestamp -- in ``--json`` output (``provenance``) and in baselines written
by ``--write-baseline`` (``[meta]``).  ``complete = false`` marks a
``--filter``/``--limit`` slice; the metadata, not a refusal, identifies it.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xsts import baseline, report
from xsts.drivers import PyXSDDriver, XmlSchemaDriver
from xsts.provenance import collect_provenance
from xsts.runner import (
    Runner,
    build_cases,
    configure_logging,
    corpus_available,
    load_default_catalog,
    run_parallel,
)
from xsts.selection import PROFILES


def reproducible_command(argv: Sequence[str]) -> str:
    """The canonical command line for a run, rebuilt from the raw arguments."""
    return shlex.join(["python", "tests/report_xsts.py", *argv])


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
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="per-case seconds (default 60; use 180 on slower or loaded machines)",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=os.cpu_count() or 1,
        help="worker processes (default: one per CPU)",
    )
    parser.add_argument("--baseline", type=Path, default=None, help="compare against a baseline")
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="exit 2 on a baseline regression or a newly observed failure",
    )
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
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    args = parse_args(raw_argv)
    if args.enforce and args.baseline is None:
        print("--enforce requires --baseline", file=sys.stderr)
        return 2
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
    complete = args.filter is None and args.limit is None

    if args.list_only:
        print(f"{len(cases)} cases would run under profile {profile.name}")
        return 0

    configure_logging()
    oracle_enabled = args.engine in ("xmlschema", "both")
    selected_engines = {
        "pyxsd": ["pyxsd"],
        "xmlschema": ["xmlschema"],
        "both": ["pyxsd", "xmlschema"],
    }[args.engine]

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
                synthesize_missing_schema=True,
            )
        else:
            driver = PyXSDDriver(
                timeout=args.timeout,
                synthesize_missing_schema=True,
                xsd_version=profile.xsd_version,
            )
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

    provenance = collect_provenance(
        profile=profile.name,
        suite_file="suite.xml",
        case_count=len(cases),
        complete=complete,
        engines=selected_engines,
        timeout=args.timeout,
        jobs=args.jobs,
        command=reproducible_command(raw_argv),
    )

    if args.write_baseline is not None:
        baseline.save(args.write_baseline, report.results_to_keys(results), provenance)
        print(
            f"wrote baseline with {len(results)} entries to {args.write_baseline}", file=sys.stderr
        )

    if args.json:
        print(report.render_json(summary, provenance=provenance))
    else:
        print(report.render_text(summary, max_failures=args.max_failures))

    if args.enforce:
        assert diff is not None  # checked before the run started
        violations = baseline.enforce_violations(diff)
        if violations:
            print(f"{len(violations)} enforcement violations", file=sys.stderr)
            for change in violations[: args.max_failures]:
                print(
                    f"  {change.kind}: {change.key} ({change.previous} -> {change.current})",
                    file=sys.stderr,
                )
            return 2
        return 0

    if diff is not None and diff.changes:
        print(f"{len(diff.changes)} baseline changes", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
