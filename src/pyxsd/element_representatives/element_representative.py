"""The ElementRepresentative system.

The ElementRepresentative (ER) system converts the schema file into a
collection of classes that represent the types in the schema.  The
system takes in the ElementTree representation of the schema file.  The
PyXSD class calls ElementTree to parse the schema file in order to keep
all calls to ElementTree in one place.  The classmethod ``factory`` in
the ElementRepresentative class starts the system up, and it is the
function PyXSD calls.  ``factory`` reads an ElementTree element and
finds the class that has the same name as the element's tag type, then
makes an instance of that class.

In these tag type classes, the ``__init__`` function is the first to be
called.  Each of these classes is a subclass of ElementRepresentative,
and their ``__init__`` calls the ``__init__`` in the
ElementRepresentative class, which collects general information about
the name and makes calls to specific functions to help with this
operation.  Some of these functions are located in ElementRepresentative
exclusively, but many can also be found in the specific classes.  For
example, ``getName()`` normally calls a version of the function in the
ER class, but on some tags the ER version of ``getName()`` would not
generate a unique name in every case, or it would lack all the needed
information.  In the classes for such tags there is another version of
``getName()`` that overrides the ElementRepresentative version.  In
general, the most common methods are found in ElementRepresentative,
while methods that are very specific to a tag are found in the tag's
class.  All the methods needed to parse the tree are found in the ER
class.  Overriding classes must all have the same arguments in order
for the system to work.  The system was designed so that it is each
ElementRepresentative's job to:

- Collect all information from the element and put it in the
  appropriate place

- Construct ElementRepresentatives for all of the element's children

The ``__init__`` function
-------------------------

In the ER ``__init__`` function, a call is made to the
``processChildren`` method, which calls the ``factory`` on all of the
children of an element.  Since the ER ``__init__`` calls this method,
any variable assignment that is needed by the children must be made
before the call to the ER ``__init__`` function.  The children are
completely processed before the parent is fully finished.  Any
developer should be mindful of this fact when creating or changing an
``__init__`` method in a tag type class.

Each ``__init__`` function has two variables as arguments:

- ``xsdElement`` - the ElementTree element that is being converted to
  an ER

- ``parent`` - the parent ER for the tag being processed

Remember: the tag type classes are called from ``factory`` so each
``__init__`` must follow this same pattern in order to work.

Class construction
------------------

The classes are constructed in the XsdType class (the base class for
SimpleType and ComplexType) via ``clsFor()``, which builds new classes
with ``types.new_class()`` by supplying a namespace, tuple of bases,
and a name. As each class is created, the standard Python protocols do
the wiring: ``__set_name__`` binds every element and attribute
descriptor to the new class, and ``SchemaBase.__init_subclass__``
collects the descriptor bookkeeping (``_elementNames_`` and
``_attributeNames_``) automatically. The classes are stored in a
dictionary in the PyXSD instance.
"""

import logging

from pyxsd import xsd_data_types
from pyxsd.namespaces import XSD_NS, NamespaceError, clark, local_name, namespace_of
from pyxsd.xsd_data_types import XsdDataType

logger = logging.getLogger(__name__)

# XSD component kinds whose declarations live in separate symbol
# spaces: an element and a type may legally share a name, and a type
# lookup must not resolve to the element.  ER classes not listed here
# are bookkeeping nodes (compositors, facets, wildcards) that share a
# single anonymous namespace.
_COMPONENT_KINDS = {
    "Schema": "schema",
    "Element": "element",
    "Attribute": "attribute",
    "Group": "group",
    "AttributeGroup": "attributeGroup",
    "ComplexType": "type",
    "SimpleType": "type",
}


def componentKind(obj):
    """Returns the XSD component kind for a representative, or ``None``."""
    return _COMPONENT_KINDS.get(type(obj).__name__)


class _AnyNamespace:
    """Sentinel: a lookup that does not filter by namespace."""

    __slots__ = ()


#: Passed as ``namespace`` to :meth:`ComponentTable.getFromName` when the
#: caller wants the historical namespace-agnostic lookup.
ANY_NAMESPACE = _AnyNamespace()


class ComponentTable(dict):
    """A parser-owned table of element representatives by name.

    Values are lists because one name may be declared once per
    component kind (for example a global element and a complex type
    both named ``T``). It behaves as a plain ``{name: [ER, ...]}``
    mapping for compatibility while ``getFromName`` filters by kind.
    """

    def getFromName(self, name, kind=None, namespace=ANY_NAMESPACE):
        """Returns the unique representative named ``name``.

        When ``kind`` is given, only representatives of that component
        kind are considered, so a type lookup ignores a same-named
        element. When ``namespace`` is given, only representatives
        whose expanded name is in that namespace are considered; pass
        ``None`` to select no-namespace declarations. Ambiguous or
        missing lookups warn and return ``None``.
        """
        entries = self.get(name)
        if not entries:
            logger.warning("getFromName Error: %s is not a key in the registry", name)
            return None
        if kind is not None:
            entries = [entry for entry in entries if componentKind(entry) == kind]
        if namespace is not ANY_NAMESPACE:
            entries = [
                entry
                for entry in entries
                if getattr(entry, "getNamespace", lambda: None)() == namespace
            ]
        if len(entries) == 1:
            return entries[0]
        if not entries:
            logger.warning("getFromName Error: %s has no %r declaration", name, kind)
            return None
        logger.warning("ElementRepresentative Error: %r", entries)
        return None


_ACTIVE_TABLE = ComponentTable()


class _RegistryProxy:
    """Stable module-level alias that forwards to the active table.

    ``from ... import registry`` binds this object permanently, so it
    cannot be a plain dict that gets replaced per parse. It delegates
    every mapping operation to the table the most recent parser
    installed, preserving the historical module-level view.
    """

    def _active(self) -> ComponentTable:
        return _ACTIVE_TABLE

    def __getitem__(self, key):
        return self._active()[key]

    def __setitem__(self, key, value):
        self._active()[key] = value

    def __contains__(self, key):
        return key in self._active()

    def __iter__(self):
        return iter(self._active())

    def __len__(self):
        return len(self._active())

    def get(self, key, default=None):
        return self._active().get(key, default)

    def setdefault(self, key, default=None):
        return self._active().setdefault(key, default)

    def keys(self):
        return self._active().keys()

    def values(self):
        return self._active().values()

    def items(self):
        return self._active().items()

    def clear(self):
        self._active().clear()

    def getFromName(self, name, kind=None):
        return self._active().getFromName(name, kind)

    def getFromNameNS(self, name, kind=None, namespace=ANY_NAMESPACE):
        return self._active().getFromName(name, kind, namespace)


def _schemaOf(obj):
    """Returns the schema ER owning ``obj``, or ``None`` when detached."""
    try:
        return obj.getSchema()
    except (AttributeError, RuntimeError):
        return None


def _tableFor(obj):
    """Returns the component table ``obj`` should register into."""
    table = getattr(_schemaOf(obj), "components", None)
    if isinstance(table, ComponentTable):
        return table
    return _ACTIVE_TABLE


class ElementRepresentative:
    """Base class for all of the tag type classes in the
    ElementRepresentative system.

    It contains the methods that control movement around the tree and
    the most general ways to gather information from the schema.
    """

    def __init__(self, xsdElement, parent):
        """See the documentation for the ElementRepresentative system at
        the top of this module.
        """
        self.xsdElement = xsdElement
        self.parent = parent
        self.tagParts = self.xsdElement.tag.split("}")
        self.tagType = self.tagParts[1]
        self.name = self.getName()
        self.register(self.name, self)
        self.superClassNames = []
        self.subClassNames = []
        self.references = []
        self.referToMe = []
        self.tagAttributes = {}
        self.processedChildren = []
        self.layerNum = self.findLayerNum()
        self.clsName = self.name
        self.clsName = self.clsName[0].upper() + self.clsName[1:]

        for name, value in xsdElement.items():
            if name == "name":
                continue
            setattr(self, name, value)
            self.tagAttributes[name] = value

        self.processChildren()

    def __str__(self):
        """Prints the ER information for a tag in the form:
        ``ClassName[TagName]``.
        """
        return "{}[{}]".format(
            self.__class__.__name__,
            self.__dict__.get("name", "???"),
        )

    def processChildren(self):
        """Calls the ``factory`` on all of the children of an element."""
        children = list(self.xsdElement)
        if not children:
            return None
        for child in children:
            processedChild = ElementRepresentative.factory(child, self)
            self.processedChildren.append(processedChild)
        return None

    @classmethod
    def factory(cls, xsdElement, parent):
        """Initializes the tag-specific class for a particular ElementTree
        schema element.  See the ER system documentation.

        usage: ``ElementRepresentative.factory(xsdElement, parent)``
        """
        clsName = cls.classNameFor(xsdElement, parent)
        if clsName in TAG_CLASSES:
            tagCls = TAG_CLASSES[clsName]
            return tagCls(xsdElement, parent)
        # Complain
        logger.warning("ElementRepresentative %s is not defined!", clsName)
        return None

    def describe(self):
        """Returns a multi-line description of this ER's attributes for
        debugging.
        """
        return "\n".join(f" {attrName} -> {value} " for attrName, value in vars(self).items())

    def findLayerNum(self):
        """Returns an integer that specifies how deep in the tree a
        particular element is.  The ``schema`` element, which is the
        root element, is 1.
        """
        if self.parent is None:
            return 1
        return self.parent.findLayerNum() + 1

    def checkTopLevelType(self):
        """Checks to see if an element is at the top level. Returns True
        if it is, False if it is not. The top level elements are all
        the children of the root ``schema`` tag.
        """
        return bool(isinstance(self.parent, Schema))

    @classmethod
    def typeFromName(cls, xsdTypeName, pyXSD):
        """Returns a schema type given the type's name.

        Returns data type classes from ``xsd_data_types`` for built-in
        types.  Calls ``clsFor`` on ERs for user-defined types.  The
        schema-namespace prefixes ``xs:`` and ``xsd:`` always denote
        built-ins; any other qualified name is looked up in the
        registry first (by full name, then by local name so prefixed
        references like ``my:customType`` resolve), with a built-in
        fallback on the local name so default-namespace schemas
        (``type="string"``) still resolve.
        """
        table = getattr(pyXSD, "components", None)
        if not isinstance(table, ComponentTable):
            table = registry
        mode = getattr(pyXSD, "mode", None)
        if getattr(mode, "namespaces", "legacy") == "strict":
            return cls._typeFromExpandedName(xsdTypeName, table, pyXSD)
        if not xsdTypeName.startswith(("xs:", "xsd:")):
            getFromNameReturned = table.getFromName(xsdTypeName, kind="type")
            if not getFromNameReturned:
                local = xsdTypeName.split(":", 1)[-1]
                getFromNameReturned = table.getFromName(local, kind="type")
            if getFromNameReturned:
                return getFromNameReturned.clsFor(pyXSD)
            local = xsdTypeName.split(":", 1)[-1]
            primitive = _PRIMITIVE_TYPES.get(local)
            if primitive is not None:
                return primitive
            logger.warning(
                "typeFromName() error: getFromName() is returning None for %s", xsdTypeName
            )
            return None
        local = xsdTypeName.split(":", 1)[1]
        primitive = _PRIMITIVE_TYPES.get(local)
        if primitive is not None:
            return primitive
        logger.warning("XsdTypeName Error: %s does not correspond to a class", local)
        return None

    @classmethod
    def _typeFromExpandedName(cls, name, table, pyXSD):
        """Strict-mode type lookup by expanded (Clark) name.

        Built-ins are recognised by the XML Schema namespace URI, so
        any prefix bound to it works. User types must match both the
        local name and the namespace of the reference; there is no
        cross-namespace local-name fallback.
        """
        uri = namespace_of(name)
        local = local_name(name)
        if uri == XSD_NS:
            primitive = _PRIMITIVE_TYPES.get(local)
            if primitive is not None:
                return primitive
            logger.warning("XsdTypeName Error: %s does not correspond to a class", local)
            return None
        found = table.getFromName(local, kind="type", namespace=uri)
        if found:
            return found.clsFor(pyXSD)
        if uri is None:
            primitive = _PRIMITIVE_TYPES.get(local)
            if primitive is not None:
                return primitive
        logger.warning("typeFromName() error: getFromName() is returning None for %s", name)
        return None

    def addSuperClassName(self, name):
        """Adds a base class name to the containing type for a particular
        element.  Calls come from Restriction and Extension.
        """
        if name is None:
            return None
        # Prevent Duplicates
        for superClassName in self.getContainingType().superClassNames:
            if superClassName == name:
                return None
        self.getContainingType().superClassNames.append(name)
        return None

    def getContainingType(self):
        """Returns the parent's ``getContainingType()``.

        If this function is being called from schema, an error is
        returned.  When an ER *is* a containing type, its own
        ``getContainingType()`` should return that ER.
        """
        if self.parent is not None:
            return self.parent.getContainingType()
        logger.error(
            "ElementRepresentative Error: the program encountered an unknown "
            "error in getContainingType()"
        )
        logger.error("The class dictionary is as follows:\n%s", self.describe())
        return None

    def getSchema(self):
        """Returns the parent's ``getSchema()``.

        getSchema() returns the containing instance when it is a schema
        tag (see the ``getSchema()`` in the ``schema`` class).
        """
        return self.parent.getSchema()

    def getNamespace(self):
        """Returns the namespace URI this declaration belongs to.

        Global declarations belong to the schema's ``targetNamespace``;
        a schema with no ``targetNamespace`` yields ``None``. Local
        declarations belong to the target namespace or not depending on
        the relevant form default (handled where declarations are
        matched).
        """
        schema = self.getSchema()
        if schema is None:
            return None
        getter = getattr(schema, "getNamespace", None)
        if getter is None:
            return getattr(schema, "targetNamespace", None)
        return getter()

    def isGlobalDeclaration(self):
        """Returns True when this declaration is a direct schema child."""
        return isinstance(self.parent, Schema)

    @property
    def expandedName(self):
        """Returns this declaration's Clark name, or its plain name."""
        if self.name is None:
            return None
        return clark(self.getNamespace(), self.name)

    def instanceName(self, *, is_attribute=False, parser=None):
        """Returns the name an instance node needs to match this declaration.

        In ``legacy`` namespace mode this is always the plain local name
        (the historical behavior). In ``strict`` mode a global
        declaration is always namespace-qualified, while a local
        declaration is qualified only when the schema's relevant form
        default (``elementFormDefault``/``attributeFormDefault``) is
        ``qualified``. A qualified result is a Clark name.
        """
        local = self.name
        if local is None:
            return None
        if parser is None:
            parser = getattr(self, "pyXSD", None) or getattr(self.getSchema(), "pyXSD", None)
        mode = getattr(parser, "mode", None)
        if getattr(mode, "namespaces", "legacy") != "strict":
            return local
        uri = self.getNamespace()
        if uri is None:
            return local
        qualified = self.isGlobalDeclaration()
        if not qualified:
            schema = self.getSchema()
            if schema is not None:
                default = (
                    schema.getAttributeFormDefault()
                    if is_attribute
                    else schema.getElementFormDefault()
                )
                qualified = default == "qualified"
        if qualified:
            return clark(uri, local)
        return local

    def resolveSchemaQName(self, value, *, is_attribute=True, parser=None):
        """Resolves a lexical QName written in this schema element.

        In ``strict`` namespace mode the prefix is resolved through the
        schema document's captured in-scope bindings; in ``legacy``
        mode the value is returned unchanged. An unbound prefix is
        reported as ``unknown-namespace-prefix`` and the raw value is
        returned so the caller's legacy fallback can still run.

        ``parser`` overrides the attached parser, which is needed while
        a class is being built before its descriptors own ``pyXSD``.
        """
        if parser is None:
            parser = getattr(self, "pyXSD", None)
            if parser is None:
                # During class building the parser is attached to the
                # schema rather than to every declaration; fall back to
                # it so schema-time references resolve in strict mode.
                parser = getattr(self.getSchema(), "pyXSD", None)
        mode = getattr(parser, "mode", None)
        if getattr(mode, "namespaces", "legacy") != "strict":
            return value
        context = getattr(self.getSchema(), "namespaceContext", None)
        if context is None:
            return value
        try:
            return context.resolve(self.xsdElement, value, is_attribute=is_attribute)
        except NamespaceError as e:
            if parser is not None:
                parser.report.add_error(
                    str(e),
                    code="unknown-namespace-prefix",
                    element=self.name,
                )
            return value

    def resolveReference(self, value, candidates, *, is_attribute=True, parser=None):
        """Returns the component a lexical QName reference names.

        ``candidates`` is an iterable of element representatives (for
        example ``schema.elements`` or ``schema.groups.values()``). In
        ``legacy`` mode, or when the prefix cannot be resolved, the
        reference's local name selects the first same-named candidate.
        In ``strict`` mode the resolved namespace must also match the
        candidate's namespace, so a reference into another namespace
        never falls back to a same-named local declaration. Returns
        ``None`` when nothing matches.
        """
        if value is None:
            return None
        resolved = self.resolveSchemaQName(value, is_attribute=is_attribute, parser=parser)
        local = local_name(resolved)
        uri = namespace_of(resolved)
        for candidate in candidates:
            if getattr(candidate, "name", None) != local:
                continue
            if uri is not None:
                getter = getattr(candidate, "getNamespace", None)
                if getter is None or getter() != uri:
                    continue
            return candidate
        return None

    def resolvedTypeName(self):
        """Returns the ``type`` attribute resolved to a Clark name.

        Returns ``None`` when the declaration carries no ``type``.
        """
        raw = self.__dict__.get("type")
        if raw is None:
            return None
        return self.resolveSchemaQName(raw, is_attribute=True)

    def getContainingTypeName(self):
        """Returns the name of the containing type."""
        theType = self.getContainingType()
        if theType is None:
            return None
        return theType.name

    def getName(self):
        """Returns the name field in the ElementTree element."""
        return self.xsdElement.get("name")

    @classmethod
    def register(cls, name, obj):
        """Stores ER objects in this parser's component table.

        Each parser owns a :class:`ComponentTable` (created by the
        schema ER), so a later parser cannot see or overwrite earlier
        declarations. Declarations are keyed by name and kind: the
        first declaration of a given (kind, name) wins, while a
        different kind may register the same name.
        """
        table = _tableFor(obj)
        entries = table.setdefault(name, [])
        kind = componentKind(obj)
        if any(componentKind(entry) == kind for entry in entries):
            logger.debug(
                "an element representative named %r (kind %r) is already "
                "registered; keeping the first one",
                name,
                kind,
            )
            return
        entries.append(obj)

    @classmethod
    def getFromName(cls, name, kind=None):
        """Retrieve an entry in this parser's component table.

        ``kind`` restricts the lookup to one XSD component kind (for
        example ``"type"``), which is how a type lookup ignores a
        same-named element declaration.
        """
        return registry.getFromName(name, kind)

    @staticmethod
    def tryConvert(variable):
        """Tries to convert a variable from a string in the xsd to a
        Python value.  Returns the entry unchanged if it cannot be
        converted.
        """
        try:
            return int(variable)
        except (TypeError, ValueError):
            pass
        try:
            return float(variable)
        except (TypeError, ValueError):
            pass
        if variable == "false":
            return False
        if variable == "true":
            return True
        return variable

    @classmethod
    def classNameFor(cls, xsdElement, parent):
        """Returns the name of the class that the factory should find.

        usage: ``ElementRepresentative.classNameFor(xsdElement, parent)``
        """
        clsName = xsdElement.tag
        tagParts = xsdElement.tag.split("}")
        if len(tagParts) == 2:
            clsName = tagParts[1]
            clsName = clsName[0].upper() + clsName[1:]
        return clsName


# Built-in XSD type names (after any prefix) to classes.  Built from
# the classes' own declared ``name`` attributes so the table can never
# drift from the lattice in ``xsd_data_types`` (intermediate helper
# classes that merely inherit a name are skipped).
_PRIMITIVE_TYPES = {
    klass.name: klass
    for klass in vars(xsd_data_types).values()
    if isinstance(klass, type)
    and issubclass(klass, XsdDataType)
    and klass is not XsdDataType
    and "name" in klass.__dict__
    and klass is not xsd_data_types.TypeList
}
# The active parser's component table, keyed by name. Each ``PyXSD``
# parse installs its own :class:`ComponentTable` here so registrations
# during class building and detached lookups (``getFromName``) see the
# right parser's declarations. Multiple ERs may share a name and are
# disambiguated by component kind (see ``ComponentTable.getFromName``).
registry = _RegistryProxy()

# Import all of the tag-specific classes after the ER class definition
# (the tag modules import this module's ElementRepresentative).  This
# replaces the old exec-based import loop.
from pyxsd.element_representatives.all import All  # noqa: E402
from pyxsd.element_representatives.annotation import Annotation  # noqa: E402
from pyxsd.element_representatives.any import Any  # noqa: E402
from pyxsd.element_representatives.any_attribute import AnyAttribute  # noqa: E402
from pyxsd.element_representatives.attribute import Attribute  # noqa: E402
from pyxsd.element_representatives.attribute_group import AttributeGroup  # noqa: E402
from pyxsd.element_representatives.choice import Choice  # noqa: E402
from pyxsd.element_representatives.complex_content import ComplexContent  # noqa: E402
from pyxsd.element_representatives.complex_type import ComplexType  # noqa: E402
from pyxsd.element_representatives.documentation import Documentation  # noqa: E402
from pyxsd.element_representatives.element import Element  # noqa: E402
from pyxsd.element_representatives.enumeration import Enumeration  # noqa: E402
from pyxsd.element_representatives.extension import Extension  # noqa: E402
from pyxsd.element_representatives.group import Group  # noqa: E402
from pyxsd.element_representatives.identity import (  # noqa: E402
    Field,
    Key,
    Keyref,
    Selector,
    Unique,
)
from pyxsd.element_representatives.length import Length  # noqa: E402
from pyxsd.element_representatives.list import List  # noqa: E402
from pyxsd.element_representatives.max_exclusive import MaxExclusive  # noqa: E402
from pyxsd.element_representatives.max_inclusive import MaxInclusive  # noqa: E402
from pyxsd.element_representatives.min_exclusive import MinExclusive  # noqa: E402
from pyxsd.element_representatives.min_inclusive import MinInclusive  # noqa: E402
from pyxsd.element_representatives.pattern import Pattern  # noqa: E402
from pyxsd.element_representatives.restriction import Restriction  # noqa: E402
from pyxsd.element_representatives.schema import Schema  # noqa: E402
from pyxsd.element_representatives.sequence import Sequence  # noqa: E402
from pyxsd.element_representatives.simple_content import SimpleContent  # noqa: E402
from pyxsd.element_representatives.simple_type import SimpleType  # noqa: E402
from pyxsd.element_representatives.union import Union  # noqa: E402
from pyxsd.element_representatives.xsd_type import XsdType  # noqa: E402

TAG_CLASSES = {
    "Element": Element,
    "Attribute": Attribute,
    "Schema": Schema,
    "XsdType": XsdType,
    "Extension": Extension,
    "SimpleType": SimpleType,
    "ComplexType": ComplexType,
    "Annotation": Annotation,
    "AttributeGroup": AttributeGroup,
    "Documentation": Documentation,
    "Restriction": Restriction,
    "Sequence": Sequence,
    "Choice": Choice,
    "All": All,
    "Union": Union,
    "Group": Group,
    "Any": Any,
    "AnyAttribute": AnyAttribute,
    "Key": Key,
    "Keyref": Keyref,
    "Unique": Unique,
    "Selector": Selector,
    "Field": Field,
    "List": List,
    "SimpleContent": SimpleContent,
    "ComplexContent": ComplexContent,
    "Enumeration": Enumeration,
    "Pattern": Pattern,
    "Length": Length,
    "MinInclusive": MinInclusive,
    "MaxInclusive": MaxInclusive,
    "MinExclusive": MinExclusive,
    "MaxExclusive": MaxExclusive,
}
