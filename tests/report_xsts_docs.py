#!/usr/bin/env python3
"""Generate the checked-in XSTS documentation fragments.

Examples::

    python tests/report_xsts_docs.py
    python tests/report_xsts_docs.py --check

Default mode writes ``docs/generated/xsts-results.md`` and
``docs/generated/xsts-headline.md``.  ``--check`` regenerates them in memory
and reports missing or changed files without writing; CI uses it to fail a
stale documentation build.  The generator reads only the checked-in
baselines, the pinned main ``suite.xml`` catalogue, and
``tests/xsts/dispositions.toml``.  It never runs an engine and never reads
the clock.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xsts.catalog import CatalogError
from xsts.dispositions import DispositionError
from xsts.documentation import PublicationError, build_fragments

#: Repository root; tests may point ``main`` at a miniature fixture instead.
REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report missing or changed fragments without writing them",
    )
    return parser.parse_args(argv)


def _check(fragments: Sequence[tuple[Path, str]], repo_root: Path) -> list[str]:
    """Missing and changed publication targets, in publication order."""
    problems: list[str] = []
    for path, content in fragments:
        relative = path.relative_to(repo_root)
        if not path.is_file():
            problems.append(f"missing: {relative.as_posix()}")
        elif path.read_text(encoding="utf-8") != content:
            problems.append(f"changed: {relative.as_posix()}")
    return problems


def run(argv: Sequence[str] | None = None, *, repo_root: Path = REPO_ROOT) -> int:
    """Write or check both fragments; return the process exit code."""
    args = parse_args(argv)
    try:
        fragments = build_fragments(repo_root)
    except (CatalogError, DispositionError, PublicationError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.check:
        problems = _check(fragments, repo_root)
        if problems:
            for problem in problems:
                print(problem, file=sys.stderr)
            return 1
        print("generated XSTS documentation is up to date", file=sys.stderr)
        return 0

    for path, content in fragments:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"wrote {path.relative_to(repo_root).as_posix()}", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None, *, repo_root: Path | None = None) -> int:
    """CLI entry point; ``repo_root`` exists so tests can use a fixture."""
    return run(argv, repo_root=REPO_ROOT if repo_root is None else repo_root)


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
