"""The schema-location hints an instance document carries.

``xsi:schemaLocation`` and ``xsi:noNamespaceSchemaLocation`` are how an
instance document points at the schema it should be validated against.
This module reads those hints off a parsed instance root: the
location/namespace/tag lookups, the full list of hint pairs, and the
resolve-a-hint-to-a-schema-path step used by the CLI and the
:func:`pyxsd.parse` convenience flow, with ``clark()`` expanding the
attribute names and the warning routed through
:func:`pyxsd.validation.report_or_log` so callers thread their own
report (instance-phase code never writes ``schema.report`` directly).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pyxsd.namespaces import XSI_NS, clark
from pyxsd.validation import IssueSeverity, ValidationReport, report_or_log


def schema_location_info(
    xml_root: Any,
    name_or_location: str,
    *,
    report: ValidationReport | None = None,
) -> str | None:
    """Extracts information from the *schemaLocation* tag or the
    *noNamespaceSchemaLocation* tag.

    Depending on the value of the parameter ``name_or_location``, the
    function outputs the namespace, the schema location, or the tag
    type. This function is meant for use with other functions to
    easily grab bits of data that are used in various locations in
    the program.

    - ``name_or_location``: a one letter string that is either 'l',
      'n', or 't'. If the variable is 'l', the location of the
      schema is returned. If it is 'n', the namespace is returned,
      if there is one. 't' returns the tag name to indicate if the
      xml uses *schemaLocation* or *noNamespaceSchemaLocation*.

    - ``report``: the report a malformed-pairs warning is recorded
      on; without one the warning is logged instead. Reading the
      hints may rewrite the root's attributes: a ``schemaLocation``
      whose pairs do not alternate namespace/location is converted
      to a ``noNamespaceSchemaLocation`` carrying the same value.
    """
    schemaLocationSplit = None
    if clark(XSI_NS, "schemaLocation") in xml_root.attrib:
        schemaLocationTag = xml_root.attrib[clark(XSI_NS, "schemaLocation")]
        schemaLocationSplit = schemaLocationTag.split()

        if not schemaLocationSplit or len(schemaLocationSplit) % 2 != 0:
            message = (
                "the 'schemaLocation' tag must be one or more "
                "namespace/location pairs separated by whitespace, with "
                "each namespace stated first, followed by the location of "
                "the schema; attempting to use the "
                "'noNamespaceSchemaLocation' tag instead"
            )
            report_or_log(report, IssueSeverity.WARNING, code="schema-hint", message=message)
            del xml_root.attrib[clark(XSI_NS, "schemaLocation")]
            xml_root.attrib[clark(XSI_NS, "noNamespaceSchemaLocation")] = schemaLocationTag

    if clark(XSI_NS, "noNamespaceSchemaLocation") in xml_root.attrib:
        if name_or_location == "t":
            return clark(XSI_NS, "noNamespaceSchemaLocation")
        if name_or_location == "n":
            return None
        if name_or_location == "l":
            return xml_root.attrib[clark(XSI_NS, "noNamespaceSchemaLocation")]

    if schemaLocationSplit is None:
        # No schema location information in the xml file.
        return None

    schemaNS = schemaLocationSplit[0]
    if name_or_location == "n":
        return schemaNS

    schemaLocation = schemaLocationSplit[1]
    if name_or_location == "l":
        return schemaLocation

    if name_or_location == "t":
        return clark(XSI_NS, "schemaLocation")

    return None


def schema_location_pairs(xml_root: Any) -> list[tuple[str | None, str]]:
    """Returns every ``(namespace, location)`` hint in the instance.

    ``xsi:schemaLocation`` may carry multiple namespace/location
    pairs; ``xsi:noNamespaceSchemaLocation`` yields a single pair
    with ``None`` for the namespace.
    """
    pairs: list[tuple[str | None, str]] = []
    schemaLocationTag = xml_root.attrib.get(clark(XSI_NS, "schemaLocation"))
    if schemaLocationTag:
        tokens = schemaLocationTag.split()
        if len(tokens) % 2 == 0:
            for i in range(0, len(tokens), 2):
                pairs.append((tokens[i] or None, tokens[i + 1]))
    noNamespace = xml_root.attrib.get(clark(XSI_NS, "noNamespaceSchemaLocation"))
    if noNamespace:
        pairs.append((None, noNamespace))
    return pairs


def absolute_schema_location_pairs(xml_root: Any, base_dir: Path) -> list[tuple[str | None, str]]:
    """The instance's schema-location pairs as absolute paths.

    Hint locations are documented relative to the instance document,
    so callers that forward the pairs into a schema compilation
    resolve them against *base_dir* (the instance document's
    directory) before handing them on; a compilation resolves the
    schema's own relative locations against its own directory, which
    is not the same directory when the schema was named explicitly.
    """
    return [
        (namespace, location if Path(location).is_absolute() else str(Path(base_dir) / location))
        for namespace, location in schema_location_pairs(xml_root)
    ]


def resolve_schema_hint(
    xml_root: Any,
    base_dir: Path,
    *,
    report: ValidationReport | None = None,
) -> tuple[str | None, Path | None]:
    """Resolves the instance's schema hint to ``(namespace, path)``.

    Returns ``(None, None)`` when the instance carries no schema
    location hint at all.

    - ``base_dir``: the directory a relative hint is resolved
      against — the instance document's own directory, or the
      working directory when the instance came from a stream.
      Schema hints are documented relative to the instance
      document, not the caller's working directory.

    - ``report``: the report a malformed-pairs warning is recorded
      on; without one the warning is logged instead.
    """
    location = schema_location_info(xml_root, "l", report=report)
    if location is None:
        return None, None
    namespace = schema_location_info(xml_root, "n", report=report)
    hint = Path(location)
    if not hint.is_absolute():
        hint = base_dir / hint
    return namespace, hint


__all__ = [
    "XSI_NS",
    "resolve_schema_hint",
    "schema_location_info",
    "schema_location_pairs",
]
