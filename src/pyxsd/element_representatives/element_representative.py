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
import re

from pyxsd import xsd_data_types
from pyxsd.namespaces import XSD_NS, NamespaceError, clark, local_name, namespace_of
from pyxsd.schema_context import context_or_ambient, last_components
from pyxsd.wildcards import (
    not_qname_consistency_problems,
    replace_wildcard,
    wildcard_declaration_problems,
    wildcard_spec,
)
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

    def getFromName(self, name, kind=None, namespace=ANY_NAMESPACE, warn=True):
        """Returns the unique representative named ``name``.

        When ``kind`` is given, only representatives of that component
        kind are considered, so a type lookup ignores a same-named
        element. When ``namespace`` is given, only representatives
        whose expanded name is in that namespace are considered; pass
        ``None`` to select no-namespace declarations. Ambiguous or
        missing lookups warn (unless ``warn`` is false) and return
        ``None``.
        """
        entries = self.get(name)
        if not entries:
            if warn:
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
            if warn:
                logger.warning("getFromName Error: %s has no %r declaration", name, kind)
            return None
        logger.warning("ElementRepresentative Error: %r", entries)
        return None


_FALLBACK_TABLE = ComponentTable()


def _resolve_active_table() -> ComponentTable:
    """The component table module-level lookups should use.

    Precedence: the active parser context, then the most recently
    completed parser on this thread (the historical "last parse wins"
    view), then an empty fallback table.
    """
    context = context_or_ambient()
    if isinstance(context.components, ComponentTable):
        return context.components
    remembered = last_components()
    if isinstance(remembered, ComponentTable):
        return remembered
    return _FALLBACK_TABLE


class _RegistryProxy:
    """Stable module-level alias that forwards to the active table.

    ``from ... import registry`` binds this object permanently, so it
    cannot be a plain dict that gets replaced per parse. It delegates
    every mapping operation to the component table of the context active
    on the calling thread (falling back to an empty table outside any
    parser run), preserving the historical module-level view without
    leaking state between parsers.
    """

    def _active(self) -> ComponentTable:
        return _resolve_active_table()

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

    def getFromName(self, name, kind=None, namespace=ANY_NAMESPACE, warn=True):
        return self._active().getFromName(name, kind, namespace, warn)

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
    return _resolve_active_table()


class ElementRepresentative:
    """Base class for all of the tag type classes in the
    ElementRepresentative system.

    It contains the methods that control movement around the tree and
    the most general ways to gather information from the schema.
    """

    # Set on element descriptors that were re-keyed in their generated
    # class because an attribute took the natural accessor name
    # (element/attribute name collision). See ``XsdType.clsFor``.
    _aliased_: bool = False

    #: Child tag grammar for this element. ``None`` means the class does
    #: not constrain its children (each child is factored into its own
    #: representative); a tuple restricts the legal local names.
    _ALLOWED_CHILDREN: tuple[str, ...] | None = None
    #: Local names of children that may appear at most once.
    _MAX_ONE_CHILDREN: tuple[str, ...] = ()
    #: Ordered grammar slots. Each slot is a tuple of tags sharing that
    #: position; the children (by tag) must occupy slots in
    #: non-decreasing order. ``annotation`` is position-independent and
    #: handled separately.
    _CHILD_ORDER: tuple[tuple[str, ...], ...] = ()
    #: Slots that, when occupied, forbid any later slot (for example
    #: ``simpleContent``/``complexContent`` excludes particles and
    #: attributes).
    _EXCLUSIVE_SLOTS: frozenset[int] = frozenset()
    #: Slots that hold alternatives: occupying such a slot with two
    #: *distinct* tags is illegal even though each tag appears only once
    #: (for example ``simpleContent``+``complexContent``, or
    #: ``choice``+``group``). Slots that may legitimately hold many
    #: children ``(attribute, attributeGroup)`` are deliberately omitted.
    _ONE_OF_SLOTS: frozenset[int] = frozenset()
    #: When true (the XSD rule for every declaration), a schema-namespace
    #: ``annotation`` child must be the first schema child. The schema
    #: root sets this false: its content model allows annotations in any
    #: position, and repeatedly.
    _ANNOTATION_FIRST: bool = True

    def __init__(self, xsdElement, parent):
        """See the documentation for the ElementRepresentative system at
        the top of this module.
        """
        self.xsdElement = xsdElement
        self.parent = parent
        self.tagParts = self.xsdElement.tag.split("}")
        # Annotation content and other foreign XML may carry no
        # namespace at all; fall back to the raw tag instead of
        # assuming a Clark name with a local part.
        self.tagType = self.tagParts[1] if len(self.tagParts) == 2 else self.xsdElement.tag
        self.name = self.getName()
        self.register(self.name, self)
        self.superClassNames = []
        self.subClassNames = []
        self.references = []
        self.referToMe = []
        self.tagAttributes = {}
        self.processedChildren = []
        self.layerNum = self.findLayerNum()
        # A declaration may be missing its name (or carry an empty one);
        # that is a schema error the parser reports later, but class
        # building must still have a usable identifier to work with.
        if self.name:
            self.clsName = self.name[0].upper() + self.name[1:]
        else:
            self.clsName = self.__class__.__name__

        for name, value in xsdElement.items():
            if name == "name":
                continue
            setattr(self, name, value)
            self.tagAttributes[name] = value

        self.childTags = []
        self.unexpectedChildTags = []
        self.rawTag = self.tagType
        self.processChildren()

    def __str__(self):
        """Prints the ER information for a tag in the form:
        ``ClassName[TagName]``.
        """
        return "{}[{}]".format(
            self.__class__.__name__,
            self.__dict__.get("name", "???"),
        )

    def _acceptChild(self, child):
        """Records a child's tag and reports whether its grammar allows it.

        Only schema-namespace children are recorded on ``childTags``; the
        duplicate/order/annotation checks operate on schema components, so
        a foreign child whose local name happens to be ``annotation``
        (arbitrary XML in an ``appinfo``/``documentation`` body, say)
        never drives grammar-order reporting. When ``_ALLOWED_CHILDREN``
        is set, a child whose local name is not in the table, or which is
        not in the XML Schema namespace, is not a schema component of this
        element; its tag is recorded on ``unexpectedChildTags`` for the
        parser to report later and the caller must not factor it.

        Subclasses that need bespoke child handling (``Element`` and
        ``Attribute`` set ``self.type`` from an inline type child) share
        this helper so the table is never bypassed.
        """
        inSchema = namespace_of(child.tag) == XSD_NS
        tag = local_name(child.tag)
        if inSchema:
            self.childTags.append(tag)
        if self._ALLOWED_CHILDREN is not None and (
            not inSchema or tag not in self._ALLOWED_CHILDREN
        ):
            self.unexpectedChildTags.append(tag)
            return False
        return True

    def processChildren(self):
        """Calls the ``factory`` on all of the children of an element.

        See ``_acceptChild`` for the grammar filtering; a child the
        grammar rejects is recorded on ``unexpectedChildTags`` and is not
        factored (``processedChildren`` holds ``None`` for its slot).
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

    # Lexical space of ``nonNegativeInteger``: an optional plus sign
    # followed by decimal digits. Leading zeros are legal (the canonical
    # form drops them, but the lexical space does not).
    _OCCURS_PATTERN = re.compile(r"^\+?[0-9]+$")

    def _reportSchemaError(self, message, *, code):
        """Records a schema problem on the parser's report.

        Falls back to logging when no parser is attached (for example
        when ERs are built in isolation).
        """
        parser = getattr(self.getSchema(), "pyXSD", None)
        if parser is not None:
            parser.report.add_error(message, code=code, element=self.name)
        else:
            logger.error("%s[%s] %s", self.name, code, message)

    def _checkWildcardDeclaration(self, *, is_attribute: bool) -> None:
        """Reports wildcard XML-attribute grammar problems.

        Shared by ``Any`` and ``AnyAttribute``: the namespace-constraint
        token grammar, the ``processContents`` value, occurrence
        attributes on ``xs:anyAttribute``, the XSD 1.1
        ``namespace``/``notNamespace`` co-presence, the 1.1
        ``notNamespace``/``notQName`` token grammar (prefixes resolved
        through the schema's recorded bindings) and the unqualified XML
        attributes outside the wildcard's allowed set. The raw
        ``xsdElement`` attributes are inspected (not ``tagAttributes``)
        so the reserved ``name`` attribute is seen too.

        The registered :class:`WildcardSpec` is refined with the expanded
        ``notQName`` names (see :meth:`refineWildcardSpec`; the parser
        runs a pre-pass so derivation checks see the refined specs even
        before the declaration walk reaches this ER), and the 1.1
        Wildcard Properties Correct consistency rule is reported: every
        ``notQName`` name must lie in a namespace the wildcard admits.
        Both fall back to the raw tokens — logging, never raising — when
        no namespace context is available.
        """
        resolver = self._wildcardQNameResolver()
        for code, message in wildcard_declaration_problems(
            self.xsdElement.attrib, is_attribute=is_attribute, resolve_qname=resolver
        ):
            self._reportSchemaError(message, code=code)
        self.refineWildcardSpec()
        spec = getattr(self, "wildcardSpec", None)
        if spec is not None:
            for code, message in not_qname_consistency_problems(spec):
                self._reportSchemaError(message, code=code)

    def refineWildcardSpec(self) -> None:
        """Expands the wildcard spec's 1.1 ``notQName`` names, if any.

        The ER constructors register a raw spec before the schema's
        prefix bindings are attached, but the attribute-wildcard
        derivation check and the binding consult the *registered* list.
        The parser calls this on every ER right after attaching the
        namespace context (before any content-model check), so the list
        holds the expanded spec; calling it again is a no-op. A
        non-wildcard ER, a missing spec or a missing namespace context
        leaves the spec untouched.
        """
        old = getattr(self, "wildcardSpec", None)
        if old is None:
            return
        spec = wildcard_spec(
            self.tagAttributes,
            is_attribute=old.is_attribute,
            target_namespace=self.getNamespace(),
            resolve_qname=self._wildcardQNameResolver(),
        )
        if spec == old:
            return
        self.wildcardSpec = spec
        containing = self.getContainingType()
        if containing is not None:
            replace_wildcard(containing, old, spec)

    def _wildcardQNameResolver(self):
        """A QName expander for ``notQName`` values, or ``None``.

        Prefix bindings are recorded per element by the parsing layer,
        so resolution has to run after the schema context is attached;
        without it (an ER built in isolation) ``None`` tells the caller
        to keep the raw tokens and stay silent.
        """
        try:
            schema = self.getSchema()
        except AttributeError:
            return None
        context = getattr(schema, "namespaceContext", None)
        element = getattr(self, "xsdElement", None)
        if context is None or element is None:
            return None

        def resolve(token):
            return context.resolve(element, token)

        return resolve

    def checkDeclarationLegality(self):
        """Reports semantic declaration-legality problems.

        The child-grammar tables cover which children a declaration may
        contain; this hook covers the *attribute* constraints of the
        XSD component's XML representation (for example
        ``default``/``fixed`` consistency, ``use`` legality or the
        lexical space of a name). Subclasses override it; the default
        does nothing. The parser calls it once per representative after
        the ER tree is built, when ``getSchema().pyXSD`` is attached and
        ``_reportSchemaError`` can reach the report.
        """
        return None

    @staticmethod
    def _invalidBoolean(value):
        """Whether an ``xs:boolean`` lexical value is illegal.

        The lexical space is exactly ``true``/``false``/``1``/``0``
        (with surrounding whitespace collapsed). Case does not vary:
        ``TRUE`` and ``False`` are schema errors, not truthy spellings.
        ``None`` (an absent attribute) is not invalid; callers only
        invoke this for attributes that are present.
        """
        return value is None or str(value).strip() not in ("true", "false", "1", "0")

    @staticmethod
    def _invalidNCName(value):
        """Whether *value* is not an XML ``NCName``.

        A ``name`` attribute is an ``xs:NCName``: no colon, no leading
        digit or hyphen, no whitespace. Shared by the ``name``/``id``
        lexical checks.
        """
        try:
            xsd_data_types.NCName(value)
        except TypeError:
            return True
        return False

    @staticmethod
    def _invalidTokenList(value, allowed):
        """Whether an XSD token-list attribute is lexically illegal.

        ``#all`` is only legal on its own; otherwise every
        whitespace-separated token must be in ``allowed``. An empty (or
        absent) value is legal and means the default. Shared by the
        ``final``/``block`` checks on elements and types.
        """
        tokens = value.split()
        if not tokens:
            return False
        if "#all" in tokens:
            return tokens != ["#all"]
        return any(token not in allowed for token in tokens)

    def _occursValue(self, attrName):
        """Returns the integer value of ``minOccurs``/``maxOccurs``.

        Invalid lexical values are reported (the schema is not legal)
        and replaced with the spec default of 1 so class building can
        continue; ``maxOccurs="unbounded"`` maps to 99999.
        """
        raw = getattr(self, attrName, 1)
        text = str(raw).strip()
        if attrName == "maxOccurs" and text == "unbounded":
            return 99999
        if self._OCCURS_PATTERN.fullmatch(text):
            return int(text)
        self._reportSchemaError(
            f"{self.__class__.__name__.lower()} '{self.name}' has an invalid "
            f"{attrName} value '{raw}'; expected a nonNegativeInteger"
            + (" or 'unbounded'" if attrName == "maxOccurs" else ""),
            code="invalid-occurs",
        )
        return 1

    def getMinOccurs(self):
        """Returns an integer value for ``minOccurs``.

        If no ``minOccurs`` has been set, uses the default of 1.
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

    def _checkParticleOccurs(self) -> None:
        """Reports an occurrence range whose minimum exceeds its maximum.

        Shared by the particle ERs (``sequence``/``choice``; ``group``
        reference sites use the reference's name in the message).
        Reading the values also reports lexical failures through
        ``_occursValue`` (``invalid-occurs``), so a garbage attribute is
        never silently ignored. ``all`` bounds are compositor-legality
        and carry their own code (``all-rule``), so ``all`` does not
        call this helper.
        """
        minimum = self.getMinOccurs()
        maximum = self.getMaxOccurs()
        if minimum > maximum:
            self._reportSchemaError(
                f"{self.rawTag} '{self.name}' has minOccurs={minimum} greater "
                f"than maxOccurs={maximum}",
                code="declaration-attribute",
            )

    def _silentOccurs(self, attrName):
        """Reads ``minOccurs``/``maxOccurs`` without reporting.

        Like ``_occursValue`` but garbage lexical values read as the
        default 1 instead of raising ``invalid-occurs`` — the
        declaration walk reports lexical failures on the owning
        declaration exactly once, so lazy readers (emptiability) must
        stay silent to avoid duplicate issues.
        """
        raw = getattr(self, attrName, 1)
        text = str(raw).strip()
        if attrName == "maxOccurs" and text == "unbounded":
            return 99999
        if self._OCCURS_PATTERN.fullmatch(text):
            return int(text)
        return 1

    @property
    def emptiable(self):
        """Whether this particle can match zero elements.

        Elements and wildcards are emptiable exactly when their
        ``minOccurs`` is 0; compositors and groups override
        ``_emptiableParticle``. A group reference resolves its
        emptiability lazily through the group it names, so group
        cycles must be guarded by the caller-supplied visited set.
        """
        return self._emptiableParticle(set())

    def _emptiableParticle(self, visited: set) -> bool:
        return self._silentOccurs("minOccurs") == 0

    def _particleChildren(self) -> list:
        """The particle children of a compositor (annotations skipped)."""
        kinds = ("Element", "Group", "Choice", "Sequence", "All", "Any")
        return [
            child
            for child in self.processedChildren or ()
            if child is not None and type(child).__name__ in kinds
        ]

    def _compositorEmptiable(self, visited: set) -> bool:
        """A compositor can match zero when its own occurrence allows it,
        or when it has no particles or every particle child can (the
        particlesHa emptiability rule).

        For ``sequence``/``all`` an empty particle set always matches
        zero (each iteration matches zero elements); ``choice``
        overrides because an empty choice with ``minOccurs >= 1``
        cannot match at all.
        """
        if self._silentOccurs("minOccurs") == 0:
            return True
        children = self._particleChildren()
        return not children or all(child._emptiableParticle(visited) for child in children)

    def resolveGroupRef(self, refSite):
        """Returns the group definition a group reference site names.

        Prefers QName-aware resolution (``resolveReference``) and falls
        back to the ``schema.groups`` table by full reference and local
        name, mirroring the content-model compiler. Returns ``None``
        when the reference cannot be resolved.
        """
        ref = getattr(refSite, "ref", None)
        if not ref:
            return None
        schema = self.getSchema()
        if schema is None:
            return None
        groups = getattr(schema, "groups", None)
        if not groups:
            return None
        resolver = getattr(refSite, "resolveReference", None)
        if resolver is not None:
            resolved = resolver(ref, groups.values(), parser=getattr(schema, "pyXSD", None))
            if resolved is not None:
                return resolved
        return groups.get(ref) or groups.get(ref.split(":")[-1])

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
        try:
            schema = self.getSchema()
        except AttributeError:
            # Detached representative (no parent); no namespace.
            return None
        if schema is None:
            return None
        overrides = getattr(schema, "namespaceOverrides", None)
        element = getattr(self, "xsdElement", None)
        if overrides and element is not None and id(element) in overrides:
            return overrides[id(element)]
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
        # A reference site (``ref="..."``) takes the expanded name of the
        # declaration it points at. The ref site lives in the referring
        # schema, so its own namespace is not the attribute's namespace:
        # ``r:id`` in a WordprocessingML type resolves to a global
        # attribute in the relationships namespace.
        referred = getattr(self, "referredAttribute", None)
        if referred is None:
            referred = getattr(self, "referredElement", None)
        if referred is not None and referred is not self:
            return referred.instanceName(is_attribute=is_attribute, parser=parser)
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
            qualified = self._localDeclarationIsQualified(schema, is_attribute)
        if qualified:
            return clark(uri, local)
        return local

    def _localDeclarationIsQualified(self, schema, is_attribute: bool) -> bool:
        """Decides whether a local declaration's name is qualified.

        An explicit ``form`` attribute on the declaration wins. Without
        one, a component spliced in from another document uses that
        document's form defaults (recorded at composition time); only a
        component native to the main schema falls back to the host
        schema's defaults.
        """
        element = getattr(self, "xsdElement", None)
        explicit = element.get("form") if element is not None else None
        if explicit is not None:
            return explicit == "qualified"
        # XSD 1.1: a local declaration carrying an explicit targetNamespace
        # is qualified into that namespace regardless of the form default
        # (TargetNS target001, IBM targetNamespace_005).
        if element is not None and element.get("targetNamespace") is not None:
            return True
        if schema is None:
            return False
        sourceDefaults = getattr(schema, "formDefaultOverrides", None)
        if sourceDefaults and element is not None and id(element) in sourceDefaults:
            elementDefault, attributeDefault = sourceDefaults[id(element)]
            default = attributeDefault if is_attribute else elementDefault
            return (default or "unqualified") == "qualified"
        default = (
            schema.getAttributeFormDefault() if is_attribute else schema.getElementFormDefault()
        )
        return default == "qualified"

    def resolveSchemaQName(self, value, *, parser=None):
        """Resolves a lexical QName written in this schema element.

        This is used for QName *values* (``type``, ``base``, ``ref``,
        ``substitutionGroup``, ``memberTypes``, ...), so an unprefixed
        name resolves through the in-scope default namespace exactly as
        XSD requires. (Unprefixed attribute *names* are never in the
        default namespace, but that rule does not apply to these
        attribute values.) In ``legacy`` namespace mode the value is
        returned unchanged. An unbound prefix is reported as
        ``unknown-namespace-prefix`` and the raw value is returned so the
        caller's legacy fallback can still run.

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
        # QName-valued schema attributes are of type ``xs:QName`` (or a
        # list of them), whose whiteSpace facet is ``collapse``, so
        # surrounding whitespace is not part of the reference.
        if isinstance(value, str):
            value = value.strip()
        try:
            return context.resolve(self.xsdElement, value)
        except NamespaceError as e:
            if parser is not None:
                parser.report.add_error(
                    str(e),
                    code="unknown-namespace-prefix",
                    element=self.name,
                )
            return value

    def resolveReference(self, value, candidates, *, parser=None):
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
        resolved = self.resolveSchemaQName(value, parser=parser)
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

    def simpleVariety(self, _seen=None):
        """Returns the XSD {variety} of this type representative.

        ``"atomic"``, ``"list"``, ``"union"`` or ``"complex"``; ``None``
        when the representative is neither a simple nor a complex type.
        A restriction inherits the variety of its base, so a restriction
        of a list is still a list and one of a union is still a union.
        ``_seen`` guards against a derivation cycle.
        """
        kind = type(self).__name__
        if kind == "ComplexType":
            return "complex"
        if kind != "SimpleType":
            return None
        if _seen is None:
            _seen = set()
        if id(self) in _seen:
            return None
        _seen = _seen | {id(self)}
        for child in self.processedChildren or ():
            if child is None:
                continue
            childKind = type(child).__name__
            if childKind == "List":
                return "list"
            if childKind == "Union":
                return "union"
        for child in self.processedChildren or ():
            if child is None or type(child).__name__ != "Restriction":
                continue
            base = child.tagAttributes.get("base")
            if base is None:
                # A restriction may derive from an inline simple type.
                for grandchild in child.processedChildren or ():
                    if grandchild is not None and type(grandchild).__name__ == "SimpleType":
                        return grandchild.simpleVariety(_seen)
                return None
            variety, _ = self.varietyOfReference(base, _seen)
            return variety
        return "atomic"

    def varietyOfReference(self, value, _seen=None):
        """Returns the {variety} of the type named by a lexical QName.

        Returns a ``(variety, representative)`` pair; the representative
        is ``None`` for a built-in type or an unresolvable name. Built-ins
        are recognised by the XML Schema namespace URI (so any prefix
        bound to it works), with the legacy ``xs:``/``xsd:`` spelling as
        a fallback. A built-in list type (``IDREFS`` and friends) reports
        ``"list"``, and the two ur-types report ``"non-atomic"``.
        """
        if _seen is None:
            _seen = set()
        resolved = self.resolveSchemaQName(value)
        local = local_name(resolved)
        uri = namespace_of(resolved)
        isBuiltin = uri == XSD_NS or (isinstance(value, str) and value.startswith(("xs:", "xsd:")))
        if isBuiltin or (uri is None and local in _PRIMITIVE_TYPES):
            return _builtinVariety(local), None
        er = self.resolveReference(value, self._globalTypeCandidates())
        if er is None:
            return None, None
        return er.simpleVariety(_seen), er

    def unionTransitiveMembershipHasNoList(self, unionER, _seen=None):
        """Whether a union's transitive membership holds no list type.

        XSD 1.1 §3.16.6.2 lets a list take a union as its item type when
        no type of variety ``list`` appears anywhere in the union's
        transitive membership; nested unions are followed recursively.
        A complex or other non-simple member is illegal as well (``list``
        item types must be simple). Atomic and unresolved members are
        acceptable here (an unresolved name is reported separately as
        ``unknown-type``).
        """
        if _seen is None:
            _seen = set()
        _seen = _seen | {id(unionER)}
        for memberName in getattr(unionER, "unionSpec", ()) or ():
            variety, memberER = self.varietyOfReference(memberName, _seen)
            if not self._membershipVarietyHasNoList(variety, memberER, _seen):
                return False
        for child in unionER.processedChildren or ():
            if (
                child is not None
                and type(child).__name__ == "SimpleType"
                and not self._membershipVarietyHasNoList(child.simpleVariety(_seen), child, _seen)
            ):
                return False
        return True

    def _membershipVarietyHasNoList(self, variety, memberER, _seen):
        """Whether one member's variety is legal under the list item rule.

        Atomic and unresolved members are acceptable; a union is
        acceptable when its own transitive membership has no list; a list
        or complex type is not.
        """
        if variety in (None, "atomic"):
            return True
        if variety == "union":
            return memberER is not None and self.unionTransitiveMembershipHasNoList(memberER, _seen)
        return False

    def _globalTypeCandidates(self):
        """Returns the global simple and complex type representatives.

        Both kinds share the ``type`` symbol space. The parser-owned
        component table keeps one entry per expanded name, so it is
        preferred; the per-schema dictionaries are the fallback. The
        list is cached on the schema because the component set does not
        change after the ER tree is built.
        """
        schema = self.getSchema()
        cached = getattr(schema, "_globalTypeCandidatesCache", None)
        if cached is not None:
            return cached
        table = getattr(schema, "components", None)
        candidates = []
        if isinstance(table, ComponentTable):
            for entries in table.values():
                for entry in entries:
                    if componentKind(entry) == "type" and entry.checkTopLevelType():
                        candidates.append(entry)
        else:
            candidates.extend(getattr(schema, "simpleTypes", {}).values())
            candidates.extend(getattr(schema, "complexTypes", {}).values())
        schema._globalTypeCandidatesCache = candidates
        return candidates

    def _globalComponentCandidates(self, kind, legacy_values, *, parser=None):
        """Returns the global candidates a reference may resolve to.

        In ``strict`` namespace mode the per-schema ``attributeGroups``
        dictionary is keyed by local name, so two definitions that share a
        local name in different namespaces (for example ``x:car`` and
        ``y:car``) collapse onto a single entry. The parser-owned component
        table preserves both by expanded name, so gather the global
        definitions of *kind* from it instead.

        A local name is only taken from the component table when it is
        declared more than once *in different namespaces* — the case the
        local-name mapping cannot represent. Otherwise the historical
        mapping is used, so duplicate expanded names (typically a
        circular ``xs:redefine`` chain, which the suite leaves
        implementation-defined) keep their existing resolution.

        In ``legacy`` mode the historical mapping is used unchanged.
        """
        if parser is None:
            parser = getattr(self, "pyXSD", None) or getattr(self.getSchema(), "pyXSD", None)
        mode = getattr(parser, "mode", None)
        legacy = legacy_values if isinstance(legacy_values, dict) else None
        if getattr(mode, "namespaces", "legacy") != "strict":
            return list(legacy.values()) if legacy is not None else legacy_values
        table = getattr(self.getSchema(), "components", None)
        if not isinstance(table, ComponentTable):
            return list(legacy.values()) if legacy is not None else legacy_values
        byLocal: dict[str, list] = {}
        for entries in table.values():
            for entry in entries:
                if componentKind(entry) == kind and entry.checkTopLevelType():
                    byLocal.setdefault(entry.name, []).append(entry)
        candidates = []
        for local, entries in byLocal.items():
            namespaces = {entry.getNamespace() for entry in entries}
            if len(entries) >= 2 and len(namespaces) >= 2:
                # Same local name in two namespaces: only the component
                # table can distinguish them.
                candidates.extend(entries)
                continue
            legacyEntry = legacy.get(local) if legacy is not None else None
            candidates.append(legacyEntry if legacyEntry is not None else entries[0])
        return candidates

    def _globalAttributeGroupCandidates(self, *, parser=None):
        """Global ``xs:attributeGroup`` definitions, namespace-aware."""
        return self._globalComponentCandidates(
            "attributeGroup", self.getSchema().attributeGroups, parser=parser
        )

    def resolvedTypeName(self):
        """Returns the ``type`` attribute resolved to a Clark name.

        Returns ``None`` when the declaration carries no ``type``.
        """
        raw = self.__dict__.get("type")
        if raw is None:
            return None
        if "|" in raw:
            # An inline type's bookkeeping name (a pipe can never occur
            # in a QName): parser.classes is keyed by the bare name, so
            # QName resolution -- which would apply the default xmlns
            # and corrupt the name in unprefixed-schema documents -- is
            # bypassed.
            return raw
        return self.resolveSchemaQName(raw)

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
        declarations. Declarations are keyed by name and kind, and the
        first declaration of a given (kind, name) wins — except that a
        local (or global) **element** declaration must not shadow a
        same-named declaration of the other scope: wildcard admission
        and the XSD 1.1 dynamic EDC rule resolve the global declaration
        a wildcard selects, and a local particle sharing the expanded
        name is a distinct component (wild063/wild076). A different
        kind may always register the same name.
        """
        table = _tableFor(obj)
        entries = table.setdefault(name, [])
        kind = componentKind(obj)
        namespace = obj.getNamespace()
        global_ = obj.isGlobalDeclaration()

        def conflicts(entry) -> bool:
            if componentKind(entry) != kind or entry.getNamespace() != namespace:
                return False
            if kind == "element":
                return entry.isGlobalDeclaration() == global_
            return True

        if any(conflicts(entry) for entry in entries):
            logger.debug(
                "an element representative named %r (kind %r, namespace %r, "
                "global %r) is already registered; keeping the first one",
                name,
                kind,
                namespace,
                global_,
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


#: Built-in XSD types whose {variety} is list rather than atomic.
_BUILTIN_LIST_TYPES = frozenset({"IDREFS", "ENTITIES", "NMTOKENS"})

#: Built-in XSD types that have no atomic value space (the ur-types).
_NON_ATOMIC_BUILTINS = frozenset({"anySimpleType", "anyType"})


def _builtinVariety(localName):
    """Returns the {variety} of a built-in XSD type by local name.

    Every built-in is an atomic simple type except the three named list
    types and the two ur-types, which have no atomic value space.
    """
    if localName in _NON_ATOMIC_BUILTINS:
        return "non-atomic"
    if localName in _BUILTIN_LIST_TYPES:
        return "list"
    return "atomic"


# The active parser's component table, keyed by name. Each ``PyXSD``
# parse installs its own :class:`ComponentTable` here so registrations
# during class building and detached lookups (``getFromName``) see the
# right parser's declarations. Multiple ERs may share a name and are
# disambiguated by component kind (see ``ComponentTable.getFromName``).
registry = _RegistryProxy()


# Namespace overrides for components spliced in from imported schemas.
# Keyed by ``id(xsdElement)`` because ElementTree elements do not allow
# attribute assignment. The parser installs the map before the ER run so
# a component can report the namespace of the document it was declared
# in rather than the main schema's target namespace.
def get_active_namespace_overrides() -> dict[int, str | None]:
    """Returns a snapshot of the active namespace-override map.

    Read once, at ``Schema`` construction time, so each parser's schema
    representative captures its own parser's snapshot. The map lives on
    the thread's schema context rather than a module global, so
    concurrent or nested parsers cannot see each other's overrides.
    """
    return dict(context_or_ambient().namespace_overrides)


def set_active_namespace_overrides(overrides: dict[int, str | None]) -> None:
    """Installs the parser-owned per-component namespace overrides.

    The map is stored as a snapshot copy, so mutating the caller's dict
    afterwards cannot rewrite declarations that already captured it.
    """
    context_or_ambient().namespace_overrides = dict(overrides)


def get_active_injected_builtin_ids() -> set[int]:
    """Returns a snapshot of the parser-injected built-in component ids.

    The parser injects the implicit ``xml`` and ``xsi`` namespace
    attribute declarations itself. Those live in well-known namespaces
    that a user schema may also target, so declaration-legality checks
    need to tell them apart from spliced user declarations: only the
    injected ones are exempt. Read once, at ``Schema`` construction
    time, mirroring :func:`get_active_namespace_overrides`.
    """
    return set(context_or_ambient().injected_builtin_ids)


def get_active_form_defaults() -> dict[int, tuple[str | None, str | None]]:
    """Returns a snapshot of the active source form-default map.

    Read once, at ``Schema`` construction time, so each parser's schema
    representative captures its own parser's snapshot.
    """
    return dict(context_or_ambient().form_defaults)


def set_active_form_defaults(
    defaults: dict[int, tuple[str | None, str | None]],
) -> None:
    """Installs the parser-owned source form defaults, as a snapshot.

    See :func:`set_active_namespace_overrides` for the snapshot
    discipline.
    """
    context_or_ambient().form_defaults = dict(defaults)


def get_active_xpath_default_namespaces() -> dict[int, str | None]:
    """Returns a snapshot of the active source XPath-default map.

    Only included/imported documents that declare an
    ``xpathDefaultNamespace`` are recorded, so the main document's value
    still applies to components that carry none of their own. Read once,
    at ``Schema`` construction time.
    """
    return dict(context_or_ambient().xpath_default_namespaces)


# Import all of the tag-specific classes after the ER class definition
# (the tag modules import this module's ElementRepresentative).  This
# replaces the old exec-based import loop.
from pyxsd.assertions import Assert, AssertionFacet  # noqa: E402
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
from pyxsd.element_representatives.fraction_digits import FractionDigits  # noqa: E402
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
from pyxsd.element_representatives.max_length import MaxLength  # noqa: E402
from pyxsd.element_representatives.min_exclusive import MinExclusive  # noqa: E402
from pyxsd.element_representatives.min_inclusive import MinInclusive  # noqa: E402
from pyxsd.element_representatives.min_length import MinLength  # noqa: E402
from pyxsd.element_representatives.notation import Notation  # noqa: E402
from pyxsd.element_representatives.pattern import Pattern  # noqa: E402
from pyxsd.element_representatives.restriction import Restriction  # noqa: E402
from pyxsd.element_representatives.schema import Schema  # noqa: E402
from pyxsd.element_representatives.sequence import Sequence  # noqa: E402
from pyxsd.element_representatives.simple_content import SimpleContent  # noqa: E402
from pyxsd.element_representatives.simple_type import SimpleType  # noqa: E402
from pyxsd.element_representatives.total_digits import TotalDigits  # noqa: E402
from pyxsd.element_representatives.union import Union  # noqa: E402
from pyxsd.element_representatives.white_space import WhiteSpace  # noqa: E402
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
    "Assert": Assert,
    "Assertion": AssertionFacet,
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
    "MinLength": MinLength,
    "MaxLength": MaxLength,
    "Notation": Notation,
    "TotalDigits": TotalDigits,
    "FractionDigits": FractionDigits,
    "WhiteSpace": WhiteSpace,
    "MinInclusive": MinInclusive,
    "MaxInclusive": MaxInclusive,
    "MinExclusive": MinExclusive,
    "MaxExclusive": MaxExclusive,
}
