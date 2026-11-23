"""Language profiles: effective versions, compatibility, and recovery rules.

A :class:`LanguageProfile` separates the processor's language identity from an
element's effective version.  An XSLT 3.0 processor stays a 3.0 processor when a
stylesheet or a contained element declares ``version="1.0"`` or
``version="2.0"``; only the compatibility behavior defined for that version is
applied.  The profile also owns the small set of compatibility conversions the
XSLT 3.0 runtime needs and the rule-specific recovery table for the active
processor profile.

Recovery is never selected by an error code alone.  Each permitted rule names
the normative condition it implements; XSLT 3.0 does not inherit the XSLT 2.0
``XTRE`` recovery table.  A :class:`RecoveryPolicy` is an explicit host choice:
recovery must be enabled for a rule, and ``strict=True`` turns every enabled
rule back into an error without changing any other conformance behavior.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, cast

from pyxsd.xdm import XdmArray, XdmAtomic, XdmFunction, XdmItem, XdmMap, XdmSequence
from pyxsd.xdm.legacy import ResultTreeFragment, legacy_string_sequence
from pyxsd.xdm.nodes import XdmNode
from pyxsd.xslt.errors import XsltDynamicError, normalized_error_code

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"

_PROCESSOR_VERSIONS = frozenset({"1.0", "2.0", "3.0", "3.1"})

#: The language versions each processor identity implements.  A processor
#: always offers its own complete language; a declaration that names a version
#: it does not implement (1.5, 3.3, 8.5, ...) never invents a vocabulary, so
#: profile-owned decisions resolve to one of these.
_IMPLEMENTED_LANGUAGE_VERSIONS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "1.0": ("1.0",),
        "2.0": ("1.0", "2.0"),
        "3.0": ("1.0", "2.0", "3.0", "3.1"),
        "3.1": ("1.0", "2.0", "3.0", "3.1"),
    }
)


def _decimal_version(value: Any, label: str) -> Decimal:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} {value!r} is not a decimal number") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{label} {value!r} is not a valid version")
    return number


@dataclass(frozen=True, slots=True)
class RecoveryRule:
    """One normative condition that permits an implementation recovery action."""

    name: str
    error_codes: frozenset[str] = frozenset()
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("a recovery rule requires a non-empty name")
        if not isinstance(self.description, str):
            raise TypeError("a recovery rule description must be a string")
        codes = frozenset(self.error_codes)
        if any(not isinstance(code, str) or not code for code in codes):
            raise TypeError("recovery rule error codes must be non-empty strings")
        object.__setattr__(self, "error_codes", codes)

    def matches(self, code: str | None) -> bool:
        """Whether *code* is one the rule's normative condition can raise."""
        local = normalized_error_code(code)
        return local is not None and local in self.error_codes


#: XSLT 3.0 §20.1 permits ignoring a URI whose ``doc()`` retrieval fails.
DOCUMENT_RETRIEVAL = RecoveryRule(
    "document-retrieval",
    frozenset({"FODC0001", "FODC0002", "FODC0005"}),
    "ignore a URI whose document retrieval failed",
)

#: XSLT 3.0 §20.1 permits ignoring an unsupported fragment identifier.
DOCUMENT_FRAGMENT_IDENTIFIER = RecoveryRule(
    "document-fragment-identifier",
    frozenset({"XTDE1160"}),
    "ignore a fragment identifier and return the document node",
)

#: The complete XSLT 3.0 recovery table selected by this processor.
XSLT30_RECOVERY_RULES = frozenset(
    {
        DOCUMENT_RETRIEVAL,
        DOCUMENT_FRAGMENT_IDENTIFIER,
    }
)

#: The regular-expression flags defined by XPath 2.0 Functions and Operators
#: §7.4.  The ``q`` (literal) flag arrived with the 3.0 function library, so a
#: native 2.0 profile does not define it.
XSLT20_REGEX_FLAGS = frozenset({"i", "m", "s", "x"})

#: The regular-expression flags of the XPath 3.0 function library.
XSLT30_REGEX_FLAGS = frozenset({"i", "m", "s", "x", "q"})

#: The XSLT 2.0 recoverable dynamic errors, used only by a native 2.0 profile.
XSLT20_RECOVERY_RULES = frozenset(
    {
        RecoveryRule(
            "strip-preserve-space-conflict",
            frozenset({"XTRE0270"}),
            "select the last matching strip-space/preserve-space declaration",
        ),
        RecoveryRule(
            "multiple-template-rules",
            frozenset({"XTRE0540"}),
            "select the last matching template rule in declaration order",
        ),
        RecoveryRule(
            "xml-space-attribute",
            frozenset({"XTRE0795"}),
            "construct the requested xml:space attribute value",
        ),
        RecoveryRule(
            "document-fragment-identifier",
            frozenset({"XTRE1160"}),
            "ignore a fragment identifier and return the document node",
        ),
        RecoveryRule(
            "duplicate-result-uri",
            frozenset({"XTRE1495"}),
            "implementation-defined handling of duplicate final result URIs",
        ),
        RecoveryRule(
            "read-write-same-resource",
            frozenset({"XTRE1500"}),
            "implementation-defined handling of reading and writing one resource",
        ),
        RecoveryRule(
            "disable-output-escaping-unsupported",
            frozenset({"XTRE1620"}),
            "ignore an unsupported disable-output-escaping request",
        ),
        RecoveryRule(
            "disable-output-escaping-unserialized",
            frozenset({"XTRE1630"}),
            "ignore disable-output-escaping when no serialization occurs",
        ),
    }
)


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    """An explicit host selection from the active profile's recovery table.

    An empty policy (the default) signals every error.  Enabling a rule permits
    the profile's defined recovery action; ``strict=True`` withdraws every
    enabled rule without affecting rules the profile never permitted.
    """

    rules: frozenset[str] = frozenset()
    strict: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.rules, str) or not isinstance(self.rules, Iterable):
            raise TypeError("recovery rules must be an iterable of rule names")
        selected = frozenset(self.rules)
        if any(not isinstance(name, str) or not name for name in selected):
            raise TypeError("recovery rule names must be non-empty strings")
        if not isinstance(self.strict, bool):
            raise TypeError("strict recovery must be a boolean")
        object.__setattr__(self, "rules", selected)

    def allows(self, rule: RecoveryRule, profile: LanguageProfile) -> bool:
        """Whether this policy permits *rule* under *profile*."""
        if not isinstance(rule, RecoveryRule):
            raise TypeError("a recovery policy requires a RecoveryRule")
        if not isinstance(profile, LanguageProfile):
            raise TypeError("a recovery policy requires a LanguageProfile")
        if self.strict or rule.name not in self.rules:
            return False
        return profile.permits_recovery(rule)


@dataclass(frozen=True, slots=True)
class LanguageProfile:
    """Processor identity plus one element's effective XSLT version."""

    processor_version: str = "3.0"
    effective_version: str = "3.0"
    schema_aware: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.processor_version, str):
            raise TypeError("processor_version must be a string")
        if self.processor_version not in _PROCESSOR_VERSIONS:
            raise ValueError(f"unsupported processor profile {self.processor_version!r}")
        _decimal_version(self.processor_version, "processor_version")
        _decimal_version(self.effective_version, "effective_version")
        if not isinstance(self.schema_aware, bool):
            raise TypeError("schema_aware must be a boolean")

    @classmethod
    def for_processor(
        cls,
        processor: str,
        effective_version: str | None = None,
        *,
        schema_aware: bool = True,
    ) -> LanguageProfile:
        """Build the profile for *processor* running *effective_version*."""
        return cls(
            processor_version=processor,
            effective_version=processor if effective_version is None else effective_version,
            schema_aware=schema_aware,
        )

    @classmethod
    def from_options(
        cls,
        options: Any,
        effective_version: str | None = None,
    ) -> LanguageProfile:
        """Build a profile from a compiler ``ProfileOptions`` value (or None)."""
        if options is None:
            return cls.for_processor("3.0", effective_version)
        declared = getattr(options, "processor_profile", "xslt-3.0")
        if not isinstance(declared, str):
            raise TypeError("processor_profile must be a string")
        processor = declared[5:] if declared.startswith("xslt-") else declared
        schema_aware = getattr(options, "schema_aware", True)
        if not isinstance(schema_aware, bool):
            raise TypeError("schema_aware must be a boolean")
        return cls.for_processor(processor, effective_version, schema_aware=schema_aware)

    @property
    def native(self) -> bool:
        """Whether this is a native pre-3.0 processor rather than compatibility mode."""
        return _decimal_version(self.processor_version, "processor_version") < Decimal("3.0")

    @property
    def legacy(self) -> bool:
        """Whether this is the native XSLT 1.0 processor.

        A native 1.0 processor owns the legacy data model: result tree
        fragments, XPath 1.0 node-sets, and the 1.0 string/boolean/number
        conversions.  A 3.0 processor running a ``version="1.0"`` stylesheet
        applies only the compatibility conversions and is not legacy.
        """
        return self.processor_version == "1.0"

    @property
    def compatibility_mode(self) -> bool:
        """Whether XPath 1.0 compatibility behavior applies to this element.

        A native pre-3.0 processor applies the compatibility rule of its own
        language: XSLT 2.0 §3.8 enables backwards-compatible processing for
        every element whose effective version is less than 2.0.  The native
        1.0 processor runs the XPath 1.0 grammar itself, so its expressions
        already use 1.0 semantics; a stylesheet it forwards also keeps the
        1.0 value model (see :attr:`result_tree_fragments`).  The default 3.0
        processor keeps the XSLT 3.0 rule (§3.9.1), where only an effective
        version of exactly 1.0 is XSLT 1.0 behavior.
        """
        effective = _decimal_version(self.effective_version, "effective_version")
        if self.native:
            return effective < Decimal("2.0")
        return effective == Decimal("1.0")

    @property
    def result_tree_fragments(self) -> bool:
        """Whether this element's content values use XSLT 1.0 fragments.

        XSLT 1.0 §11.1 makes a content-initialized variable, parameter, or
        with-parameter a result tree fragment.  The model belongs to a native
        1.0 processor when the element is processed with 1.0 behavior; a
        later effective version keeps the later language's value model.
        """
        if not self.legacy:
            return False
        return _decimal_version(self.effective_version, "effective_version") == Decimal("1.0")

    @property
    def language_version(self) -> str:
        """The implemented language version that governs this element.

        Profile-owned version decisions — function arities, element
        availability, and the runtime bridge — key off a declared version the
        processor implements.  A declaration naming a version the processor
        does not implement keeps the processor's own language: a future
        version (``8.5``) is processed forwards-compatibly with the
        implemented language, and an unimplemented intermediate version
        (``1.5`` under a 3.0 processor) does not invent a vocabulary.
        ``compatibility_mode`` and the forwards-compatible checks keep reading
        the declared version itself.
        """
        declared = _decimal_version(self.effective_version, "effective_version")
        if self.legacy:
            # A native 1.0 processor executes the 1.0 language, but a
            # forwards-compatible stylesheet may select a later implemented
            # function surface (the engine offers those as extensions, as it
            # long has).  A declaration the engine does not implement keeps
            # the 1.0 surface.
            for candidate in reversed(_IMPLEMENTED_LANGUAGE_VERSIONS["3.0"]):
                if declared == Decimal(candidate):
                    return candidate
            return "1.0"
        implemented = _IMPLEMENTED_LANGUAGE_VERSIONS.get(self.processor_version)
        if implemented is None:  # pragma: no cover - every profile is in the table
            implemented = _IMPLEMENTED_LANGUAGE_VERSIONS["3.0"]
        for candidate in reversed(implemented):
            if declared == Decimal(candidate):
                return candidate
        return implemented[-1]

    @property
    def xpath_version(self) -> str:
        """The normative XPath version selected by the processor profile."""
        if self.processor_version == "1.0":
            return "1.0"
        if self.processor_version == "2.0":
            return "2.0"
        return "3.1"

    @property
    def regex_flags(self) -> frozenset[str]:
        """The regular-expression flags this processor profile defines.

        The XSLT 2.0 instruction layer owns this set for
        ``xsl:analyze-string``: an effective flag string is checked against
        the profile, so a 3.0-only flag is an ``XTDE1145`` under a native 2.0
        processor even when the flag value is computed at run time.
        """
        if self.xpath_version in ("1.0", "2.0"):
            return XSLT20_REGEX_FLAGS
        return XSLT30_REGEX_FLAGS

    @property
    def recovery_rules(self) -> frozenset[RecoveryRule]:
        """The rule table owned by this processor profile."""
        if not self.native:
            return XSLT30_RECOVERY_RULES
        if self.processor_version == "2.0":
            return XSLT20_RECOVERY_RULES
        return frozenset()

    def permits_recovery(self, rule: RecoveryRule) -> bool:
        """Whether the active normative profile permits *rule* at all."""
        if not isinstance(rule, RecoveryRule):
            raise TypeError("permits_recovery requires a RecoveryRule")
        return rule in self.recovery_rules

    def recovery_rule_for(self, value: RecoveryRule | str) -> RecoveryRule | None:
        """Resolve a rule object, rule name, or error code to a permitted rule."""
        if isinstance(value, RecoveryRule):
            return value if self.permits_recovery(value) else None
        if not isinstance(value, str):
            raise TypeError("a recovery rule is named by RecoveryRule or string")
        for rule in self.recovery_rules:
            if rule.name == value or rule.matches(value):
                return rule
        return None

    def system_property(self, name: str) -> str:
        """Return the profile's value for one ``xl:*`` system property.

        An empty string means the property is not owned by the language
        profile: the caller falls back to its own vendor table.  Native
        pre-3.0 profiles own more properties than 3.0 does, because the
        capabilities those properties advertise arrived with XSLT 3.0.
        """
        uri, local = _property_name(name)
        if uri != XSLT_NS:
            return ""
        if local == "version":
            return self.processor_version
        if local == "xpath-version":
            return self.xpath_version
        if local == "is-schema-aware":
            # XSLT 1.0 has no schema-aware processing and defines no such
            # property.  A native 1.0 processor advertises that absence rather
            # than falling through to the 3.0 vendor table's ``yes``.
            if self.legacy:
                return "no"
            return "yes" if self.schema_aware else "no"
        if local in ("supports-backwards-compatibility", "supports-namespace-axis"):
            # Both properties exist in XSLT 2.0.  A native 1.0 processor does
            # not define them, so its answer falls through to the caller.
            if self.processor_version == "1.0":
                return ""
            # This engine implements backwards-compatible processing and the
            # namespace axis, a native 2.0 processor no less than 3.0.
            return "yes"
        if self.native:
            # These capability properties arrived with XSLT 3.0.  A native
            # pre-3.0 profile advertises the absence of the capability rather
            # than falling through to the 3.0 vendor table.
            if local == "supports-dynamic-evaluation":
                return "no"
            if local == "supports-higher-order-functions":
                return "no"
        return ""

    def check_xdm_2_0_value(self, value: Iterable[XdmItem], label: str) -> None:
        """Reject maps, arrays, and function items in one XDM 2.0 value.

        The XDM 2.0 data model has no maps, arrays, or function items; a
        native 2.0 profile cannot bind, store, or deliver one.  Such values
        can only enter evaluation through the host boundary (supplied
        parameters, bound variables, or Python customization callbacks), so
        the profile owns this check and the seams that accept host values
        call it.  The error is a type error: XSLT 2.0 has no code for an item
        kind its data model does not define.
        """
        if not self.native:
            return
        for item in value:
            if isinstance(item, (XdmMap, XdmArray, XdmFunction)):
                raise XsltDynamicError(
                    f"{label} contains an item kind (map, array, or function item) "
                    "the XDM 2.0 data model of a native XSLT 2.0 processor does not define",
                    code="XPTY0004",
                )

    def check_legacy_node_set(self, value: Iterable[XdmItem], label: str) -> None:
        """Reject a result tree fragment where a node-set is required.

        XSLT 1.0 §11.1 permits string and boolean conversion of a result tree
        fragment but no node-set operation.  A ``select`` expression that
        delivers a fragment to ``xsl:apply-templates`` or ``xsl:for-each``
        therefore fails instead of exposing the fragment's root node.
        """
        if not self.legacy:
            return
        for item in value:
            if isinstance(item, ResultTreeFragment):
                raise XsltDynamicError(
                    f"{label} requires a node-set, but a result tree fragment is "
                    "present; a native XSLT 1.0 processor does not convert a "
                    "result tree fragment where a node-set is required",
                    code="XPTY0004",
                )

    def _atomize(self, item: XdmItem) -> tuple[XdmAtomic, ...]:
        if isinstance(item, XdmAtomic):
            return (item,)
        if isinstance(item, XdmNode):
            # XDM guarantees node atomization yields atomic values.
            return cast(tuple[XdmAtomic, ...], tuple(item.typed_value))
        raise XsltDynamicError(
            "a function item cannot be atomized for string conversion",
            code="FOTY0013",
        )

    def string_sequence(self, sequence: XdmSequence) -> str:
        """Convert one value to a string using this profile's conversion rules.

        In compatibility mode the sequence is atomized and only its first item is
        converted, as XSLT 1.0 behavior requires for attribute value templates
        and separator-free ``xsl:value-of``.  A native XSLT 1.0 processor uses
        the legacy conversions, where a node-set converts through its first
        node in document order and a result tree fragment through its root.
        Otherwise a multi-item sequence is a type error and an empty sequence
        produces a zero-length string.
        """
        if self.legacy:
            return legacy_string_sequence(sequence)
        atomized: list[XdmAtomic] = []
        for item in sequence:
            atomized.extend(self._atomize(item))
        if not atomized:
            return ""
        if not self.compatibility_mode and len(atomized) > 1:
            raise XsltDynamicError(
                "string conversion requires a singleton sequence",
                code="XPTY0004",
            )
        return atomized[0].string_value


def _property_name(value: str) -> tuple[str | None, str]:
    if not isinstance(value, str) or not value:
        return None, ""
    if value.startswith("Q{"):
        close = value.find("}")
        if close < 0:
            return None, ""
        return value[2:close], value[close + 1 :]
    if value.startswith("{"):
        close = value.find("}")
        if close < 0:
            return None, ""
        return value[1:close], value[close + 1 :]
    prefix, separator, local = value.partition(":")
    if not separator:
        return None, value
    if prefix != "xsl":
        return None, local
    return XSLT_NS, local


def system_property_for(version: str, processor: str = "3.0") -> str:
    """Return ``system-property('xsl:version')`` for one processor/version pair."""
    return LanguageProfile.for_processor(processor, version).system_property("xsl:version")


__all__ = [
    "DOCUMENT_FRAGMENT_IDENTIFIER",
    "DOCUMENT_RETRIEVAL",
    "XSLT20_RECOVERY_RULES",
    "XSLT20_REGEX_FLAGS",
    "XSLT30_RECOVERY_RULES",
    "XSLT30_REGEX_FLAGS",
    "LanguageProfile",
    "RecoveryPolicy",
    "RecoveryRule",
    "system_property_for",
]
