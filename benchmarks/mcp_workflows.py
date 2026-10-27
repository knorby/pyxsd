"""Measure clean-worker schema reuse without inventing release thresholds.

The benchmark reports observable counts and resource figures only. It asserts
the structural cache contract: one compile per cold clean worker, none for a
warm revision, and one after a dependency changes.
"""

import hashlib
import json
import resource
import shutil
import sys
import tempfile
import time
from pathlib import Path

from pyxsd import __version__
from pyxsd.mcp.cache import CompileRequest, SchemaCache
from pyxsd.mcp.config import Limits
from pyxsd.mcp.contracts import FileRef, SchemaRef
from pyxsd.mcp.snapshots import Snapshot, SnapshotFile, WorkspacePolicyError

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mcp"


def _manifest(root: Path, names: list[str]) -> tuple[SnapshotFile, ...]:
    entries = []
    for name in sorted(names):
        content = (root / name).read_bytes()
        entries.append(SnapshotFile(name, hashlib.sha256(content).hexdigest(), len(content)))
    return tuple(entries)


def _run(cache: SchemaCache, snapshot: Snapshot, schema: str, document: str) -> float:
    request = CompileRequest(
        snapshot=snapshot,
        schema_ref=SchemaRef(source=FileRef(path=schema)),
        hints=(),
        processor_version=__version__,
        owner_id="benchmark",
    )
    started = time.perf_counter()
    with cache.acquire(request) as compiled:
        parsed = compiled.parse("/workspace/" + document)
        assert parsed.report.error_count == 0
    return (time.perf_counter() - started) * 1000


def _small_fixture_metrics(work: Path, limits: Limits) -> dict[str, object]:
    source = work / "small"
    shutil.copytree(FIXTURE, source)
    names = ["catalog.xsd", "types.xsd", "document.xml"]
    snapshot = Snapshot("benchmark", "small-cold", source, _manifest(source, names))
    cache = SchemaCache(limits)
    try:
        before = cache.info().misses
        cold_ms = _run(cache, snapshot, "catalog.xsd", "document.xml")
        cold_compiles = cache.info().misses - before
        before = cache.info().misses
        warm_ms = _run(cache, snapshot, "catalog.xsd", "document.xml")
        warm_compiles = cache.info().misses - before
        changed = source / "types.xsd"
        changed.write_text(
            changed.read_text(encoding="utf-8").replace(
                "</xs:schema>", "  <!-- dependency revision two -->\n</xs:schema>"
            ),
            encoding="utf-8",
        )
        invalidated = Snapshot("benchmark", "small-changed", source, _manifest(source, names))
        before = cache.info().misses
        invalidated_ms = _run(cache, invalidated, "catalog.xsd", "document.xml")
        invalidated_compiles = cache.info().misses - before
        assert (cold_compiles, warm_compiles, invalidated_compiles) == (1, 0, 1)
        return {
            "dataset": "checked-in-small",
            "files": len(names),
            "cold_ms": round(cold_ms, 3),
            "warm_ms": round(warm_ms, 3),
            "dependency_changed_ms": round(invalidated_ms, 3),
            "cold_compiles": cold_compiles,
            "warm_compiles": warm_compiles,
            "dependency_changed_compiles": invalidated_compiles,
            "cache": cache.info().__dict__,
        }
    finally:
        cache.close()


def _musicxml_metrics(limits: Limits) -> dict[str, object]:
    example = ROOT / "examples" / "musicxml"
    schema = example / "schemas" / "musicxml.xsd"
    instance = example / "instance.xml"
    if not schema.is_file() or not instance.is_file():
        return {
            "dataset": "musicxml",
            "available": False,
            "reason": "run examples/musicxml/download_schemas.py to fetch the licensed corpus",
        }
    names = sorted(
        str(path.relative_to(example))
        for path in example.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    snapshot = Snapshot("benchmark", "musicxml", example, _manifest(example, names))
    cache = SchemaCache(limits)
    try:
        before = cache.info().misses
        cold_ms = _run(cache, snapshot, "schemas/musicxml.xsd", "instance.xml")
        cold_compiles = cache.info().misses - before
        before = cache.info().misses
        warm_ms = _run(cache, snapshot, "schemas/musicxml.xsd", "instance.xml")
        warm_compiles = cache.info().misses - before
        assert (cold_compiles, warm_compiles) == (1, 0)
        return {
            "dataset": "musicxml",
            "available": True,
            "license": "public-domain score from the BSD-licensed music21 corpus",
            "files": len(names),
            "cold_ms": round(cold_ms, 3),
            "warm_ms": round(warm_ms, 3),
            "cold_compiles": cold_compiles,
            "warm_compiles": warm_compiles,
            "cache": cache.info().__dict__,
        }
    except WorkspacePolicyError as exc:
        return {
            "dataset": "musicxml",
            "available": True,
            "benchmarked": False,
            "license": "public-domain score from the BSD-licensed music21 corpus",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    finally:
        cache.close()


def main() -> int:
    limits = Limits(workers=1)
    with tempfile.TemporaryDirectory(prefix="pyxsd-mcp-benchmark-") as directory:
        metrics = {
            "python": sys.version.split()[0],
            "pyxsd": __version__,
            "limits": {"workers": limits.workers, "cache_entries": limits.cache_entries},
            "rss_maxrss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "rss_units": "KiB on Linux, bytes on macOS",
            "small": _small_fixture_metrics(Path(directory), limits),
            "musicxml": _musicxml_metrics(limits),
        }
        encoded = json.dumps(metrics, sort_keys=True)
        metrics["serialized_result_bytes"] = len(encoded.encode("utf-8"))
        print(json.dumps(metrics, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
