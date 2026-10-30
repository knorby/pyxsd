"""Portable skill packaging: bounded metadata, deterministic, safe exports."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from pyxsd.skills import SkillInfo, export_skill, list_skills

EXPECTED = {"pyxsd-development", "pyxsd-mcp", "pyxsd-xml-doctor"}


@pytest.mark.parametrize("declared", ["different-name", ""])
def test_skill_metadata_rejects_missing_or_mismatched_name(declared):
    from pyxsd.skills import _parse_metadata

    with pytest.raises(ValueError, match="name"):
        _parse_metadata("directory-name", f"---\nname: {declared}\ndescription: Example\n---\n")


@pytest.mark.parametrize(
    "name,description",
    [("x" * 65, "Example"), ("bad/name", "Example"), ("valid", ""), ("valid", "x" * 1025)],
)
def test_skill_metadata_rejects_invalid_bounds(name, description):
    from pyxsd.skills import _parse_metadata

    with pytest.raises(ValueError):
        _parse_metadata(name, f"---\nname: {name}\ndescription: {description}\n---\n")


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


def test_export_rejects_invalid_names_and_accepts_string_destinations(tmp_path):
    from pyxsd.skills import export_skill

    with pytest.raises(KeyError):
        export_skill(123, tmp_path)
    destination = str(tmp_path)
    exported = export_skill("pyxsd-xml-doctor", destination)  # type: ignore[arg-type]
    assert exported == tmp_path / "pyxsd-xml-doctor"


def test_export_refuses_a_symlinked_target(tmp_path):
    from pyxsd.skills import export_skill

    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "pyxsd-mcp").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="plain directory"):
        export_skill("pyxsd-mcp", tmp_path)


def test_skills_cli_main_covers_list_export_and_error_paths(tmp_path, capsys):
    from pyxsd.skills.__main__ import main

    assert main(["list"]) == 0
    assert "pyxsd-development:" in capsys.readouterr().out

    destination = tmp_path / "direct"
    destination.mkdir()
    assert main(["export", "pyxsd-xml-doctor", str(destination)]) == 0
    assert str(destination / "pyxsd-xml-doctor") in capsys.readouterr().out

    assert main(["export", "not-a-skill", str(destination)]) == 2
    assert "Unknown skill" in capsys.readouterr().err

    assert main(["export", "pyxsd-xml-doctor", str(destination)]) == 2
    assert "Cannot export skill" in capsys.readouterr().err

    assert main(["export", "pyxsd-mcp", str(tmp_path / "missing")]) == 2
    assert "Cannot export skill" in capsys.readouterr().err


def test_skills_module_entrypoint_lists_skills(monkeypatch, capsys):
    import runpy
    import sys

    monkeypatch.setattr(sys, "argv", ["pyxsd.skills", "list"])
    with (
        pytest.warns(RuntimeWarning, match="found in sys.modules"),
        pytest.raises(SystemExit) as raised,
    ):
        runpy.run_module("pyxsd.skills", run_name="__main__")
    assert raised.value.code == 0
    assert "pyxsd-mcp:" in capsys.readouterr().out


def test_skills_cli_lists_and_exports_without_mcp(tmp_path):
    listed = subprocess.run(
        [sys.executable, "-m", "pyxsd.skills", "list"],
        capture_output=True,
        text=True,
        encoding="utf-8",
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
        encoding="utf-8",
        timeout=30,
    )
    assert exported.returncode == 0, exported.stderr
    assert (destination / "pyxsd-xml-doctor" / "SKILL.md").is_file()
    help_text = subprocess.run(
        [sys.executable, "-m", "pyxsd.skills", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert help_text.returncode == 0


def test_npx_repository_root_discovers_all_packaged_skills():
    command = "npx skills add https://github.com/knorby/pyxsd"
    for document in (Path("SKILLS.md"), Path("docs/skills.md")):
        text = document.read_text(encoding="utf-8")
        assert command in text
        assert "/tree/develop/src/pyxsd/skills/" not in text
    for name in EXPECTED:
        assert (Path("src/pyxsd/skills") / name / "SKILL.md").is_file()
