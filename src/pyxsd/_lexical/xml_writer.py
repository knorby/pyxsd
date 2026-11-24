"""Schema-neutral lexical XML serialization and cloning.

The writer emits only the recorded expanded names, lexical attributes, text,
tails, and namespace declarations needed to round-trip the tree.  It never
routes through :class:`pyxsd.writers.xml_tree_writer.XmlTreeWriter`, so it adds
no XSD type, default, namespace, or ``xsi:*`` behavior.  Output is compact:
generated indentation would become character data in mixed content.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TypeVar, cast

from pyxsd._lexical.tree import LexicalElement
from pyxsd.namespaces import XML_NS, local_name, namespace_of
from pyxsd.writers.xml_tag_writer import _escape_attribute, _escape_text

__all__ = ["clone_lexical", "serialize_lexical"]

_NodeT = TypeVar("_NodeT", bound=LexicalElement)
_Operation = tuple[str, "LexicalElement | str", "dict[str, str] | None"]


def serialize_lexical(root: LexicalElement) -> str:
    """Serialize a lexical subtree as a well-formed XML fragment.

    Expanded names become lexical names using the element's recorded namespace
    frame. An explicit name preference takes precedence if still bound to
    the name's namespace. Missing bindings are generated deterministically.
    Recorded declarations are emitted where they differ
    from the output's inherited scope, so prefixed QNames in lexical values
    still resolve to the same namespace URI after reparsing.

    The result carries no XML declaration and no formatting whitespace.  It
    does not add ``xsi:*`` attributes or schema defaults.
    """
    if not isinstance(root, LexicalElement):
        raise TypeError("root must be a LexicalElement")
    output: list[str] = []
    operations: list[_Operation] = [("element", root, {})]
    while operations:
        kind, payload, inherited = operations.pop()
        if kind == "text":
            output.append(_escape_text(cast(str, payload)))
            continue
        if kind == "close":
            output.append(f"</{payload}>")
            continue
        node = cast(LexicalElement, payload)
        frame = dict(cast("dict[str, str]", inherited))
        declarations: dict[str, str] = {}
        context = node.revision.context_for(node)
        prefixes = context.name_prefixes or {}
        _apply_recorded_frame(context.namespaces, frame, declarations)
        lexical_name = _element_name(node._name_, frame, declarations, prefixes.get(None))
        attributes = [
            (
                _attribute_name(expanded, frame, declarations, prefixes.get(expanded)),
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


def clone_lexical(root: _NodeT) -> _NodeT:
    """Return an independent lexical copy of *root* with fresh contexts.

    The clone starts with its node family's default revision and copies the
    source dirty flag, not the source's typed interpretation. Namespace frames,
    name prefixes, base URIs, and source occurrences are copied. The copy is
    iterative, so deeply nested documents do not consume the Python stack.
    """
    if not isinstance(root, LexicalElement):
        raise TypeError("root must be a LexicalElement")
    source_revision = root.revision
    revision = type(root)._create_revision()
    if source_revision.dirty:
        revision.mark_dirty()
    clone_root = type(root)._clone_new(root, revision)
    revision._adopt_root(clone_root)
    stack: list[tuple[LexicalElement, LexicalElement]] = [(root, clone_root)]
    while stack:
        source, target = stack.pop()
        revision._register_context(target, replace(source.revision.context_for(source)))
        for source_child in source._children_:
            child = type(source_child)._clone_new(source_child, revision)
            target._children_.append(child)
            child._parent_ = target
            stack.append((source_child, child))
    return clone_root


def _declaration_key(item: tuple[str, str]) -> tuple[bool, str]:
    return (item[0] != "", item[0])


def _apply_recorded_frame(
    recorded: Mapping[str, str] | None, frame: dict[str, str], declarations: dict[str, str]
) -> None:
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


def _element_name(
    name: str, frame: dict[str, str], declarations: dict[str, str], preferred: str | None
) -> str:
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
    prefix = _bound_prefix(frame, uri, preferred, allow_default=True)
    if prefix is None:
        prefix = _generate_prefix(frame, uri, declarations)
    return f"{prefix}:{local}" if prefix else local


def _attribute_name(
    name: str, frame: dict[str, str], declarations: dict[str, str], preferred: str | None
) -> str:
    uri = namespace_of(name)
    if uri is None:
        return name
    local = local_name(name)
    if uri == "":
        return local
    if uri == XML_NS:
        return f"xml:{local}"
    prefix = _bound_prefix(frame, uri, preferred or None, allow_default=False)
    if prefix is None:
        prefix = _generate_prefix(frame, uri, declarations)
    return f"{prefix}:{local}"


def _undeclare_default(frame: dict[str, str], declarations: dict[str, str]) -> None:
    if frame.get("", "") != "":
        declarations[""] = ""
        frame[""] = ""


def _bound_prefix(
    frame: dict[str, str], uri: str, preferred: str | None, *, allow_default: bool
) -> str | None:
    if preferred is not None and frame.get(preferred) == uri:
        return preferred
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
