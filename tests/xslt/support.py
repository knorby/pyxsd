"""Shared helpers for XSLT test modules.

The helpers preserve the small, explicit stylesheet and transformation shapes
used by the existing test families.  Namespace declarations are supplied by
the caller so a family keeps its own vocabulary without a second implementation
of the surrounding stylesheet wrapper.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xslt.processor import XsltProcessor

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"

_NO_NAMESPACES: Mapping[str, str] = MappingProxyType({})


def stylesheet(
    body: str,
    *,
    version: str = "3.0",
    attributes: str = "",
    namespaces: Mapping[str, str] = _NO_NAMESPACES,
) -> bytes:
    """Wrap *body* in one XSLT stylesheet with the given declarations."""
    declarations = "".join(f' xmlns:{prefix}="{uri}"' for prefix, uri in namespaces.items())
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}"'
        f'{declarations} version="{version}"{attributes}>'
        f"{body}</xsl:stylesheet>"
    ).encode()


def transform(
    source: bytes,
    input_source: bytes | XdmDocument | None = None,
    *,
    delivery: str = "document",
    **options: Any,
) -> Any:
    """Compile *source* and run it with the test's delivery and options."""
    compiled = XsltProcessor().compile(io.BytesIO(source))
    if input_source is None:
        return compiled.transform(delivery=delivery, **options)
    return compiled.transform(source=input_source, delivery=delivery, **options)


def element_children(node: Any) -> tuple[XdmNode, ...]:
    """The element children of *node*, in document order."""
    return tuple(child for child in node.children if child.kind is NodeKind.ELEMENT)
