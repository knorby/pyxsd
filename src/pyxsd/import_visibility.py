"""Authored direct-import visibility for schema composition.

XSD 1.1 §4.2.6 (the same rule in 1.0): a schema document that
references a component in another namespace must itself author an
``xs:import`` for that namespace. Composition splices every composed
document into one main tree, which makes transitively supplied
components resolvable without the referencing document's own import.

This module records each document's authored ``xs:import`` namespaces
and effective target namespace before composition removes the
directives (:func:`record_authored_document`), then audits the
composed tree's QName component references afterwards
(:func:`audit_direct_imports`). The global component lookup and the
element-representative run are unchanged: this is a post-composition
audit over the provenance composition already records.
"""

from typing import TYPE_CHECKING, Any

from pyxsd.namespaces import (
    XLINK_NS,
    XML_NS,
    XSD_NS,
    XSI_NS,
    NamespaceError,
    clark,
    local_name,
    namespace_of,
)

if TYPE_CHECKING:
    from pyxsd.schema_composition import CompositionContext

__all__ = ["audit_direct_imports", "record_authored_document"]

#: Namespaces whose vocabularies are built into the compiler, so a
#: reference into them needs no authored import: the schema-for-schema
#: types, and the ``xml``/``xsi``/``xlink`` attribute declarations the
#: strict-mode injections supply (the same set a namespace-only import
#: never reports as unresolved, see ``splice_included_schema``).
_BUILTIN_VOCABULARY_NAMESPACES = (XSD_NS, XML_NS, XLINK_NS, XSI_NS)

#: Component-reference attributes that hold a single QName (XSD 1.1
#: §4.2.6). ``notQName``, default/fixed values and XPath expressions
#: are deliberately absent: they are not all component references
#: subject to the import rule.
_SINGLE_REFERENCE_ATTRIBUTES = (
    "ref",
    "base",
    "type",
    "refer",
    "itemType",
    "defaultAttributes",
    "substitutionGroup",
)

#: Component-reference attributes that hold a whitespace-separated
#: QName list; every name in the list is a reference of its own.
_LIST_REFERENCE_ATTRIBUTES = ("memberTypes",)


def record_authored_document(
    ctx: "CompositionContext", root: Any, *, adopted_namespace: str | None = None
) -> None:
    """Records one schema document's authored imports and namespace.

    Runs at the start of every real ``splice_composed_schemas`` call,
    while the document's own ``xs:import`` directives are still its
    children: the directives say which namespaces the document itself
    authorizes, and ``targetNamespace`` (or the chameleon-adopted
    namespace, for a redefined base whose root is not stamped) says
    which namespace its components and unprefixed references belong
    to. Only an import the document actually wrote is recorded; the
    synthetic import tag a caller-supplied or instance-hint schema
    creates never passes through here, and supplying such a schema
    does not remove the referencing document's §4.2.6 obligation.

    An ``xs:import`` with no ``namespace`` attribute authors the
    absent namespace, preserved as ``None`` in the import set.
    """
    imports: set[str | None] = set()
    for child in root:
        if not isinstance(child.tag, str):
            continue
        if child.tag != clark(XSD_NS, "import"):
            continue
        imports.add(child.get("namespace"))
    ctx.authored_import_namespaces[id(root)] = imports
    ctx.authored_effective_namespaces[id(root)] = root.get("targetNamespace") or adopted_namespace
    ctx.authored_document_roots[id(root)] = root


def audit_direct_imports(ctx: "CompositionContext", main_root: Any) -> None:
    """Audits QName component references against authored imports.

    Runs once after the composed and additional schemas are spliced
    and before the element-representative run. Each element's authored
    document root comes from ``authored_element_roots`` (the override
    block's own declarations, whose value defaults composition scopes
    to the overridden document) and then ``composed_schema_roots`` (the
    main root for the main document's own elements); its QNames are
    resolved in its own namespace context, and a reference is legal only
    when its namespace is the document's effective namespace, one of the
    built-in vocabulary namespaces (``xs``/``xml``/``xsi``/``xlink``),
    or one the document imported itself. Anything else is
    reported as ``import-required`` at the declaration's origin.

    Composed documents retain only their spliced children in the main
    tree, so a composed ``xs:schema`` root's own ``defaultAttributes``
    is not reached by the tree walk. Every recorded authored root is
    audited once for that attribute, with its own namespace context,
    effective namespace, authored imports, and declaration origin; the
    main root is already covered by the walk and is skipped here.

    Only XSD-language elements are audited: annotation payloads and
    foreign markup are skipped, whatever QName-shaped attributes they
    carry. Unresolvable prefixes keep their existing namespace error
    and never gain a duplicate import error.
    """
    for element, in_annotation in _iter_schema_elements(main_root):
        if in_annotation:
            continue
        root = ctx.authored_element_roots.get(
            id(element), ctx.composed_schema_roots.get(id(element), main_root)
        )
        effective = ctx.authored_effective_namespaces[id(root)]
        imports = ctx.authored_import_namespaces[id(root)]
        for attribute in _SINGLE_REFERENCE_ATTRIBUTES:
            value = element.get(attribute)
            if value:
                _audit_reference(ctx, element, value, effective, imports)
        for attribute in _LIST_REFERENCE_ATTRIBUTES:
            value = element.get(attribute)
            if value:
                for token in value.split():
                    _audit_reference(ctx, element, token, effective, imports)
    for root in ctx.authored_document_roots.values():
        if root is main_root:
            continue
        default_attributes = root.get("defaultAttributes")
        if default_attributes:
            _audit_reference(
                ctx,
                root,
                default_attributes,
                ctx.authored_effective_namespaces[id(root)],
                ctx.authored_import_namespaces[id(root)],
            )


def _iter_schema_elements(root: Any):
    """Yields ``(element, in_annotation)`` for XSD-namespace elements.

    The flag is true inside an ``xs:annotation`` payload: those
    subtrees hold arbitrary markup (XSD-namespace element names
    included), not schema components, so the audit never reads them.
    Foreign markup outside annotations is filtered by the namespace
    check the flag cannot reach.
    """
    stack: list[tuple[Any, bool]] = [(root, False)]
    while stack:
        element, in_annotation = stack.pop()
        tag = element.tag
        if not isinstance(tag, str):
            continue
        if namespace_of(tag) != XSD_NS:
            continue
        payload = in_annotation or local_name(tag) == "annotation"
        yield element, in_annotation
        for child in reversed(element):
            stack.append((child, payload))


def _audit_reference(
    ctx: "CompositionContext",
    element: Any,
    value: str,
    effective: str | None,
    imports: set[str | None],
) -> None:
    """Reports one QName reference into an unauthorized namespace."""
    try:
        resolved = ctx.namespace_context.resolve(element, value)
    except NamespaceError:
        # An unbound prefix is the existing namespace error's
        # responsibility; the visibility audit never duplicates it.
        return
    namespace = namespace_of(resolved)
    if (
        namespace is None
        or namespace == effective
        or namespace in _BUILTIN_VOCABULARY_NAMESPACES
        or namespace in imports
    ):
        return
    ctx.report.add_error(
        f"the reference '{value}' names the namespace '{namespace}', "
        "which the referencing schema document does not import",
        code="import-required",
        phase="schema",
        location=ctx.declaration_origins.get(element),
    )
