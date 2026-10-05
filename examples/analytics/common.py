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


def _wide_decimals(path: str, data_type: pa.DataType) -> list[tuple[str, pa.DataType]]:
    """Find decimals wider than the consumer profile, including nested fields."""
    if pa.types.is_decimal(data_type):
        return [(path, data_type)] if data_type.precision > 38 else []
    findings: list[tuple[str, pa.DataType]] = []
    if pa.types.is_struct(data_type):
        for child in data_type:
            findings.extend(_wide_decimals(f"{path}.{child.name}", child.type))
    elif (
        pa.types.is_list(data_type)
        or pa.types.is_large_list(data_type)
        or pa.types.is_fixed_size_list(data_type)
    ):
        findings.extend(_wide_decimals(f"{path}.item", data_type.value_type))
    return findings


def assert_consumer_profile(table: pa.Table) -> None:
    """Reject Arrow columns outside the exact consumer profile demonstrated here."""
    for field in table.schema:
        for path, data_type in _wide_decimals(field.name, field.type):
            raise ValueError(
                f"column {path!r} has {data_type} precision {data_type.precision} > 38; "
                "choose a narrower XSD decimal or convert outside the consumer"
            )
