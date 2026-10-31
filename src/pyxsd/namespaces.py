"""XML namespace capture and QName resolution.

ElementTree discards the prefix-to-URI bindings of a document once it is
parsed, so QName-valued attributes (``type``, ``ref``, ``base``,
``xsi:type``) cannot be resolved afterwards. This module parses a
document with :func:`xml.etree.ElementTree.iterparse` while tracking
namespace scopes, then answers two questions about any element: what URI
does a prefix name here, and what prefixes name a URI.

Names are represented in ElementTree's own Clark notation,
``{uri}local``, so expanded instance tags need no translation.
"""

from __future__ import annotations

import os
import weakref
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO, TYPE_CHECKING, Any, Literal

from pyxsd.exceptions import NamespaceError  # re-exported for compatibility

if TYPE_CHECKING:
    from pyxsd.xml_policy import XmlLimits, XmlParserPolicy

#: The XML Schema namespace.
XSD_NS = "http://www.w3.org/2001/XMLSchema"

#: The XML Schema instance namespace.
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

#: The XML namespace. Its ``xml`` prefix is bound implicitly by the XML
#: specification and need not (must not) be declared, so resolution has
#: to supply it.
XML_NS = "http://www.w3.org/XML/1998/namespace"

#: The XLink namespace. Its schema document is not bundled, but the
#: XLink 1.0 attribute declarations are part of the vocabulary a
#: conforming processor resolves for the namespace, so they are
#: registered as built-ins.
XLINK_NS = "http://www.w3.org/1999/xlink"


def clark(uri: str | None, local: str) -> str:
    """Return the Clark name for ``uri`` and ``local``.

    With no URI the local name is returned unchanged, which is how
    no-namespace components are keyed.
    """
    if uri:
        return f"{{{uri}}}{local}"
    return local


def local_name(name: str) -> str:
    """Return the local part of a Clark name (or a plain name).

    A ``{``-prefixed name without a closing ``}`` is malformed (e.g. an
    unresolvable ``type="{oops"`` QName already reported at schema
    phase); it is returned unchanged so downstream lookups simply miss
    instead of raising.
    """
    if name.startswith("{"):
        _, sep, local = name.partition("}")
        if sep:
            return local
    return name


def local_part(name: str) -> str:
    """Return the local part of a name in lexical QName or Clark form.

    Schema-time lookups are fed both spellings: an authored reference is
    lexical (``hl7:QTY``) while chameleon pre-processing (XSD 1.1
    Appendix F.1) rewrites an unprefixed reference into the expanded
    (Clark) name of the adopting namespace, whose URI may itself contain
    colons. Stripping the namespace braces first keeps both spellings
    resolving to the same local part.
    """
    return local_name(name).rpartition(":")[2]


def namespace_of(name: str) -> str | None:
    """Return the namespace URI of a Clark name, or ``None``.

    A malformed Clark name without a closing ``}`` has no namespace.
    """
    if name.startswith("{"):
        uri, sep, _ = name[1:].partition("}")
        if sep:
            return uri
    return None


class NamespaceContext:
    """The in-scope namespace bindings of every element in parsed documents.

    Bindings are recorded per element, so a prefix rebound in a nested
    scope resolves correctly there while the outer binding remains valid
    for its own scope. One context can accumulate several documents (a
    schema, its includes and imports, and the instance).
    """

    def __init__(self) -> None:
        self._bindings: weakref.WeakKeyDictionary[ET.Element, dict[str, str]] = (
            weakref.WeakKeyDictionary()
        )
        self.root_bindings: dict[str, str] = {}

    def record(self, element: ET.Element, frame: dict[str, str]) -> None:
        """Record ``element``'s in-scope ``prefix -> uri`` bindings."""
        self._bindings[element] = frame
        if not self.root_bindings:
            self.root_bindings = frame

    def bindings_for(self, element: ET.Element) -> dict[str, str]:
        """Return the in-scope bindings of ``element`` (possibly empty)."""
        return self._bindings.get(element, self.root_bindings)

    def resolve(self, element: ET.Element, qname: str, *, is_attribute: bool = False) -> str:
        """Resolve a lexical QName to a Clark name.

        ``is_attribute`` matters for unprefixed names: attributes are
        never in the default namespace, while element content is.
        An already-expanded Clark name is returned unchanged.
        """
        if qname.startswith("{"):
            return qname
        if ":" in qname:
            prefix, local = qname.split(":", 1)
            uri = self._lookup(element, prefix)
            if uri is None:
                raise NamespaceError(f"the namespace prefix {prefix!r} is not bound in scope")
            return clark(uri, local)
        if is_attribute:
            return qname
        uri = self._lookup(element, "")
        return clark(uri, qname) if uri else qname

    def prefix_for(self, element: ET.Element, uri: str | None) -> str | None:
        """Return a prefix bound to ``uri`` in ``element``'s scope.

        The default namespace is returned as ``""``. ``None`` is
        returned when the URI is bound nowhere in scope.
        """
        frame = self.bindings_for(element)
        for prefix, bound in frame.items():
            if bound == uri and prefix:
                return prefix
        if frame.get("") == uri:
            return ""
        return None

    def _lookup(self, element: ET.Element, prefix: str) -> str | None:
        # The ``xml`` prefix is implicitly bound by the XML spec.
        if prefix == "xml":
            return XML_NS
        return self.bindings_for(element).get(prefix)


def parse_with_namespaces(
    source: str | os.PathLike[str] | IO[bytes] | IO[str],
    context: NamespaceContext | None = None,
    *,
    policy: XmlParserPolicy | None = None,
    limits: XmlLimits | None = None,
) -> ET.Element:
    """Parse ``source`` and record namespace bindings in ``context``.

    Returns the root element. The resulting tree is the same one
    :func:`xml.etree.ElementTree.parse` would produce; the context is the
    only addition. When ``context`` is omitted, an internal throwaway is
    used, which is useful only for the returned tree.

    ``policy`` selects an opt-in parser posture; ``None`` and
    :meth:`~pyxsd.xml_policy.XmlParserPolicy.default` keep the stdlib
    parser and its current DTD/entity behavior. ``limits`` optionally
    bounds the supplied bytes (text streams are measured by their UTF-8
    encoding), element depth, and element count; a violation raises
    :class:`~pyxsd.resource_policy.ResourcePolicyError`.
    Defusedxml denials are converted to the same error with the original
    exception as ``__cause__``; malformed XML still raises
    :class:`xml.etree.ElementTree.ParseError`.
    """
    ctx = context if context is not None else NamespaceContext()
    if limits is not None and limits.max_bytes is not None:
        with _byte_limited_source(source, limits.max_bytes) as bounded:
            return _parse_events(bounded, ctx, policy, limits)
    return _parse_events(source, ctx, policy, limits)


def _parse_events(
    source: str | os.PathLike[str] | IO[bytes] | IO[str] | _ByteLimitedReader,
    ctx: NamespaceContext,
    policy: XmlParserPolicy | None,
    limits: XmlLimits | None,
) -> ET.Element:
    pending: list[tuple[str, str]] = []
    stack: list[dict[str, str]] = [{}]
    root: ET.Element | None = None
    count = 0

    for event, payload in _iterparse_events(source, policy):
        if event == "start-ns":
            prefix, uri = payload
            pending.append((prefix or "", uri))
        elif event == "start":
            count += 1
            if limits is not None:
                if limits.max_depth is not None and len(stack) > limits.max_depth:
                    raise _policy_error(f"the XML input exceeds max_depth={limits.max_depth}")
                if limits.max_elements is not None and count > limits.max_elements:
                    raise _policy_error(f"the XML input exceeds max_elements={limits.max_elements}")
            frame = dict(stack[-1])
            for prefix, uri in pending:
                frame[prefix] = uri
            pending.clear()
            stack.append(frame)
            ctx.record(payload, frame)
            if root is None:
                root = payload
        elif event == "end":
            stack.pop()
        # "end-ns" needs no handling: element scopes already bound and
        # released the declarations, because start-ns precedes the
        # element's start event and end-ns follows its end event.

    if root is None:
        raise ET.ParseError("the document has no root element")
    return root


def _iterparse_events(
    source: str | os.PathLike[str] | IO[bytes] | IO[str] | _ByteLimitedReader,
    policy: XmlParserPolicy | None,
) -> Iterator[tuple[str, Any]]:
    events: tuple[Literal["start", "end", "start-ns", "end-ns"], ...] = (
        "start",
        "end",
        "start-ns",
        "end-ns",
    )
    if policy is not None and policy.use_defusedxml:
        from defusedxml import ElementTree as DefusedET
        from defusedxml.common import DefusedXmlException
        from defusedxml.ElementTree import DefusedXMLParser

        from pyxsd.xml_policy import configure_expat_security

        parser = DefusedXMLParser(
            target=ET.TreeBuilder(),
            forbid_dtd=policy.forbid_dtd,
            forbid_entities=policy.forbid_entities,
            forbid_external=policy.forbid_external,
        )
        # Defusedxml's flags cannot reject an external SYSTEM/PUBLIC
        # DOCTYPE while still allowing a plain internal DTD. The shared
        # declaration guard adds exactly that check (and leaves the
        # defused handlers, with their exception causes, intact).
        configure_expat_security(parser, policy)
        try:
            yield from DefusedET.iterparse(source, events=events, parser=parser)
        except DefusedXmlException as exc:
            raise _policy_error(_defused_message(exc)) from exc
    else:
        yield from ET.iterparse(source, events=events)


def _defused_message(exc: Exception) -> str:
    from defusedxml.common import DTDForbidden, EntitiesForbidden

    if isinstance(exc, DTDForbidden):
        return "XML DOCTYPE declarations are forbidden by the parser policy"
    if isinstance(exc, EntitiesForbidden):
        return "XML entity declarations are forbidden by the parser policy"
    return "XML external references are forbidden by the parser policy"


@contextmanager
def _byte_limited_source(
    source: str | os.PathLike[str] | IO[bytes] | IO[str],
    max_bytes: int,
) -> Iterator[_ByteLimitedReader]:
    if isinstance(source, (str, os.PathLike)):
        # Let ElementTree see a file object here so the same counter applies
        # to named files and caller-supplied streams alike.
        with open(source, "rb") as stream:
            yield _ByteLimitedReader(stream, max_bytes)
    else:
        yield _ByteLimitedReader(source, max_bytes)


class _ByteLimitedReader:
    """A read-only stream proxy that fails as soon as its budget is crossed."""

    def __init__(self, stream: IO[bytes] | IO[str], max_bytes: int) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._seen = 0

    def read(self, size: int = -1) -> Any:
        data = self._stream.read(size)
        # A text stream has no encoding of its own here; UTF-8 is the
        # documented unit so a byte budget is not silently a character
        # budget.
        self._seen += len(data.encode("utf-8")) if isinstance(data, str) else len(data)
        if self._seen > self._max_bytes:
            raise _policy_error(f"the XML input exceeds max_bytes={self._max_bytes}")
        return data


def _policy_error(message: str) -> Exception:
    # Imported lazily: pyxsd.resource_policy imports NamespaceContext from
    # this module, so a module-level import here would be circular.
    from pyxsd.resource_policy import ResourcePolicyError

    return ResourcePolicyError(message)


__all__ = [
    "XSD_NS",
    "XSI_NS",
    "NamespaceContext",
    "NamespaceError",
    "clark",
    "local_name",
    "namespace_of",
    "parse_with_namespaces",
]
