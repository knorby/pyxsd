from pyxsd.element_representatives.element_representative import ElementRepresentative


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

    def __init__(self, xsdElement, parent):
        """Creates a dictionary for attributes.  Adds itself to the
        attribute group dictionary in schema (definitions) or to the
        containing type's reference list (reference sites).  See
        ElementRepresentative for more documentation.
        """
        self.attributes = {}
        super().__init__(xsdElement, parent)
        self.isRefSite = self.xsdElement.get("ref") is not None
        if self.isRefSite:
            self.ref = self.tagAttributes["ref"]
            self.getContainingType().attributeGroupRefs.append(self)
        else:
            attrGroupContainer = self.parent.getContainingType()
            attrGroupContainer.attributeGroups[self.name] = self
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
