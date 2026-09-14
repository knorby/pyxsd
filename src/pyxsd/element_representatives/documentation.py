import logging

from pyxsd.element_representatives.element_representative import ElementRepresentative

logger = logging.getLogger(__name__)


class Documentation(ElementRepresentative):
    """The class for the documentation tag."""

    def __init__(self, xsdElement, parent):
        """See ElementRepresentative for documentation.  Adds its
        documentation to the ``__doc__`` field for the ER.
        """
        super().__init__(xsdElement, parent)
        self.contType = self.getContainingType()
        if xsdElement.text is not None:
            self.contType.__doc__ = xsdElement.text.strip()

    def processChildren(self):
        """``documentation`` content is arbitrary XML, never schema
        components; a child such as an unqualified ``<Documentation/>``
        must be ignored rather than treated as a declaration.
        """
        for child in self.xsdElement:
            logger.debug("Skipping documentation content %s", child.tag)
            self.processedChildren.append(None)
        return None

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|Documentation.
        The name on this class is used for almost nothing.
        """
        return f"{self.getContainingTypeName()}|{self.__class__.__name__}"
