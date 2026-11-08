"""Corpus globs stay portable, deterministic, and bounded."""

import subprocess
import sys

import pytest

pytest.importorskip("pydantic")

from pyxsd.mcp.errors import ToolError


def test_glob_matches_within_segments_and_across_descendant_directories():
    from pyxsd.mcp.corpus import CorpusGlob

    pattern = CorpusGlob("corpus/**/*.xml")
    assert pattern.matches("corpus/a.xml")
    assert pattern.matches("corpus/nested/b.xml")
    assert pattern.matches("corpus/nested/deep/c.xml")
    assert not pattern.matches("corpus/a.txt")
    assert not pattern.matches("a.xml")
    assert not pattern.matches("other/a.xml")


def test_glob_character_classes_apply_to_one_character():
    from pyxsd.mcp.corpus import CorpusGlob

    pattern = CorpusGlob("data/[a-c]?.xml")
    assert pattern.matches("data/a1.xml")
    assert pattern.matches("data/c2.xml")
    assert not pattern.matches("data/d1.xml")
    assert not pattern.matches("data/a12.xml")
    assert not pattern.matches("data/a1.json")


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [("*a*b", "xxaYYb", True), ("*a*b", "xxaYY", False)],
)
def test_glob_stars_preserve_literal_matching(pattern, path, expected):
    from pyxsd.mcp.corpus import CorpusGlob

    assert CorpusGlob(pattern).matches(path) is expected


def test_glob_matching_avoids_exponential_backtracking():
    script = (
        "from pyxsd.mcp.corpus import CorpusGlob; "
        "assert not CorpusGlob('*' + 'a*' * 12 + 'b').matches('a' * 34)"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=5
        )
    except subprocess.TimeoutExpired:
        pytest.fail("Corpus glob matching exceeded five seconds")
    assert completed.returncode == 0, completed.stderr


def test_manifest_selection_is_lexical_and_overflow_fails_closed():
    from pyxsd.mcp.corpus import select_manifest_paths

    paths = ("z.xml", "a/b.xml", "a.xml", "a.txt")
    assert select_manifest_paths("**/*.xml", paths, max_matches=3) == (
        "a.xml",
        "a/b.xml",
        "z.xml",
    )
    with pytest.raises(ToolError, match="quota_exceeded"):
        select_manifest_paths("**/*.xml", paths, max_matches=2)


def test_manifest_selection_rejects_unsafe_candidate_paths():
    from pyxsd.mcp.corpus import select_manifest_paths

    with pytest.raises(ToolError, match="invalid_path"):
        select_manifest_paths("**/*.xml", ("../outside.xml",), max_matches=2)


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        ".",
        "..",
        "corpus/",
        "corpus//a.xml",
        "/corpus/a.xml",
        "./corpus/a.xml",
        "corpus/../a.xml",
        "C:/corpus/a.xml",
        "corpus\\a.xml",
        "corpus/a\x00.xml",
        "corux/a\n.xml",
        "corpus/\ud800.xml",
    ],
)
def test_unsafe_glob_spellings_are_invalid_paths(pattern):
    from pyxsd.mcp.corpus import CorpusGlob

    with pytest.raises(ToolError, match="invalid_path"):
        CorpusGlob(pattern)


@pytest.mark.parametrize(
    "pattern", ["corpus/[a-b/*.xml", "corpus/**extra/*.xml", "corpus/[z-a].xml", "corpus/[!].xml"]
)
def test_malformed_glob_patterns_are_invalid_arguments(pattern):
    from pyxsd.mcp.corpus import CorpusGlob

    with pytest.raises(ToolError, match="invalid_argument"):
        CorpusGlob(pattern)


@pytest.mark.parametrize("budget", [0, -1, True, 1.5, "2"])
def test_manifest_selection_requires_a_positive_integer_budget(budget):
    from pyxsd.mcp.corpus import select_manifest_paths

    with pytest.raises(ToolError, match="invalid_argument"):
        select_manifest_paths("**/*.xml", ("a.xml",), max_matches=budget)
