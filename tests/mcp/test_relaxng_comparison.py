"""Language dispatch and identical-reference lease handling for comparison."""

import anyio
import pytest
from tests.mcp.test_comparison import IDENTICAL
from tests.mcp.test_exploration import harness, upload

from pyxsd.mcp.contracts import FileRef, SchemaRef
from pyxsd.mcp.services.comparison import CompareSchemasRequest, ComparisonService

RNG = '<element xmlns="http://relaxng.org/ns/structure/1.0" name="root"><empty/></element>'


@pytest.mark.parametrize("format", ["relax-ng", "xsd"])
def test_identical_reference_uses_one_lease(tmp_path, format):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs, workspace, {"schema.xml": RNG if format == "relax-ng" else IDENTICAL}
            )
            ref = SchemaRef(source=FileRef(path="schema.xml"), format=format)
            result = await ComparisonService(inputs, pool).compare(
                CompareSchemasRequest(workspace_id=workspace, old_schema=ref, new_schema=ref)
            )
            assert result["structural"]["totals"] == {"added": 0, "removed": 0, "changed": 0}

    anyio.run(scenario)


@pytest.mark.parametrize("reverse", [False, True])
def test_mixed_languages_are_explicitly_not_comparable(tmp_path, reverse):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(inputs, workspace, {"old.rng": RNG, "new.xsd": IDENTICAL})
            refs = [
                SchemaRef(source=FileRef(path="old.rng"), format="relax-ng"),
                SchemaRef(source=FileRef(path="new.xsd")),
            ]
            if reverse:
                refs.reverse()
            result = await ComparisonService(inputs, pool).compare(
                CompareSchemasRequest(
                    workspace_id=workspace, old_schema=refs[0], new_schema=refs[1]
                )
            )
            assert result["structural"]["status"] == "not_comparable"
            assert "added" not in result["structural"]
            assert result["corpus"]["status"] == "unsupported"

    anyio.run(scenario)


def test_rng_diff_and_corpus_are_separated(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, inputs, pool, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "old.rng": RNG,
                    "new.rng": RNG.replace("<empty/>", "<text/>"),
                    "doc.xml": "<root/>",
                },
            )
            result = await ComparisonService(inputs, pool).compare(
                CompareSchemasRequest(
                    workspace_id=workspace,
                    old_schema=SchemaRef(source=FileRef(path="old.rng"), format="relax-ng"),
                    new_schema=SchemaRef(source=FileRef(path="new.rng"), format="relax-ng"),
                    corpus=(FileRef(path="doc.xml"),),
                )
            )
            assert result["structural"]["changed"]
            assert result["corpus"]["status"] == "unsupported"
            assert "totals" not in result["corpus"]
            assert "No" in result["conclusions"]["statement"]

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("old_format", "new_format", "identical"),
    [
        ("relax-ng", "relax-ng", False),
        ("relax-ng", "relax-ng", True),
        ("xsd", "xsd", True),
        ("xsd", "relax-ng", False),
        ("relax-ng", "xsd", False),
    ],
)
def test_worker_dispatch_with_real_cache_leases(tmp_path, old_format, new_format, identical):
    from pyxsd.mcp.cache import SchemaCache
    from pyxsd.mcp.services.comparison import execute_comparison
    from pyxsd.mcp.worker_protocol import ComparisonOperation
    from pyxsd.mcp.workers import capture_to_wire

    async def scenario():
        async with harness(tmp_path) as (_, inputs, _, workspace):
            await upload(
                inputs,
                workspace,
                {
                    "old.xml": RNG if old_format == "relax-ng" else IDENTICAL,
                    "new.xml": RNG if new_format == "relax-ng" else IDENTICAL,
                },
            )
            old = SchemaRef(source=FileRef(path="old.xml"), format=old_format)
            new = old if identical else SchemaRef(source=FileRef(path="new.xml"), format=new_format)
            async with inputs.capture(workspace) as capture:
                cache = SchemaCache(inputs.limits)
                try:
                    result = execute_comparison(
                        ComparisonOperation(
                            request_id="comparison",
                            capture=capture_to_wire(capture),
                            old_schema=old,
                            new_schema=new,
                        ),
                        cache,
                        "test",
                    )
                    assert cache.info().active == 0
                    assert cache.info().misses == (1 if identical else 2)
                    if old_format == new_format:
                        assert result["structural"]["totals"] == {
                            "added": 0,
                            "removed": 0,
                            "changed": 0,
                        }
                    else:
                        assert result["structural"]["status"] == "not_comparable"
                finally:
                    cache.close()

    anyio.run(scenario)
