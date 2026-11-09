"""Scalar conversion between XSD values and the shared JSON value contract."""

from __future__ import annotations

import decimal
import math
from typing import Any, NoReturn

from pyxsd.data_conversion.contracts import FormatName, JsonValue, raise_conversion
from pyxsd.data_conversion.names import expanded_to_eqname
from pyxsd.exceptions import PyXSDError
from pyxsd.xsd_data_types import (
    Boolean,
    QName,
    String,
    TypeList,
    XsdDataType,
    XsdList,
    _ListString,
)


def _type_name(datatype: type[Any]) -> str:
    return getattr(datatype, "name", None) or datatype.__name__


def selected_value(value: Any) -> Any:
    """Unwrap the selected member, including nested restricted unions."""
    while getattr(type(value), "_unionMembers", None):
        # Nilled union shells have no selected scalar member.
        value = getattr(value, "memberValue", None)
    return value


def _invalid_scalar(value: Any, datatype: type[Any], expected: str) -> NoReturn:
    raise_conversion(
        "scalar-type-invalid",
        "",
        f"expected {expected} for xs:{_type_name(datatype)}, got {type(value).__name__}",
    )


def scalar_to_json(value: Any, *, format: FormatName, typed: bool = True) -> JsonValue:
    """Convert one runtime XSD scalar to its JSON-safe representation.

    Integer types are recognized before the decimal family, booleans before
    integers, and finite floats before the non-finite lexical spellings.
    ``typed=False`` keeps the native lexical form, including XSD list
    strings, instead of the typed JSON value.
    """
    if value is None:
        return None

    if not typed:
        if isinstance(value, XsdDataType):
            return value.lexical()
        if isinstance(value, str):
            return value
        raise_conversion(
            "scalar-type-invalid",
            "",
            f"expected a lexical string, got {type(value).__name__}",
        )

    value = selected_value(value)
    if isinstance(value, Boolean):
        return int(value) if format == "pyxsd" else bool(int(value))
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, decimal.Decimal):
        # Fixed point, never binary float or exponent notation.
        return str(value) if format == "pyxsd" else f"{value:f}"
    if isinstance(value, float):
        number = float(value)
        if math.isnan(number):
            return "NaN"
        if math.isinf(number):
            return "INF" if number > 0 else "-INF"
        return number
    if isinstance(value, _ListString):
        return [str(token) for token in value.tokens]
    if isinstance(value, list):
        return [scalar_to_json(item, format=format, typed=typed) for item in value]
    if isinstance(value, QName):
        if format != "pyxsd-json":
            return str(value)
        try:
            return expanded_to_eqname(value.to_expanded_name(strict=True))
        except ValueError as exc:
            raise_conversion("namespace-unresolved", "", str(exc))
    if isinstance(value, XsdDataType):
        return value.lexical()
    if isinstance(value, str):
        return value
    raise_conversion(
        "scalar-type-invalid",
        "",
        f"cannot convert {type(value).__name__} to a JSON scalar",
    )


def scalar_to_lexical(
    value: JsonValue,
    *,
    datatype: type[Any],
    format: FormatName,
    typed: bool = True,
) -> str:
    """Convert one JSON scalar back to the XSD lexical form for *datatype*.

    The new formats require the fixed scalar contract: booleans are not
    integers, integers are not floats, decimals are strings, and XSD lists
    are arrays. Native ``pyxsd`` retains its lexical-string compatibility
    exception; ``typed=False`` requires lexical strings for every format.
    """
    if not typed:
        if isinstance(value, str):
            return value
        _invalid_scalar(value, datatype, "a lexical string")

    if format == "pyxsd" and isinstance(value, str):
        return value

    members = getattr(datatype, "_unionMembers", None)
    if members:
        fallback = None
        for member in members:
            try:
                lexical = scalar_to_lexical(value, datatype=member, format=format)
            except PyXSDError:
                continue
            if fallback is None:
                fallback = lexical
            try:
                member(lexical)
            except (TypeError, ValueError):
                continue
            return lexical
        if fallback is not None:
            # Correct representation, invalid XSD lexical value: let the binder report it.
            return fallback
        _invalid_scalar(value, datatype, "a value accepted by a union member")

    if issubclass(datatype, Boolean):
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, Boolean):
            return "true" if int(value) else "false"
        if format == "pyxsd" and type(value) is int and value in (0, 1):
            return "true" if value else "false"
        _invalid_scalar(value, datatype, "a JSON boolean")

    if issubclass(datatype, int):
        if isinstance(value, (bool, Boolean)) or not isinstance(value, int):
            _invalid_scalar(value, datatype, "a JSON integer")
        return str(int(value))

    if issubclass(datatype, decimal.Decimal):
        if format == "pyxsd":
            if isinstance(value, (bool, Boolean)):
                _invalid_scalar(value, datatype, "a decimal string")
            if isinstance(value, decimal.Decimal):
                return f"{value:f}"
            if isinstance(value, int):
                return str(int(value))
            _invalid_scalar(value, datatype, "a decimal string")
        if isinstance(value, str):
            return value
        _invalid_scalar(value, datatype, "a JSON decimal string")

    if issubclass(datatype, float):
        if isinstance(value, (bool, Boolean)):
            _invalid_scalar(value, datatype, "a JSON number")
        if isinstance(value, int):
            try:
                return str(float(value))
            except OverflowError:
                _invalid_scalar(value, datatype, "a finite JSON number")
        if isinstance(value, float):
            number = float(value)
            if not math.isfinite(number):
                _invalid_scalar(value, datatype, "a finite JSON number or INF/-INF/NaN string")
            return str(number)
        if isinstance(value, str):
            if value in ("INF", "-INF", "NaN"):
                return value
            _invalid_scalar(value, datatype, "a JSON number or INF/-INF/NaN string")
        _invalid_scalar(value, datatype, "a JSON number or INF/-INF/NaN string")

    if issubclass(datatype, (XsdList, TypeList, _ListString)):
        if not isinstance(value, list):
            _invalid_scalar(value, datatype, "a JSON array")
        item_type = datatype.itemType if issubclass(datatype, XsdList) else String
        return " ".join(
            scalar_to_lexical(item, datatype=item_type, format=format, typed=True) for item in value
        )

    if isinstance(value, str):
        return value
    _invalid_scalar(value, datatype, "a JSON string")
