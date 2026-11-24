"""Schemaless XML and native JSON documents with explicit XML projection.

Optional JSON Schema and OpenAPI guidance loads only when requested, so the
native XML, native JSON, and explicit mapping paths have no optional import.
"""

from pyxsd.schemaless.document import SchemalessDocument
from pyxsd.schemaless.errors import (
    JsonLimitError,
    JsonParseError,
    JsonSchemaError,
    ScalarTypeError,
    SchemalessParseError,
    XmlMappingError,
)
from pyxsd.schemaless.json_document import JsonDocument, from_data, from_json
from pyxsd.schemaless.json_values import JsonLimits, NativeJsonValue
from pyxsd.schemaless.mapping import MappingRule, XmlMapping
from pyxsd.schemaless.nodes import NodeMapping, attribute, child, children, text
from pyxsd.schemaless.parser import parse_xml
from pyxsd.schemaless.profile import DocumentProfile, PathProfile, ProfileLimits, TypeCandidate
from pyxsd.schemaless.tree import SchemalessElement, SchemalessRevision
from pyxsd.schemaless.types import ScalarAdapter, TypePolicy

__all__ = [
    "DocumentProfile",
    "JsonDocument",
    "JsonLimitError",
    "JsonLimits",
    "JsonParseError",
    "JsonSchemaError",
    "MappingRule",
    "NativeJsonValue",
    "NodeMapping",
    "PathProfile",
    "ProfileLimits",
    "ScalarAdapter",
    "ScalarTypeError",
    "SchemalessDocument",
    "SchemalessElement",
    "SchemalessParseError",
    "SchemalessRevision",
    "TypeCandidate",
    "TypePolicy",
    "XmlMapping",
    "XmlMappingError",
    "attribute",
    "child",
    "children",
    "from_data",
    "from_json",
    "parse_xml",
    "text",
]
