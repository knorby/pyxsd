"""RFC 3339 format assertions for the native JSON Schema evaluator."""

from __future__ import annotations

import re
from calendar import monthrange
from importlib import import_module
from threading import Lock
from typing import Any

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


def assertion_checker() -> Any:
    """Return the shared Draft 2020-12 checker with pyxsd's RFC 3339 checks."""
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
            _checker = checker
        return _checker
