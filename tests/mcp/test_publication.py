"""Publication consumes exact retained bytes, never another Python execution."""

import hashlib

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_previews import harness, preview

from pyxsd.mcp.errors import ToolError
from pyxsd.mcp.safe_io import AdmittedFile, SafeDirectory
from pyxsd.mcp.workspaces import Principal

REPAIR = """from io import StringIO
def transform(document, context):
    return document.schema.parse(StringIO('<root><item>7</item></root>'))
"""


def test_commit_retains_bytes_replays_receipt_and_consumes_preview(tmp_path, monkeypatch):
    from pyxsd.mcp.publication import CommitTransformRequest, PublicationService

    async def scenario():
        async with harness(tmp_path) as (previews, principal, workspace):
            counter = tmp_path / "execution-count"
            source = REPAIR.replace(
                "    return",
                f"    with open({str(counter)!r}, 'a') as stream:\n"
                "        stream.write('1')\n    return",
            )
            record = await preview(previews, principal, workspace, source)

            async def forbidden_execution(*_args, **_kwargs):
                raise AssertionError("Commit must not execute Python again")

            monkeypatch.setattr(previews.execution, "run", forbidden_execution)
            publication = PublicationService(previews)
            request = CommitTransformRequest(
                preview_id=record.id,
                expected_candidate_digest=record.candidate.digest,
                destination="fixed.xml",
                idempotency_key="commit",
            )
            receipt = await publication.commit(principal, request)
            snapshot = await previews.snapshots.get(principal, workspace)
            with SafeDirectory(snapshot.root) as directory:
                content = directory.read("fixed.xml", max_bytes=8192).content
            assert hashlib.sha256(content).hexdigest() == record.candidate.digest
            assert receipt.candidate_digest == record.candidate.digest
            assert receipt.revision == snapshot.revision
            assert await publication.commit(principal, request) == receipt
            assert counter.read_text(encoding="ascii") == "1"
            with pytest.raises(ToolError, match="idempotency_conflict"):
                await publication.commit(
                    principal, request.model_copy(update={"destination": "different.xml"})
                )
            with pytest.raises(ToolError, match="preview_consumed"):
                await publication.commit(
                    principal, request.model_copy(update={"idempotency_key": "another"})
                )

    anyio.run(scenario)


@pytest.mark.parametrize("phase", ["before-intent", "after-intent", "before-head", "after-receipt"])
def test_publication_retries_durable_boundaries_without_duplicate_revision(
    tmp_path, monkeypatch, phase
):
    from pyxsd.mcp.publication import CommitTransformRequest, PublicationService

    async def scenario():
        async with harness(tmp_path) as (previews, principal, workspace):
            record = await preview(previews, principal, workspace, REPAIR)
            request = CommitTransformRequest(
                preview_id=record.id,
                expected_candidate_digest=record.candidate.digest,
                destination="recovered.xml",
                idempotency_key="recover",
            )
            state = previews.snapshots.state
            state_run = state.run
            publish_files = previews.snapshots._publish_files

            async def interrupted_state(callback):
                if phase == "before-intent" and callback.__name__ == "intent":
                    raise OSError("Injected before intent")
                return await state_run(callback)

            async def interrupted_publish(*args, **kwargs):
                if phase == "after-intent":
                    raise OSError("Injected after durable intent")
                if phase == "before-head":

                    async def fail(_snapshot):
                        raise OSError("Injected after staging bytes")

                    kwargs["before_publish"] = fail
                result = await publish_files(*args, **kwargs)
                if phase == "after-receipt":
                    raise OSError("Injected after committed receipt")
                return result

            monkeypatch.setattr(state, "run", interrupted_state)
            monkeypatch.setattr(previews.snapshots, "_publish_files", interrupted_publish)
            with pytest.raises(OSError, match="Injected"):
                await PublicationService(previews).commit(principal, request)
            monkeypatch.setattr(state, "run", state_run)
            monkeypatch.setattr(previews.snapshots, "_publish_files", publish_files)
            receipt = await PublicationService(previews).commit(principal, request)
            assert receipt.candidate_digest == record.candidate.digest
            assert await PublicationService(previews).commit(principal, request) == receipt
            count = await state.run(
                lambda db: db.execute(
                    "SELECT count(*) FROM snapshots WHERE status='ready'"
                ).fetchone()[0]
            )
            assert count == 2

    anyio.run(scenario)


@pytest.mark.parametrize("malformed", [False, True])
def test_invalid_publication_requires_explicit_operator_and_caller_permission(tmp_path, malformed):
    from pyxsd.mcp.publication import CommitTransformRequest, PublicationService

    async def scenario():
        async with harness(tmp_path) as (previews, principal, _workspace):
            content = "<root>" if malformed else "<root><item>bad</item></root>"
            source = (
                "from pyxsd.document import Document\n"
                f"def transform(document, context):\n    Document.to_string = lambda self: {content!r}\n"
            )
            record = await preview(previews, principal, _workspace, source)
            request = CommitTransformRequest(
                preview_id=record.id,
                expected_candidate_digest=record.candidate.digest,
                destination="invalid.xml",
                idempotency_key="invalid",
                allow_invalid=True,
            )
            with pytest.raises(ToolError, match="invalid_candidate"):
                await PublicationService(previews).commit(principal, request)
            if malformed:
                with pytest.raises(ToolError, match="invalid_candidate"):
                    await PublicationService(previews, write_invalid=True).commit(
                        principal, request
                    )
            else:
                limited = Principal(
                    principal.owner_id, frozenset({"pyxsd:read", "pyxsd:upload", "pyxsd:write"})
                )
                with pytest.raises(ToolError, match="forbidden"):
                    await PublicationService(previews, write_invalid=True).commit(limited, request)
                receipt = await PublicationService(previews, write_invalid=True).commit(
                    principal, request
                )
                assert receipt.valid is False

    anyio.run(scenario)


@pytest.mark.parametrize("case", ["digest", "exists", "scope", "revision", "path"])
def test_publication_preconditions_refuse_without_changing_revision(tmp_path, case):
    from pyxsd.mcp.publication import CommitTransformRequest, PublicationService

    async def scenario():
        async with harness(tmp_path) as (previews, principal, workspace):
            record = await preview(previews, principal, workspace, REPAIR)
            arguments = {
                "preview_id": record.id,
                "expected_candidate_digest": record.candidate.digest,
                "destination": "fixed.xml",
                "idempotency_key": "refusal",
            }
            code = "candidate_mismatch"
            if case == "digest":
                arguments["expected_candidate_digest"] = "0" * 64
            elif case == "exists":
                arguments["destination"] = "document.xml"
                code = "publication_conflict"
            elif case == "scope":
                principal = Principal(principal.owner_id, frozenset({"pyxsd:read"}))
                code = "forbidden"
            elif case == "revision":
                await previews.snapshots.put_bytes(
                    principal,
                    workspace,
                    [AdmittedFile("other.xml", b"<other/>")],
                    expected_revision=record.revision,
                )
                code = "stale_reference"
            else:
                arguments["destination"] = "../escape.xml"
                code = "invalid_path"
            before = await previews.snapshots.get(principal, workspace)
            with pytest.raises(ToolError, match=code):
                await PublicationService(previews).commit(
                    principal, CommitTransformRequest(**arguments)
                )
            assert (await previews.snapshots.get(principal, workspace)).revision == before.revision

    anyio.run(scenario)


def test_concurrent_same_key_returns_one_receipt_and_revision(tmp_path, monkeypatch):
    from pyxsd.mcp.publication import CommitTransformRequest, PublicationService

    async def scenario():
        async with harness(tmp_path) as (previews, principal, workspace):
            record = await preview(previews, principal, workspace, REPAIR)
            request = CommitTransformRequest(
                preview_id=record.id,
                expected_candidate_digest=record.candidate.digest,
                destination="concurrent.xml",
                idempotency_key="concurrent",
            )
            entered, release = anyio.Event(), anyio.Event()
            original = previews.snapshots._publish_files

            async def paused(*args, **kwargs):
                entered.set()
                await release.wait()
                return await original(*args, **kwargs)

            monkeypatch.setattr(previews.snapshots, "_publish_files", paused)
            receipts = []

            async def commit():
                receipts.append(await PublicationService(previews).commit(principal, request))

            with anyio.fail_after(10):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(commit)
                    await entered.wait()
                    tasks.start_soon(commit)
                    await anyio.wait_all_tasks_blocked()
                    release.set()
            assert len(receipts) == 2
            assert receipts[0] == receipts[1]
            count = await previews.snapshots.state.run(
                lambda db: db.execute(
                    "SELECT count(*) FROM snapshots WHERE status='ready'"
                ).fetchone()[0]
            )
            assert count == 2

    anyio.run(scenario)


def test_receipt_lifetime_matches_workspace_lifetime(tmp_path):
    from pyxsd.mcp.publication import CommitTransformRequest, PublicationService

    async def scenario():
        async with harness(tmp_path) as (previews, principal, workspace):
            record = await preview(previews, principal, workspace, REPAIR)
            request = CommitTransformRequest(
                preview_id=record.id,
                expected_candidate_digest=record.candidate.digest,
                destination="fixed.xml",
                idempotency_key="lifetime",
            )
            publication = PublicationService(previews)
            await publication.commit(principal, request)
            await previews.snapshots.workspaces.release(principal, workspace)
            count = await previews.snapshots.state.run(
                lambda db: db.execute("SELECT count(*) FROM publications").fetchone()[0]
            )
            assert count == 0
            with pytest.raises(ToolError, match="not_found"):
                await publication.commit(principal, request)

    anyio.run(scenario)


@pytest.mark.parametrize("target", ["source", "candidate-reference"])
def test_changed_retained_inputs_refuse_publication(tmp_path, monkeypatch, target):
    from tests.mcp.test_exploration import _chunks

    from pyxsd.mcp.publication import CommitTransformRequest, PublicationService

    async def scenario():
        async with harness(tmp_path) as (previews, principal, workspace):
            record = await preview(previews, principal, workspace, REPAIR)
            expected = "source_changed"
            if target == "source":
                snapshot = await previews.snapshots.get(principal, workspace)
                (snapshot.root / "document.xml").write_bytes(b"<changed/>")
            else:
                other = await previews.artifacts.put(
                    principal,
                    workspace,
                    _chunks(b"<other/>"),
                    media_type="application/xml",
                    limit=8192,
                    ttl=60,
                )
                original_get = previews.get

                async def mismatched(*args):
                    original = await original_get(*args)
                    return original.model_copy(
                        update={"candidate": original.candidate.model_copy(update={"id": other.id})}
                    )

                monkeypatch.setattr(previews, "get", mismatched)
                expected = "candidate_mismatch"
            with pytest.raises(ToolError, match=expected):
                await PublicationService(previews).commit(
                    principal,
                    CommitTransformRequest(
                        preview_id=record.id,
                        expected_candidate_digest=record.candidate.digest,
                        destination="fixed.xml",
                        idempotency_key="integrity",
                    ),
                )

    anyio.run(scenario)


@pytest.mark.parametrize("state", ["expired", "missing", "foreign-owner"])
def test_unavailable_preview_cannot_publish(tmp_path, state):
    from pyxsd.mcp.publication import CommitTransformRequest, PublicationService

    async def scenario():
        async with harness(tmp_path) as (previews, principal, workspace):
            record = await preview(previews, principal, workspace, REPAIR)
            code = "preview_expired"
            if state == "expired":
                expired = record.model_copy(update={"expires_at": 0.0})
                previews._previews[(principal.owner_id, record.id)] = expired
            elif state == "missing":
                code = "preview_unavailable"
                previews._previews.pop((principal.owner_id, record.id))
            else:
                code = "preview_unavailable"
                principal = Principal("other", principal.scopes)
            with pytest.raises(ToolError, match=code):
                await PublicationService(previews).commit(
                    principal,
                    CommitTransformRequest(
                        preview_id=record.id,
                        expected_candidate_digest=record.candidate.digest,
                        destination="fixed.xml",
                        idempotency_key="unavailable",
                    ),
                )

    anyio.run(scenario)


def test_replace_requires_scope_and_matching_destination_digest(tmp_path):
    from pyxsd.mcp.publication import CommitTransformRequest, PublicationService

    async def scenario():
        async with harness(tmp_path) as (previews, principal, workspace):
            record = await preview(previews, principal, workspace, REPAIR)
            publication = PublicationService(previews)
            request = CommitTransformRequest(
                preview_id=record.id,
                expected_candidate_digest=record.candidate.digest,
                destination="document.xml",
                mode="replace",
                idempotency_key="replace",
            )
            with pytest.raises(ToolError, match="publication_conflict"):
                await publication.commit(principal, request)
            limited = Principal(principal.owner_id, principal.scopes - {"pyxsd:replace"})
            with pytest.raises(ToolError, match="forbidden"):
                await publication.commit(limited, request)
            request = request.model_copy(
                update={"expected_destination_digest": record.input_digest}
            )
            receipt = await publication.commit(principal, request)
            assert receipt.candidate_digest == record.candidate.digest

    anyio.run(scenario)
