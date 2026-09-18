"""XSD 1.1 ``xs:openContent`` components and schema-phase legality.

The ``xs:openContent`` child of a complex type (or of the
``xs:restriction``/``xs:extension`` inside its complex content) selects
the type's *open content*: a wildcard admitting undeclared children
around (``interleave``) or after (``suffix``) the declared content. This
module supplies:

* :class:`OpenContent`, the parsed component (a ``mode`` plus the
  optional :class:`~pyxsd.wildcards.WildcardSpec`);
* :class:`OpenContentER`, the element representative for the
  ``xs:openContent`` tag, which registers itself in the declaration walk
  and stores the component on the containing complex type;
* the schema-phase legality checks (mode vocabulary, no wildcard under
  ``mode="none"`` and exactly one ``xs:any`` otherwise, and at most one
  ``xs:openContent`` per complex type).

The child wildcard is validated through the shared Area D wildcard
legality, but it is deliberately *not* factored into an ``Any``
representative: that would register the spec as a content-model wildcard
on the containing type and so implement open-content admission here.
Content-model merge and instance enforcement belong to a later task, so
this phase cannot change an instance verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pyxsd.element_representatives.element_representative import ElementRepresentative
from pyxsd.namespaces import XSD_NS, local_name, namespace_of
from pyxsd.wildcards import (
    WildcardSpec,
    not_qname_consistency_problems,
    wildcard_declaration_problems,
    wildcard_spec,
)

#: The XSD 1.1 ``mode`` vocabulary of ``xs:openContent``.
OPEN_CONTENT_MODES = frozenset({"none", "interleave", "suffix"})

#: The ``mode`` value assumed when the attribute is absent (XSD 1.1 §3.4.2.2).
DEFAULT_OPEN_CONTENT_MODE = "interleave"


@dataclass(frozen=True)
class OpenContent:
    """The parsed open-content component of a complex type.

    ``mode`` is ``"none"``, ``"interleave"`` or ``"suffix"``. ``wildcard``
    is the ``xs:any`` constraint for a non-``none`` mode and ``None`` for
    ``none`` (which admits no undeclared content).
    """

    mode: str
    wildcard: WildcardSpec | None


class OpenContentER(ElementRepresentative):
    """The element representative for the ``xs:openContent`` tag.

    It carries the ``mode`` and an optional single ``xs:any`` child. The
    child is validated with the shared wildcard legality but kept out of
    the representative tree, so the open-content wildcard never reaches
    the content-model wildcard registry.
    """

    #: Only an annotation and the single wildcard may appear inside.
    _ALLOWED_CHILDREN = ("annotation", "any")
    _MAX_ONE_CHILDREN = ("annotation", "any")

    #: Unqualified XML attributes the XML representation allows
    #: (foreign-namespace attributes are always admissible).
    _ALLOWED_ATTRIBUTES = ("id", "mode")

    def __init__(self, xsdElement, parent):
        self.anyElement: Any = None
        super().__init__(xsdElement, parent)

    def processChildren(self):
        """Factors an ``annotation`` normally and keeps ``any`` raw.

        ``xs:any`` is not turned into an :class:`~pyxsd.element_representatives.any.Any`
        representative on purpose: that constructor registers the spec as
        a content-model wildcard on the containing type. Here the raw
        element is kept for the schema-phase legality check instead.
        """
        children = list(self.xsdElement)
        if not children:
            return None
        for child in children:
            if not self._acceptChild(child):
                self.processedChildren.append(None)
                continue
            if namespace_of(child.tag) == XSD_NS and local_name(child.tag) == "any":
                self.anyElement = child
                self.processedChildren.append(None)
                continue
            self.processedChildren.append(ElementRepresentative.factory(child, self))
        return None

    def getName(self):
        """Returns a bookkeeping name for the open-content declaration."""
        return f"{self.getContainingTypeName()}|openContent"

    def checkDeclarationLegality(self) -> None:
        """Parses and stores the component, reporting ``open-content-invalid``.

        An illegal ``mode`` value, a ``mode="none"`` carrying a wildcard,
        a non-``none`` mode without exactly one ``xs:any``, and a second
        ``xs:openContent`` on the same complex type are all
        ``open-content-invalid``. The child ``xs:any`` grammar is reported
        through the shared wildcard legality (``wildcard-invalid`` /
        ``invalid-attribute``). A structurally valid component is stored
        as ``OpenContent`` on the containing type.
        """
        self._checkAttributeLegality()
        mode = self._mode()
        if mode is None:
            return
        any_count = self.childTags.count("any")
        if not self._checkModeWildcard(mode, any_count):
            return
        spec = self._wildcardSpec(self.anyElement)
        holding = self.getContainingType()
        if holding is None:
            return
        declarations = getattr(holding, "_openContentDeclarations", None)
        if declarations is None:
            declarations = []
            holding._openContentDeclarations = declarations
        declarations.append(self)
        if len(declarations) > 1:
            self._reportSchemaError(
                f"complexType '{holding.name}' may contain at most one <openContent>",
                code="open-content-invalid",
            )
            return
        holding.openContent = OpenContent(mode, spec)

    def _checkAttributeLegality(self) -> None:
        """Reports unqualified XML attributes outside the allowed set."""
        allowed = frozenset(self._ALLOWED_ATTRIBUTES)
        for raw in self.xsdElement.attrib:
            if raw.startswith("{"):
                continue
            if raw not in allowed:
                self._reportSchemaError(
                    f"<openContent> does not allow the '{raw}' attribute",
                    code="declaration-attribute",
                )

    def _mode(self) -> str | None:
        """The effective ``mode``, or ``None`` after reporting an illegal one."""
        raw = self.tagAttributes.get("mode")
        mode = DEFAULT_OPEN_CONTENT_MODE if raw is None else str(raw).strip()
        if mode not in OPEN_CONTENT_MODES:
            self._reportSchemaError(
                f"<openContent> mode '{raw}' is not one of 'none', 'interleave', 'suffix'",
                code="open-content-invalid",
            )
            return None
        return mode

    def _checkModeWildcard(self, mode: str, any_count: int) -> bool:
        """Checks the mode/wildcard pairing; returns structural validity.

        ``mode="none"`` forbids the child wildcard; every other mode
        requires exactly one (XSD 1.1 §3.4.3 Complex Type Definition
        Representation OK).
        """
        if mode == "none":
            if any_count:
                self._reportSchemaError(
                    "<openContent mode='none'> must not contain an <any> child",
                    code="open-content-invalid",
                )
                return False
            return True
        if any_count != 1:
            self._reportSchemaError(
                f"<openContent mode='{mode}'> requires exactly one <any> child",
                code="open-content-invalid",
            )
            return False
        return True

    def _wildcardSpec(self, element: Any) -> WildcardSpec | None:
        """Builds and validates the child wildcard spec, if present.

        The shared Area D legality is used unchanged: namespace-constraint
        tokens (including the 1.1 ``notNamespace``), ``processContents``,
        ``notQName`` and the allowed attribute set. Returns the registered
        shape of the spec; a malformed attribute is reported, never raised.
        """
        if element is None:
            return None
        resolver = self._qnameResolver(element)
        for code, message in wildcard_declaration_problems(
            element.attrib, is_attribute=False, resolve_qname=resolver
        ):
            self._reportSchemaError(message, code=code)
        spec = wildcard_spec(
            element.attrib,
            is_attribute=False,
            target_namespace=self.getNamespace(),
            resolve_qname=resolver,
        )
        for code, message in not_qname_consistency_problems(
            spec, target_namespace=self.getNamespace()
        ):
            self._reportSchemaError(message, code=code)
        return spec

    def _qnameResolver(self, element: Any):
        """A ``notQName`` QName expander for *element*, or ``None``."""
        try:
            schema = self.getSchema()
        except AttributeError:
            return None
        context = getattr(schema, "namespaceContext", None)
        if context is None:
            return None

        def resolve(token):
            return context.resolve(element, token)

        return resolve


__all__ = [
    "DEFAULT_OPEN_CONTENT_MODE",
    "OPEN_CONTENT_MODES",
    "OpenContent",
    "OpenContentER",
]
