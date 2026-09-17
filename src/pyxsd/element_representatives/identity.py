"""Element representatives for XSD identity constraints.

XSD 1.0 lets an element declaration carry identity constraints:
``xs:key`` (fields present and unique), ``xs:unique`` (fields unique
when present) and ``xs:keyref`` (fields must match a named key or
unique). Each constraint contains ``xs:selector`` and ``xs:field``
child tags whose ``xpath`` attributes say which descendant nodes the
constraint covers and where the compared values come from.

These representatives record the constraint on their parent element
declaration and check the XML-representation legality of the
constraint (attribute set, name, required children) at schema phase;
the actual value checking happens after the instance document is
bound, in :mod:`pyxsd.identity`.
"""

import logging
from typing import TYPE_CHECKING, Any

from pyxsd.element_representatives.element_representative import ElementRepresentative
from pyxsd.namespaces import clark
from pyxsd.xpath_subset import XPathError, parse_xpath_subset
from pyxsd.xsd_data_types import NCName

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


class _DeclarationSite:
    """Namespace helpers for constraints and their selector/field terms.

    Both the constraint tags and the ``selector``/``field`` terms
    resolve QNames written in their own document against the
    declaration site's in-scope prefix bindings, the declaring
    document's target namespace and the effective
    ``xpathDefaultNamespace`` (XSD 1.1 §3.13.2 host-element
    precedence: the term's own attribute, else the containing
    constraint's, else the schema's).
    """

    if TYPE_CHECKING:
        #: Provided by ``ElementRepresentative``; declared here only
        #: for the type checker (the mixin never shadows them).
        getSchema: Callable[[], Any]
        getNamespace: Callable[[], Any]

    def _declarationNamespaces(self):
        """Returns the in-scope prefix bindings at the declaration site."""
        try:
            schema = self.getSchema()
        except AttributeError:
            return {}
        context = getattr(schema, "namespaceContext", None)
        if context is None:
            return {}
        return context.bindings_for(self.xsdElement)  # type: ignore[attr-defined]

    def _schemaTargetNamespace(self):
        """Returns the declaring schema document's target namespace."""
        try:
            return self.getSchema().getNamespace()
        except AttributeError:
            return None

    def _xpathDefaultNamespace(self):
        """Returns the namespace unprefixed element names resolve to.

        The effective ``xpathDefaultNamespace``: the term's or
        constraint's own attribute, else the containing constraint's,
        else the schema's (XSD 1.1 §3.13.2 host-element precedence).
        With no ``xpathDefaultNamespace`` declared anywhere the
        ``<schema>`` element's declared default ``##local`` applies,
        so unprefixed names are in no namespace (idG029: a
        default-``xmlns`` binding alone does not qualify selector
        names). The ``##defaultNamespace`` keyword form resolves to
        the declaration site's in-scope default namespace
        (``##targetNamespace`` the target namespace, ``##local`` the
        no namespace) and any other value is a literal namespace URI.
        """
        value = self._rawXpathDefaultNamespace()
        if value is None:
            return None
        if value == "##defaultNamespace":
            return self._declarationNamespaces().get("")
        if value == "##targetNamespace":
            return self._schemaTargetNamespace()
        if value == "##local":
            return None
        if value.startswith("##"):
            raise XPathError(f"invalid xpathDefaultNamespace value '{value}'")
        return value

    def _rawXpathDefaultNamespace(self):
        """Returns the raw ``xpathDefaultNamespace`` value in force."""
        for owner in (self, getattr(self, "parent", None)):
            attributes = getattr(owner, "tagAttributes", None) or {}
            value = attributes.get("xpathDefaultNamespace")
            if value is not None and value.strip():
                return value.strip()
        try:
            schema = self.getSchema()
        except AttributeError:
            return None
        return schema.xsdElement.get("xpathDefaultNamespace")


class IdentityConstraint(_DeclarationSite, ElementRepresentative):
    """Base class for the key, unique and keyref tags."""

    #: Local names of tags that may appear inside an identity
    #: constraint. ``element`` and other declarations are not allowed.
    _ALLOWED_CHILDREN = ("annotation", "selector", "field")

    #: At most one annotation, and the selector/field cardinality is
    #: checked against the parsed children (exactly one selector, at
    #: least one field).
    _MAX_ONE_CHILDREN = ("annotation",)

    #: ``annotation`` first, then the ``selector``, then the ``field``s.
    _CHILD_ORDER = (("annotation",), ("selector",), ("field",))

    #: Unqualified XML attributes the constraint's representation
    #: allows. ``refer`` extends the set on ``keyref``;
    #: ``xpathDefaultNamespace`` (XSD 1.1) is reserved legal here and
    #: interpreted by the XPath machinery.
    _ALLOWED_ATTRIBUTES: tuple[str, ...] = ("name", "id", "xpathDefaultNamespace")

    #: Attributes of the XSD 1.1 identity-constraint *reference* form
    #: (``<xs:unique ref="..."/>``): the site borrows the referred
    #: constraint's name, selector and fields, so it carries none of
    #: them itself.
    _REF_ALLOWED_ATTRIBUTES: tuple[str, ...] = ("ref", "id", "xpathDefaultNamespace")

    def __init__(self, xsdElement, parent):
        """Collects the selector and field paths from the child tags.

        ``xs:selector`` and ``xs:field`` children supply the XPath
        subset paths (the ``xpath`` attribute on each tag). Constraints
        are only legal on element declarations; a constraint found
        anywhere else is dropped from its container's identity table
        and reported by the parser's declaration walk.

        See ElementRepresentative for documentation.
        """
        super().__init__(xsdElement, parent)
        self.isConstraintRef = xsdElement.get("ref") is not None
        self.selector = self._firstPathOf("Selector")
        self.fieldPaths = [
            child.xpath for child in self._childrenOfKind("Field") if child is not None
        ]
        self.constraintName = self.xsdElement.get("name") or self.name
        container = self.parent
        if container is not None and container.__class__.__name__ == "Element":
            container.identities.append(self)
        else:
            # XSD 1.0 §3.11.2: identity constraints live only on
            # element declarations. The constraint is not registered on
            # the (non-element) container; the misplacement tuple is
            # reported by the parser's declaration walk, following the
            # construction-time pattern the attribute representatives
            # use.
            where = container.__class__.__name__ if container is not None else "unknown parent"
            self.misplacement = (
                "declaration-child",
                f"identity constraint '{self.constraintName}' must be declared "
                f"inside an element declaration; it was found inside a {where} "
                "and is ignored",
            )

    def _firstPathOf(self, kind):
        """Returns the first processed child of ``kind`` with a path."""
        for child in self._childrenOfKind(kind):
            if child is not None:
                return child.xpath
        return ""

    @property
    def parsedSelectorPath(self):
        """Returns the first selector child's parsed path, or ``None``.

        ``None`` means the schema-phase parse failed (an
        ``xpath-invalid`` error is already on the report) or no
        selector child exists.
        """
        for child in self._childrenOfKind("Selector"):
            if child is not None:
                return getattr(child, "parsedXPath", None)
        return None

    @property
    def parsedFieldPaths(self):
        """Returns the field children's parsed paths.

        The tuple is aligned with ``fieldPaths``; a ``None`` slot means
        that field's schema-phase parse failed.
        """
        return tuple(
            getattr(child, "parsedXPath", None)
            for child in self._childrenOfKind("Field")
            if child is not None
        )

    def _childrenOfKind(self, kind):
        """Returns the processed children whose class is ``kind``."""
        return [
            child
            for child in self.processedChildren
            if child is not None and child.__class__.__name__ == kind
        ]

    def getName(self):
        """Makes a unique bookkeeping name for the constraint."""
        contName = self.getContainingTypeName()
        return f"{contName}|{self.__class__.__name__}|{self.xsdElement.get('name') or '?'}"

    def checkDeclarationLegality(self):
        """Reports identity-constraint declaration legality problems.

        Covers the Identity-constraint Definition representation
        constraints at the XML level: the attribute set (bogus
        attributes are a schema error), the required ``name`` (an
        NCName when present), the ``refer`` requirement of ``keyref``
        and the required ``selector``/``field`` children (exactly one
        selector, at least one field). A reference site (``ref`` form)
        borrows all of that from the constraint it names and only
        carries ``ref``/``id``.
        """
        if self.isConstraintRef:
            self._checkConstraintAttributes()
            return
        self._checkConstraintName()
        self._checkConstraintAttributes()
        self._checkSelectorFieldChildren()
        self._checkRefer()

    def _checkConstraintName(self):
        """A constraint requires a name; a present one is an NCName."""
        name = self.xsdElement.get("name")
        if not name:
            self._reportSchemaError(
                f"{self.__class__.__name__} declaration is missing a name",
                code="declaration-name",
            )
            return
        if "|" in name:
            # "|"-containing names are bookkeeping renames the redefine
            # machinery writes onto base copies; they are not
            # author-written names.
            return
        try:
            NCName(name)
        except TypeError:
            self._reportSchemaError(
                f"identity constraint name '{name}' is not a valid NCName",
                code="declaration-attribute",
            )

    def _checkConstraintAttributes(self):
        """Every unqualified attribute must be in the allowed set.

        Qualified attributes (``vc:*`` version selectors, XSI and
        friends) are left to their own checks.
        """
        allowed = frozenset(
            self._REF_ALLOWED_ATTRIBUTES if self.isConstraintRef else self._ALLOWED_ATTRIBUTES
        )
        for raw in self._unqualifiedAttributes():
            if raw not in allowed:
                self._reportSchemaError(
                    f"<{self.rawTag}> does not allow the '{raw}' attribute",
                    code="declaration-attribute",
                )

    def _checkSelectorFieldChildren(self):
        """A constraint carries exactly one selector and a field."""
        selectors = self._childrenOfKind("Selector")
        if len(selectors) > 1:
            self._reportSchemaError(
                f"<{self.rawTag}> may contain at most one <selector>",
                code="declaration-child",
            )
        elif not selectors:
            self._reportSchemaError(
                f"<{self.rawTag}> requires a <selector> child",
                code="declaration-child",
            )
        if not self._childrenOfKind("Field"):
            self._reportSchemaError(
                f"<{self.rawTag}> requires at least one <field> child",
                code="declaration-child",
            )

    def _checkRefer(self):
        """Only ``keyref`` carries ``refer``; the base does nothing."""

    def _constraintClarkName(self):
        """Returns the constraint's Clark name, or ``None`` unnamed.

        Identity-constraint definitions are identified by their name
        and the target namespace of the schema document that declares
        them (XSD 1.1 §3.11.1), so ``refer`` and ``ref`` QNames
        resolve against exactly that pair. ``getNamespace()`` reflects
        the declaring document — included and imported components
        carry their own document's namespace, not the host's.
        """
        name = self.constraintName
        if not name:
            return None
        return clark(self.getNamespace(), name)

    def _referClarkName(self):
        """Resolves the keyref's ``refer`` QName to a Clark name.

        ``None`` means the QName itself does not resolve (an unbound
        prefix, or an unusable ``xpathDefaultNamespace``).
        """
        refer = (self.tagAttributes.get("refer") or "").strip()
        if not refer:
            return None
        return self._resolveComponentQName(refer)

    def _refClarkName(self):
        """Resolves a reference site's ``ref`` QName to a Clark name.

        ``None`` means the reference does not resolve (empty, or a
        prefix that is not declared).
        """
        ref = (self.tagAttributes.get("ref") or "").strip()
        if not ref:
            return None
        return self._resolveComponentQName(ref)

    def _resolveComponentQName(self, qname):
        """Resolves a QName-valued identity-constraint attribute.

        ``refer`` and ``ref`` name schema components, so their QNames
        resolve like ``type`` and ``ref`` elsewhere: a prefix through
        the declaration site's in-scope bindings, an unprefixed name
        through the declaration site's in-scope default namespace.
        Declaring an ``xpathDefaultNamespace`` (self → constraint →
        schema precedence) overrides that default for the constraint's
        references: ``##defaultNamespace`` keeps the in-scope default
        namespace, ``##targetNamespace`` the document's target
        namespace, ``##local`` no namespace, and any other value is a
        literal namespace URI. ``None`` means the name does not
        resolve.
        """
        qname = qname.strip()
        prefix, separator, local = qname.partition(":")
        if separator:
            namespace = self._declarationNamespaces().get(prefix)
            if namespace is None:
                return None
            return clark(namespace, local)
        default = self._rawXpathDefaultNamespace()
        if default in (None, "", "##defaultNamespace"):
            return clark(self._declarationNamespaces().get(""), qname)
        if default == "##targetNamespace":
            return clark(self.getNamespace(), qname)
        if default == "##local":
            return qname
        if default.startswith("##"):
            return None
        return clark(default, qname)

    def _unqualifiedAttributes(self):
        """Yields the unqualified attribute names of the raw element."""
        for raw in self.xsdElement.attrib:
            if not raw.startswith("{"):
                yield raw


class _PathTerm(_DeclarationSite, ElementRepresentative):
    """Shared representation rules for the selector and field tags."""

    #: Only an annotation may appear inside a selector or field;
    #: anything else is rejected by the child-grammar table.
    _ALLOWED_CHILDREN = ("annotation",)

    _MAX_ONE_CHILDREN = ("annotation",)

    #: Unqualified attributes the tag's representation allows.
    _ALLOWED_ATTRIBUTES = ("xpath", "id", "xpathDefaultNamespace")

    def checkDeclarationLegality(self):
        """Reports selector/field representation legality.

        The tags carry only ``xpath`` (plus the XSD 1.1
        ``xpathDefaultNamespace`` reservation): any other unqualified
        attribute and a missing or empty ``xpath`` are schema errors.
        A present ``xpath`` is parsed at schema phase under the
        declaration site's namespace bindings and effective
        ``xpathDefaultNamespace``; a path outside the subset is an
        ``xpath-invalid`` schema error and the constraint is left
        without a parsed path (the instance-phase evaluation then
        skips it).
        """
        allowed = frozenset(self._ALLOWED_ATTRIBUTES)
        for raw in self._unqualifiedAttributes():
            if raw not in allowed:
                self._reportSchemaError(
                    f"<{self.rawTag}> does not allow the '{raw}' attribute",
                    code="declaration-attribute",
                )
        xpath = self.tagAttributes.get("xpath")
        if xpath is None or not xpath.strip():
            self._reportSchemaError(
                f"<{self.rawTag}> requires a non-empty xpath attribute",
                code="declaration-attribute",
            )
            return
        self.parsedXPath = self._parseDeclarationXPath(xpath)

    def _parseDeclarationXPath(self, xpath):
        """Parses the ``xpath`` attribute at schema phase.

        Uses the declaration site's prefix bindings, the schema's
        target namespace and the effective ``xpathDefaultNamespace``.
        Returns the parsed path, or ``None`` after reporting an
        ``xpath-invalid`` schema error.
        """
        try:
            return parse_xpath_subset(
                xpath,
                self._declarationNamespaces(),
                self._xpathDefaultNamespace(),
                self._schemaTargetNamespace(),
            )
        except XPathError as exc:
            self._reportSchemaError(
                f"<{self.rawTag}> xpath '{xpath.strip()}' is outside the "
                f"identity-constraint XPath subset: {exc}",
                code="xpath-invalid",
            )
            return None

    def _unqualifiedAttributes(self):
        """Yields the unqualified attribute names of the raw element."""
        for raw in self.xsdElement.attrib:
            if not raw.startswith("{"):
                yield raw


class Selector(_PathTerm):
    """The class for the selector tag inside an identity constraint."""

    def checkDeclarationLegality(self):
        """Extends the shared checks with the selector's role rules.

        The selector grammar (XSD 1.1 §3.11.6.2) admits element steps
        only — attribute steps belong to fields (idI149).
        """
        super().checkDeclarationLegality()
        parsed = getattr(self, "parsedXPath", None)
        if parsed is None:
            return
        if any(
            step[0] == "attribute" for _descendant, steps in parsed.alternatives for step in steps
        ):
            self._reportSchemaError(
                "<selector> xpath must not select attribute nodes; "
                "attribute steps are only allowed in <field>",
                code="xpath-invalid",
            )
            self.parsedXPath = None

    def getName(self):
        """Returns a bookkeeping name for the selector."""
        return f"{self.getContainingTypeName()}|selector"


class Field(_PathTerm):
    """The class for the field tag inside an identity constraint."""

    def __init__(self, xsdElement, parent):
        """Records the field's ``xpath`` attribute.

        See ElementRepresentative for documentation.
        """
        super().__init__(xsdElement, parent)
        self.xpath = self.tagAttributes.get("xpath", "")

    def getName(self):
        """Returns a bookkeeping name for the field."""
        return f"{self.getContainingTypeName()}|field"


class Key(IdentityConstraint):
    """The class for the key tag."""


class Unique(IdentityConstraint):
    """The class for the unique tag."""


class Keyref(IdentityConstraint):
    """The class for the keyref tag.

    A keyref additionally carries a ``refer`` attribute naming the
    key or unique that its values must match.
    """

    _ALLOWED_ATTRIBUTES = (*IdentityConstraint._ALLOWED_ATTRIBUTES, "refer")

    def __init__(self, xsdElement, parent):
        """Stores the referenced constraint name.

        See IdentityConstraint and ElementRepresentative for
        documentation.
        """
        super().__init__(xsdElement, parent)
        self.refer = self.tagAttributes.get("refer", "")

    def _checkRefer(self):
        """A keyref requires a non-empty ``refer`` attribute."""
        refer = self.tagAttributes.get("refer")
        if refer is None or not refer.strip():
            self._reportSchemaError(
                "<keyref> requires a non-empty refer attribute",
                code="declaration-attribute",
            )
