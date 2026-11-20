"""XLink 1.0 URI-reference escaping shared by RELAX NG syntax and resources.

RELAX NG 1.0 defines its anyURI-valued attributes through XML Schema's
``anyURI``: the XLink 1.0 section 5.4 escaping procedure is applied, and the
result must be an RFC 2396 URI reference as amended by RFC 2732.  XSD 1.0
``anyURI`` validation and RELAX NG ``datatypeLibrary``, ``href``, and
``xml:base`` handling use this one implementation, so a value is escaped the
same way everywhere and never through a generic URL normalizer.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

__all__ = ["is_uri_reference", "normalize_uri_reference", "xlink_escape"]

#: Characters XLink 1.0 section 5.4 converts to UTF-8 percent escapes.
_XLINK_ESCAPED_ASCII = frozenset(
    [chr(code) for code in range(0x20)]
    + [" ", "<", ">", '"', "{", "}", "|", "\\", "^", "`", "\x7f"]
)
_ANYURI_PATH = re.compile(r"^(?:[A-Za-z0-9\-_.!~*'():@&=+$,;]|/|%[0-9A-Fa-f]{2})*$")
_ANYURI_URIC = re.compile(r"^(?:[A-Za-z0-9\-_.!~*'();/?:@&=+$,\[\]]|%[0-9A-Fa-f]{2})*$")
_ANYURI_AUTHORITY = re.compile(r"^(?:[A-Za-z0-9\-_.!~*'()$+,;:@&=]|%[0-9A-Fa-f]{2}|\[|\])+$")
_ANYURI_SCHEME_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _is_xml_char(code: int) -> bool:
    """Whether *code* is an XML 1.0 Char (XSD 1.0 lexical space)."""
    return (
        code in (0x9, 0xA, 0xD)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
    )


def xlink_escape(text: str) -> str:
    """Apply the XLink 1.0 section 5.4 escaping procedure to *text*."""
    escaped: list[str] = []
    for char in text:
        code = ord(char)
        if code > 0x7F or char in _XLINK_ESCAPED_ASCII:
            if not _is_xml_char(code):
                raise TypeError(f"anyURI contains a non-XML character U+{code:04X}")
            escaped.extend(f"%{byte:02X}" for byte in char.encode("utf-8"))
        else:
            escaped.append(char)
    return "".join(escaped)


def _check_anyuri_authority(netloc: str) -> None:
    """Validate the RFC 2396 authority, including RFC 2732 IPv6 literals."""
    if _ANYURI_AUTHORITY.fullmatch(netloc) is None:
        raise TypeError(f"anyURI authority {netloc!r} is not a legal RFC 2396 authority")
    if "[" not in netloc:
        return
    open_index = netloc.index("[")
    close_index = netloc.index("]", open_index)
    literal = netloc[open_index + 1 : close_index]
    if "%" in literal:
        raise TypeError("anyURI IPv6 literals may not carry a zone identifier in XSD 1.0")
    try:
        ipaddress.IPv6Address(literal)
    except ValueError as exc:
        raise TypeError(f"anyURI host {literal!r} is not an IPv6 address") from exc
    suffix = netloc[close_index + 1 :]
    if suffix:
        port = suffix.removeprefix(":")
        if port and not port.isdigit():
            raise TypeError(f"anyURI port {port!r} is not numeric")


def _has_empty_authority(escaped: str) -> bool:
    """Whether the escaped reference contains an empty ``//`` authority."""
    rest = escaped
    scheme = _ANYURI_SCHEME_PREFIX.match(rest)
    if scheme is not None:
        rest = rest[scheme.end() :]
    if not rest.startswith("//"):
        return False
    authority = rest[2:]
    for separator in "/?#":
        index = authority.find(separator)
        if index != -1:
            authority = authority[:index]
    return not authority


def normalize_uri_reference(text: str) -> str:
    """Return the XLink-escaped form when *text* is an XSD 1.0 anyURI lexical."""
    escaped = xlink_escape(text)
    if escaped.count("#") > 1:
        raise TypeError("anyURI contains more than one fragment separator")
    if _has_empty_authority(escaped):
        raise TypeError(f"anyURI {text!r} has an empty authority component")
    try:
        parts = urlsplit(escaped)
    except ValueError as exc:
        raise TypeError(f"anyURI {text!r} is not a legal URI reference") from exc
    if not parts.scheme and ":" in parts.path.split("/", 1)[0]:
        raise TypeError(f"anyURI {text!r} uses a colon in a relative first path segment")
    if parts.netloc:
        _check_anyuri_authority(parts.netloc)
    if _ANYURI_PATH.fullmatch(parts.path) is None:
        raise TypeError(f"anyURI path is not legal RFC 2396 syntax: {text!r}")
    if _ANYURI_URIC.fullmatch(parts.query) is None:
        raise TypeError(f"anyURI query is not legal RFC 2396 uric: {text!r}")
    if _ANYURI_URIC.fullmatch(parts.fragment) is None:
        raise TypeError(f"anyURI fragment is not legal RFC 2396 uric: {text!r}")
    return escaped


def is_uri_reference(text: str) -> bool:
    """Whether *text* escapes to a legal XSD 1.0 ``anyURI`` lexical."""
    try:
        normalize_uri_reference(text)
    except TypeError:
        return False
    return True
