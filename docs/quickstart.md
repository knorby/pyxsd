# Quickstart

```{eval-rst}
.. meta::
   :description: Install pyxsd, validate supplied XML against XSD, query namespaced elements, and revalidate and save an edited document.
```

Validate a small inventory, query its items, and save a checked edit. This
walkthrough uses only the base library and the two files supplied below.

## Requirements

Python 3.11 or newer (3.11–3.15 tested). The base dependencies, `elementpath`
and `defusedxml`, are pure Python; no compiled extensions are required.
Before using untrusted input, read {doc}`xml-security` for parser protections
and limits. The files in this walkthrough are trusted sample data.

## Installation

Install into the Python environment in which you will run the examples:

```sh
python -m pip install pyxsd
```

In a uv-managed project, use `uv add pyxsd` and run scripts with `uv run python`.
For **CLI-only** use, `uv tool install pyxsd` creates an isolated tool
environment; it does not make library imports available in another project.

| Goal | Installation |
| --- | --- |
| Library imports and the `pyxsd` CLI | `python -m pip install pyxsd` |
| Isolated CLI only | `uv tool install pyxsd` |
| Pydantic or Arrow/Parquet | `python -m pip install 'pyxsd[pydantic]'` or `'pyxsd[arrow]'`; see {doc}`integrations`. |
| Local MCP server and Python client | `python -m pip install 'pyxsd[mcp]'`; see {doc}`mcp`. |
| MCP server with every optional adapter | `python -m pip install 'pyxsd[mcp-all]'`; record preview/export need Arrow. |
| Repository examples | Follow the family's README; `examples-*` dependency groups are checkout-only, not published extras. |
| Development and documentation | From a checkout, `uv sync --group dev --group docs`; see {doc}`contributing`. |

This site follows `develop` and may describe unreleased behavior. Compare
`pyxsd --version` with the matching repository tag when using a release.

## Create the input files

Save this as `inventory.xsd` in an empty working directory:

```xml
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    targetNamespace="urn:inventory" xmlns:i="urn:inventory"
    elementFormDefault="qualified">
  <xs:element name="inventory">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="item" maxOccurs="unbounded">
          <xs:complexType>
            <xs:sequence>
              <xs:element name="name" type="xs:string"/>
              <xs:element name="quantity" type="xs:positiveInteger"/>
            </xs:sequence>
            <xs:attribute name="id" type="xs:string" use="required"/>
          </xs:complexType>
        </xs:element>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
```

Save this as `inventory.xml` beside it:

```xml
<inventory xmlns="urn:inventory">
  <item id="B1"><name>Bolts</name><quantity>4</quantity></item>
</inventory>
```

## Library use

Run Python from that directory. Compile the schema, then parse the XML:

```python
from pyxsd import ParseModes, Schema

schema = Schema.compile("inventory.xsd", mode=ParseModes.NAMESPACED)
schema.require_valid()
document = schema.parse("inventory.xml")
document.require_valid()
```

`schema.report` contains schema findings. `document.report` merges those
with instance findings. A returned object alone does not establish validity:
`require_valid()` raises `ValidationError` if its report contains errors.
Unreadable or malformed input can raise `PyXSDError` before binding completes.

## Query the document

Use query prefixes even when the XML uses a default namespace:

```python
ns = {"i": "urn:inventory"}
item = document.find("i:item", namespaces=ns)
print(item.name, item.quantity)  # Bolts 4
items = document.findall("i:item", namespaces=ns)
assert len(items) == 1
assert document.xpath("count(/i:inventory/i:item)", namespaces=ns) == 1
```

`find` and `findall` accept ElementTree paths relative to the root and return
bound nodes (`None` or an empty list when absent). `xpath` accepts XPath
expressions, including scalar queries; absolute paths include the root name.
See {doc}`data-model` for query semantics and export fidelity.

Simple children such as `item.name` provide value access. Some parsed complex
children are present in `_children_` while their descriptor returns `None`;
use `find`, `findall`, or `xpath` to retrieve those nodes. Type stubs describe
declarations, not a guarantee that every descriptor is populated after parsing.

## Edit, revalidate, and write

Change a bound text node, then check a fresh document before writing it:

```python
name = document.find("i:item/i:name", namespaces=ns)
name._value_ = ["BOLTS"]
checked = document.revalidate()
checked.require_valid()
checked.write("updated.xml")
```

The saved item name is `BOLTS`. `_value_` is a list of lexical strings in the
{doc}`data-model`. The old report describes the original parse; neither edits
nor `write()` refresh it. Value changes can violate a type or facet just as
structural edits can violate a content model. `revalidate()` serializes and
reparses the tree; use its returned document and report.

For reusable edits, see {doc}`transforms/using`. Transforms can mutate the
original tree; they do not automatically copy it or revalidate their result.

## Command line

From the directory containing the two input files:

```sh
pyxsd -i inventory.xml -s inventory.xsd --namespaces strict --strict
```

A successful run exits `0` without writing XML. Change the quantity to `0`
to see a validation report on stderr and exit code `1`. Without `--strict`,
recoverable validation errors do not fail the command. Namespace handling,
binding retention, and failure-on-errors are separate choices; see
{doc}`binding` and {doc}`cli`.

## What pyxsd validates

See {doc}`supported` for supported XSD constructs and known gaps. Validation
reports describe the checks performed, not automatic repairs or complete
domain-specific conformance.

## Next steps

- {doc}`validation`: interpret findings and troubleshoot unexpected results.
- {doc}`schema-resolution`: resolve includes, imports, and instance hints.
- {doc}`demo`: run a custom transform from a repository checkout.
- {doc}`batch`: parse multiple files sequentially with one compiled schema.
- {doc}`integrations`: generate Pydantic snapshots or Arrow/Parquet exports.
