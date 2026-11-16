"""Schema-neutral lexical XML serialization and cloning for RELAX NG trees.

The writer emits only the recorded expanded names, lexical attributes, text,
tails, and namespace declarations needed to round-trip the tree.  It never
routes through :class:`pyxsd.writers.xml_tree_writer.XmlTreeWriter`, so it adds
no XSD type, default, namespace, or ``xsi:*`` behavior.  Output is compact:
generated indentation would become character data in mixed content.
"""

from __future__ import annotations

from typing import cast

from pyxsd.namespaces import XML_NS, local_name, namespace_of
from pyxsd.relaxng.tree import LexicalRevision, NodeContext, RelaxNGElement
from pyxsd.writers.xml_tag_writer import _escape_attribute, _escape_text

__all__ = ["clone_relaxng", "serialize_relaxng"]

_Operation = tuple[str, "RelaxNGElement | str", "dict[str, str] | None"]


def serialize_relaxng(root: RelaxNGElement) -> str:
    """Serialize a lexical RELAX NG subtree as a well-formed XML fragment.

    Expanded names become lexical names using the element's recorded namespace
    frame.  Bindings that are absent from the recorded frame are generated
    deterministically.  Recorded declarations are emitted where they differ
    from the output's inherited scope, so prefixed QNames in lexical values
    still resolve to the same namespace URI after reparsing.

    The result carries no XML declaration and no formatting whitespace.  It
    does not add ``xsi:*`` attributes or schema defaults.
    """
    if not isinstance(root, RelaxNGElement):
        raise TypeError("root must be a RelaxNGElement")
    output: list[str] = []
    operations: list[_Operation] = [("element", root, {})]
    while operations:
        kind, payload, inherited = operations.pop()
        if kind == "text":
            output.append(_escape_text(payload))
            continue
        if kind == "close":
            output.append(f"</{payload}>")
            continue
        node = cast(RelaxNGElement, payload)
        frame = dict(cast("dict[str, str]", inherited))
        declarations: dict[str, str] = {}
        _apply_recorded_frame(node, frame, declarations)
        lexical_name = _element_name(node._name_, frame, declarations)
        attributes = [
            (
                _attribute_name(expanded, frame, declarations),
                value,
            )
            for expanded, value in sorted(node._attribs_.items())
        ]
        output.append(f"<{lexical_name}")
        for prefix, uri in declarations.items():
            spelling = f"xmlns:{prefix}" if prefix else "xmlns"
            output.append(f' {spelling}="{_escape_attribute(uri)}"')
        for name, value in attributes:
            output.append(f' {name}="{_escape_attribute(value)}"')
        if node._value_ is None and not node._children_:
            output.append("/>")
            continue
        output.append(">")
        operations.append(("close", lexical_name, None))
        for child in reversed(node._children_):
            if child._tail_ is not None:
                operations.append(("text", child._tail_, None))
            operations.append(("element", child, frame))
        text = node.text_value()
        if text is not None:
            operations.append(("text", text, None))
    return "".join(output)


def clone_relaxng(root: RelaxNGElement) -> RelaxNGElement:
    """Return an independent lexical copy of *root* with fresh contexts.

    The clone belongs to a new revision with a new revision ID and copies the
    source dirty flag.  Namespace frames, base URIs, and source occurrences are
    copied so revalidation does not lose external lexical context.  The copy is
    iterative, so deeply nested documents do not consume the Python stack.
    """
    if not isinstance(root, RelaxNGElement):
        raise TypeError("root must be a RelaxNGElement")
    source_revision = root.revision
    revision = LexicalRevision()
    if source_revision.dirty:
        revision.mark_dirty()
    clone_root = RelaxNGElement._new(
        root._name_,
        revision,
        attributes=root._attribs_,
        text=root.text_value(),
        tail=root._tail_,
    )
    revision._adopt_root(clone_root)
    stack: list[tuple[RelaxNGElement, RelaxNGElement]] = [(root, clone_root)]
    while stack:
        source, target = stack.pop()
        revision._register_context(target, _copy_context(source.revision.context_for(source)))
        for source_child in source._children_:
            child = RelaxNGElement._new(
                source_child._name_,
                revision,
                attributes=source_child._attribs_,
                text=source_child.text_value(),
                tail=source_child._tail_,
            )
            target._children_.append(child)
            child._parent_ = target
            stack.append((source_child, child))
    return clone_root


def _copy_context(context: NodeContext) -> NodeContext:
    namespaces = None if context.namespaces is None else dict(context.namespaces)
    return NodeContext(namespaces, context.base_uri, context.source)


def _declaration_key(item: tuple[str, str]) -> tuple[bool, str]:
    return (item[0] != "", item[0])


def _apply_recorded_frame(
    node: RelaxNGElement, frame: dict[str, str], declarations: dict[str, str]
) -> None:
    recorded = node.revision.context_for(node).namespaces
    if recorded is None:
        return
    for prefix, uri in sorted(recorded.items(), key=_declaration_key):
        if prefix == "xml":
            if uri != XML_NS:
                raise ValueError("the xml prefix cannot be rebound")
            continue
        if prefix == "" and uri == "" and "" not in frame:
            continue
        if frame.get(prefix) == uri:
            continue
        declarations[prefix] = uri
        frame[prefix] = uri


def _element_name(name: str, frame: dict[str, str], declarations: dict[str, str]) -> str:
    uri = namespace_of(name)
    if uri is None:
        if ":" not in name:
            _undeclare_default(frame, declarations)
        return name
    local = local_name(name)
    if uri == "":
        _undeclare_default(frame, declarations)
        return local
    if uri == XML_NS:
        return f"xml:{local}"
    prefix = _bound_prefix(frame, uri, allow_default=True)
    if prefix is None:
        prefix = _generate_prefix(frame, uri, declarations)
    return f"{prefix}:{local}" if prefix else local


def _attribute_name(name: str, frame: dict[str, str], declarations: dict[str, str]) -> str:
    uri = namespace_of(name)
    if uri is None:
        return name
    local = local_name(name)
    if uri == "":
        return local
    if uri == XML_NS:
        return f"xml:{local}"
    prefix = _bound_prefix(frame, uri, allow_default=False)
    if prefix is None:
        prefix = _generate_prefix(frame, uri, declarations)
    return f"{prefix}:{local}"


def _undeclare_default(frame: dict[str, str], declarations: dict[str, str]) -> None:
    if frame.get("", "") != "":
        declarations[""] = ""
        frame[""] = ""


def _bound_prefix(frame: dict[str, str], uri: str, *, allow_default: bool) -> str | None:
    candidates = sorted(
        (prefix for prefix, bound in frame.items() if bound == uri and (allow_default or prefix)),
        key=lambda prefix: (prefix != "", prefix),
    )
    return candidates[0] if candidates else None


def _generate_prefix(frame: dict[str, str], uri: str, declarations: dict[str, str]) -> str:
    index = 0
    while True:
        prefix = f"ns{index}"
        bound = frame.get(prefix)
        if bound is None or bound == uri:
            break
        index += 1
    declarations[prefix] = uri
    frame[prefix] = uri
    return prefix
