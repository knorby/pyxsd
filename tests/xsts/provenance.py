"""Provenance recorded alongside XSTS runs.

:func:`collect_provenance` gathers the facts a report consumer needs to judge
and reproduce a run: what was run (profile, suite file, case count), how it
was selected (engines, per-case timeout, job count, the exact command),
against what (source and corpus revisions, interpreter, platform, engine
versions), and when (a single UTC completion timestamp).

The result feeds both the JSON report and baseline ``[meta]`` tables, so every
value must render through ``json.dumps`` onto a valid TOML right-hand side:
scalars and lists of scalars only, never nested tables.  That is why
``engine_versions`` is a list of ``name==version`` strings and ``platform`` is
a single summary string.

Environment access stays behind small helpers (``_git_revision``,
``_distribution_version``, ``_current_time``, ...) so tests can pin each fact
without depending on the machine.  A supplied ``now`` is used verbatim; the
clock helper is consulted only when ``now`` is omitted, and the timestamp
always comes from run metadata -- generated output is never stamped at
generation time.
"""

from __future__ import annotations

import importlib.metadata
import platform
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

#: Repository root, so git queries work from any working directory.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Path of the pinned corpus submodule, relative to the repository root.
CORPUS_PATH = "tests/xsts/corpus"

#: Distributions recorded on every run; they are hard dependencies.
BASE_DISTRIBUTIONS = ("pyxsd", "elementpath")

#: The optional oracle distribution, recorded only when selected and installed.
OPTIONAL_DISTRIBUTION = "xmlschema"


def _git_revision(ref: str) -> str:
    """Resolve *ref* to a commit SHA in this repository.

    Git failures degrade to ``"unknown"``: a finished run must not be lost
    because the tree was exported without ``.git`` or git is absent.
    """
    try:
        completed = subprocess.run(
            ["git", "rev-parse", ref],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return completed.stdout.strip()


def _current_time() -> datetime:
    """The only place a run timestamp may read the clock."""
    return datetime.now(UTC)


def _distribution_version(name: str) -> str | None:
    """The installed version of *name*, or ``None`` when it is not installed."""
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _python_version() -> str:
    return platform.python_version()


def _platform_summary() -> str:
    return platform.platform()


def _iso_utc(moment: datetime) -> str:
    """*moment* as ISO-8601 UTC; a naive datetime is taken to be UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def collect_provenance(
    *,
    profile: str,
    suite_file: str,
    case_count: int,
    complete: bool,
    engines: Sequence[str],
    timeout: float,
    jobs: int,
    command: str,
    now: datetime | None = None,
) -> dict[str, object]:
    """Assemble the run metadata shared by JSON reports and baselines."""
    engine_versions = [f"{name}=={_distribution_version(name)}" for name in BASE_DISTRIBUTIONS]
    if OPTIONAL_DISTRIBUTION in engines:
        version = _distribution_version(OPTIONAL_DISTRIBUTION)
        if version is not None:
            engine_versions.append(f"{OPTIONAL_DISTRIBUTION}=={version}")
    moment = now if now is not None else _current_time()
    return {
        "profile": profile,
        "suite_file": suite_file,
        "case_count": case_count,
        "complete": complete,
        "engines": list(engines),
        "timeout_seconds": timeout,
        "jobs": jobs,
        "command": command,
        "source_revision": _git_revision("HEAD"),
        "corpus_revision": _git_revision(f"HEAD:{CORPUS_PATH}"),
        "python_version": _python_version(),
        "platform": _platform_summary(),
        "engine_versions": sorted(engine_versions),
        "run_completed_at": _iso_utc(moment),
    }
