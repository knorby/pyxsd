"""The process-scoped memo over elementpath's regex translation.

elementpath re-translates a regex source pattern on every ``fn:matches``,
``fn:replace``, ``fn:tokenize``, and ``fn:analyze-string`` call, so one sweep
that evaluates the same pattern a million times pays the translation a million
times.  ``translate_pattern`` is a pure function of its six arguments and the
installed Unicode data, so the translation is memoised once per process at the
XPath-engine import seam.

These tests pin the structural install (every already-loaded elementpath
binding is patched), the cache key (all six public arguments plus the Unicode
version), the uncached error path, the idempotent install, the bound, the real
``fn:matches`` seam, and the untouched XSD pattern path.  The cache is
process-global, so counter assertions are deltas around the call under test,
never absolute values.
"""

from __future__ import annotations

import inspect
import re
import sys
from collections.abc import Iterator

import elementpath.regex
import elementpath.regex.patterns
import pytest
from elementpath.regex import RegexError

from pyxsd.facets import compile_pattern
from pyxsd.xdm import XdmAtomic

# Importing the package runs the engine module, which installs the memo.
from pyxsd.xpath import regex_cache
from pyxsd.xpath.engine import XPathEngine
from pyxsd.xpath.environment import ExpressionEnvironment, XPathStaticContext


def _counts() -> tuple[int, int]:
    """The current ``(hits, misses)`` pair, read as a delta baseline."""
    info = regex_cache.translation_cache_info()
    return info.hits, info.misses


@pytest.fixture(autouse=True)
def _isolated_translation_cache() -> Iterator[None]:
    """Start and end every test from an empty cache and zeroed counters."""
    regex_cache.clear_translation_cache()
    yield
    regex_cache.clear_translation_cache()


# ---------------------------------------------------------------------------
# §7.1 — the structural seam


def test_install_patches_every_loaded_elementpath_binding() -> None:
    """Every loaded elementpath module that bound the translator holds the wrapper.

    A module that keeps the original is a silent cache miss in production, so
    the installer patches the definition module and then sweeps every module
    imported before it.  This fails if the installer only rebinds the
    ``elementpath`` package re-export, which the hot ``fn:matches`` call site
    does not read.
    """
    wrapper = regex_cache.translate_pattern
    assert elementpath.regex.patterns.translate_pattern is wrapper

    bound = [
        module.translate_pattern
        for name, module in sys.modules.items()
        if (name == "elementpath" or name.startswith("elementpath."))
        and hasattr(module, "translate_pattern")
    ]
    assert bound
    assert set(bound) == {wrapper}
    # The measured hot path is one of the swept modules, not a future import.
    assert elementpath.xpath2._xpath2_functions.translate_pattern is wrapper

    # The wrapper is a faithful stand-in: callers see elementpath's function.
    assert wrapper.__name__ == "translate_pattern"
    assert wrapper.__wrapped__ is regex_cache._ORIGINAL
    assert inspect.signature(wrapper) == inspect.signature(regex_cache._ORIGINAL)
    wrapper_first: tuple[str, ...] = tuple(inspect.signature(wrapper).parameters)
    assert wrapper_first == (
        "pattern",
        "flags",
        "xsd_version",
        "back_references",
        "lazy_quantifiers",
        "anchors",
    )


# ---------------------------------------------------------------------------
# §7.2 — the happy path


def test_repeated_translation_is_served_from_the_cache() -> None:
    """A second translation of the same arguments is one hit and no miss.

    Keyword and positional calls normalise to one content-addressed key, so a
    caller (``facets``) that uses keywords shares the entry elementpath's
    positional calls created.
    """
    regex_cache.clear_translation_cache()
    before = _counts()

    first = regex_cache.translate_pattern("[a-z]+")
    after_first = _counts()
    second = regex_cache.translate_pattern("[a-z]+")
    after_second = _counts()

    assert first == second == "[a-z]+"
    assert after_first == (before[0], before[1] + 1)
    assert after_second == (after_first[0] + 1, after_first[1])

    keyword = regex_cache.translate_pattern(
        pattern="[a-z]+",
        flags=0,
        xsd_version="1.0",
        back_references=True,
        lazy_quantifiers=True,
        anchors=True,
    )
    assert keyword == first
    assert _counts() == (after_second[0] + 1, after_second[1])


# ---------------------------------------------------------------------------
# §7.3 — key completeness


@pytest.mark.parametrize(
    ("kwargs", "variation"),
    [
        pytest.param({"pattern": "abc"}, {"pattern": "abd"}, id="pattern"),
        pytest.param({"pattern": "abc"}, {"flags": re.IGNORECASE}, id="flags"),
        pytest.param({"pattern": "abc"}, {"xsd_version": "1.1"}, id="xsd_version"),
        pytest.param({"pattern": "abc"}, {"back_references": False}, id="back_references"),
        pytest.param({"pattern": "abc"}, {"lazy_quantifiers": False}, id="lazy_quantifiers"),
        pytest.param({"pattern": "abc"}, {"anchors": False}, id="anchors"),
    ],
)
def test_every_argument_participates_in_the_key(
    kwargs: dict[str, object],
    variation: dict[str, object],
) -> None:
    """Varying any one public argument must miss instead of reusing an entry.

    Dropping an argument from the key turns exactly the corresponding
    sub-case's second call into a hit, so a wrong translation would be
    served for a different option set.
    """
    regex_cache.clear_translation_cache()
    before = _counts()

    regex_cache.translate_pattern(**kwargs)
    regex_cache.translate_pattern(**{**kwargs, **variation})
    assert _counts() == (before[0], before[1] + 2)

    # The base entry is still cached: varying one argument did not evict it.
    regex_cache.translate_pattern(**kwargs)
    assert _counts() == (before[0] + 1, before[1] + 2)


# ---------------------------------------------------------------------------
# §7.4 — the Unicode data axis


def test_installed_unicode_version_participates_in_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two installed Unicode versions cannot share one translated entry.

    ``UnicodeSubset.__str__`` reads the installed data, so a version switch
    must re-translate even for identical arguments.
    """
    before = _counts()

    monkeypatch.setattr(regex_cache, "_unicode_version", lambda: "14.0.0")
    first = regex_cache.translate_pattern("\\p{L}")
    monkeypatch.setattr(regex_cache, "_unicode_version", lambda: "15.0.0")
    second = regex_cache.translate_pattern("\\p{L}")

    assert first == second
    assert _counts() == (before[0], before[1] + 2)


# ---------------------------------------------------------------------------
# §7.5 — errors are not cached


def test_invalid_pattern_is_not_cached() -> None:
    """An invalid pattern raises on every call and never occupies an entry.

    ``lru_cache`` does not store exceptions, so the class, message, and
    ``__cause__`` chain are unchanged; the wrapper does add its own frames to
    the traceback.  A cached exception would change the chain, not just frames.
    """
    before = _counts()

    for _ in range(2):
        with pytest.raises(RegexError, match="unexpected meta character"):
            regex_cache.translate_pattern("a**b")

    info = regex_cache.translation_cache_info()
    assert (info.hits, info.misses) == (before[0], before[1] + 2)
    assert info.currsize == 0


# ---------------------------------------------------------------------------
# §7.6 — one install per process


def test_install_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second install changes nothing and reports that it did nothing.

    Importing ``pyxsd.xpath`` already installed the memo, so the guard is
    cleared to exercise the first-call path as a fresh process would; the
    second call must then decline without rewrapping or evicting an entry.
    """
    wrapper = regex_cache.translate_pattern
    regex_cache.translate_pattern("abc")
    before = _counts()

    monkeypatch.setattr(regex_cache, "_INSTALLED", False)
    assert regex_cache.install_regex_translation_cache() is True
    assert regex_cache.install_regex_translation_cache() is False

    assert regex_cache.translate_pattern is wrapper
    assert elementpath.regex.patterns.translate_pattern is wrapper
    regex_cache.translate_pattern("abc")
    assert _counts() == (before[0] + 1, before[1])


# ---------------------------------------------------------------------------
# §7.7 — the bound


def test_cache_is_bounded_and_still_correct() -> None:
    """The memo keeps its 1,024-entry ceiling and stays correct past it."""
    assert regex_cache._translate_cached.cache_parameters()["maxsize"] == (
        regex_cache._CACHE_MAXSIZE
    )

    patterns = [f"a{index}" for index in range(regex_cache._CACHE_MAXSIZE + 76)]
    assert [regex_cache.translate_pattern(pattern) for pattern in patterns] == patterns
    assert regex_cache.translation_cache_info().currsize == regex_cache._CACHE_MAXSIZE

    # The oldest entries were evicted, so recomputing one is a miss and correct.
    before = _counts()
    assert regex_cache.translate_pattern("a0") == "a0"
    assert _counts() == (before[0], before[1] + 1)


# ---------------------------------------------------------------------------
# §7.8 — the real seam


def test_xpath_matches_evaluation_uses_the_cache() -> None:
    """``fn:matches`` evaluated twice translates once, through the real engine.

    The XPath 3.0 engine leaves the call to elementpath's ``fn:matches``,
    whose bound reference was swept by the installer.  The second evaluation
    must add one hit and no miss; patching only the package re-export leaves
    this call site on the original and adds neither.
    """
    engine = XPathEngine("3.0")
    compiled = engine.compile('matches("abc", "a.c")', XPathStaticContext())

    first = engine.evaluate(compiled, ExpressionEnvironment())
    before = _counts()
    second = engine.evaluate(compiled, ExpressionEnvironment())
    after = _counts()

    assert first[0].value == XdmAtomic.boolean(True).value
    assert second[0].value == XdmAtomic.boolean(True).value
    assert after == (before[0] + 1, before[1])


# ---------------------------------------------------------------------------
# §7.9 — the XSD pattern path is unchanged


def test_facets_pattern_translation_result_is_unchanged() -> None:
    """``facets.compile_pattern`` behaves identically across a cache clear.

    Whether the module imported before or after the installer, the XSD path
    must produce the same anchored pattern and the same match answers.  The
    test does not assert which function object ``facets`` holds.
    """
    before = compile_pattern("[a-z]+")
    regex_cache.clear_translation_cache()
    after = compile_pattern("[a-z]+")

    assert before.pattern == after.pattern
    for compiled in (before, after):
        assert compiled.search("abc") is not None
        assert compiled.search("ABC") is None
