"""XSD 1.1 ``xs:alternative`` type alternatives (conditional type assignment).

An element declaration may carry ``xs:alternative`` children that
associate an XPath test with a type.  The alternatives are evaluated in
declaration order at instance phase, and the first whose test is true
supplies the element's governing type (XSD 1.1 §3.3.4.1, §3.12).  This
module owns the schema-phase half:

* :class:`Alternative`, one compiled type alternative (its ``test``, the
  parsed expression, the declaration-site namespace bindings and the
  resolved type);
* :class:`AlternativeER`, the element representative for the
  ``xs:alternative`` tag, so the declaration walk can see it and collect
  it on the owning element in declaration order;
* :func:`compile_alternatives`, the schema-phase step that parses every
  ``test`` with the CTA XPath subset and reports ``alternative-invalid``;
* :func:`check_element_alternatives`, the pass that runs once generated
  classes exist: it resolves each alternative's type and enforces the
  XSD 1.1 conditional-type-assignment legality rule that each type must
  be validly derived from the element's declared type (``xs:error`` and
  the ``xs:anyType`` ur-type are exempt, XSD 1.1 §3.3.6.1 clause 7).

There is deliberately no "required derivation ordering": a later
alternative's type may be validly derived from an earlier one's.  XSD 1.1
§3.3.2.1/§3.12 impose no such rule, and the corpus
(IBM ``S3_12/s3_12v08``) declares exactly that broad-then-narrow shape
as valid.

The instance-phase selection (first true test's type governs, with
``xsi:type`` taking precedence) is layered on top of the ordered
:class:`Alternative` list this module collects.
"""

from __future__ import annotations

from typing import Any

from pyxsd.derivation import is_validly_derived
from pyxsd.element_representatives.element_representative import ElementRepresentative
from pyxsd.element_representatives.identity import _DeclarationSite
from pyxsd.namespaces import XSD_NS, clark, local_name, namespace_of
from pyxsd.xpath_assertions import CompiledXPath, parse_cta_xpath
from pyxsd.xpath_subset import XPathError

__all__ = [
    "Alternative",
    "AlternativeER",
    "check_element_alternatives",
    "compile_alternatives",
]

#: Sentinel marking an ``AlternativeER`` whose ``test`` has not been
#: compiled yet (a test-free alternative legitimately compiles to ``None``).
_UNSET = object()

#: The ``xs:error`` type definition, which an alternative may name even
#: though it is not derived from the declared type (XSD 1.1 §3.3.6.1
#: clause 7.2).
_XSD_ERROR = clark(XSD_NS, "error")


class Alternative:
    """A compiled ``xs:alternative`` (a type alternative component).

    Attributes are plain slots so the instance phase can read the
    ordered table off the element (``Element.compiledAlternatives``)
    without re-parsing anything.
    """

    __slots__ = (
        "compiled",
        "er",
        "inline_type",
        "is_error",
        "namespaces",
        "resolved_name",
        "test",
        "type_class",
        "type_name",
        "xpath_default_namespace",
    )

    def __init__(
        self,
        *,
        er: Any,
        test: str | None,
        compiled: CompiledXPath | None,
        namespaces: dict[str, str],
        xpath_default_namespace: str | None,
        type_name: str | None,
        inline_type: Any,
    ) -> None:
        self.er = er
        self.test = test
        self.compiled = compiled
        self.namespaces = namespaces
        self.xpath_default_namespace = xpath_default_namespace
        self.type_name = type_name
        self.inline_type = inline_type
        #: The type reference resolved to a Clark name (filled by
        #: :func:`check_element_alternatives`).
        self.resolved_name: str | None = None
        #: The Python class standing for the alternative's type.
        self.type_class: type | None = None
        #: Whether the type is ``xs:error`` (legal, always admissible).
        self.is_error: bool = False


class AlternativeER(_DeclarationSite, ElementRepresentative):
    """The element representative for the ``xs:alternative`` tag.

    ``xs:alternative`` appears inside an element declaration, after the
    inline type (if any) and before the identity constraints.  It carries
    an optional XPath 2.0 ``test`` and exactly one of a ``type``
    attribute or an inline ``simpleType``/``complexType``.  The
    ``_DeclarationSite`` mixin resolves the declaration-site prefix
    bindings and ``xpathDefaultNamespace`` for the test.
    """

    #: Only an annotation and (at most) one inline type may appear.
    _ALLOWED_CHILDREN = ("annotation", "simpleType", "complexType")
    _MAX_ONE_CHILDREN = ("annotation", "simpleType", "complexType")
    _CHILD_ORDER = (("annotation",), ("simpleType", "complexType"))
    #: The inline type slot holds mutually exclusive alternatives.
    _ONE_OF_SLOTS = frozenset({1})

    #: Unqualified attributes the XML representation allows; foreign
    #: namespace attributes are always admissible.
    _ALLOWED_ATTRIBUTES = ("id", "test", "type", "xpathDefaultNamespace")

    def __init__(self, xsdElement, parent) -> None:
        self.test = xsdElement.get("test")
        self.type_name = xsdElement.get("type")
        # Sentinel: a compiled alternative may legitimately hold a
        # ``None`` expression (a test-free default alternative).
        self._compiled: Any = _UNSET
        super().__init__(xsdElement, parent)
        # The inline type (if any) is built on demand: the derived-type
        # check and the instance-phase selection need it.  Building every
        # alternative's inline type eagerly in the global class-building
        # loop would surface an inline type whose base is not yet
        # implemented as a schema error even when the element declares
        # the ur-type and the alternative is never selected.
        inline_type = self._inline_type()
        if inline_type is not None:
            inline_type._alternativeInline = True
        # Record on the owning element in declaration order.  The element
        # allocates ``alternatives`` before its children are factored.
        container = self.parent
        alternatives = getattr(container, "alternatives", None)
        if alternatives is not None:
            alternatives.append(self)

    def getName(self) -> str:
        """Returns a bookkeeping name for the alternative."""
        return f"{self.getContainingTypeName()}|alternative"

    def _inline_type(self) -> Any:
        """Returns the alternative's inline type representative, if any."""
        for child in getattr(self, "processedChildren", None) or ():
            if child is not None and type(child).__name__ in ("SimpleType", "ComplexType"):
                return child
        return None

    def checkDeclarationLegality(self) -> None:
        """Reports the alternative's XML-representation constraints.

        Covers the allowed attribute set, the requirement that exactly
        one of a ``type`` attribute / inline type is present (XSD 1.1
        §3.12.3), and the compilation of the ``test`` (an out-of-subset
        or statically invalid expression is ``alternative-invalid``).
        """
        allowed = frozenset(self._ALLOWED_ATTRIBUTES)
        for raw in self.xsdElement.attrib:
            if raw.startswith("{"):
                continue
            if raw not in allowed:
                self._reportSchemaError(
                    f"<alternative> does not allow the '{raw}' attribute",
                    code="declaration-attribute",
                )
        inline = self._inline_type()
        supplied = (1 if self.type_name is not None else 0) + (1 if inline is not None else 0)
        if supplied != 1:
            self._reportSchemaError(
                "<alternative> must carry exactly one of a 'type' attribute, "
                "a simpleType child or a complexType child",
                code="alternative-invalid",
            )
        self.compile()

    def compile(self) -> Alternative | None:
        """Parses the ``test`` once, reporting ``alternative-invalid``.

        Returns the compiled :class:`Alternative`; idempotent, because the
        declaration sweep and the derivation pass may both consult it.
        A test-free alternative is legal (it becomes the default) and
        compiles to an :class:`Alternative` with ``compiled=None``.  An
        empty or unusable test still returns an ``Alternative`` (so the
        type can be checked) after reporting, so the single report is not
        lost.
        """
        if self._compiled is not _UNSET:
            return self._compiled
        namespaces: dict[str, str] = {}
        default_namespace: str | None = None
        compiled: CompiledXPath | None = None
        if self.test is not None:
            text = self.test.strip()
            try:
                namespaces = dict(self._declarationNamespaces() or {})
                default_namespace = self._xpathDefaultNamespace()
                compiled = parse_cta_xpath(
                    text,
                    namespaces,
                    default_namespace=default_namespace,
                )
            except XPathError as exc:
                self._reportSchemaError(
                    f"<alternative> test {text!r} is outside the "
                    f"conditional-type-assignment XPath subset: {exc}",
                    code="alternative-invalid",
                )
        else:
            try:
                namespaces = dict(self._declarationNamespaces() or {})
                default_namespace = self._xpathDefaultNamespace()
            except XPathError:
                namespaces = {}
                default_namespace = None
        self._compiled = Alternative(
            er=self,
            test=self.test,
            compiled=compiled,
            namespaces=namespaces,
            xpath_default_namespace=default_namespace,
            type_name=self.type_name,
            inline_type=self._inline_type(),
        )
        return self._compiled

    def resolveTypeClass(self, pyXSD: Any) -> type | None:
        """Resolves the alternative's type to a Python class.

        An inline type builds its class directly; a ``type`` attribute
        resolves through the declaration-site namespace context.  A
        reference to ``xs:error`` sets :attr:`Alternative.is_error` and
        returns ``None``.  An unresolved reference returns ``None``.
        """
        if self._compiled is _UNSET:
            self.compile()
        alternative: Alternative = self._compiled
        inline = alternative.inline_type
        if inline is not None and alternative.type_name is not None:
            # Ambiguous: both a type reference and an inline type are
            # present.  The representation check already reported it;
            # deriving from either would add a misleading second issue.
            return None
        if inline is not None:
            try:
                alternative.type_class = inline.clsFor(pyXSD)
            except Exception:
                alternative.type_class = None
            return alternative.type_class
        raw = alternative.type_name
        if raw is None:
            return None
        resolved = self.resolveSchemaQName(raw, parser=pyXSD)
        alternative.resolved_name = resolved
        if _is_error_name(resolved):
            alternative.is_error = True
            return None
        alternative.type_class = ElementRepresentative.typeFromName(resolved, pyXSD)
        return alternative.type_class


def _is_error_name(resolved: Any) -> bool:
    """Whether *resolved* names ``xs:error`` (Clark or lexical form)."""
    if not isinstance(resolved, str):
        return False
    if namespace_of(resolved) == XSD_NS and local_name(resolved) == "error":
        return True
    return resolved in ("xs:error", "xsd:error")


def compile_alternatives(element: Any) -> list[Alternative]:
    """Compiles every alternative of *element* in declaration order.

    Returns the compiled alternatives (a test-free default alternative is
    included).  Idempotent per element: the declaration sweep and the
    derivation pass share one compile, so an unusable test is reported
    once.  Never raises.
    """
    cached = getattr(element, "compiledAlternatives", None)
    if cached is not None:
        return cached
    compiled: list[Alternative] = []
    for representative in getattr(element, "alternatives", None) or ():
        result = representative.compile()
        if result is not None:
            compiled.append(result)
    element.compiledAlternatives = compiled
    return compiled


def _is_ur_type(declared: Any) -> bool:
    """Whether *declared* is ``xs:anyType`` (or its stand-in)."""
    from pyxsd import xsd_data_types
    from pyxsd.schema_base import SchemaBase

    return declared is SchemaBase or declared is xsd_data_types.AnyType


def check_element_alternatives(element: Any) -> None:
    """Enforces the CTA legality rules for *element*'s alternatives.

    Runs after generated classes exist.  For every alternative whose type
    resolves, the type must be validly derived from the element's
    declared type (``xs:error`` and the ``xs:anyType`` ur-type are
    exempt, XSD 1.1 §3.3.6.1 clause 7).  An unresolvable type is reported
    ``alternative-invalid``.

    Note: a later alternative's type *may* be validly derived from an
    earlier alternative's type.  The corpus (IBM ``S3_12/s3_12v08``)
    declares a broad first alternative followed by narrower ones, which
    is legal; there is no "required derivation ordering" rule in XSD 1.1
    §3.3.2.1/§3.12, so none is enforced.
    """
    alternatives = getattr(element, "compiledAlternatives", None)
    if not alternatives:
        return
    try:
        declared = element.getType()
    except Exception:
        declared = None
    if declared is None or _is_ur_type(declared):
        # The declared type is the ur-type: every type is validly derived
        # from it, so there is nothing to enforce.  Resolving the
        # alternatives here would force their inline types to build,
        # surfacing unrelated gaps (for example a not-yet-implemented
        # built-in base) as schema errors; skip it.
        return
    pyXSD = getattr(element, "pyXSD", None) or getattr(element.getSchema(), "pyXSD", None)
    if pyXSD is None:
        return

    for alternative in alternatives:
        alternative.er.resolveTypeClass(pyXSD)
        if alternative.is_error:
            continue
        type_class = alternative.type_class
        if type_class is None:
            # An inline type build failure has already been reported by
            # the class builder, and a missing type by the representation
            # check.  Only an unresolved *named* type is reported here so
            # the alternative does not silently pass.
            if alternative.type_name is not None and alternative.inline_type is None:
                element._reportSchemaError(
                    f"the type '{alternative.type_name}' of a type alternative "
                    f"of element '{element.name}' could not be resolved",
                    code="alternative-invalid",
                )
            continue
        reason = is_validly_derived(type_class, declared)
        if reason is not None:
            element._reportSchemaError(
                f"the type '{alternative.type_name or _inline_label(alternative)}' "
                f"of a type alternative of element '{element.name}' is not "
                f"validly derived from the declared type "
                f"'{_class_label(declared)}'",
                code="alternative-invalid",
            )


def _inline_label(alternative: Alternative) -> str:
    """A readable label for an inline-typed alternative."""
    return "<inline type>"


def _class_label(cls: Any) -> str:
    """A readable label for a resolved type class."""
    name = cls.__dict__.get("name")
    if isinstance(name, str) and name:
        return name
    return getattr(cls, "__name__", str(cls))
