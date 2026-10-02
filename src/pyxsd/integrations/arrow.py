"""Explicit Arrow record schemas, tables and row-batched Parquet output.

Install ``pyxsd[arrow]``. The input XML tree and record selection are
materialized; output batching is not streaming XML parsing.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ModuleNotFoundError as exc:
    if exc.name != "pyarrow":
        raise
    raise ImportError("Install pyxsd[arrow] to use Arrow and Parquet projections") from exc

from pyxsd.document import Document
from pyxsd.schema import Schema

from . import IntegrationError
from ._shape import ElementShape, ShapeSet, list_item_type, scalar_kind
from ._values import prepare_document, project

__all__ = ["RecordProjection", "records"]


def _metadata(name: str, path: tuple[str, ...], scalar: type | None) -> dict[bytes, bytes]:
    facets = getattr(scalar, "_facetConstraints_", None)
    return {
        b"pyxsd:name": name.encode(),
        b"pyxsd:declaration": json.dumps(path).encode(),
        b"pyxsd:type": getattr(scalar, "name", "complex").encode(),
        b"pyxsd:facets": json.dumps(asdict(facets) if facets else {}, default=str).encode(),
    }


def _scalar_type(cls: type) -> Any:
    kind = scalar_kind(cls)
    if kind == "bool":
        return pa.bool_()
    if kind == "int":
        lower, upper = getattr(cls, "_min", None), getattr(cls, "_max", None)
        facets = getattr(cls, "_facetConstraints_", None)
        if facets:
            minimum = facets.min_inclusive
            maximum = facets.max_inclusive
            if facets.min_exclusive is not None:
                minimum = facets.min_exclusive + 1
            if facets.max_exclusive is not None:
                maximum = facets.max_exclusive - 1
            if minimum is not None:
                lower = max(lower, int(minimum)) if lower is not None else int(minimum)
            if maximum is not None:
                upper = min(upper, int(maximum)) if upper is not None else int(maximum)
        if lower is not None and upper is not None:
            for bits in (8, 16, 32, 64):
                if lower >= 0 and upper < 2**bits:
                    return getattr(pa, f"uint{bits}")()
                if -(2 ** (bits - 1)) <= lower and upper < 2 ** (bits - 1):
                    return getattr(pa, f"int{bits}")()
        return pa.string()
    if kind == "decimal":
        facets = getattr(cls, "_facetConstraints_", None)
        digits = facets.total_digits if facets else None
        scale = facets.fraction_digits if facets else None
        if digits is not None and scale is not None:
            precision = digits + scale
            if precision <= 38:
                return pa.decimal128(precision, scale)
            if precision <= 76:
                return pa.decimal256(precision, scale)
        return pa.string()
    if kind == "float":
        return pa.float64()
    if kind in ("base64", "hex"):
        return pa.binary()
    if kind == "list":
        return pa.list_(pa.field("item", _scalar_type(list_item_type(cls)), nullable=False))
    return pa.string()


def _element_type(shape: ElementShape) -> Any:
    if shape.primitive:
        assert shape.scalar is not None
        return _scalar_type(shape.scalar)
    fields = _fields(shape)
    if not fields:
        shape.fail("empty struct has no supported Parquet representation")
    return pa.struct(fields)


def _fields(shape: ElementShape) -> list[Any]:
    fields = []
    if shape.nillable:
        fields.append(pa.field("$nil", pa.bool_(), nullable=False))
    for attr in shape.attributes:
        if not attr.prohibited:
            fields.append(
                pa.field(
                    attr.alias,
                    _scalar_type(attr.scalar),
                    nullable=not attr.required,
                    metadata=_metadata(attr.name, (*shape.path, attr.alias), attr.scalar),
                )
            )
    if shape.scalar is not None:
        fields.append(
            pa.field(
                "$",
                _scalar_type(shape.scalar),
                nullable=shape.nillable,
                metadata=_metadata("$", shape.path, shape.scalar),
            )
        )
    for child in shape.children:
        data_type = _element_type(child)
        if child.repeated:
            data_type = pa.list_(
                pa.field("item", data_type, nullable=child.nillable and child.primitive)
            )
        # Scalars/structs can be absent in a choice or inactive optional group.
        # Current presence and group constraints are enforced by the projector.
        fields.append(
            pa.field(
                child.name,
                data_type,
                nullable=not child.repeated,
                metadata=_metadata(child.name, child.path, child.scalar),
            )
        )
    return fields


def _scalar_row(cls: type, data_type: Any, value: Any) -> Any:
    if value is None:
        return None
    if scalar_kind(cls) == "list":
        item = list_item_type(cls)
        return [_scalar_row(item, data_type.value_type, v) for v in value]
    if pa.types.is_string(data_type) and isinstance(value, (int, Decimal)):
        return format(value, "f") if isinstance(value, Decimal) else str(value)
    return value


def _element_row(shape: ElementShape, data_type: Any, value: Any) -> Any:
    if value is None:
        return None
    if shape.primitive:
        assert shape.scalar is not None
        return _scalar_row(shape.scalar, data_type, value)
    result: dict[str, Any] = {}
    if shape.nillable:
        result["$nil"] = value["$nil"]
    for attr in shape.attributes:
        if not attr.prohibited:
            result[attr.alias] = _scalar_row(
                attr.scalar, data_type.field(attr.alias).type, value.get(attr.alias)
            )
    if shape.scalar is not None:
        result["$"] = _scalar_row(shape.scalar, data_type.field("$").type, value.get("$"))
    for child in shape.children:
        field_type = data_type.field(child.name).type
        if child.repeated:
            result[child.name] = [
                _element_row(child, field_type.value_type, v) for v in value.get(child.name, [])
            ]
        else:
            result[child.name] = _element_row(child, field_type, value.get(child.name))
    return result


class RecordProjection:
    """A fixed declaration-specific schema; rows never determine column types."""

    def __init__(self, schema: Schema, *, element: str, path: tuple[str, ...] = ()) -> None:
        self._schema = schema
        self._shape = ShapeSet(schema).resolve(element, path)
        self._type = _element_type(self._shape)
        fields = (
            [pa.field("value", self._type, nullable=self._shape.nillable)]
            if self._shape.primitive
            else list(self._type)
        )
        self.schema = pa.schema(
            fields,
            metadata={
                b"pyxsd:projection": b"1",
                b"pyxsd:declaration": json.dumps(self._shape.path).encode(),
                b"pyxsd:presence": b"absent/nil scalars collapse; attributes include effective defaults",
                b"pyxsd:exactness": b"numerical value; temporal strings; no original lexical provenance",
            },
        )

    def batches(
        self,
        document: Document,
        *,
        selector: str | None = None,
        namespaces: dict[str, str] | None = None,
        batch_size: int = 10000,
        revalidate: bool = False,
    ) -> Iterator[Any]:
        """Yield at most batch_size projected rows at a time, not an XML stream."""
        if type(batch_size) is not int or batch_size <= 0:
            raise IntegrationError("batch_size must be a positive integer")
        document = prepare_document(self._schema, document, revalidate)
        nodes = (
            [document.root]
            if selector is None
            else document.findall(selector, namespaces=namespaces)
        )
        rows = []
        for index, node in enumerate(nodes, 1):
            try:
                value = _element_row(self._shape, self._type, project(self._shape, node))
                rows.append({"value": value} if self._shape.primitive else value)
            except (IntegrationError, TypeError, ValueError) as exc:
                raise IntegrationError(
                    f"row {index} ({'/'.join(self._shape.path)}): {exc}"
                ) from exc
            if len(rows) == batch_size:
                yield self._batch(rows, index)
                rows = []
        if rows:
            yield self._batch(rows, len(nodes))

    def _batch(self, rows: list[dict[str, Any]], last_row: int) -> Any:
        try:
            return pa.RecordBatch.from_pylist(rows, schema=self.schema)
        except (pa.ArrowException, ValueError, OverflowError) as exc:
            raise IntegrationError(f"Arrow batch ending at row {last_row}: {exc}") from exc

    def table(
        self,
        document: Document,
        *,
        selector: str | None = None,
        namespaces: dict[str, str] | None = None,
        revalidate: bool = False,
    ) -> Any:
        """Materialize the selected rows, including a fully typed empty table."""
        return pa.Table.from_batches(
            list(
                self.batches(
                    document, selector=selector, namespaces=namespaces, revalidate=revalidate
                )
            ),
            schema=self.schema,
        )

    def write_parquet(
        self,
        document: Document,
        destination: Any,
        *,
        selector: str | None = None,
        namespaces: dict[str, str] | None = None,
        batch_size: int = 10000,
        revalidate: bool = False,
    ) -> None:
        """Atomically replace local paths; borrowed sinks may retain partial output."""
        if type(batch_size) is not int or batch_size <= 0:
            raise IntegrationError("batch_size must be a positive integer")
        document = prepare_document(self._schema, document, revalidate)
        temporary: Path | None = None
        target: Path | None = None
        if isinstance(destination, (str, os.PathLike)):
            target = Path(destination)
            with tempfile.NamedTemporaryFile(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, delete=False
            ) as file:
                temporary = Path(file.name)
            sink: Any = str(temporary)
        else:
            sink = destination
        try:
            with pq.ParquetWriter(sink, self.schema, version="2.6") as writer:
                for batch in self.batches(
                    document, selector=selector, namespaces=namespaces, batch_size=batch_size
                ):
                    writer.write_batch(batch)
            if target is not None and temporary is not None:
                os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def records(schema: Schema, *, element: str, path: tuple[str, ...] = ()) -> RecordProjection:
    """Prepare an explicit global/local record projection before any rows exist."""
    return RecordProjection(schema, element=element, path=path)
