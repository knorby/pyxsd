"""Small, neutral views over generated CDA datatype classes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pyxsd import Extensions

CDA = "urn:hl7-org:v3"
SYSTEMS = {
    "2.16.840.1.113883.6.1": "http://loinc.org",
    "2.16.840.1.113883.6.96": "http://snomed.info/sct",
    "2.16.840.1.113883.6.88": "http://www.nlm.nih.gov/research/umls/rxnorm",
    "2.16.840.1.113883.6.8": "http://unitsofmeasure.org",
    "2.16.840.1.113883.4.1": "http://hl7.org/fhir/sid/us-ssn",
    "2.16.840.1.113883.4.6": "http://hl7.org/fhir/sid/us-npi",
}


def attr(node, name: str) -> str | None:
    value = getattr(node, name, None)
    return None if value is None else str(value)


def children(node, name: str) -> list:
    return [n for n in getattr(node, "_children_", ()) if n._name_ == f"{{{CDA}}}{name}"]


def child(node, name: str):
    return next(iter(children(node, name)), None)


def text(node) -> str:
    if node is None:
        return ""
    value = getattr(node, "_value_", "") or ""
    leading = "".join(map(str, value)) if isinstance(value, list) else str(value)
    return leading + "".join(text(n) + str(getattr(n, "_tail_", "") or "") for n in node._children_)


def system_uri(root: str | None) -> str | None:
    return SYSTEMS.get(root, f"urn:oid:{root}" if root else None)


@dataclass(frozen=True)
class IdentifierValue:
    system: str | None
    value: str | None
    root: str | None
    extension: str | None
    assigning_authority: str | None
    null_flavor: str | None


@dataclass(frozen=True)
class DateTimeValue:
    lexical: str | None
    precision: str | None
    timezone: str | None
    null_flavor: str | None


@dataclass(frozen=True)
class CodingValue:
    system: str | None
    code: str | None
    display: str | None
    text: str | None
    translations: tuple[CodingValue, ...]
    known_system: bool
    null_flavor: str | None


@dataclass(frozen=True)
class QuantityValue:
    value: Decimal | None
    unit: str | None
    null_flavor: str | None


def datetime_value(raw: str | None, null_flavor: str | None = None) -> DateTimeValue:
    """Preserve precision separately; padded clock fields are lexical only.

    A timezone-free clock remains timezone-free and must not be used as a
    FHIR instant. Calendar checks reject invalid dates rather than repairing them.
    """
    if null_flavor or not raw:
        return DateTimeValue(None, None, None, null_flavor)
    match = re.fullmatch(r"(\d{4}(?:\d{2}){0,5})(\.\d+)?([+-]\d{4})?", raw)
    if not match:
        raise ValueError(f"Unsupported CDA timestamp: {raw}")
    digits, fraction, offset = match.groups()
    length = len(digits)
    if (fraction and length != 14) or (offset and length < 10):
        raise ValueError(f"Unsupported CDA timestamp: {raw}")
    # Padding here validates partial calendar values, not clinical precision.
    datetime.strptime(digits + "0101000000"[max(0, length - 4) :], "%Y%m%d%H%M%S")
    zone = None
    if offset:
        hours, minutes = int(offset[1:3]), int(offset[3:])
        if hours > 14 or minutes > 59 or (hours == 14 and minutes):
            raise ValueError(f"Invalid CDA timezone: {offset}")
        zone = offset[:3] + ":" + offset[3:]
    lexical = digits[:4]
    if length >= 6:
        lexical += "-" + digits[4:6]
    if length >= 8:
        lexical += "-" + digits[6:8]
    if length >= 10:
        lexical += (
            "T" + digits[8:10] + ":" + (digits[10:12] or "00") + ":" + (digits[12:14] or "00")
        )
    lexical += (fraction or "") + (zone or "")
    precision = (
        "fraction"
        if fraction
        else {4: "year", 6: "month", 8: "day", 10: "hour", 12: "minute", 14: "second"}[length]
    )
    return DateTimeValue(lexical, precision, zone, None)


class IdentifierMixin:
    def as_identifier(self) -> IdentifierValue:
        root, extension, null = (
            attr(self, "root"),
            attr(self, "extension"),
            attr(self, "nullFlavor"),
        )
        return IdentifierValue(
            system_uri(root),
            None if null else extension or root,
            root,
            extension,
            attr(self, "assigningAuthorityName"),
            null,
        )


class TimestampMixin:
    def as_datetime(self) -> DateTimeValue:
        return datetime_value(attr(self, "value"), attr(self, "nullFlavor"))


class CodingMixin:
    def as_coding(self) -> CodingValue:
        root, null = attr(self, "codeSystem"), attr(self, "nullFlavor")
        return CodingValue(
            system_uri(root),
            None if null else attr(self, "code"),
            attr(self, "displayName"),
            text(child(self, "originalText")) or None,
            tuple(n.as_coding() for n in children(self, "translation")),
            root in SYSTEMS,
            null,
        )


class QuantityMixin:
    def as_quantity(self) -> QuantityValue:
        raw, null = attr(self, "value"), attr(self, "nullFlavor")
        return QuantityValue(
            None if null or raw is None else Decimal(raw), attr(self, "unit"), null
        )


def cda_extensions() -> Extensions:
    extensions = Extensions()
    for name, mixin in (
        ("II", IdentifierMixin),
        ("TS", TimestampMixin),
        ("CD", CodingMixin),
        ("PQ", QuantityMixin),
    ):
        extensions.for_type(f"{{{CDA}}}{name}", mixin=mixin)
    return extensions
