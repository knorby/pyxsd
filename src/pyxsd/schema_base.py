import logging
from typing import Any, ClassVar

from pyxsd import xsi
from pyxsd.binding import BindingPolicy, ParseModes
from pyxsd.content_model import (
    first_required_name,
    match_content_associations,
    particle_names,
)
from pyxsd.derivation import combinedBlock, derivationMessage, is_validly_derived
from pyxsd.namespaces import XML_NS, NamespaceError, local_name, namespace_of
from pyxsd.validation import IssueSeverity
from pyxsd.wildcards import WildcardSpec
from pyxsd.xsd_data_types import AnySimpleType, XsdDataType, qname_context, xsd_value_key

logger = logging.getLogger(__name__)


def _mode_for(cls) -> BindingPolicy:
    """The binding policy stamped on a generated class (or STRICT)."""
    return getattr(cls, "_parseMode_", ParseModes.STRICT)


def _global_declaration(components, local: str, kind: str, uri: str | None):
    """Returns the *global* declaration named ``local`` in ``uri``.

    Wildcard ``processContents`` checks look up global declarations
    only. A local declaration inside an unrelated type must not satisfy
    a strict wildcard, so candidates are filtered by kind, global
    scope, and expanded name.
    """
    if components is None:
        return None
    entries = components.get(local)
    if not entries:
        return None
    for entry in entries:
        if componentKind(entry) != kind:
            continue
        is_global = getattr(entry, "isGlobalDeclaration", None)
        if is_global is not None and not is_global():
            continue
        namespace = getattr(entry, "getNamespace", None)
        if namespace is not None and namespace() != uri:
            continue
        return entry
    return None


def nil_content_kind(element: Any) -> str | None:
    """The kind of content a nilled element must not have.

    Returns ``"elements"`` when the element has child elements,
    ``"characters"`` when it has any character content (whitespace
    included: ``xsi:nil`` requires the element to be empty), and
    ``None`` when the element is truly empty. One helper keeps the
    emptiness rule identical for roots and children.
    """
    if list(element):
        return "elements"
    if element.text is not None and element.text != "":
        return "characters"
    return None


class SchemaBase:
    """Serves as the base class for all schema type classes created.

    The pythonic instance tree is built from this class.  This class
    also contains the means to do non-fatal parser error checking.  A
    little bit of the work this class does is also done in the PyXSD
    parser. The schema and xml file do not line up perfectly.  The top
    level element in the schema and the schema tag both contain
    information relevant to the top-level tag in the XML. For this
    reason, the tree building/checking must be started in the same
    location the method ``makeInstanceFromTag`` is called in this
    class.

    Class creation is wired through ``__init_subclass__``: when a class
    is generated from the schema (see
    :meth:`pyxsd.element_representatives.xsd_type.XsdType.clsFor`),
    every :class:`~pyxsd.element_representatives.element.Element` and
    :class:`~pyxsd.element_representatives.attribute.Attribute`
    descriptor placed in the class body is registered under its element
    name in the class's ``_elementNames_``/``_attributeNames_`` lists.
    The descriptors themselves learn their owning class through the
    standard ``__set_name__`` protocol.

    Recoverable validation problems are recorded on the
    :class:`~pyxsd.validation.ValidationReport` owned by the running
    :class:`~pyxsd.parser.PyXSD` instance (which every generated class
    carries as its ``pyXSD`` attribute) instead of being printed. When
    no parser is attached, issues fall back to the ``pyxsd`` logging
    hierarchy.
    """

    #: Default element bookkeeping for subclasses that declare no
    #: element descriptors of their own (overridden per subclass by
    #: ``__init_subclass__``).
    _elementNames_: ClassVar[list[str]] = []

    #: Default attribute bookkeeping, like ``_elementNames_``.
    _attributeNames_: ClassVar[list[str]] = []

    # Per-instance bookkeeping consumed by the writers, the transform
    # framework, and identity checking. Assigned by the parser and
    # ``makeInstanceFromTag``; annotations only.
    _name_: str
    _attribs_: dict[str, str]
    _value_: list[str] | None

    def __init_subclass__(cls, **kwargs):
        """Collects the descriptor bookkeeping for a new subclass.

        Every ``Element``/``Attribute`` descriptor in the new class
        body is recorded, in declaration order, so the instance-tree
        machinery can find them without per-class closures. Rebinding
        a descriptor inherited from a base class has no effect on the
        base's own bookkeeping.
        """
        super().__init_subclass__(**kwargs)
        elementNames = []
        attributeNames = []
        for attrName, value in cls.__dict__.items():
            if isinstance(value, Element):
                elementNames.append(attrName)
            elif isinstance(value, Attribute):
                attributeNames.append(attrName)
        cls._elementNames_ = elementNames
        cls._attributeNames_ = attributeNames

    def __init__(self, *args):
        """Creates the instances that are in the tree.

        These objects are initialized from within SchemaBase.

        The signature accepts (and ignores) a positional value so the
        generated classes for schema-defined simple types - which
        carry SchemaBase in their method resolution order for the
        ``__init_subclass__`` bookkeeping - can be instantiated with
        the value being validated. Lexical validation happens in the
        data-type ``__new__`` before ``__init__`` is reached.
        """
        self._children_ = []
        self._value_ = None

    def __getattr__(self, name):
        """Provides a helpful error for attributes normal lookup misses.

        ``__getattr__`` is only consulted when regular lookup (the
        class descriptors, the instance dictionary, and the class
        hierarchy) has already failed, so declaring this hook cannot
        change any successful lookup. Internal names keep a plain
        ``AttributeError`` so that copy/pickle/introspection probing
        behaves as usual.
        """
        # Dunder probes from copy, pickle, and introspection: fail plainly.
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        try:
            elements = [descriptor.name for descriptor in self._getElements()]
            attributes = list(self.descAttributes())
        except Exception:  # never mask the original lookup failure
            raise AttributeError(name) from None
        raise AttributeError(
            f"{type(self).__name__!r} instance has no attribute {name!r}; "
            f"the schema declares elements {elements} and attributes {attributes}. "
            "Only schema-declared names are available as attributes; use "
            "_children_/_attribs_ for the raw containers."
        )

    # ------------------------------------------------------------------
    # Validation issue plumbing
    # ------------------------------------------------------------------

    @classmethod
    def _report_issue(cls, severity, message, *, code, element=None):
        """Record a validation issue on the owning parser's report.

        Falls back to logging when the class has no attached parser
        (for example hand-written overlay classes that subclass
        SchemaBase directly).
        """
        parser = getattr(cls, "pyXSD", None)
        if parser is not None:
            if severity is IssueSeverity.ERROR:
                parser.report.add_error(message, code=code, element=element)
            else:
                parser.report.add_warning(message, code=code, element=element)
        else:
            logger.log(
                logging.ERROR if severity is IssueSeverity.ERROR else logging.WARNING,
                "%s[%s] %s",
                element or cls.__name__,
                code,
                message,
            )

    @classmethod
    def _report_error(cls, message, *, code, element=None):
        """Record an error-severity validation issue."""
        cls._report_issue(IssueSeverity.ERROR, message, code=code, element=element)

    @classmethod
    def _report_warning(cls, message, *, code, element=None):
        """Record a warning-severity validation issue."""
        cls._report_issue(IssueSeverity.WARNING, message, code=code, element=element)

    @classmethod
    def _reportStrayCharacters(cls, elementTag):
        """Reports character data under an element-only content model.

        XSD 1.1 §3.4.3.2 (Element Locally Valid (Complex Type)): an
        element whose governing type's content type is element-only has
        no character content other than whitespace. Mixed types and
        simple content are not checked here (their text is legal or is
        the value), and elements whose content model could not be
        compiled keep the legacy tolerance. Only direct text of
        *elementTag* is inspected; deeper nodes are checked when the
        binder recurses into them.
        """
        texts = [elementTag.text]
        texts.extend(child.tail for child in elementTag)
        for text in texts:
            if text is not None and text.strip():
                cls._report_error(
                    f"element '{elementTag.tag.split('}')[-1]}' has character "
                    "content but its content model is element-only",
                    code="unexpected-character",
                    element=cls.__name__,
                )
                return

    @classmethod
    def _node_name(cls, node):
        """The name an instance node is matched under.

        Legacy mode keeps the historical local-name comparison; strict
        mode uses ElementTree's Clark tag, which is the expanded name
        for a namespaced node and a plain local name otherwise.
        """
        if getattr(_mode_for(cls), "namespaces", "legacy") == "strict":
            return node.tag
        return node.tag.split("}")[-1]

    @classmethod
    def _instance_name_of(cls, descriptor, *, is_attribute=False):
        """The instance name for a declaration, honoring the active mode."""
        name_fn = getattr(descriptor, "instanceName", None)
        if name_fn is None:
            return getattr(descriptor, "name", None)
        return name_fn(parser=getattr(cls, "pyXSD", None), is_attribute=is_attribute)

    @classmethod
    def _qname_bindings(cls, element):
        """The prefix bindings in scope at ``element``, or ``None``.

        Only strict namespace mode resolves ``xs:QName`` values; legacy
        mode keeps lexical comparison (returning ``None`` disables
        resolution for the duration of a binding call).
        """
        if getattr(_mode_for(cls), "namespaces", "legacy") != "strict":
            return None
        parser = getattr(cls, "pyXSD", None)
        context = getattr(parser, "namespaceContext", None)
        if context is None:
            return None
        return context.bindings_for(element)

    @classmethod
    def _wildcard_element_specs(cls, instance) -> list[WildcardSpec]:
        """Element wildcard constraints visible to ``instance``.

        Specs are gathered along the MRO (least-derived first) so a
        wildcard contributed by an extension base still applies to the
        derived instance.
        """
        specs: list[WildcardSpec] = []
        for klass in reversed(type(instance).__mro__):
            specs.extend(klass.__dict__.get("wildcardElementSpecs_", ()))
        return specs

    @classmethod
    def _wildcard_attribute_specs(cls, instance) -> list[WildcardSpec]:
        """Attribute wildcard constraints visible to ``instance``."""
        specs: list[WildcardSpec] = []
        for klass in reversed(type(instance).__mro__):
            specs.extend(klass.__dict__.get("wildcardAttributeSpecs_", ()))
        return specs

    @classmethod
    def _wildcard_match(
        cls, specs: list[WildcardSpec], node_name: str, target_namespace: str | None
    ) -> WildcardSpec | None:
        """The first wildcard constraint admitting ``node_name``.

        ``node_name`` must be a Clark/expanded name; ``None`` means no
        wildcard admits the node.
        """
        uri = namespace_of(node_name)
        for spec in specs:
            if spec.allows(uri, target_namespace):
                return spec
        return None

    @classmethod
    def _checkWildcardAttribute(cls, attr, value, spec, parser) -> bool:
        """Checks a wildcard-matched attribute against ``processContents``.

        Returns ``False`` when the attribute should be dropped (an error
        has been reported). ``skip`` accepts unconditionally; ``lax``
        validates when a global declaration exists and otherwise
        accepts; ``strict`` requires a declaration.
        """
        if spec.process_contents == "skip":
            return True
        local = local_name(attr)
        uri = namespace_of(attr)
        components = getattr(parser, "components", None)
        declaration = _global_declaration(components, local, "attribute", uri)
        if declaration is None:
            if spec.process_contents == "strict":
                cls._report_error(
                    f"no declaration found for attribute '{local}' required by a strict wildcard",
                    code="wildcard-no-declaration",
                    element=cls.__name__,
                )
                return False
            return True
        try:
            declaration.pyXSD = parser
            declaration.getType()(value)
        except Exception as e:
            cls._report_error(
                f"attribute '{local}' has an invalid value: {e}",
                code="value",
                element=cls.__name__,
            )
            return False
        return True

    @classmethod
    def _bindWildcardChild(cls, instance, subElement, spec) -> None:
        """Binds one child accepted by an element wildcard.

        ``skip`` (and legacy mode) binds generically. ``lax`` validates
        against a matching global declaration when one exists and binds
        generically otherwise. ``strict`` reports
        ``wildcard-no-declaration`` when no declaration matches.
        """
        parser = getattr(cls, "pyXSD", None)
        mode = getattr(parser, "mode", None)
        if getattr(mode, "namespaces", "legacy") != "strict" or spec.process_contents == "skip":
            instance._children_.append(cls.makeGenericInstance(subElement))
            return
        local = local_name(subElement.tag)
        uri = namespace_of(subElement.tag)
        components = getattr(parser, "components", None)
        descriptor = _global_declaration(components, local, "element", uri)
        if descriptor is not None:
            if descriptor.isAbstract():
                cls._report_error(
                    f"element '{local}' is declared abstract; "
                    "only its substitution group members may appear in the xml",
                    code="abstract-element",
                    element=cls.__name__,
                )
                return
            descriptor.pyXSD = parser
            subElCls = cls._classForChild(descriptor, subElement)
            if subElCls is not None:
                cls._addChildInstance(instance, subElement, subElCls, descriptor)
                return
        if spec.process_contents == "lax":
            instance._children_.append(cls.makeGenericInstance(subElement))
            return
        cls._report_error(
            f"no declaration found for element '{local}' required by a strict wildcard",
            code="wildcard-no-declaration",
            element=cls.__name__,
        )
        instance._children_.append(cls.makeGenericInstance(subElement))

    # ------------------------------------------------------------------
    # Instance tree construction
    # ------------------------------------------------------------------

    @classmethod
    def makeInstanceFromTag(cls, elementTag, forcedText=None):
        """Takes in a schema type class and its corresponding xml element.

        It then instantiates the class, adds a name from the name in
        the xml element, and then hands the instance and the element to
        other methods to add attributes, elements, and values to this
        instance. It adds these according to the schema classes, and
        not the element. A non-fatal (when possible) error is raised
        when the xml element does not correspond to the schema class.

        Types declared ``abstract`` may not be instantiated directly
        (derived classes may); the violation is recorded on the
        validation report and parsing continues.

        When the class carries simple content (a complex type with
        ``simpleContent``), the element's text is validated and bound
        through the content's simple type instead of the raw
        ``addValueTo`` path.

        - ``elementTag`` - the xml element that corresponds to ``cls``

        - ``forcedText`` - a default or fixed value to use when the
          element has no text of its own
        """
        content_cls = getattr(cls, "_simpleContentType_", None)
        if not isinstance(content_cls, type):
            content_cls = None
        if content_cls is None:
            instance = cls()
        else:
            instance = cls._simpleContentInstance(elementTag, content_cls, forcedText)
        instance._name_ = cls._node_name(elementTag)
        if cls.__dict__.get("abstract_"):
            cls._report_error(
                f"type '{cls.__name__}' is declared abstract and may not be instantiated directly",
                code="abstract-type",
                element=instance._name_,
            )
        cls.addAttributesTo(instance, elementTag)
        cls.addElementsTo(instance, elementTag)
        if content_cls is None:
            cls.addValueTo(instance, elementTag)

        return instance

    @classmethod
    def _simpleContentInstance(cls, elementTag, contentCls, forcedText):
        """Builds a simple-content element through its content type.

        The content class carries the lexical validation and any facet
        constraints; the element instance itself is typed by the same
        base, so the bound ``_value_`` is the validated one.  An invalid
        value is reported with code ``value`` and yields an unvalidated
        instance (or the raw text under the ``raw`` policy).
        """
        text = elementTag.text
        if text is None and forcedText is not None:
            text = forcedText
        lexical = text if text is not None else ""
        try:
            with qname_context(cls._qname_bindings(elementTag)):
                typed = contentCls(lexical)
                instance = cls(lexical)
        except (TypeError, ValueError) as e:
            cls._report_error(
                f"the value of the '{elementTag.tag.split('}')[-1]}' element "
                f"is not valid for its type: {e}",
                code="value",
                element=cls.__name__,
            )
            if _mode_for(cls).invalid_value == "raw":
                raw = cls._rawPrimitiveValue(elementTag, elementTag.text)
                if forcedText is not None and elementTag.text is None:
                    raw._value_ = [forcedText]
                return raw
            unvalidated = cls._unvalidated()  # type: ignore[attr-defined]
            unvalidated._attribs_ = dict(elementTag.attrib)
            unvalidated._value_ = None
            unvalidated._children_ = []
            return unvalidated
        instance._attribs_ = dict(elementTag.attrib)
        instance._value_ = [str(typed)]
        instance._children_ = list(elementTag)
        return instance

    @classmethod
    def addAttributesTo(cls, instance, elementTag):
        """Called by ``makeInstanceFromTag()``.

        Adds attributes according to the schema by calling
        ``getAttributesFromTag()``. The attributes are then checked.
        """
        tagsUsed = instance.getAttributesFromTag(elementTag)
        instance.checkAttributes(tagsUsed, elementTag)

    def getAttributesFromTag(self, elementTag):
        """Adds attributes to the ``_attribs_`` dictionary in the
        instance.

        Only attributes in the type classes are added. All of the
        attribute values are validated against descriptors in the
        Attribute class in element_representatives. The only exception
        to this procedure is for namespace and schemaLocation tags, as
        the program currently does not have any mechanism to actually
        check these.

        - ``elementTag``: the xml element that the instance represents
        """
        self._attribs_ = {}
        usedAttributes = []
        xsiPrefix = f"{{{xsi.XSI_NAMESPACE}}}"
        # QName-valued attributes resolve against this element's in-scope
        # prefix bindings (strict mode only).
        with qname_context(self._qname_bindings(elementTag)):
            # XSI-namespace attributes (xsi:nil, xsi:type, ...) are stored
            # under their conventional display spelling so the writers emit
            # valid xml (the document's own xmlns:xsi declaration, a plain
            # attribute here, keeps the output reparseable).
            for attr in elementTag.attrib:
                if "xmlns" in attr or "xsi:" in attr or attr.startswith(xsiPrefix):
                    displayKey = xsi.xsi_attr_key(attr)
                    setattr(self, displayKey, elementTag.attrib[attr])
                    usedAttributes.append(displayKey)
                    self._attribs_[displayKey] = elementTag.attrib[attr]
            for name in self.descAttributeNames():
                descriptor = self.descAttributes()[name]
                matchName = self._instance_name_of(descriptor, is_attribute=True)
                if matchName in elementTag.attrib:
                    # Bind through the declaration's own descriptor: a
                    # subclass element sharing the name must not capture
                    # the value (and a plain setattr would find it first
                    # in the MRO).
                    descriptor.__set__(self, elementTag.attrib[matchName])
                    usedAttributes.append(matchName)
                    self._attribs_[matchName] = elementTag.attrib[matchName]
            # Attribute wildcard (xs:anyAttribute) pass-through. In legacy
            # namespace mode every undeclared attribute is accepted raw; in
            # strict mode the wildcard's namespace constraint must admit the
            # attribute, and ``processContents`` decides whether a global
            # declaration is required.
            if getattr(self, "hasWildcardAttributes_", False):
                cls = type(self)
                strict = getattr(_mode_for(cls), "namespaces", "legacy") == "strict"
                specs = self._wildcard_attribute_specs(self) if strict else []
                targetNamespace = getattr(cls, "_targetNamespace_", None) if strict else None
                parser = getattr(cls, "pyXSD", None)
                for attr, value in elementTag.attrib.items():
                    if "xmlns" in attr or "xsi:" in attr or attr.startswith(xsiPrefix):
                        continue
                    if attr in usedAttributes:
                        continue
                    if strict:
                        spec = self._wildcard_match(specs, attr, targetNamespace)
                        if spec is None:
                            continue
                        if not self._checkWildcardAttribute(attr, value, spec, parser):
                            continue
                    self._attribs_[attr] = value
                    usedAttributes.append(attr)
        return usedAttributes

    @classmethod
    def addElementsTo(cls, instance, elementTag):
        """Checks order on the child elements, with different functions
        for ``sequence``, ``choice``, and ``all``.

        Iterates through all the elements specified in the class of the
        schema, and matches these elements with the elements from the
        xml. Redirects elements that are primitive types (integer,
        double, string, and so on) to another function. Calls
        ``makeInstanceFromTag()`` on all the children.

        Children may also match as ``substitutionGroup`` members of a
        declared element: a member element is parsed with its own type
        wherever its head element is declared, unless the head blocks
        substitution.

        - ``instance`` - the instance of ``cls`` that is having
          elements added to it.

        - ``elementTag`` - the xml element that corresponds to ``cls``
        """
        subElements = list(elementTag)

        elemDescriptors = instance._getElements()

        # No early return on childless elements: the order checkers
        # still run with an empty child list so that required content
        # (e.g. a choice with minOccurs=1) is reported when absent.

        # Substitution-group dispatch: member xml children may appear
        # wherever their head element is declared.
        substitutionGroups = cls._schemaSubstitutionGroups(elemDescriptors)
        declaredByName = {descriptor.name: descriptor for descriptor in elemDescriptors}
        declaredByExpanded = {
            getattr(descriptor, "expandedName", None): descriptor for descriptor in elemDescriptors
        }
        memberHeadMap: dict[str, str] = {}
        for headName, members in substitutionGroups.items():
            headDescriptor = declaredByName.get(headName) or declaredByExpanded.get(headName)
            headMatch = (
                cls._instance_name_of(headDescriptor) if headDescriptor is not None else headName
            )
            for member in members:
                memberHeadMap[cls._instance_name_of(member)] = headMatch

        # Wildcard (xs:any) constraints live in the compiled model as
        # "any" particles, so order and occurrence are checked for
        # wildcard content like any other content. The pre-filter below
        # only applies when the model could not be compiled (the legacy
        # order checkers must not see wildcard children).
        hasWildcard = getattr(instance, "hasWildcardElements_", False)
        strictNamespaces = getattr(_mode_for(cls), "namespaces", "legacy") == "strict"
        wildcardSpecs = cls._wildcard_element_specs(instance) if hasWildcard else []
        targetNamespace = getattr(cls, "_targetNamespace_", None)

        model = getattr(instance, "_contentModel_", None)
        if model is not None and getattr(cls, "_elementOnly_", False):
            cls._reportStrayCharacters(elementTag)
        if model is None and hasWildcard:
            declaredNames = {cls._instance_name_of(descriptor) for descriptor in elemDescriptors}
            declaredNames.update(memberHeadMap)
            declaredChildren = []
            for subElement in subElements:
                nodeName = cls._node_name(subElement)
                if nodeName in declaredNames:
                    declaredChildren.append(subElement)
                elif (
                    strictNamespaces
                    and cls._wildcard_match(wildcardSpecs, nodeName, targetNamespace) is not None
                ) or not strictNamespaces:
                    continue
                else:
                    declaredChildren.append(subElement)
        else:
            declaredChildren = subElements

        if model is not None:
            complete, leftover, childMatches = match_content_associations(
                model,
                declaredChildren,
                memberHeadMap,
                name_of=cls._node_name,
                target_namespace=targetNamespace,
                namespace_checked=strictNamespaces,
            )
        else:
            complete, leftover, childMatches = False, None, []
        # The particle that admitted each declared child. Binding uses
        # these records so a child accepted by a particular wildcard is
        # bound (and validated) through that wildcard.
        admittedBy = {match.position: match.particle for match in childMatches}

        if model is None or not complete:
            sOrC = getattr(elemDescriptors[0], "sOrC", None) if elemDescriptors else None

            if sOrC == "sequence":
                cls.checkElementOrderInSequence(elemDescriptors, declaredChildren, memberHeadMap)

            elif sOrC == "choice":
                cls.checkElementOrderInChoice(elemDescriptors, declaredChildren, memberHeadMap)

            elif sOrC == "all":
                cls.checkElementOrderInAll(elemDescriptors, declaredChildren, memberHeadMap)

            if model is not None and leftover:
                # The compiled model rejected some children the legacy
                # checkers do not cover (a closed model must consume
                # every child).
                declared = particle_names(model)

                def exceeds_wildcard(node_name: str) -> bool:
                    """True when a wildcard admits the name but the model
                    still refused it: the wildcard's occurrence limits
                    are exhausted."""
                    if not hasWildcard:
                        return False
                    if not strictNamespaces:
                        return True
                    return (
                        cls._wildcard_match(wildcardSpecs, node_name, targetNamespace) is not None
                    )

                for subElement in leftover:
                    subElementName = cls._node_name(subElement)
                    head = memberHeadMap.get(subElementName, subElementName)
                    if head in declared:
                        cls._report_error(
                            f"element '{subElementName}' is not allowed in this "
                            "position in the content model",
                            code="order",
                            element=cls.__name__,
                        )
                    elif exceeds_wildcard(subElementName):
                        cls._report_error(
                            f"element '{subElementName}' exceeds the occurrence "
                            "limits of the wildcard that allows it",
                            code="order",
                            element=cls.__name__,
                        )
                    else:
                        cls._report_error(
                            f"element '{subElementName}' is not declared in the "
                            "content model and no wildcard allows it",
                            code="unexpected-element",
                            element=cls.__name__,
                        )

            # A required particle is genuinely unmet when the best match
            # consumed nothing (or there was nothing to consume): the
            # model admitted no satisfying path. A static check on a
            # partially-matched model would flag particles that the
            # matching did satisfy, so it is skipped there.
            if model is not None and not complete:
                stalled = not leftover or leftover == declaredChildren
                missing = first_required_name(model) if stalled else None
                if missing is not None:
                    cls._report_error(
                        f"the content model requires element '{missing}', "
                        "which is missing from the xml",
                        code="occurrence-min",
                        element=cls.__name__,
                    )

        # Children are matched (and recorded) in document order so the
        # instance tree preserves the xml's layout. Several uses of one
        # declaration (for example repeated group references) are
        # consumed in declaration order, and when a declaration repeats
        # every occurrence aggregates into one accessor list.
        descriptorQueues: dict[str, list] = {}
        for descriptor in elemDescriptors:
            descriptorQueues.setdefault(cls._instance_name_of(descriptor), []).append(descriptor)
        descriptorUses: dict[str, int] = {}

        for elementIndex, subElement in enumerate(subElements):
            subElementName = cls._node_name(subElement)
            admitted = admittedBy.get(elementIndex)
            if admitted is not None and admitted.kind == "any" and admitted.spec is not None:
                # The model admitted the child through this wildcard
                # particle. Bind it through that particle even when a
                # declaration with the same name exists elsewhere in the
                # model: position decides, not the name.
                cls._bindWildcardChild(instance, subElement, admitted.spec)
                continue

            matched = False
            queue = descriptorQueues.get(subElementName)
            if queue:
                matched = True
                used = descriptorUses.get(subElementName, 0)
                bindingDescriptor = queue[min(used, len(queue) - 1)]
                descriptorUses[subElementName] = used + 1
                descriptor = bindingDescriptor
                if (
                    admitted is not None
                    and admitted.kind == "element"
                    and admitted.descriptor is not None
                    and admitted.descriptor is not bindingDescriptor
                ):
                    # The particle that consumed the child carries its
                    # own declaration (a repeated declaration, or a
                    # group's per-use copy): validate through it while
                    # the accessor slot stays with the class descriptor.
                    descriptor = admitted.descriptor
                repeated = len(queue) > 1 or any(item.isList() for item in queue)
                if descriptor.isAbstract():
                    cls._report_error(
                        f"element '{subElementName}' is declared abstract; "
                        "only its substitution group members may appear in the xml",
                        code="abstract-element",
                        element=cls.__name__,
                    )
                subElCls = cls._classForChild(descriptor, subElement)
                if subElCls is None:
                    cls._report_error(
                        "no type in the schema corresponds to the type "
                        f"stated in the '{subElementName}' element",
                        code="unknown-type",
                        element=cls.__name__,
                    )
                    if _mode_for(cls).unresolved_type == "generic":
                        instance._children_.append(cls.makeGenericInstance(subElement))
                else:
                    cls._addChildInstance(
                        instance,
                        subElement,
                        subElCls,
                        descriptor,
                        aggregate=repeated,
                        leader=queue[0],
                        binding=bindingDescriptor,
                    )

            if not matched and substitutionGroups:
                matched = cls._addSubstitutionMember(instance, subElement, elemDescriptors)

            if not matched:
                wildcardSpec = None
                if admitted is not None and admitted.kind == "any" and admitted.spec is not None:
                    wildcardSpec = admitted.spec
                elif hasWildcard:
                    if strictNamespaces:
                        wildcardSpec = cls._wildcard_match(
                            wildcardSpecs, subElementName, targetNamespace
                        )
                    else:
                        wildcardSpec = WildcardSpec()
                if wildcardSpec is not None:
                    cls._bindWildcardChild(instance, subElement, wildcardSpec)
                elif _mode_for(cls).undeclared_content == "generic":
                    instance._children_.append(cls.makeGenericInstance(subElement))
        return instance

    @classmethod
    def _resolveXsiTypeName(cls, subElement, value: str, pyXSD) -> str | None:
        """Resolves a lexical ``xsi:type`` QName against the instance scope.

        In ``legacy`` namespace mode the raw value is returned unchanged.
        In ``strict`` mode the value is expanded through the instance
        namespace context; an unbound prefix is reported as
        ``unknown-namespace-prefix`` and ``None`` is returned so the
        caller keeps the declared type.
        """
        mode = getattr(pyXSD, "mode", None)
        if getattr(mode, "namespaces", "legacy") != "strict":
            return value
        context = getattr(pyXSD, "namespaceContext", None)
        if context is None:
            return value
        try:
            return context.resolve(subElement, value)
        except NamespaceError as exc:
            cls._report_error(str(exc), code="unknown-namespace-prefix", element=cls.__name__)
            return None

    @classmethod
    def _classForChild(cls, descriptor, subElement):
        """Resolves the class used to build one matched child element.

        Uses the declared type by default; ``xsi:type`` on the xml
        element dispatches to another schema type. Unresolvable
        ``xsi:type`` values are recorded on the validation report and
        the declared type is kept. Returns ``None`` when no type can
        be resolved.
        """
        subElCls = descriptor.getType() if descriptor is not None else None
        xsiTypeName = xsi.xsi_type_name(subElement)
        if xsiTypeName is None:
            return subElCls
        pyXSD = getattr(cls, "pyXSD", None)
        resolvedName = cls._resolveXsiTypeName(subElement, xsiTypeName, pyXSD)
        if resolvedName is None:
            return subElCls
        resolved = ElementRepresentative.typeFromName(resolvedName, pyXSD)
        if resolved is not None:
            blocked = combinedBlock(
                descriptor.getBlock() if descriptor is not None else None,
                subElCls,
            )
            reason = is_validly_derived(resolved, subElCls, blocked)
            if reason is not None:
                cls._report_error(
                    derivationMessage(resolved, subElCls, reason),
                    code="xsi-type",
                    element=cls.__name__,
                )
                return subElCls
            logger.debug(
                "Element %r dispatched via xsi:type to %s", subElement.tag, resolved.__name__
            )
            return resolved
        cls._report_error(
            f"xsi:type '{xsiTypeName}' on element "
            f"'{subElement.tag.split('}')[-1]}' does not correspond to a "
            "type in the schema",
            code="xsi-type",
            element=cls.__name__,
        )
        return subElCls

    @classmethod
    def _addChildInstance(
        cls,
        instance,
        subElement,
        subElCls,
        descriptor,
        *,
        aggregate=False,
        leader=None,
        binding=None,
    ):
        """Builds and stores the instance for one matched child element.

        Handles ``xsi:nil`` (nillable elements carry no content to
        validate), ``fixed`` value checking on simple content, and the
        primitive/complex split. Appends the built instance to the
        parent's ``_children_`` and, for primitive content, exposes it
        as an instance attribute. ``aggregate`` is true when the
        declaration occurs in more than one particle of the effective
        model; ``leader`` is the first descriptor for the declaration,
        which owns the accessor and storage slot in that case.
        ``binding`` is the class descriptor that owns the accessor slot
        when it differs from ``descriptor`` (the validating declaration
        carried by the matched particle); it defaults to ``descriptor``.
        """
        subElementName = cls._node_name(subElement)
        nilled = xsi.xsi_nil_is_true(subElement)
        if nilled and not descriptor.isNillable():
            cls._report_error(
                f"element '{subElementName}' carries xsi:nil but its declaration is not nillable",
                code="nil",
                element=cls.__name__,
            )
            nilled = False
        if nilled and descriptor.getFixed() is not None:
            cls._report_error(
                f"element '{subElementName}' is marked nil but its declaration has a fixed value",
                code="nil",
                element=cls.__name__,
            )

        if aggregate and leader is not None:
            storage = leader
        elif binding is not None:
            storage = binding
        else:
            storage = descriptor
        accessor, descriptorBound = cls._childAccessor(instance, storage, subElement)

        # for elements with primitive types
        contentKind = getattr(subElCls, "_contentKind_", None)
        isComplex = (
            contentKind == "complex"
            if contentKind is not None
            else issubclass(subElCls, SchemaBase)
        )
        if nilled and isComplex:
            # A nilled element carries no content to validate: the
            # emptiness rule is checked, declared attributes are still
            # validated, and an empty shell is bound.
            cls._checkNilContent(subElement, subElementName)
            subInstance = cls._nilledInstance(subElCls, subElement, subElementName)
            subInstance._descriptor_ = descriptor
            subInstance._nil_ = True
            instance._children_.append(subInstance)
            return None
        if not isComplex:
            if nilled:
                subInstance = cls._nilPrimitive(subElCls, subElement, subElementName)
            else:
                subInstance = cls._primitiveForElement(subElCls, subElement, descriptor)
            if subInstance is not None:
                # invalid values are skipped; the error is
                # already in the report
                subInstance._name_ = subElementName
                # Identity constraints are checked against this
                # descriptor after the tree is fully bound.
                subInstance._descriptor_ = descriptor
                subInstance._nil_ = nilled
                instance._children_.append(subInstance)
                if descriptorBound:
                    bound = getattr(type(instance), accessor, None)
                    if aggregate:
                        # Several particles use this declaration: every
                        # occurrence aggregates through the leader's slot.
                        storage.bind(instance, subInstance, append=True)
                    elif bound is storage:
                        setattr(instance, accessor, subInstance)
                    elif bound is None:
                        # A substitution-group member descriptor is not a
                        # class attribute; store it plainly like the
                        # historical setattr did.
                        instance.__dict__[accessor] = subInstance
                    else:
                        # Another declaration shadows the accessor in a
                        # subclass; store through the declaring
                        # descriptor itself.
                        storage.bind(instance, subInstance)
                else:
                    instance.__dict__[accessor] = subInstance
                if not nilled:
                    cls._checkFixedElement(descriptor, subElCls, subInstance, subElementName)
            return None

        forcedText = None
        if subElement.text is None and not list(subElement):
            forcedText = descriptor.getDefault()
            if forcedText is None:
                forcedText = descriptor.getFixed()
        subInstance = subElCls.makeInstanceFromTag(subElement, forcedText)
        subInstance._name_ = subElementName
        subInstance._descriptor_ = descriptor
        subInstance._nil_ = nilled
        instance._children_.append(subInstance)
        if (
            not nilled
            and getattr(subElCls, "_simpleContentType_", None) is not None
            and getattr(subInstance, "_value_", None) is not None
        ):
            # Simple-content complex types carry a scalar value whose
            # fixed declaration constrains it like a primitive's.
            cls._checkFixedElement(descriptor, subElCls, subInstance, subElementName)
        return None

    @classmethod
    def _childAccessor(cls, instance, descriptor, subElement):
        """Returns the Python attribute name for a matched child.

        The base name is the declaration's local name, which is also the
        descriptor's bound name (so ``setattr`` reaches the descriptor).
        A descriptor aliased at class-build time (element/attribute name
        collision) binds through its alias instead. In strict namespace
        mode two declarations that share a local name but differ in
        namespace would collide; the second is exposed as
        ``local_prefix`` (using the instance's in-scope prefix, or a
        numeric suffix when the namespace is the default). Returns
        ``(name, descriptor_bound)``.
        """
        base = getattr(descriptor, "name", None) or subElement.tag.split("}")[-1]
        if getattr(descriptor, "_aliased_", False):
            # The descriptor was re-keyed at class-build time because
            # an attribute took the natural accessor (element/attribute
            # name collision); ``setattr`` must target the alias so the
            # value reaches the element descriptor.
            return getattr(descriptor, "bindingKey", base), True
        if getattr(_mode_for(cls), "namespaces", "legacy") != "strict":
            return base, True
        used = instance.__dict__.setdefault("_childAccessors_", {})
        uri = getattr(descriptor, "getNamespace", lambda: None)()
        previous = used.get(base)
        if previous is None or previous == uri:
            used[base] = uri
            return base, True
        prefix = ""
        parser = getattr(cls, "pyXSD", None)
        context = getattr(parser, "namespaceContext", None)
        if context is not None:
            try:
                prefix = context.prefix_for(subElement, uri) or ""
            except Exception:
                prefix = ""
        accessor = f"{base}_{prefix}" if prefix else f"{base}_{len(used)}"
        used[accessor] = uri
        return accessor, False

    @classmethod
    def _primitiveForElement(cls, subElCls, subElement, descriptor):
        """Builds a typed instance for a primitive-typed child element.

        An empty element (no text, no children) takes its declared
        ``default`` value, or its ``fixed`` value when there is no
        default; otherwise the lexical content is validated normally
        (see ``primitiveValueFor``). Invalid values yield ``None``
        with the error already on the validation report.
        """
        emptyContent = subElement.text is None and not list(subElement)
        if emptyContent:
            forced = descriptor.getDefault()
            if forced is None:
                forced = descriptor.getFixed()
            if forced is not None:
                return cls._valueForcedPrimitive(subElCls, subElement, forced, "default")
        return cls.primitiveValueFor(subElCls, subElement)

    @classmethod
    def _valueForcedPrimitive(cls, subElCls, subElement, forcedValue, code):
        """Builds a typed instance for a forced (default/fixed) value.

        The forced value must be valid for the element's type; a
        violation is a schema problem and is recorded with the given
        report code (``default`` or ``fixed-element``).
        """
        try:
            with qname_context(cls._qname_bindings(subElement)):
                instance = subElCls(forcedValue)
        except (TypeError, ValueError) as e:
            cls._report_error(
                f"the forced value {forcedValue!r} of the "
                f"'{subElement.tag.split('}')[-1]}' element is not valid "
                f"for its type: {e}",
                code=code,
                element=cls.__name__,
            )
            return None
        instance._attribs_ = {
            xsi.xsi_attr_key(key): value for key, value in subElement.attrib.items()
        }
        instance._value_ = None
        instance._children_ = list(subElement)
        return instance

    @classmethod
    def _checkNilContent(cls, subElement, subElementName):
        """Enforces the nil emptiness rule with code ``nil``.

        An element carrying ``xsi:nil="true"`` may have attributes but
        no character or element content; whitespace counts as character
        content. The offending content itself is never bound.
        """
        kind = nil_content_kind(subElement)
        if kind == "elements":
            cls._report_error(
                f"element '{subElementName}' is marked nil but contains child elements",
                code="nil",
                element=cls.__name__,
            )
        elif kind == "characters":
            cls._report_error(
                f"element '{subElementName}' is marked nil but contains character content",
                code="nil",
                element=cls.__name__,
            )
        return None

    @classmethod
    def _nilledInstance(cls, subElCls, subElement, subElementName):
        """Builds an attribute-validated empty shell for a nilled element.

        Declared attributes (required, prohibited, fixed, defaults) are
        checked exactly as they would be for a non-nilled element; the
        content model and value validation are skipped, because a
        nilled element has no content.
        """
        unvalidated = getattr(subElCls, "_unvalidated", None)
        # Simple-content classes are datatype subclasses whose
        # constructor demands a lexical value; a bare shell skips it.
        subInstance = unvalidated() if unvalidated is not None else subElCls()
        subInstance._name_ = subElementName
        subElCls.addAttributesTo(subInstance, subElement)
        subInstance._value_ = None
        subInstance._children_ = []
        return subInstance

    @classmethod
    def _nilPrimitive(cls, subElCls, subElement, subElementName):
        """Builds an unvalidated instance for a nillable primitive element.

        A nillable element may carry no content, so no lexical form is
        available; the bare instance keeps the raw attributes (which
        include ``xsi:nil``) for the writers. Content on a nilled
        element is reported (code ``nil``) and not bound.
        """
        try:
            subInstance = subElCls._unvalidated()
        except Exception:
            cls._report_error(
                f"could not build a nil instance for the '{subElementName}' element",
                code="value",
                element=cls.__name__,
            )
            return None
        cls._checkNilContent(subElement, subElementName)
        subInstance._attribs_ = {
            xsi.xsi_attr_key(key): value for key, value in subElement.attrib.items()
        }
        subInstance._value_ = None
        subInstance._children_ = []
        return subInstance

    @classmethod
    def _checkFixedElement(cls, descriptor, subElCls, subInstance, subElementName):
        """Validates a primitive element's value against ``fixed``."""
        fixed = descriptor.getFixed()
        if fixed is None:
            return None
        try:
            fixedInstance = subElCls(fixed)
        except Exception:
            cls._report_error(
                f"fixed value {fixed!r} of element '{subElementName}' is not valid for its type",
                code="fixed-element",
                element=cls.__name__,
            )
            return None
        if xsd_value_key(subInstance) != xsd_value_key(fixedInstance):
            cls._report_error(
                f"element '{subElementName}' has a value that conflicts "
                f"with its fixed value {fixed!r}",
                code="fixed-element",
                element=cls.__name__,
            )
        return None

    @classmethod
    def _addSubstitutionMember(cls, instance, subElement, elemDescriptors):
        """Parses a child as a substitution-group member, if it is one.

        Matches the xml child name against the members registered
        under each declared element (the head). Returns True when the
        child was handled. Members blocked by the head's ``block``
        attribute are reported and rejected.
        """
        subElementName = cls._node_name(subElement)
        declared = {descriptor.name: descriptor for descriptor in elemDescriptors}
        declaredExpanded = {
            getattr(descriptor, "expandedName", None): descriptor for descriptor in elemDescriptors
        }
        for headName, members in cls._schemaSubstitutionGroups(elemDescriptors).items():
            headDescriptor = declared.get(headName) or declaredExpanded.get(headName)
            if headDescriptor is None:
                continue
            for memberER in members:
                if cls._instance_name_of(memberER) != subElementName:
                    continue
                block = headDescriptor.getBlock()
                if block and ("substitution" in block.split() or block == "#all"):
                    cls._report_error(
                        f"substitution-group member '{subElementName}' is "
                        f"blocked by head element '{headName}' (block={block!r})",
                        code="blocked",
                        element=cls.__name__,
                    )
                    return False
                if memberER.tagAttributes.get("type"):
                    subElCls = memberER.getType()
                else:
                    subElCls = headDescriptor.getType()
                if subElCls is None:
                    return False
                # An xsi:type on the member overrides the member's
                # declared type, provided it is validly derived.
                xsiTypeName = xsi.xsi_type_name(subElement)
                if xsiTypeName is not None:
                    override = ElementRepresentative.typeFromName(
                        xsiTypeName, getattr(cls, "pyXSD", None)
                    )
                    if override is not None:
                        blocked = combinedBlock(memberER.getBlock(), subElCls)
                        reason = is_validly_derived(override, subElCls, blocked)
                        if reason is None:
                            subElCls = override
                        else:
                            cls._report_error(
                                derivationMessage(override, subElCls, reason),
                                code="xsi-type",
                                element=cls.__name__,
                            )
                # The member declaration supplies the value constraints
                # (nillable, default, fixed, identity); the head's
                # particle supplied the occurrence match.
                cls._addChildInstance(instance, subElement, subElCls, memberER)
                return True
        return False

    @staticmethod
    def _schemaSubstitutionGroups(elemDescriptors):
        """Returns the substitution-group map of the owning schema.

        Used by both the dispatch path and the declared-name
        computation. Overlay classes without ER ancestry get an empty
        map.
        """
        if not elemDescriptors:
            return {}
        try:
            schemaER = elemDescriptors[0].getSchema()
        except Exception:
            return {}
        return getattr(schemaER, "substitutionGroups", None) or {}

    @classmethod
    def addValueTo(cls, instance, elementTag):
        """Checks to see if the tag has a value, and assigns it to the
        element instance if it does.

        Uses the ElementTree function ``.text`` to retrieve this
        information from the tag.
        """
        if not elementTag.text:
            return
        if elementTag.get(f"{{{XML_NS}}}space") == "preserve":
            # ``xml:space="preserve"`` asks the parser to keep the
            # character data exactly, including leading, trailing, and
            # repeated whitespace.  The default path below strips and
            # splits lines, which is only appropriate for the untyped
            # pass-through case.
            instance._value_ = [elementTag.text]
            return
        instance._value_ = []
        if "\n" in elementTag.text.rstrip("\n"):
            dataEntry = elementTag.text.splitlines()
            for line in dataEntry:
                line = line.strip()
                if line:
                    instance._value_.append(line)
        else:
            stripped = elementTag.text.strip()
            if stripped:
                instance._value_.append(stripped)
        instance._value_ = instance._value_ if instance._value_ else None

    @classmethod
    def checkElementOrderInChoice(cls, descriptors, subElements, memberHeadMap):
        """Checks to see that elements in a choice field follow the rules
        of such a field.

        Finds the ``choice`` compositor that owns the descriptors and
        checks the total child count against the choice's own
        ``minOccurs``/``maxOccurs``. Each element in the choice is then
        checked against its own ``maxOccurs`` (``minOccurs`` is not
        applicable to individual branches: any branch may be the one
        that does not appear).

        - ``descriptors`` - the schema-specified elements that make up
          the choice.

        - ``subElements`` - the declared children of an element being
          processed in ``addElementsTo()``.

        - ``memberHeadMap`` - substitution-group member name to head
          name, so member children count toward the head's limits.
        """
        subElementNames = [cls._node_name(elem) for elem in subElements]
        subElementNames = [memberHeadMap.get(name, name) for name in subElementNames]

        choiceER = None
        for descriptor in descriptors:
            parent = getattr(descriptor, "parent", None)
            if parent is not None and parent.__class__.__name__ == "Choice":
                choiceER = parent
                break

        if choiceER is not None:
            minOccurs = choiceER.getMinOccurs()
            maxOccurs = choiceER.getMaxOccurs()
        else:
            # Defensive fallback: derive the limits from the first
            # descriptor (the pre-phase-8 behavior).
            minOccurs = descriptors[0].getMinOccurs()
            maxOccurs = descriptors[0].getMaxOccurs()

        if minOccurs < 0:
            cls._report_warning(
                "the value of 'minOccurs' must be greater than or equal to "
                "zero; continuing with the default value of 1",
                code="schema",
                element=cls.__name__,
            )
            minOccurs = 1

        if maxOccurs < 0:
            cls._report_warning(
                "the value of 'maxOccurs' must be greater than or equal to "
                "zero; continuing with the default value of 1",
                code="schema",
                element=cls.__name__,
            )
            maxOccurs = 1

        if len(subElementNames) < minOccurs:
            cls._report_error(
                "the xml does not contain enough elements for the choice "
                f"(minOccurs is {minOccurs})",
                code="occurrence-min",
                element=cls.__name__,
            )

        elif len(subElementNames) > maxOccurs:
            cls._report_error(
                f"the xml contains too many elements for the choice (maxOccurs is {maxOccurs})",
                code="occurrence-max",
                element=cls.__name__,
            )

        if choiceER is not None:
            for descriptor in descriptors:
                count = subElementNames.count(cls._instance_name_of(descriptor))
                if count > descriptor.getMaxOccurs():
                    cls._report_error(
                        f"element '{descriptor.name}' occurs more times than "
                        f"maxOccurs ({descriptor.getMaxOccurs()}) allows "
                        "within the choice",
                        code="occurrence-max",
                        element=cls.__name__,
                    )

        return None

    @classmethod
    def checkElementOrderInAll(cls, descriptors, subElements, memberHeadMap):
        """Checks the occurrence counts in an ``all`` content model.

        Unlike ``sequence``, ``all`` does not constrain the order of
        children, so each element's occurrence count is checked
        against its ``minOccurs``/``maxOccurs`` limits regardless of
        position. Children that match no declared element are ignored
        here (wildcard pass-through handles them upstream).

        - ``descriptors`` - the schema-specified elements of the
          ``all`` compositor.

        - ``subElements`` - the declared children of an element being
          processed in ``addElementsTo()``.

        - ``memberHeadMap`` - substitution-group member name to head
          name, so member children count toward the head's limits.
        """
        subElementNames = [cls._node_name(elem) for elem in subElements]
        subElementNames = [memberHeadMap.get(name, name) for name in subElementNames]
        for descriptor in descriptors:
            count = subElementNames.count(cls._instance_name_of(descriptor))
            if count < descriptor.getMinOccurs():
                cls._report_error(
                    f"element '{descriptor.name}' occurs fewer times than "
                    f"minOccurs ({descriptor.getMinOccurs()}) requires",
                    code="occurrence-min",
                    element=cls.__name__,
                )
                continue
            if count > descriptor.getMaxOccurs():
                cls._report_error(
                    f"element '{descriptor.name}' occurs more times than "
                    f"maxOccurs ({descriptor.getMaxOccurs()}) allows",
                    code="occurrence-max",
                    element=cls.__name__,
                )

    @classmethod
    def checkElementOrderInSequence(cls, descriptors, subElements, memberHeadMap):
        """Checks the element order in sequence fields to make sure that
        the order specified in the schema is preserved in the xml.

        Records non-fatal issues when a problem is found. Checks
        minOccurs and maxOccurs on each element as well.

        - ``descriptors`` - a list of schema-specified elements that
          define parameters for an element. Called ``descriptors``
          because the program takes advantage of descriptors in Python
          to help check the data. These descriptors are in the Element
          class in element_representatives.

        - ``subElements`` - all of the children of an element that is
          being processed in ``addElementsTo()``. Correspond to
          elements in ``descriptors``.

        - ``memberHeadMap`` - substitution-group member name to head
          name, so member children are validated in place of the head.
        """
        descriptorNames = [cls._instance_name_of(d) for d in descriptors]
        subElementNames = [cls._node_name(elem) for elem in subElements]
        subElementNames = [memberHeadMap.get(name, name) for name in subElementNames]
        for index in range(0, len(descriptors)):
            descriptor = descriptors[index]
            dname = descriptorNames[index]
            displayName = descriptor.name or dname
            count, subElementNames = cls.consume(dname, subElementNames)

            if count == 0:
                if descriptor.getMinOccurs() == 0 and dname not in subElementNames:
                    continue
                cls._report_error(
                    f"order error - expected element '{displayName}' in a different position",
                    code="order",
                    element=cls.__name__,
                )
                continue

            if count < descriptor.getMinOccurs():
                cls._report_error(
                    f"element '{displayName}' occurs fewer times than minOccurs "
                    f"({descriptor.getMinOccurs()}) requires; this may also "
                    "indicate an ordering problem",
                    code="occurrence-min",
                    element=cls.__name__,
                )
                continue

            if count > descriptor.getMaxOccurs():
                cls._report_error(
                    f"element '{displayName}' occurs more times than maxOccurs "
                    f"({descriptor.getMaxOccurs()}) allows",
                    code="occurrence-max",
                    element=cls.__name__,
                )
                continue

    @classmethod
    def consume(cls, dname, subElements):
        """Used to check the number of times an element type in the schema
        is used with the xml elements. Used by
        ``checkElementOrderInSequence()``.

        - ``dname`` - the name of the descriptor that is currently
          being checked.

        - ``subElements`` - the list of subElement names being checked.
        """
        count = 0
        while len(subElements) > 0 and subElements[0] == dname:
            subElements = subElements[1:]
            count += 1

        return count, subElements

    @classmethod
    def primitiveValueFor(cls, subElCls, subElement):
        """Used to check and assign primitive values to an instance.

        Called by ``addElementsTo()``.  NOTE: this method may not work
        correctly for all elements with primitive data types.

        - ``subElCls`` - the schema type class that corresponds to the
          subElement that is being processed.

        - ``subElement`` - the subElement that has a primitive data
          type.
        """
        dataTypeChildren = list(subElement)
        dataTypeText = subElement.text

        if dataTypeChildren:
            # A simple-typed element cannot contain child elements, and
            # an undeclared attribute must never stand in for its text
            # value.
            cls._report_error(
                f"the '{subElement.tag.split('}')[-1]}' element has a "
                "simple type but contains child elements",
                code="unexpected-element",
                element=cls.__name__,
            )
            return None

        # Empty simple content is the empty lexical form: valid for
        # string types and invalid for everything else (XSD has no
        # 'empty element means true' convention, though 0.1 built
        # True here, making <x/> silently read as the string 'True').
        dataTypeVal = dataTypeText if dataTypeText is not None else ""

        try:
            with qname_context(cls._qname_bindings(subElement)):
                dataTypeValInst = subElCls(dataTypeVal)
        except (TypeError, ValueError) as e:
            cls._report_error(
                f"the value of the '{subElement.tag.split('}')[-1]}' element "
                f"is not valid for its type: {e}",
                code="value",
                element=cls.__name__,
            )
            if _mode_for(cls).invalid_value == "raw":
                return cls._rawPrimitiveValue(subElement, dataTypeText)
            return None
        dataTypeValInst._attribs_ = dict(subElement.attrib)
        # Preserve the value as the datatype normalized it: a
        # whitespace-collapsing type yields the collapsed form, while an
        # ``xs:string`` (or ``xml:space="preserve"``) content keeps its
        # significant leading and trailing whitespace.  Do not use
        # ``str.strip()`` here -- it would discard the preserved spaces.
        dataTypeValInst._value_ = [str(dataTypeValInst)] if dataTypeText is not None else None
        dataTypeValInst._children_ = dataTypeChildren

        return dataTypeValInst

    @classmethod
    def _rawPrimitiveValue(cls, subElement, dataTypeText):
        """Binds an invalid lexical value as an unvalidated string.

        Used by the ``raw`` invalid-value policy so a data-mapping user
        keeps the original text (and the report still records the
        problem). The stored value keeps the document's exact spelling
        rather than the stripped lexical form.
        """
        instance: Any = AnySimpleType(dataTypeText if dataTypeText is not None else "")
        instance._attribs_ = dict(subElement.attrib)
        instance._value_ = [dataTypeText] if dataTypeText is not None else None
        instance._children_ = []
        return instance

    @classmethod
    def makeGenericInstance(cls, elementTag):
        """Builds a pass-through instance for wildcard (``xs:any``)
        content.

        Undeclared children permitted by a wildcard are stored raw:
        attributes keep their lexical values, text is split like
        untyped data (see ``addValueTo``), and children are recursed
        generically. Called by ``addElementsTo()`` when the
        instance's type declares a wildcard.

        - ``elementTag`` - the undeclared xml element to store raw.
        """
        instance = SchemaBase()
        instance._name_ = cls._node_name(elementTag)
        instance._attribs_ = dict(elementTag.attrib)
        cls.addValueTo(instance, elementTag)
        instance._children_ = [cls.makeGenericInstance(child) for child in elementTag]
        return instance

    # ------------------------------------------------------------------
    # Descriptor access
    # ------------------------------------------------------------------

    def _getElements(self):
        """Returns the element descriptors visible to this instance.

        Walks the MRO of the instance's class collecting each class's
        own ``Element`` descriptors, least-derived first, so an
        extension's content model comes out in XSD order: base
        elements before extension elements. The merge keys on the
        declaration name, not the class-attribute key: a derived
        declaration replaces every inherited declaration of the same
        name (including an inherited declaration that a derived
        attribute forced onto an alias key), while several distinct
        declarations of one name inside one class body are all kept.
        """
        ordered: dict[str, list] = {}
        for klass in reversed(type(self).__mro__):
            own = [klass.__dict__[key] for key in klass.__dict__.get("_elementNames_", ())]
            if not own:
                continue
            ownNames = {descriptor.name for descriptor in own}
            for name in [name for name in ordered if name in ownNames]:
                del ordered[name]
            for descriptor in own:
                ordered.setdefault(descriptor.name, []).append(descriptor)
        return [descriptor for entries in ordered.values() for descriptor in entries]

    def descAttributes(self):
        """Returns a dictionary of the attribute descriptors.

        These attributes are from the schema and use descriptors, which
        are specified in the Attribute class in
        element_representatives, that help check element attribute
        values. Walks the MRO collecting each class's own attribute
        descriptors (derived classes shadow bases by name). Uses lazy
        evaluation by storing the descriptor attributes in a variable
        called ``_descAttrs_``, which it returns if this variable is
        set.
        """
        if "_descAttrs_" in self.__dict__:
            return self._descAttrs_
        attrs = {}
        for klass in type(self).__mro__:
            for name in klass.__dict__.get("_attributeNames_", ()):
                if name not in attrs:
                    attrs[name] = klass.__dict__[name]

        self.__dict__["_descAttrs_"] = attrs

        return attrs

    def descAttributeNames(self):
        """Returns a list that has all of the names of attribute
        descriptors. Calls ``descAttributes()``, and returns a list of
        the keys from that dictionary.
        """
        return list(self.descAttributes().keys())

    def checkAttributes(self, usedAttrs, elementTag):
        """Checks to see that required attributes are used in the xml,
        and does other such checks on the attributes.

        Note: the attribute descriptors check the values in element
        attributes.

        - ``usedAttrs`` - a list containing the names of attributes
          that were put into the instance.

        - ``elementTag`` - the ElementTree tag for the instance that is
          being checked.
        """
        descriptorAttributes = self.descAttributes()

        descriptorAttributeNames = self.descAttributeNames()

        attrInElementTag = [xsi.xsi_attr_key(attr) for attr in elementTag.attrib]

        elementName = getattr(self, "_name_", None) or self.__class__.__name__

        if len(usedAttrs) > len(attrInElementTag):
            self._report_warning(
                "the parser recorded more attributes than the xml file contains",
                code="internal",
                element=elementName,
            )
        elif len(usedAttrs) < len(attrInElementTag):
            for attrET in attrInElementTag:
                if attrET not in usedAttrs:
                    self._report_warning(
                        f"attribute '{attrET}' is not declared in the schema and was not parsed",
                        code="unexpected-attribute",
                        element=elementName,
                    )
        for descriptorAttrName in descriptorAttributeNames:
            descriptor = descriptorAttributes[descriptorAttrName]
            matchName = self._instance_name_of(descriptor, is_attribute=True)
            found = matchName in usedAttrs
            attrUse = descriptor.getUse()
            if attrUse == "required" and not found:
                self._report_error(
                    f"attribute '{descriptorAttrName}' is required but was not found",
                    code="missing-attribute",
                    element=elementName,
                )
            if found and attrUse == "prohibited":
                self._report_error(
                    f"attribute '{descriptorAttrName}' is prohibited and "
                    "must not appear in the xml",
                    code="prohibited-attribute",
                    element=elementName,
                )
            attributeDescriptor = descriptor
            if found:
                self._checkFixedAttribute(attributeDescriptor, self, elementName)
            else:
                self._applyAttributeDefault(attributeDescriptor, self, elementName)

    def _checkFixedAttribute(self, attributeDescriptor, instance, elementName):
        """Validates a present attribute's value against ``fixed``.

        Both values are compared as typed values (through the
        attribute's type), so boolean spellings like 'true'/'1' agree.
        """
        fixed = attributeDescriptor.getFixed()
        if fixed is None:
            return None
        attributeType = attributeDescriptor.getType()
        if not issubclass(attributeType, XsdDataType):
            return None  # complex-typed attributes have no lexical fixed value
        stored = instance.__dict__.get(attributeDescriptor.name)
        try:
            fixedInstance = attributeType(fixed)
        except Exception:
            self._report_error(
                f"fixed value {fixed!r} of attribute '{attributeDescriptor.name}' "
                "is not valid for its type",
                code="fixed-attribute",
                element=elementName,
            )
            return None
        if xsd_value_key(stored) != xsd_value_key(fixedInstance):
            self._report_error(
                f"attribute '{attributeDescriptor.name}' has a value that "
                f"conflicts with its fixed value {fixed!r}",
                code="fixed-attribute",
                element=elementName,
            )
        return None

    def _applyAttributeDefault(self, attributeDescriptor, instance, elementName):
        """Applies ``default``/``fixed`` values for an absent attribute.

        Per XSD 1.0, an absent attribute whose declaration carries a
        default (or a fixed value) takes that value. The value is
        applied through the attribute descriptor, so it is validated
        and stored typed; it is deliberately not added to the raw
        ``_attribs_`` container, keeping xml output faithful to the
        input document.
        """
        default = attributeDescriptor.getDefault()
        fixed = attributeDescriptor.getFixed()
        value = default if default is not None else fixed
        if value is None:
            return None
        # Bind through the declaration's own descriptor so an inherited
        # element sharing the name cannot capture the value.
        attributeDescriptor.__set__(instance, value)
        return None

    @staticmethod
    def dumpCls(cls):
        """For debugging purposes only. Logs the contents of a class at
        debug level.

        - ``cls`` - the class to dump the contents of.
        """
        logger.debug("In dumpCls[%s] bases = %s", cls.__name__, cls.__bases__)

        for key, value in cls.__dict__.items():
            logger.debug("  %s - %r", key, value)


from pyxsd.element_representatives.attribute import Attribute  # noqa: E402
from pyxsd.element_representatives.element import Element  # noqa: E402
from pyxsd.element_representatives.element_representative import (  # noqa: E402
    ElementRepresentative,
    componentKind,
)
