import logging

from pyxsd.element_representatives.element_representative import ElementRepresentative
from pyxsd.namespaces import XML_NS
from pyxsd.xsd_data_types import Language

logger = logging.getLogger(__name__)


class Documentation(ElementRepresentative):
    """The class for the documentation tag."""

    #: The ``xml:lang`` attribute, in Clark notation.
    _XML_LANG = f"{{{XML_NS}}}lang"

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

    def checkDeclarationLegality(self):
        """Reports the ``xml:lang`` lexical constraint on documentation.

        ``xml:lang`` (when present) is an ``xs:language``; an empty or
        whitespace-only value is not in its lexical space. An empty
        ``source`` is deliberately *not* reported: the empty string is a
        valid ``xs:anyURI`` (annotB003 is expected valid).
        """
        value = self.tagAttributes.get(self._XML_LANG)
        if value is None:
            return
        try:
            Language(value)
        except TypeError:
            self._reportSchemaError(
                f"documentation xml:lang value '{value}' is not a valid language",
                code="declaration-attribute",
            )
