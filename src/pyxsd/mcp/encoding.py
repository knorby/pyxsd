"""Lossless scalar presentation and explicit core-record JSON encoding."""

import base64
import datetime as dt
import math
from dataclasses import fields
from decimal import Decimal
from xml.etree.ElementTree import QName

from pydantic import JsonValue
from pyxsd.introspection import (
    ComponentDescription,
    FacetDescription,
    IdentityDescription,
    MemberDescription,
    ParticleDescription,
    SchemaDescription,
    WildcardDescription,
)
from pyxsd.locations import ConstraintInfo, IssueLocation, PathStep
from pyxsd.xsd_data_types import Boolean

from .contracts import WireModel
from .errors import ToolError

_CORE_RECORDS = (
    PathStep,
    IssueLocation,
    ConstraintInfo,
    ComponentDescription,
    FacetDescription,
    IdentityDescription,
    MemberDescription,
    ParticleDescription,
    SchemaDescription,
    WildcardDescription,
)


def encode_value(value: object, *, max_depth: int = 64) -> JsonValue:
    """Encode supported values without repr, decimal rounding or silent truncation.

    Decimal and unsafe integers become strings; bytes have an explicit base64
    envelope. XML lexical strings (including timezone and submicrosecond precision)
    stay lexical. QName strings are not claimed to carry resolved prefix bindings.
    Only registered core dataclasses, not arbitrary application objects, are exposed.
    """
    if max_depth <= 0:
        raise ValueError("max_depth must be positive")
    return _encode(value, max_depth, set())


def _encode(value: object, depth: int, active: set[int]) -> JsonValue:
    if depth < 0 or id(value) in active:
        raise ToolError("unsupported_value", "Cyclic or excessively nested value")
    if value is None:
        return None
    if isinstance(value, (bool, Boolean)):
        return bool(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, int):
        return int(value) if abs(value) <= 2**53 - 1 else str(Decimal(value))
    if isinstance(value, (float, Decimal)):
        if isinstance(value, Decimal):
            if value.is_finite():
                return str(value)
        elif math.isfinite(value):
            return float(value)
        raise ToolError("unsupported_value", "Nonfinite numeric value")
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, QName):
        return str(value)
    active.add(id(value))
    try:
        if isinstance(value, (list, tuple)):
            return [_encode(item, depth - 1, active) for item in value]
        if isinstance(value, dict) and all(isinstance(key, str) for key in value):
            return {str(key): _encode(item, depth - 1, active) for key, item in value.items()}
        if isinstance(value, _CORE_RECORDS):
            return {
                field.name: _encode(getattr(value, field.name), depth - 1, active)
                for field in fields(value)
            }
        if isinstance(value, WireModel):
            return _encode(value.model_dump(), depth - 1, active)
        raise ToolError("unsupported_value", "Value has no supported wire representation")
    finally:
        active.remove(id(value))
