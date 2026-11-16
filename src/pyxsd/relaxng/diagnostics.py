"""Deterministic RELAX NG expectations, recovery findings, and completeness.

A fail-fast validation outcome stops at the earliest definitive mismatch and
retains the derivative state from just before that occurrence.  A recovery
traversal instead continues through independent unexpected subtrees, failed
attributes or scalars, and missing attributes or content, retaining every
finding in source order.  Both project the preceding residual into a concise,
sorted :class:`ExpectationSummary` and record instance-phase findings on a
:class:`ValidationReport`.

The projection deliberately describes the residual, not the branch that
failed last.  Element and attribute names are collected from every viable
alternative; ``group`` follows its second operand only when the first is
nullable, while ``interleave`` and ``choice`` expose all operands.  Names are
returned as expanded Clark names (or deterministic wildcard descriptions) in
sorted order, so diagnostics never depend on set/dict iteration or source
insertion order.  Traversals are iterative and guard reference cycles; schema
recursion is supported without a Python stack frame per pattern node.

Recovery states are diagnostic only.  A report built from a recovery traversal
stays invalid whenever any finding exists, and a retention cap marks the
report's ``diagnostics_complete`` value false without changing its verdict.
Budget exhaustion is raised by
:func:`~pyxsd.relaxng.validator.validate_element_recovering` and never reaches
this module as a grammar mismatch.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final, Literal

from pyxsd.locations import IssueLocation, LocationPrecision
from pyxsd.namespaces import clark
from pyxsd.relaxng.datatypes import XSD_LIBRARY
from pyxsd.relaxng.limits import RelaxNGLimits
from pyxsd.relaxng.names import AnyName, ExceptName, Name, NameChoice, NameClass, NsName
from pyxsd.relaxng.patterns import (
    Attribute,
    Choice,
    Data,
    DataExcept,
    Element,
    Empty,
    Group,
    Interleave,
    List,
    NotAllowed,
    OneOrMore,
    Pattern,
    Ref,
    Text,
    Value,
)
from pyxsd.relaxng.validator import (
    RecoveryOutcome,
    ValidationMismatch,
    ValidationOutcome,
    _After,
    _nullable,
    expanded_name,
)
from pyxsd.validation import ValidationReport

__all__ = [
    "INVALID_SCALAR",
    "INVALID_STRUCTURE",
    "MISSING_ATTRIBUTE",
    "MISSING_CONTENT",
    "UNEXPECTED_ATTRIBUTE",
    "UNEXPECTED_CONTENT",
    "UNEXPECTED_ELEMENT",
    "ExpectationSummary",
    "expectations",
    "report_first_mismatch",
    "report_mismatches",
]

#: Element occurrence is not admitted by the residual at this position.
UNEXPECTED_ELEMENT: Final = "unexpected-element"
#: Attribute occurrence is not admitted by the residual at this position.
UNEXPECTED_ATTRIBUTE: Final = "unexpected-attribute"
#: A still-required attribute was absent when the start tag closed.
MISSING_ATTRIBUTE: Final = "missing-attribute"
#: Required character data or child content was absent before the end tag.
MISSING_CONTENT: Final = "missing-content"
#: Character or attribute data does not satisfy an allowed datatype/value.
INVALID_SCALAR: Final = "invalid-scalar"
#: Character data appeared where only child elements (or nothing) were allowed.
UNEXPECTED_CONTENT: Final = "unexpected-content"
#: The input root is not an element or has no usable expanded name.
INVALID_STRUCTURE: Final = "invalid-structure"

_MismatchKind = Literal[
    "unexpected-element",
    "unexpected-attribute",
    "missing-attribute",
    "missing-content",
    "invalid-scalar",
    "unexpected-content",
    "invalid-structure",
]


@dataclass(frozen=True)
class ExpectationSummary:
    """Sorted projection of what one residual accepts at its current position.

    ``elements`` and ``attributes`` hold expanded names (``{uri}local`` or a
    bare local name) and deterministic wildcard descriptions (``*``,
    ``{uri}*``, ``{}*`` for the no-namespace wildcard, or ``BASE except
    EXCLUDED``).  ``content`` holds concise content labels such as ``text``,
    ``empty``, ``data``, ``list``, and exact ``value`` lexical forms.
    ``datatypes`` holds deterministic datatype summaries for every reachable
    data/value/list pattern, including attribute values.  Every tuple is
    sorted and deduplicated.
    """

    elements: tuple[str, ...] = ()
    attributes: tuple[str, ...] = ()
    content: tuple[str, ...] = ()
    datatypes: tuple[str, ...] = ()

    def describe(self) -> str:
        """Render the non-empty projections in one deterministic sentence."""
        parts: list[str] = []
        if self.elements:
            parts.append("elements " + ", ".join(self.elements))
        if self.attributes:
            parts.append("attributes " + ", ".join(self.attributes))
        if self.content:
            parts.append("content " + ", ".join(self.content))
        if self.datatypes:
            parts.append("datatypes " + ", ".join(self.datatypes))
        return "; ".join(parts) if parts else "no expectation"


def expectations(pattern: Pattern) -> ExpectationSummary:
    """Return the sorted expectation projection for *pattern*.

    The result describes the supplied residual only.  Callers diagnosing a
    first mismatch should pass ``outcome.mismatch.preceding`` so the summary
    cannot drift toward whichever alternative failed last.
    """
    if not isinstance(pattern, Pattern):
        raise TypeError("expectations() requires a RELAX NG pattern")
    elements, content, datatypes = _content_projection(pattern)
    attributes, attribute_datatypes = _attribute_projection(pattern)
    return ExpectationSummary(
        tuple(sorted(elements)),
        tuple(sorted(attributes)),
        tuple(sorted(content)),
        tuple(sorted(datatypes | attribute_datatypes)),
    )


def report_first_mismatch(outcome: ValidationOutcome, report: ValidationReport) -> None:
    """Record the earliest definitive mismatch on *report*.

    The finding's expectations come from ``outcome.mismatch.preceding``; its
    location is the exact source occurrence (the carrier element for
    attributes and character data).  A valid outcome is a no-op.  An invalid
    outcome without mismatch evidence is an internal contradiction, and an
    unknown event raises :class:`ValueError` rather than inventing a code.
    """
    if outcome.valid:
        return
    mismatch = outcome.mismatch
    if mismatch is None:
        raise ValueError("an invalid RELAX NG outcome must carry a first mismatch")
    _report_mismatch(mismatch, report)


def report_mismatches(
    outcome: RecoveryOutcome,
    report: ValidationReport,
    *,
    limits: RelaxNGLimits | None = None,
) -> None:
    """Record every finding from a completed recovery traversal on *report*.

    Findings stay in traversal order, which is source order for independent
    occurrences.  ``limits.max_diagnostics`` tightens (never widens) the
    report's retained-issue budget; suppressed details remain counted and mark
    the report's ``diagnostics_complete`` value false.  A report that already
    declares itself incomplete is never upgraded.
    """
    if not outcome.valid and not outcome.mismatches:
        raise ValueError("an invalid RELAX NG recovery outcome must carry findings")
    report._tighten_issue_retention(None if limits is None else limits.max_diagnostics)
    for mismatch in outcome.mismatches:
        _report_mismatch(mismatch, report)
    report._set_diagnostics_complete(outcome.complete and report.suppressed_count == 0)


def _report_mismatch(mismatch: ValidationMismatch, report: ValidationReport) -> None:
    """Record one mismatch using the deterministic expectation projection."""
    summary = expectations(mismatch.preceding)
    attribute_expected = False
    attribute_datatypes: set[str] = set()
    if mismatch.event == "attribute" and mismatch.name is not None:
        uri, local = expanded_name(mismatch.name)
        attribute_expected, attribute_datatypes = _matching_attribute_datatypes(
            mismatch.preceding, uri, local
        )
    kind = _mismatch_kind(mismatch, summary, attribute_expected)
    scalar_datatypes = (
        attribute_datatypes if kind == INVALID_SCALAR and mismatch.event == "attribute" else None
    )
    location, precision = _mismatch_location(mismatch)
    report.add_error(
        _mismatch_message(mismatch, summary, kind, scalar_datatypes),
        code=kind,
        element=_element_display(mismatch.element),
        phase="instance",
        location=location,
        location_precision=precision,
        details=_mismatch_details(mismatch, summary, scalar_datatypes),
    )


def _mismatch_kind(
    mismatch: ValidationMismatch, summary: ExpectationSummary, attribute_expected: bool
) -> _MismatchKind:
    event = mismatch.event
    if event == "structure":
        return INVALID_STRUCTURE
    if event == "start_tag_open":
        return UNEXPECTED_ELEMENT
    if event == "attribute":
        return INVALID_SCALAR if attribute_expected else UNEXPECTED_ATTRIBUTE
    if event == "start_tag_close":
        return MISSING_ATTRIBUTE
    if event in ("end_tag", "document_end"):
        return MISSING_CONTENT
    if event == "text":
        return INVALID_SCALAR if _expects_scalar(summary) else UNEXPECTED_CONTENT
    raise ValueError(f"unknown RELAX NG mismatch event {event!r}")


def _expects_scalar(summary: ExpectationSummary) -> bool:
    """Whether any non-empty content alternative accepts character data."""
    return any(label != "empty" for label in summary.content)


def _matching_attribute_datatypes(
    pattern: Pattern, uri: str | None, local: str
) -> tuple[bool, set[str]]:
    """Return whether the name is expected and the datatypes declared for it.

    Every viable branch is visited so repeated declarations of the same
    attribute contribute all their datatypes.  Child element contents are not
    entered; attributes after an end tag belong to the parent.
    """
    expected = False
    datatypes: set[str] = set()
    pending = [pattern]
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        key = id(node)
        if key in seen:
            continue
        seen.add(key)
        if isinstance(node, _After):
            pending.append(node.first)
        elif isinstance(node, Ref):
            pending.append(node.target)
        elif isinstance(node, (Choice, Group, Interleave)):
            pending.extend((node.first, node.second))
        elif isinstance(node, OneOrMore):
            pending.append(node.pattern)
        elif isinstance(node, Element):
            continue
        elif isinstance(node, Attribute):
            if node.name_class.contains(uri, local):
                expected = True
                datatypes.update(_datatype_descriptions(node.pattern))
        elif not isinstance(node, (Data, DataExcept, Empty, List, NotAllowed, Text, Value)):
            raise TypeError(f"unknown RELAX NG pattern {type(node).__name__}")
    return expected, datatypes


def _expects_attribute_name(pattern: Pattern, uri: str | None, local: str) -> bool:
    """Search current-position attributes without entering child elements."""
    return _matching_attribute_datatypes(pattern, uri, local)[0]


def _mismatch_location(
    mismatch: ValidationMismatch,
) -> tuple[IssueLocation | None, LocationPrecision]:
    element = mismatch.element
    if element is None:
        return None, "unknown"
    location = element.source_location
    if location is None:
        return None, "unknown"
    if not all(isinstance(step.name, str) for step in location.path):
        # A comment/PI root has no expanded element name to address exactly;
        # reporting an unencodable path would make the report unserializable.
        return None, "unknown"
    if mismatch.event == "attribute" and mismatch.name is not None:
        return replace(location, attribute=mismatch.name), "exact"
    if mismatch.event in ("start_tag_open", "structure"):
        return location, "exact"
    return location, "container"


def _element_display(element: object | None) -> str | None:
    """Return an element's expanded name when it has a usable one."""
    name = getattr(element, "_name_", None)
    return name if isinstance(name, str) and name else None


def _mismatch_details(
    mismatch: ValidationMismatch,
    summary: ExpectationSummary,
    attribute_datatypes: set[str] | None = None,
) -> dict[str, object]:
    datatypes = summary.datatypes if attribute_datatypes is None else attribute_datatypes
    details: dict[str, object] = {
        "event": mismatch.event,
        "expected-elements": list(summary.elements),
        "expected-attributes": list(summary.attributes),
        "expected-content": list(summary.content),
        "expected-datatypes": sorted(datatypes),
    }
    if mismatch.name is not None:
        details["actual-name"] = mismatch.name
    if mismatch.lexical is not None:
        details["actual-lexical"] = mismatch.lexical
    if mismatch.content_index is not None:
        details["content-index"] = mismatch.content_index
    if mismatch.recovery is not None:
        details["recovery"] = mismatch.recovery
    return details


def _mismatch_message(
    mismatch: ValidationMismatch,
    summary: ExpectationSummary,
    kind: _MismatchKind,
    attribute_datatypes: set[str] | None,
) -> str:
    if kind == INVALID_STRUCTURE:
        structural_name = getattr(mismatch.element, "_name_", None)
        if not isinstance(structural_name, str):
            return "document structure is invalid: the document root is not an element"
        if not structural_name:
            return "document structure is invalid: the element name is empty"
        return f"document structure is invalid near element {structural_name!r}"
    name = mismatch.name if mismatch.name is not None else _element_display(mismatch.element)
    lexical = mismatch.lexical
    if kind == UNEXPECTED_ELEMENT:
        return f"element {name!r} is not allowed here; {_element_expectation(summary)}"
    if kind == UNEXPECTED_ATTRIBUTE:
        return f"attribute {name!r} is not allowed here; {_attribute_expectation(summary)}"
    if kind == MISSING_ATTRIBUTE:
        return f"required attribute is missing; {_attribute_expectation(summary)}"
    if kind == MISSING_CONTENT:
        return f"required content is missing; {_content_expectation(summary)}"
    if kind == INVALID_SCALAR:
        if mismatch.event == "attribute":
            return (
                f"attribute {name!r} value {lexical!r} is not a valid scalar value; "
                f"{_attribute_scalar_expectation(attribute_datatypes or set())}"
            )
        return (
            f"character data {lexical!r} is not a valid scalar value; "
            f"{_scalar_expectation(summary)}"
        )
    return f"character data {lexical!r} is not allowed here; {_content_expectation(summary)}"


def _element_expectation(summary: ExpectationSummary) -> str:
    if summary.elements:
        return "expected element " + ", ".join(summary.elements)
    if summary.content or summary.datatypes:
        return "expected " + _content_phrase(summary)
    return "no element is expected here"


def _attribute_expectation(summary: ExpectationSummary) -> str:
    if summary.attributes:
        return "expected attribute " + ", ".join(summary.attributes)
    return "no attribute is expected here"


def _content_expectation(summary: ExpectationSummary) -> str:
    parts: list[str] = []
    if summary.elements:
        parts.append("element " + ", ".join(summary.elements))
    if summary.content or summary.datatypes:
        parts.append("content " + _content_phrase(summary))
    if not parts:
        return "no content is expected here"
    return "expected " + " or ".join(parts)


def _scalar_expectation(summary: ExpectationSummary) -> str:
    if summary.content:
        return "expected " + _content_phrase(summary)
    return "expected a valid scalar value"


def _attribute_scalar_expectation(datatypes: set[str]) -> str:
    """Describe only the datatypes declared for the failing attribute."""
    if datatypes:
        return "expected datatype " + ", ".join(sorted(datatypes))
    return "expected a valid scalar value"


def _content_phrase(summary: ExpectationSummary) -> str:
    content = ", ".join(summary.content)
    if not summary.datatypes:
        return content or "no content"
    datatypes = ", ".join(summary.datatypes)
    return f"{content} ({datatypes})" if content else datatypes


def _content_projection(pattern: Pattern) -> tuple[set[str], set[str], set[str]]:
    """Collect current-position element names, content labels, and datatypes.

    ``group`` exposes its second operand only once its first operand may be
    empty; child elements stop the walk because their contents are not
    expected at this position.  Attributes do not consume content slots.
    """
    elements: set[str] = set()
    content: set[str] = set()
    datatypes: set[str] = set()
    pending = [pattern]
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        key = id(node)
        if key in seen:
            continue
        seen.add(key)
        if isinstance(node, _After):
            pending.append(node.first)
        elif isinstance(node, Ref):
            pending.append(node.target)
        elif isinstance(node, (Choice, Interleave)):
            pending.extend((node.first, node.second))
        elif isinstance(node, Group):
            pending.append(node.first)
            if _nullable(node.first):
                pending.append(node.second)
        elif isinstance(node, OneOrMore):
            pending.append(node.pattern)
        elif isinstance(node, Element):
            elements.update(_name_class_descriptions(node.name_class))
        elif isinstance(node, Attribute):
            continue
        elif isinstance(node, Text):
            content.add("text")
        elif isinstance(node, Empty):
            content.add("empty")
        elif isinstance(node, List):
            content.add("list")
            datatypes.update(_datatype_descriptions(node.pattern))
        elif isinstance(node, (Data, DataExcept, Value)):
            content.add("data" if not isinstance(node, Value) else f"value {node.lexical!r}")
            datatypes.update(_datatype_descriptions(node))
        elif isinstance(node, NotAllowed):
            continue
        else:
            raise TypeError(f"unknown RELAX NG pattern {type(node).__name__}")
    return elements, content, datatypes


def _attribute_projection(pattern: Pattern) -> tuple[set[str], set[str]]:
    """Collect current-position attribute names and their value datatypes.

    Attribute order is semantically unordered, so both operands of a group,
    interleave, or choice remain visible.  Child element contents are not.
    """
    attributes: set[str] = set()
    datatypes: set[str] = set()
    pending = [pattern]
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        key = id(node)
        if key in seen:
            continue
        seen.add(key)
        if isinstance(node, _After):
            pending.append(node.first)
        elif isinstance(node, Ref):
            pending.append(node.target)
        elif isinstance(node, (Choice, Group, Interleave)):
            pending.extend((node.first, node.second))
        elif isinstance(node, OneOrMore):
            pending.append(node.pattern)
        elif isinstance(node, Element):
            continue
        elif isinstance(node, Attribute):
            attributes.update(_name_class_descriptions(node.name_class))
            datatypes.update(_datatype_descriptions(node.pattern))
        elif not isinstance(node, (Data, DataExcept, Empty, List, NotAllowed, Text, Value)):
            raise TypeError(f"unknown RELAX NG pattern {type(node).__name__}")
    return attributes, datatypes


def _name_class_descriptions(name_class: NameClass) -> tuple[str, ...]:
    """Return sorted expanded-name and wildcard descriptions for *name_class*."""
    pending = [name_class]
    descriptions: set[str] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, Name):
            descriptions.add(clark(current.uri, current.local))
        elif isinstance(current, AnyName):
            descriptions.add("*")
        elif isinstance(current, NsName):
            descriptions.add(clark(current.uri, "*") if current.uri else "{}*")
        elif isinstance(current, NameChoice):
            pending.extend(current.alternatives)
        elif isinstance(current, ExceptName):
            excluded = "|".join(_name_class_descriptions(current.excluded))
            for base in _name_class_descriptions(current.base):
                descriptions.add(f"{base} except {excluded}" if excluded else base)
        else:
            raise TypeError(f"unknown RELAX NG name class {type(current).__name__}")
    return tuple(sorted(descriptions))


def _datatype_descriptions(pattern: Pattern) -> set[str]:
    """Collect deterministic datatype summaries from one value-bearing pattern."""
    descriptions: set[str] = set()
    pending = [pattern]
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        key = id(node)
        if key in seen:
            continue
        seen.add(key)
        if isinstance(node, (Data, DataExcept, Value)):
            descriptions.add(_datatype_label(node.datatype))
        elif isinstance(node, List):
            pending.append(node.pattern)
        elif isinstance(node, _After):
            pending.append(node.first)
        elif isinstance(node, Ref):
            pending.append(node.target)
        elif isinstance(node, (Choice, Group, Interleave)):
            pending.extend((node.first, node.second))
        elif isinstance(node, OneOrMore):
            pending.append(node.pattern)
        elif isinstance(node, (Attribute, Element, Empty, NotAllowed, Text)):
            continue
        else:
            raise TypeError(f"unknown RELAX NG pattern {type(node).__name__}")
    return descriptions


def _datatype_label(datatype: object) -> str:
    """Describe a compiled datatype without hashes or object identities."""
    type_name = getattr(datatype, "type_name", None)
    library = getattr(datatype, "library_uri", None)
    if isinstance(type_name, str) and type_name:
        if library == XSD_LIBRARY:
            label = f"xsd:{type_name}"
        elif isinstance(library, str) and library:
            label = f"{{{library}}}{type_name}"
        else:
            label = type_name
    else:
        label = type(datatype).__name__
    parameters = getattr(datatype, "parameters", ())
    if isinstance(parameters, (tuple, list)) and parameters:
        specifications: set[str] = set()
        for parameter in parameters:
            name = getattr(parameter, "name", None)
            value = getattr(parameter, "value", None)
            if isinstance(name, str) and isinstance(value, str):
                specifications.add(f"{name}={value!r}")
        if specifications:
            label = f"{label}({', '.join(sorted(specifications))})"
    return label
