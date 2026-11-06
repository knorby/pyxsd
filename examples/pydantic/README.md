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

## Validation limits

Types, supported constraints, and nested access structure come from the
compiled XSD. Pydantic validates the projected-data contract; it does not
replace full XML validation, ordering checks, or document-scoped keyrefs.
`revalidate=True` explicitly requests fresh XML validation. Temporal values
such as `placed` remain validated XSD strings, preserving their offsets.
Generated models are snapshots: changing them does not modify the XML.

See [the integration guide](../../docs/integrations.md) for aliases, supported
shapes, and limitations.

## Order assessment

The [`assess_orders.py`](assess_orders.py) companion validates XML, creates fresh
Pydantic snapshots, and then applies synthetic approval rules. From the repository
root, with the same Pydantic extra installed:

```sh
python examples/pydantic/assess_orders.py
python examples/pydantic/assess_orders.py examples/pydantic/orders_review.xml
```

Each result contains the order number, disposition (`accepted`,
`approval_required`, or `rejected`), currency, exact subtotal as a JSON string,
and reason codes. The default input reports:

| Order | Currency / subtotal | Disposition |
| --- | --- | --- |
| `PO-2026-1041` | USD `681.90` | `approval_required` |
| `PO-2026-1042` | EUR `450.00` | `accepted` |
| `PO-2026-1043` | USD `100.00` | `accepted` |

The review fixture adds rejection and threshold cases:

- `PO-2026-2001`: unknown account `NWL-99`, rejected with subtotal reported.
- `PO-2026-2002`: unknown SKU `GADGET-X`, rejected with subtotal reported.
- `PO-2026-2003`: USD and EUR lines, rejected as `mixed_currencies`, with no
  aggregate subtotal or currency; the application supplies no exchange rate.
- `PO-2026-2004`: USD `600.00`, accepted. Only a subtotal strictly above the
  threshold requires approval.

Accounts, SKUs, and the `600.00` threshold are synthetic policy fixtures, not
XSD constraints or financial advice. XSD-valid orders can fail policy.
Malformed XML raises `PyXSDError`; XSD-invalid input raises `ValidationError`
during fresh snapshot validation. Neither reaches policy evaluation. Absent
versus nil notes remain distinguishable through `model_fields_set`, and
assessment does not mutate the source XML.

`tests/test_optional_examples.py`, `tests/test_order_assessment_example.py`, and
`tests/test_data_workflows_docs.py` cover these workflows and expected values.
