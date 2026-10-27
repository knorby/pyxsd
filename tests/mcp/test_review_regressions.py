"""Boundary regressions found during the D1-E4 review."""

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anyio")
pytest.importorskip("defusedxml")

import anyio
from tests.mcp.test_jobs import harness, operation, terminal

from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import FileRef, SchemaRef
from pyxsd.mcp.state import State
from pyxsd.mcp.worker_protocol import ComparisonOperation, ExportOperation, WorkerReply
from pyxsd.mcp.workers import snapshot_to_wire


@pytest.mark.parametrize(
    "kind,budget", [("parse", 11), ("execution", 22), ("export", 33), ("compare", 33)]
)
def test_job_deadline_uses_operation_budget(tmp_path, kind, budget):
    async def scenario():
        async with harness(
            tmp_path, Limits(compile_query_seconds=11, transform_seconds=22, batch_seconds=33)
        ) as (jobs, _workspaces, pool, principal, workspace, snapshot, _now):
            from pyxsd.mcp import workers

            jobs.monotonic = lambda: 100.0
            deadlines = []

            async def run(op, *, deadline):
                deadlines.append(deadline)
                return WorkerReply(request_id=op.request_id, role=op.role, status="ok", result={})

            pool.run = run
            op = operation(workers, snapshot)
            schema = SchemaRef(source=FileRef(path="schema.xsd"))
            fields = {
                "request_id": "budget",
                "owner_id": principal.owner_id,
                "snapshot": snapshot_to_wire(snapshot),
            }
            if kind == "execution":
                op = op.model_copy(update={"role": "execution"})
            elif kind == "export":
                op = ExportOperation(**fields, schema_ref=schema, documents=(), element="root")
            elif kind == "compare":
                op = ComparisonOperation(**fields, old_schema=schema, new_schema=schema)
            ref = await jobs.submit(principal, workspace.id, op, idempotency_key=kind)
            assert (await terminal(jobs, ref.id)).state == "succeeded"
            assert deadlines == [100.0 + budget]

    anyio.run(scenario)


def test_corpus_limit_cannot_exceed_operator_maximum():
    assert Limits(corpus_files=1000).corpus_files == 1000
    assert Limits(corpus_files=2, corpus_max_files=2).corpus_files == 2
    with pytest.raises(ValueError, match="corpus"):
        Limits(corpus_files=1001)
    with pytest.raises(ValueError, match="corpus"):
        Limits(corpus_files=3, corpus_max_files=2)


@pytest.mark.parametrize("through_services", [False, True])
def test_restart_recovery_honors_configured_result_retention(tmp_path, through_services):
    async def scenario():
        async with harness(tmp_path) as (
            jobs,
            workspaces,
            _pool,
            principal,
            workspace,
            snapshot,
            _now,
        ):
            from pyxsd.mcp import workers

            ref = await jobs.submit(
                principal, workspace.id, operation(workers, snapshot), idempotency_key="restart-ttl"
            )
            assert (await terminal(jobs, ref.id)).state == "succeeded"
            await workspaces.state.run(
                lambda db: db.execute(
                    "UPDATE jobs SET state='running' WHERE id=?", (ref.id,)
                ).close()
            )
            root = workspaces.state.root
        limits = Limits(job_result_seconds=7)
        if through_services:
            pytest.importorskip("mcp")
            from pyxsd.mcp.config import Settings
            from pyxsd.mcp.server import Services

            async with Services(Settings(state_dir=root, limits=limits)) as services:
                row = await services.state.run(
                    lambda db: db.execute("SELECT * FROM jobs WHERE id=?", (ref.id,)).fetchone()
                )
        else:
            with State(root, limits=limits) as recovered:
                row = await recovered.run(
                    lambda db: db.execute("SELECT * FROM jobs WHERE id=?", (ref.id,)).fetchone()
                )
        assert row["state"] == "failed"
        assert row["expires"] - row["completed"] == 7

    anyio.run(scenario)
