"""Schema-derived purchase-order access models; requires pyxsd[pydantic]."""

from pathlib import Path
from typing import Any

import pyxsd
from pydantic import BaseModel, ValidationError
from pyxsd.integrations.pydantic import ModelSet, models

DATA = Path(__file__).resolve().parent


def prepare() -> tuple[pyxsd.Schema, ModelSet, type[BaseModel]]:
    # No Python Customer/Line/Order class declarations: the XSD is the source.
    schema = pyxsd.compile(DATA / "orders.xsd", mode=pyxsd.ParseModes.NAMESPACED)
    registry = models(schema)
    Order = registry.model_for(element="orders", path=("order",))
    return schema, registry, Order


def python_order() -> dict[str, Any]:
    # Aliases encode XML attributes (@), simple content ($), and child names.
    # Exact decimal strings are accepted; binary floats are not.
    return {
        "@number": "PO-2026-1044",
        "placed": "2026-10-01T09:00:00Z",
        "customer": {
            "@account": "RRL-42",
            "name": "River Research Lab",
            "contact": "procurement@example.invalid",
        },
        "line": [
            {
                "@sku": "LOGGER-USB",
                "description": "Temperature logger",
                "quantity": 4,
                "price": {"$": "125.50"},
            }
        ],
        "expedited": False,
    }


def main() -> None:
    schema, registry, Order = prepare()
    print("Schema-derived purchase-order models (no handwritten model classes)")
    print("Fields:", {name: str(field.annotation) for name, field in Order.model_fields.items()})
    # Generation and standalone validation work before any XML is parsed.
    supplied = python_order()
    supplied.pop("expedited")
    candidate = Order.model_validate(supplied)
    print("Validated Python input:", candidate.model_dump(exclude_unset=True, by_alias=True))
    print(
        "Price:",
        candidate.line[0].price.xml_value,
        type(candidate.line[0].price.xml_value).__name__,
    )
    print("Effective currency:", candidate.line[0].price.attr_currency)
    print("Explicit price fields:", candidate.line[0].price.model_fields_set)
    print("Order status from XSD default:", candidate.attr_status)
    print(
        "Omitted expedited:",
        candidate.expedited,
        "explicitly set:",
        "expedited" in candidate.model_fields_set,
    )
    invalid = python_order()
    invalid["line"][0]["quantity"] = 0
    try:
        Order.model_validate(invalid)
    except ValidationError as error:
        print("Rejected invalid quantity (xs:positiveInteger):", error.errors(include_url=False))

    document = schema.parse(DATA / "orders.xml")
    snapshot = registry.from_document(document, revalidate=True)
    print(f"XML snapshot: {len(snapshot.order)} orders")
    for order in snapshot.order:
        subtotal = sum(line.quantity * line.price.xml_value for line in order.line)
        currency = order.line[0].price.attr_currency
        print(order.attr_number, order.customer.name, len(order.line), "lines;", subtotal, currency)
    # Compare absent note, explicit nil, and non-nil text without changing XML.
    for order in snapshot.order:
        print(order.attr_number, "note:", order.note, "present:", "note" in order.model_fields_set)
    print("JSON Schema required fields:", Order.model_json_schema()["required"])


if __name__ == "__main__":
    main()
