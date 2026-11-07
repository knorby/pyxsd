"""Descriptor-relative reads never follow client-selected filesystem links."""

import hashlib
import os
import sys

import pytest

pytest.importorskip("pydantic")

from pyxsd.mcp.errors import ToolError


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".",
        "..",
        "../x",
        "x/../y",
        "/x",
        "x//y",
        "x/",
        "./x",
        "C:/x",
        "C:x",
        "\\\\host\\share",
        "x\\y",
        "https://example/x",
        "x\x00y",
        "x\ny",
        "a" * 256,
        "\ud800",
        "x\x7fy",
        None,
        "/".join(["a"] * 2050),
    ],
)
def test_unsafe_logical_paths_are_rejected(path):
    from pyxsd.mcp.safe_io import validate_paths

    with pytest.raises(ToolError, match="invalid_path"):
        validate_paths([path])


@pytest.mark.parametrize(
    "paths",
    [
        ["x", "x"],
        ["X", "x"],
        ["e\u0301", "\u00e9"],
        ["Dir/a", "dir/b"],
        ["x", "x/y"],
        ["x/y", "x"],
    ],
)
def test_normalization_and_case_collisions_are_rejected_without_creating_files(paths):
    from pyxsd.mcp.safe_io import validate_paths

    with pytest.raises(ToolError, match="path_collision"):
        validate_paths(paths)


def test_paths_preserve_spelling_and_allow_distinct_relative_names():
    from pyxsd.mcp.safe_io import validate_paths

    assert validate_paths(["schemas/order.xsd", "data/input.xml"]) == (
        "schemas/order.xsd",
        "data/input.xml",
    )
    assert validate_paths([]) == ()
    assert validate_paths(["data/a", "data/b"]) == ("data/a", "data/b")


def test_capture_hashes_original_bytes_and_verifies_contents(tmp_path):
    from pyxsd.mcp.safe_io import SafeDirectory

    (tmp_path / "nested").mkdir()
    path = tmp_path / "nested" / "input.xml"
    path.write_bytes(b"\xff\x00original")
    with SafeDirectory(tmp_path) as directory:
        captured = directory.read("nested/input.xml", max_bytes=10)
        assert captured.content == b"\xff\x00original"
        assert captured.digest == hashlib.sha256(captured.content).hexdigest()
        directory.verify(captured, max_bytes=10)
        path.write_bytes(b"replacement")
        with pytest.raises(ToolError, match="source_changed"):
            directory.verify(captured, max_bytes=20)
    directory.close()
    with pytest.raises(ToolError, match="closed"):
        directory.read("nested/input.xml", max_bytes=10)


def test_empty_files_and_exact_byte_limit_are_supported(tmp_path):
    from pyxsd.mcp.safe_io import SafeDirectory

    (tmp_path / "empty").touch()
    (tmp_path / "full").write_bytes(b"1234")
    with SafeDirectory(tmp_path) as directory:
        assert directory.read("empty", max_bytes=0).content == b""
        assert directory.read("full", max_bytes=4).content == b"1234"
        with pytest.raises(ToolError, match="quota_exceeded"):
            directory.read("full", max_bytes=3)
        with pytest.raises(FileNotFoundError):
            directory.read("missing", max_bytes=4)


@pytest.mark.parametrize("link_parent", [False, True])
def test_symlink_inputs_are_never_followed(tmp_path, link_parent):
    from pyxsd.mcp.safe_io import SafeDirectory

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret")
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside if link_parent else outside / "secret")
    with SafeDirectory(root) as directory, pytest.raises(ToolError, match="unsafe_path"):
        directory.read("link/secret" if link_parent else "link", max_bytes=100)


def test_root_symlinks_and_nonregular_files_fail_closed(tmp_path):
    from pyxsd.mcp.safe_io import SafeDirectory

    root = tmp_path / "root"
    root.mkdir()
    link = tmp_path / "link"
    link.symlink_to(root)
    with pytest.raises(ToolError, match="unsafe_path"):
        SafeDirectory(link)
    os.mkfifo(root / "pipe")
    with SafeDirectory(root) as directory:
        for path in ("pipe", "."):
            with pytest.raises(ToolError):
                directory.read(path, max_bytes=10)
        (root / "directory").mkdir()
        with pytest.raises(ToolError, match="unsafe_path"):
            directory.read("directory", max_bytes=10)


def test_missing_platform_primitives_fail_closed(tmp_path, monkeypatch):
    from pyxsd.mcp.safe_io import SafeDirectory

    monkeypatch.setattr(os, "supports_dir_fd", set())
    with pytest.raises(ToolError, match="unsupported_platform"):
        SafeDirectory(tmp_path)


@pytest.mark.parametrize("budget", [-1, True, 1.5])
def test_invalid_read_budgets_are_rejected(tmp_path, budget):
    from pyxsd.mcp.safe_io import SafeDirectory

    with SafeDirectory(tmp_path) as directory, pytest.raises(ToolError, match="invalid_argument"):
        directory.read("input", max_bytes=budget)


def test_mutation_during_read_is_detected(tmp_path, monkeypatch):
    from pyxsd.mcp.safe_io import SafeDirectory

    path = tmp_path / "input"
    path.write_bytes(b"before")
    original = os.read

    def changing_read(fd, count):
        data = original(fd, count)
        if data:
            path.write_bytes(b"after!")
        return data

    monkeypatch.setattr(os, "read", changing_read)
    with SafeDirectory(tmp_path) as directory, pytest.raises(ToolError, match="source_changed"):
        directory.read("input", max_bytes=10)


def test_deletion_during_verification_is_a_source_change(tmp_path):
    from pyxsd.mcp.safe_io import SafeDirectory

    path = tmp_path / "input"
    path.write_bytes(b"original")
    with SafeDirectory(tmp_path) as directory:
        captured = directory.read("input", max_bytes=10)
        path.unlink()
        with pytest.raises(ToolError, match="source_changed"):
            directory.verify(captured, max_bytes=10)


def test_parent_link_swap_cannot_redirect_an_open_descriptor(tmp_path, monkeypatch):
    from pyxsd.mcp.safe_io import SafeDirectory

    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "input").write_bytes(b"admitted")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "input").write_bytes(b"private")
    with SafeDirectory(tmp_path) as directory:
        original = os.open

        def swapping_open(path, flags, **kwargs):
            fd = original(path, flags, **kwargs)
            if path == "nested":
                (tmp_path / "nested").rename(tmp_path / "previous")
                (tmp_path / "nested").symlink_to(outside)
            return fd

        monkeypatch.setattr(os, "open", swapping_open)
        captured = directory.read("nested/input", max_bytes=10)
        assert captured.content == b"admitted"
        with pytest.raises(ToolError, match="unsafe_path"):
            directory.verify(captured, max_bytes=10)


def test_growth_cannot_exceed_the_budget_during_read(tmp_path, monkeypatch):
    from pyxsd.mcp.safe_io import SafeDirectory

    path = tmp_path / "input"
    path.write_bytes(b"1234")
    original = os.read

    def growing_read(fd, count):
        path.write_bytes(b"12345")
        return original(fd, count)

    with SafeDirectory(tmp_path) as directory:
        monkeypatch.setattr(os, "read", growing_read)
        with pytest.raises(ToolError, match="quota_exceeded"):
            directory.read("input", max_bytes=4)


def test_inconsistent_regular_file_size_is_rejected(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from pyxsd.mcp.safe_io import SafeDirectory

    (tmp_path / "input").write_bytes(b"1234")
    original = os.fstat

    def reported_size(fd):
        info = original(fd)
        return SimpleNamespace(
            st_mode=info.st_mode,
            st_dev=info.st_dev,
            st_ino=info.st_ino,
            st_size=info.st_size + 1,
            st_mtime_ns=info.st_mtime_ns,
            st_ctime_ns=info.st_ctime_ns,
        )

    with SafeDirectory(tmp_path) as directory:
        monkeypatch.setattr(os, "fstat", reported_size)
        with pytest.raises(ToolError, match="source_changed"):
            directory.read("input", max_bytes=10)


def test_filesystem_encoding_errors_are_typed_denials(tmp_path, monkeypatch):
    from pyxsd.mcp.safe_io import SafeDirectory

    def ascii_only_open(*args, **kwargs):
        raise UnicodeEncodeError("ascii", "\u00e9", 0, 1, "not encodable")

    with SafeDirectory(tmp_path) as directory:
        monkeypatch.setattr(os, "open", ascii_only_open)
        with pytest.raises(ToolError, match="unsafe_path"):
            directory.read("input", max_bytes=10)


def test_admitted_path_depth_is_bounded_before_recursive_storage_operations():
    from pyxsd.mcp.errors import ToolError
    from pyxsd.mcp.safe_io import validate_paths

    accepted = "/".join(["a"] * 128)
    assert validate_paths([accepted]) == (accepted,)
    with pytest.raises(ToolError, match="invalid_path"):
        validate_paths([accepted + "/a"])


def test_glob_enumeration_is_deterministic_and_prunes_the_excluded_state(tmp_path):
    from pyxsd.mcp.safe_io import SafeDirectory

    (tmp_path / "corpus").mkdir()
    (tmp_path / "corpus" / "nested").mkdir()
    (tmp_path / "state" / "nested").mkdir(parents=True)
    (tmp_path / "corpus" / "a.xml").write_text("a")
    (tmp_path / "corpus" / "nested" / "b.xml").write_text("b")
    (tmp_path / "state" / "nested" / "secret.xml").write_text("secret")
    (tmp_path / "ignored.txt").write_text("ignored")
    with SafeDirectory(tmp_path) as directory:
        first = directory.glob("**/*.xml", max_entries=6, max_matches=10, exclude="state")
        second = directory.glob("**/*.xml", max_entries=6, max_matches=10, exclude="state")
    assert first == second == ("corpus/a.xml", "corpus/nested/b.xml")


def test_glob_results_are_lexical_across_directories(tmp_path):
    from pyxsd.mcp.corpus import select_manifest_paths
    from pyxsd.mcp.safe_io import SafeDirectory

    (tmp_path / "b").mkdir()
    (tmp_path / "aa.xml").write_text("aa")
    (tmp_path / "b.xml").write_text("b")
    (tmp_path / "b" / "a.xml").write_text("nested")
    with SafeDirectory(tmp_path) as directory:
        selected = directory.glob("**/*.xml", max_entries=10, max_matches=10)
    lexical = select_manifest_paths("**/*.xml", selected, max_matches=10)
    assert selected == lexical == ("aa.xml", "b.xml", "b/a.xml")


def test_glob_without_matches_returns_an_empty_tuple(tmp_path):
    from pyxsd.mcp.safe_io import SafeDirectory

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "input.txt").write_text("text")
    with SafeDirectory(tmp_path) as directory:
        assert directory.glob("**/*.xml", max_entries=10, max_matches=10) == ()


@pytest.mark.parametrize("budget", [-1, 0, True, 1.5, "2"])
def test_glob_requires_positive_integer_entry_and_match_budgets(tmp_path, budget):
    from pyxsd.mcp.safe_io import SafeDirectory

    (tmp_path / "a.xml").write_text("a")
    with SafeDirectory(tmp_path) as directory:
        with pytest.raises(ToolError, match="invalid_argument"):
            directory.glob("*.xml", max_entries=budget, max_matches=10)
        with pytest.raises(ToolError, match="invalid_argument"):
            directory.glob("*.xml", max_entries=10, max_matches=budget)


def test_glob_entry_exhaustion_fails_instead_of_truncating(tmp_path):
    from pyxsd.mcp.safe_io import SafeDirectory

    for name in ("a.xml", "b.xml", "c.xml"):
        (tmp_path / name).write_text(name)
    with SafeDirectory(tmp_path) as directory, pytest.raises(ToolError, match="quota_exceeded"):
        directory.glob("*.xml", max_entries=2, max_matches=10)


def test_glob_match_overflow_fails_instead_of_returning_a_prefix(tmp_path):
    from pyxsd.mcp.safe_io import SafeDirectory

    (tmp_path / "a.xml").write_text("a")
    (tmp_path / "b.xml").write_text("b")
    with SafeDirectory(tmp_path) as directory, pytest.raises(ToolError, match="quota_exceeded"):
        directory.glob("*.xml", max_entries=10, max_matches=1)


def test_glob_rejects_matched_nonregular_entries(tmp_path):
    from pyxsd.mcp.safe_io import SafeDirectory

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("secret")
    os.mkfifo(tmp_path / "pipe.xml")
    (tmp_path / "link.xml").symlink_to(outside / "secret")
    with SafeDirectory(tmp_path) as directory:
        for pattern in ("pipe.xml", "link.xml", "*.xml"):
            with pytest.raises(ToolError, match="unsafe_path"):
                directory.glob(pattern, max_entries=10, max_matches=10)


def test_glob_never_follows_directory_symlinks(tmp_path):
    from pyxsd.mcp.safe_io import SafeDirectory

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.xml").write_text("secret")
    (root / "link").symlink_to(outside)
    with SafeDirectory(root) as directory:
        assert directory.glob("link/**/*.xml", max_entries=10, max_matches=10) == ()
        assert directory.glob("**/*.xml", max_entries=10, max_matches=10) == ()


def test_glob_after_close_fails_closed(tmp_path):
    from pyxsd.mcp.safe_io import SafeDirectory

    (tmp_path / "a.xml").write_text("a")
    directory = SafeDirectory(tmp_path)
    directory.close()
    with pytest.raises(ToolError, match="closed"):
        directory.glob("*.xml", max_entries=1, max_matches=1)


def test_glob_ignores_an_unmatchable_deep_chain_without_recursing_on_the_stack(tmp_path):
    from pyxsd.mcp.safe_io import SafeDirectory

    deep = tmp_path
    for _ in range(300):
        deep = deep / "d"
        deep.mkdir()
    recursion_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(200)
    try:
        with SafeDirectory(tmp_path) as directory:
            assert directory.glob("shallow/*.xml", max_entries=500, max_matches=10) == ()
    finally:
        sys.setrecursionlimit(recursion_limit)


def test_glob_rejects_a_matched_directory(tmp_path):
    from pyxsd.mcp.safe_io import SafeDirectory

    (tmp_path / "corpus").mkdir()
    with SafeDirectory(tmp_path) as directory, pytest.raises(ToolError, match="unsafe_path"):
        directory.glob("corpus", max_entries=10, max_matches=10)


def test_glob_reports_a_root_enumeration_failure(tmp_path, monkeypatch):
    from pyxsd.mcp.safe_io import SafeDirectory

    (tmp_path / "a.xml").write_text("a")

    def fail(_directory_fd):
        raise OSError("unreadable")

    with SafeDirectory(tmp_path) as directory:
        monkeypatch.setattr(os, "scandir", fail)
        with pytest.raises(ToolError, match="unsafe_path"):
            directory.glob("*.xml", max_entries=10, max_matches=10)


def test_glob_reports_an_entry_inspection_failure(tmp_path, monkeypatch):
    from pyxsd.mcp.safe_io import SafeDirectory

    (tmp_path / "a.xml").write_text("a")
    with SafeDirectory(tmp_path) as directory:
        monkeypatch.setattr(
            os.DirEntry,
            "stat",
            lambda _self, *, follow_symlinks: (_ for _ in ()).throw(OSError("vanished")),
        )
        with pytest.raises(ToolError, match="unsafe_path"):
            directory.glob("*.xml", max_entries=10, max_matches=10)


def test_glob_reports_a_child_directory_open_failure(tmp_path, monkeypatch):
    from pyxsd.mcp.safe_io import SafeDirectory

    (tmp_path / "corpus").mkdir()
    (tmp_path / "corpus" / "a.xml").write_text("a")
    with SafeDirectory(tmp_path) as directory:
        monkeypatch.setattr(
            os,
            "open",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("permission denied")),
        )
        with pytest.raises(ToolError, match="unsafe_path"):
            directory.glob("corpus/*.xml", max_entries=10, max_matches=10)


def test_glob_reports_a_child_enumeration_failure_and_closes_the_child(tmp_path, monkeypatch):
    from pyxsd.mcp.safe_io import SafeDirectory

    (tmp_path / "corpus").mkdir()
    (tmp_path / "corpus" / "a.xml").write_text("a")
    original_scandir = os.scandir
    calls = 0

    def flaky(directory_fd):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_scandir(directory_fd)
        raise OSError("unreadable")

    with SafeDirectory(tmp_path) as directory:
        monkeypatch.setattr(os, "scandir", flaky)
        with pytest.raises(ToolError, match="unsafe_path"):
            directory.glob("corpus/*.xml", max_entries=10, max_matches=10)


def test_glob_reports_a_descriptor_close_failure(tmp_path, monkeypatch):
    from pyxsd.mcp.safe_io import SafeDirectory

    (tmp_path / "a.xml").write_text("a")
    original_close = os.close

    def fail_once(descriptor):
        original_close(descriptor)
        raise OSError("closed")

    with SafeDirectory(tmp_path) as directory, monkeypatch.context() as patch:
        patch.setattr(os, "close", fail_once)
        with pytest.raises(ToolError, match="unsafe_path"):
            directory.glob("*.xml", max_entries=10, max_matches=10)
