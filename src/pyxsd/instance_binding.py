"""The instance-binding phase: binds an XML instance against a compiled
schema.

``bind_instance`` is the instance-binding phase of the pipeline:
it dispatches the instance document's root element against the
compiled schema's classes, builds the pythonic instance tree, records
non-fatal validation issues on the report, and returns the bound root
instance. The helpers in this module were moved verbatim from the
compiled schema (with ``self`` replaced by the explicit ``schema`` /
``xml_root`` / ``report`` parameters).
"""

import logging
from typing import Any, Protocol

from pyxsd import xsi
from pyxsd.derivation import (
    combinedBlock,
    derivationMessage,
    is_valid_xsi_type,
)
from pyxsd.element_representatives.element_representative import ElementRepresentative
from pyxsd.namespaces import NamespaceError
from pyxsd.schema_base import SchemaBase, nil_content_kind
from pyxsd.schema_hints import schema_location_info
from pyxsd.validation import ValidationReport
from pyxsd.wildcards import (
    NAMESPACE_ANY,
    WildcardSpec,
)
from pyxsd.xsd_data_types import (
    AnySimpleType,
    AnyType,
    qname_context,
    whitespace_mode,
    xsd_value_key,
)

logger = logging.getLogger(__name__)


class BoundSchemaProtocol(Protocol):
    """The slice of the compiled schema the binding phase reads.

    Annotation-only structural view of
    :class:`~pyxsd.schema.Schema`, which satisfies it without this
    module importing it: ``bind_instance`` and its helpers accept any
    object exposing these five attributes, breaking the
    binding→schema import edge (``schema.py`` calls
    ``bind_instance``).
    """

    report: ValidationReport
    mode: Any
    classes: dict[str, type[SchemaBase]]
    namespace_context: Any
    components: Any


def bind_instance(schema: BoundSchemaProtocol, xml_root: Any, report: ValidationReport) -> Any:
    """Reads the given xml file in the context of the xsd file.

    Produces instances of the above classes. Does validation.
    Returns a schema instance object.
    """
    logger.debug("Starting to parse the xml file.")

    # Binding diagnostics from here on belong to the instance phase.
    report.phase = "instance"

    schemaClass = schema.classes["schema"]

    schemaClassInstance = schemaClass()

    rootName = xml_root.tag.split("}")[-1]

    topLevelDescriptors = schemaClassInstance._getElements()

    # A schema with no global element declarations is legal (and is what
    # conditional inclusion leaves behind when it empties a document):
    # there is then no descriptor for the root to match, and the
    # ``unknown-root`` branch below reports it instead of aborting.
    if getattr(schema.mode, "namespaces", "legacy") == "strict":
        matching = [
            descriptor
            for descriptor in topLevelDescriptors
            if descriptor.instanceName(parser=schema) == xml_root.tag
        ]
    else:
        matching = [descriptor for descriptor in topLevelDescriptors if descriptor.name == rootName]

    if len(matching) > 1:
        elementNames = ", ".join(element.name for element in matching)
        report.add_error(
            "invalid schema: there is more than one global element named "
            f"'{rootName}' ({elementNames}); parsing only '{matching[0].name}'",
            code="multiple-roots",
            phase="schema",
        )

    if not matching:
        report.add_error(
            f"the xml root element '{rootName}' does not correspond to any "
            "global element declaration in the schema",
            code="unknown-root",
        )
        return None

    rootElement = matching[0]
    rootElementName = rootElement.name

    subInstance = None
    if rootElementName == rootName:
        with whitespace_mode(schema.mode.whitespace):
            subCls: Any = class_for_root(schema, rootElement, xml_root, report)
            if subCls is None:
                return None
            if subCls is SchemaBase:
                # An element declaration with no type is implicitly
                # xs:anyType. Bind it through the ur-type class so
                # its children go through the lax wildcard and a
                # matching global declaration is validated (so a
                # required attribute on the child is enforced,
                # AU_required00101m1_n).
                subCls = AnyType
            generate_correct_schema_tags(schema, xml_root, report)
            contentKind = getattr(subCls, "_contentKind_", None)
            isComplex = (
                contentKind == "complex"
                if contentKind is not None
                else issubclass(subCls, SchemaBase)
            )
            if isComplex:
                nilled = xsi.xsi_nil_is_true(xml_root)
                if xsi.xsi_nil_declared(xml_root) and not rootElement.isNillable():
                    report.add_error(
                        f"the root element '{rootName}' is not nillable but carries xsi:nil",
                        code="nil",
                        element=rootName,
                    )
                    nilled = False
                if nilled:
                    # A nilled root carries no content to validate:
                    # the emptiness rule is checked, declared
                    # attributes are validated, and an empty shell
                    # is bound. The content check is reported here
                    # rather than through ``_checkNilContent``: the
                    # ur-type stand-in class has no attached parser,
                    # so its classmethod would log instead of record.
                    if rootElement.getFixed() is not None:
                        report.add_error(
                            f"the root element '{rootName}' is marked nil "
                            "but its declaration has a fixed value",
                            code="nil",
                            element=rootName,
                        )
                    nilContent = nil_content_kind(xml_root)
                    if nilContent == "elements":
                        report.add_error(
                            f"the root element '{rootName}' is marked nil "
                            "but contains child elements",
                            code="nil",
                            element=rootName,
                        )
                    elif nilContent == "characters":
                        report.add_error(
                            f"the root element '{rootName}' is marked nil "
                            "but contains character content",
                            code="nil",
                            element=rootName,
                        )
                    subInstance = subCls._nilledInstance(
                        subCls, xml_root, subCls._node_name(xml_root)
                    )
                    subInstance._nil_ = True
                else:
                    forcedText = None
                    if xml_root.text is None and not list(xml_root):
                        forcedText = rootElement.getDefault()
                        if forcedText is None:
                            forcedText = rootElement.getFixed()
                    subInstance = subCls.makeInstanceFromTag(xml_root, forcedText)
                    if getattr(subCls, "_simpleContentType_", None) is not None:
                        subCls._checkFixedElement(rootElement, subCls, subInstance, rootElementName)
                    else:
                        subCls._checkElementValueConstraint(
                            rootElement, subCls, subInstance, rootElementName
                        )
            else:
                # The root element's declared type is a primitive
                # (simple) data type: build a typed instance directly.
                # ``xs:anyType`` is the exception: its lax ``##any``
                # content wildcard admits undeclared children, so they
                # are bound through that wildcard instead of being
                # rejected as simple-typed content.
                if subCls is AnyType and list(xml_root):
                    subInstance = any_type_root_instance(
                        schema, xml_root, subCls, rootElement, schemaClass, report
                    )
                else:
                    subInstance = primitive_root_instance(
                        schema, xml_root, subCls, rootElement, report
                    )
                # xsi:type may replace the declared root type, so the root
                # instance is stored directly instead of validated against
                # the declared element type.
            schemaClassInstance.__dict__[rootElementName] = subInstance
            subInstance._descriptor_ = rootElement
            check_identity_constraints_entry(schema, subInstance, report)

    return subInstance


def class_for_root(
    schema: BoundSchemaProtocol, rootElement: Any, xml_root: Any, report: ValidationReport
) -> type[SchemaBase] | None:
    """Resolves the class used to instantiate the root element.

    Honors ``xsi:type`` on the root element (dispatch to another
    schema type) and rejects abstract root element declarations,
    recording problems on the validation report.
    """
    if rootElement.isAbstract():
        report.add_error(
            f"root element '{rootElement.name}' is declared abstract; "
            "abstract elements may not appear in instance documents",
            code="abstract-element",
            element=rootElement.name,
        )

    subCls = rootElement.getType()
    if subCls is SchemaBase and rootElement.tagAttributes.get("type") is None:
        # An untyped declaration that is a substitution-group member
        # takes the head's type definition (SUN typeDef00204m).
        headName = rootElement.getSubstitutionGroupHead(parser=schema)
        if headName:
            schemaER = rootElement.getSchema()
            for candidate in getattr(schemaER, "elements", None) or []:
                if candidate is rootElement:
                    continue
                names = {candidate.name, getattr(candidate, "expandedName", None)}
                if headName in names:
                    headCls = candidate.getType()
                    if headCls is not None and headCls is not SchemaBase:
                        subCls = headCls
                    break
    if subCls is None:
        report.add_error(
            f"the type of root element '{rootElement.name}' could not be resolved",
            code="unknown-type",
            element=rootElement.name,
        )
        return None

    xsiTypeName = xsi.xsi_type_name(xml_root)
    if xsiTypeName is None:
        # xsi:type takes precedence over conditional type assignment
        # (XSD 1.1 §3.3.4.1); without it, the declaration's
        # alternatives select the governing type.
        from pyxsd.alternatives import ERROR_TYPE, select_alternative_type

        selected = select_alternative_type(rootElement, xml_root, schema)
        if selected is ERROR_TYPE:
            report.add_error(
                f"root element '{rootElement.name}' selects the xs:error "
                "type, whose value space is empty, so it cannot be valid",
                code="alternative-error",
                element=rootElement.name,
            )
            return subCls
        if selected is not None:
            return selected
        return subCls

    resolvedName = resolve_xsi_type_name(schema, xsiTypeName, xml_root, report)
    if resolvedName is None:
        return subCls
    resolved = ElementRepresentative.typeFromName(resolvedName, schema, warn=False)
    if resolved is None:
        report.add_error(
            f"xsi:type '{xsiTypeName}' on the root element does not "
            "correspond to a type in the schema",
            code="xsi-type",
            element=rootElement.name,
        )
        return subCls
    blocked = combinedBlock(rootElement.getBlock(), subCls)
    reason = is_valid_xsi_type(resolved, subCls, blocked)
    if reason is not None:
        report.add_error(
            derivationMessage(resolved, subCls, reason),
            code="xsi-type",
            element=rootElement.name,
        )
        return subCls
    logger.debug("Root element dispatched via xsi:type to %s", resolved.__name__)
    return resolved


def generate_correct_schema_tags(
    schema: BoundSchemaProtocol, xml_root: Any, report: ValidationReport
) -> None:
    """Generates the proper schema information and namespace
    information for a tag.

    ElementTree leaves the schema information in a form that is not
    valid XML on its own.

    - ``report``: the report any schema-hint warnings found here are
      routed to (the caller's per-parse instance report).
    """
    locationTagName = schema_location_info(xml_root, "t", report=report)
    if locationTagName is None or locationTagName not in xml_root.attrib:
        # No schema location information in the xml file; nothing
        # to regenerate.
        return None

    schemaLocation = xml_root.attrib[locationTagName]

    del xml_root.attrib[locationTagName]

    ns = schema_location_info(xml_root, "n", report=report)

    xml_root.attrib["xmlns:xsi"] = "http://www.w3.org/2001/XMLSchema-instance"

    if ns:
        xml_root.attrib["xsi:schemaLocation"] = schemaLocation
        xml_root.attrib["xmlns"] = ns
        xml_root.attrib[f"xmlns:{ns.lower()}"] = ns
        return None

    xml_root.attrib["xsi:noNamespaceSchemaLocation"] = schemaLocation
    return None


def check_identity_constraints_entry(
    schema: BoundSchemaProtocol, rootInstance: Any, report: ValidationReport
) -> None:
    """Runs the identity-constraint check over the bound tree.

    - ``report``: the report identity-constraint findings are routed
      to (the caller's per-parse instance report), not the schema's
      frozen compilation report.
    """
    # Imported lazily: importing pyxsd.identity before
    # element_representative (above) triggers a circular import
    # (identity -> schema_base -> element_representative -> attribute).
    from pyxsd.identity import check_identity_constraints

    check_identity_constraints(rootInstance, report)
    return None


def any_type_root_instance(
    schema: BoundSchemaProtocol,
    xml_root: Any,
    dataTypeClass: Any,
    rootElement: Any,
    binder: Any,
    report: ValidationReport,
) -> Any:
    """Builds the root instance for an ``xsd:anyType``-typed element.

    ``xs:anyType`` is the ur-type: its content is mixed character
    data plus a lax ``##any`` wildcard over element children, and
    every attribute is admissible. A root with child elements is
    therefore not "a simple type containing child elements";
    children are bound through the lax wildcard (a global
    declaration or a child ``xsi:type`` is honored, anything else
    is generic). The ``xsi:nil`` emptiness rules match the
    primitive root path.
    """
    rootName = (
        xml_root.tag
        if getattr(schema.mode, "namespaces", "legacy") == "strict"
        else xml_root.tag.split("}")[-1]
    )
    report_unknown_xsi_attributes(schema, xml_root, rootName, report)
    nilled = xsi.xsi_nil_is_true(xml_root)
    if xsi.xsi_nil_declared(xml_root) and not rootElement.isNillable():
        report.add_error(
            f"the root element '{rootName}' is not nillable but carries xsi:nil",
            code="nil",
            element=rootName,
        )
        nilled = False
    if nilled:
        nilContent = nil_content_kind(xml_root)
        if nilContent == "elements":
            report.add_error(
                f"the root element '{rootName}' is marked nil but contains child elements",
                code="nil",
                element=rootName,
            )
        elif nilContent == "characters":
            report.add_error(
                f"the root element '{rootName}' is marked nil but contains character content",
                code="nil",
                element=rootName,
            )

    instance = dataTypeClass._unvalidated()
    instance._name_ = rootName
    instance._attribs_ = {xsi.xsi_attr_key(key): val for key, val in xml_root.attrib.items()}
    if nilled:
        instance._value_ = None
        instance._children_ = []
        return instance
    text = xml_root.text
    instance._value_ = [text] if text else None
    instance._children_ = []
    wildcard = WildcardSpec(namespace=NAMESPACE_ANY, process_contents="lax")
    for child in xml_root:
        binder._bindAnyTypeChild(instance, child, wildcard)
    return instance


def primitive_root_instance(
    schema: BoundSchemaProtocol,
    xml_root: Any,
    dataTypeClass: Any,
    rootElement: Any,
    report: ValidationReport,
) -> Any:
    """Builds a typed instance for a root element whose declared
    type is a primitive data type rather than a complex type.

    Honors ``xsi:nil`` (rejected on non-nillable elements with code
    ``nil``). The document's text is validated through the data
    type's constructor; an invalid lexical value is reported (code
    ``value``) and the value is dropped so parsing can continue.
    Empty content takes the element's ``default``/``fixed`` value,
    and a ``fixed`` value is enforced with code ``fixed-element``.
    A simple-typed element may not carry child elements.
    """
    rootName = (
        xml_root.tag
        if getattr(schema.mode, "namespaces", "legacy") == "strict"
        else xml_root.tag.split("}")[-1]
    )
    report_unknown_xsi_attributes(schema, xml_root, rootName, report)
    if dataTypeClass is not AnyType:
        # The ur-type admits any attribute; every other primitive
        # (simple) type has no attribute uses.
        report_undeclared_simple_attributes(schema, xml_root, rootName, report)
    nilled = xsi.xsi_nil_is_true(xml_root)
    if xsi.xsi_nil_declared(xml_root) and not rootElement.isNillable():
        report.add_error(
            f"the root element '{rootName}' is not nillable but carries xsi:nil",
            code="nil",
            element=rootName,
        )
        nilled = False

    nilContent = nil_content_kind(xml_root) if nilled else None
    if list(xml_root):
        if nilled:
            report.add_error(
                f"the root element '{rootName}' is marked nil but contains child elements",
                code="nil",
                element=rootName,
            )
        else:
            report.add_error(
                f"the root element '{rootName}' has a simple type but contains child elements",
                code="unexpected-element",
                element=rootName,
            )

    if nilContent == "characters":
        report.add_error(
            f"the root element '{rootName}' is marked nil but contains character content",
            code="nil",
            element=rootName,
        )
    text = xml_root.text or ""
    value = None
    with qname_context(qname_bindings_for(schema, xml_root)):
        if nilled and rootElement.getFixed() is not None:
            report.add_error(
                f"the root element '{rootName}' is marked nil "
                "but its declaration has a fixed value",
                code="nil",
                element=rootName,
            )
        if not nilled:
            forced = None
            if xml_root.text is None and not list(xml_root):
                forced = rootElement.getDefault()
                if forced is None:
                    forced = rootElement.getFixed()
            if forced is not None:
                try:
                    value = dataTypeClass(forced)
                except (TypeError, ValueError) as exc:
                    report.add_error(
                        f"the root element '{rootName}' has an invalid default value: {exc}",
                        code=getattr(exc, "code", "default"),
                        element=rootName,
                    )
            else:
                try:
                    value = dataTypeClass(text)
                except (TypeError, ValueError) as exc:
                    report.add_error(
                        f"the root element '{rootName}' has an invalid "
                        f"{getattr(dataTypeClass, 'name', dataTypeClass.__name__)} "
                        f"value: {exc}",
                        code=getattr(exc, "code", "value"),
                        element=rootName,
                    )
                    if schema.mode.invalid_value == "raw":
                        value = AnySimpleType(text)

        if not nilled and value is not None:
            fixed = rootElement.getFixed()
            if fixed is not None:
                try:
                    fixedValue = dataTypeClass(fixed)
                except (TypeError, ValueError):
                    fixedValue = None
                if fixedValue is not None and xsd_value_key(value) != xsd_value_key(fixedValue):
                    report.add_error(
                        f"the root element '{rootName}' has a value that conflicts "
                        f"with its fixed value {fixed!r}",
                        code="fixed-element",
                        element=rootName,
                    )

    instance = dataTypeClass._unvalidated() if value is None else value
    instance._name_ = rootName
    instance._attribs_ = {xsi.xsi_attr_key(key): val for key, val in xml_root.attrib.items()}
    if nilled:
        # A nilled element has no value: the content rule above has
        # reported any character content, which is not bound here.
        instance._value_ = None
    else:
        instance._value_ = [str(value)] if value is not None else ([text] if text else None)
    instance._children_ = []
    return instance


def report_undeclared_simple_attributes(
    schema: BoundSchemaProtocol, elementTag: Any, elementName: str, report: ValidationReport
) -> None:
    """Rejects attributes on a simple-typed root element.

    A simple type has no attribute uses and no attribute wildcard, so
    any attribute other than the built-in schema-instance bookkeeping
    (``xsi:*``) or a namespace declaration is undeclared. The ur-type
    and primitive root paths do not run the attribute-declaration
    pass, so the check is made here (SUN typeDef01201m1/01202m1).
    """
    for attr in elementTag.attrib:
        if "xmlns" in attr:
            continue
        if xsi.is_builtin_xsi_attribute(attr):
            continue
        report.add_error(
            f"attribute '{xsi.xsi_attr_key(attr)}' is not declared in the "
            "schema and was not parsed",
            code="unexpected-attribute",
            element=elementName,
        )


def report_unknown_xsi_attributes(
    schema: BoundSchemaProtocol, elementTag: Any, elementName: str, report: ValidationReport
) -> None:
    """Rejects schema-instance attributes that are not built-ins.

    The four built-in xsi attributes are handled specially; any
    other attribute in the namespace is an ordinary attribute. The
    ur-type and primitive root paths do not run attribute-declaration
    or wildcard checks, so an undeclared unknown xsi attribute is
    reported here (attMd001-011).
    """
    for attr in elementTag.attrib:
        if xsi.unknown_xsi_attribute(attr):
            report.add_error(
                f"attribute '{attr}' is not declared in the schema and was not parsed",
                code="unexpected-attribute",
                element=elementName,
            )


def qname_bindings_for(schema: BoundSchemaProtocol, element: Any) -> dict[str, str] | None:
    """Prefix bindings in scope at ``element``, or ``None``.

    Only strict namespace mode resolves ``xs:QName`` values; legacy
    mode keeps lexical comparison (``None`` disables resolution).
    """
    if getattr(schema.mode, "namespaces", "legacy") != "strict":
        return None
    return schema.namespace_context.bindings_for(element)


def resolve_xsi_type_name(
    schema: BoundSchemaProtocol, value: str, element: Any, report: ValidationReport
) -> str | None:
    """Resolves a lexical ``xsi:type`` QName against the instance scope.

    In ``legacy`` namespace mode the raw value is returned unchanged.
    In ``strict`` mode the value is expanded through the instance
    namespace context; an unbound prefix is reported as
    ``unknown-namespace-prefix`` and ``None`` is returned so the
    caller keeps the declared type.
    """
    if getattr(schema.mode, "namespaces", "legacy") != "strict":
        return value
    try:
        return schema.namespace_context.resolve(element, value)
    except NamespaceError as exc:
        report.add_error(
            str(exc),
            code="unknown-namespace-prefix",
            element=element.tag,
        )
        return None
