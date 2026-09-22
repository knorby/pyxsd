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

    #: The particle tags an ``all`` may contain: element declarations,
    #: wildcards (XSD 1.1) and group references that resolve to an
    #: ``all`` group (XSD 1.1). Anything else — a nested compositor, a
    #: group definition, an inline type — is reported ``all-rule``.
    _PARTICLE_TAGS = ("annotation", "element", "any", "group")

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

    def checkDeclarationLegality(self):
        """Reports ``all`` compositor legality problems (``all-rule``).

        XSD 1.1 relaxes 1.0 by letting the element (and wildcard)
        particles of an ``all`` carry relaxed occurrence bounds, but the
        ``all`` itself must still have a ``minOccurs`` of 0 or 1 and a
        ``maxOccurs`` of exactly 1, it must sit at the root of a content
        model (never directly inside a ``sequence`` or ``choice``), its
        particles are limited to element declarations, wildcards and
        group references that resolve to an ``all`` group, and it
        carries no ``name`` attribute. Duplicate element particles under
        one ``all`` are reported by the parser's content-model sweep.
        """
        self._checkAllName()
        self._checkAllOccursBounds()
        self._checkAllPlacement()
        self._checkAllChildren()
        self._checkAllParticleOccurs()

    def _checkAllName(self) -> None:
        """``all`` has no ``name`` attribute in its XML representation."""
        if self.xsdElement.get("name") is not None:
            self._reportSchemaError(
                "all must not carry a name attribute",
                code="all-rule",
            )

    def _checkAllOccursBounds(self) -> None:
        """The ``all`` itself has {min occurs} in 0..1 and {max occurs} in 0..1.

        XSD 1.1 relaxes 1.0's fixed ``maxOccurs=1`` by allowing an
        *emptiable* ``all`` (``minOccurs=maxOccurs=0``, mgO001/mgO018)
        as a complex type's content model; the corpus keeps an
        emptiable ``all`` inside a group definition illegal (mgO019),
        as it does a contradictory ``minOccurs=1 maxOccurs=0``
        (mgC009) and any value above 1 (mgAb, mgP). Reading the bounds
        also reports lexical failures through ``_occursValue``
        (``invalid-occurs``); only lexically valid values outside the
        allowed range are reported here.
        """
        minimum = self.getMinOccurs()
        maximum = self.getMaxOccurs()
        if minimum > 1:
            self._reportSchemaError(
                f"all '{self.name}' has minOccurs={minimum}; only 0 or 1 is allowed",
                code="all-rule",
            )
        if maximum > 1:
            self._reportSchemaError(
                f"all '{self.name}' has maxOccurs="
                f"{self.tagAttributes.get('maxOccurs') or maximum}; only 0 or 1 is allowed",
                code="all-rule",
            )
            return
        if maximum == 0:
            if minimum >= 1:
                self._reportSchemaError(
                    f"all '{self.name}' has minOccurs={minimum} greater than maxOccurs=0",
                    code="all-rule",
                )
            elif self.parent is not None and self.parent.__class__.__name__ == "Group":
                self._reportSchemaError(
                    f"all '{self.name}' inside a group must not be emptiable (maxOccurs=0)",
                    code="all-rule",
                )

    def _checkAllPlacement(self) -> None:
        """An ``all`` is only the root of a content model.

        Reaching an ``all`` through a group reference inside a
        ``sequence``/``choice`` is reported by ``Group``
        (``_checkAllGroupReference``); this covers the direct shapes.
        """
        parentName = self.parent.__class__.__name__ if self.parent is not None else None
        if parentName in ("Sequence", "Choice"):
            self._reportSchemaError(
                "all can only appear as the root of a content model; it cannot "
                f"appear inside {parentName.lower()}",
                code="all-rule",
            )

    def _checkAllChildren(self) -> None:
        """Limits the particles of an ``all`` to the legal kinds."""
        for tag in self.childTags:
            if tag in self._PARTICLE_TAGS:
                continue
            self._reportSchemaError(
                f"all '{self.name}' may only contain element declarations, "
                f"wildcards and group references; found <{tag}>",
                code="all-rule",
            )
        for child in self.processedChildren:
            if child is not None and child.__class__.__name__ == "Group":
                self._checkAllGroupRef(child)

    def _checkAllParticleOccurs(self) -> None:
        """XSD 1.0 caps each element particle inside an ``all`` at 0..1.

        XSD 1.1 relaxes this (Saxon all001/all003: an element particle of
        an ``all`` may carry a relaxed ``minOccurs``/``maxOccurs``); in
        1.0 mode each over-occurring particle is reported ``all-rule``.
        """
        if not self._isXsd10():
            return
        for element in self.elements:
            if getattr(element, "isRefSite", False):
                continue
            minimum = element.getMinOccurs()
            maximum = element.getMaxOccurs()
            if minimum > 1 or maximum > 1:
                self._reportSchemaError(
                    f"element '{element.name}' in all '{self.name}' has "
                    f"minOccurs={minimum} maxOccurs={maximum}; "
                    "XSD 1.0 allows only 0 or 1",
                    code="all-rule",
                )

    def _checkAllGroupRef(self, refSite) -> None:
        """A group reference inside an ``all`` must name an ``all`` group.

        XSD 1.1 (all008/all009/all011): the referenced group's content
        model must itself be an ``all``, and the reference must carry
        ``minOccurs=maxOccurs=1``. A group *definition* is not a
        particle at all.
        """
        if not getattr(refSite, "isRefSite", False):
            self._reportSchemaError(
                f"all '{self.name}' may only contain a group reference, not a group definition",
                code="all-rule",
            )
            return
        minimum = refSite.getMinOccurs()
        maximum = refSite.getMaxOccurs()
        if minimum != 1 or maximum != 1:
            self._reportSchemaError(
                f"group reference '{refSite.ref}' inside all '{self.name}' must "
                "have minOccurs=maxOccurs=1",
                code="all-rule",
            )
        group = self.resolveGroupRef(refSite)
        compositor = group.getCompositor() if group is not None else None
        if compositor is None or compositor.__class__.__name__ != "All":
            self._reportSchemaError(
                f"group reference '{refSite.ref}' inside all '{self.name}' must "
                "name a group whose content model is an all",
                code="all-rule",
            )

    def _emptiableParticle(self, visited: set) -> bool:
        """An all can match zero when empty or all-empty children."""
        return self._compositorEmptiable(visited)
