"""Pinned, verified downloads for one self-contained example family.

Adapted from ``examples/healthcare/common/sources.py`` with two additions
such families need: individual-file sections (some XSD closures are only
published as loose files) and install-time localization of absolute
``schemaLocation`` URLs (official schemas sometimes import companions by
absolute URL; pyxsd's default resource policy correctly refuses remote
loading, so the URLs are rewritten to the local mirror once, after digest
verification).

Every section of ``sources.toml`` is either:

``kind = "archive"``
    One zip, pinned by its SHA-256, extracted into ``destination/``.

``kind = "files"``
    Loose files, each pinned by SHA-256, placed at ``path`` under
    ``destination/`` (paths mirror the origin URL layout so the
    localization step can rewrite absolute imports mechanically).

Two sections may share one ``destination``; each section verifies only
its own files, recorded in ``.<section>.verified.json``.
Files modified by localization keep their pristine bytes in
``.pristine/`` so re-verification stays exact.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tempfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

FAMILY_ROOT = Path(__file__).resolve().parents[1]
FAMILY = FAMILY_ROOT.name
CACHE_ROOT = FAMILY_ROOT / ".cache"

#: Mirrors the healthcare budget: archives cap at 80 MB on the wire and
#: 300 MB expanded, so a compromised upstream cannot fill the disk.
MAX_ARCHIVE_BYTES = 80_000_000
MAX_EXPANDED_BYTES = 300_000_000

#: URL prefixes whose referenced files live in the shared mirror at the
#: same relative path (an absolute URL under a prefix maps to the path
#: after it, under the destination).
MIRROR_PREFIXES = (
    "http://schemas.opengis.net/",
    "http://www.w3.org/",
    "http://docs.oasis-open.org/",
)

SETUP_HINT = (
    f"uv run python examples/{FAMILY}/download_sources.py (see examples/{FAMILY}/README.md)"
)


class SourceVerificationError(ValueError):
    """A downloaded or cached source does not match its manifest."""


def manifest() -> dict:
    return tomllib.loads((FAMILY_ROOT / "sources.toml").read_text(encoding="utf-8"))


def section(name: str) -> dict:
    try:
        return manifest()[name]
    except KeyError:
        raise KeyError(f"unknown {FAMILY} source section: {name!r}") from None


def _marker_path(cache_root: Path, spec: dict, name: str) -> Path:
    return cache_root / spec["destination"] / f".{name}.verified.json"


def cache_path(name: str) -> Path:
    """Path of a section's entry file, whether or not it exists yet."""
    spec = section(name)
    return CACHE_ROOT / spec["destination"] / spec["entry"]


def require_cache(name: str) -> Path:
    """Entry-file path for an installed section, with the setup hint."""
    path = cache_path(name)
    if not path.is_file():
        raise FileNotFoundError(f"missing {FAMILY} source {name!r}: {path}; run: {SETUP_HINT}")
    return path


def resolve_sample(name: str, relative: str) -> Path:
    """Resolve ``<section>:<relative path>`` inside an installed section."""
    base = CACHE_ROOT / section(name)["destination"]
    candidate = (base / relative).resolve()
    if PurePosixPath(relative).is_absolute() or not candidate.is_relative_to(base):
        raise ValueError("sample path escapes section cache")
    if not candidate.is_file():
        raise FileNotFoundError(f"missing {FAMILY} sample {name}:{relative}; run: {SETUP_HINT}")
    return candidate


def _verified_bytes(cache_root: Path, spec: dict, name: str, recorded: dict[str, str]) -> bool:
    """Hash pristine copies where they exist, installed files otherwise."""
    target = cache_root / spec["destination"]
    for relative, digest in recorded.items():
        pristine = target / ".pristine" / relative
        source = pristine if pristine.is_file() else target / relative
        try:
            actual = hashlib.sha256(source.read_bytes()).hexdigest()
        except OSError:
            return False
        if actual != digest:
            return False
    return True


def _download(url: str) -> bytes:
    if not url.startswith(("http://", "https://")):
        raise SourceVerificationError(f"unsupported source URL: {url}")
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise SourceVerificationError("download too large")
    return data


def _safe_member(relative: str) -> PurePosixPath:
    member = PurePosixPath(relative)
    if member.is_absolute() or ".." in member.parts or "\\" in relative:
        raise SourceVerificationError(f"unsafe archive member: {relative!r}")
    return member


def _extract(spec: dict) -> dict[str, bytes]:
    """Return {member path: bytes} for an archive section, fully checked."""
    data = _download(spec["url"])
    if hashlib.sha256(data).hexdigest() != spec["sha256"]:
        raise SourceVerificationError(f"{spec['url']}: download digest mismatch")
    members: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if sum(info.file_size for info in archive.infolist()) > MAX_EXPANDED_BYTES:
            raise SourceVerificationError("expanded archive too large")
        for info in archive.infolist():
            if info.is_dir():
                continue
            _safe_member(info.filename)
            members[info.filename] = archive.read(info)
    if spec["entry"] not in members:
        raise SourceVerificationError(f"archive entry missing: {spec['entry']}")
    return members


def _fetch_files(spec: dict) -> dict[str, bytes]:
    """Return {file path: bytes} for a files section, digests verified."""
    fetched: dict[str, bytes] = {}
    for file_spec in spec.get("files", []):
        _safe_member(file_spec["path"])
        data = _download(file_spec["url"])
        digest = hashlib.sha256(data).hexdigest()
        if digest != file_spec["sha256"]:
            raise SourceVerificationError(f"{file_spec['url']}: download digest mismatch")
        fetched[file_spec["path"]] = data
    if spec["entry"] not in fetched:
        raise SourceVerificationError(f"section entry missing from files: {spec['entry']}")
    return fetched


def localize(target: Path, extra_map: dict[str, str] | None = None) -> list[Path]:
    """Rewrite absolute ``schemaLocation`` URLs to mirror-relative paths.

    ``extra_map`` maps absolute URLs to sibling-relative locations (a
    manifest ``rewrite`` table can point an import at the file sitting
    next to the entry schema). Only references whose
    target exists in the tree are rewritten; pristine bytes of every
    modified file are preserved under ``.pristine/`` so verification can
    re-hash the exact content that was pinned. Returns the changed files.
    """
    extra_map = extra_map or {}
    changed: list[Path] = []
    for path in sorted(target.rglob("*.xsd")):
        if ".pristine" in path.relative_to(target).parts:
            continue
        text = original = path.read_text(encoding="utf-8")
        for url in sorted(set(_absolute_schema_locations(text))):
            if url in extra_map:
                mapped = (path.parent / extra_map[url]).resolve()
            else:
                mapped = None
                for prefix in MIRROR_PREFIXES:
                    if url.startswith(prefix):
                        mirror_rel = url[len(prefix) :]
                        mapped = (target / mirror_rel).resolve()
                        break
            if mapped is None or not mapped.is_file():
                continue  # leave genuinely remote references untouched
            relative = Path(*[".."] * len(path.parent.relative_to(target).parts))
            relative = (relative / mapped.relative_to(target)).as_posix()
            text = text.replace(f'schemaLocation="{url}"', f'schemaLocation="{relative}"')
        if text != original:
            pristine = target / ".pristine" / path.relative_to(target)
            pristine.parent.mkdir(parents=True, exist_ok=True)
            if not pristine.exists():
                pristine.write_bytes(original.encode("utf-8"))
            path.write_text(text, encoding="utf-8")
            changed.append(path)
    return changed


def _absolute_schema_locations(text: str):
    for match in re.finditer(r'schemaLocation="(http[^"]+)"', text):
        yield match.group(1)


def install(source: str | dict, *, cache_root: Path | None = None, force: bool = False) -> Path:
    """Install one manifest section (by name or as a spec dict).

    A valid cache verifies without network access. Tests pass synthetic
    spec dicts together with ``cache_root``; production callers pass the
    section name and use the family's default ``.cache``.
    """
    cache_root = cache_root if cache_root is not None else CACHE_ROOT
    spec = section(source) if isinstance(source, str) else source
    name = spec.get("name", source if isinstance(source, str) else "<synthetic>")
    target = cache_root / spec["destination"]
    marker = _marker_path(cache_root, spec, name)
    if marker.is_file() and not force:
        try:
            recorded = json.loads(marker.read_text(encoding="utf-8"))["files"]
        except (OSError, ValueError, KeyError):
            recorded = None
        if recorded is not None and _verified_bytes(cache_root, spec, name, recorded):
            return target
        raise SourceVerificationError(f"corrupt cache: {target}; retry with --force")
    content = _extract(spec) if spec["kind"] == "archive" else _fetch_files(spec)
    with tempfile.TemporaryDirectory(dir=cache_root if cache_root.exists() else None) as temporary:
        staging = Path(temporary)
        for relative, data in sorted(content.items()):
            member = staging / relative
            member.parent.mkdir(parents=True, exist_ok=True)
            member.write_bytes(data)
        target.mkdir(parents=True, exist_ok=True)
        for relative in sorted(content):
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(staging / relative, destination)
        digests = {
            relative: hashlib.sha256((staging / relative).read_bytes()).hexdigest()
            for relative in sorted(content)
        }
        marker.write_text(
            json.dumps({"section": name, "files": digests}, sort_keys=True), encoding="utf-8"
        )
    if spec.get("localize") or spec.get("rewrite"):
        # Localization happens on the installed tree, not the staging
        # copy: shared destinations mean a section's absolute imports can
        # point at files another section installed. Pristine bytes land
        # in .pristine/ so marker verification stays exact.
        localize(target, spec.get("rewrite"))
    return target


def fetch_sources(*, force: bool = False, only: list[str] | None = None) -> list[tuple[str, Path]]:
    names = only if only else list(manifest())
    unknown = [n for n in names if n not in manifest()]
    if unknown:
        raise KeyError(f"unknown {FAMILY} source sections: {unknown}")
    return [(name, install(name, force=force)) for name in names]
