"""Serialization parameters, validation, and layered output resolution.

The serializer is independent of the transformation engine: parameters arrive
as :class:`SerializationParameters`, and the raw ``xsl:output`` attribute
vocabulary is resolved here before any output method sees a value.  Every
invalid value or inconsistent combination is a coded ``SEPM0016`` error.
"""

from __future__ import annotations

import codecs
from collections.abc import Mapping
from dataclasses import dataclass, fields
from math import isfinite
from typing import Any

from pyxsd.xslt.errors import XsltSerializationError

__all__ = ["SerializationParameters", "method_local_name"]

_METHODS = frozenset({"xml", "xhtml", "html", "text"})
_NORMALIZATION_FORMS = frozenset({"NFC", "NFD", "NFKC", "NFKD"})
_TRUE_VALUES = frozenset({"yes", "true", "1"})
_FALSE_VALUES = frozenset({"no", "false", "0"})

#: XSLT-defined output attributes that do not feed the string serializer.
#: They are legal on ``xsl:output`` and must not be rejected when a caller
#: passes a complete effective output mapping.
_XSLT_OUTPUT_METADATA = frozenset(
    {
        "allow-duplicate-names",
        "build-tree",
        "html-version",
        "json-node-output-method",
        "parameter-document",
        "use-character-maps",
    }
)

_ATTRIBUTE_FIELDS: dict[str, str] = {
    "byte-order-mark": "byte_order_mark",
    "cdata-section-elements": "cdata_section_elements",
    "doctype-public": "doctype_public",
    "doctype-system": "doctype_system",
    "encoding": "encoding",
    "escape-uri-attributes": "escape_uri_attributes",
    "include-content-type": "include_content_type",
    "indent": "indent",
    "item-separator": "item_separator",
    "media-type": "media_type",
    "method": "method",
    "normalization-form": "normalization_form",
    "omit-xml-declaration": "omit_xml_declaration",
    "standalone": "standalone",
    "suppress-indentation": "suppress_indentation",
    "undeclare-prefixes": "undeclare_prefixes",
    "version": "version",
}


def _invalid(message: str, *, cause: BaseException | None = None) -> XsltSerializationError:
    return XsltSerializationError(message, code="SEPM0016", cause=cause)


def method_local_name(value: str) -> str:
    """The local part of an output-method EQName.

    ``Q{uri}html`` and the lexical QName ``xsl:html`` both compare as
    ``html``; an unprefixed name is already local.
    """
    if value.startswith("Q{"):
        close = value.find("}")
        if close >= 0:
            return value[close + 1 :]
    if ":" in value:
        return value.rsplit(":", 1)[1]
    return value


def _boolean(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
    raise _invalid(f"{label} must be a boolean, got {value!r}")


def _standalone(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "omit":
            return None
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
    raise _invalid(f"standalone must be yes, no, omit, or None, got {value!r}")


def _optional_text(value: Any, label: str, *, empty_is_none: bool = False) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid(f"{label} must be a string or None, got {value!r}")
    if empty_is_none and not value:
        return None
    return value


def _name_list(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(token for token in value.split() if token)
    if isinstance(value, (tuple, list)):
        tokens: list[str] = []
        for token in value:
            if not isinstance(token, str):
                raise _invalid(f"{label} entries must be strings, got {token!r}")
            if token:
                tokens.append(token)
        return tuple(tokens)
    raise _invalid(f"{label} must be a whitespace-separated string or sequence")


def _version(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise _invalid(f"version must be a non-negative number, got {value!r}")
    text = str(value).strip()
    if not text:
        raise _invalid("version must be a non-negative number")
    try:
        number = float(text)
    except ValueError as error:
        raise _invalid(f"version must be a non-negative number, got {value!r}") from error
    if number < 0 or not isfinite(number):
        raise _invalid(f"version must be a non-negative number, got {value!r}")
    return text


def _normalization_form(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid(f"normalization-form must be a string, got {value!r}")
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.lower() == "none":
        return "none"
    upper = normalized.upper()
    if upper not in _NORMALIZATION_FORMS:
        raise _invalid(f"unsupported normalization-form {value!r}")
    return upper


def _encoding(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(f"encoding must be a non-empty string, got {value!r}")
    try:
        codecs.lookup(value)
    except LookupError as error:
        raise _invalid(f"unknown encoding {value!r}", cause=error) from error
    return value


@dataclass(frozen=True, slots=True)
class SerializationParameters:
    """One resolved set of serialization parameters."""

    method: str = "xml"
    version: str = "1.0"
    encoding: str = "UTF-8"
    indent: bool = False
    omit_xml_declaration: bool = False
    standalone: bool | None = None
    doctype_public: str | None = None
    doctype_system: str | None = None
    media_type: str | None = None
    item_separator: str | None = None
    cdata_section_elements: tuple[str, ...] = ()
    normalization_form: str | None = None
    byte_order_mark: bool = False
    suppress_indentation: tuple[str, ...] = ()
    escape_uri_attributes: bool = True
    include_content_type: bool = True
    undeclare_prefixes: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.method, str):
            raise _invalid(f"method must be a string, got {self.method!r}")
        method = method_local_name(self.method)
        if method not in _METHODS:
            raise _invalid(f"unsupported serialization method {self.method!r}")
        object.__setattr__(self, "method", method)

        object.__setattr__(self, "version", _version(self.version))
        object.__setattr__(self, "encoding", _encoding(self.encoding))
        object.__setattr__(self, "indent", _boolean(self.indent, "indent"))
        object.__setattr__(
            self,
            "omit_xml_declaration",
            _boolean(self.omit_xml_declaration, "omit-xml-declaration"),
        )
        object.__setattr__(self, "standalone", _standalone(self.standalone))
        object.__setattr__(
            self,
            "doctype_public",
            _optional_text(self.doctype_public, "doctype-public", empty_is_none=True),
        )
        object.__setattr__(
            self,
            "doctype_system",
            _optional_text(self.doctype_system, "doctype-system", empty_is_none=True),
        )
        if self.doctype_public is not None and self.doctype_system is None:
            raise _invalid("doctype-public requires doctype-system")
        object.__setattr__(self, "media_type", _optional_text(self.media_type, "media-type"))
        object.__setattr__(
            self, "item_separator", _optional_text(self.item_separator, "item-separator")
        )
        object.__setattr__(
            self,
            "cdata_section_elements",
            _name_list(self.cdata_section_elements, "cdata-section-elements"),
        )
        object.__setattr__(self, "normalization_form", _normalization_form(self.normalization_form))
        object.__setattr__(
            self, "byte_order_mark", _boolean(self.byte_order_mark, "byte-order-mark")
        )
        object.__setattr__(
            self,
            "suppress_indentation",
            _name_list(self.suppress_indentation, "suppress-indentation"),
        )
        object.__setattr__(
            self,
            "escape_uri_attributes",
            _boolean(self.escape_uri_attributes, "escape-uri-attributes"),
        )
        object.__setattr__(
            self,
            "include_content_type",
            _boolean(self.include_content_type, "include-content-type"),
        )
        object.__setattr__(
            self, "undeclare_prefixes", _boolean(self.undeclare_prefixes, "undeclare-prefixes")
        )

    @classmethod
    def _resolve_field(cls, name: str) -> str | None:
        if name in _XSLT_OUTPUT_METADATA:
            return None
        field = _ATTRIBUTE_FIELDS.get(name)
        if field is None:
            for candidate in fields(cls):
                if name == candidate.name:
                    return candidate.name
            raise _invalid(f"unknown serialization attribute {name!r}")
        return field

    @classmethod
    def from_attributes(
        cls,
        attributes: Mapping[str, Any],
        *,
        base: SerializationParameters | None = None,
    ) -> SerializationParameters:
        """Build parameters from effective ``xsl:output``-style attributes."""
        values: dict[str, Any] = {}
        if base is not None:
            values.update({field.name: getattr(base, field.name) for field in fields(cls)})
        for name, value in attributes.items():
            field = cls._resolve_field(name)
            if field is not None:
                values[field] = value
        return cls(**values)

    @classmethod
    def merge(
        cls,
        named: Mapping[str, Any] | None = None,
        overrides: Mapping[str, Any] | None = None,
        explicit: Mapping[str, Any] | None = None,
        **keyword_arguments: Any,
    ) -> SerializationParameters:
        """Layer named output, result-document overrides, then explicit values."""
        values: dict[str, Any] = {}
        layers: tuple[Mapping[str, Any] | None, ...] = (
            named,
            overrides,
            explicit,
            keyword_arguments,
        )
        for layer in layers:
            if layer is None:
                continue
            for name, value in layer.items():
                field = cls._resolve_field(name)
                if field is not None:
                    values[field] = value
        return cls(**values)
