"""Portable skill packaging: bounded metadata, deterministic, safe exports."""

import re
import subprocess
import sys

import pytest

from pyxsd.skills import SkillInfo, export_skill, list_skills

EXPECTED = {"pyxsd-development", "pyxsd-mcp", "pyxsd-xml-doctor"}


def test_list_skills_reports_bounded_metadata():
    skills = list_skills()
    assert {skill.name for skill in skills} == EXPECTED
    for skill in skills:
        assert isinstance(skill, SkillInfo)
        assert len(skill.name) <= 64
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", skill.name)
        assert 0 < len(skill.description) <= 1024
        assert skill.description != skill.name


def test_exported_bundles_are_deterministic_and_self_contained(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    for destination in (first, second):
        destination.mkdir()
        for skill in list_skills():
            exported = export_skill(skill.name, destination)
            assert exported == destination / skill.name
            assert (exported / "SKILL.md").is_file()
    for skill in EXPECTED:
        left = sorted(
            path.relative_to(first / skill).as_posix()
            for path in (first / skill).rglob("*")
            if path.is_file()
        )
        right = sorted(
            path.relative_to(second / skill).as_posix()
            for path in (second / skill).rglob("*")
            if path.is_file()
        )
        assert left == right
        assert left, f"{skill} exported no files"
        for relative in left:
            assert (first / skill / relative).read_bytes() == (
                second / skill / relative
            ).read_bytes()


def test_exported_skills_reference_only_local_files(tmp_path):
    for skill in list_skills():
        exported = export_skill(skill.name, tmp_path)
        markdown = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(exported.rglob("*.md"))
        )
        assert "../" not in markdown
        assert "/Users/" not in markdown
        assert "C:\\" not in markdown


def test_export_rejects_unknown_overwrite_and_traversal(tmp_path):
    with pytest.raises(KeyError):
        export_skill("not-a-skill", tmp_path)
    export_skill("pyxsd-xml-doctor", tmp_path)
    with pytest.raises(FileExistsError):
        export_skill("pyxsd-xml-doctor", tmp_path)
    linked = tmp_path / "linked"
    linked.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (linked / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        export_skill("pyxsd-mcp", linked / "escape")


def test_skills_cli_lists_and_exports_without_mcp(tmp_path):
    listed = subprocess.run(
        [sys.executable, "-m", "pyxsd.skills", "list"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert listed.returncode == 0, listed.stderr
    for name in EXPECTED:
        assert name in listed.stdout
    destination = tmp_path / "cli"
    destination.mkdir()
    exported = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyxsd.skills",
            "export",
            "pyxsd-xml-doctor",
            str(destination),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert exported.returncode == 0, exported.stderr
    assert (destination / "pyxsd-xml-doctor" / "SKILL.md").is_file()
    help_text = subprocess.run(
        [sys.executable, "-m", "pyxsd.skills", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert help_text.returncode == 0
