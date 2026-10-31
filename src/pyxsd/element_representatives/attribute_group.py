from pyxsd.element_representatives.element_representative import (
    ElementRepresentative,
    componentKind,
)
from pyxsd.namespaces import local_part
from pyxsd.xsd_data_types import NCName


def _bucket_by_name(candidates) -> dict:
    """Bucket candidates by local name, preserving their input order."""
    by_name: dict = {}
    for candidate in candidates:
        by_name.setdefault(candidate.name, []).append(candidate)
    return by_name


def _attribute_group_index(schema) -> dict:
    """The per-compile attributeGroup candidate index.

    Lives on the schema declaration as ``_attribute_group_index`` so
    every reference-site resolution on one compile shares one table
    (scanning the whole component table per reference is what made
    group-heavy schemas pay references times components per compile). The
    schema representative is a stable per-compile object, the component
    table it indexes is frozen once the ER run finishes (ahead of the
    declaration checks that consult this index), and the cache dies
    with the schema, so nothing leaks between compiles.

    - ``candidates``: the global attributeGroup declarations, in the
      component table's scan order (the order the per-call scan used;
      the historical ``attributeGroups`` dict is the source when no
      component table was built)
    - ``by_name``: each name's declaration candidates as a list,
      preserving that order, so a reference consults only same-name
      declarations
    - ``class_by_name``: the class-compatible candidates (built from
      ``_globalAttributeGroupCandidates``), bucketed once per compile by
      :func:`_class_attribute_group_bucket`. This bucket can differ from
      ``by_name`` for duplicate/redefine and legacy same-local-name
      declarations; a reference site may share a declaration-time target
      across phases only when the two same-name sequences match.

    There is deliberately no memo keyed by the lexical ``ref`` string:
    two reference sites spelling the same QName in different composed
    documents may bind its prefix differently, so one shared lexical
    answer could be wrong. Reference sites never enter the index: they
    fail ``checkTopLevelType`` exactly as they failed the scan's filter.
    """
    cache = getattr(schema, "_attribute_group_index", None)
    if cache is None:
        table = getattr(schema, "components", None)
        candidates = []
        if table:
            for entries in table.values():
                for entry in entries:
                    if componentKind(entry) == "attributeGroup" and entry.checkTopLevelType():
                        candidates.append(entry)
        else:
            candidates = list(getattr(schema, "attributeGroups", {}).values())
        cache = {
            "candidates": candidates,
            "by_name": _bucket_by_name(candidates),
        }
        schema._attribute_group_index = cache
    return cache


def _class_attribute_group_bucket(schema, local, *, parser):
    """Return the current class-building candidates for one local name.

    Class building historically selected from
    ``_globalAttributeGroupCandidates``, whose duplicate/redefine and
    legacy same-local-name rules are not literally identical to the
    declaration index's component-table order. Bucket those candidates
    once per compile, in their original order, so a reference site can
    compare the two sequences and share a declaration-time target only
    when they are identical.
    """
    index = _attribute_group_index(schema)
    class_by_name = index.get("class_by_name")
    if class_by_name is None:
        class_by_name = _bucket_by_name(schema._globalAttributeGroupCandidates(parser=parser))
        index["class_by_name"] = class_by_name
    return class_by_name.get(local, ())


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

    def _resolve_group(self, *, parser=None, for_class_building=False):
        """Return this site's global group, caching compatible successes only.

        Declaration checks and class building share one successful
        resolution per site when both consult the same candidate
        sequence. Class building keeps its historical
        ``_globalAttributeGroupCandidates`` selection: when its bucket
        differs from the declaration index's bucket, the site ignores
        any declaration-time target and resolves against the class
        bucket without caching the result. A failed resolution is never
        cached, so repeated diagnostics keep flowing through
        ``resolveReference``.
        """
        schema = self.getSchema()
        if parser is None:
            parser = getattr(schema, "host", None)
        index = _attribute_group_index(schema)
        local = local_part(str(self.ref).strip())
        declaration_candidates = index["by_name"].get(local, ())
        selected_candidates = declaration_candidates
        cacheable = True
        if for_class_building:
            selected_candidates = _class_attribute_group_bucket(schema, local, parser=parser)
            cacheable = tuple(declaration_candidates) == tuple(selected_candidates)
        if cacheable:
            cached = getattr(self, "_resolvedAttributeGroup", None)
            if cached is not None:
                return cached
        resolved = self.resolveReference(self.ref, selected_candidates, parser=parser)
        if cacheable and resolved is not None:
            self._resolvedAttributeGroup = resolved
        return resolved

    def _resolves_to_group(self) -> bool:
        """Whether ``self.ref`` names a global attributeGroup component.

        Delegates to :meth:`_resolve_group`, which consults the
        per-compile index and caches a successful declaration-time
        target for reuse by compatible class-building calls.
        """
        return self._resolve_group() is not None
