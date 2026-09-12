import copy
import logging

from pyxsd.compositors import Compositor
from pyxsd.element_representatives.xsd_type import XsdType
from pyxsd.wildcards import register_wildcard

logger = logging.getLogger(__name__)


class ComplexType(XsdType):
    """The class for the complexType tag."""

    def __init__(self, xsdElement, parent):
        """Keeps a list of sequences, choices, and alls that are
        children of it.  Stores itself in the schema dictionary of
        complexTypes.  Uses the XsdType ``__init__``.  See
        ElementRepresentative for more documentation.
        """
        self.sequencesOrChoices = []
        super().__init__(xsdElement, parent)
        self.getSchema().complexTypes[self.name] = self

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

        contributed = []
        for element in compositor.elements:
            if getattr(element, "isRefSite", False):
                contributed.extend(self._flattenGroupRef(element, visited | {groupKey}))
                continue
            # The group's element representatives are shared by every
            # type that references the group. Each reference gets its
            # own shallow copy, so folding this reference's occurrence
            # limits (and resolving element refs) never mutates the
            # shared declaration.
            use = copy.copy(element)
            if getattr(use, "isElementRef", False):
                # ``<xs:element ref="..."/>`` inside a named group must
                # resolve to its global declaration exactly as it would
                # directly inside the type.
                self._resolveElementRef(use)
            use.sOrC = compInfo
            contributed.append(use)
        self._foldRefOccurrences(refSite, contributed)
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
            refMin = int(refSite.tagAttributes["minOccurs"])
            for element in elements:
                element.minOccurs = str(refMin * element.getMinOccurs())
        if "maxOccurs" in refSite.tagAttributes:
            refMax = getattr(refSite, "maxOccurs", 1)
            refMax = 99999 if refMax == "unbounded" else int(refMax)
            for element in elements:
                folded = min(99999, refMax * element.getMaxOccurs())
                element.maxOccurs = "unbounded" if folded >= 99999 else str(folded)
