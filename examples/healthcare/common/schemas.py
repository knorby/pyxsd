"""Call these factories once per CLI invocation, then reuse the schemas."""

from pyxsd import Schema
from pyxsd.binding import ParseModes

from .cda_datatypes import cda_extensions
from .sources import cda_schema_path, fhir_schema_path

MODE = ParseModes.LAX.replace(namespaces="strict")


def compile_cda(*, with_mixins: bool = True) -> Schema:
    return Schema.compile(
        cda_schema_path(), mode=MODE, extensions=cda_extensions() if with_mixins else None
    ).require_valid()


def compile_fhir() -> Schema:
    return Schema.compile(fhir_schema_path(), mode=MODE).require_valid()
