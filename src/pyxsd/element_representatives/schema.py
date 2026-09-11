import copy

from pyxsd.element_representatives.complex_type import ComplexType
from pyxsd.element_representatives.element_representative import (
    _ACTIVE_NAMESPACE_OVERRIDES,
    ComponentTable,
)


class Schema(ComplexType):
    """The class for the schema tag.

    Subclass of ComplexType, because it is so similar to it.
    """

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
        # imported schemas (set by the parser before the ER run).
        self.namespaceOverrides = _ACTIVE_NAMESPACE_OVERRIDES
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
