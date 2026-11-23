"""Parse XML into a schema-neutral lexical tree."""

from pyxsd.schemaless.document import SchemalessDocument
from pyxsd.schemaless.errors import ScalarTypeError, SchemalessParseError
from pyxsd.schemaless.json_document import JsonDocument, from_data, from_json
from pyxsd.schemaless.parser import parse_xml
from pyxsd.schemaless.profile import DocumentProfile, PathProfile, ProfileLimits, TypeCandidate
from pyxsd.schemaless.tree import SchemalessElement, SchemalessRevision
from pyxsd.schemaless.types import ScalarAdapter, TypePolicy

__all__ = [
    "DocumentProfile",
    "JsonDocument",
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
    "from_data",
    "from_json",
    "parse_xml",
]
