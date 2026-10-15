"""Dict/JSON export of a bound document tree.

See the export convention in ``docs/data-model.md``: child elements become
keys (Clark name when namespace-qualified), attributes are prefixed with
``@``, element text is stored under ``$``, and a repeated child becomes a
list. Primitive nodes are the typed XSD scalar (an ``int``/``str``/...
subclass), so ``typed=True`` passes those through and ``typed=False``
falls back to the lexical text.

For an element whose content admits character data (mixed content, the
ur-type, wildcard pass-through), ``$`` holds all of the element's
recorded character data in document order: the leading text plus every
child's ``_tail_``, concatenated. The dict shape cannot represent
text/element interleaving; ``to_string()`` and ``xpath()`` are the
faithful mixed-content views. Element-only content keeps the legacy
export and ignores its children's whitespace tails, which are
insignificant there.
"""

from typing import Any

from pyxsd.xsd_data_types import XsdDataType

#: Key holding an element's character data when it also has attributes or
#: child elements (a text-only element exports the scalar directly).
TEXT_KEY = "$"

_COMMENT_NAME = "_comment_"


def _node_name(node: Any) -> str:
    return getattr(node, "_name_", None) or ""


def _text_of(node: Any) -> str | None:
    """The node's character data, joined from the writer's value pieces."""
    value = getattr(node, "_value_", None)
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return "".join(str(piece) for piece in value)
    return str(value)


def _leaf_value(node: Any, *, typed: bool) -> Any:
    if getattr(node, "_nil_", False):
        return None
    if typed and isinstance(node, XsdDataType):
        return node
    text = _text_of(node)
    if text is None:
        return None if typed else ""
    return text


def _export_node(node: Any, *, typed: bool, always_list: bool) -> Any:
    """Exports one bound node, applying the module's convention.

    A character-data-admitting element whose children carry ``_tail_``
    values exports all of its recorded text under ``$``, in document
    order.
    """
    children = [
        child
        for child in (getattr(node, "_children_", None) or [])
        if _node_name(child) != _COMMENT_NAME
    ]
    attribs = getattr(node, "_attribs_", None) or {}
    if not children and not attribs:
        return _leaf_value(node, typed=typed)

    result: dict[str, Any] = {}
    for key, raw in attribs.items():
        if typed:
            # A declared attribute's typed value lives in the instance
            # dictionary; a wildcard attribute has only its lexical form.
            typed_value = node.__dict__.get(key)
            result["@" + key] = raw if typed_value is None else typed_value
        else:
            result["@" + key] = raw

    groups: dict[str, list[Any]] = {}
    for child in children:
        exported = _export_node(child, typed=typed, always_list=always_list)
        groups.setdefault(_node_name(child), []).append(exported)
    for name, values in groups.items():
        result[name] = values if always_list else (values[0] if len(values) == 1 else values)

    text = _text_of(node)
    tails = [
        tail for tail in (getattr(child, "_tail_", None) for child in children) if tail is not None
    ]
    if tails and getattr(node, "_elementOnly_", True) is False:
        pieces = [] if text is None else [text]
        pieces.extend(tails)
        result[TEXT_KEY] = "".join(pieces)
    elif text is not None:
        result[TEXT_KEY] = text
    return result


def bound_to_dict(root: Any, *, typed: bool = True, always_list: bool = False) -> dict:
    """Exports the bound tree rooted at ``root`` as a plain dict.

    - ``typed``: keep typed Python scalar values; otherwise use the
      lexical text.
    - ``always_list``: wrap every child value in a list, even a single
      occurrence.

    A primitive root is returned under ``"$"``. For character-data-
    admitting content ``$`` keeps every recorded text piece in document
    order; element-only content keeps the legacy export.
    """
    exported = _export_node(root, typed=typed, always_list=always_list)
    if isinstance(exported, dict):
        return exported
    return {TEXT_KEY: exported}
