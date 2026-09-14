from pyxsd.element_representatives.element_representative import ElementRepresentative
from pyxsd.xsd_data_types import NCName


class Group(ElementRepresentative):
    """The class for the ``group`` tag.

    A group tag is either a top-level *definition* (carries a
    ``name``, wraps a ``sequence``/``choice``/``all`` compositor and
    is registered in the schema's ``groups`` dictionary) or a
    *reference site* (carries a ``ref`` and stands in for the named
    group's content model inside another compositor or type).
    Reference sites are flattened into the referencing type's element
    list when the generated class is built (see
    ``ComplexType._flattenGroupRef``).
    """

    #: Child grammar of a global ``group``: an optional annotation and
    #: exactly one particle (``all``/``choice``/``sequence``).
    _ALLOWED_CHILDREN = ("annotation", "all", "choice", "sequence")
    _MAX_ONE_CHILDREN = ("annotation", "all", "choice", "sequence")
    _CHILD_ORDER = (("all", "choice", "sequence"),)
    #: The particle slot holds mutually exclusive alternatives.
    _ONE_OF_SLOTS = frozenset({0})

    def __init__(self, xsdElement, parent):
        """Sets up the group as a definition or a reference site, then
        uses the ER ``__init__``.  See ElementRepresentative for more
        documentation.
        """
        self.isRefSite = xsdElement.get("ref") is not None
        if not self.isRefSite:
            # The compositor child appends itself here while the ER
            # children are processed, so this must exist first.
            self.sequencesOrChoices = []
        super().__init__(xsdElement, parent)
        if self.isRefSite:
            self.ref = self.tagAttributes["ref"]
            if parent is not None and parent.__class__.__name__ == "Schema":
                # A group reference is not a top-level declaration.
                self.misplacement = (
                    "misplaced-declaration",
                    f"group reference '{self.ref}' cannot appear at the top level of a schema",
                )
            elif parent is not None and parent.__class__.__name__ == "Any":
                # A wildcard is not a model group: it takes no group
                # particles (groupO026).
                self.misplacement = (
                    "misplaced-declaration",
                    f"group reference '{self.ref}' cannot appear inside an any wildcard",
                )
            elif hasattr(parent, "elements"):
                # Nested inside a compositor.
                parent.elements.append(self)
            else:
                # Direct child of a containing type (complexType,
                # extension, restriction, ...); only types that carry a
                # content model have sequencesOrChoices.
                containingType = self.getContainingType()
                compositors = getattr(containingType, "sequencesOrChoices", None)
                if compositors is not None:
                    compositors.append(self)
                else:
                    self.misplacement = (
                        "misplaced-declaration",
                        f"group reference '{self.ref}' cannot appear inside "
                        f"{containingType.__class__.__name__}",
                    )
        else:
            self.getSchema().groups[self.name] = self

    def getContainingType(self):
        """Group definitions are containing types; reference sites
        delegate to their parent chain.
        """
        if self.xsdElement.get("ref") is not None:
            return ElementRepresentative.getContainingType(self)
        return self

    def getCompositor(self):
        """Returns the compositor ER inside a group definition, or
        ``None`` if the group has no content model.
        """
        if self.isRefSite or not self.sequencesOrChoices:
            return None
        return self.sequencesOrChoices[0]

    def _emptiableParticle(self, visited: set) -> bool:
        """Whether this group particle can match zero elements.

        A definition is as emptiable as its compositor; a reference
        site is emptiable when it is optional or when the group it
        names is, resolving lazily through the definition. ``visited``
        holds the definitions already being resolved so a group
        reference cycle reads as not emptiable instead of recursing
        forever.
        """
        if not self.isRefSite:
            compositor = self.getCompositor()
            return compositor is not None and compositor._emptiableParticle(visited)
        if self._silentOccurs("minOccurs") == 0:
            return True
        group = self.resolveGroupRef(self)
        if group is None or id(group) in visited:
            return False
        visited.add(id(group))
        compositor = group.getCompositor()
        return compositor is not None and compositor._emptiableParticle(visited)

    def checkDeclarationLegality(self):
        """Reports group declaration and reference legality problems.

        A definition's ``name`` must be an NCName and a top-level group
        carries no occurrence attributes. A reference site must not
        carry ``name``, its ``minOccurs`` must not exceed its
        ``maxOccurs``, and a reference whose group's content model is
        an ``all`` must carry ``minOccurs=maxOccurs=1`` and may only be
        used where that ``all`` would itself be legal — the root of a
        content model (``all-rule``).
        """
        if self.isRefSite:
            self._checkRefSiteName()
            minimum = self.getMinOccurs()
            maximum = self.getMaxOccurs()
            if minimum > maximum:
                self._reportSchemaError(
                    f"group reference '{self.ref}' has minOccurs={minimum} greater "
                    f"than maxOccurs={maximum}",
                    code="declaration-attribute",
                )
            self._checkAllGroupReference(minimum, maximum)
        else:
            self._checkDefinitionPlacement()
            self._checkDefinitionName()
            self._checkGlobalOccurs()

    def _checkDefinitionPlacement(self) -> None:
        """A group definition is only a top-level (or redefined) component.

        ``name`` marks a definition, and definitions live at the top
        level of a schema (or inside ``redefine``); a ``name``-carrying
        ``group`` under an extension, restriction, sequence, choice or
        complexType is a misplaced declaration (groupC004-008) — the
        compositor child there must be a ``ref``.
        """
        parentName = self.parent.__class__.__name__ if self.parent is not None else None
        if parentName not in ("Schema", "Redefine"):
            self._reportSchemaError(
                f"group definition '{self.name}' may only appear at the top level "
                f"of a schema, not inside {parentName.lower() if parentName else 'nothing'}",
                code="misplaced-declaration",
            )

    def _checkRefSiteName(self) -> None:
        """``name`` is only allowed on the top-level definition."""
        if self.xsdElement.get("name") is not None:
            self._reportSchemaError(
                f"group reference '{self.ref}' must not carry a name attribute",
                code="declaration-attribute",
            )

    def _checkDefinitionName(self) -> None:
        """A group definition's ``name`` is an NCName."""
        name = self.xsdElement.get("name")
        if name is None or "|" in name:
            # "|"-containing names are bookkeeping renames the redefine
            # machinery writes onto the base copy of a redefined group;
            # they are not author-written names.
            return
        try:
            NCName(name)
        except TypeError:
            self._reportSchemaError(
                f"group name '{name}' is not a valid NCName",
                code="declaration-attribute",
            )

    def _checkGlobalOccurs(self) -> None:
        """A top-level group definition takes no occurrence attributes.

        The particle occurrence attributes belong on *references* to
        the group, not on the definition itself (groupD).
        """
        parentName = self.parent.__class__.__name__ if self.parent is not None else None
        if parentName not in ("Schema", "Redefine"):
            return
        for attr in ("minOccurs", "maxOccurs"):
            if attr in self.tagAttributes:
                self._reportSchemaError(
                    f"top-level group '{self.name}' must not carry '{attr}'",
                    code="declaration-attribute",
                )

    def _checkAllGroupReference(self, minimum, maximum) -> None:
        """Guards a reference to a group whose content model is an ``all``.

        A particle carrying an ``all`` term must have {min occurs} and
        {max occurs} of at most 1 (particlesEa: minOccurs=2 or
        maxOccurs=2 is illegal, minOccurs=0 is fine). The reference may
        also only be used where the ``all`` is itself legal — never
        from inside a ``sequence`` or ``choice``, directly or through
        the reference (mgA020: "all can only be root"). A reference
        *inside* an ``all`` is stricter — exactly 1/1 (all009) — and is
        checked by ``All``.
        """
        group = self.resolveGroupRef(self)
        compositor = group.getCompositor() if group is not None else None
        if compositor is None or compositor.__class__.__name__ != "All":
            return
        if minimum > 1 or maximum > 1:
            self._reportSchemaError(
                f"group reference '{self.ref}' names a group whose content model "
                "is an all; it must have minOccurs and maxOccurs of at most 1",
                code="all-rule",
            )
        node = self.parent
        while node is not None and node.__class__.__name__ != "Schema":
            if node.__class__.__name__ in ("Sequence", "Choice"):
                self._reportSchemaError(
                    f"group reference '{self.ref}' places an all inside a "
                    f"{node.__class__.__name__.lower()}; all can only be the root "
                    "of a content model",
                    code="all-rule",
                )
                return
            node = node.parent

    def getName(self):
        """Top-level definitions use their schema name; reference
        sites make a name like this-
        ``ContainingTypeName``|groupRef|``ref``.
        """
        if self.isRefSite:
            contName = self.getContainingTypeName()
            return f"{contName}|groupRef|{self.xsdElement.get('ref')}"
        return self.xsdElement.get("name")
