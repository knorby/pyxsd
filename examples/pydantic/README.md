# Dynamic purchase-order access models

This example turns an XSD into Pydantic models **at runtime**, then uses them
for both independent Python input and typed access to an XML export. There
are no handwritten `Order`, `Customer`, `Line`, or `Price` model classes.

The fictional procurement data contains three purchase orders, two customer
accounts, multiple line items, USD/EUR prices, different status and shipping
states, and absent versus explicitly nil notes. All data is synthetic; no
schema downloads or network access are needed.

## Run

From the repository root, with pyxsd installed:

```bash
pip install 'pyxsd[pydantic]'
python examples/pydantic/demo.py
```

For an editable checkout use `pip install -e '.[pydantic]'`. Arrow is not
needed. Data paths resolve relative to the script, not the working directory.

## Follow the data flow

1. **Compile `orders.xsd`.** The XSD defines the customer/line-item structure,
   `xs:positiveInteger` quantities, exact decimal prices, enumerated order
   statuses, required attributes, defaults, repetition, and nil support.
2. **Generate an access model before reading XML.**

   ```python
   registry = models(schema)
   Order = registry.model_for(element="orders", path=("order",))
   ```

   `order` is a local declaration inside `orders`. The model registry follows
   that declaration, rather than guessing a shape from whichever row happens
   to appear first. Changing the supported XSD structure changes the generated
   models on the next compilation; no separate Python model needs updating.
3. **Validate ordinary Python data.** `python_order()` supplies dictionaries
   with XML aliases: `@number` for an attribute and `{"$": "125.50"}` for the
   price's simple content. The resulting typed access is:

   ```python
   order.customer.name  # str
   order.line[0].quantity  # int
   order.line[0].price.xml_value  # Decimal('125.50')
   order.line[0].price.attr_currency  # 'USD', from the XSD default
   ```

   A quantity of zero is rejected by the generated model. Decimal input is an
   exact string or `Decimal`, not a float. A boolean must be a Python boolean,
   not the string `"true"`.
4. **Snapshot the XML using the same schema.**

   ```python
   document = schema.parse(DATA / "orders.xml")
   snapshot = registry.from_document(document, revalidate=True)
   ```

   `snapshot.order` is always a list, and each order has a list of nested line
   models. The script computes exact subtotals with typed attribute access:

   ```text
   PO-2026-1041 River Research Lab 2 lines; 681.90 USD
   PO-2026-1042 Coastal Water Observatory 1 lines; 450.00 EUR
   PO-2026-1043 River Research Lab 1 lines; 100.00 USD
   ```

   These are line subtotals, not tax/shipping-inclusive invoice totals. Each
   sample order uses one currency; the example does not perform FX conversion.
5. **Inspect presence and JSON Schema.** Effective defaults do not count as
   supplied values in `model_fields_set`. An absent optional boolean has a
   `None` placeholder, but explicitly supplying `None` is invalid because it
   is not nillable. An absent note and an explicitly nil note are both `None`
   values, distinguished by their presence in `model_fields_set`.

   Use `model_dump(exclude_unset=True, by_alias=True)` for a snapshot of explicit
   presence. Ordinary dumps can include default/omitted placeholders and are
   not a lossless XML round-trip format.

## What this does—and does not—prove

Types, supported constraints, and nested access structure come from the
compiled XSD. Pydantic validates the projected-data contract; it does not
replace full XML validation, ordering checks, or document-scoped keyrefs.
`revalidate=True` explicitly requests fresh XML validation. Temporal values
such as `placed` remain validated XSD strings, preserving their offsets.
Generated models are snapshots: changing them does not modify the XML.

See [the integration guide](../../docs/integrations.md) for aliases, supported
shapes, and limitations. `demo.py`, `orders.xsd`, and `orders.xml` are the entire
example; its behavior is exercised by `tests/test_optional_examples.py`.
