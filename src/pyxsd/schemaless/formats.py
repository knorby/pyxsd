"""Format assertions for the native JSON Schema evaluator."""

from __future__ import annotations

import base64
import re
import struct
from binascii import Error as BinasciiError
from calendar import monthrange
from decimal import Decimal
from importlib import import_module
from ipaddress import IPv4Address, IPv6Address
from math import isfinite
from threading import Lock
from typing import Any
from uuid import UUID

from pyxsd.schemaless.json_values import _pointer_token

_FULL_DATE = re.compile(r"([0-9]{4})-(0[1-9]|1[0-2])-([0-9]{2})")
_HOUR = r"(?:[01][0-9]|2[0-3])"
_MINUTE = r"[0-5][0-9]"
# Allow the leap-second spelling without consulting a leap-second schedule.
_FULL_TIME = re.compile(
    rf"{_HOUR}:{_MINUTE}:(?:[0-5][0-9]|60)(?:\.[0-9]+)?(?:[Zz]|[+-]{_HOUR}:{_MINUTE})"
)
_INT64_TEXT = re.compile(r"-?[0-9]+")
# At this midpoint, round-to-nearest ties toward the overflowing power of two.
_BINARY32_OVERFLOW = 2**128 - 2**103

_checker_lock = Lock()
_checker: Any = None


def is_rfc3339_date(value: object) -> bool:
    """Check full-date syntax and the Gregorian day range."""
    if not isinstance(value, str):
        return True
    match = _FULL_DATE.fullmatch(value)
    if match is None:
        return False
    year, month, day = map(int, match.groups())
    return 1 <= day <= monthrange(year, month)[1]


def is_rfc3339_date_time(value: object) -> bool:
    """Check full-date and full-time joined by an RFC 3339 separator."""
    if not isinstance(value, str):
        return True
    return (
        value[10:11] in ("T", "t") and is_rfc3339_date(value[:10]) and is_rfc3339_time(value[11:])
    )


def is_rfc3339_time(value: object) -> bool:
    """Check full-time syntax, including the required UTC offset."""
    return not isinstance(value, str) or _FULL_TIME.fullmatch(value) is not None


def is_uuid(value: object) -> bool:
    """Check the plain hyphenated RFC 4122 spelling, ignoring hex case."""
    if not isinstance(value, str):
        return True
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    # Match the canonical hyphen positions and reject other spellings UUID accepts.
    return str(parsed) == value.lower()


def is_ipv4(value: object) -> bool:
    """Check dotted-decimal IPv4 syntax."""
    if not isinstance(value, str):
        return True
    try:
        IPv4Address(value)
    except ValueError:
        return False
    return True


def is_ipv6(value: object) -> bool:
    """Check IPv6 syntax without a scope ID."""
    if not isinstance(value, str):
        return True
    try:
        return IPv6Address(value).scope_id is None
    except ValueError:
        return False


def is_json_pointer(value: object) -> bool:
    """Check RFC 6901 pointer syntax without resolving it against a document."""
    if not isinstance(value, str) or value == "":
        return True
    if not value.startswith("/"):
        return False
    try:
        for raw in value.split("/")[1:]:
            _pointer_token(raw, value)
    except ValueError:
        return False
    return True


def is_regex(value: object) -> bool:
    """Check whether Python's ``re`` can compile the pattern, not ECMA-262 syntax."""
    if not isinstance(value, str):
        return True
    try:
        re.compile(value)
    except (re.error, OverflowError, RecursionError, ValueError):
        return False
    return True


def _is_bounded_integer(value: object, limit: int) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return -limit <= value < limit
    if isinstance(value, Decimal):
        # Compare first so tiny or huge exponents never need integer expansion.
        return value.is_finite() and -limit <= value < limit and value == value.to_integral_value()
    if isinstance(value, float):
        return value.is_integer() and -limit <= value < limit
    return True


def is_int32(value: object) -> bool:
    """Check whether a number is a signed 32-bit integer."""
    return _is_bounded_integer(value, 2**31)


def is_int64(value: object) -> bool:
    """Check signed 64-bit integers, including ASCII decimal strings."""
    if isinstance(value, str):
        if _INT64_TEXT.fullmatch(value) is None:
            return False
        sign = "-" if value.startswith("-") else ""
        digits = value[len(sign) :].lstrip("0") or "0"
        if len(digits) > 19:
            return False
        value = int(sign + digits)
    return _is_bounded_integer(value, 2**63)


def is_float(value: object) -> bool:
    """Check whether a number rounds to a finite binary32 value."""
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, Decimal)):
        if isinstance(value, Decimal) and not value.is_finite():
            return False
        # Conversion to binary64 first can round an exact value to the midpoint.
        return -_BINARY32_OVERFLOW < value < _BINARY32_OVERFLOW
    if not isinstance(value, float):
        return True
    try:
        return isfinite(struct.unpack(">f", struct.pack(">f", value))[0])
    except OverflowError:
        return False


def is_double(value: object) -> bool:
    """Check whether a number rounds to a finite binary64 value."""
    if isinstance(value, bool) or not isinstance(value, (int, Decimal, float)):
        return True
    try:
        return isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def is_base64(value: object) -> bool:
    """Check canonical RFC 4648 base64 without whitespace."""
    if not isinstance(value, str):
        return True
    if not value.isascii() or re.search(r"\s", value):
        return False
    try:
        decoded = base64.b64decode(value, validate=True)
    except BinasciiError:
        return False
    return base64.b64encode(decoded).decode("ascii") == value


def assertion_checker() -> Any:
    """Return the shared Draft 2020-12 checker with pyxsd's format checks."""
    global _checker
    # Unlike a cache, the lock also guarantees identity on concurrent first use.
    with _checker_lock:
        if _checker is None:
            api = import_module("jsonschema")
            checker = api.FormatChecker(formats=())
            checker.checkers.update(api.Draft202012Validator.FORMAT_CHECKER.checkers)
            checker.checks("date")(is_rfc3339_date)
            checker.checks("date-time")(is_rfc3339_date_time)
            checker.checks("time")(is_rfc3339_time)
            checker.checks("uuid")(is_uuid)
            checker.checks("ipv4")(is_ipv4)
            checker.checks("ipv6")(is_ipv6)
            checker.checks("json-pointer")(is_json_pointer)
            checker.checks("regex")(is_regex)
            checker.checks("int32")(is_int32)
            checker.checks("int64")(is_int64)
            checker.checks("float")(is_float)
            checker.checks("double")(is_double)
            checker.checks("byte")(is_base64)
            _checker = checker
        return _checker
