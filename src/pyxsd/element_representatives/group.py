from pyxsd.element_representatives.element_representative import ElementRepresentative


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

    def getName(self):
        """Top-level definitions use their schema name; reference
        sites make a name like this-
        ``ContainingTypeName``|groupRef|``ref``.
        """
        if self.isRefSite:
            contName = self.getContainingTypeName()
            return f"{contName}|groupRef|{self.xsdElement.get('ref')}"
        return self.xsdElement.get("name")
