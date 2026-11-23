"""Parse XML into a schema-neutral lexical tree."""

from pyxsd.schemaless.errors import SchemalessParseError
from pyxsd.schemaless.parser import parse_xml
from pyxsd.schemaless.tree import SchemalessElement, SchemalessRevision

__all__ = ["SchemalessElement", "SchemalessParseError", "SchemalessRevision", "parse_xml"]
