"""Render a BPMN 2.0 diagram from its interchange (DI) geometry.

The semantic model says *what* a shape is; the ``BPMNDiagram`` section says
*where* it goes. The renderer therefore reads the document twice: the
semantic tree supplies each element's tag (task, gateway, event) and name,
and the DI tree supplies ``dc:Bounds`` and ``di:waypoint`` coordinates.
BPMN DI is authoritative -- no auto-layout is attempted.

Run from a pyxsd checkout after installing the pinned sources::

    uv run python examples/showcase/bpmn/render_svg.py <file.bpmn> [-o out.svg]
"""

from __future__ import annotations

BPMN_MODEL = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMN_DI = "http://www.omg.org/spec/BPMN/20100524/DI"
DC = "http://www.omg.org/spec/DD/20100524/DC"
DD_DI = "http://www.omg.org/spec/DD/20100524/DI"

NAMESPACES = {"bpmn": BPMN_MODEL, "bpmndi": BPMN_DI, "dc": DC, "di": DD_DI}

#: Element tags that render as rounded rectangles.
_TASK_TAGS = frozenset(
    {
        "task",
        "userTask",
        "serviceTask",
        "sendTask",
        "receiveTask",
        "manualTask",
        "scriptTask",
        "businessRuleTask",
        "callActivity",
    }
)
_SUBPROCESS_TAGS = frozenset({"subProcess", "adHocSubProcess", "transaction"})

_PAD = 12.0


def _kind_for(tag: str) -> str:
    """Map a semantic element tag to the glyph it should render as."""
    if tag in _TASK_TAGS:
        return "task"
    if tag in _SUBPROCESS_TAGS:
        return "subprocess"
    if tag.endswith("Gateway"):
        return "gateway"
    if tag == "startEvent":
        return "start_event"
    if tag == "endEvent":
        return "end_event"
    if tag.endswith("Event"):
        return "event"
    if tag == "lane":
        return "lane"
    if tag == "participant":
        return "participant"
    return "other"


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else [value]


def _semantic_index(document) -> dict[str, dict[str, str]]:
    """Index semantic elements by id: ``{id: {"kind": ..., "name": ...}}``.

    ``document.to_dict()`` uses Clark-notation keys. Child attribute access
    on this ref-based schema returns ``None``, so the index is built from the
    exported mapping rather than from bound attribute descriptors.
    """
    index: dict[str, dict[str, str]] = {}

    def walk(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if not key.startswith("{"):
                continue
            namespace, _, tag = key[1:].partition("}")
            if namespace != BPMN_MODEL:
                continue
            for child in _as_list(value):
                if not isinstance(child, dict):
                    continue
                identifier = child.get("@id")
                if isinstance(identifier, str):
                    index[identifier] = {
                        "kind": _kind_for(tag),
                        "name": str(child.get("@name") or ""),
                    }
                walk(child)

    walk(document.to_dict())
    return index


def _attr(element: object, name: str) -> str:
    value = element._attribs_.get(name)  # type: ignore[attr-defined]
    return "" if value is None else str(value)


def _bounds(shape: object) -> tuple[float, float, float, float] | None:
    for child in shape._children_:  # type: ignore[attr-defined]
        if child._name_ != f"{{{DC}}}Bounds":
            continue
        attributes = child._attribs_
        try:
            return (
                float(attributes["x"]),
                float(attributes["y"]),
                float(attributes["width"]),
                float(attributes["height"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
    return None


def di_shapes(document) -> list[dict]:
    """Return one record per ``BPMNDiagram`` shape, in document order.

    ``element_id`` is the semantic id referenced by ``BPMNShape/@bpmnElement``
    (not the shape's own DI id), so callers can join geometry back to the
    process model. ``kind`` classifies the referenced element.
    """
    index = _semantic_index(document)
    shapes: list[dict] = []
    for shape in document.xpath("//bpmndi:BPMNShape", namespaces=NAMESPACES):
        element_id = _attr(shape, "bpmnElement") or _attr(shape, "id")
        bounds = _bounds(shape)
        if bounds is None:
            continue
        semantic = index.get(element_id, {})
        x, y, width, height = bounds
        shapes.append(
            {
                "element_id": element_id,
                "kind": semantic.get("kind", "other"),
                "x": x,
                "y": y,
                "w": width,
                "h": height,
                "is_horizontal": _attr(shape, "isHorizontal").lower() in {"true", "1"},
                "label": semantic.get("name", ""),
            }
        )
    return shapes


def di_edges(document) -> list[dict]:
    """Return one record per ``BPMNEdge`` with its absolute waypoints."""
    index = _semantic_index(document)
    edges: list[dict] = []
    for edge in document.xpath("//bpmndi:BPMNEdge", namespaces=NAMESPACES):
        element_id = _attr(edge, "bpmnElement") or _attr(edge, "id")
        waypoints: list[tuple[float, float]] = []
        for child in edge._children_:
            if child._name_ != f"{{{DD_DI}}}waypoint":
                continue
            attributes = child._attribs_
            try:
                waypoints.append((float(attributes["x"]), float(attributes["y"])))
            except (KeyError, TypeError, ValueError):
                continue
        edges.append(
            {
                "element_id": element_id,
                "waypoints": waypoints,
                "label": index.get(element_id, {}).get("name", ""),
            }
        )
    return edges
