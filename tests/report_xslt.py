#!/usr/bin/env python3
"""Run and check the per-profile XSLT conformance baselines.

Examples::

    python tests/report_xslt.py --profile xslt30 --check
    python tests/report_xslt.py --profile xslt30 --enforce
    python tests/report_xslt.py --profile xslt30 --write-baseline
    python tests/report_xslt.py --profile xslt30 --census census-xslt30.json

``--check`` regenerates the deterministic report from the checked-in baseline
and the pinned catalog without executing a stylesheet.  It exits nonzero when
the baseline records another profile, corpus commit, or per-case timeout
budget, omits an applicable case, or adds one the catalog no longer declares.
The budget is part of a baseline's identity: verdicts recorded under one
budget are not comparable with a run under another.

``--enforce`` executes the selected profile and compares every case outcome
with its baseline.  A semantic change -- a regression, an improvement, an added
case, or a removed case -- exits 2; move only the entries an intentional
behavior change actually flips in that change's commit.  A baseline recorded
under another per-case budget is refused for the same reason ``--check``
refuses it, before any verdict is compared.  ``timeout`` is the per-case
harness budget, not a verdict: only the ``fail``↔``timeout`` pair is reported
as harness drift, because machine-speed flakiness alone can expire a budget
before the same failure (performance work belongs to Stage 9/Task 40).
Every other non-pass move is strict, including a timeout into another verdict,
and a passing case becoming a timeout is still a regression.
``--write-baseline`` rewrites a whole baseline and is refused for a filtered or
limited slice.

``--census PATH`` executes the selected profile exactly as ``--enforce`` does
and writes one deterministic JSON document of per-case outcomes, normalized
failure signatures, and signature groups.  It never compares or writes a
baseline, and it cannot be combined with ``--check``, ``--enforce``, or
``--write-baseline``.

The report contains no timestamps, absolute paths, or wall-clock values.  It is
a function of the checked-in baseline, the pinned catalog, and the profile
identity.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

#: Repository root; tests point ``repo_root`` at a synthetic fixture instead.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tests.xslt import baseline, census, report  # noqa: E402
from tests.xslt.catalog import CatalogError, load_catalog  # noqa: E402
from tests.xslt.selection import select_cases  # noqa: E402
from tests.xslt.suite import SUITE_PROFILES, run_parallel  # noqa: E402

#: The pinned corpus, relative to the repository root.
CORPUS_PATH = Path("tests/xslt/corpus")

#: Where the per-profile baselines live.
BASELINE_DIR = Path("tests/xslt/baselines")

#: Default per-case budget in seconds.  The full regex and Unicode families
#: need a real budget on a developer machine and more on a CI runner.
DEFAULT_TIMEOUT = 5.0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=sorted(SUITE_PROFILES), default="xslt30")
    parser.add_argument("--limit", type=int, default=None, help="run at most N cases")
    parser.add_argument("--filter", default=None, help="only cases whose name contains this")
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=(
            "per-case seconds (default: PYXSD_XSLT_TIMEOUT when set, otherwise "
            f"{DEFAULT_TIMEOUT:g})"
        ),
    )
    parser.add_argument("--jobs", type=int, default=None, help="worker processes")
    parser.add_argument("--baseline", type=Path, default=None, help="compare against this file")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the checked-in baseline and regenerated report without running",
    )
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="exit 2 on any change from the baseline",
    )
    parser.add_argument(
        "--write-baseline",
        type=Path,
        default=None,
        help="write the observed outcomes to this baseline file",
    )
    parser.add_argument(
        "--census",
        type=Path,
        default=None,
        metavar="PATH",
        help="execute the profile and write a deterministic census to this JSON file",
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="print how many cases would run, without running them",
    )
    return parser.parse_args(argv)


def default_baseline(profile: str, repo_root: Path) -> Path:
    return repo_root / BASELINE_DIR / f"{profile}.toml"


def baseline_path(args: argparse.Namespace, repo_root: Path) -> Path:
    return args.baseline if args.baseline is not None else default_baseline(args.profile, repo_root)


def pinned_corpus(repo_root: Path) -> str:
    """The corpus commit recorded by the superproject, or ``""`` outside git."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", f"HEAD:{CORPUS_PATH.as_posix()}"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return completed.stdout.strip()


def catalog_for(repo_root: Path):
    return load_catalog(repo_root / CORPUS_PATH / "catalog.xml")


def case_timeout(args: argparse.Namespace) -> float:
    if args.timeout is not None:
        return args.timeout
    configured = os.environ.get("PYXSD_XSLT_TIMEOUT")
    return float(configured) if configured else DEFAULT_TIMEOUT


def report_from_baseline(profile: str, recorded: baseline.Baseline) -> report.Report:
    return report.outcomes_report(
        recorded.cases,
        corpus=recorded.meta["corpus"],
        profile=profile,
    )


def check(
    profile: str,
    repo_root: Path,
    *,
    timeout: float,
    path: Path | None = None,
    json_output: bool = False,
) -> int:
    """Validate one checked-in baseline against the pinned catalog.

    *timeout* is the effective per-case budget for this run.  A baseline
    recorded under another budget is drift: the same cases time out
    differently, so its verdicts do not describe this run.
    """
    path = default_baseline(profile, repo_root) if path is None else path
    try:
        recorded = baseline.load_baseline(path)
        catalog = catalog_for(repo_root)
    except (CatalogError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    problems: list[str] = []
    if recorded.meta.get("schema") != baseline.BASELINE_SCHEMA:
        problems.append(f"baseline schema {recorded.meta.get('schema')!r} is not current")
    if recorded.meta.get("profile") != profile:
        problems.append(f"baseline profile {recorded.meta.get('profile')!r} is not {profile!r}")
    expected_corpus = pinned_corpus(repo_root) or catalog.corpus_revision
    if recorded.meta.get("corpus") != expected_corpus:
        problems.append(
            f"baseline corpus {recorded.meta.get('corpus')!r} is not pinned {expected_corpus!r}"
        )
    recorded_budget = recorded.meta.get("timeout_seconds")
    if recorded_budget != timeout:
        problems.append(
            f"baseline timeout budget {recorded_budget!r} is not this run's {timeout:g}s; "
            "regenerate the baseline deliberately with --write-baseline"
        )

    declared = {
        case.name for case in select_cases(catalog, SUITE_PROFILES[profile].feature_profile)
    }
    recorded_cases = set(recorded.cases)
    missing = sorted(declared - recorded_cases)
    extra = sorted(recorded_cases - declared)
    if missing:
        problems.append(f"{len(missing)} applicable cases are missing, first {missing[0]!r}")
    if extra:
        problems.append(f"{len(extra)} recorded cases are not applicable, first {extra[0]!r}")

    generated = ""
    if "corpus" in recorded.meta:
        try:
            generated = report.render_json(report_from_baseline(profile, recorded))
            regenerated = report.render_json(report_from_baseline(profile, recorded))
        except ValueError as exc:
            problems.append(f"baseline outcomes cannot form a report: {exc}")
        else:
            if generated != regenerated:
                problems.append("report generation is not deterministic")
    else:
        problems.append("baseline records no corpus identity")

    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    if json_output:
        sys.stdout.write(generated)
    try:
        label = path.relative_to(repo_root).as_posix()
    except ValueError:
        label = str(path)
    print(
        f"{profile}: {len(recorded_cases)} cases reproducible from {label}",
        file=sys.stderr,
    )
    return 0


def run_corpus(args: argparse.Namespace, repo_root: Path) -> int:
    profile = SUITE_PROFILES[args.profile]
    try:
        catalog = catalog_for(repo_root)
    except (CatalogError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    cases = select_cases(catalog, profile.feature_profile)
    if args.filter:
        cases = tuple(case for case in cases if args.filter in case.name)
    if args.limit is not None:
        cases = cases[: args.limit]
    complete = not args.filter and args.limit is None

    if args.list_only:
        print(f"{len(cases)} cases would run under profile {args.profile}")
        return 0

    def progress(done: int, total: int) -> None:
        if done % 250 == 0 or done == total:
            print(f"  ...{done}/{total} cases", file=sys.stderr)

    timeout = case_timeout(args)
    results = run_parallel(
        cases,
        profile,
        timeout=timeout,
        jobs=args.jobs,
        on_progress=progress,
    )
    if args.census is not None:
        observed = census.build_census(
            results,
            profile=args.profile,
            corpus=pinned_corpus(repo_root) or catalog.corpus_revision,
            timeout_seconds=timeout,
            root=repo_root,
        )
        census.write_census(args.census, observed)
        print(
            f"wrote {len(results)} census records to {args.census}",
            file=sys.stderr,
        )
        return 0
    current = baseline.build_baseline(
        results,
        corpus=catalog.corpus_revision,
        profile=args.profile,
    )
    current = baseline.Baseline(
        meta={
            **current.meta,
            "timeout_seconds": int(timeout) if timeout.is_integer() else timeout,
        },
        cases=current.cases,
    )
    generated = report_from_baseline(args.profile, current)

    if args.write_baseline is not None:
        if not complete:
            print(
                "error: --write-baseline requires a complete run without filters", file=sys.stderr
            )
            return 2
        baseline.write_baseline(args.write_baseline, current)
        print(
            f"wrote {len(current.cases)} outcomes to {args.write_baseline}",
            file=sys.stderr,
        )
    elif args.enforce:
        target = baseline_path(args, repo_root)
        try:
            recorded = baseline.load_baseline(target)
        except (ValueError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        recorded_budget = recorded.meta.get("timeout_seconds")
        current_budget = current.meta.get("timeout_seconds")
        if recorded_budget != current_budget:
            print(
                f"error: baseline timeout budget {recorded_budget!r} is not this run's "
                f"{current_budget!r}s; verdicts recorded under one budget are not comparable, "
                "so regenerate the baseline deliberately with --write-baseline",
                file=sys.stderr,
            )
            return 2
        changes, drift = baseline.gate_changes(recorded, current)
        for change in drift:
            print(
                f"[harness] {change.key}: {change.previous} -> {change.current}",
                file=sys.stderr,
            )
        if changes:
            for change in changes[:50]:
                print(
                    f"[{change.kind}] {change.key}: {change.previous} -> {change.current}",
                    file=sys.stderr,
                )
            print(f"{len(changes)} changes from the baseline", file=sys.stderr)
            return 2

    if args.json:
        sys.stdout.write(report.render_json(generated))
    return 0


def run(argv: Sequence[str] | None = None, *, repo_root: Path = REPO_ROOT) -> int:
    """Run or check one profile; return the process exit code."""
    args = parse_args(argv)
    if args.census is not None:
        conflicts = [
            flag
            for flag, chosen in (
                ("--check", args.check),
                ("--enforce", args.enforce),
                ("--write-baseline", args.write_baseline is not None),
            )
            if chosen
        ]
        if conflicts:
            print(
                f"error: --census cannot be combined with {', '.join(conflicts)}",
                file=sys.stderr,
            )
            return 2
    if args.check:
        if args.write_baseline is not None or args.enforce:
            print(
                "error: --check cannot be combined with --enforce or --write-baseline",
                file=sys.stderr,
            )
            return 2
        return check(
            args.profile,
            repo_root,
            timeout=case_timeout(args),
            path=baseline_path(args, repo_root),
            json_output=args.json,
        )
    if args.write_baseline is not None and args.enforce:
        print("error: --enforce and --write-baseline are mutually exclusive", file=sys.stderr)
        return 2
    return run_corpus(args, repo_root)


def main(argv: Sequence[str] | None = None, *, repo_root: Path | None = None) -> int:
    return run(argv, repo_root=REPO_ROOT if repo_root is None else repo_root)


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
