"""XSD 1.1 ``xs:assert`` assertions on complex types.

An ``xs:assert`` carries an XPath 2.0 ``test`` that must hold for the
element it governs (:mod:`pyxsd.xpath_assertions` parses and evaluates
the subset). This module supplies:

* :class:`Assert`, the element representative for the ``xs:assert`` tag,
  so the declaration walk can see it (it is otherwise ignored);
* :func:`compile_assertions`, the schema-phase step that parses every
  ``test`` for a complex type and reports ``assert-invalid`` when the
  expression is unusable;
* :func:`check_element_assertions`, the instance-phase hook that runs
  the governing type's assertions — including the base types' — against
  a bound element and reports ``assert-failed`` on a false result or a
  dynamic evaluation error.

Assertions are inherited: a type's effective assertion set is the union
of its own and its base types' (XSD 1.1 §3.4.2.2; the IBM ``test10`` and
``test18`` corpus cases turn on this). The bind-time hook walks the
generated class MRO and runs every class's own assertion list, so no
separate inheritance table is needed.
"""

from __future__ import annotations

from typing import Any, NamedTuple, cast

from elementpath.decoder import get_atomic_sequence
from elementpath.exceptions import ElementPathError

from pyxsd.element_representatives.element_representative import (
    _PRIMITIVE_TYPES,
    ElementRepresentative,
)
from pyxsd.element_representatives.identity import _DeclarationSite
from pyxsd.namespaces import XSD_NS, clark, local_name
from pyxsd.xpath_assertions import (
    CompiledXPath,
    assertion_requires_context,
    evaluate,
    parse_assertion_xpath,
)
from pyxsd.xpath_subset import XPathError

#: The simple types that carry no usable value space for comparison;
#: an attribute declared as one of these stays untyped in the XPath data
#: model.
_NON_ATOMIC_BUILTINS = frozenset({"anyType", "anySimpleType", "anyAtomicType"})

#: Sentinel marking an ``AssertionFacet`` whose ``test`` has not been
#: compiled yet (its compiled form may legitimately be ``None``).
_UNSET = object()


class SimpleAssertionError(TypeError):
    """Raised when an ``xs:assertion`` facet is not satisfied.

    The ``code`` attribute lets the binding paths report the failure as
    ``assert-failed`` rather than a generic invalid-value error. It is a
    ``TypeError`` so a union member whose assertion fails is treated as
    not matching and the next member is tried (XSD 1.1 Part 2).
    """

    code = "assert-failed"


class Assertion(NamedTuple):
    """A compiled ``xs:assert`` or ``xs:assertion`` on a type."""

    #: The raw ``test`` attribute value (stripped).
    test: str
    #: The parsed, subset-validated expression.
    compiled: CompiledXPath
    #: The declaration site's in-scope prefix bindings.
    namespaces: dict[str, str]
    #: The effective ``xpathDefaultNamespace`` (``None`` for ``##local``).
    xpath_default_namespace: str | None


class SimpleAssertion(NamedTuple):
    """A compiled ``xs:assertion`` facet on a simple type."""

    #: The raw ``test`` attribute value (stripped).
    test: str
    #: The parsed, subset-validated expression.
    compiled: CompiledXPath
    #: The declaration site's in-scope prefix bindings.
    namespaces: dict[str, str]
    #: The effective ``xpathDefaultNamespace`` (``None`` for ``##local``).
    xpath_default_namespace: str | None
    #: Whether the test reads the XPath focus, which a simple-type
    #: assertion does not define; such a test is a dynamic error.
    requires_context: bool


class Assert(_DeclarationSite, ElementRepresentative):
    """The element representative for the ``xs:assert`` tag.

    ``xs:assert`` appears directly inside ``xs:complexType`` and inside an
    ``xs:restriction``/``xs:extension`` (of ``complexContent`` or
    ``simpleContent``). It carries the XPath 2.0 ``test`` and the optional
    XSD 1.1 ``xpathDefaultNamespace``; the ``_DeclarationSite`` mixin
    resolves both against the declaration site exactly as the identity
    constraints do.
    """

    #: Only an annotation may appear inside an assertion.
    _ALLOWED_CHILDREN = ("annotation",)
    _MAX_ONE_CHILDREN = ("annotation",)

    #: Unqualified attributes the XML representation allows; foreign
    #: namespace attributes are always admissible.
    _ALLOWED_ATTRIBUTES = ("test", "id", "xpathDefaultNamespace")

    def __init__(self, xsdElement, parent):
        super().__init__(xsdElement, parent)
        self.test = self.xsdElement.get("test")

    def getName(self):
        """Returns a bookkeeping name for the assertion."""
        return f"{self.getContainingTypeName()}|assert"

    def checkDeclarationLegality(self) -> None:
        """Reports illegal attributes on ``xs:assert``.

        A missing or empty ``test`` is not reported here: the
        schema-phase compile turns it into an ``assert-invalid`` issue
        (without a second, duplicate report).
        """
        allowed = frozenset(self._ALLOWED_ATTRIBUTES)
        for raw in self.xsdElement.attrib:
            if raw.startswith("{"):
                continue
            if raw not in allowed:
                self._reportSchemaError(
                    f"<assert> does not allow the '{raw}' attribute",
                    code="declaration-attribute",
                )


class AssertionFacet(_DeclarationSite, ElementRepresentative):
    """The element representative for the ``xs:assertion`` facet tag.

    ``xs:assertion`` is an XSD 1.1 constraining facet on a simple type
    restriction (Part 2); it also appears on a ``simpleContent``
    restriction's inline/direct facets. It records itself on the
    containing type's ``assertions`` list, which the class builder folds
    into the effective facet set (a restriction inherits its base's
    assertions: XSD 1.1 §4.3.15). The ``_DeclarationSite`` mixin resolves
    the declaration-site prefix bindings and ``xpathDefaultNamespace``.
    """

    #: Only an annotation may appear inside an assertion facet.
    _ALLOWED_CHILDREN = ("annotation",)
    _MAX_ONE_CHILDREN = ("annotation",)

    #: Unqualified attributes the XML representation allows.
    _ALLOWED_ATTRIBUTES = ("test", "id", "xpathDefaultNamespace")

    def __init__(self, xsdElement, parent):
        super().__init__(xsdElement, parent)
        self.test = self.xsdElement.get("test")
        # Sentinel: a compiled assertion may legitimately be ``None`` (an
        # unusable test), so presence cannot key off the value.
        self._compiled: Any = _UNSET
        self.getContainingType().assertions.append(self)

    def getName(self):
        """Returns a bookkeeping name for the assertion facet."""
        return f"{self.getContainingTypeName()}|assertion"

    def checkDeclarationLegality(self) -> None:
        """Reports illegal attributes and compiles the ``test``.

        A missing or empty ``test`` is not reported here: the compile
        turns it into an ``assert-invalid`` issue (a single report, even
        though the declaration sweep and the class builder may each
        consult the compiled form).
        """
        allowed = frozenset(self._ALLOWED_ATTRIBUTES)
        for raw in self.xsdElement.attrib:
            if raw.startswith("{"):
                continue
            if raw not in allowed:
                self._reportSchemaError(
                    f"<assertion> does not allow the '{raw}' attribute",
                    code="declaration-attribute",
                )
        self.compile()

    def compile(self) -> SimpleAssertion | None:
        """Parses the ``test`` once, reporting ``assert-invalid`` on failure.

        Idempotent: the parser's declaration sweep and the class builder
        may both need the result, and whichever runs first does the single
        compile. An empty test, an unbound prefix, an out-of-subset
        construct or an unusable ``xpathDefaultNamespace`` yields ``None``.
        """
        if self._compiled is not _UNSET:
            return self._compiled
        text = (self.test or "").strip()
        try:
            namespaces = dict(self._declarationNamespaces() or {})
            default_namespace = self._xpathDefaultNamespace()
            expression = parse_assertion_xpath(
                text,
                namespaces,
                default_namespace=default_namespace,
            )
        except XPathError as exc:
            self._reportSchemaError(
                f"<assertion> test {text!r} is outside the assertion XPath subset: {exc}",
                code="assert-invalid",
            )
            self._compiled = None
            return None
        self._compiled = SimpleAssertion(
            text,
            expression,
            namespaces,
            default_namespace,
            requires_context=assertion_requires_context(expression),
        )
        return self._compiled


def compile_simple_assertions(source: Any) -> list[SimpleAssertion]:
    """Compiles every ``xs:assertion`` facet of *source*.

    *source* is a ``SimpleType`` or a ``ComplexType`` with simple
    content. Returns the usable compiled assertions; an unusable one is
    reported ``assert-invalid`` by :meth:`AssertionFacet.compile` and
    dropped. Cached on the type, so the parser's sweep and the class
    builder share one compile.
    """
    cached = getattr(source, "_compiledAssertionFacets", None)
    if cached is not None:
        return cached
    compiled: list[SimpleAssertion] = []
    for representative in getattr(source, "assertions", None) or ():
        result = representative.compile()
        if result is not None:
            compiled.append(result)
    source._compiledAssertionFacets = compiled
    return compiled


def _assert_representatives(complex_type: Any) -> list[Any]:
    """Returns the ``Assert`` ERs belonging to *complex_type*.

    Only the type's own assertions count. They are direct children of the
    complex type, or children of the ``restriction``/``extension`` inside
    its ``complexContent``/``simpleContent``; nested element declarations
    own their own asserts and are not descended into.
    """
    result: list[Any] = []
    for child in getattr(complex_type, "processedChildren", None) or ():
        if child is None:
            continue
        kind = type(child).__name__
        if kind == "Assert":
            result.append(child)
        elif kind in ("ComplexContent", "SimpleContent"):
            for contained in getattr(child, "processedChildren", None) or ():
                if contained is None:
                    continue
                contained_kind = type(contained).__name__
                if contained_kind == "Assert":
                    result.append(contained)
                elif contained_kind in ("Restriction", "Extension"):
                    for nested in getattr(contained, "processedChildren", None) or ():
                        if nested is not None and type(nested).__name__ == "Assert":
                            result.append(nested)
    return result


def compile_assertions(complex_type: Any) -> list[Assertion]:
    """Parses every ``xs:assert`` of *complex_type*.

    Returns the compiled assertions, reporting ``assert-invalid`` (and
    dropping the assertion) for an empty ``test``, an unbound prefix, an
    out-of-subset construct, or an unusable ``xpathDefaultNamespace``.
    Idempotent per type: the parser's declaration sweep and the class
    builder may each need the result, and whichever runs first does the
    single compile (so an unusable expression is reported once, never
    twice). Never raises.
    """
    cached = getattr(complex_type, "_compiledAssertions", None)
    if cached is not None:
        return cached
    compiled: list[Assertion] = []
    for representative in _assert_representatives(complex_type):
        text = (representative.test or "").strip()
        try:
            namespaces = dict(representative._declarationNamespaces() or {})
            default_namespace = representative._xpathDefaultNamespace()
            expression = parse_assertion_xpath(
                text,
                namespaces,
                default_namespace=default_namespace,
            )
        except XPathError as exc:
            representative._reportSchemaError(
                f"<assert> test {text!r} is outside the assertion XPath subset: {exc}",
                code="assert-invalid",
            )
            continue
        compiled.append(Assertion(text, expression, namespaces, default_namespace))
    complex_type._compiledAssertions = compiled
    return compiled


class _ElementPathType:
    """A minimal XSD type descriptor for the elementpath evaluator.

    elementpath atomizes a node to its declared value type by consulting
    the type object's name (a Clark name) in its built-in value table.
    This adapter carries just enough of the ``XsdTypeProtocol`` surface
    for a built-in atomic type; it is only used to stamp the
    schema-declared attributes so value comparisons see the typed value.
    """

    __slots__ = ("name",)

    xsd_version = "1.1"

    def __init__(self, name: str) -> None:
        self.name = name

    def is_list(self) -> bool:
        return False

    def is_simple(self) -> bool:
        return True

    def has_mixed_content(self) -> bool:
        return False

    def is_element_only(self) -> bool:
        return False

    @property
    def parent(self):
        return None

    @property
    def simple_type(self):
        return None

    @property
    def root_type(self):
        return self


def _elementpath_type(dtype: Any) -> _ElementPathType | None:
    """Maps a pyxsd data type class to an elementpath type descriptor.

    The nearest built-in simple type in the class's MRO supplies the
    name; a user restriction therefore reports its primitive base
    (``GEOGRAPHIC_LOCATION`` -> ``xs:string``). A type with no built-in
    ancestor (a list/union, or ``anySimpleType``) stays untyped.
    """
    for klass in getattr(dtype, "__mro__", (dtype,)):
        local = klass.__dict__.get("name")
        if not isinstance(local, str) or local not in _PRIMITIVE_TYPES:
            continue
        if local in _NON_ATOMIC_BUILTINS:
            return None
        return _ElementPathType(clark(XSD_NS, local))
    return None


def attribute_type_map(cls: Any) -> dict[str, _ElementPathType]:
    """Maps the generated class's declared attributes to elementpath types.

    Walks the MRO (including base types) so inherited attribute
    declarations are covered; attributes with no usable simple type are
    left out, and so remain ``xs:untypedAtomic`` to the evaluator.
    """
    types: dict[str, _ElementPathType] = {}
    for klass in getattr(cls, "__mro__", (cls,)):
        for key in klass.__dict__.get("_attributeNames_", ()):
            descriptor = klass.__dict__.get(key)
            if descriptor is None:
                continue
            try:
                declared = descriptor.getType()
            except Exception:
                continue
            mapped = _elementpath_type(declared)
            if mapped is None:
                continue
            name = getattr(descriptor, "name", None)
            if name:
                types.setdefault(name, mapped)
            try:
                instance_name = descriptor.instanceName(
                    parser=getattr(getattr(cls, "schema", None), "_host", None)
                )
            except Exception:
                instance_name = None
            if instance_name and instance_name != name:
                types.setdefault(instance_name, mapped)
    return types


def element_type_map(
    cls: Any,
    _seen: set[int] | None = None,
) -> dict[str, _ElementPathType]:
    """Maps the element names reachable from the governing type.

    Walks the declared child elements of ``cls`` (and recursively of any
    complex-typed child) so an assertion sees descendant elements'
    declared types too — ``data(child::d) instance of xs:date`` and
    ``even-number lt 500`` depend on it. Names with no usable simple
    type stay untyped; a name collision keeps the first declaration.
    """
    types: dict[str, _ElementPathType] = {}
    seen = _seen if _seen is not None else set()
    if id(cls) in seen:
        return types
    seen.add(id(cls))
    for klass in getattr(cls, "__mro__", (cls,)):
        for key in klass.__dict__.get("_elementNames_", ()):
            descriptor = klass.__dict__.get(key)
            if descriptor is None:
                continue
            try:
                declared = descriptor.getType()
            except Exception:
                continue
            name = getattr(descriptor, "name", None)
            mapped = _elementpath_type(declared)
            if mapped is not None and name:
                types.setdefault(name, mapped)
                try:
                    instance_name = descriptor.instanceName(
                        parser=getattr(getattr(cls, "schema", None), "_host", None)
                    )
                except Exception:
                    instance_name = None
                if instance_name and instance_name != name:
                    types.setdefault(instance_name, mapped)
            elif isinstance(declared, type):
                for child_name, child_type in element_type_map(declared, seen).items():
                    types.setdefault(child_name, child_type)
    return types


def _assertion_value(cls: Any, element_tag: Any) -> Any:
    """The XDM value bound to ``$value`` for an element's assertions.

    For a complex type with simple content (including a list type) this
    is the typed value decoded with elementpath's own decoders, so
    ``$value instance of xs:date`` and ``count($value)`` see real
    atomics. For other complex content the typed value is the empty
    sequence (XSD 1.1 §3.13.4.2), which ``empty($value)`` observes.
    """
    content_cls = getattr(cls, "_simpleContentType_", None)
    if content_cls is None:
        return []
    text = element_tag.text or ""
    item_cls = getattr(content_cls, "itemType", None)
    if isinstance(item_cls, type):
        item_type = _elementpath_type(item_cls)
        if item_type is None:
            return []
        values: list[Any] = []
        for token in text.split():
            values.extend(get_atomic_sequence(cast(Any, item_type), token))
        return values
    atomic_type = _elementpath_type(content_cls)
    if atomic_type is None:
        return []
    return list(get_atomic_sequence(cast(Any, atomic_type), text))


def check_element_assertions(cls: Any, element_tag: Any) -> None:
    """Evaluates the governing type's assertions against a bound element.

    ``cls`` is the generated class of the element's governing type and
    ``element_tag`` the element's underlying ElementTree node. Every
    assertion on the class MRO is evaluated with the element as context
    item; a false result and a dynamic evaluation error both report
    ``assert-failed`` (XSD 1.1 §3.13.4.2: a failed or erroring assertion
    makes the element invalid, never a crash).
    """
    assertions: list[Assertion] = []
    for klass in cls.__mro__:
        own = klass.__dict__.get("_assertions_")
        if own:
            assertions.extend(own)
    if not assertions:
        return

    attribute_types = attribute_type_map(cls)
    element_types = element_type_map(cls)
    namespaces = cls._qname_bindings(element_tag)
    value = _assertion_value(cls, element_tag)
    context = local_name(element_tag.tag)
    for assertion in assertions:
        try:
            result = evaluate(
                assertion.compiled,
                element_tag,
                value=value,
                attribute_types=attribute_types or None,
                element_types=element_types or None,
                namespaces=namespaces,
            )
            satisfied = assertion.compiled.tree.boolean_value(result)
        except (XPathError, ElementPathError, TypeError, ValueError, LookupError) as exc:
            cls._report_error(
                f"assertion test {assertion.test!r} could not be evaluated "
                f"on element '{context}': {exc}",
                code="assert-failed",
                element=context,
            )
            continue
        if not satisfied:
            cls._report_error(
                f"assertion test {assertion.test!r} is not satisfied by element '{context}'",
                code="assert-failed",
                element=context,
            )


def _simple_assertion_value(instance: Any, lexical: str | None) -> Any:
    """The XDM value bound to ``$value`` for a simple type's assertions.

    The typed value is decoded through elementpath's own decoders so a
    comparison sees the XSD value space: an atomic type yields a
    one-item sequence of the narrowest built-in type, a list type yields
    one atomic per item, and a union yields its selected member's value
    (XSD 1.1 Part 2, ``xs:assertion``).
    """
    member = getattr(instance, "memberValue", None)
    if member is not None:
        return _simple_assertion_value(member, str(member))
    text = lexical if isinstance(lexical, str) else str(instance)
    item_cls = getattr(type(instance), "itemType", None)
    if isinstance(item_cls, type):
        item_type = _elementpath_type(item_cls)
        if item_type is None:
            return []
        values: list[Any] = []
        for token in text.split():
            values.extend(get_atomic_sequence(cast(Any, item_type), token))
        return values
    atomic_type = _elementpath_type(type(instance))
    if atomic_type is None:
        return []
    return list(get_atomic_sequence(cast(Any, atomic_type), text))


def check_simple_assertions(
    instance: Any,
    lexical: str | None,
    assertions: Any,
) -> None:
    """Evaluates a simple type's assertion facets against a bound value.

    ``instance`` is the validated value and ``lexical`` the
    whitespace-processed lexical form it was built from. Each assertion is
    evaluated with ``$value`` bound to the typed value and no context
    item. A false result, a context-dependent test (no context item is
    defined for a simple-type assertion) and a dynamic evaluation error
    all raise :class:`SimpleAssertionError` (a ``TypeError`` carrying the
    ``assert-failed`` code): the binding paths report it, and a union
    treats the member as not matching and tries the next one.
    """
    if not assertions:
        return
    value = _simple_assertion_value(instance, lexical)
    for assertion in assertions:
        if assertion.requires_context:
            raise SimpleAssertionError(
                f"assertion test {assertion.test!r} reads the XPath context, "
                "which a simple type assertion does not define"
            )
        try:
            result = evaluate(assertion.compiled, None, value=value)
            satisfied = assertion.compiled.tree.boolean_value(result)
        except (XPathError, ElementPathError, TypeError, ValueError, LookupError) as exc:
            raise SimpleAssertionError(
                f"assertion test {assertion.test!r} could not be evaluated for the value: {exc}"
            ) from exc
        if not satisfied:
            raise SimpleAssertionError(
                f"assertion test {assertion.test!r} is not satisfied by the value"
            )
