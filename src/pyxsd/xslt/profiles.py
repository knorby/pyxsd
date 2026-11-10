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

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from pyxsd.xdm import XdmAtomic, XdmItem, XdmSequence
from pyxsd.xdm.nodes import XdmNode
from pyxsd.xslt.errors import XsltDynamicError, normalized_error_code

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"

_PROCESSOR_VERSIONS = frozenset({"1.0", "2.0", "3.0", "3.1"})


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
    def compatibility_mode(self) -> bool:
        """Whether XPath 1.0 compatibility behavior applies to this element."""
        if self.native:
            return _decimal_version(self.processor_version, "processor_version") < Decimal("2.0")
        return _decimal_version(self.effective_version, "effective_version") == Decimal("1.0")

    @property
    def xpath_version(self) -> str:
        """The normative XPath version selected by the processor profile."""
        if self.processor_version == "1.0":
            return "1.0"
        if self.processor_version == "2.0":
            return "2.0"
        return "3.1"

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
        """Return the profile's value for one ``xl:*`` system property."""
        uri, local = _property_name(name)
        if uri != XSLT_NS:
            return ""
        if local == "version":
            return self.processor_version
        if local == "xpath-version":
            return self.xpath_version
        if local == "is-schema-aware":
            return "yes" if self.schema_aware else "no"
        if local == "supports-backwards-compatibility":
            return "no" if self.native else "yes"
        return ""

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
        and separator-free ``xsl:value-of``.  Otherwise a multi-item sequence is
        a type error and an empty sequence produces a zero-length string.
        """
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
        return atomized[0].lexical


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
    "XSLT30_RECOVERY_RULES",
    "LanguageProfile",
    "RecoveryPolicy",
    "RecoveryRule",
    "system_property_for",
]
