"""Schema-aware kind-test and type-derivation predicates.

A kind test asks two independent questions: does the node have the kind and
name the test spells, and does its *type annotation* satisfy the test's type
clause?  XPath 3.1 section 2.5.5.3 defines the element and attribute type
clause as ``derives-from(AT, TypeName)`` where ``AT`` is the node's type
annotation; section 2.5.5.4 adds substitution-group membership and the named
declaration's ``nillable`` property for ``schema-element``.

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

from pyxsd import xsd_data_types as xsd
from pyxsd.derivation import blockTokens, is_valid_xsi_type, is_validly_derived
from pyxsd.namespaces import local_name, namespace_of
from pyxsd.xdm.nodes import NodeKind, XdmNode

if TYPE_CHECKING:
    from pyxsd.xdm.schema import SchemaPool

XSD_NS = "http://www.w3.org/2001/XMLSchema"
UNTYPED = f"{{{XSD_NS}}}untyped"
UNTYPED_ATOMIC = f"{{{XSD_NS}}}untypedAtomic"
ANY_TYPE = f"{{{XSD_NS}}}anyType"


def builtin_class(type_name: str) -> type[Any] | None:
    """The generated class for one built-in XSD type name, if there is one."""
    if namespace_of(type_name) != XSD_NS:
        return None
    from pyxsd.xdm.annotations import builtin_type_classes

    return builtin_type_classes().get(local_name(type_name))


def builtin_derives(actual_name: str | None, declared_name: str) -> bool:
    """Whether one built-in type name derives from another by name alone.

    ``xs:anyType`` is the base of every type, ``xs:anySimpleType`` of every
    simple type and ``xs:anyAtomicType`` of every atomic type (XDM 3.1 section
    2.7.2 and XSD 1.1 Part 2 section 3.3).  Below those ur-types the generated classes
    do not form one Python hierarchy, so :func:`is_validly_derived` supplies
    the XSD corrections (``xs:integer`` derives from ``xs:decimal``,
    ``xs:boolean`` does not derive from ``xs:integer``, and the string
    primitives do not derive from ``xs:string``).
    """
    if actual_name is None:
        return False
    if actual_name == declared_name:
        return True
    actual_local = local_name(actual_name)
    declared_local = local_name(declared_name)
    if actual_local == declared_local:
        return True
    if namespace_of(declared_name) == XSD_NS:
        if declared_local == "anyType":
            return True
        if declared_local == "anySimpleType":
            return actual_local not in {"anyType", "untyped"}
        if declared_local == "anyAtomicType":
            return actual_local not in {"anyType", "untyped", "anySimpleType"}
    actual_class = builtin_class(actual_name)
    declared_class = builtin_class(declared_name)
    if isinstance(actual_class, type) and isinstance(declared_class, type):
        return is_validly_derived(actual_class, declared_class) is None
    return False


def derives_from(
    annotation: Any,
    declared_name: str,
    schema: SchemaPool | None,
) -> bool:
    """Whether a node's type annotation is *declared_name* or derives from it.

    ``annotation.actual_type`` is the generated class that governs a validated
    occurrence, including anonymous local types; unvalidated nodes carry only
    the ``type_name`` string ``xs:untyped`` (elements) or ``xs:untypedAtomic``
    (attributes).  XDM 3.1 section 2.7.2 says no predefined type derives from
    ``xs:untyped``, so an untyped element matches only ``xs:untyped`` and its
    base ``xs:anyType``; an untyped attribute matches ``xs:untypedAtomic`` and
    the ``xs:anyAtomicType``/``xs:anySimpleType``/``xs:anyType`` chain.  The
    value-space of the node is never consulted.
    """
    actual_name = getattr(annotation, "type_name", None)
    if actual_name == declared_name:
        return True
    actual_class = getattr(annotation, "actual_type", None)
    if not isinstance(actual_class, type):
        if actual_name == UNTYPED:
            return declared_name in (UNTYPED, ANY_TYPE)
        if actual_name == UNTYPED_ATOMIC:
            return declared_name in (
                UNTYPED_ATOMIC,
                f"{{{XSD_NS}}}anyAtomicType",
                f"{{{XSD_NS}}}anySimpleType",
                ANY_TYPE,
            )
        actual_class = _class_for_name(actual_name, schema)
        if actual_class is None:
            return builtin_derives(actual_name, declared_name)
    return _class_derives_from(actual_class, declared_name, schema)


def _class_for_name(name: str | None, schema: SchemaPool | None) -> type[Any] | None:
    """The generated class for one type annotation name, when it is known."""
    if name is None:
        return None
    adapter = schema.get_type(name) if schema is not None else None
    if adapter is not None:
        return getattr(adapter, "_type_class", None)
    return builtin_class(name)


def _class_derives_from(
    actual_class: type[Any],
    declared_name: str,
    schema: SchemaPool | None,
) -> bool:
    declared_class = declared_class_for(declared_name, schema)
    if declared_class is None:
        return False
    return _class_derives_from_class(actual_class, declared_class, schema)


def _class_derives_from_class(
    actual_class: type[Any],
    declared_class: type[Any],
    schema: SchemaPool | None,
) -> bool:
    if declared_class is xsd.AnyType:
        return True
    if declared_class in (xsd.AnySimpleType, xsd.AnyAtomicType):
        # The ur-type clauses are not Python inheritance: a list type is
        # simple but not atomic even though it is a String subclass.
        adapter = schema.type_for_class(actual_class) if schema is not None else None
        if adapter is not None:
            if declared_class is xsd.AnyAtomicType:
                return adapter.is_atomic()
            return adapter.is_simple()
        return is_valid_xsi_type(actual_class, declared_class) is None
    # XPath 3.1 section 2.5.5's derives-from accepts the union clause only for
    # a *pure* union type; the xsi:type relation is wider.
    if (
        schema is not None
        and _is_union(declared_class)
        and not _is_pure_union(declared_class, schema)
    ):
        return False
    return is_validly_derived(actual_class, declared_class) is None


def _is_union(type_class: type[Any]) -> bool:
    return bool(getattr(type_class, "_unionMembers", None))


def _is_pure_union(type_class: type[Any], schema: SchemaPool) -> bool:
    """XPath 3.1's pure union type: a union definition whose members are not lists.

    A union derived by restriction inherits ``_unionMembers`` through its MRO
    but is not itself a union definition (its facets are non-empty), and a
    member that is a list (or a restricted union) makes the whole union
    impure.  Such a union is still a valid ``xsi:type`` base, but the
    ``derives-from`` union clause does not apply to it.
    """
    members = vars(type_class).get("_unionMembers")
    if not members:
        return False
    for member in members:
        adapter = schema.type_for_class(member)
        if adapter.is_list():
            return False
        if _is_union(member) and not _is_pure_union(member, schema):
            return False
    return True


def _matches_declared_type(
    annotation: Any,
    declared_type: Any,
    schema: SchemaPool | None,
) -> bool:
    """Whether *annotation* derives from a declaration's type.

    A named type uses the shared name/class predicate; an anonymous local type
    has no name to look up, so its generated class is compared directly.
    """
    declared_name = getattr(declared_type, "name", None)
    if declared_name is not None:
        return derives_from(annotation, declared_name, schema)
    actual_class = getattr(annotation, "actual_type", None)
    declared_class = getattr(declared_type, "_type_class", None)
    if not isinstance(actual_class, type) or not isinstance(declared_class, type):
        return False
    return _class_derives_from_class(actual_class, declared_class, schema)


def declared_class_for(declared_name: str, schema: SchemaPool | None) -> type[Any] | None:
    """The generated class of one declared type, from the pool or the builtins."""
    declared = schema.get_type(declared_name) if schema is not None else None
    if declared is not None:
        return getattr(declared, "_type_class", None)
    return builtin_class(declared_name)


def matches_element_test(
    node: XdmNode,
    name: str | None,
    type_name: str | None,
    nillable: bool,
    schema: SchemaPool | None,
) -> bool:
    """The ``element(N, T)`` kind test of XPath 3.1 section 2.5.5.3.

    Substitution groups do not affect this test.  The nilled rule applies only
    when a type name is present and the type name has no ``?`` marker.
    """
    if node.kind is not NodeKind.ELEMENT:
        return False
    if name is not None and node.name != name:
        return False
    if type_name is None:
        return True
    if node.nilled and not nillable:
        return False
    return derives_from(node.annotation, type_name, schema)


def matches_attribute_test(
    node: XdmNode,
    name: str | None,
    type_name: str | None,
    schema: SchemaPool | None,
) -> bool:
    """The ``attribute(N, T)`` kind test of XPath 3.1 section 2.5.5.5."""
    if node.kind is not NodeKind.ATTRIBUTE:
        return False
    if name is not None and node.name != name:
        return False
    if type_name is None:
        return True
    return derives_from(node.annotation, type_name, schema)


def matches_schema_element(
    node: XdmNode,
    name: str,
    schema: SchemaPool | None,
) -> bool:
    """The ``schema-element(N)`` kind test of XPath 3.1 section 2.5.5.4.

    The declaration for *name* must be in scope (a static check elsewhere); a
    candidate matches when its name is *name* or a member of the actual
    substitution group, its type annotation derives from the declared type,
    and it is not nilled when the declaration is not nillable.
    """
    if node.kind is not NodeKind.ELEMENT or schema is None:
        return False
    declaration = schema.get_element(name)
    if declaration is None or node.name not in element_declaration_names(schema, name):
        return False
    declared_type = getattr(declaration, "type", None)
    if declared_type is not None and not _matches_declared_type(
        node.annotation, declared_type, schema
    ):
        return False
    # The nilled rule is about the declaration of the *candidate* node (the
    # declaration named N in XPath 3.1 section 2.5.5.4), not the head named by
    # the test; a nilled substitute whose own declaration is not nillable
    # cannot match.
    candidate = schema.get_element(node.name)
    assert candidate is not None  # every accepted name is a global declaration
    return not (node.nilled and not candidate.nillable)


def matches_schema_attribute(
    node: XdmNode,
    name: str,
    schema: SchemaPool | None,
) -> bool:
    """The ``schema-attribute(N)`` kind test of XPath 3.1 section 2.5.5.6."""
    if node.kind is not NodeKind.ATTRIBUTE or schema is None:
        return False
    declaration = schema.get_attribute(name)
    if declaration is None or node.name != name:
        return False
    declared_type = getattr(declaration, "type", None)
    return _matches_declared_type(node.annotation, declared_type, schema)


def declaration_exists(schema: SchemaPool | None, kind: str, name: str) -> bool:
    """Whether one in-scope element or attribute declaration exists."""
    if schema is None:
        return False
    if kind == "schema-element":
        return schema.get_element(name) is not None
    return schema.get_attribute(name) is not None


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
    *actual* substitution group.  The schema pool stores only direct edges, so
    the walk is transitive; abstract members never appear in an instance, and
    a member whose own declaration blocks substitution is not part of the
    actual group (XSD 1.1 section 3.3.1).
    """
    names = {name}
    pending = {name}
    while pending:
        discovered: set[str] = set()
        for current in pending:
            for member in schema.get_substitution_group(current) or frozenset():
                assert member.name is not None
                if getattr(member, "abstract", False):
                    continue
                tokens = blockTokens(getattr(member, "block", None))
                if "#all" in tokens or "substitution" in tokens:
                    continue
                discovered.add(member.name)
        pending = discovered - names
        names.update(pending)
    return tuple(sorted(names))
