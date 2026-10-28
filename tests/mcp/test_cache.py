"""Worker-local reuse must preserve freshness, serialization and object lifetimes."""

import gc
import hashlib
import io
import os
import threading
import weakref
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("defusedxml")

from pyxsd import Schema, __version__
from pyxsd.introspection import describe_schema
from pyxsd.mcp import contracts
from pyxsd.mcp.captures import CaptureFile, InputCapture
from pyxsd.mcp.config import Limits
from pyxsd.mcp.errors import ToolError
from pyxsd.schema_context import last_components

XSD = '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"><xs:element name="root" type="xs:int"/></xs:schema>'


def request(tmp_path: Path, **kwargs):
    from pyxsd.mcp.cache import CompileRequest

    files = kwargs.pop("files", {"root.xsd": XSD})
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    manifest = tuple(
        CaptureFile(name, hashlib.sha256(text.encode()).hexdigest(), len(text.encode()))
        for name, text in sorted(files.items())
    )
    return CompileRequest(
        capture=InputCapture("workspace", 1, tmp_path, manifest),
        schema_ref=contracts.SchemaRef(source=contracts.FileRef(path="root.xsd")),
        hints=(),
        processor_version=__version__,
        **kwargs,
    )


def test_reuses_one_compile_and_restores_legacy_components(tmp_path, monkeypatch):
    from pyxsd.mcp.cache import SchemaCache

    prior = Schema.compile(io.StringIO(XSD))
    remembered = last_components()
    calls = []
    original = Schema.compile

    def compile(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(Schema, "compile", compile)
    cache = SchemaCache(Limits())
    req = request(tmp_path)
    with cache.acquire(req) as first:
        assert first.parse(io.StringIO("<root>3</root>")).root == 3
    with cache.acquire(req) as second:
        assert second is first
    assert len(calls) == 1
    assert last_components() is remembered is prior.components
    assert cache.info().hits == cache.info().misses == 1
    cache.close()


def test_cache_applies_collection_and_message_budgets(tmp_path):
    from pyxsd.mcp.cache import SchemaCache

    schema = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:element name="r"><xs:complexType><xs:sequence>
        <xs:element name="i" type="xs:int" maxOccurs="unbounded"/>
      </xs:sequence></xs:complexType></xs:element></xs:schema>"""
    req = request(
        tmp_path,
        files={"root.xsd": schema, "d.xml": "<r><i>bad</i><i>bad</i><i>bad</i></r>"},
    )
    cache = SchemaCache(Limits(collected_issues=1, message_bytes=16))
    try:
        with cache.acquire(req) as compiled:
            report = compiled.parse("/workspace/d.xml").report
        assert len(report.issues) == 1
        assert report.error_count == 3
        assert report.suppressed_count == 2
        assert 0 < len(report.issues[0].message.encode("utf-8")) <= 16
    finally:
        cache.close()


@pytest.mark.parametrize("field", ["version", "binding", "namespaces", "xsd", "hints", "mapping"])
def test_every_compile_input_separates_keys(tmp_path, field):
    from pyxsd.mcp.cache import SchemaCache

    req = request(tmp_path)
    changed = {
        "version": lambda: replace(req, processor_version="different"),
        "binding": lambda: replace(
            req,
            schema_ref=req.schema_ref.model_copy(
                update={"options": contracts.CompileOptions(binding="strict")}
            ),
        ),
        "namespaces": lambda: replace(
            req,
            schema_ref=req.schema_ref.model_copy(
                update={"options": contracts.CompileOptions(namespaces="legacy")}
            ),
        ),
        "xsd": lambda: replace(
            req,
            schema_ref=req.schema_ref.model_copy(
                update={"options": contracts.CompileOptions(xsd_version="1.0")}
            ),
        ),
        "hints": lambda: replace(req, hints=(("urn:absent", "missing.xsd"),)),
        "mapping": lambda: replace(
            req,
            schema_ref=req.schema_ref.model_copy(
                update={
                    "options": contracts.CompileOptions(
                        namespace_schemas={"urn:absent": contracts.FileRef(path="missing.xsd")}
                    )
                }
            ),
        ),
    }[field]()
    cache = SchemaCache(Limits())
    with cache.acquire(req) as first, cache.acquire(changed) as second:
        assert second is not first
    assert cache.info().misses == 2
    cache.close()


def test_changed_same_stat_dependency_and_missing_appearance(tmp_path):
    from pyxsd.mcp.cache import SchemaCache

    root = XSD.replace("<xs:element", '<xs:include schemaLocation="dep.xsd"/><xs:element')
    req = request(tmp_path, files={"root.xsd": root})
    cache = SchemaCache(Limits())
    with cache.acquire(req) as absent:
        assert absent.report.warnings
    with cache.acquire(req) as still_absent:
        assert still_absent is absent
    dependency = XSD.replace(
        '<xs:element name="root" type="xs:int"/>',
        '<xs:simpleType name="Number"><xs:restriction base="xs:int"><xs:minInclusive value="1"/></xs:restriction></xs:simpleType>',
    )
    present = request(tmp_path, files={"root.xsd": root, "dep.xsd": dependency})
    with cache.acquire(present) as before:
        assert not before.report.has_errors
    stat = (tmp_path / "dep.xsd").stat()
    updated = request(
        tmp_path, files={"root.xsd": root, "dep.xsd": dependency.replace('value="1"', 'value="2"')}
    )
    os.utime(tmp_path / "dep.xsd", ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with cache.acquire(updated) as after:
        assert after is not before is not absent
    assert cache.info().active == 0
    assert cache.info().misses == 3
    cache.close()


def test_reentrant_failure_keeps_outer_pin_until_close(tmp_path):
    from pyxsd.mcp.cache import SchemaCache

    cache = SchemaCache(Limits())
    req = request(tmp_path)
    with cache.acquire(req) as schema:
        with pytest.raises(ToolError, match="cache_busy"), cache.acquire(req):
            pytest.fail("Reentrant access cannot share mutable parser state")
        assert cache.info().active == 1
        cache.close()
        assert cache.info().entries == 1
        assert schema.parse(io.StringIO("<root>9</root>")).root == 9
    assert cache.info().entries == cache.info().active == 0


def test_different_schemas_can_operate_in_parallel(tmp_path):
    from pyxsd.mcp.cache import SchemaCache

    cache = SchemaCache(Limits())
    request(tmp_path)
    barrier = threading.Barrier(2, timeout=5)
    errors = []

    def worker(directory, schema_text):
        try:
            directory.mkdir()
            with cache.acquire(
                request(
                    directory,
                    files={"root.xsd": schema_text},
                )
            ) as schema:
                barrier.wait()
                assert schema.parse(io.StringIO("<root>7</root>")).root == 7
        except BaseException as exc:
            errors.append(exc)
            barrier.abort()

    threads = [
        threading.Thread(target=worker, args=(tmp_path / str(index), schema_text))
        for index, schema_text in enumerate((XSD, XSD.replace("xs:int", "xs:long")))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert not errors
    cache.close()


def test_compile_failure_does_not_retain_entry_or_compatibility_state(tmp_path):
    from pyxsd.exceptions import PyXSDError
    from pyxsd.mcp.cache import SchemaCache

    remembered = last_components()
    cache = SchemaCache(Limits())
    req = request(tmp_path, files={"root.xsd": "<broken>"})
    with pytest.raises(PyXSDError), cache.acquire(req):
        pytest.fail("Malformed schema cannot produce a lease")
    assert cache.info().entries == cache.info().active == 0
    assert last_components() is remembered
    cache.close()


def test_capture_root_and_generation_do_not_change_cache_identity(tmp_path):
    from pyxsd.mcp.cache import SchemaCache

    cache = SchemaCache(Limits())
    first = request(tmp_path)
    other_root = tmp_path / "other"
    other_root.mkdir()
    (other_root / "root.xsd").write_text(XSD, encoding="utf-8")
    second = replace(
        first,
        capture=InputCapture(
            "different-workspace",
            99,
            other_root,
            (CaptureFile("root.xsd", first.capture.files[0].digest, len(XSD.encode())),),
        ),
    )
    with cache.acquire(first) as original:
        assert original.parse(io.StringIO("<root>3</root>")).root == 3
    (first.capture.root / "root.xsd").unlink()
    with cache.acquire(second) as reused:
        assert reused is original
        assert reused.parse(io.StringIO("<root>4</root>")).root == 4
    assert cache.info().hits == 1
    cache.close()


def test_lru_idle_pins_and_derived_lifetime(tmp_path):
    from pyxsd.mcp.cache import SchemaCache

    now = [0.0]
    cache = SchemaCache(Limits(cache_entries=1, cache_idle_seconds=2), clock=lambda: now[0])
    req = request(tmp_path)
    with cache.acquire(req) as schema:
        describe_schema(schema)
        schema_ref, derived_ref = weakref.ref(schema), weakref.ref(schema._introspection_index)
        with (
            pytest.raises(ToolError, match="cache_busy"),
            cache.acquire(replace(req, processor_version="other")),
        ):
            pytest.fail("Pinned entries cannot be evicted")
    del schema
    now[0] = 2
    cache.sweep()
    gc.collect()
    assert schema_ref() is derived_ref() is None
    assert cache.info().entries == 0
    with cache.acquire(req) as first:
        pass
    with cache.acquire(replace(req, processor_version="other")) as second:
        assert second is not first
    assert cache.info().evictions == 2
    cache.close()


def test_disabled_cache_exception_and_closed_leases(tmp_path):
    from pyxsd.mcp.cache import SchemaCache

    cache = SchemaCache(Limits(), enabled=False)
    req = request(tmp_path)
    with pytest.raises(RuntimeError, match="operation"), cache.acquire(req) as first:
        raise RuntimeError("operation")
    with cache.acquire(req) as second:
        assert second is not first
        cache.close()
        assert second.parse(io.StringIO("<root>4</root>")).root == 4
    assert cache.info().entries == cache.info().active == 0
    assert cache.info().bypasses == 2
    with pytest.raises(ToolError, match="cache_closed"), cache.acquire(req):
        pytest.fail("Closed cache cannot acquire")


def test_singleflight_and_serial_operations(tmp_path):
    from pyxsd.mcp.cache import SchemaCache

    cache = SchemaCache(Limits())
    req = request(tmp_path)
    barrier = threading.Barrier(4, timeout=5)
    seen, errors = [], []
    active = 0
    guard = threading.Lock()

    def worker():
        nonlocal active
        try:
            barrier.wait()
            with cache.acquire(req) as schema:
                with guard:
                    active += 1
                    assert active == 1
                assert schema.parse(io.StringIO("<root>8</root>")).root == 8
                seen.append(schema)
                with guard:
                    active -= 1
        except BaseException as exc:
            errors.append(exc)
            barrier.abort()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert not errors
    assert len(seen) == 4 and all(item is seen[0] for item in seen)
    assert cache.info().misses == 1
    cache.close()


def test_recent_hit_controls_eviction_and_request_owns_options(tmp_path):
    from pyxsd.mcp.cache import SchemaCache

    cache = SchemaCache(Limits(cache_entries=2))
    first_req = request(tmp_path)
    second_req = replace(first_req, processor_version="two")
    third_req = replace(first_req, processor_version="three")
    with cache.acquire(first_req) as first, cache.acquire(second_req) as second:
        pass
    with cache.acquire(first_req) as hit:
        assert hit is first
    with cache.acquire(third_req):
        pass
    with cache.acquire(first_req) as hit:
        assert hit is first
    with cache.acquire(second_req) as new_second:
        assert new_second is not second
    assert cache.info().evictions == 2
    options = contracts.CompileOptions()
    original = contracts.SchemaRef(source=contracts.FileRef(path="root.xsd"), options=options)
    owned = replace(first_req, schema_ref=original)
    options.namespace_schemas["urn:later"] = contracts.FileRef(path="missing.xsd")
    assert not owned.schema_ref.options.namespace_schemas
    with cache.acquire(owned) as schema:
        assert not schema.report.has_errors
    cache.close()


def test_repeated_parses_release_documents_and_cache_off_is_bounded(tmp_path):
    from pyxsd.mcp.cache import SchemaCache

    cache = SchemaCache(Limits(cache_entries=1), enabled=False)
    req = request(tmp_path)
    references = []
    with cache.acquire(req) as schema:
        for _ in range(100):
            document = schema.parse(io.StringIO("<root>5</root>"))
            references.append(weakref.ref(document))
        del document
        gc.collect()
        assert all(reference() is None for reference in references)
        assert schema._host._elementParents == schema._host._elementTypes == {}
        with pytest.raises(ToolError, match="cache_busy"), cache.acquire(req):
            pytest.fail("Bypass compiles still consume active capacity")
    assert cache.info().active == 0
    cache.close()


def test_non_source_policy_failure_during_hit_verification_is_not_hidden(tmp_path):
    from pyxsd.mcp.cache import SchemaCache
    from pyxsd.mcp.captures import CaptureDependency, InputPolicyError

    cache = SchemaCache(Limits())
    req = request(tmp_path)
    try:
        with cache.acquire(req):
            pass
        key = next(iter(cache._entries))
        cache._entries[key].policy._dependencies["../escape.xsd"] = CaptureDependency(
            "../escape.xsd", None, 0
        )
        with pytest.raises(InputPolicyError, match="invalid_path"), cache.acquire(req):
            pass
    finally:
        cache.close()


def test_changed_capture_dependency_evicts_the_cached_schema(tmp_path):
    from pyxsd.mcp.cache import SchemaCache
    from pyxsd.mcp.captures import InputPolicyError

    cache = SchemaCache(Limits())
    req = request(tmp_path)
    try:
        with cache.acquire(req):
            pass
        (tmp_path / "root.xsd").write_text(XSD + " ")
        with pytest.raises(InputPolicyError, match="source_changed"), cache.acquire(req):
            pass
        assert cache.info().stale == 1
        assert cache.info().entries == 0
    finally:
        cache.close()
