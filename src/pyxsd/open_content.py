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

import copy
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

#: The XSD 1.1 ``mode`` vocabulary of ``xs:defaultOpenContent`` (no ``none``:
#: a default that admits nothing is spelled by simply omitting the element).
DEFAULT_OPEN_CONTENT_MODES = frozenset({"interleave", "suffix"})

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


@dataclass(frozen=True)
class DefaultOpenContent:
    """The parsed ``xs:defaultOpenContent`` component of one schema document.

    ``applies_to_empty`` is the effective ``appliesToEmpty`` value: the
    default open content attaches to a complex type with empty content
    only when it is true.
    """

    mode: str
    wildcard: WildcardSpec
    applies_to_empty: bool = False

    def component(self) -> OpenContent:
        """Returns a fresh :class:`OpenContent` for attachment to one type.

        A new component is built per attachment (and the wildcard copied),
        so the schema default is never shared as mutable state between the
        types that inherit it.
        """
        return OpenContent(self.mode, copy.copy(self.wildcard))


def namespace_resolver(schema: Any, element: Any):
    """A ``notQName`` QName expander for *element*, or ``None``.

    Prefix bindings are recorded per element by the parsing layer, so a
    resolver is available only once a namespace context is attached.
    """
    context = getattr(schema, "namespaceContext", None)
    if context is None or element is None:
        return None

    def resolve(token):
        return context.resolve(element, token)

    return resolve


def open_content_wildcard_spec(
    element: Any,
    *,
    report_error,
    target_namespace: Any = None,
    resolve_qname: Any = None,
) -> WildcardSpec:
    """Validates and builds the wildcard of an open-content ``xs:any``.

    The shared Area D wildcard legality is used unchanged (namespace
    constraints including the 1.1 ``notNamespace``/``notQName``,
    ``processContents`` and the allowed attribute set), plus the
    open-content-specific rule that the child wildcard is not a particle:
    ``minOccurs``/``maxOccurs`` are not part of its XML representation
    (Saxon ``open048``/bug 15618). A malformed attribute is reported,
    never raised.
    """
    for code, message in wildcard_declaration_problems(
        element.attrib, is_attribute=False, resolve_qname=resolve_qname
    ):
        report_error(message, code)
    for name in ("minOccurs", "maxOccurs"):
        if name in element.attrib:
            report_error(
                f"<any> inside open content must not carry an occurrence attribute '{name}'",
                "wildcard-invalid",
            )
    spec = wildcard_spec(
        element.attrib,
        is_attribute=False,
        target_namespace=target_namespace,
        resolve_qname=resolve_qname,
    )
    for code, message in not_qname_consistency_problems(spec, target_namespace=target_namespace):
        report_error(message, code)
    return spec


def default_open_content_element(schema_element: Any) -> Any:
    """Returns the ``xs:defaultOpenContent`` child of *schema_element*, if any."""
    if schema_element is None:
        return None
    for child in schema_element:
        if namespace_of(child.tag) == XSD_NS and local_name(child.tag) == "defaultOpenContent":
            return child
    return None


def parse_default_open_content(
    element: Any,
    *,
    report_error,
    target_namespace: Any = None,
    resolve_qname: Any = None,
) -> DefaultOpenContent | None:
    """Validates and parses one ``xs:defaultOpenContent`` element.

    Used both by the declaration walk (the main document's element, via
    :class:`DefaultOpenContentER`) and by the parser for a composed
    document's default, whose element is never spliced into the main
    tree. Returns ``None`` when the declaration is structurally unusable
    (an illegal mode or a missing/duplicated wildcard); reported problems
    use ``open-content-invalid`` for the mode/wildcard rules and the
    shared declaration codes for attribute/child grammar.
    """
    allowed = frozenset({"id", "mode", "appliesToEmpty"})
    for raw in element.attrib:
        if raw.startswith("{"):
            continue
        if raw not in allowed:
            report_error(
                f"<defaultOpenContent> does not allow the '{raw}' attribute",
                "declaration-attribute",
            )
    raw_mode = element.get("mode")
    mode = DEFAULT_OPEN_CONTENT_MODE if raw_mode is None else str(raw_mode).strip()
    if mode not in DEFAULT_OPEN_CONTENT_MODES:
        report_error(
            f"<defaultOpenContent> mode '{raw_mode}' is not one of 'interleave', 'suffix'",
            "open-content-invalid",
        )
        return None
    applies = False
    raw_applies = element.get("appliesToEmpty")
    if raw_applies is not None:
        text = str(raw_applies).strip()
        if text in ("true", "1"):
            applies = True
        elif text not in ("false", "0"):
            report_error(
                f"<defaultOpenContent> has an invalid appliesToEmpty value "
                f"'{raw_applies}'; expected true, false, 1 or 0",
                "declaration-attribute",
            )

    schema_children = [child for child in element if namespace_of(child.tag) == XSD_NS]
    tags = [local_name(child.tag) for child in schema_children]
    for child in schema_children:
        tag = local_name(child.tag)
        if tag not in ("annotation", "any"):
            report_error(
                f"<{tag}> is not allowed inside <defaultOpenContent>",
                "declaration-child",
            )
    if "annotation" in tags and tags[0] != "annotation":
        report_error(
            "<annotation> must be the first child of <defaultOpenContent>",
            "declaration-order",
        )
    any_children = [child for child in schema_children if local_name(child.tag) == "any"]
    if len(any_children) != 1:
        report_error(
            "<defaultOpenContent> requires exactly one <any> child",
            "open-content-invalid",
        )
        return None
    spec = open_content_wildcard_spec(
        any_children[0],
        report_error=report_error,
        target_namespace=target_namespace,
        resolve_qname=resolve_qname,
    )
    return DefaultOpenContent(mode, spec, applies)


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
        ``notQName`` and the allowed attribute set, plus the open-content
        rule that the child is a wildcard component, not a particle (no
        occurrence attributes). Returns the registered shape of the spec;
        a malformed attribute is reported, never raised.
        """
        if element is None:
            return None
        return open_content_wildcard_spec(
            element,
            report_error=lambda message, code: self._reportSchemaError(message, code=code),
            target_namespace=self.getNamespace(),
            resolve_qname=self._qnameResolver(element),
        )

    def _qnameResolver(self, element: Any):
        """A ``notQName`` QName expander for *element*, or ``None``."""
        try:
            schema = self.getSchema()
        except AttributeError:
            return None
        return namespace_resolver(schema, element)


class DefaultOpenContentER(ElementRepresentative):
    """The element representative for the ``xs:defaultOpenContent`` tag.

    A schema-level default applies (XSD 1.1 §3.4.2.4) to every complex
    type declared in the same schema document that has no explicit
    ``xs:openContent``, non-empty explicit content, or empty content with
    ``appliesToEmpty="true"``. The parsed component is stored on the
    schema as :class:`DefaultOpenContent`; the parser attaches copies to
    the individual types after the declaration walk (so an explicit
    ``xs:openContent``, parsed on the same walk, wins).
    """

    #: Only an annotation and the single wildcard may appear inside.
    _ALLOWED_CHILDREN = ("annotation", "any")
    _MAX_ONE_CHILDREN = ("annotation", "any")
    _CHILD_ORDER = (("annotation",), ("any",))

    #: Unqualified XML attributes the XML representation allows.
    _ALLOWED_ATTRIBUTES = ("id", "mode", "appliesToEmpty")

    def __init__(self, xsdElement, parent):
        self.anyElement: Any = None
        super().__init__(xsdElement, parent)

    def processChildren(self):
        """Factors an ``annotation`` normally and keeps ``any`` raw.

        As for ``xs:openContent``, the wildcard is deliberately not turned
        into an ``Any`` representative: that constructor would register it
        as a content-model wildcard on the (schema) element.
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
        """Returns a bookkeeping name for the default open-content declaration."""
        return "defaultOpenContent"

    def checkDeclarationLegality(self) -> None:
        """Parses and stores the schema's default open content.

        The schema-level declaration shares the ``openContent`` mode and
        wildcard rules (except that ``mode="none"`` is not part of the
        ``defaultOpenContent`` vocabulary), reported as
        ``open-content-invalid``; an illegal ``appliesToEmpty`` lexical
        value is a ``declaration-attribute``. A structurally valid
        declaration is stored on the schema ER for the parser's
        inheritance pass.
        """
        schema = self.getSchema()
        parsed = parse_default_open_content(
            self.xsdElement,
            report_error=lambda message, code: self._reportSchemaError(message, code=code),
            target_namespace=self.getNamespace(),
            resolve_qname=self._qnameResolver(),
        )
        if schema is not None:
            schema.defaultOpenContent = parsed

    def _qnameResolver(self):
        """A ``notQName`` QName expander for the declaration, or ``None``."""
        try:
            schema = self.getSchema()
        except AttributeError:
            return None
        return namespace_resolver(schema, self.xsdElement)


__all__ = [
    "DEFAULT_OPEN_CONTENT_MODE",
    "DEFAULT_OPEN_CONTENT_MODES",
    "OPEN_CONTENT_MODES",
    "DefaultOpenContent",
    "DefaultOpenContentER",
    "OpenContent",
    "OpenContentER",
    "default_open_content_element",
    "namespace_resolver",
    "open_content_wildcard_spec",
    "parse_default_open_content",
]
