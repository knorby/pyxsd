import logging

from pyxsd.compositors import Compositor
from pyxsd.element_representatives.xsd_type import XsdType

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
        (see ``_foldRefOccurrences``). Note that this mutates the
        shared element ERs, so a group referenced multiple times with
        conflicting occurrence limits resolves with the last
        reference's limits.
        """
        groupName = refSite.ref
        if groupName in visited:
            message = (
                f"circular group reference chain involving '{groupName}' "
                f"(reached from '{self.name}')"
            )
            self._report_ref_error(message, code="circular-group")
            return []

        group = self.getSchema().groups.get(groupName)
        if group is None:
            message = f"group reference '{groupName}' in type '{self.name}' could not be resolved"
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

        if not getattr(self, "hasWildcardElements", False) and getattr(
            group, "hasWildcardElements", False
        ):
            self.hasWildcardElements = True
        if not getattr(self, "hasWildcardAttributes", False) and getattr(
            group, "hasWildcardAttributes", False
        ):
            self.hasWildcardAttributes = True

        contributed = []
        for element in compositor.elements:
            if getattr(element, "isRefSite", False):
                contributed.extend(self._flattenGroupRef(element, visited | {groupName}))
            else:
                element.sOrC = compInfo
                contributed.append(element)
        self._foldRefOccurrences(refSite, contributed)
        return contributed

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
