import logging

from pyxsd.element_representatives.element_representative import ElementRepresentative

logger = logging.getLogger(__name__)


class Extension(ElementRepresentative):
    """The class for the extension tag."""

    #: An extension adds a particle and attributes to a base; unlike a
    #: restriction it has no facets and no inline ``simpleType``.
    _ALLOWED_CHILDREN = (
        "annotation",
        "openContent",
        "group",
        "all",
        "choice",
        "sequence",
        "attribute",
        "attributeGroup",
        "anyAttribute",
        "assert",
    )
    _MAX_ONE_CHILDREN = (
        "annotation",
        "openContent",
        "group",
        "all",
        "choice",
        "sequence",
        "anyAttribute",
    )
    _CHILD_ORDER = (
        ("annotation",),
        ("openContent",),
        ("group", "all", "choice", "sequence"),
        ("attribute", "attributeGroup"),
        ("anyAttribute",),
        ("assert",),
    )
    #: The particle slot is an alternative: an extension holds at most
    #: one of group/all/choice/sequence.
    _ONE_OF_SLOTS = frozenset({2})

    # Set when the extension declares no ``base`` attribute; the
    # containing type reports it while building its class (see
    # ``XsdType._reportMissingDerivationBase``).
    hasNoBase: bool = False

    def __init__(self, xsdElement, parent):
        """See ElementRepresentative for documentation."""
        super().__init__(xsdElement, parent)
        base = self.tagAttributes.get("base")
        if base is None:
            self.hasNoBase = True
            return
        self.addSuperClassName(base)

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
