from pyxsd.compositors import Compositor
from pyxsd.element_representatives.xsd_type import XsdType


class ComplexType(XsdType):
    """The class for the complexType tag."""

    def __init__(self, xsdElement, parent):
        """Keeps a list of sequences and choices that are children of it.
        Stores itself in the schema dictionary of complexTypes.  Uses
        the XsdType ``__init__``.  See ElementRepresentative for more
        documentation.
        """
        self.sequencesOrChoices = []
        super().__init__(xsdElement, parent)
        self.getSchema().complexTypes[self.name] = self

    def getElements(self):
        """Returns a list of elements.

        Uses lazy evaluation.  Goes through the ``sequencesOrChoices``
        list and each one's elements, adds its container information,
        then adds the element to a list which it returns.
        """
        elements = getattr(self, "elements_", None)

        if elements is not None:
            return elements

        self.elements_ = []
        for item in self.sequencesOrChoices:
            try:
                itemInfo = Compositor(item.tagType)
            except ValueError:
                itemInfo = None
            for element in item.elements:
                element.sOrC = itemInfo
                self.elements_.append(element)
        return self.elements_

    def gatherFacets(self):
        """Returns a blank dictionary. Needed for SimpleType, so the
        function can be called for any type without error.
        """
        return {}
