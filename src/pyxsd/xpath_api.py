"""Public document XPath: query a projection of the bound tree.

The projection is an ``xml.etree.ElementTree`` view built from the same
containers the writer serializes, so it reflects committed mutations. It
exists only to run XPath/ElementPath; results are mapped back to the
original bound nodes, and scalar results pass through unchanged.

The projection is deliberately lossy relative to the source XML: it holds
what the bound tree holds. Mixed-content tails and comment nodes are not
present, an element with ``_value_ is None`` projects ``text=None``, and
descriptor assignment is reflected only through the write-through
containers (see ``docs/data-model.md``).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

import elementpath

from pyxsd.namespaces import clark


def _expanded_name(node: Any) -> str | None:
    """The Clark-notation name for a bound node.

    ``_name_`` is the *matching* name, which is the local name in legacy
    namespace mode even for a namespaced declaration; the declaration
    behind the node knows the namespace, so the projection can still tag
    nodes with their expanded name. A local declaration with an
    unqualified form has no namespace and keeps its local name.
    """
    name = getattr(node, "_name_", None)
    if name is None:
        return None
    if name.startswith("{"):
        return name
    descriptor = getattr(node, "_descriptor_", None)
    get_namespace = getattr(descriptor, "getNamespace", None)
    uri = get_namespace() if callable(get_namespace) else None
    return clark(uri, name) if uri else name


def select(root: ET.Element, expr: str, *, namespaces: dict[str, str] | None = None) -> Any:
    """Evaluates *expr* over the projection, returning elementpath's result."""
    return elementpath.select(root, expr, namespaces=namespaces)


def _text_of(node: Any) -> str | None:
    """ElementTree text for a bound node.

    A bound node's ``_value_`` is a list of text pieces (or ``None`` for
    element-only content); ElementTree wants a single string.
    """
    raw = getattr(node, "_value_", None)
    if raw is None:
        return None
    if isinstance(raw, list):
        joined = "".join(str(piece) for piece in raw)
        return joined or None
    return str(raw)


def project(root: Any) -> tuple[ET.Element, dict[ET.Element, Any]]:
    """Builds an ElementTree projection of the bound tree.

    Returns ``(et_root, mapping)`` where ``mapping`` maps each projection
    element to the original bound node. Tags use the node's instance name
    (Clark notation in strict namespace mode, local names otherwise);
    ``attrib`` mirrors ``_attribs_`` and ``text`` mirrors ``_value_``.
    Non-element children (comments) are skipped.
    """
    mapping: dict[ET.Element, Any] = {}

    def build(node: Any) -> ET.Element | None:
        name = _expanded_name(node)
        if name is None:
            return None
        attribs = getattr(node, "_attribs_", None) or {}
        element = ET.Element(name, {key: str(value) for key, value in attribs.items()})
        element.text = _text_of(node)
        mapping[element] = node
        for child in getattr(node, "_children_", None) or []:
            built = build(child)
            if built is not None:
                element.append(built)
        return element

    et_root = build(root)
    if et_root is None:
        raise ValueError("project() requires an element node")
    return et_root, mapping
