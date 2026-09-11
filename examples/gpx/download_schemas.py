"""Download the GPX 1.1 XSD used by the gpx example.

GPX has a single self-contained schema, but it is not committed to this
repository so the example data stays independent of the specification's
distribution terms. Run this script once to fetch a copy into
``examples/gpx/schemas/``; the example and its tests use it from there
and are skipped when it is absent.

    uv run python examples/gpx/download_schemas.py

The file comes from TopoGrafix, the maintainer of the GPX format.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://www.topografix.com/GPX/1/1"

#: The GPX 1.1 schema. It imports nothing, so this is the whole set.
SCHEMA_FILES = ("gpx.xsd",)

DEFAULT_DEST = Path(__file__).resolve().parent / "schemas"


def download(dest: Path, *, force: bool = False, quiet: bool = False) -> int:
    """Fetch every schema into *dest*.

    Returns the number of files that failed. Existing files are kept
    unless *force* is set, so re-running is cheap.
    """
    dest.mkdir(parents=True, exist_ok=True)
    failures = 0
    for name in SCHEMA_FILES:
        target = dest / name
        if target.exists() and not force:
            if not quiet:
                print(f"keep    {name}")
            continue
        url = f"{BASE_URL}/{name}"
        request = urllib.request.Request(url, headers={"User-Agent": "pyxsd-example"})
        try:
            with urllib.request.urlopen(request) as response:
                data = response.read()
        except (urllib.error.URLError, OSError) as exc:
            failures += 1
            print(f"FAILED  {name}: {exc}", file=sys.stderr)
            continue
        target.write_bytes(data)
        if not quiet:
            print(f"fetched {name} ({len(data)} bytes)")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest",
        type=Path,
        default=DEFAULT_DEST,
        help=f"destination directory (default: {DEFAULT_DEST})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-download files that already exist",
    )
    parser.add_argument("--quiet", action="store_true", help="only report failures")
    args = parser.parse_args(argv)

    failures = download(args.dest, force=args.force, quiet=args.quiet)
    if failures:
        print(f"{failures} file(s) failed to download", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"schemas in {args.dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
