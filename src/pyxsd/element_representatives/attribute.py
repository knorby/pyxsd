import logging
from typing import Any

from pyxsd.element_representatives.element_representative import (
    _PRIMITIVE_TYPES,
    ElementRepresentative,
)
from pyxsd.namespaces import XSI_NS
from pyxsd.xsd_data_types import AnySimpleType, Boolean, NCName, QName, XsdDataType

logger = logging.getLogger(__name__)


class Attribute(ElementRepresentative):
    """The class for the attribute tag.

    The element tag and the attribute tag are the most important in the
    xml and in the program, so this class contains some machinery that
    many of the other classes do not have. The element and attribute
    classes contain descriptor methods. By specifying ``__get__``,
    ``__set__``, and ``__delete__`` (with ``__get__`` and ``__set__``
    being the most important), these methods specify how a variable is
    set and how it is retrieved. Any modification of these methods
    should be made under extreme caution! Both Element and Attribute,
    primarily Attribute, use these descriptors to add a level of
    checking to the program. If some variable is set to some value that
    does not match the specifications in the schema, an error will be
    raised. These methods add a powerful layer of functionality with a
    small amount of code; however, these functions are almost invisible
    unless they raise an error, so developers should bear in mind these
    methods when modifying the program.
    """

    #: Child grammar of an ``attribute`` declaration: an optional
    #: annotation and at most one inline ``simpleType``. (The
    #: ``type``/inline-type conflict is an attribute check, not a child
    #: -grammar one.)
    _ALLOWED_CHILDREN = ("annotation", "simpleType")
    #: Attributes the schema for schemas allows on an ``xs:attribute``
    #: declaration (XSD 1.1 §3.2.2, adding ``targetNamespace`` and
    #: ``inheritable``).
    _ATTRIBUTE_ATTRIBUTES = frozenset(
        {
            "id",
            "name",
            "ref",
            "type",
            "use",
            "default",
            "fixed",
            "form",
            "targetNamespace",
            "inheritable",
        }
    )
    _MAX_ONE_CHILDREN = ("annotation", "simpleType")

    # The owning parser is attached during clsFor.  Annotation only:
    # the attribute is assigned dynamically.
    pyXSD: Any

    def __init__(self, xsdElement, parent):
        """Adds itself to the attribute dictionary in its containing
        type. See ElementRepresentative for documentation.

        Reference sites (``<xs:attribute ref="..."/>``) carry no
        ``name`` of their own; a bookkeeping name is assigned in
        ``getName`` and the site is resolved to the referenced global
        declaration when the generated class is built (see
        ``XsdType.resolveAttributeRefs``).
        """
        self.isAttributeRef = xsdElement.get("ref") is not None
        if self.isAttributeRef:
            self.ref = xsdElement.get("ref")
        super().__init__(xsdElement, parent)
        # Only types with an attribute table accept attribute
        # declarations; a misplaced attribute (inside a group
        # definition, for example) is reported rather than crash.
        container = self.getContainingType()
        attributes = getattr(container, "attributes", None)
        if attributes is not None:
            attributes[self.name] = self
        else:
            self.misplacement = (
                "misplaced-declaration",
                f"attribute '{self.name}' cannot be declared inside {container.__class__.__name__}",
            )

    def getName(self):
        """Returns the attribute's schema name.

        Reference sites make a unique bookkeeping name like
        ``ContainingTypeName``|attributeRef|``ref``; the resolved
        reference later adopts the referred declaration's name so
        instance matching and Python access work.
        """
        if getattr(self, "isAttributeRef", False):
            contName = self.getContainingTypeName()
            return f"{contName}|attributeRef|{self.ref}"
        return self.xsdElement.get("name")

    def __set_name__(self, owner, name):
        """Called when this descriptor is bound as ``name`` on ``owner``.

        Stores the owning generated class so error messages can name
        it, and warns if the class attribute name does not match the
        schema attribute name (they are normally identical; a mismatch
        means a descriptor was rebound under a different name).
        """
        self.owner = owner
        if name != self.name:
            logger.warning(
                "attribute descriptor for %r was bound as %r on %s",
                self.name,
                name,
                owner.__name__,
            )

    def __str__(self):
        """Prints its name in a form that allows for quick identification
        of an attribute, without needing a bulky name that does not
        match the name used.
        """
        return f"{self.getContainingTypeName()}|{self.__class__.__name__}|{self.getName()}"

    def processChildren(self):
        """There is a special ``processChildren()`` here to handle special
        types, which can be declared as a child of an attribute. If an
        attribute child can exist that is not a type, then this
        function will screw it up; however, as far as the developers
        knew at the time of writing this program, they cannot.

        A child the grammar rejects is not factored; ``_acceptChild``
        records it on ``unexpectedChildTags`` for the parser to report.
        """
        children = list(self.xsdElement)

        if not children:
            return None

        for child in children:
            if not self._acceptChild(child):
                self.processedChildren.append(None)
                continue
            processedChild = ElementRepresentative.factory(child, self)
            self.processedChildren.append(processedChild)
            # An unknown child (for example an ``xsd:notation``) does
            # not carry a type name; only an inline ``xsd:simpleType``
            # supplies one when the attribute has no explicit type.
            if processedChild is None:
                continue
            if processedChild.__class__.__name__ != "SimpleType":
                continue
            # An explicit ``type`` attribute wins over any child.
            if "type" in self.tagAttributes:
                continue
            self.type = processedChild.name
            self.tagAttributes["type"] = self.type
            # NOTE: the factory call above already processed the child's
            # children inside ElementRepresentative.__init__; the old
            # code's extra processedChild.processChildren() call here
            # constructed every grandchild ER twice.
        return None

    def getType(self):
        """Returns its type from the class dictionary in PyXSD.

        Reference sites use the referenced global declaration's type.
        The instance of PyXSD is attached to every element and attribute
        while the classes for the schema types are being built.
        Clearly, this function is used after the main ER run.

        An attribute declared inside a global ``attributeGroup`` is never
        installed as a class descriptor, so it never receives ``pyXSD``;
        fall back to the owning schema's parser (as ``Element.getType``
        does) so value-constraint validation can still resolve its type.
        """
        if getattr(self, "isAttributeRef", False):
            referred = getattr(self, "referredAttribute", None)
            if referred is None:
                raise TypeError(f"attribute reference {self.ref!r} was not resolved")
            return referred.getType()

        if "type" not in self.__dict__:
            # XSD: an attribute declaration with no ``type`` and no
            # inline ``simpleType`` child takes anySimpleType, which
            # accepts any value.
            return AnySimpleType

        # Resolve the QName first so strict mode disambiguates types
        # that share a local name across namespaces; in legacy mode this
        # is the same raw-type lookup as before.
        parser = getattr(self, "pyXSD", None) or getattr(self.getSchema(), "pyXSD", None)
        resolved = self.resolvedTypeName()
        if parser is not None and resolved is not None and resolved in parser.classes:
            return parser.classes[resolved]

        return self.typeFromName(resolved, parser)

    def __get__(self, obj, objtype=None):
        """Gets an attribute value from the obj's dictionary.

        Returns its value if it has one; returns the default value if
        it does not. When accessed through the class itself, returns
        the descriptor, per the descriptor protocol.

        See the Python documentation for full documentation on
        descriptors.
        """
        if obj is None:
            return self
        if self.name in obj.__dict__:
            return obj.__dict__[self.name]
        default = getattr(self, "default", None)
        return default

    def __set__(self, obj, value):
        """Sets values to attributes.

        Converts text Boolean values to binary values (integers 0 and
        1), and validates the value against the attribute's type.

        See the Python documentation for full documentation on
        descriptors.
        """
        if issubclass(self.getType(), XsdDataType):
            if self.getType() is Boolean and isinstance(value, str):
                if value in ("true", "True"):
                    value = 1
                elif value in ("False", "false"):
                    value = 0
            try:
                value = self.getType()(value)
            except Exception as e:
                message = f"attribute '{self.name}' has an invalid value: {e}"
                parser = getattr(self, "pyXSD", None)
                if parser is not None:
                    parser.report.add_error(
                        message,
                        code=getattr(e, "code", "invalid-attribute"),
                        element=getattr(obj, "_name_", None),
                    )
                else:
                    logger.error(message)
        elif not isinstance(value, self.getType()):
            # The declared type did not resolve to a validating datatype
            # (a malformed or unresolved declaration). Record a
            # structured error; assignment must never raise out of
            # instance binding.
            message = (
                f"attribute '{self.name}' has a value that cannot be "
                f"validated against its declared type"
            )
            parser = getattr(self, "pyXSD", None)
            if parser is not None:
                parser.report.add_error(
                    message,
                    code="invalid-attribute",
                    element=getattr(obj, "_name_", None),
                )
            else:
                logger.error(message)

        obj.__dict__[self.name] = value

    def __delete__(self, obj):
        """Deletes an entry from the dictionary.

        See the Python documentation for full documentation on
        descriptors.
        """
        del obj.__dict__[self.name]

    def getUse(self):
        """Returns the 'use' value, which says if the attribute is
        required or optional (the default).
        """
        if "use" not in self.__dict__:
            self.use = "optional"
        return self.use

    def getDefault(self):
        """Returns the attribute's schema ``default`` value, or ``None``.

        When the attribute is absent from an instance document, the
        default supplies its value. A reference site may override the
        referenced declaration's default.
        """
        if self.tagAttributes.get("default") is not None:
            return self.tagAttributes["default"]
        if getattr(self, "isAttributeRef", False):
            referred = getattr(self, "referredAttribute", None)
            if referred is not None:
                return referred.getDefault()
        return None

    def getFixed(self):
        """Returns the attribute's schema ``fixed`` value, or ``None``.

        A ``fixed`` attribute must either be absent (in which case it
        takes the fixed value) or carry exactly that value. A reference
        site may override the referenced declaration's fixed value.
        """
        if self.tagAttributes.get("fixed") is not None:
            return self.tagAttributes["fixed"]
        if getattr(self, "isAttributeRef", False):
            referred = getattr(self, "referredAttribute", None)
            if referred is not None:
                return referred.getFixed()
        return None

    #: Legal values of the ``use`` attribute (XSD 1.0/1.1).
    _USE_VALUES = frozenset({"optional", "required", "prohibited"})
    #: Attributes a reference site must not redeclare.
    _REF_FORBIDDEN = ("type", "form")

    def checkDeclarationLegality(self):
        """Reports attribute-declaration attribute (XML) constraints.

        Covers the "Attribute Declaration Properties Correct" schema
        representation constraint at the XML level: ``default``/``fixed``
        consistency and value validity, ``use``/``form`` legality and
        the global-only rule, ``ref`` conflicts, name/id lexical space
        and the XSI-namespace prohibition.
        """
        self._checkAttributeDefaultFixed()
        self._checkAttributeUnknownAttributes()
        self._checkAttributeUse()
        self._checkAttributeForm()
        self._checkAttributeRef()
        self._checkAttributeType()
        self._checkAttributeName()
        self._checkAttributeNamespace()

    def _checkAttributeUnknownAttributes(self) -> None:
        """Reports attributes outside the attribute-declaration grammar.

        The schema for schemas allows id, name, ref, type, use, default,
        fixed, form and (XSD 1.1) targetNamespace on an ``xs:attribute``
        declaration; anything else is a schema error. Foreign-namespace
        attributes are left to the implementation-defined extension
        point and skipped.
        """
        for attr in getattr(self.xsdElement, "attrib", {}) or {}:
            if attr in self._ATTRIBUTE_ATTRIBUTES or "}" in attr:
                continue
            self._reportSchemaError(
                f"attribute declaration '{self.name}' has an unrecognised attribute '{attr}'",
                code="declaration-attribute",
            )

    def _checkAttributeDefaultFixed(self) -> None:
        if "default" in self.tagAttributes and "fixed" in self.tagAttributes:
            self._reportSchemaError(
                f"attribute '{self.name}' must not carry both a default and a fixed value",
                code="declaration-attribute",
            )

    def _checkAttributeUse(self) -> None:
        use = self.tagAttributes.get("use")
        if use is None:
            return
        if self.isGlobalDeclaration():
            self._reportSchemaError(
                f"global attribute '{self.name}' must not carry a use attribute",
                code="declaration-attribute",
            )
            return
        if use not in self._USE_VALUES:
            self._reportSchemaError(
                f"attribute '{self.name}' has an invalid use value '{use}'; "
                "expected optional, required or prohibited",
                code="declaration-attribute",
            )
            return
        if "default" in self.tagAttributes and use != "optional":
            self._reportSchemaError(
                f"attribute '{self.name}' with a default value must have use='optional'",
                code="declaration-attribute",
            )
        elif "fixed" in self.tagAttributes and use == "prohibited":
            self._reportSchemaError(
                f"attribute '{self.name}' with a fixed value must not use use='prohibited'",
                code="declaration-attribute",
            )

    def _checkAttributeForm(self) -> None:
        form = self.tagAttributes.get("form")
        if form is None:
            return
        if self.isGlobalDeclaration():
            self._reportSchemaError(
                f"global attribute '{self.name}' must not carry a form attribute",
                code="declaration-attribute",
            )
        elif form not in ("qualified", "unqualified"):
            self._reportSchemaError(
                f"attribute '{self.name}' has an invalid form value '{form}'",
                code="declaration-attribute",
            )

    def _checkAttributeRef(self) -> None:
        ref = self.tagAttributes.get("ref")
        if self.isGlobalDeclaration():
            if ref is not None:
                self._reportSchemaError(
                    f"global attribute '{self.name}' must not carry a ref attribute",
                    code="declaration-attribute",
                )
            return
        if ref is None:
            return
        if self.xsdElement.get("name") is not None:
            self._reportSchemaError(
                f"attribute reference '{ref}' must not also declare a name",
                code="declaration-attribute",
            )
        for attr in self._REF_FORBIDDEN:
            if attr in self.tagAttributes:
                self._reportSchemaError(
                    f"attribute reference '{ref}' must not also carry '{attr}'",
                    code="declaration-attribute",
                )
        if "simpleType" in self.childTags:
            self._reportSchemaError(
                f"attribute reference '{ref}' must not also declare a simpleType",
                code="declaration-attribute",
            )
        self._checkRefFixedOverride(ref)

    def _checkRefFixedOverride(self, ref: str) -> None:
        fixed = self.tagAttributes.get("fixed")
        if fixed is None:
            return
        referred = self._referredAttribute(ref)
        if referred is None:
            return
        referredFixed = referred.tagAttributes.get("fixed")
        if referredFixed is not None and referredFixed != fixed:
            self._reportSchemaError(
                f"attribute reference '{ref}' fixed value '{fixed}' does not "
                f"match the referenced declaration's '{referredFixed}'",
                code="declaration-attribute",
            )

    def _referredAttribute(self, ref: str) -> Any:
        table = getattr(self.getSchema(), "components", None)
        if table is None:
            return None
        local = ref.split(":", 1)[-1]
        for entry in table.get(local) or ():
            if type(entry).__name__ == "Attribute" and entry.isGlobalDeclaration():
                return entry
        return None

    def _checkAttributeType(self) -> None:
        raw = self.xsdElement.get("type")
        if raw is None or getattr(self, "isAttributeRef", False):
            return
        if self._invalidQName(raw):
            self._reportSchemaError(
                f"attribute '{self.name}' has an invalid type QName '{raw}'",
                code="declaration-attribute",
            )
            return
        if "simpleType" in self.childTags:
            self._reportSchemaError(
                f"attribute '{self.name}' may not carry both a type and an inline simpleType",
                code="declaration-attribute",
            )
        resolved = self._resolvedType(raw)
        if type(resolved).__name__ == "ComplexType":
            self._reportSchemaError(
                f"attribute '{self.name}' type '{raw}' is a complex type; "
                "attribute types must be simple",
                code="declaration-attribute",
            )

    def _checkAttributeName(self) -> None:
        name = self.xsdElement.get("name")
        if name is None:
            return
        if name == "xmlns" or self._invalidNCName(name):
            self._reportSchemaError(
                f"attribute name '{name}' is not a valid NCName",
                code="declaration-attribute",
            )

    def _checkAttributeNamespace(self) -> None:
        if getattr(self, "isAttributeRef", False):
            return
        try:
            uri = self._declaredNamespace()
        except (AttributeError, RuntimeError):
            return
        if uri != XSI_NS:
            return
        if self._isInjectedBuiltin():
            # The parser-inbuilt declarations of the xsi namespace
            # (XSD 1.1 §3.2.7.2) are exactly the legal way a global
            # attribute lands there; only user declarations are
            # prohibited.
            return
        self._reportSchemaError(
            f"attribute '{self.name}' must not be in the XML Schema instance namespace",
            code="declaration-attribute",
        )

    def _isInjectedBuiltin(self) -> bool:
        """Whether this representative is a parser-injected component.

        The parser registers injected components (built-in ``xml``/``xsi``
        namespace attributes, spliced imports) in the namespace-override
        map keyed by ``id(xsdElement)``; a user declaration never shares
        an id with it.
        """
        schema = self.getSchema()
        injected = getattr(schema, "injectedBuiltinIds", None)
        element = getattr(self, "xsdElement", None)
        if not injected or element is None:
            return False
        return id(element) in injected

    def _declaredNamespace(self) -> str | None:
        """Returns this declaration's XSD target namespace, if any.

        Unlike :meth:`getNamespace`, a local declaration contributes to
        the target namespace only when its effective ``form`` is
        ``qualified`` (explicitly or through ``attributeFormDefault``).
        """
        schema = self.getSchema()
        if schema is None:
            return None
        if self.isGlobalDeclaration():
            # ``getNamespace`` is override-aware, which matters for the
            # synthetic XML-namespace attribute declarations the parser
            # injects under the schema root.
            return self.getNamespace()
        uri = schema.getNamespace()
        if uri is None:
            return None
        if self._localDeclarationIsQualified(schema, is_attribute=True):
            return uri
        return None

    def _resolvedType(self, raw: str) -> Any:
        """Returns the datatype class or type representative named by ``raw``.

        Built-ins resolve from ``_PRIMITIVE_TYPES`` (when prefixed with
        ``xs``/``xsd`` or when no user type claims the local name); a
        user-defined simple or complex type resolves to its
        representative so a complex one can be rejected. Returns ``None``
        when nothing matches.
        """
        if ":" in raw:
            prefix, local = raw.split(":", 1)
            if prefix in ("xs", "xsd"):
                return _PRIMITIVE_TYPES.get(local)
        else:
            local = raw
        table = getattr(self.getSchema(), "components", None)
        if table is not None:
            for entry in table.get(local) or ():
                if type(entry).__name__ in ("SimpleType", "ComplexType"):
                    return entry
        return _PRIMITIVE_TYPES.get(local)

    @staticmethod
    def _invalidQName(value: str) -> bool:
        if value.startswith("{"):
            # An already-expanded (Clark) name, as the parser writes for
            # the synthetic XML-namespace attribute declarations. Accept
            # it when it is well formed.
            return "}" not in value
        try:
            QName(value)
        except TypeError:
            return True
        return False

    @staticmethod
    def _invalidNCName(value: str) -> bool:
        try:
            NCName(value)
        except TypeError:
            return True
        return False
