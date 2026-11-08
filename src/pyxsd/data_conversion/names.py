"""Expanded-name, EQName, and deterministic lexical-prefix helpers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import NoReturn

from pyxsd.data_conversion.contracts import raise_conversion
from pyxsd.xsd_data_types import NCName


def _valid_ncname(text: str) -> bool:
    return NCName._pattern.fullmatch(text) is not None


def _invalid_name(name: str, kind: str) -> NoReturn:
    raise_conversion("name-invalid", "", f"invalid {kind} {name!r}")


def _split_expanded(name: str) -> tuple[str, str]:
    """Split a Clark expanded name into its namespace URI and local part."""
    if name.startswith("{"):
        end = name.find("}")
        if end < 0:
            _invalid_name(name, "expanded name")
        uri = name[1:end]
        local = name[end + 1 :]
    else:
        uri = ""
        local = name
    if not _valid_ncname(local):
        _invalid_name(name, "expanded name")
    return uri, local


def expanded_to_eqname(name: str) -> str:
    """Convert a Clark expanded name to the profile's ``Q{uri}local`` EQName."""
    uri, local = _split_expanded(name)
    return f"Q{{{uri}}}{local}" if uri else local


def eqname_to_expanded(name: str) -> str:
    """Convert a ``Q{uri}local`` EQName to a Clark expanded name."""
    if not name.startswith("Q{"):
        if not _valid_ncname(name):
            _invalid_name(name, "EQName")
        return name
    end = name.find("}", 2)
    if end < 0:
        _invalid_name(name, "EQName")
    uri = name[2:end]
    local = name[end + 1 :]
    if not _valid_ncname(local):
        _invalid_name(name, "EQName")
    return f"{{{uri}}}{local}" if uri else local


class PrefixAllocator:
    """Deterministic lexical-QName prefixes shared by BadgerFish and JsonML.

    Known namespace URIs come from the compiled schema's expanded names,
    sorted so the same URI always receives the same ``ns0``, ``ns1``, ...
    prefix regardless of instance order or dictionary iteration order.
    Namespace URIs outside that known set receive a prefix derived from the
    URI itself, so a wildcard name allocates the same prefix in every run.

    ``frame()`` reports the prefixes actually used, which lets JsonML emit
    only declarations needed by the subtree. ``attribute`` marks an
    attribute name; this scheme always uses explicit prefixes for both
    elements and attributes, so it selects the same prefix for either.
    """

    def __init__(self, known_names: Iterable[str] = ()) -> None:
        self._prefixes: dict[str, str] = {}
        self._used: dict[str, str] = {}
        uris: set[str] = set()
        for name in known_names:
            uri, _local = _split_expanded(name)
            if uri:
                uris.add(uri)
        for index, uri in enumerate(sorted(uris)):
            self._prefixes[uri] = f"ns{index}"

    def lexical(self, expanded_name: str, *, attribute: bool = False) -> str:
        """Return the lexical QName for *expanded_name* and record its use."""
        uri, local = _split_expanded(expanded_name)
        if not uri:
            return local
        prefix = self._prefixes.get(uri)
        if prefix is None:
            prefix = "ns_" + uri.encode("utf-8").hex()
            self._prefixes[uri] = prefix
        self._used[prefix] = uri
        return f"{prefix}:{local}"

    def frame(self) -> dict[str, str]:
        """Return the prefix-to-URI bindings used so far, ordered by prefix."""
        return dict(sorted(self._used.items()))
