"""Materialize, parse, and locate an XML document without a schema."""

from __future__ import annotations

import copy
from codecs import BOM_UTF16_BE, BOM_UTF16_LE
from contextlib import suppress
from io import BytesIO
from os import PathLike
from typing import IO
from xml.etree import ElementTree as ET
from xml.parsers import expat

from pyxsd.locations import IssueLocation, PathStep, SourceTextSpan
from pyxsd.namespaces import NamespaceContext, parse_with_namespaces
from pyxsd.resource_policy import ResourcePolicyError
from pyxsd.schemaless.document import SchemalessDocument
from pyxsd.schemaless.errors import SchemalessParseError
from pyxsd.schemaless.tree import SchemalessElement
from pyxsd.source_spans import SourceIndex
from pyxsd.validation import ValidationReport
from pyxsd.xdm.document import CaptureSource
from pyxsd.xml_policy import (
    XmlLimits,
    XmlParserPolicy,
    configure_expat_security,
    enforce_tree_limits,
)


def parse_xml(
    source: str | PathLike[str] | bytes | bytearray | memoryview | IO[str] | IO[bytes] | ET.Element,
    *,
    xml_policy: XmlParserPolicy | None = None,
    xml_limits: XmlLimits | None = None,
) -> SchemalessDocument:
    """Read XML from a path, bytes, stream, or caller-built element.

    Parsed sources retain the same bytes for the source index and XDM capture.
    A caller-built tree has no original byte spans. Malformed XML raises
    :class:`SchemalessParseError`; policy and budget errors propagate unchanged.
    """
    if isinstance(source, ET.Element):
        enforce_tree_limits(source, xml_limits, count_non_element_nodes=False)
        element = copy.deepcopy(source)
        element.tail = None  # A root's tail lies outside its document.
        return _bind_xml(element, xml_policy=xml_policy, xml_limits=xml_limits)
    max_bytes = xml_limits.max_bytes if xml_limits is not None else None
    captured = CaptureSource.from_input(source, max_bytes=max_bytes)
    return _parse_captured_xml(captured, xml_policy=xml_policy, xml_limits=xml_limits)


def _parse_captured_xml(
    captured: CaptureSource,
    *,
    xml_policy: XmlParserPolicy | None,
    xml_limits: XmlLimits | None,
) -> SchemalessDocument:
    """Parse a byte revision with an already resolved source name and URI."""
    limits = xml_limits or XmlLimits()
    policy = xml_policy or XmlParserPolicy.default()
    # CaptureSource enforces the read budget for paths and streams, but
    # trusts already-materialized buffers. Check those before any parse.
    if limits.max_bytes is not None and len(captured.data) > limits.max_bytes:
        raise ResourcePolicyError(f"the XML input exceeds max_bytes={limits.max_bytes}")
    namespaces = NamespaceContext()
    try:
        element = parse_with_namespaces(
            BytesIO(captured.data), namespaces, policy=policy, limits=limits
        )
    except (ET.ParseError, LookupError, ValueError) as exc:
        raise _diagnose_malformed(captured, policy, exc) from exc
    index = SourceIndex(captured.source_name, captured.data, policy=policy, limits=limits)
    return _bind_xml(
        element,
        captured=captured,
        namespaces=namespaces,
        index=index,
        xml_policy=policy,
        xml_limits=limits,
    )


def _bind_xml(
    element: ET.Element,
    *,
    captured: CaptureSource | None = None,
    namespaces: NamespaceContext | None = None,
    index: SourceIndex | None = None,
    xml_policy: XmlParserPolicy | None,
    xml_limits: XmlLimits | None,
) -> SchemalessDocument:
    source_name = captured.source_name if captured is not None else "<memory>"
    base_uri = captured.document_uri if captured is not None else None
    location = IssueLocation(source_name, (PathStep(element.tag, 1),))
    root = SchemalessElement.from_etree(
        element, namespaces=namespaces, base_uri=base_uri, source=location
    )
    return SchemalessDocument(
        root,
        source_name,
        ValidationReport(diagnostics_complete=True),
        source_index=index,
        capture_source=captured,
        xml_policy=xml_policy,
        xml_limits=xml_limits,
    )


class _StopAtEntityReference(Exception):
    """Stop Expat at a reference that ElementTree could not expand."""


def _diagnose_malformed(
    source: CaptureSource, policy: XmlParserPolicy, error: ET.ParseError | LookupError | ValueError
) -> SchemalessParseError:
    # Match ElementTree's namespace and declaration handling. Its first pass
    # checked the start events up to the error against the structural budgets.
    parser = expat.ParserCreate(namespace_separator="}")
    configure_expat_security(parser, policy)
    path: list[PathStep] = []
    counts: list[dict[str, int]] = [{}]
    encoding: str | None = None
    reference_position: tuple[int, int] | None = None

    def start(name: str, attributes: dict[str, str]) -> None:
        expanded = "{" + name if "}" in name else name
        siblings = counts[-1]
        index = siblings.get(expanded, 0) + 1
        siblings[expanded] = index
        path.append(PathStep(expanded, index))
        counts.append({})

    def end(name: str) -> None:
        path.pop()
        counts.pop()

    def declaration(version: str, declared_encoding: str | None, standalone: int) -> None:
        nonlocal encoding
        encoding = declared_encoding

    def stop_at_reference(*args: object) -> int:
        nonlocal reference_position
        # Expat advances past the reference when the callback unwinds.
        reference_position = parser.CurrentByteIndex, parser.CurrentLineNumber
        raise _StopAtEntityReference

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.XmlDeclHandler = declaration
    parser.SkippedEntityHandler = stop_at_reference
    if parser.ExternalEntityRefHandler is None:
        parser.ExternalEntityRefHandler = stop_at_reference
    with suppress(expat.ExpatError, LookupError, ValueError, _StopAtEntityReference):
        parser.Parse(source.data, True)

    byte_offset, line = reference_position or (parser.ErrorByteIndex, parser.ErrorLineNumber)
    if isinstance(error, ET.ParseError):
        message = expat.ErrorString(error.code)
    else:
        # The encoding label may not be a usable codec; the prefix at this
        # failure is the ASCII declaration (or its sniffable UTF-16 form).
        encoding = None
        message = "unsupported XML encoding"
    span = _error_span(source.data, byte_offset, line, encoding)
    location = IssueLocation(source.source_name, tuple(path), span=span)
    return SchemalessParseError(
        message, location=location, open_elements=tuple(step.name for step in path)
    )


def _error_span(
    content: bytes, byte_offset: int, line: int, encoding: str | None
) -> SourceTextSpan:
    """Convert Expat's byte position into a point in the decoded source."""
    if content.startswith((BOM_UTF16_LE, b"<\x00")):
        encoding = "utf-16-le"
    elif content.startswith((BOM_UTF16_BE, b"\x00<")):
        encoding = "utf-16-be"
    prefix = (
        content[:byte_offset].decode(encoding or "utf-8", errors="ignore").removeprefix("\ufeff")
    )
    offset = len(prefix)
    # Preserve both characters of CRLF in the offset, but count columns only
    # from the last XML newline. A leading BOM is not a source character.
    column = offset - max(prefix.rfind("\r"), prefix.rfind("\n"))
    return SourceTextSpan(offset, offset, line, column, line, column)
