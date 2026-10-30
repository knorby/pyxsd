"""Project ownership is explicit, stable, and side-effect-free."""

from pathlib import Path

import pytest

pytest.importorskip("pydantic")

from pyxsd.mcp.config import Settings


def test_default_state_is_project_local(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    assert settings.project_root == tmp_path
    assert settings.state_dir == tmp_path / ".pyxsd/mcp"
    assert not (tmp_path / ".pyxsd").exists()


def test_scope_precedence_and_freezing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "project").mkdir()
    settings = Settings(project_root=Path("project"), state_dir=Path("identity"))
    monkeypatch.chdir(tmp_path / "project")
    assert settings.project_root == tmp_path / "project"
    assert settings.state_dir == tmp_path / "identity"
    assert Settings(source_root=tmp_path).state_dir == tmp_path / "project/.pyxsd/mcp"
    assert not (tmp_path / "identity").exists()


def test_nested_project_does_not_search_for_git(tmp_path):
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "nested"
    nested.mkdir()
    assert Settings(project_root=nested).state_dir == nested / ".pyxsd/mcp"


def test_project_root_must_exist_as_directory(tmp_path):
    for path in (tmp_path / "missing", tmp_path / "file"):
        if path.name == "file":
            path.write_text("file")
        with pytest.raises(ValueError, match="project_root"):
            Settings(project_root=path)


def test_source_grant_cannot_be_reserved_state(tmp_path):
    for state in (tmp_path, tmp_path.parent):
        with pytest.raises(ValueError, match="state_dir"):
            Settings(project_root=tmp_path, state_dir=state, source_root=tmp_path)
    assert Settings(project_root=tmp_path, source_root=tmp_path).source_root == tmp_path
    assert Settings(project_root=tmp_path, persist=False, source_root=tmp_path)
