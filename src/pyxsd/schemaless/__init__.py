"""Parse XML into a schema-neutral lexical tree."""

from pyxsd.schemaless.document import SchemalessDocument
from pyxsd.schemaless.errors import ScalarTypeError, SchemalessParseError, XmlMappingError
from pyxsd.schemaless.json_document import JsonDocument, from_data, from_json
from pyxsd.schemaless.mapping import MappingRule, XmlMapping
from pyxsd.schemaless.nodes import NodeMapping, attribute, child, children, text
from pyxsd.schemaless.parser import parse_xml
from pyxsd.schemaless.profile import DocumentProfile, PathProfile, ProfileLimits, TypeCandidate
from pyxsd.schemaless.tree import SchemalessElement, SchemalessRevision
from pyxsd.schemaless.types import ScalarAdapter, TypePolicy

__all__ = [
    "DocumentProfile",
    "JsonDocument",
    "MappingRule",
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
