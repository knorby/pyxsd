"""Static lexical validation for attribute values with a fixed value space.

Several XSLT attributes are attribute value templates whose permitted legal
values are enumerations or simple lexical forms (``yes``/``no``, an integer,
``ascending``/``descending``).  When an AVT contains no unescaped braces, the
effective value is already known during static analysis, so an illegal value is
the static error ``XTSE0020`` rather than the dynamic ``XTDE0030`` that a
non-constant template would produce at execution time.

The helpers in this module keep that rule in one place so each owning compiler
site validates only its own attributes.  They never validate a dynamic AVT: a
value with an unescaped ``{`` is deferred to the runtime, where the effective
value decides.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pyxsd.locations import IssueLocation
from pyxsd.xslt.errors import XsltStaticError

if TYPE_CHECKING:
    from pyxsd.xslt.compiler.expressions import CompiledSequenceType

__all__ = [
    "constant_value",
    "require_boolean_value",
    "require_choice_value",
    "require_integer_sequence_value",
    "require_integer_value",
    "require_item_type",
]

#: The legal lexical forms of the XSLT ``yes-or-no`` type.
BOOLEAN_VALUES = frozenset({"yes", "true", "1", "no", "false", "0"})

#: The lexical form of one ``xs:integer`` after leading/trailing whitespace.
_INTEGER = re.compile(r"[+-]?[0-9]+")


def constant_value(source: str) -> str | None:
    """The effective value of *source* when it contains no dynamic part.

    An unescaped ``{`` starts a value template and makes the effective value
    unknowable statically, so this returns ``None``.  Doubled braces are the
    AVT escapes for literal braces and are returned as one brace each.
    """
    index = 0
    parts: list[str] = []
    while index < len(source):
        character = source[index]
        if character != "{":
            parts.append(character)
            index += 1
            continue
        if index + 1 < len(source) and source[index + 1] == "{":
            parts.append("{")
            index += 2
            continue
        return None
    return "".join(parts).replace("}}", "}")


def _value_error(
    message: str,
    source: str,
    label: str,
    uri: str | None,
    location: IssueLocation | None,
) -> XsltStaticError:
    return XsltStaticError(
        f"{label} has an invalid value {source!r}: {message}",
        code="XTSE0020",
        uri=uri,
        location=location,
    )


def require_boolean_value(
    source: str,
    *,
    label: str,
    uri: str | None,
    location: IssueLocation | None,
) -> None:
    """Reject a constant AVT that is not an XSLT boolean lexical form."""
    value = constant_value(source)
    if value is None:
        return
    if value.strip() not in BOOLEAN_VALUES:
        raise _value_error("expected yes, no, true, false, 1, or 0", source, label, uri, location)


def require_integer_value(
    source: str,
    *,
    label: str,
    uri: str | None,
    location: IssueLocation | None,
) -> None:
    """Reject a constant AVT that is not an ``xs:integer`` lexical form."""
    value = constant_value(source)
    if value is None:
        return
    if _INTEGER.fullmatch(value.strip()) is None:
        raise _value_error("expected an integer", source, label, uri, location)


def require_integer_sequence_value(
    source: str,
    *,
    label: str,
    uri: str | None,
    location: IssueLocation | None,
) -> None:
    """Reject a constant AVT that is not a whitespace-separated integer list.

    ``xsl:number/@start-at`` accepts one or more integers (XSLT 3.0 section
    12.1) so it uses the list form rather than the single-integer rule.
    """
    value = constant_value(source)
    if value is None:
        return
    tokens = value.split()
    if not tokens or any(_INTEGER.fullmatch(token) is None for token in tokens):
        raise _value_error("expected one or more integers", source, label, uri, location)


def require_choice_value(
    source: str,
    choices: frozenset[str],
    *,
    label: str,
    uri: str | None,
    location: IssueLocation | None,
) -> None:
    """Reject a constant AVT whose value is outside *choices*."""
    value = constant_value(source)
    if value is None:
        return
    if value.strip() not in choices:
        allowed = ", ".join(sorted(choices))
        raise _value_error(f"expected one of {allowed}", source, label, uri, location)


def require_item_type(
    sequence_type: CompiledSequenceType,
    *,
    label: str,
    uri: str | None,
    location: IssueLocation | None,
) -> None:
    """Require a compiled sequence type to be an item type.

    ``xsl:context-item`` and ``xsl:global-context-item`` declare the type of a
    single item; an occurrence indicator or ``empty-sequence()`` is therefore
    the static error ``XTSE0020``.
    """
    if sequence_type.item_type is None or sequence_type.occurrence:
        raise XsltStaticError(
            f"{label} must be an item type without an occurrence indicator",
            code="XTSE0020",
            uri=uri,
            location=location,
        )
