"""String format assertions for the native JSON Schema evaluator."""

from __future__ import annotations

import re
from calendar import monthrange
from importlib import import_module
from ipaddress import IPv4Address, IPv6Address
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
            _checker = checker
        return _checker
