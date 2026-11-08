"""Call these factories once per CLI invocation, then reuse the schemas."""

from pyxsd import Schema
from pyxsd.binding import ParseModes

from .cda_datatypes import cda_extensions
from .sources import cda_schema_path, fhir_schema_path

MODE = ParseModes.LAX_NAMESPACED


def compile_cda(*, with_mixins: bool = True) -> Schema:
    schema = Schema.compile(
        cda_schema_path(), mode=MODE, extensions=cda_extensions() if with_mixins else None
    )
    schema.require_valid()
    return schema


def compile_fhir() -> Schema:
    schema = Schema.compile(fhir_schema_path(), mode=MODE)
    schema.require_valid()
    return schema
