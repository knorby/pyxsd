from pyxsd.element_representatives.element_representative import (
    ElementRepresentative,
    componentKind,
)
from pyxsd.xsd_data_types import NCName


class AttributeGroup(ElementRepresentative):
    """The class for the attributeGroup tag.

    A top-level definition (carries a ``name``) collects its
    attribute children into a dictionary and registers itself in the
    schema's ``attributeGroups`` dictionary. A reference site (carries
    a ``ref``, usually inside a complexType or its restriction/
    extension) is recorded on the containing type and resolved when
    the generated class is built, pulling the named group's
    attribute descriptors into that type (see
    ``XsdType.resolveAttributeGroupRefs``).
    """

    #: Child grammar of an ``attributeGroup`` definition: an optional
    #: annotation, any number of attribute/attributeGroup members, then
    #: an optional ``anyAttribute`` wildcard.
    _ALLOWED_CHILDREN = ("annotation", "attribute", "attributeGroup", "anyAttribute")
    _MAX_ONE_CHILDREN = ("annotation", "anyAttribute")
    _CHILD_ORDER = (("attribute", "attributeGroup"), ("anyAttribute",))

    def __init__(self, xsdElement, parent):
        """Creates a dictionary for attributes.  Adds itself to the
        attribute group dictionary in schema (definitions) or to the
        containing type's reference list (reference sites).  See
        ElementRepresentative for more documentation.
        """
        self.attributes = {}
        # Reference sites nested inside a definition (or another
        # reference) collect here so resolution can recurse.
        self.attributeGroupRefs = []
        super().__init__(xsdElement, parent)
        self.isRefSite = self.xsdElement.get("ref") is not None
        if self.isRefSite:
            self.ref = self.tagAttributes["ref"]
            self.getContainingType().attributeGroupRefs.append(self)
        else:
            container = self.parent.getContainingType()
            groups = getattr(container, "attributeGroups", None)
            if groups is not None:
                groups[self.name] = self
            else:
                self.misplacement = (
                    "misplaced-declaration",
                    f"attributeGroup '{self.name}' cannot be declared inside "
                    f"{container.__class__.__name__}",
                )
            self.getSchema().attributeGroups[self.name] = self

    def getContainingType(self):
        """Returns self for definitions, because attribute groups are
        containing types; reference sites delegate to their parent
        chain.
        """
        if self.xsdElement.get("ref") is not None:
            return ElementRepresentative.getContainingType(self)
        return self

    def getName(self):
        """Definitions use their schema name; reference sites make a
        name like this- ``ContainingTypeName``|attributeGroupRef|``ref``.
        """
        if self.xsdElement.get("ref") is not None:
            contName = self.getContainingTypeName()
            return f"{contName}|attributeGroupRef|{self.xsdElement.get('ref')}"
        return self.xsdElement.get("name")

    def checkDeclarationLegality(self) -> None:
        """Reports attributeGroup declaration and reference legality.

        A definition carries an NCName ``name`` and appears only at the
        top level of a schema (or a redefine). A reference site names a
        global attributeGroup, carries neither a ``name`` nor member
        children, and never appears at the top level (attgC001).
        """
        if self.isRefSite:
            self._checkRefSite()
        else:
            self._checkDefinition()

    def _checkDefinition(self) -> None:
        parentName = self.parent.__class__.__name__ if self.parent is not None else None
        if parentName not in ("Schema", "Redefine"):
            self._reportSchemaError(
                f"attributeGroup definition '{self.name}' may only appear at the "
                f"top level of a schema, not inside "
                f"{parentName.lower() if parentName else 'nothing'}",
                code="misplaced-declaration",
            )
        name = self.xsdElement.get("name")
        for duplicate in getattr(self, "_duplicateAttributeNames_", ()):
            self._reportSchemaError(
                f"attribute '{duplicate}' is declared more than once in attributeGroup '{name}'",
                code="duplicate-attribute",
            )
        if name is None or "|" in name:
            return
        try:
            NCName(name)
        except TypeError:
            self._reportSchemaError(
                f"attributeGroup name '{name}' is not a valid NCName",
                code="declaration-attribute",
            )

    def _checkRefSite(self) -> None:
        if self.parent is not None and self.parent.__class__.__name__ == "Schema":
            self._reportSchemaError(
                "a top-level attributeGroup must be a definition with a name, not a reference",
                code="declaration-attribute",
            )
        if self.xsdElement.get("name") is not None:
            self._reportSchemaError(
                f"attributeGroup reference '{self.ref}' must not carry a name attribute",
                code="declaration-attribute",
            )
        if not self.ref:
            self._reportSchemaError(
                "attributeGroup reference has an empty ref value",
                code="declaration-attribute",
            )
        members = [
            child
            for child in (self.processedChildren or ())
            if child is not None and child.__class__.__name__ != "Annotation"
        ]
        if members:
            self._reportSchemaError(
                f"attributeGroup reference '{self.ref}' must not carry child declarations",
                code="declaration-attribute",
            )
            return
        if self.ref and not self._resolves_to_group():
            self._reportSchemaError(
                f"attributeGroup reference '{self.ref}' does not name a global attributeGroup",
                code="unknown-attributeGroup",
            )

    def _resolves_to_group(self) -> bool:
        """Whether ``self.ref`` names a global attributeGroup component."""
        schema = self.getSchema()
        parser = getattr(schema, "pyXSD", None)
        candidates = []
        table = getattr(schema, "components", None)
        if table:
            for entries in table.values():
                for entry in entries:
                    if componentKind(entry) == "attributeGroup" and entry.checkTopLevelType():
                        candidates.append(entry)
        else:
            candidates = list(getattr(schema, "attributeGroups", {}).values())
        return self.resolveReference(self.ref, candidates, parser=parser) is not None
