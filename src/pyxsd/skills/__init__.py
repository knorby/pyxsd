"""Portable skill packaging: list and export guidance without MCP installed.

Skills are plain directories under this package: ``<name>/SKILL.md`` plus any
reference files. Export copies the directory verbatim to a caller-chosen
destination; nothing writes to a global home location, and destinations that
traverse symlinks or already exist are refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

_MAX_NAME = 64
_MAX_DESCRIPTION = 1024


@dataclass(frozen=True)
class SkillInfo:
    """One packaged skill's public metadata."""

    name: str
    description: str


def _parse_metadata(name: str, document: str) -> SkillInfo:
    match = re.search(r"^---\s*\n(.*?\n)?---\s*\n", document, re.DOTALL)
    header = match.group(1) if match else ""
    fields: dict[str, str] = {}
    for line in header.splitlines():
        key, separator, value = line.partition(":")
        if separator and value.strip():
            fields[key.strip()] = value.strip()
    description = fields.get("description", "")
    if len(name) > _MAX_NAME or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name) is None:
        raise ValueError(f"Skill name is invalid: {name!r}")
    if fields.get("name") != name:
        raise ValueError(f"Skill metadata name must match directory: {name!r}")
    if not description or len(description) > _MAX_DESCRIPTION:
        raise ValueError(f"Skill description is invalid for: {name!r}")
    return SkillInfo(name=name, description=description)


def list_skills() -> tuple[SkillInfo, ...]:
    """Return every packaged skill with bounded, validated metadata."""
    root = resources.files("pyxsd.skills")
    skills: list[SkillInfo] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        if not (entry / "SKILL.md").is_file():
            continue
        document = (entry / "SKILL.md").read_text(encoding="utf-8")
        skills.append(_parse_metadata(entry.name, document))
    return tuple(skills)


def export_skill(name: str, destination: Path) -> Path:
    """Copy one skill directory verbatim; refuse overwrite and symlink travel.

    The destination must exist and be a real directory. The exported skill
    lands in ``destination / name``; an existing target is an error, and any
    symlink encountered while walking the destination path is refused so an
    export cannot be redirected through a link.
    """
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name):
        raise KeyError(name)
    known = {skill.name for skill in list_skills()}
    if name not in known:
        raise KeyError(name)
    if not isinstance(destination, Path):
        destination = Path(destination)
    target = destination / name
    probe = destination
    if not probe.exists() or not probe.is_dir():
        raise FileNotFoundError(str(destination))
    while True:
        if probe.is_symlink():
            raise ValueError("Destination path traverses a symlink")
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise ValueError("Export target is not a plain directory")
        if probe == probe.parent:
            break
        probe = probe.parent
    if target.exists():
        raise FileExistsError(str(target))

    source_root = resources.files("pyxsd.skills") / name
    shared = resources.files("pyxsd.skills") / "references"

    def _walk(entry: Traversable, prefix: str) -> list[tuple[str, Traversable]]:
        """Depth-first listing with relative names; Traversable has no rglob."""
        found: list[tuple[str, Traversable]] = []
        for child in sorted(entry.iterdir(), key=lambda item: item.name):
            relative = f"{prefix}{child.name}"
            if child.is_dir():
                found.extend(_walk(child, f"{relative}/"))
            else:
                found.append((relative, child))
        return found

    for relative, entry in _walk(source_root, ""):
        destination_file = target / relative
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        destination_file.write_bytes(entry.read_bytes())
    for relative, entry in _walk(shared, ""):
        destination_file = target / "references" / relative
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        destination_file.write_bytes(entry.read_bytes())
    return target
