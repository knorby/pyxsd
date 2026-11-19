#!/usr/bin/env python3
"""Run and check the per-profile XSLT conformance baselines.

Examples::

    python tests/report_xslt.py --profile xslt30 --check
    python tests/report_xslt.py --profile xslt30 --enforce
    python tests/report_xslt.py --profile xslt30 --write-baseline
    python tests/report_xslt.py --profile xslt30 --census census-xslt30.json
    python tests/report_xslt.py --profile xslt30 --census slice.json --filter number/
    python tests/report_xslt.py --profile xslt30 --census fast.json --skip-timeouts

``--check`` regenerates the deterministic report from the checked-in baseline
and the pinned catalog without executing a stylesheet.  It exits nonzero when
the baseline records another profile, corpus commit, or per-case timeout
budget, omits an applicable case, or adds one the catalog no longer declares,
and it refuses any verdict string outside the outcome vocabulary.  The budget is
part of a baseline's identity: verdicts recorded under one budget are not
comparable with a run under another.

``--enforce`` executes the selected profile and compares every case outcome
with its baseline.  A semantic change -- a regression, an improvement, an added
case, or a removed case -- exits 2; move only the entries an intentional
behavior change actually flips in that change's commit.  A baseline recorded
under another profile, corpus, or per-case budget is refused before any verdict
is compared.  ``timeout`` is the per-case harness budget, not a verdict: only
the ``fail``↔``timeout`` pair is reported as harness drift, because
machine-speed flakiness alone can expire a budget before the same failure
(performance work belongs to Stage 9/Task 40).  Every other non-pass move is
strict, including a timeout into another verdict, and a passing case becoming a
timeout is still a regression.  ``--write-baseline`` rewrites a whole baseline
and refuses a filtered, limited, or skip-timeouts slice.

``--census PATH`` executes the selected profile exactly as ``--enforce`` does
and writes one deterministic JSON document of per-case outcomes, normalized
failure signatures, and signature groups.  It never compares or writes a
baseline, and it cannot be combined with ``--check``, ``--enforce``, or
``--write-baseline``.

``--filter TOKEN[,TOKEN]`` narrows ``--census``, ``--enforce``, or
``--list-only`` to a deterministic slice.  A token ending in ``/`` selects a
family by name prefix (``number/``); any other token must equal a full case id
(``number/number-2701``).  Tokens are stripped, de-duplicated, and unioned, and
a token that matches no case is an error.  The matcher is verbatim and
case-sensitive: no substring, glob, or regex form survives.  A partial census
records what it measured in a ``selection`` block.  ``--check`` validates the
whole baseline and ``--write-baseline`` writes the whole profile, so both refuse
to be narrowed.

``--skip-timeouts`` (census only) drops exactly the rows the matching baseline
records as ``timeout`` -- never a family list and never a gap or failure row --
after checking that the baseline records this run's profile, corpus pin, and
budget.  The census names those rows in a ``skipped`` section that carries no
outcome, signature, message, or total, so a row the run did not execute cannot
be read as a verdict.  ``--enforce`` and ``--check`` must measure every recorded
timeout row, ``--write-baseline`` records a complete profile, and
``--list-only`` runs nothing, so all four refuse the flag.

The milestone and baseline-moving instruments cannot be narrowed at all:
milestone boundaries and every baseline-moving commit run the full, unfiltered
census and --check on all three profiles (xslt30, xslt20, xslt10); never
--filter and never --skip-timeouts.

``--include-gated`` measures, in addition, the cases the profile's selection
excludes because they declare a ``unicode-version`` dependency the engine's
tables do not satisfy.  It requires ``--census``: those outcomes are recorded in
a labelled ``gated`` section, excluded from the profile totals, and can never
become or be compared with a baseline.  The gated set is computed from the
catalog with the profile's version claim cleared, never from an outcome.

The report contains no timestamps, absolute paths, or wall-clock values.  It is
a function of the checked-in baseline, the pinned catalog, and the profile
identity.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Collection, Sequence
from pathlib import Path

#: Repository root; tests point ``repo_root`` at a synthetic fixture instead.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tests.xslt import baseline, census, report  # noqa: E402
from tests.xslt.catalog import CatalogError, load_catalog  # noqa: E402
from tests.xslt.model import TestCase  # noqa: E402
from tests.xslt.outcomes import CaseResult, Outcome  # noqa: E402
from tests.xslt.selection import select_cases  # noqa: E402
from tests.xslt.suite import SUITE_PROFILES, run_parallel, with_unicode_version  # noqa: E402

#: The pinned corpus, relative to the repository root.
CORPUS_PATH = Path("tests/xslt/corpus")

#: Where the per-profile baselines live.
BASELINE_DIR = Path("tests/xslt/baselines")

#: Default per-case budget in seconds.  The full regex and Unicode families
#: need a real budget on a developer machine and more on a CI runner.
DEFAULT_TIMEOUT = 5.0

#: Development-loop honesty rules.  The first is mechanical: the milestone
#: instruments (--check, --write-baseline, a bare run) refuse a filter and
#: --enforce refuses --skip-timeouts, so a narrowed run cannot stand in for the
#: gate.  The second is discipline this module cannot enforce; the mechanical
#: half is that a skip set is always visible in the census that used it.
HONESTY_RULES = (
    "milestone boundaries and every baseline-moving commit run the full, "
    "unfiltered census and --check on all three profiles (xslt30, xslt20, "
    "xslt10); never --filter and never --skip-timeouts",
    "any diff touching src/, tests/xslt/suite.py, or tests/xslt/environment.py "
    "runs every affected family without --skip-timeouts, because such a change "
    "can move any case's verdict or its cost",
)


class SelectionError(ValueError):
    """A selection the requested mode cannot honour; run() turns it into exit 2."""


def matches_token(name: str, token: str) -> bool:
    """Family prefix for a token ending in "/", exact id otherwise."""
    return name.startswith(token) if token.endswith("/") else name == token


def parse_filter_tokens(value: str | None) -> tuple[str, ...]:
    """Canonical tokens: stripped, de-duplicated, order preserved; empty rejected."""
    if value is None:
        return ()
    tokens: list[str] = []
    for raw in value.split(","):
        token = raw.strip()
        if not token:
            raise SelectionError(
                "--filter token '' is empty; a token is a family prefix ending in '/' "
                "or an exact case id"
            )
        if token not in tokens:
            tokens.append(token)
    return tuple(tokens)


def narrowing_refusal(args: argparse.Namespace) -> str | None:
    """The first reason this mode cannot be narrowed, or ``None``.

    Every sentence is argument-level: it names the mode and why narrowing it
    would make the instrument lie, and none of them needs the catalog.
    """
    filter_refused = args.census is None and not args.enforce and not args.list_only
    if args.filter is not None and filter_refused:
        if args.check:
            return "--check validates the whole baseline and cannot be narrowed by --filter"
        if args.write_baseline is not None:
            return (
                "--write-baseline requires a complete run; --filter would record "
                "a slice as the profile"
            )
        return (
            "--filter requires --census, --enforce, or --list-only; a plain run "
            "has no slice to compare or report"
        )
    if args.limit is not None:
        if args.write_baseline is not None:
            return (
                "--write-baseline requires a complete run; --limit would record "
                "a slice as the profile"
            )
        if args.check:
            return "--check validates the whole baseline and cannot be narrowed by --limit"
    if args.skip_timeouts and args.census is None:
        if args.check:
            reason = "--check must measure every row"
        elif args.enforce:
            reason = "--enforce must measure every recorded timeout row"
        elif args.write_baseline is not None:
            reason = "--write-baseline records a complete profile"
        elif args.list_only:
            reason = "--list-only runs nothing"
        else:
            reason = "a plain run has no census to report it"
        return f"--skip-timeouts is only valid with --census; {reason}"
    if args.limit is not None and args.limit < 1:
        return f"--limit must be at least 1, got {args.limit}"
    return None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=sorted(SUITE_PROFILES), default="xslt30")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "run at most N cases (N >= 1); a limited --enforce compares only its slice; "
            "refused with --check and --write-baseline"
        ),
    )
    parser.add_argument(
        "--filter",
        default=None,
        metavar="TOKEN[,TOKEN]",
        help=(
            "census, enforce, or list-only: select cases by token; a token ending in "
            "'/' is a family prefix, any other token is an exact case id; refused with "
            "--check and --write-baseline"
        ),
    )
    parser.add_argument(
        "--skip-timeouts",
        action="store_true",
        help=(
            "census only: skip the cases the matching baseline records as timeout "
            "(profile, corpus, and budget must match); refused with --check, "
            "--enforce, --write-baseline, and --list-only"
        ),
    )
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
    parser.add_argument(
        "--include-gated",
        action="store_true",
        help=(
            "with --census, also measure the cases excluded by a declared "
            "unicode-version dependency, in their own section outside the totals"
        ),
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


def progress_reporter(noun: str) -> Callable[[int, int], None]:
    """A progress callback that reports every 250 cases and the last one."""

    def report(done: int, total: int) -> None:
        if done % 250 == 0 or done == total:
            print(f"  ...{done}/{total} {noun}", file=sys.stderr)

    return report


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


def budget_value(timeout: float) -> int | float:
    """One budget as a baseline records it: whole seconds stay integers."""
    return int(timeout) if timeout.is_integer() else timeout


def restrict(recorded: baseline.Baseline, names: Collection[str]) -> baseline.Baseline:
    """The same baseline with only *names*; out-of-slice rows cannot appear."""
    wanted = set(names)
    return baseline.Baseline(
        meta=recorded.meta,
        cases={name: outcome for name, outcome in recorded.cases.items() if name in wanted},
    )


def baseline_identity_problem(
    recorded: baseline.Baseline,
    *,
    profile: str,
    corpus: str,
    timeout: int | float,
) -> str | None:
    """The first identity dimension this baseline disagrees on, or ``None``.

    A slice or skip set is compared with exactly one profile's baseline: the
    verdict vocabulary, the corpus, and the per-case budget are all part of what
    a row means, so a mismatch is a refusal rather than drift.  The budget
    sentence is the one ``--enforce`` has always used.
    """
    recorded_profile = recorded.meta.get("profile")
    if recorded_profile != profile:
        return (
            f"baseline profile {recorded_profile!r} is not this run's {profile!r}; "
            "a slice or skip set is never comparable across profiles"
        )
    recorded_corpus = recorded.meta.get("corpus")
    if recorded_corpus != corpus:
        return (
            f"baseline corpus {recorded_corpus!r} is not pinned {corpus!r}; "
            "a slice or skip set is never comparable across corpora"
        )
    recorded_budget = recorded.meta.get("timeout_seconds")
    if recorded_budget != timeout:
        return (
            f"baseline timeout budget {recorded_budget!r} is not this run's {timeout!r}s; "
            "verdicts recorded under one budget are not comparable, so regenerate the "
            "baseline deliberately with --write-baseline"
        )
    return None


def run_corpus(args: argparse.Namespace, repo_root: Path) -> int:
    profile = SUITE_PROFILES[args.profile]
    try:
        catalog = catalog_for(repo_root)
    except (CatalogError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    cases = select_cases(catalog, profile.feature_profile)
    available = len(cases)
    gated_cases: tuple[TestCase, ...] = ()
    if args.include_gated:
        # Applicability, not verdicts: the catalog's declarations under a
        # profile that makes no Unicode-version claim, minus what this profile
        # already selects.
        selected = {case.name for case in cases}
        cleared = with_unicode_version(profile, "").feature_profile
        gated_cases = tuple(
            case for case in select_cases(catalog, cleared) if case.name not in selected
        )
    tokens = parse_filter_tokens(args.filter)
    if tokens:
        # Zero-match is decided over the full candidate set the run would
        # consider, before skipping or limiting: a gated-only family needs
        # --include-gated, and a token is never rescued by a narrow slice.
        universe = [case.name for case in cases]
        universe.extend(case.name for case in gated_cases)
        for token in tokens:
            if not any(matches_token(name, token) for name in universe):
                raise SelectionError(
                    f"--filter token {token!r} matches no case in profile {args.profile}"
                )
        cases = tuple(
            case for case in cases if any(matches_token(case.name, token) for token in tokens)
        )
        gated_cases = tuple(
            case for case in gated_cases if any(matches_token(case.name, token) for token in tokens)
        )
    matched = len(cases)

    timeout = case_timeout(args)
    budget = budget_value(timeout)
    corpus = pinned_corpus(repo_root) or catalog.corpus_revision
    recorded: baseline.Baseline | None = None
    if args.enforce or args.skip_timeouts:
        # Identity before execution: a baseline of another profile, corpus, or
        # budget does not describe this run's rows, and a skip set derived from
        # it would hide the wrong cases.
        target = baseline_path(args, repo_root)
        try:
            recorded = baseline.load_baseline(target)
        except (ValueError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        problem = baseline_identity_problem(
            recorded, profile=args.profile, corpus=corpus, timeout=budget
        )
        if problem is not None:
            print(f"error: {problem}", file=sys.stderr)
            return 2

    skipped_names: tuple[str, ...] = ()
    if args.skip_timeouts and recorded is not None:
        # Exactly the baseline's recorded timeout rows, intersected with the
        # cases this run matched: a timeout outside the slice was never in
        # scope and is not "skipped".
        recorded_timeouts = {
            name for name, outcome in recorded.cases.items() if outcome == Outcome.TIMEOUT
        }
        skipped_names = tuple(sorted({case.name for case in cases} & recorded_timeouts))
        cases = tuple(case for case in cases if case.name not in recorded_timeouts)
        print(
            f"skipping {len(skipped_names)} recorded timeout cases from the "
            f"{args.profile} baseline",
            file=sys.stderr,
        )

    if args.limit is not None:
        cases = cases[: args.limit]
        gated_cases = gated_cases[: args.limit]
    partial = bool(tokens) or args.limit is not None or args.skip_timeouts

    if args.list_only:
        print(f"{len(cases)} cases would run under profile {args.profile}")
        return 0

    selection = None
    if partial:
        selection = census.Selection(
            filters=tokens,
            limit=args.limit,
            skip_timeouts=args.skip_timeouts,
            available=available,
            matched=matched,
            selected=len(cases),
        )
    skipped = None
    if args.skip_timeouts:
        skipped = census.Skipped(
            source={
                "profile": args.profile,
                "corpus": corpus,
                "timeout_seconds": budget,
            },
            names=skipped_names,
        )
    if args.enforce and partial and recorded is not None:
        print(f"enforcing {len(cases)} of {len(recorded.cases)} cases", file=sys.stderr)

    results = run_parallel(
        cases,
        profile,
        timeout=timeout,
        jobs=args.jobs,
        on_progress=progress_reporter("cases"),
    )
    gated_results: tuple[CaseResult, ...] = ()
    if args.include_gated:
        gated_results = run_parallel(
            gated_cases,
            with_unicode_version(profile, ""),
            timeout=timeout,
            jobs=args.jobs,
            on_progress=progress_reporter("gated cases"),
        )
    if args.census is not None:
        observed = census.build_census(
            results,
            profile=args.profile,
            corpus=corpus,
            timeout_seconds=timeout,
            root=repo_root,
            gated=gated_results,
            skipped=skipped,
            selection=selection,
        )
        census.write_census(args.census, observed)
        print(
            f"wrote {len(results) + len(gated_results)} census records to {args.census}",
            file=sys.stderr,
        )
        return 0
    current = baseline.build_baseline(
        results,
        corpus=catalog.corpus_revision,
        profile=args.profile,
    )
    current = baseline.Baseline(
        meta={**current.meta, "timeout_seconds": budget},
        cases=current.cases,
    )
    generated = report_from_baseline(args.profile, current)

    if args.write_baseline is not None:
        baseline.write_baseline(args.write_baseline, current)
        print(
            f"wrote {len(current.cases)} outcomes to {args.write_baseline}",
            file=sys.stderr,
        )
    elif args.enforce and recorded is not None:
        names = {case.name for case in cases}
        # Only a partial run is sliced.  Then an out-of-slice row cannot appear
        # as removed (it was never enforced) and a slice member absent from the
        # baseline still appears as added, because restrict keeps the run's
        # names on both sides.
        previous = restrict(recorded, names) if partial else recorded
        observed_baseline = restrict(current, names) if partial else current
        changes, drift = baseline.gate_changes(previous, observed_baseline)
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
    if args.include_gated and args.census is None:
        print("error: --include-gated requires --census", file=sys.stderr)
        return 2
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
    if args.check and (args.write_baseline is not None or args.enforce):
        print(
            "error: --check cannot be combined with --enforce or --write-baseline",
            file=sys.stderr,
        )
        return 2
    if args.write_baseline is not None and args.enforce:
        print("error: --enforce and --write-baseline are mutually exclusive", file=sys.stderr)
        return 2
    refusal = narrowing_refusal(args)
    if refusal is not None:
        print(f"error: {refusal}", file=sys.stderr)
        return 2
    if args.check:
        return check(
            args.profile,
            repo_root,
            timeout=case_timeout(args),
            path=baseline_path(args, repo_root),
            json_output=args.json,
        )
    try:
        return run_corpus(args, repo_root)
    except SelectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None, *, repo_root: Path | None = None) -> int:
    return run(argv, repo_root=REPO_ROOT if repo_root is None else repo_root)


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
