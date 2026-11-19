r"""Memoise elementpath's regex translation for the process.

elementpath translates the source pattern to Python regex syntax on every
``fn:matches``/``fn:replace``/``fn:tokenize``/``fn:analyze-string`` call.
``translate_pattern`` is a pure function of its arguments and elementpath's
installed Unicode data, and a single sweep can call it more than a million
times for one distinct pattern (``unicode90-001``).  This module wraps it once
at the XPath-engine import seam, as the engine already does for elementpath's
parsers, and leaves results and errors untouched.

The cache is process-scoped on purpose: the key is content-addressed and the
function retains no transformation, document, or parser state.
``_CACHE_MAXSIZE`` bounds the number of entries, not their bytes: a translation
scales with the pattern's Unicode-class expansion (``\p{Ll}`` is 858 characters
and ``\p{L}`` about 1.6 thousand; a 1,000-character pattern built from 200
``\p{L}`` classes is roughly 1.26 MiB), so 1,024 large computed classes could
retain over 1 GiB.  In the two censuses measured for this memo, each worker
retained at most 433 entries with a largest entry of 3,466 characters and peak
worker RSS stayed below the un-memoised run; a stylesheet that computes
thousands of large distinct classes can still retain multiple GiB for the
process lifetime, which is the residual risk this count bound does not remove.
``install_unicode_data`` changes the translation input, so the installed version
is part of the key; a caller that installs different data under an identical
version string must call ``clear_translation_cache()``.

``lru_cache`` never stores an exception, so an invalid pattern is re-translated
on every call and raises the same exception class and message with the same
``__cause__`` chain as before; only successful translations are memoised.  The
wrapper adds its own frames to the traceback, so the *frames* differ even though
the exception and its cause do not; because the patch is process-global, a
co-installed elementpath consumer sees those additional frames too.

The installer patches ``elementpath.regex.patterns`` (the definition module)
and then every module in ``sys.modules`` that has already bound the original
function.  Patching the definition module covers every elementpath module
imported *later*; the scan covers those imported *before*.  Between the two no
loaded *elementpath* module can keep the uncached hot path, and a future
elementpath release that adds a module is covered without a change here.  The
scan matches the attribute name, so a module that bound the function under
another name would be missed; elementpath has no such binding.
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


# ``@wraps`` copies elementpath's ``__doc__`` over any docstring written here,
# so the contract lives in the module docstring instead.
@wraps(_ORIGINAL)
def translate_pattern(
    pattern: str,
    flags: int = 0,
    xsd_version: str = "1.0",
    back_references: bool = True,
    lazy_quantifiers: bool = True,
    anchors: bool = True,
) -> str:
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
    """Install the memo once per process for callers that install sequentially.

    Returns ``True`` only for the call that installs it; a later call changes
    nothing and returns ``False``.
    """
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
