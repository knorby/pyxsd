from pyxsd.element_representatives.element_representative import ElementRepresentative


class All(ElementRepresentative):
    """The class for the ``all`` tag.

    Like ``sequence`` and ``choice``, but its element children may
    appear in any order in an instance document (each at most once in
    XSD 1.0). Adds itself to the ``sequencesOrChoices`` list in its
    containing type, so the compositor machinery treats it uniformly.
    """

    #: An ``all`` may carry at most one ``annotation``; its element (and
    #: XSD 1.1 wildcard/group) children may repeat, so only the
    #: annotation is capped.
    _MAX_ONE_CHILDREN = ("annotation",)

    def __init__(self, xsdElement, parent):
        """Adds itself to the sequencesOrChoices list in its containing
        type.  Makes a blank list for element children.  Uses the ER
        ``__init__``.  See ElementRepresentative for more
        documentation.
        """
        self.elements = []
        super().__init__(xsdElement, parent)
        containingType = self.getContainingType()
        compositors = getattr(containingType, "sequencesOrChoices", None)
        if compositors is not None:
            compositors.append(self)
        else:
            self.misplacement = (
                "misplaced-declaration",
                f"all cannot appear inside {containingType.__class__.__name__}",
            )

    def getName(self):
        """Makes a name like this- ``all````some id number``."""
        compositors = getattr(self.getContainingType(), "sequencesOrChoices", None)
        allNum = len(compositors) + 1 if compositors is not None else 1
        return f"all{allNum}"
