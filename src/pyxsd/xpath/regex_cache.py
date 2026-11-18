"""Memoise elementpath's regex translation for the process.

elementpath translates the source pattern to Python regex syntax on every
``fn:matches``/``fn:replace``/``fn:tokenize``/``fn:analyze-string`` call.
``translate_pattern`` is a pure function of its arguments and elementpath's
installed Unicode data, and a single sweep can call it more than a million
times for one distinct pattern (``unicode90-001``).  This module wraps it once
at the XPath-engine import seam, following the project's documented policy of
patching elementpath parsers, and leaves results and errors untouched.

The cache is process-scoped on purpose: the key is content-addressed and the
function retains no transformation, document, or parser state.  It is bounded
at ``_CACHE_MAXSIZE`` entries, so a pathological caller cannot grow it without
limit.  ``install_unicode_data`` changes the translation input, so the
installed version is part of the key; a caller that installs different data
under an identical version string must call ``clear_translation_cache()``.

``lru_cache`` never stores an exception, so an invalid pattern is re-translated
on every call and raises the same ``RegexError`` with the same traceback and
``__cause__`` chain as before; only successful translations are memoised.

The installer patches ``elementpath.regex.patterns`` (the definition module)
and then every module in ``sys.modules`` that has already bound the original
function.  Patching the definition module covers every elementpath module
imported *later*; the scan covers those imported *before*.  Between the two no
loaded module can keep the uncached hot path, and a future elementpath release
that adds a module is covered without a change here.
"""

from __future__ import annotations

import sys
from functools import lru_cache, wraps
from typing import TYPE_CHECKING

import elementpath.regex.patterns
from elementpath.regex import unicode_version as _unicode_version

if TYPE_CHECKING:
    from functools import _CacheInfo  # the named tuple lru_cache reports

_ORIGINAL = elementpath.regex.patterns.translate_pattern
_CACHE_MAXSIZE = 1024
_INSTALLED = False


def _original_call(
    pattern: str,
    flags: int,
    xsd_version: str,
    back_references: bool,
    lazy_quantifiers: bool,
    anchors: bool,
    unicode_version: str,
) -> str:
    """Call the captured original; ``unicode_version`` only widens the key."""
    del unicode_version
    return _ORIGINAL(pattern, flags, xsd_version, back_references, lazy_quantifiers, anchors)


_translate_cached = lru_cache(maxsize=_CACHE_MAXSIZE)(_original_call)


@wraps(_ORIGINAL)
def translate_pattern(
    pattern: str,
    flags: int = 0,
    xsd_version: str = "1.0",
    back_references: bool = True,
    lazy_quantifiers: bool = True,
    anchors: bool = True,
) -> str:
    """Translate one pattern with the process-wide memo (elementpath's signature)."""
    return _translate_cached(
        pattern,
        flags,
        xsd_version,
        back_references,
        lazy_quantifiers,
        anchors,
        _unicode_version(),
    )


def _patch_loaded_modules() -> None:
    """Rebind the wrapper wherever a loaded elementpath module holds the original."""
    for name in sorted(sys.modules):
        if name != "elementpath" and not name.startswith("elementpath."):
            continue
        module = sys.modules[name]
        # Only modules that still hold the original are rebound, which makes the
        # scan safe to repeat and safe against modules that already got the wrapper.
        if getattr(module, "translate_pattern", None) is _ORIGINAL:
            # The target module is whichever one the scan found, so rebind its
            # global explicitly rather than assigning a literal attribute.
            module.__dict__["translate_pattern"] = translate_pattern


def install_regex_translation_cache() -> bool:
    """Install the memo once per process; ``True`` only for the installing call."""
    global _INSTALLED
    if _INSTALLED:
        return False
    # Patch the definition module first so modules imported after this point
    # bind the wrapper; the scan then covers modules imported before it.
    elementpath.regex.patterns.translate_pattern = translate_pattern
    _patch_loaded_modules()
    _INSTALLED = True
    return True


def translation_cache_info() -> _CacheInfo:
    """Hits, misses, bound, and current size of the translation memo."""
    return _translate_cached.cache_info()


def clear_translation_cache() -> None:
    """Drop every memoised translation and zero the counters.

    This is the documented escape hatch for a caller that installs different
    Unicode data under an unchanged version string.
    """
    _translate_cached.cache_clear()
