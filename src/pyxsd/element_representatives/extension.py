import logging

from pyxsd.element_representatives.element_representative import ElementRepresentative

logger = logging.getLogger(__name__)


class Extension(ElementRepresentative):
    """The class for the extension tag."""

    def __init__(self, xsdElement, parent):
        """See ElementRepresentative for documentation."""
        super().__init__(xsdElement, parent)
        self.addSuperClassName(self.tagAttributes["base"])

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|extension.  The
        name on this class is used for almost nothing.
        """
        contName = self.getContainingTypeName()
        return contName + "|extension"

    def addBaseToComplexType(self):
        """Used by complexContent. Adds its base to the complexType."""
        baseType = self.getFromName(self.tagAttributes["base"])
        if not baseType:
            logger.warning(
                "could not resolve the base %r for the extension of %s",
                self.tagAttributes.get("base"),
                self.name,
            )
            return None
        return None
