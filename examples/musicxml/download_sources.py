"""Fetch and verify the pinned musicxml sources into .cache/ (idempotent)."""

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.musicxml.common.sources import fetch_sources, manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        action="append",
        metavar="SECTION",
        choices=sorted(manifest()),
        help="install a single pinned section (repeatable)",
    )
    parser.add_argument("--force", action="store_true", help="replace a corrupt or stale cache")
    args = parser.parse_args(argv)
    for name, path in fetch_sources(force=args.force, only=args.only):
        print(f"verified: {name} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
