"""Fetches the large-schema benchmark corpus on demand.

The corpus is downloaded into a gitignored cache (``tests/benchmarks/
.cache/``) and never committed: rerunning this module is cheap because
already-extracted corpora are skipped, and ``--force`` re-downloads.

Usage::

    python -m tests.benchmarks.fetch [name] [--force]
"""

from __future__ import annotations

import os
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# Schema corpora for the compile-time benchmarks. ``zip`` entries name a
# remote zip archive; the archive is extracted under
# ``<cache>/<name>/`` and ``entry`` is the benchmark's entry-point
# schema, relative to that directory. ``local`` entries are committed
# example schemas and resolve against the repository root.
SCHEMAS: dict[str, dict] = {
    "fhir-single": {
        "url": "https://www.hl7.org/fhir/fhir-all-xsd.zip",
        "url_sha256": None,
        "kind": "zip",
        "entry": "fhir-single.xsd",
    },
    "cda-r2": {
        "url": "https://github.com/HL7/CDA-core-2.0/archive/refs/heads/master.zip",
        "url_sha256": None,
        "kind": "zip",
        # The zip's root folder is CDA-core-2.0-master/; extraction
        # preserves the tree so CDA.xsd's relative includes resolve.
        "entry": "CDA-core-2.0-master/schema/normative/infrastructure/cda/CDA.xsd",
    },
    "ooxml-wml": {
        "url": None,
        "url_sha256": None,
        "kind": "local",
        "entry": "examples/docx/schemas/wml.xsd",
    },
    "ooxml-sml": {
        "url": None,
        "url_sha256": None,
        "kind": "local",
        "entry": "examples/docx/schemas/sml.xsd",
    },
    "musicxml": {
        "url": None,
        "url_sha256": None,
        "kind": "local",
        "entry": "examples/musicxml/schemas/musicxml.xsd",
    },
}

REPO_ROOT = Path(__file__).resolve().parents[2]


def cache_dir() -> Path:
    """The gitignored download cache: ``tests/benchmarks/.cache``."""
    return Path(__file__).resolve().parent / ".cache"


def entry_point(name: str) -> Path:
    """Absolute path to benchmark *name*'s entry-point schema."""
    entry = SCHEMAS[name]
    if entry["kind"] == "zip":
        return cache_dir() / name / entry["entry"]
    return REPO_ROOT / entry["entry"]


def _download(url: str, dest: Path) -> None:
    """Downloads *url* to *dest* atomically (temp file, then rename)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=dest.parent, prefix=dest.name, suffix=".tmp", delete=False
    ) as tmp:
        try:
            with urllib.request.urlopen(url) as response:
                while True:
                    chunk = response.read(1 << 16)
                    if not chunk:
                        break
                    tmp.write(chunk)
            os.replace(tmp.name, dest)
        except BaseException:
            tmp.close()
            Path(tmp.name).unlink(missing_ok=True)
            raise


def _extract(archive: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zipped:
        zipped.extractall(target)


def fetch(name: str | None = None, force: bool = False) -> list[Path]:
    """Ensures one (default: every non-``local``) benchmark is cached.

    Returns the entry-point paths of everything ensured. An already
    extracted corpus is skipped unless *force* re-downloads it.
    """
    names = [name] if name is not None else list(SCHEMAS)
    if name is not None and name not in SCHEMAS:
        raise KeyError(name)
    ensured: list[Path] = []
    for bench in names:
        entry = SCHEMAS[bench]
        target = entry_point(bench)
        if entry["kind"] == "local":
            if not target.is_file():
                raise FileNotFoundError(f"local benchmark schema missing: {target}")
            ensured.append(target)
            continue
        if target.is_file() and not force:
            print(f"{bench}: cached ({target})")
            ensured.append(target)
            continue
        archive = cache_dir() / f"{bench}.zip"
        print(f"{bench}: downloading {entry['url']} ...")
        _download(entry["url"], archive)
        _extract(archive, cache_dir() / bench)
        if not target.is_file():
            raise FileNotFoundError(
                f"downloaded archive for {bench} did not contain {entry['entry']}"
            )
        ensured.append(target)
        print(f"{bench}: extracted {target}")
    return ensured


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in args
    args = [arg for arg in args if not arg.startswith("--")]
    name = args[0] if args else None
    fetch(name, force=force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
