import copy
import logging

from pyxsd.compositors import Compositor
from pyxsd.element_representatives.xsd_type import XsdType
from pyxsd.wildcards import register_wildcard

logger = logging.getLogger(__name__)


def _isTrue(value: str) -> bool:
    """XSD ``xs:boolean`` truth: ``true`` or ``1``, case-insensitive."""
    return str(value).strip().lower() in ("true", "1")


#: The model-group particles the empty-explicit-content rule recognises.
_MODEL_GROUP_KINDS = ("Group", "All", "Choice", "Sequence")

#: The particle children that make a model group non-empty.
_PARTICLE_KINDS = ("Group", "All", "Choice", "Sequence", "Element", "Any")


class ComplexType(XsdType):
    """The class for the complexType tag."""

    #: Child grammar of ``complexType`` (XSD 1.0/1.1): an optional
    #: annotation, openContent, a single content kind
    #: (simpleContent/complexContent or a particle), attributes, an
    #: anyAttribute and assertions.
    _ALLOWED_CHILDREN = (
        "annotation",
        "openContent",
        "simpleContent",
        "complexContent",
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
        "simpleContent",
        "complexContent",
        "anyAttribute",
    )
    _CHILD_ORDER = (
        ("annotation",),
        ("openContent",),
        ("simpleContent", "complexContent"),
        ("group", "all", "choice", "sequence"),
        ("attribute", "attributeGroup"),
        ("anyAttribute",),
        ("assert",),
    )
    #: A simpleContent/complexContent slot excludes later particle and
    #: attribute slots.
    _EXCLUSIVE_SLOTS = frozenset({2})
    #: The content-kind slot (simpleContent/complexContent) and the
    #: particle slot (group/all/choice/sequence) are each alternatives.
    _ONE_OF_SLOTS = frozenset({2, 3})

    def __init__(self, xsdElement, parent):
        """Keeps a list of sequences, choices, and alls that are
        children of it.  Stores itself in the schema dictionary of
        complexTypes.  Uses the XsdType ``__init__``.  See
        ElementRepresentative for more documentation.
        """
        self.sequencesOrChoices = []
        # A simpleContent restriction applies its facets directly to the
        # complex type, whose element representatives record them here
        # (the other facet attributes are set on first assignment).
        self.patterns = []
        self.assertions = []
        # Parsed by the xs:openContent ER during the declaration walk
        # (XSD 1.1 §3.4.2): ``None`` when the type declares no open
        # content. Content-model use is a later task.
        self.openContent = None
        super().__init__(xsdElement, parent)
        self.getSchema().complexTypes[self.name] = self

    def checkDeclarationLegality(self) -> None:
        """Reports complexType attribute legality.

        Covers the ``mixed`` xs:boolean lexical space, the ``name``
        NCName, and the rule that only a direct schema child may be
        named: an inline complexType (inside an element declaration or
        another type) is anonymous, and a stray ``name`` there is a
        schema error even though ``getName`` would happily register it
        as a global type.

        The XSD 1.1 ``assert`` children are compiled here (the parser
        sweep reaches every type before any class is built) and stored on
        ``compiledAssertions`` for the class builder; an unusable
        ``test`` is reported as ``assert-invalid``.
        """
        # Assertions are compiled first: an inline (bookkeeping-named)
        # declaration returns early below and would otherwise never
        # compile.
        from pyxsd.assertions import compile_assertions

        self.compiledAssertions = compile_assertions(self)
        mixed = self.tagAttributes.get("mixed")
        if mixed is not None and self._invalidBoolean(mixed):
            self._reportSchemaError(
                f"complexType '{self.name}' has an invalid mixed value "
                f"'{mixed}'; expected true, false, 1 or 0",
                code="declaration-attribute",
            )
        apply = self.tagAttributes.get("defaultAttributesApply")
        if apply is not None and self._invalidBoolean(apply):
            self._reportSchemaError(
                f"complexType '{self.name}' has an invalid "
                f"defaultAttributesApply value '{apply}'; expected true, "
                "false, 1 or 0",
                code="declaration-attribute",
            )
        name = self.xsdElement.get("name")
        if name is None or "|" in name:
            # A pipe marks an internal bookkeeping name (an inline type,
            # or the ``Name|base`` clone an xs:redefine keeps); it is not
            # a schema-author NCName and never subject to name legality.
            return
        if self._invalidNCName(name):
            self._reportSchemaError(
                f"complexType name '{name}' is not a valid NCName",
                code="declaration-attribute",
            )
        if not self.isGlobalDeclaration():
            self._reportSchemaError(
                f"inline complexType must not carry a name ('{name}')",
                code="declaration-attribute",
            )

    def effectiveMixed(self, _seen: set[int] | None = None) -> bool:
        """The type's effective ``mixed`` value (XSD 1.1 §3.4.2.3.3).

        Clause 1: the ``mixed`` attribute on ``complexContent``, when
        present, wins over the one on ``complexType``; absent both, the
        value is false (``mixed`` is an ``xs:boolean``: ``1``/``true``
        are true, anything else is false). Clause 4.2.2: an extension
        whose explicit content is empty takes the *base's* content type,
        so a mixed base keeps its character-data allowance through an
        attribute-only or bare extension. ``_seen`` guards a derivation
        cycle (itself an invalid schema).
        """
        value = self._ownMixed()
        if value or self.getDerivation() != "extension":
            return value
        if not self._explicitContentEmpty():
            return value
        base = self._baseComplexType(_seen)
        if base is None:
            return False
        return base.effectiveMixed((_seen or set()) | {id(self)})

    def _ownMixed(self) -> bool:
        """The ``mixed`` value written on this type (clause 1)."""
        for child in self.processedChildren or ():
            if child is not None and type(child).__name__ == "ComplexContent":
                value = (getattr(child, "tagAttributes", {}) or {}).get("mixed")
                if value is not None:
                    return _isTrue(value)
                break
        value = (getattr(self, "tagAttributes", {}) or {}).get("mixed")
        return value is not None and _isTrue(value)

    def _explicitContentEmpty(self) -> bool:
        """Whether the type's explicit content is empty (clause 2).

        The particle children live on ``extension``/``restriction`` when
        a ``complexContent`` wrapper is present, otherwise on the type.
        Clause 2.1: no model group at all, an empty ``all``/``sequence``,
        an empty ``choice`` with ``minOccurs=0``, or any model group
        with ``maxOccurs=0`` is empty explicit content. A ``group``
        reference is always a particle (its referenced content is not
        inlined here).
        """
        holder = self
        complex_content = self._firstProcessedChild(self, "ComplexContent")
        if complex_content is not None:
            holder = (
                self._firstProcessedChild(complex_content, "Extension")
                or self._firstProcessedChild(complex_content, "Restriction")
                or complex_content
            )
        particles = [
            child
            for child in getattr(holder, "processedChildren", None) or ()
            if child is not None and type(child).__name__ in _MODEL_GROUP_KINDS
        ]
        if not particles:
            return True
        particle = particles[0]
        # Silent occurrence reads: a garbage value is reported once on
        # the owning declaration, not here.
        if particle._silentOccurs("maxOccurs") == 0:
            return True
        kind = type(particle).__name__
        if kind == "Group":
            return False
        members = [
            child
            for child in getattr(particle, "processedChildren", None) or ()
            if child is not None and type(child).__name__ in _PARTICLE_KINDS
        ]
        if members:
            return False
        if kind in ("All", "Sequence"):
            return True
        return particle._silentOccurs("minOccurs") == 0

    def defaultAttributesApplies(self) -> bool:
        """Whether the schema document's default attribute group applies.

        XSD 1.1 §3.1.2 / §3.4.2: a schema-level ``defaultAttributes``
        supplies extra attribute uses to every complex type definition in
        the same schema document unless that type sets
        ``defaultAttributesApply="false"`` (the default is ``true``).
        """
        value = self.tagAttributes.get("defaultAttributesApply")
        return value is None or _isTrue(value)

    def acceptsDefaultOpenContent(self, *, appliesToEmpty: bool) -> bool:
        """Whether a schema-level default open content may attach to this type.

        XSD 1.1 §3.4.2.4: a schema's ``defaultOpenContent`` supplies the
        open content of every complex type declared in the same schema
        document that has no explicit ``xs:openContent``; the default
        attaches only when the type's explicit content type is not empty,
        or -- for an empty content type -- when ``appliesToEmpty`` is
        true. A type carrying its own ``xs:openContent`` is never touched.

        The effective content type is used, not the type's own particle:
        an extension whose own content is empty but whose base carries
        element-only/mixed content reuses the base content type
        (§3.4.2.3.3 clause 4.2.2), so it is *not* empty and the default
        applies (Saxon bug 13459, ``open046``).
        """
        if self.openContent is not None:
            return False
        return appliesToEmpty or self._effectiveContentVariety() != "empty"

    def _explicitContentIsEmpty(self) -> bool:
        """Whether the explicit content type has {variety} ``empty``.

        Unlike :meth:`_explicitContentEmpty`, a ``simpleContent`` type has
        {variety} ``simple`` (not ``empty``), so it is *not* empty here.
        """
        for child in self.processedChildren or ():
            if child is not None and type(child).__name__ == "SimpleContent":
                return False
        return self._explicitContentEmpty()

    def _effectiveContentVariety(self, _seen: set[int] | None = None) -> str:
        """The {content type}.{variety} of this type (extension-aware).

        ``empty``, ``simple``, ``element-only`` or ``mixed``. An extension
        with empty explicit content over a base whose final content type
        is element-only/mixed reuses the base content type (§3.4.2.3.3
        clause 4.2.2), so it takes the base's variety rather than reading
        empty. ``_seen`` guards a derivation cycle.
        """
        seen = _seen or set()
        if id(self) in seen:
            return "empty"
        seen = seen | {id(self)}
        if self._firstProcessedChild(self, "SimpleContent") is not None:
            return "simple"
        if self.getDerivation() == "extension" and self._explicitContentEmpty():
            base = self._baseComplexType(seen)
            if base is not None:
                base_variety = base._effectiveContentVariety(seen)
                if base_variety in ("element-only", "mixed"):
                    return base_variety
        if self.effectiveMixed(_seen):
            return "mixed"
        if self._explicitContentEmpty():
            return "empty"
        return "element-only"

    def effectiveOpenContent(self, _seen: set[int] | None = None):
        """The type's effective {open content}, or ``None`` (XSD 1.1 §3.4.2.3.3).

        ``self.openContent`` is the parsed ``<openContent>`` wildcard
        element (explicit, or the schema default the parser attached). An
        extension's explicit content type already carries its base's open
        content, so an explicit (or default) wildcard element combines
        with it by namespace union (clause 6.2); with no wildcard element
        the base's open content is inherited. A restriction's explicit
        content type never carries the base's open content, so only the
        wildcard element (if any) applies. Simple content has no open
        content. ``_seen`` guards a derivation cycle.
        """
        from pyxsd.open_content import OpenContent, combine_open_content

        seen = _seen or set()
        if id(self) in seen:
            return None
        seen = seen | {id(self)}
        if self._firstProcessedChild(self, "SimpleContent") is not None:
            return None
        explicit = None
        if self.getDerivation() == "extension":
            base = self._baseComplexType(seen)
            if base is not None and base._effectiveContentVariety(seen) in (
                "element-only",
                "mixed",
            ):
                explicit = base.effectiveOpenContent(seen)
        own = self.openContent
        if own is None:
            return explicit
        if own.mode == "none":
            return explicit
        combined = combine_open_content(explicit, own, self.getNamespace())
        if combined is None and own.wildcard is not None:
            return OpenContent(own.mode, copy.copy(own.wildcard))
        return combined

    def _baseComplexType(self, _seen: set[int] | None = None) -> "ComplexType | None":
        """The first base type that is a complex type, or ``None``."""
        for name in getattr(self, "superClassNames", None) or ():
            _, representative = self.varietyOfReference(name)
            if representative is None or type(representative).__name__ != "ComplexType":
                continue
            if _seen and id(representative) in _seen:
                return None
            return representative
        return None

    def getElements(self):
        """Returns a list of elements.

        Uses lazy evaluation.  Goes through the ``sequencesOrChoices``
        list and each one's elements, adds its container information,
        then adds the element to a list which it returns.

        Group reference sites (direct children of the type or nested
        inside a compositor) are flattened into their named group's
        content model first (see ``_flattenGroupRef``). For ``all``
        compositors, an XSD 1.0 violation (an element with
        ``maxOccurs`` greater than one) is logged.
        """
        elements = getattr(self, "elements_", None)

        if elements is not None:
            return elements

        self.elements_ = []
        for item in self.sequencesOrChoices:
            if getattr(item, "isRefSite", False):
                self.elements_.extend(self._flattenGroupRef(item, frozenset()))
                continue
            try:
                itemInfo = Compositor(item.tagType)
            except ValueError:
                itemInfo = None
            for element in item.elements:
                if getattr(element, "isRefSite", False):
                    self.elements_.extend(self._flattenGroupRef(element, frozenset()))
                    continue
                if getattr(element, "isElementRef", False):
                    self._resolveElementRef(element)
                element.sOrC = itemInfo
                self.elements_.append(element)
            if itemInfo is Compositor.ALL:
                for element in item.elements:
                    if not getattr(element, "isRefSite", False) and element.getMaxOccurs() > 1:
                        logger.warning(
                            "element '%s' in the 'all' compositor of %s has "
                            "maxOccurs=%r; XSD 1.0 only allows at most one "
                            "occurrence inside 'all'",
                            element.name,
                            self.name,
                            getattr(element, "maxOccurs", 1),
                        )
        return self.elements_

    def _flattenGroupRef(self, refSite, visited):
        """Returns the element list a group reference stands for.

        Resolves the reference site against the schema's ``groups``
        dictionary and collects the referenced group's compositor
        elements, recursing through nested group references with a
        visited set to reject circular references. Unresolvable or
        circular references are recorded on the validation report and
        yield an empty contribution.

        When the reference site specifies ``minOccurs``/``maxOccurs``,
        the occurrence limits are folded onto each contributed element
        (see ``_foldRefOccurrences``). Each reference works on its own
        copies of the group's element representatives, so a reference's
        limits and compositor information never leak into the shared
        declaration or into other references to the same group.
        """
        group = refSite.resolveReference(refSite.ref, self.getSchema().groups.values())
        groupName = refSite.ref.split(":", 1)[-1]
        groupKey = getattr(group, "expandedName", None) or groupName
        if groupKey in visited:
            message = (
                f"circular group reference chain involving '{groupName}' "
                f"(reached from '{self.name}')"
            )
            self._report_ref_error(message, code="circular-group")
            return []

        if group is None:
            message = f"group reference '{refSite.ref}' in type '{self.name}' could not be resolved"
            self._report_ref_error(message, code="unknown-group")
            return []

        compositor = group.getCompositor()
        if compositor is None:
            message = f"group '{groupName}' has no content model to contribute"
            self._report_ref_error(message, code="unknown-group")
            return []

        try:
            compInfo = Compositor(compositor.tagType)
        except ValueError:
            compInfo = None

        for spec in getattr(group, "wildcardElementSpecs", ()):
            register_wildcard(self, spec)
        for spec in getattr(group, "wildcardAttributeSpecs", ()):
            register_wildcard(self, spec)

        contributed = self._compositorElements(compositor, compInfo, visited, groupKey)
        self._foldRefOccurrences(refSite, contributed)
        return contributed

    def _compositorElements(self, compositor, compInfo, visited, groupKey):
        """Returns the elements a group compositor contributes, in order.

        Walks the compositor's own children, descending nested
        compositors (which never appear in ``compositor.elements``, as
        that list holds only the immediate element and group-reference
        children) and recursing through group references. Each element
        is a per-use shallow copy carrying the compositor it sits in as
        its ``sOrC``; ``visited``/``groupKey`` guard reference cycles
        exactly as ``_flattenGroupRef`` does.
        """
        contributed = []
        for child in getattr(compositor, "processedChildren", None) or ():
            if child is None:
                continue
            kind = type(child).__name__
            if kind == "Element":
                # The group's element representatives are shared by every
                # complex type that references the group. Each reference gets
                # its own shallow copy, so folding this reference's occurrence
                # limits (and resolving element refs) never mutates the
                # shared declaration.
                use = copy.copy(child)
                if getattr(use, "isElementRef", False):
                    # ``<xs:element ref="..."/>`` inside a named group must
                    # resolve to its global declaration exactly as it would
                    # directly inside the type.
                    self._resolveElementRef(use)
                use.sOrC = compInfo
                contributed.append(use)
            elif kind == "Group" and getattr(child, "isRefSite", False):
                contributed.extend(self._flattenGroupRef(child, visited | {groupKey}))
            elif kind in ("Sequence", "Choice", "All"):
                try:
                    nestedInfo = Compositor(child.tagType)
                except ValueError:
                    nestedInfo = None
                contributed.extend(self._compositorElements(child, nestedInfo, visited, groupKey))
        return contributed

    def _resolveElementRef(self, refSite):
        """Resolves an element reference site to its global declaration.

        The referenced global element declaration supplies the content
        model and the value constraints (type, nillable, fixed,
        default, abstract); the reference site keeps its own
        occurrence limits. The site's ``name`` becomes the referred
        element's name so instance matching works, and the site's
        ``referredElement`` attribute records the declaration for
        ``Element.getType`` and the value accessors.

        Unresolvable references are recorded on the validation report
        and leave the site nameless (matching then fails with the
        usual unknown-element handling).
        """
        candidate = refSite.resolveReference(refSite.ref, self.getSchema().elements)
        if candidate is not None:
            refSite.referredElement = candidate
            refSite.name = candidate.name
            # Identity constraints declared on the global element
            # apply wherever the element is referenced.
            refSite.identities = list(candidate.identities)
            return None
        message = f"element reference '{refSite.ref}' in type '{self.name}' could not be resolved"
        self._report_ref_error(message, code="unknown-elementRef")
        return None

    def _foldRefOccurrences(self, refSite, elements):
        """Folds a group reference site's occurrence limits onto the
        elements the reference contributes.

        Each element's effective occurrence becomes the reference's
        limit multiplied by the element's own limit (capped like the
        rest of the parser's unbounded handling at 99999).
        """
        if "minOccurs" in refSite.tagAttributes:
            refMin = refSite.getMinOccurs()
            for element in elements:
                element.minOccurs = str(refMin * element.getMinOccurs())
        if "maxOccurs" in refSite.tagAttributes:
            refMax = refSite.getMaxOccurs()
            for element in elements:
                folded = min(99999, refMax * element.getMaxOccurs())
                element.maxOccurs = "unbounded" if folded >= 99999 else str(folded)
