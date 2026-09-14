import logging

from pyxsd.element_representatives.element_representative import ElementRepresentative
from pyxsd.namespaces import XSD_NS, namespace_of

logger = logging.getLogger(__name__)


class Annotation(ElementRepresentative):
    """The class for the annotation tag."""

    def __init__(self, xsdElement, parent):
        """See ElementRepresentative for documentation."""
        super().__init__(xsdElement, parent)

    def processChildren(self):
        """Only XSD-namespaced children are schema components.

        An ``annotation`` element itself carries ``appinfo`` and
        ``documentation`` children, but a malformed schema may nest
        arbitrary foreign XML inside those; content outside the XSD
        namespace is not a component and must not be interpreted.
        """
        for child in self.xsdElement:
            if namespace_of(child.tag) != XSD_NS:
                logger.debug("Skipping foreign annotation content %s", child.tag)
                self.processedChildren.append(None)
                continue
            self.processedChildren.append(ElementRepresentative.factory(child, self))
        return None

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|Annotation.  The
        name on this class is used for almost nothing.
        """
        contName = self.getContainingTypeName()
        return f"{contName}|{self.__class__.__name__}"
