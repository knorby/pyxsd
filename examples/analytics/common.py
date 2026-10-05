"""Shared observation preparation for the analytics examples; requires pyxsd[arrow]."""

import json
from pathlib import Path

import pyarrow as pa

import pyxsd
from pyxsd.integrations.arrow import RecordProjection, records
from pyxsd.integrations.projection import FieldSource

ARROW_DATA = Path(__file__).resolve().parents[1] / "arrow"


def prepare_observations() -> tuple[
    pyxsd.Schema, pyxsd.Document, RecordProjection, RecordProjection
]:
    """Compile the fixture schema and prepare nested and contextual projections."""
    schema = pyxsd.compile(ARROW_DATA / "observations.xsd", mode=pyxsd.ParseModes.NAMESPACED)
    document = schema.parse(ARROW_DATA / "observations.xml")
    document.require_valid()
    nested = records(schema, element="observations", path=("observation",))
    contextual = records(
        schema,
        element="observations",
        path=("observation",),
        columns={
            "station": FieldSource(attribute="station"),
            "sequence": FieldSource(path=("sequence",)),
            "accession": FieldSource(path=("accession",)),
            "collected": FieldSource(path=("collected",)),
            "analyte": FieldSource(path=("analyte",)),
            "reading": FieldSource(path=("reading",)),
            "unit": FieldSource(path=("reading",), attribute="unit"),
            "quality": FieldSource(path=("quality",)),
        },
    )
    return schema, document, nested, contextual


def load_stations(path: Path) -> list[dict[str, str]]:
    """Load station reference data, failing on duplicate keys before any join."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    stations: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in payload["stations"]:
        key = entry["station"]
        if key in seen:
            raise ValueError(f"duplicate station key {key!r} in {path}")
        seen.add(key)
        stations.append({"station": key, "region": entry["region"]})
    return stations


def assert_consumer_profile(table: pa.Table) -> None:
    """Reject Arrow columns outside the exact consumer profile demonstrated here."""
    for field in table.schema:
        if pa.types.is_decimal(field.type) and field.type.precision > 38:
            raise ValueError(
                f"column {field.name!r} has {field.type} precision {field.type.precision} > 38; "
                "choose a narrower XSD decimal or convert outside the consumer"
            )
