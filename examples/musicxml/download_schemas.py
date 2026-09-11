"""Download the MusicXML 4.0 XSD set used by the musicxml example.

The MusicXML schema is large (``musicxml.xsd`` alone is ~360 KB), so it is
not committed to this repository. Run this script once to fetch a copy into
``examples/musicxml/schemas/``; the example and its tests use it from there
and are skipped when it is absent.

    uv run python examples/musicxml/download_schemas.py

The schemas come from the W3C Music Notation Community Group's MusicXML 4.0
release archive. ``musicxml.xsd`` imports the XML and XLink namespaces by
absolute URL; those imports are rewritten to the local ``xml.xsd`` and
``xlink.xsd`` files so the schema resolves without network access.
"""

from __future__ import annotations

import argparse
import io
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ZIP_URL = "https://github.com/w3c-cg/musicxml/releases/download/v4.0/musicxml-4.0.zip"

#: The files extracted from the archive into ``schemas/``. The XML and XLink
#: schemas are pulled alongside ``musicxml.xsd`` because it imports them.
SCHEMA_FILES = ("musicxml.xsd", "xml.xsd", "xlink.xsd")

#: Remote ``schemaLocation`` values in ``musicxml.xsd`` and the local file
#: each should be rewritten to.
REWRITTEN_IMPORTS = {
    "http://www.musicxml.org/xsd/xml.xsd": "xml.xsd",
    "http://www.musicxml.org/xsd/xlink.xsd": "xlink.xsd",
}

DEFAULT_DEST = Path(__file__).resolve().parent / "schemas"


def download(dest: Path, *, force: bool = False, quiet: bool = False) -> int:
    """Fetch and extract the schema set into *dest*.

    Returns the number of files that failed. Existing files are kept unless
    *force* is set, so re-running is cheap.
    """
    dest.mkdir(parents=True, exist_ok=True)
    if all((dest / name).exists() for name in SCHEMA_FILES) and not force:
        if not quiet:
            print(f"keep    {', '.join(SCHEMA_FILES)}")
        return 0

    request = urllib.request.Request(ZIP_URL, headers={"User-Agent": "pyxsd-example"})
    try:
        with urllib.request.urlopen(request) as response:
            archive = zipfile.ZipFile(io.BytesIO(response.read()))
    except (urllib.error.URLError, OSError, zipfile.BadZipFile) as exc:
        print(f"FAILED  {ZIP_URL}: {exc}", file=sys.stderr)
        return len(SCHEMA_FILES)

    failures = 0
    for name in SCHEMA_FILES:
        target = dest / name
        if target.exists() and not force:
            if not quiet:
                print(f"keep    {name}")
            continue
        member = f"schema/{name}"
        try:
            text = archive.read(member).decode("utf-8")
        except KeyError:
            failures += 1
            print(f"FAILED  {member}: not in archive", file=sys.stderr)
            continue
        for remote, local in REWRITTEN_IMPORTS.items():
            text = text.replace(remote, local)
        target.write_text(text, encoding="utf-8")
        if not quiet:
            print(f"fetched {name} ({len(text)} chars)")
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
        help="re-extract files that already exist",
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
