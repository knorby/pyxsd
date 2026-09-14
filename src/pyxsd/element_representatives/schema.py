import copy

from pyxsd.element_representatives.complex_type import ComplexType
from pyxsd.element_representatives.element_representative import (
    ComponentTable,
    get_active_form_defaults,
    get_active_namespace_overrides,
)


class Schema(ComplexType):
    """The class for the schema tag.

    Subclass of ComplexType, because it is so similar to it.
    """

    # ``Schema`` reuses the ``ComplexType`` implementation but not its
    # child grammar: the schema's children are the top-level declaration
    # set. The table below is the Real Schema child grammar (XSD 1.0/1.1).
    _ALLOWED_CHILDREN = (
        "annotation",
        "include",
        "import",
        "redefine",
        "override",
        "defaultOpenContent",
        "notation",
        "attribute",
        "element",
        "simpleType",
        "complexType",
        "group",
        "attributeGroup",
    )
    #: ``annotation`` may appear repeatedly and in any position on the
    #: schema element (before the composition tags and before/after each
    #: declaration), so it is deliberately not a "max one" child; the
    #: order table below keeps imports/includes ahead of declarations
    #: (the generic order check ignores ``annotation``).
    _MAX_ONE_CHILDREN = ("defaultOpenContent",)
    _CHILD_ORDER = (
        ("include", "import", "redefine", "override"),
        ("annotation",),
        ("defaultOpenContent",),
        (
            "notation",
            "attribute",
            "element",
            "simpleType",
            "complexType",
            "group",
            "attributeGroup",
        ),
    )
    _EXCLUSIVE_SLOTS = frozenset()
    #: The schema root allows annotations in any position (and repeated),
    #: so the generic "annotation must be first" rule does not apply.
    _ANNOTATION_FIRST = False

    def __init__(self, xsdElement, parent):
        """Stores all the attributeGroups, complexTypes, and simpleTypes
        in the document in dictionaries. Also has a list of top-level
        elements (should be only one) and a dictionary of top-level
        groups.

        See ElementRepresentative for more documentation.
        """
        # Every ER in this parse registers into this parser-owned
        # table, so declarations cannot leak between parsers.
        self.components = ComponentTable()
        # Per-element namespace overrides for components spliced in from
        # imported schemas (set by the parser before the ER run). The
        # map is captured once, by value, so later parsers cannot
        # rewrite this schema's component namespaces.
        self.namespaceOverrides = get_active_namespace_overrides()
        # The source document's form defaults per spliced component,
        # captured with the same snapshot discipline.
        self.formDefaultOverrides = get_active_form_defaults()
        self.attributeGroups = {}
        self.complexTypes = {}
        self.simpleTypes = {}
        self.groups = {}
        self.substitutionGroups = {}
        self.elements = []
        self._elements = None
        super().__init__(xsdElement, parent)

    def getName(self):
        """Returns 'schema'."""
        return "schema"

    def getNamespace(self):
        """Returns the schema's ``targetNamespace``, or ``None``.

        Read from the element directly so it is available while the ER
        is still being registered (before ``tagAttributes`` is filled).
        """
        return self.xsdElement.get("targetNamespace")

    def getElementFormDefault(self):
        """Returns the schema's ``elementFormDefault`` (default unqualified)."""
        return self.xsdElement.get("elementFormDefault", "unqualified")

    def getAttributeFormDefault(self):
        """Returns the schema's ``attributeFormDefault`` (default unqualified)."""
        return self.xsdElement.get("attributeFormDefault", "unqualified")

    def getElements(self):
        """Returns a list of elements."""
        if self._elements is None:
            self._elements = copy.copy(self.elements)
        return self._elements

    def getSchema(self):
        """Returns the schema ER object.

        In ER, a method with the same name points down to the method by
        the same name in its parent.
        """
        return self
