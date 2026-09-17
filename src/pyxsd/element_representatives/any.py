from pyxsd.element_representatives.element_representative import ElementRepresentative
from pyxsd.wildcards import register_wildcard, wildcard_spec


class Any(ElementRepresentative):
    """The class for the ``any`` tag (element wildcard).

    Marks the containing type as having a wildcard content-model
    slot: instance children that the schema does not declare are
    accepted when they satisfy the wildcard's namespace constraint.
    ``processContents`` decides how strongly they are checked:
    ``skip`` binds them generically, ``lax`` validates them when a
    declaration exists and otherwise binds generically, and ``strict``
    requires a declaration (``wildcard-no-declaration`` otherwise).
    """

    #: A wildcard may carry at most one ``annotation`` child; it has no
    #: other legal children in its XML representation.
    _MAX_ONE_CHILDREN = ("annotation",)

    def __init__(self, xsdElement, parent):
        """Flags the containing type with a wildcard element slot and
        records the namespace/processContents constraint. Uses the ER
        ``__init__``.  See ElementRepresentative for more documentation.
        """
        super().__init__(xsdElement, parent)
        self.wildcardSpec = wildcard_spec(
            self.tagAttributes,
            is_attribute=False,
            target_namespace=self.getNamespace(),
        )
        register_wildcard(self.getContainingType(), self.wildcardSpec)

    def getName(self):
        """Makes a name like this- ``ContainingTypeName``|any.  The
        name on this class is used for almost nothing.
        """
        contName = self.getContainingTypeName()
        return f"{contName}|any"

    def checkDeclarationLegality(self):
        """Reports wildcard declaration-legality problems.

        The namespace-constraint token grammar and the
        ``processContents`` value are reported as ``wildcard-invalid``,
        unqualified XML attributes outside the wildcard's allowed set as
        ``invalid-attribute``. ``xs:any`` is a particle, so the
        occurrence-range checks (``invalid-occurs`` for garbage lexical
        values, ``declaration-attribute`` for ``minOccurs`` above
        ``maxOccurs``) are shared with the compositors.
        """
        self._checkWildcardDeclaration(is_attribute=False)
        self._checkParticleOccurs()
