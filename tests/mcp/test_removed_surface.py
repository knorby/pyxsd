"""The cutover leaves one artifact store and no legacy public surface."""

from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("pydantic")

import anyio

from pyxsd.mcp.config import Limits, Settings
from pyxsd.mcp.storage import Storage


def test_obsolete_modules_and_symbols_are_absent():
    package = Path(__file__).parents[2] / "src" / "pyxsd" / "mcp"
    for name in (
        "state.py",
        "workspaces.py",
        "snapshot_store.py",
        "snapshots.py",
        "artifacts.py",
        "publication.py",
    ):
        assert not (package / name).exists()
    sources = "\n".join(path.read_text(encoding="utf-8") for path in package.rglob("*.py"))
    for symbol in ("Principal", "WorkspaceStore", "SnapshotStore", "PublicationService"):
        assert symbol not in sources
    for field in (
        "artifact_entries",
        "artifact_bytes_per_workspace",
        "snapshot_revisions",
        "managed_bytes",
        "workspace_idle_seconds",
        "preview_seconds",
        "diagnostic_seconds",
    ):
        assert field not in Limits.model_fields
    assert set(Settings.model_fields) == {
        "project_root",
        "state_dir",
        "persist",
        "execution_backend",
        "limits",
        "source_root",
        "enabled_groups",
        "pydantic",
        "arrow",
    }


def test_fresh_store_has_only_the_artifact_table(tmp_path):
    async def scenario():
        from pyxsd.mcp.output_store import OutputStore

        with Storage(tmp_path / "store") as storage:
            outputs = OutputStore(storage, Limits())
            await outputs.put_bytes(b"x", name="a.txt", media_type="text/plain", kind="result")
            try:
                tables = await storage.run(
                    lambda db: {
                        row[0]
                        for row in db.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                    }
                )
                version = await storage.run(
                    lambda db: db.execute("PRAGMA user_version").fetchone()[0]
                )
            finally:
                outputs.close()
        assert tables == {"artifacts"}
        assert version == 1

    anyio.run(scenario)


def test_advertised_tools_have_no_removed_parameters(tmp_path):
    from tests.mcp.test_server import connected

    async def scenario():
        async with connected(
            tmp_path,
            enabled_groups={"exploration", "workspaces", "execution"},
        ) as (client, _services):
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert "pyxsd_commit_transform" not in tools
            assert "pyxsd_job_status" not in tools
            assert "pyxsd_delete_artifact" in tools
            assert "pyxsd_list_artifacts" in tools
            for name in ("pyxsd_manage_workspace", "pyxsd_put_files"):
                properties = tools[name].input_schema["properties"]
                assert "revision" not in properties
                assert "expected_revision" not in properties
                assert "idempotency_key" not in properties
            assert set(tools["pyxsd_manage_workspace"].input_schema["properties"]) == {
                "action",
                "workspace_id",
            }
            assert set(tools["pyxsd_put_files"].input_schema["properties"]) == {
                "workspace_id",
                "files",
            }
            assert set(tools["pyxsd_read_artifact"].input_schema["properties"]) == {
                "artifact_id",
                "offset",
                "length",
            }
            assert set(tools["pyxsd_list_artifacts"].input_schema["properties"]) == {
                "limit",
                "cursor",
            }

    anyio.run(scenario)
