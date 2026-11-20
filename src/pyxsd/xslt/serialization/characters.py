"""Character maps and Unicode normalization shared by every output method.

A character map replaces selected characters before escaping and normalization.
Mapped replacement text is emitted verbatim: it is never escaped, normalized,
or mapped a second time.  Unmapped runs are handed to the caller's transform,
which applies normalization and the method's escaping rules.

The serializer modules depend on this layer; it depends on nothing but the
serialization error type, so no output method owns a second implementation of
character substitution.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, Literal, cast

from pyxsd.xslt.errors import XsltSerializationError

__all__ = [
    "CharacterMap",
    "apply_mapped",
    "character_map_index",
    "character_map_pairs",
    "normalize_text",
]

CharacterMap = Mapping[str, str]
_NormalizationForm = Literal["NFC", "NFD", "NFKC", "NFKD"]


def _invalid(message: str) -> XsltSerializationError:
    return XsltSerializationError(message, code="SEPM0016")


def character_map_pairs(value: Any) -> tuple[tuple[str, str], ...]:
    """Validate one character-map parameter as ``(character, replacement)`` pairs.

    A mapping or an iterable of pairs is accepted.  Each key must be exactly one
    character and each replacement must be a string; anything else is a
    serialization parameter error.
    """
    if value is None:
        return ()
    if isinstance(value, Mapping):
        entries: Iterable[Any] = value.items()
    elif isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise _invalid("character-maps must map one character to a replacement string")
    else:
        entries = value
    pairs: list[tuple[str, str]] = []
    for entry in entries:
        if isinstance(entry, (str, bytes)) or not isinstance(entry, Sequence) or len(entry) != 2:
            raise _invalid("character-maps entries must be (character, replacement) pairs")
        character, replacement = entry
        if not isinstance(character, str) or len(character) != 1:
            raise _invalid("a character-map key must be exactly one character")
        if not isinstance(replacement, str):
            raise _invalid("a character-map replacement must be a string")
        pairs.append((character, replacement))
    return tuple(pairs)


def character_map_index(pairs: tuple[tuple[str, str], ...]) -> CharacterMap:
    """Index validated pairs for one serialization pass."""
    return MappingProxyType(dict(pairs))


def apply_mapped(value: str, characters: CharacterMap, transform: Any) -> str:
    """Run *transform* over the unmapped runs of *value*.

    Characters present in *characters* contribute their replacement text
    verbatim; runs without a mapping are passed to *transform* as a whole.
    """
    if not characters:
        return transform(value)
    pieces: list[str] = []
    pending: list[str] = []
    for character in value:
        replacement = characters.get(character)
        if replacement is None:
            pending.append(character)
            continue
        if pending:
            pieces.append(transform("".join(pending)))
            pending.clear()
        pieces.append(replacement)
    if pending:
        pieces.append(transform("".join(pending)))
    return "".join(pieces)


def normalize_text(value: str, form: str | None) -> str:
    """Apply one resolved ``normalization-form`` to a character run."""
    if form is None or form == "none":
        return value
    return unicodedata.normalize(cast(_NormalizationForm, form), value)
