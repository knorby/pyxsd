"""Persistent state only appears when retaining output."""

import pytest

pytest.importorskip("mcp")
import anyio

from pyxsd.mcp.config import Limits, Settings
from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.output_store import OutputStore
from pyxsd.mcp.server import Services, create_server
from pyxsd.mcp.storage import Storage
from pyxsd.mcp.tools import capability_data


def test_discovery_and_absent_reads_leave_project_untouched(tmp_path):
    async def scenario():
        settings = Settings(project_root=tmp_path)
        async with Services(settings) as services:
            create_server(settings, services)
            capabilities = capability_data(services)
            assert capabilities["project_root"] == str(tmp_path)
            assert capabilities["state_dir"] == str(tmp_path / ".pyxsd/mcp")
            assert capabilities["state_creation"] == "first_artifact"
            await services.inputs.create()
            assert await services.outputs.list() == ([], None)
            with pytest.raises(ToolError, match="not_found"):
                await services.outputs.read("a" * 32, offset=0, length=1)
            await services.outputs.delete("a" * 32)
            assert not (tmp_path / ".pyxsd").exists()
        assert not (tmp_path / ".pyxsd").exists()

    anyio.run(scenario)


def test_source_exclusion_preserves_safe_directory_symlink_faults(tmp_path):
    from pyxsd.mcp.contracts import FileRef

    (tmp_path / "loop.xsd").symlink_to("loop.xsd")

    async def scenario():
        async with Services(Settings(project_root=tmp_path, source_root=tmp_path)) as services:
            workspace = await services.inputs.create()
            with pytest.raises(ToolError):
                async with services.inputs.operation_capture(
                    workspace.id, (FileRef(path="loop.xsd"),)
                ):
                    pytest.fail("source symlink admitted")

    anyio.run(scenario)


def test_first_output_and_peer_discovery(tmp_path):
    async def scenario():
        root = tmp_path / "state"
        with Storage(root) as a, Storage(root) as b:
            first, second = OutputStore(a, Limits()), OutputStore(b, Limits())
            assert a.scratch != b.scratch
            assert not a.scratch.is_relative_to(root)
            assert await second.list() == ([], None)
            with pytest.raises(ToolError, match="invalid_argument"):
                await first.put_bytes(b"x", name="../bad", media_type="text/plain", kind="result")
            assert not root.exists()
            ref = await first.put_bytes(
                b"hello", name="a.txt", media_type="text/plain", kind="result"
            )
            assert await second.read(ref.id, offset=0, length=5) == b"hello"
            a.close()
            assert await second.get(ref.id) == ref
        with Storage(root) as reopened:
            assert await OutputStore(reopened, Limits()).get(ref.id) == ref

    anyio.run(scenario)


def test_constructors_do_not_reconcile_peer_staging(tmp_path):
    root = tmp_path / "state"
    staging = root / "store-v2/artifacts/active"
    staging.mkdir(parents=True)
    marker = staging / "a.tmp"
    marker.write_bytes(b"in progress")
    with Storage(root) as storage:
        OutputStore(storage, Limits())
    assert marker.read_bytes() == b"in progress"


@pytest.mark.parametrize("custom", [False, True])
def test_source_state_exclusion_and_uploaded_names(tmp_path, custom):
    from pyxsd.mcp.contracts import FileRef, InlineFile, SchemaRef

    async def scenario():
        settings = Settings(
            project_root=tmp_path,
            source_root=tmp_path,
            **({"state_dir": tmp_path / "custom-state"} if custom else {}),
        )
        state = settings.state_dir
        relative_state = state.relative_to(tmp_path).as_posix()
        state.mkdir(parents=True)
        schema = '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"/>'
        (state / "private.xsd").write_text(schema)
        (tmp_path / "main.xsd").write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            f'<xs:include schemaLocation="{relative_state}/private.xsd"/></xs:schema>'
        )
        async with Services(settings) as services:
            workspace = await services.inputs.create()
            for path in (f"{relative_state}/private.xsd", "main.xsd"):
                with pytest.raises(ToolError, match="invalid_argument"):
                    async with services.inputs.operation_capture(
                        workspace.id, (SchemaRef(source=FileRef(path=path)),)
                    ):
                        pytest.fail("state admitted as source")
            await services.inputs.put_files(
                workspace.id, [InlineFile(path=".pyxsd/mcp/private.xsd", text=schema)]
            )
            async with services.inputs.operation_capture(
                workspace.id, (FileRef(path=".pyxsd/mcp/private.xsd"),)
            ) as capture:
                assert len(capture.files) == 1

    anyio.run(scenario)
