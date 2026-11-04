"""Pinned downloads. Verify archives and every cached file before reuse."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tempfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

HEALTHCARE_ROOT = Path(__file__).resolve().parents[1]


class SourceVerificationError(ValueError):
    """A downloaded or cached source does not match its manifest."""


def manifest() -> dict:
    return tomllib.loads((HEALTHCARE_ROOT / "sources.toml").read_text(encoding="utf-8"))


def document_path(name: str) -> Path:
    spec = manifest()["documents"][name]
    return resolve_document(f"{spec['repository']}:{spec['path']}")


def corpus_root(name: str) -> Path:
    """Return a fetched repository root, not the archive's outer cache directory."""
    spec = manifest()[name]
    entry = HEALTHCARE_ROOT / ".cache" / spec["destination"] / spec["entry"]
    if not entry.is_file():
        raise FileNotFoundError(
            f"Missing {name} repository. Run: uv run python "
            "examples/healthcare/download_sources.py --all-corpora"
        )
    return entry.parent.resolve()


def resolve_document(value: str | Path) -> Path:
    """Resolve a named example, repo:relative/path, or an explicit local file."""
    text = str(value)
    if text in manifest()["documents"]:
        return document_path(text)
    name, separator, relative = text.partition(":")
    if separator and name in ("hl7", "onc", "vendor"):
        root = corpus_root(name)
        candidate = (root / relative).resolve()
        if Path(relative).is_absolute() or not candidate.is_relative_to(root):
            raise ValueError("document path escapes repository")
    else:
        candidate = Path(value).resolve()
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    return candidate


def _schema_path(name: str) -> Path:
    spec = manifest()[name]
    path = HEALTHCARE_ROOT / ".cache" / spec["destination"] / spec["entry"]
    if not path.is_file():
        raise FileNotFoundError("Run: uv run python examples/healthcare/download_sources.py")
    return path


def cda_schema_path() -> Path:
    return _schema_path("cda")


def fhir_schema_path() -> Path:
    return _schema_path("fhir")


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(80_000_001)
    if len(data) > 80_000_000:
        raise SourceVerificationError("archive too large")
    return data


def install(spec: dict, *, root: Path, force: bool = False) -> Path:
    """Install an archive atomically; --force repairs a corrupt cache."""
    target = root / spec["destination"]
    marker = target / ".verified.json"
    if target.exists() and not force:
        try:
            saved = json.loads(marker.read_text(encoding="utf-8"))
            valid = saved["archive"] == spec["sha256"] and all(
                hashlib.sha256((target / name).read_bytes()).hexdigest() == digest
                for name, digest in saved["files"].items()
            )
        except (OSError, ValueError, KeyError):
            valid = False
        if not valid:
            raise SourceVerificationError(f"corrupt cache: {target}; retry with --force")
        return target
    data = _download(spec["url"])
    if hashlib.sha256(data).hexdigest() != spec["sha256"]:
        raise SourceVerificationError("download digest mismatch")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
        stage = Path(temporary) / "tree"
        stage.mkdir()
        digests = {}
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(info.file_size for info in archive.infolist()) > 300_000_000:
                raise SourceVerificationError("expanded archive too large")
            for info in archive.infolist():
                member = PurePosixPath(info.filename)
                if member.is_absolute() or ".." in member.parts or "\\" in info.filename:
                    raise SourceVerificationError("unsafe archive member")
                if info.is_dir():
                    continue
                content = archive.read(info)
                path = stage / member
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                digests[info.filename] = hashlib.sha256(content).hexdigest()
        if not (stage / spec["entry"]).is_file():
            raise FileNotFoundError(spec["entry"])
        (stage / ".verified.json").write_text(
            json.dumps({"archive": spec["sha256"], "files": digests}, sort_keys=True),
            encoding="utf-8",
        )
        if target.exists():
            shutil.rmtree(target)
        stage.rename(target)
    return target


def fetch_sources(*, include_onc=False, include_vendor=False, force=False) -> list[Path]:
    specs = manifest()
    names = ["cda", "fhir", "hl7"]
    if include_onc:
        names.append("onc")
    if include_vendor:
        names.append("vendor")
    return [install(specs[name], root=HEALTHCARE_ROOT / ".cache", force=force) for name in names]
