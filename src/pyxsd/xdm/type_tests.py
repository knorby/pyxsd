"""Schema-aware kind-test and type-derivation predicates.

A kind test asks two independent questions: does the node have the kind and
name the test spells, and does its *type annotation* satisfy the test's type
clause?  XPath 3.1 section 2.5.5.3 defines the element and attribute type
clause as ``derives-from(AT, TypeName)`` where ``AT`` is the node's type
annotation; section 2.5.5.4 adds substitution-group membership and the
candidate declaration's ``nillable`` property for ``schema-element``.

The same predicates serve the XSLT pattern compiler (``match="element(...)"``),
the XPath expression kind tests installed on the elementpath parser, and the
declared sequence types checked by the processor (``as=``, ``treat as``,
function conversion).  Keeping one implementation here is what makes those
three surfaces agree.

This module depends only on the XDM node layer and the schema pool's public
lookup methods; it never imports the XSLT compiler or the XPath engine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyxsd.namespaces import local_name, namespace_of
from pyxsd.xdm.nodes import NodeKind, XdmNode

if TYPE_CHECKING:
    from pyxsd.xdm.schema import SchemaPool

XSD_NS = "http://www.w3.org/2001/XMLSchema"


def builtin_class(type_name: str) -> type[Any] | None:
    """The generated class for one built-in XSD type name, if there is one."""
    if namespace_of(type_name) != XSD_NS:
        return None
    from pyxsd.xdm.annotations import builtin_type_classes

    return builtin_type_classes().get(local_name(type_name))


def builtin_derives(actual_name: str | None, declared_name: str) -> bool:
    """Whether one built-in type name derives from another by name alone."""
    if actual_name is None:
        return False
    actual_local = local_name(actual_name)
    declared_local = local_name(declared_name)
    if declared_local == "anyType":
        return True
    if actual_local == declared_local:
        return True
    if declared_local == "anySimpleType":
        return actual_local not in {"anyType", "untyped"}
    if declared_local == "anyAtomicType":
        return actual_local not in {"anyType", "untyped", "anySimpleType"}
    actual_class = builtin_class(actual_name)
    declared_class = builtin_class(declared_name)
    if isinstance(actual_class, type) and isinstance(declared_class, type):
        return issubclass(actual_class, declared_class)
    return False


def derives_from(
    annotation: Any,
    declared_name: str,
    schema: SchemaPool | None,
) -> bool:
    """Whether a node's type annotation is *declared_name* or derives from it.

    ``annotation.actual_type`` is the generated class that governs a validated
    occurrence, including anonymous local types; unvalidated nodes carry only
    the ``type_name`` string.  A class comparison is authoritative when both
    sides have a class, because generated classes encode the derivation chain;
    the built-in name table is the fallback.
    """
    actual_name = getattr(annotation, "type_name", None)
    if actual_name == declared_name:
        return True
    actual_class = getattr(annotation, "actual_type", None)
    declared_class = declared_class_for(declared_name, schema)
    if isinstance(actual_class, type) and isinstance(declared_class, type):
        return issubclass(actual_class, declared_class)
    return builtin_derives(actual_name, declared_name)


def declared_class_for(declared_name: str, schema: SchemaPool | None) -> type[Any] | None:
    """The generated class of one declared type, from the pool or the builtins."""
    declared = schema.get_type(declared_name) if schema is not None else None
    if declared is not None:
        return getattr(declared, "_type_class", None)
    return builtin_class(declared_name)


def declaration_key(declaration: Any, *, require_global: bool) -> str | None:
    """The expanded name of a governing element/attribute declaration."""
    if declaration is None:
        return None
    name = getattr(declaration, "name", None)
    if not isinstance(name, str) or not name:
        return None
    if name.startswith("{"):
        return name
    if require_global:
        is_global = getattr(declaration, "isGlobalDeclaration", None)
        if callable(is_global) and not is_global():
            return None
    get_namespace = getattr(declaration, "getNamespace", None)
    namespace = get_namespace() if callable(get_namespace) else None
    if namespace is None:
        parent = getattr(declaration, "parent", None)
        namespace = getattr(parent, "target_namespace", None)
    return f"{{{namespace}}}{name}" if namespace else name


def declaration_matches(
    node: XdmNode,
    declarations: tuple[str, ...],
    kind: str,
) -> bool:
    """Whether a node's governing declaration is one of *declarations*."""
    declaration = node.annotation.governing_declaration
    key = declaration_key(declaration, require_global=kind == "schema-element")
    if key is None or key not in declarations:
        return False
    if kind == "schema-attribute":
        return node.kind is NodeKind.ATTRIBUTE and node.name in declarations
    return node.kind is NodeKind.ELEMENT


def element_declaration_names(schema: SchemaPool, name: str) -> tuple[str, ...]:
    """The declaration names accepted by ``schema-element(name)``.

    XPath 3.1 section 2.5.5.4 includes direct and indirect members of the
    substitution group, while the schema pool exposes only direct edges.
    """
    names = {name}
    pending = {name}
    while pending:
        discovered: set[str] = set()
        for current in pending:
            for member in schema.get_substitution_group(current) or frozenset():
                assert member.name is not None
                discovered.add(member.name)
        pending = discovered - names
        names.update(pending)
    return tuple(sorted(names))
