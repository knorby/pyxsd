import logging
from typing import Any

from pyxsd.element_representatives.element_representative import ElementRepresentative
from pyxsd.namespaces import local_name, namespace_of
from pyxsd.schema_context import current_context
from pyxsd.xsd_data_types import XsdDataType

logger = logging.getLogger(__name__)


def _xsd_derived(value_cls: type | None, declared_cls: type | None) -> bool:
    """Whether *value_cls* is validly derived from *declared_cls* in XSD.

    Mirrors the storage check: an ``xsi:type`` value may be a Python
    subclass of the declared type, or XSD-derived where the Python
    lattice does not mirror the XSD hierarchy (``xs:integer`` from
    ``xs:decimal``, wild064.v2). Unresolvable pairs are not derived.
    """
    if value_cls is None or declared_cls is None:
        return False
    from pyxsd.derivation import is_valid_xsi_type

    try:
        return is_valid_xsi_type(value_cls, declared_cls) is None
    except Exception:
        return False


def _isTrue(value: Any) -> bool:
    """XSD ``xs:boolean`` truth: ``true``/``1``, case-insensitive."""
    return value is not None and str(value).strip().lower() in ("true", "1")


class Element(ElementRepresentative):
    """The class for the element tag.

    The element tag and the attribute tag are the most important in the
    xml and in the program, so this class contains some machinery that
    many of the other classes do not have. The element and attribute
    classes contain descriptor methods. By specifying ``__get__``,
    ``__set__``, and ``__delete__`` (with ``__get__`` and
    ``__set__`` being the most important), these methods specify how a variable is
    set and how it is retrieved. Any modification of these methods
    should be made under extreme caution! If some variable is set to
    some value that does not match the specifications in the schema, an
    error will be raised. These methods add a powerful layer of
    functionality with a small amount of code; however, these functions
    are almost invisible unless they raise an error, so developers
    should bear in mind these methods when modifying the program.

    When a generated class is created, ``__set_name__`` binds this
    descriptor to the class that declares it, and
    ``SchemaBase.__init_subclass__`` records the binding in the class's
    ``_elementNames_`` bookkeeping.
    """

    #: Child grammar of an ``element`` declaration: an optional
    #: annotation, at most one inline type, XSD 1.1 ``alternative`` type
    #: alternatives and identity constraints. Global and local element
    #: declarations share this content model.
    _ALLOWED_CHILDREN = (
        "annotation",
        "simpleType",
        "complexType",
        "key",
        "keyref",
        "unique",
        "alternative",
    )
    _MAX_ONE_CHILDREN = ("annotation", "simpleType", "complexType")
    #: ``alternative`` precedes the identity constraints in the XSD 1.1
    #: content model (saxonData CTA cta0045).
    _CHILD_ORDER = (
        ("simpleType", "complexType"),
        ("alternative",),
        ("key", "keyref", "unique"),
    )
    #: The inline-type slot holds mutually exclusive alternatives.
    _ONE_OF_SLOTS = frozenset({0})

    # Set by ComplexType._resolveElementRef for ``ref`` sites; the
    # owning parser is attached during clsFor.  Annotations only: the
    # attributes are assigned dynamically.
    referredElement: Any
    host: Any

    def __init__(self, xsdElement, parent):
        """Adds itself to the element list in its parent.

        Reference sites (``<xs:element ref="..."/>``) are detected
        before the base run so name assignment can account for them;
        their content model comes from the referenced global element
        declaration (see
        ``ComplexType._resolveElementRef``).

        See ElementRepresentative for documentation.
        """
        self.isElementRef = xsdElement.get("ref") is not None
        if self.isElementRef:
            self.ref = xsdElement.get("ref")
        # Identity constraints (xs:key/xs:unique/xs:keyref) declared
        # inside this element record themselves here.
        self.identities = []
        # XSD 1.1 type alternatives (xs:alternative) record themselves
        # here in declaration order; the schema phase compiles them and
        # the instance phase selects among them.
        self.alternatives = []
        super().__init__(xsdElement, parent)
        # A stray ``element`` declaration may appear inside a parent
        # that carries no element list (for example an identity
        # constraint); the parser reports the placement error, but the
        # ER run must not crash.
        elements = getattr(parent, "elements", None)
        if elements is not None:
            elements.append(self)
        else:
            logger.warning(
                "element declaration '%s' cannot be a child of %s; ignoring it",
                self.name,
                parent.__class__.__name__ if parent is not None else "nothing",
            )

    def getName(self):
        """Returns the element's schema name.

        Reference sites make a unique bookkeeping name like
        ``ContainingTypeName``|elementRef|``ref``; the resolved
        reference later adopts the referred declaration's name for
        matching (see ``_resolveElementRef``).
        """
        if getattr(self, "isElementRef", False):
            contName = self.getContainingTypeName()
            return f"{contName}|elementRef|{self.ref}"
        return self.xsdElement.get("name")

    def processChildren(self):
        """Processes children of a non-reference element.

        Reference elements carry no content model of their own (only
        annotations, which carry no parse-relevant information), so
        their children are not made into ERs. A non-reference element's
        children are filtered through ``_acceptChild`` so an illegal
        child is reported rather than factored.
        """
        if getattr(self, "isElementRef", False):
            return None
        children = list(self.xsdElement)

        if not children:
            return None

        for child in children:
            if not self._acceptChild(child):
                self.processedChildren.append(None)
                continue
            processedChild = ElementRepresentative.factory(child, self)
            self.processedChildren.append(processedChild)
            childClassName = processedChild.__class__.__name__ if processedChild is not None else ""
            if childClassName in ("SimpleType", "ComplexType"):
                self.type = processedChild.name
                self.tagAttributes["type"] = self.type
            # NOTE: the factory call above already processed the child's
            # children inside ElementRepresentative.__init__; do not
            # call processedChild.processChildren() again here (the old
            # code did, which constructed every grandchild ER twice and
            # double-registered sequences/elements).
        return None

    def getType(self):
        """Returns its type from the compiled class dictionary.

        Reference sites use the referenced declaration's type. The
        compiled schema's host is attached to every element and attribute
        while the classes for the schema types are being built.
        Clearly, this function is used after the main ER run.
        """
        if getattr(self, "isElementRef", False):
            return self.referredElement.getType()

        if "type" not in self.__dict__:
            # An element with no type attribute and no inline type
            # declaration defaults to anyType, which accepts any
            # content; SchemaBase instances parse generically.
            logger.debug(
                "element '%s' declares no type; treating it as anyType",
                self.name,
            )
            from pyxsd.schema_base import SchemaBase

            return SchemaBase

        # Resolve the QName first. In strict namespace mode this yields
        # an expanded name, which disambiguates types that share a local
        # name across namespaces (a strict-mode local-name fallback
        # would silently bind the wrong one). In legacy mode
        # ``resolvedTypeName`` returns the raw type, so this is the same
        # lookup as before. Declarations that are not installed as class
        # descriptors (for example a named group's shared elements) were
        # never stamped with ``host`` by the class builder; fall back to
        # the owning schema's parser, as ``instanceName`` does.
        parser = getattr(self, "host", None) or getattr(self.getSchema(), "host", None)
        resolved = self.resolvedTypeName()
        if parser is not None and resolved is not None and resolved in parser.classes:
            return parser.classes[resolved]

        return self.typeFromName(resolved, parser)

    def __set_name__(self, owner, name):
        """Called when this descriptor is bound as ``name`` on ``owner``.

        Stores the owning generated class and the class-attribute key
        it was bound under (``bindingKey``). For most descriptors the
        key equals the schema element name. When a type declares an
        element and an attribute with the same name, the element is
        re-keyed under an aliased name (``<name>_element``), and that
        alias is recorded here as an intentional binding.
        """
        self.owner = owner
        self.bindingKey = name
        if name != self.name:
            if getattr(self, "_aliased_", False):
                logger.debug(
                    "element descriptor for %r aliased as %r on %s",
                    self.name,
                    name,
                    owner.__name__,
                )
            else:
                logger.warning(
                    "element descriptor for %r was bound as %r on %s",
                    self.name,
                    name,
                    owner.__name__,
                )

    def _storageKey(self):
        """Returns the instance-dictionary key this descriptor stores under.

        Descriptors keep values in the instance ``__dict__`` keyed by
        their schema name so instance access and bookkeeping stay
        stable. An aliased descriptor (see ``__set_name__``) stores
        under its alias instead, so a same-named attribute and element
        never share one storage slot.
        """
        if getattr(self, "_aliased_", False):
            return getattr(self, "bindingKey", self.name)
        return self.name

    def __str__(self):
        """Prints its name in a form that allows for quick identification
        of an element, without needing a bulky name that does not match
        the name used.
        """
        return f"{self.getContainingTypeName()}|{self.__class__.__name__}|{self.name}"

    def __get__(self, obj, objtype=None):
        """Gets an element value from the obj's dictionary.

        Returns its value if it has one; returns the default value if
        it does not. When accessed through the class itself, returns
        the descriptor, per the descriptor protocol.

        See the Python documentation for full documentation on
        descriptors.
        """
        if obj is None:
            return self
        key = self._storageKey()
        if key in obj.__dict__:
            return obj.__dict__[key]

        default = getattr(self, "default", None)
        return default

    def __set__(self, obj, value):
        """Sets an element's value in the obj's dictionary.

        The value must be an instance of the element's type (or the
        assignment raises ``TypeError``). If the element may occur more
        than once (``maxOccurs`` greater than one), the value is
        appended to a list; otherwise it is stored directly.

        As with attributes, a scalar assignment also writes the lexical
        form through to the child node the writer serializes, so a
        later ``to_string()`` reflects it (F1). Only a bare value (no
        ``_name_``) triggers this: internal binding also assigns child
        nodes through descriptors, and a bound node must keep the
        container the binder gave it (notably a nilled node keeps
        ``_value_ is None``). A repeated element has no unambiguous
        target node, so only ``__dict__`` is updated there (constructing
        new child nodes is not supported).

        See the Python documentation for full documentation on
        descriptors.
        """
        self.bind(obj, value)
        if (
            current_context() is None
            and not self.isList()
            and getattr(value, "_name_", None) is None
        ):
            stored = obj.__dict__.get(self._storageKey())
            if isinstance(stored, XsdDataType):
                name = type(obj)._instance_name_of(self, is_attribute=False)
                for child in getattr(obj, "_children_", []) or []:
                    if getattr(child, "_name_", None) == name:
                        child._value_ = stored.lexical()
                        break
        return None

    def bind(self, obj, value, *, append: bool | None = None):
        """Stores ``value`` for this descriptor without MRO dispatch.

        Binding code calls this directly so a declaration always stores
        through its own descriptor even when another declaration shadows
        its accessor name in a subclass. ``append`` forces list
        aggregation (or scalar storage when false); ``None`` uses the
        declaration's own occurrence limit, which is the
        descriptor-protocol behavior.
        """
        if not isinstance(value, self.getType()):
            # An xsi:type value may be XSD-derived without being a Python
            # subclass (the xs:decimal → xs:integer step). Under the
            # ``raw`` invalid-value policy a primitive child whose
            # lexical value failed validation is bound as a plain string
            # so no data is lost; the validation report still records
            # the problem.
            parser = getattr(self, "host", None)
            policy = getattr(parser, "mode", None)
            if getattr(policy, "invalid_value", "drop") != "raw" and not _xsd_derived(
                type(value), self.getType()
            ):
                raise TypeError(
                    f"{value!r} is not an instance of the type of element "
                    f"{self.name!r} ({self.getType().__name__})"
                )

        key = self._storageKey()
        if self.isList() if append is None else append:
            obj.__dict__.setdefault(key, []).append(value)
            return None

        obj.__dict__[key] = value
        return None

    def __delete__(self, obj):
        """Deletes an entry from the dictionary.

        See the Python documentation for full documentation on
        descriptors.
        """
        del obj.__dict__[self._storageKey()]

    def isList(self):
        """Returns true if maxOccurs is greater than one.

        If it is true, treats all of the elements that are from the
        schema definition as a list. Otherwise returns false.
        """
        maxOccurs = self.getMaxOccurs()
        return maxOccurs > 1

    def getMinOccurs(self):
        """Returns an integer value for ``minOccurs``.

        If no ``minOccurs`` has been set, uses the default of 1. See
        ``ElementRepresentative._occursValue`` for the lexical
        validation that replaced the old unguarded ``int()`` call.
        """
        return self._occursValue("minOccurs")

    def getMaxOccurs(self):
        """Returns an integer value for ``maxOccurs``.

        If no ``maxOccurs`` has been set, uses the default of 1. If
        ``maxOccurs`` is set to 'unbounded', returns 99999, since this
        should cover about every case in which someone would use
        'unbounded'.
        """
        return self._occursValue("maxOccurs")

    def getDefault(self):
        """Returns the element's schema ``default`` value, or ``None``.

        Per XSD 1.0 a ``default`` value only applies to simple (or
        mixed) content: when the element is empty in the instance, the
        default supplies its value. Reference sites use the
        referenced declaration's value.
        """
        if getattr(self, "isElementRef", False):
            return self.referredElement.getDefault()
        return self.tagAttributes.get("default")

    def getFixed(self):
        """Returns the element's schema ``fixed`` value, or ``None``.

        A ``fixed`` element must either be absent or carry exactly
        that value (simple content only, per XSD 1.0). Reference
        sites use the referenced declaration's value.
        """
        if getattr(self, "isElementRef", False):
            return self.referredElement.getFixed()
        return self.tagAttributes.get("fixed")

    def isNillable(self):
        """Returns True when the element declaration is ``nillable``.

        ``nillable`` is an ``xs:boolean``: ``true`` and ``1`` (in any
        case, with surrounding whitespace) are true, ``false``/``0`` and
        an absent attribute are false. Reference sites use the
        referenced declaration's setting.
        """
        if getattr(self, "isElementRef", False):
            return self.referredElement.isNillable()
        return _isTrue(self.tagAttributes.get("nillable"))

    def isAbstract(self):
        """Returns True when the element declaration is ``abstract``.

        Abstract elements may not appear in instance documents
        directly; only their substitution group members can.
        Reference sites use the referenced declaration's setting.
        """
        if getattr(self, "isElementRef", False):
            return self.referredElement.isAbstract()
        return self.tagAttributes.get("abstract") == "true"

    def getBlock(self):
        """Returns the element's effective ``block`` attribute value.

        An explicit ``block`` on the declaration wins (an empty value
        means "nothing is blocked"); otherwise the schema's
        ``blockDefault`` supplies it. Reference sites use the referenced
        declaration's value.
        """
        if getattr(self, "isElementRef", False):
            return self.referredElement.getBlock()
        explicit = self.tagAttributes.get("block")
        if explicit is not None:
            return explicit
        return self.getSchemaBlockDefault()

    def getSubstitutionGroupHead(self, parser=None):
        """Returns the head named by the ``substitutionGroup`` attribute.

        In ``legacy`` mode this is the reference's local name. In
        ``strict`` mode the reference is resolved through the schema
        document's namespace context and returned as its expanded
        (Clark) name, so the parser matches it against the head
        declaration in the correct namespace rather than any same-named
        local element.
        """
        head = self.tagAttributes.get("substitutionGroup")
        if head is None:
            return None
        resolved = self.resolveSchemaQName(head, parser=parser)
        if namespace_of(resolved) is None:
            return local_name(resolved)
        return resolved

    def getSubstitutionGroupHeads(self, parser=None):
        """Returns every head named by the ``substitutionGroup`` attribute.

        XSD 1.1 allows an element declaration to belong to more than one
        substitution group: the attribute holds a whitespace-separated
        list of QNames (saxon subsgroup001/002). XSD 1.0 admitted a
        single head, which is the one-element case. Each name is
        resolved through the schema document's namespace context exactly
        as :meth:`getSubstitutionGroupHead` resolves a single head; an
        unresolvable name is returned as written so the caller can
        report it as an unknown head.
        """
        raw = self.tagAttributes.get("substitutionGroup")
        if raw is None:
            return []
        heads = []
        for token in raw.split():
            resolved = self.resolveSchemaQName(token, parser=parser)
            if namespace_of(resolved) is None:
                heads.append(local_name(resolved))
            else:
                heads.append(resolved)
        return heads

    #: Element ``final`` accepts only these tokens, plus ``#all`` alone.
    #: Notably ``substitution`` is *not* a legal final token.
    #: Attributes the schema for schemas allows on an ``xs:element``
    #: declaration (XSD 1.1 §3.3.2, adding ``targetNamespace``).
    _ELEMENT_ATTRIBUTES = frozenset(
        {
            "id",
            "name",
            "ref",
            "type",
            "substitutionGroup",
            "minOccurs",
            "maxOccurs",
            "default",
            "fixed",
            "nillable",
            "abstract",
            "block",
            "final",
            "form",
            "targetNamespace",
        }
    )
    _FINAL_TOKENS = (
        "extension",
        "restriction",
    )  #: Element ``block`` additionally accepts ``substitution``.
    _BLOCK_TOKENS = ("extension", "restriction", "substitution")
    #: Attributes only a non-reference local element may not carry.
    _LOCAL_ONLY_FORBIDDEN = ("abstract", "final", "substitutionGroup")
    #: Attributes a reference site must not redeclare.
    _REF_FORBIDDEN = (
        "type",
        "form",
        "default",
        "fixed",
        "nillable",
        "abstract",
        "block",
        "final",
        "substitutionGroup",
    )

    def checkDeclarationLegality(self):
        """Reports element-declaration attribute (XML) constraints.

        Covers the ``final``/``block`` token lists, the ``minOccurs``/
        ``maxOccurs`` ordering, the global-only attributes and the
        conflicts a ``ref`` reference site must not introduce (the
        reference site's content model is not factored, so those are
        checked from the raw attributes here).
        """
        self._checkElementOccurs()
        self._checkElementRef()
        self._checkElementUnknownAttributes()
        if getattr(self, "isElementRef", False):
            return
        self._checkElementValueConstraint()
        self._checkElementTypeConflict()
        self._checkElementName()
        self._checkElementBooleanAttributes()
        if self.isGlobalDeclaration():
            self._checkElementFinalAndBlock()
        else:
            self._checkLocalElementAttributes()
        self._checkElementAlternatives()

    def _checkElementAlternatives(self) -> None:
        """Compiles the element's XSD 1.1 ``xs:alternative`` children.

        The alternatives are collected in declaration order and their
        ``test`` expressions parsed with the CTA XPath subset; the type
        references and the derivation rules are checked once generated
        classes exist (see :func:`pyxsd.alternatives.check_element_alternatives`).
        A non-final alternative must carry a ``test``: only the final
        entry may omit it to become the default (XSD 1.1 §3.3.2.1, the
        ``{default type definition}`` mapping).
        """
        alternatives = getattr(self, "alternatives", None) or []
        if not alternatives:
            return
        from pyxsd.alternatives import compile_alternatives

        self.compiledAlternatives = compile_alternatives(self)
        for index, alternative in enumerate(alternatives):
            if alternative.test is None and index != len(alternatives) - 1:
                self._reportSchemaError(
                    f"type alternative {index + 1} of element '{self.name}' has "
                    "no test but is not the final alternative",
                    code="alternative-invalid",
                )

    def _checkElementName(self) -> None:
        """Reports a ``name`` that is not an ``xs:NCName``.

        The XML representation types ``name`` as ``xs:NCName``; a colon
        (a qualified name belongs in ``ref``) or a leading digit/dash is
        as illegal as an empty name.
        """
        name = self.xsdElement.get("name")
        if name is not None and "|" not in name and self._invalidNCName(name):
            self._reportSchemaError(
                f"element name '{name}' is not a valid NCName",
                code="declaration-attribute",
            )

    def _checkElementBooleanAttributes(self) -> None:
        """Reports ``abstract``/``nillable`` values outside xs:boolean.

        The lexical space is exactly true/false/1/0; ``isAbstract`` and
        the nillable helper otherwise read an unrecognised spelling as
        false, silently accepting an illegal schema.
        """
        for attr in ("abstract", "nillable"):
            value = self.xsdElement.get(attr)
            if value is not None and self._invalidBoolean(value):
                self._reportSchemaError(
                    f"element '{self.name}' has an invalid {attr} value "
                    f"'{value}'; expected true, false, 1 or 0",
                    code="declaration-attribute",
                )

    def _checkElementUnknownAttributes(self) -> None:
        """Reports attributes outside the element-declaration grammar.

        The schema for schemas fixes the attribute list of an
        ``xs:element`` declaration: id, name, ref, type,
        substitutionGroup, minOccurs, maxOccurs, default, fixed,
        nillable, abstract, block, final, form and (XSD 1.1)
        targetNamespace. A spelling such as the early-draft ``nullable``
        or an arbitrary attribute is a schema error (MS elemK007,
        elemN006). Foreign-namespace attributes are left to the
        implementation-defined extension point and skipped.
        """
        for attr in getattr(self.xsdElement, "attrib", {}) or {}:
            if attr in self._ELEMENT_ATTRIBUTES or "}" in attr:
                continue
            self._reportSchemaError(
                f"element declaration '{self.name}' has an unrecognised attribute '{attr}'",
                code="declaration-attribute",
            )

    def _checkElementValueConstraint(self) -> None:
        """Reports an element that carries both ``default`` and ``fixed``.

        An element declaration's {value constraint} is either a default
        or a fixed value; the XSD XML representation forbids both
        (e-props-correct, value-constraint consistency). The lexical
        value itself is validated after type classes are built, so a
        user-defined simple type's facets apply too.
        """
        if "default" in self.tagAttributes and "fixed" in self.tagAttributes:
            self._reportSchemaError(
                f"element '{self.name}' must not carry both a default and a fixed value",
                code="declaration-attribute",
            )

    def _checkElementTypeConflict(self) -> None:
        """Reports an element that names a type and declares an inline type.

        The ``type`` attribute and an inline ``simpleType``/``complexType``
        are mutually exclusive in the XML representation of an element
        declaration. The raw attribute is read from the element because
        ``processChildren`` overwrites ``tagAttributes['type']`` with the
        inline type's generated name.
        """
        if self.xsdElement.get("type") is None:
            return
        if any(tag in ("simpleType", "complexType") for tag in self.childTags):
            self._reportSchemaError(
                f"element '{self.name}' may not carry both a type and an inline type",
                code="declaration-attribute",
            )

    def _checkElementOccurs(self) -> None:
        minimum = self.getMinOccurs()
        maximum = self.getMaxOccurs()
        if minimum > maximum:
            self._reportSchemaError(
                f"element '{self.name}' has minOccurs={minimum} greater than maxOccurs={maximum}",
                code="declaration-attribute",
            )

    def _checkElementFinalAndBlock(self) -> None:
        final = self.tagAttributes.get("final")
        if final is not None and self._invalidTokenList(final, self._FINAL_TOKENS):
            self._reportSchemaError(
                f"element '{self.name}' has an invalid final value '{final}'; "
                "expected extension, restriction or #all",
                code="declaration-attribute",
            )
        block = self.tagAttributes.get("block")
        if block is not None and self._invalidTokenList(block, self._BLOCK_TOKENS):
            self._reportSchemaError(
                f"element '{self.name}' has an invalid block value '{block}'; "
                "expected extension, restriction, substitution or #all",
                code="declaration-attribute",
            )

    def _checkLocalElementAttributes(self) -> None:
        for attr in self._LOCAL_ONLY_FORBIDDEN:
            if attr in self.tagAttributes:
                self._reportSchemaError(
                    f"local element '{self.name}' must not carry '{attr}'",
                    code="declaration-attribute",
                )

    def _checkElementRef(self) -> None:
        if not getattr(self, "isElementRef", False):
            return
        if self.isGlobalDeclaration():
            self._reportSchemaError(
                f"global element '{self.name}' must not carry a ref attribute",
                code="declaration-attribute",
            )
        if self.xsdElement.get("name") is not None:
            self._reportSchemaError(
                f"element reference '{self.ref}' must not also declare a name",
                code="declaration-attribute",
            )
        for attr in self._REF_FORBIDDEN:
            if attr in self.tagAttributes:
                self._reportSchemaError(
                    f"element reference '{self.ref}' must not also carry '{attr}'",
                    code="declaration-attribute",
                )
        for child in self.xsdElement:
            if local_name(child.tag) in ("simpleType", "complexType"):
                self._reportSchemaError(
                    f"element reference '{self.ref}' must not also declare an inline type",
                    code="declaration-attribute",
                )
