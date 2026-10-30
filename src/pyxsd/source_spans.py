"""Exact original-byte spans for XML element occurrences.

A :class:`SourceIndex` parses the original bytes with public
:mod:`xml.parsers.expat` events and records each element's byte range without
building another element tree. Paths use the same expanded-name and
same-name-sibling rules as :class:`pyxsd.locations.ParseProvenance`, so a
diagnostic address can be sliced directly out of the source.

The whole input is parsed and validated before any span is exposed. The index
applies the same declaration policy and structural budgets as
:func:`pyxsd.namespaces.parse_with_namespaces`, and it never installs an
external-resource fetcher.
"""

from __future__ import annotations

from dataclasses import dataclass
from xml.parsers import expat

from pyxsd.exceptions import PyXSDError
from pyxsd.locations import PathStep
from pyxsd.resource_policy import ResourcePolicyError
from pyxsd.xml_policy import XmlLimits, XmlParserPolicy, configure_expat_security

_NAMESPACE_SEPARATOR = "\x1f"


@dataclass(frozen=True)
class SourceSpan:
    """A half-open byte range in the original input: ``content[start:end]``."""

    start: int
    end: int


class SourceIndex:
    """Lazily queried map of element paths to exact source byte spans.

    ``source`` names the input in diagnostics. ``content`` is the original
    encoding; spans index its bytes, so a UTF-16 or BOM-prefixed document is
    sliced in that encoding rather than re-encoded. ``policy`` defaults to
    :meth:`~pyxsd.xml_policy.XmlParserPolicy.default` (the same declaration
    behavior as core parsing); ``limits`` bounds the supplied bytes, element
    depth (the root is depth one), and start-tag count.
    """

    def __init__(
        self,
        source: str,
        content: bytes,
        *,
        policy: XmlParserPolicy | None = None,
        limits: XmlLimits | None = None,
    ) -> None:
        self.source = source
        self._content = content
        effective = XmlParserPolicy.default() if policy is None else policy
        if limits is not None and limits.max_bytes is not None and len(content) > limits.max_bytes:
            raise ResourcePolicyError(f"the XML input exceeds max_bytes={limits.max_bytes}")
        unit, open_marker, close_marker, terminator = _encoding_markers(content)
        parser = expat.ParserCreate(namespace_separator=_NAMESPACE_SEPARATOR)
        # This parser starts with no declaration handlers, so the shared
        # guards are the ones that actually decide; no fetcher is installed.
        configure_expat_security(parser, effective)
        state = _IndexState(parser, content, unit, open_marker, close_marker, terminator, limits)
        parser.StartElementHandler = state.start
        parser.EndElementHandler = state.end
        parser.CharacterDataHandler = state.characters
        parser.CommentHandler = state.comment
        parser.ProcessingInstructionHandler = state.processing_instruction
        parser.StartCdataSectionHandler = state.start_cdata
        parser.SkippedEntityHandler = state.skipped_entity
        try:
            parser.Parse(content, True)
        except expat.ExpatError as exc:
            raise PyXSDError(f"{source}: malformed XML: {exc}") from exc
        self._spans = state.spans

    def span(self, path: tuple[PathStep, ...]) -> SourceSpan | None:
        """Return this occurrence's exact byte range, or ``None`` if unknown."""
        return self._spans.get(path)

    def extract(self, path: tuple[PathStep, ...]) -> bytes | None:
        """Return this occurrence's original bytes, or ``None`` if unknown."""
        span = self._spans.get(path)
        if span is None:
            return None
        return self._content[span.start : span.end]


class _IndexState:
    """One parse's path stack, opening offsets, and pending end-tag spans."""

    def __init__(
        self,
        parser: expat.XMLParserType,
        content: bytes,
        unit: int,
        open_marker: bytes,
        close_marker: bytes,
        terminator: bytes,
        limits: XmlLimits | None,
    ) -> None:
        self._parser = parser
        self._content = content
        self._unit = unit
        self._open_marker = open_marker
        self._close_marker = close_marker
        self._terminator = terminator
        self._limits = limits
        self.spans: dict[tuple[PathStep, ...], SourceSpan] = {}
        self._path: list[PathStep] = []
        self._counts: list[dict[str, int]] = [{}]
        self._starts: list[int] = []
        self._elements = 0
        self._pending: tuple[tuple[PathStep, ...], int, int] | None = None

    def start(self, name: str, attrs: dict[str, str]) -> None:
        self._advance()
        self._elements += 1
        limits = self._limits
        if limits is not None:
            depth = len(self._path) + 1
            if limits.max_depth is not None and depth > limits.max_depth:
                raise ResourcePolicyError(f"the XML input exceeds max_depth={limits.max_depth}")
            if limits.max_elements is not None and self._elements > limits.max_elements:
                raise ResourcePolicyError(
                    f"the XML input exceeds max_elements={limits.max_elements}"
                )
        expanded = _expanded_name(name)
        counts = self._counts[-1]
        index = counts.get(expanded, 0) + 1
        counts[expanded] = index
        self._path.append(PathStep(expanded, index))
        self._counts.append({})
        self._starts.append(self._parser.CurrentByteIndex)

    def end(self, name: str) -> None:
        self._advance()
        anchor = self._parser.CurrentByteIndex
        path = tuple(self._path)
        self._path.pop()
        self._counts.pop()
        start = self._starts.pop()
        if self._path:
            self._pending = (path, start, anchor)
        else:
            # The document element is the final end event. Trailing comments,
            # processing instructions, and whitespace cannot belong to it, so
            # its span is already decidable without waiting for another event.
            self._record(path, start, anchor, len(self._content))

    def characters(self, data: str) -> None:
        self._advance()

    def comment(self, data: str) -> None:
        self._advance()

    def processing_instruction(self, target: str, data: str) -> None:
        self._advance()

    def start_cdata(self) -> None:
        self._advance()

    def skipped_entity(self, name: str, is_parameter_entity: bool) -> None:
        self._advance()

    def _advance(self) -> None:
        """Close the pending element before the event at the current index."""
        pending = self._pending
        if pending is None:
            return
        path, start, anchor = pending
        self._record(path, start, anchor, self._parser.CurrentByteIndex)
        self._pending = None

    def _record(
        self,
        path: tuple[PathStep, ...],
        start: int,
        anchor: int,
        next_index: int,
    ) -> None:
        """Store one span unless its opening offset has no source element."""
        if not self._content.startswith(self._open_marker, start):
            # Markup expanded from an entity declaration has no addressable
            # opening tag; expose no span instead of an empty one.
            return
        self.spans[path] = SourceSpan(start, self._span_end(anchor, next_index))

    def _span_end(self, anchor: int, next_index: int) -> int:
        """Resolve an end event's byte index into an exclusive span end.

        Expat reports ``CurrentByteIndex`` at the ``<`` of an ordinary
        closing tag and just past ``/>`` for a self-closing tag. A following
        construct at the same index therefore proves self-closing (which
        also disambiguates ``<x/></r>``, where the next bytes look like a
        close tag). Skipped trailing whitespace after the root is the only
        content that appears at a later index without its own event, so the
        closing-tag preposition remains the deciding test there.
        """
        if next_index <= anchor:
            return anchor
        if not self._content.startswith(self._close_marker, anchor):
            return anchor
        return self._content.index(self._terminator, anchor) + self._unit


def _expanded_name(raw: str) -> str:
    """Convert an Expat namespaced name to ElementTree's Clark notation."""
    uri, separator, local = raw.partition(_NAMESPACE_SEPARATOR)
    if not separator:
        return raw
    return f"{{{uri}}}{local}"


def _encoding_markers(content: bytes) -> tuple[int, bytes, bytes, bytes]:
    """Return ``(unit width, opening '<', closing '</', '>' marker)``.

    Expat accepts UTF-8, UTF-16, ISO-8859-1, and US-ASCII. Only UTF-16 has
    multi-byte markup; its BOM or leading ``<`` reveals the byte order, and
    every other accepted encoding is ASCII-compatible for markup.
    """
    if content.startswith((b"\xff\xfe", b"<\x00")):
        return 2, b"<\x00", b"<\x00/\x00", b">\x00"
    if content.startswith((b"\xfe\xff", b"\x00<")):
        return 2, b"\x00<", b"\x00<\x00/", b"\x00>"
    return 1, b"<", b"</", b">"


__all__ = ["SourceIndex", "SourceSpan"]
