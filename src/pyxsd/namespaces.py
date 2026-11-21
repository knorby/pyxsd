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
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
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


def _require_lexical_qname(qname: str) -> None:
    """Reject a lexical QName that is not ``NCName`` or ``NCName:NCName``."""
    from elementpath.helpers import is_ncname

    parts = qname.split(":")
    if len(parts) > 2 or not all(is_ncname(part) for part in parts):
        raise NamespaceError(f"the lexical QName {qname!r} is not valid")


@dataclass(frozen=True, slots=True)
class ExpandedName:
    """An expanded name: a namespace URI and a local part.

    ``uri`` is ``None`` when the name is in no namespace. Clark notation
    (``{uri}local``) stays the internal key representation, so this value
    is a typed view at the public boundary rather than a replacement for
    the string keys the rest of the library uses.
    """

    uri: str | None
    local: str

    def __post_init__(self) -> None:
        # ``""`` and ``None`` both mean "no namespace"; canonicalize so the
        # two spellings cannot become unequal keys for the same name.
        if self.uri == "":
            object.__setattr__(self, "uri", None)

    @property
    def clark(self) -> str:
        """This name in Clark notation."""
        return clark(self.uri, self.local)

    @classmethod
    def from_clark(cls, name: str) -> ExpandedName:
        """Build an expanded name from a Clark name.

        Unlike :func:`local_name` and :func:`namespace_of`, which stay
        tolerant of names already reported as malformed elsewhere, this
        constructor rejects a name it cannot represent: an empty name, a
        missing closing ``}``, or an empty local part.
        """
        if not name.startswith("{"):
            if not name:
                raise NamespaceError("an expanded name cannot be empty")
            return cls(None, name)
        uri, sep, local = name[1:].partition("}")
        if not sep:
            raise NamespaceError(f"malformed expanded name {name!r}: missing closing '}}'")
        if not local:
            raise NamespaceError(f"malformed expanded name {name!r}: empty local part")
        return cls(uri or None, local)


@dataclass(frozen=True, slots=True)
class NamespaceBindings:
    """An immutable snapshot of one element's in-scope namespace bindings.

    Build one with :meth:`from_mapping` or :meth:`NamespaceContext.snapshot`.
    The snapshot copies its source, so later recording in the context cannot
    change it, and it iterates in sorted prefix order so prefix selection is
    deterministic. A prefix that is not bound here raises
    :class:`NamespaceError`; the implicit ``xml`` prefix always resolves.
    """

    _bindings: Mapping[str, str]

    def __post_init__(self) -> None:
        # Validate at the boundary and copy: the value must never observe
        # a later change in the mapping it was built from. Sorting keeps
        # iteration -- and any prefix chosen from it -- deterministic.
        checked: dict[str, str] = {}
        for prefix, uri in self._bindings.items():
            if not isinstance(prefix, str) or not isinstance(uri, str):
                raise TypeError("namespace bindings must map str prefixes to str URIs")
            checked[prefix] = uri
        object.__setattr__(self, "_bindings", MappingProxyType(dict(sorted(checked.items()))))

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, str]) -> NamespaceBindings:
        """Return a snapshot of ``mapping``'s ``prefix -> uri`` pairs."""
        return cls(mapping)

    def as_dict(self) -> dict[str, str]:
        """Return a mutable copy of the bindings, in sorted prefix order."""
        return dict(self._bindings)

    def resolve_name(self, qname: str, *, is_attribute: bool = False) -> str:
        """Resolve a lexical name to a Clark name.

        ``is_attribute`` matters for unprefixed names: attributes are
        never in the default namespace, while element names are. An
        already-expanded Clark name is returned unchanged. A malformed
        lexical QName raises :class:`NamespaceError` rather than becoming
        an invalid expanded-name key.
        """
        return self._resolve_in(
            self._bindings,
            qname,
            is_attribute=is_attribute,
            strict=True,
        )

    def resolve_qname(self, qname: str) -> str:
        """Resolve QName-valued content to a Clark name.

        QName content follows element-name rules rather than
        attribute-name rules, so an unprefixed lexical QName uses the
        default namespace. Keeping this a separate operation stops a
        caller from applying attribute semantics to content. A malformed
        lexical QName raises :class:`NamespaceError`.
        """
        return self._resolve_in(self._bindings, qname, strict=True)

    def prefix_for(self, uri: str | None) -> str | None:
        """Return a prefix bound to ``uri``.

        The default namespace is returned as ``""``. ``None`` is returned
        when the URI is bound nowhere in scope.
        """
        return self._prefix_in(self._bindings, uri)

    @staticmethod
    def _resolve_in(
        bindings: Mapping[str, str],
        qname: str,
        *,
        is_attribute: bool = False,
        strict: bool = False,
    ) -> str:
        """Resolve a lexical name against a raw binding frame.

        This is the one resolution implementation. :class:`NamespaceContext`
        calls it with the element's recorded frame so a QName lookup on a
        compilation hot path does not materialize a snapshot per call. That
        legacy entry point stays tolerant of malformed lexical names; the
        public snapshot methods pass ``strict=True`` and reject them.
        """
        if qname.startswith("{"):
            return qname
        if strict:
            _require_lexical_qname(qname)
        if ":" in qname:
            prefix, local = qname.split(":", 1)
            # The ``xml`` prefix is implicitly bound by the XML spec.
            uri = XML_NS if prefix == "xml" else bindings.get(prefix)
            if uri is None:
                raise NamespaceError(f"the namespace prefix {prefix!r} is not bound in scope")
            return clark(uri, local)
        if is_attribute:
            return qname
        uri = bindings.get("")
        return clark(uri, qname) if uri else qname

    @staticmethod
    def _prefix_in(bindings: Mapping[str, str], uri: str | None) -> str | None:
        for prefix, bound in bindings.items():
            if bound == uri and prefix:
                return prefix
        if bindings.get("") == uri:
            return ""
        return None


class NamespaceContext:
    """The in-scope namespace bindings of every element in parsed documents.

    Bindings are recorded per element, so a prefix rebound in a nested
    scope resolves correctly there while the outer binding remains valid
    for its own scope. One context can accumulate several documents (a
    schema, its includes and imports, and the instance).

    The context also records, weakly, how each root it produced can be
    read again (see :meth:`record_root_source`): a path string or a
    seekable stream. That provenance is what lets a later phase re-read a
    document's prolog -- for DTD declaration evidence -- after the tree
    was already parsed. Elements and streams are held weakly on both
    sides, so a long-lived context retains neither.
    """

    def __init__(self) -> None:
        self._bindings: weakref.WeakKeyDictionary[ET.Element, dict[str, str]] = (
            weakref.WeakKeyDictionary()
        )
        self.root_bindings: dict[str, str] = {}
        # Root elements of documents this context parsed, mapped to a
        # re-readable source: a path string, or a weak reference to a
        # seekable stream. Values stay weak for streams so the context
        # never keeps one alive.
        self._root_sources: weakref.WeakKeyDictionary[ET.Element, Any] = weakref.WeakKeyDictionary()

    def record_root_source(self, root: ET.Element, source: Any) -> None:
        """Record, best effort, how the document rooted at *root* re-reads.

        A path-like *source* is recorded as its filesystem path (it can be
        reopened); a seekable stream is recorded by weak reference (it can
        be rewound); anything else -- a non-seekable stream, an
        unweakreffable object -- records nothing, and the root later
        resolves to no provenance. Failure is silent by design: the
        ordinary parse already succeeded without this information.
        """
        if isinstance(source, (str, os.PathLike)):
            self._root_sources[root] = os.fspath(source)
            return
        seekable = getattr(source, "seekable", None)
        if seekable is None or not seekable():
            return
        try:
            self._root_sources[root] = weakref.ref(source)
        except TypeError:
            # An unweakreffable stream is deliberately not retained.
            return

    def root_source(self, root: ET.Element) -> Any:
        """The re-readable source recorded for *root*: path, stream, or None.

        Reopening or rewinding happens strictly after the original parse,
        so the source may have changed or vanished since; callers must
        treat any failure to re-read as "no evidence" rather than an
        error. A stream whose weak reference died also resolves to None.
        """
        provenance = self._root_sources.get(root)
        if provenance is None or isinstance(provenance, str):
            return provenance
        return provenance()

    def record(self, element: ET.Element, frame: dict[str, str]) -> None:
        """Record ``element``'s in-scope ``prefix -> uri`` bindings."""
        self._bindings[element] = frame
        if not self.root_bindings:
            self.root_bindings = frame

    def bindings_for(self, element: ET.Element) -> dict[str, str]:
        """Return the in-scope bindings of ``element`` (possibly empty)."""
        return self._frame(element)

    def snapshot(self, element: ET.Element) -> NamespaceBindings:
        """Return an immutable copy of ``element``'s in-scope bindings."""
        return NamespaceBindings.from_mapping(self._frame(element))

    def resolve(self, element: ET.Element, qname: str, *, is_attribute: bool = False) -> str:
        """Resolve a lexical QName to a Clark name.

        ``is_attribute`` matters for unprefixed names: attributes are
        never in the default namespace, while element content is.
        An already-expanded Clark name is returned unchanged.
        """
        return NamespaceBindings._resolve_in(self._frame(element), qname, is_attribute=is_attribute)

    def prefix_for(self, element: ET.Element, uri: str | None) -> str | None:
        """Return a prefix bound to ``uri`` in ``element``'s scope.

        The default namespace is returned as ``""``. ``None`` is
        returned when the URI is bound nowhere in scope. When several
        prefixes name the same URI, the first declaration in scope wins,
        preserving the accessor names generated from this lookup.
        """
        return NamespaceBindings._prefix_in(self._frame(element), uri)

    def _frame(self, element: ET.Element) -> dict[str, str]:
        return self._bindings.get(element, self.root_bindings)


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

    The returned root's re-read provenance is recorded on *ctx* when the
    source admits it (path or seekable stream). Re-reading happens later
    and best effort: a source that changed or vanished since the parse
    yields no evidence, never an error.
    """
    ctx = context if context is not None else NamespaceContext()
    if limits is not None and limits.max_bytes is not None:
        with _byte_limited_source(source, limits.max_bytes) as bounded:
            root = _parse_events(bounded, ctx, policy, limits)
    else:
        root = _parse_events(source, ctx, policy, limits)
    # Record the caller's original source (not a byte-limited wrapper):
    # the wrapper is exhausted and unseekable, while the original path or
    # stream is what a later prolog re-read can use.
    ctx.record_root_source(root, source)
    return root


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
    "ExpandedName",
    "NamespaceBindings",
    "NamespaceContext",
    "NamespaceError",
    "clark",
    "local_name",
    "namespace_of",
    "parse_with_namespaces",
]
