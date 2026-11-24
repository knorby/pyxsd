# Schemaless XML and native JSON

```{eval-rst}
.. meta::
   :description: Load XML without a grammar, keep native JSON values, type and profile either input, expose native XDM to XSLT 3.0, and project JSON into XML with explicit, JSON Schema, or OpenAPI mappings.
```

`pyxsd.schemaless` is the schema-optional document family. "Schemaless" means
that XSD or RELAX NG is not required to ingest the document; it does not mean
that constraints are impossible. Two document types keep the input models
apart:

- `SchemalessDocument` owns an editable lexical XML tree;
- `JsonDocument` owns native JSON data and addresses it with RFC 6901 JSON
  Pointers.

Neither type converts the other's values unless an explicit mapping asks for
it. XPath and XML occurrence paths address the XML tree; JSON Pointers address
the JSON value. See {doc}`data-model` for the shared storage rules and
{doc}`validation` for reports and locations.

## Install and choose a representation

The base installation includes schemaless XML, native JSON, and explicit XML
projection. JSON Schema and OpenAPI guidance are optional:

```bash
pip install pyxsd                    # schemaless XML, native JSON, XML projection
pip install 'pyxsd[jsonschema]'      # adds JSON Schema and OpenAPI guidance
pip install 'pyxsd[mcp-all]'         # adds the optional JSON Schema capability to the MCP set
```

Native XML and native JSON work without `jsonschema`; the import stays lazy
until an operation asks for schema guidance. The optional extra is
`jsonschema>=4.25,<5`.

| Input | Document | Authoritative storage | Primary transform route |
| --- | --- | --- | --- |
| XML without a grammar | `SchemalessDocument` | Expanded names, attributes, ordered children, text and tails | Untyped nodes from `to_xdm()`; `xpath()`; XSLT |
| JSON text or Python data | `JsonDocument` | Native mappings, lists, `int`/`Decimal`, booleans, strings, and null | Maps, arrays, and atomics from `to_xdm()`; XSLT 3.0 |
| JSON that must be XML | `SchemalessDocument` from `to_xml()` | Projected lexical XML | Node-oriented XSLT, including XSLT 2.0 |

## Schemaless XML

`parse_xml()` accepts a path, bytes, text or binary streams, or a caller-built
ElementTree element. It applies the opt-in parser policy and limits described
in {doc}`xml-security`. The lexical stores remain authoritative: parsing does
not trim text, normalize attribute values, or interpret leading zeros.

```python
import io

from pyxsd.schemaless import parse_xml

source = b'<orders><order id="02138"><total>10.50</total></order></orders>'
orders = parse_xml(io.BytesIO(source))
order = orders.root.children[0]

assert orders.xpath("/orders/order/@id") == ["02138"]
assert order.attribute_lexical("id") == "02138"
assert order.children[0].text_value() == "10.50"
assert orders.to_string() == '<orders><order id="02138"><total>10.50</total></order></orders>'
```

`orders.schema` is `None`; the parse report establishes well-formedness, not
XSD or RELAX NG validity. Traversal, XPath, `to_string()`, `write()`, and the
native `to_dict()`/`to_data(format="pyxsd")` exports work as they do for
schema-bound documents. Schema-directed export formats raise an explicit
capability error because no schema selects their layout. Edits such as
`set_text()` and `set_attribute()` dirty the lexical revision; see
{doc}`data-model` for the full node protocol.

## Typing, profiles, and mixins

The default scalar policy is `TypePolicy.lexical()`: every XML scalar reads as
a string. `TypePolicy.infer()` is opt-in conservative inference, and
`pin_type()` installs an explicit occurrence pin. None of them rewrites the
lexical tree or adds XSD type annotations to XDM nodes.

```python
import io

from pyxsd.schemaless import ScalarTypeError, TypePolicy, parse_xml

inferred = parse_xml(
    io.StringIO("<r><id>02138</id><active>true</active><note>12</note></r>"),
    types=TypePolicy.infer(),
)
assert inferred.root.children[0].scalar_value() == "02138"  # leading zero stays lexical
assert inferred.root.children[1].scalar_value() is True
assert inferred.root.children[2].scalar_value() == 12

report = inferred.pin_type("/r/active", "xs:integer")
assert report.has_errors
pin_failure = None
try:
    inferred.root.children[1].scalar_value()
except ScalarTypeError as error:
    pin_failure = error
assert pin_failure is not None
```

Inference is path-wide: a candidate is selected only when every occurrence at
that path accepts it. It declines leading-zero integers, `NaN`/`INF`, and
non-strict booleans such as `True`; candidates are ranked deterministically.
`test_type()` reports failures without installing a pin. A failed pin remains
in the document's policy: typed reads at the failing occurrences raise
`ScalarTypeError`. Lexical reads, writing, untyped XDM, and native exports
requested with `typed=False` remain available; the default typed `to_dict()`
and `to_data()` raise `ScalarTypeError` until the pin is removed or those
exports are requested lexically. Built-in XSD scalar types and custom
`ScalarAdapter` objects are supported; there is no built-in UUID type.

`profile()` walks one current revision and returns a bounded, immutable
snapshot. Candidate counts are evidence only: profiling never installs a pin.

```python
import io

from pyxsd.schemaless import ProfileLimits, parse_xml

profiled = parse_xml(io.StringIO("<r><n>1</n><n>2</n><n>bad</n></r>"))
profile = profiled.profile(ProfileLimits(max_samples=2))
row = profile.at("/r/n")

assert (row.occurrences, row.min_occurs, row.max_occurs) == (3, 3, 3)
assert row.candidate("xs:integer").passed == 2
assert row.candidate("xs:integer").failed == 1
assert row.samples == ("1", "2")
assert profile.complete is False
```

`NodeMapping` is the opt-in Python projection. A registered class is a mixin
over the ordinary lexical node, not another tree. Descriptor `type=` entries
compile into occurrence pins, and descriptor writes use the same scalar
adapters as ordinary typed access.

```python
import io

from pyxsd.schemaless import NodeMapping, attribute, children, parse_xml, text


class OrderView:
    id = attribute("id", type="xs:integer")
    note = text(default="")
    lines = children("line")


node_mapping = NodeMapping()
node_mapping.register(OrderView, path="/order")
mapped = parse_xml(io.StringIO('<order id="7"><line/><line/></order>'), node_mapping=node_mapping)

assert mapped.root.id == 7
assert len(mapped.root.lines) == 2
assert mapped.root.note == ""
```

Registrations are document-local. Matching classes combine in deterministic
registration order, class identity stays stable for the document's lifetime,
and there is no process-global type registry. A singular `child()` that sees
repeated elements raises instead of choosing one; there is no unrestricted
attribute lookup that could hide method, namespace, or mixed-content
ambiguity. See {doc}`extensions` for the established mixin precedence
discipline.

## Native JSON

`from_json()` decodes strict JSON text, bytes, or a stream.
`from_data()` copies Python data into the same native model. Objects stay
mappings with string keys, arrays stay lists, and no number passes through
`float` merely to be stored.

```python
from decimal import Decimal

from pyxsd.schemaless import from_json

payload = from_json('{"id": "02138", "total": 10.50, "flags": [true, null]}')

assert payload.value_at("/id") == "02138"
assert payload.value_at("/total") == Decimal("10.50")
assert payload.value_at("/flags/1") is None
assert payload.to_json() == '{"id": "02138", "total": 10.50, "flags": [true, null]}'
```

Malformed JSON text raises `JsonParseError` with the decoder's line, column,
and offset when it reports them. Value-model failures—duplicate object keys
and non-finite constants—also raise `JsonParseError`, but they have no text
location: `line`, `column`, and `position` are `None`. Non-UTF-8 byte input may
report only a byte offset.

`value_at()` returns deep copies of containers, so a read cannot mutate
authoritative storage. `to_data()` returns native values, including `Decimal`;
`to_json()` emits their exact spelling. Input member order is retained for
deterministic projection and diagnostics, but JSON Schema gives it no
semantic meaning. `JsonLimits` supplies independent byte, depth, value, and
diagnostic budgets.

### Native JSON versus `parse-json`

These are separate entry points with deliberately different numeric contracts:

| Entry point | JSON number result |
| --- | --- |
| `from_json()` / `from_data()` | `int` for an integral token, `Decimal` for any other token; `float` only when host data supplies one |
| XPath `parse-json()` | `xs:double` for every JSON number, as the XPath 3.1 standard requires |

Use `JsonDocument` when numeric value and precision must survive. Do not
transfer its precision claim to standardized `parse-json`, and do not expect
the schemaless parser to change `parse-json` behavior.

### Native maps and arrays for XDM and XSLT

| JSON value | XDM result |
| --- | --- |
| Object | `XdmMap` with `xs:string` keys |
| Array | XDM array retaining member order |
| String | `xs:string` |
| Parsed integral number | `xs:integer` |
| Parsed non-integral number | `xs:decimal` without a float round trip |
| Finite float supplied through `from_data` | `xs:double` |
| Boolean | `xs:boolean` |
| Null | Empty sequence |

Maps and arrays are XDM **items**, not XML nodes. Use `?key` lookup, array
lookup, and XPath 3.1 expressions; XML child axes, node identity, and source
spans do not apply. Schemaless XML produces a document node whose elements
carry the `xs:untyped` annotation and whose attributes and text atomize as
`xs:untypedAtomic`. Native JSON therefore targets XSLT 3.0 and XPath 3.1.
XSLT 2.0 has no map or array items and must use the explicit XML projection
below.

A root null produces the empty sequence, so a stylesheet invocation has no
global context item unless the caller supplies another entry or selection. A
null object property leaves the key present with an empty-sequence value; a
null array member remains one member whose value is the empty sequence.

```python
import io

from pyxsd.schemaless import from_json
from pyxsd.xdm import XdmArray, XdmAtomic, XdmMap
from pyxsd.xslt.processor import XsltProcessor

sales = from_json('{"total": 3, "lines": [{"sku": "A"}, {"sku": "B"}]}')
sequence = sales.to_xdm()
root = sequence[0]
assert isinstance(root, XdmMap)
lines = root.get(XdmAtomic.string("lines"))[0]
assert isinstance(lines, XdmArray)
assert lines.size == 2

null_sequence = from_json("null").to_xdm()
assert null_sequence == ()

stylesheet = XsltProcessor().compile(
    io.StringIO(
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0">'
        '<xsl:output method="text"/>'
        '<xsl:template name="main"><xsl:value-of select=".?total"/></xsl:template>'
        "</xsl:stylesheet>"
    )
)
rendered = stylesheet.transform(
    global_context_item=sales.to_xdm(), initial_template="main"
).serialize()
assert rendered == "3"
```

## XML projection

`JsonDocument.to_xml()` is a one-way projection from unchanged native storage
into a fresh `SchemalessDocument`. `XmlMapping.elements()` is the conservative
default: object properties become elements, arrays get wrappers, and nothing
becomes an attribute without a rule. `XmlMapping.compact()` is the opt-in
data-centric alternative: scalar object properties default to attributes while
objects and arrays remain elements. Explicit `MappingRule` values override
either policy.

```python
from pyxsd.schemaless import MappingRule, XmlMapping, from_data

record = from_data({"id": "02138", "note": "hello", "lines": ["A", "B"]})
element_first = record.to_xml(XmlMapping.elements("order"))
compact = record.to_xml(
    XmlMapping.compact("order", rules={"/lines": MappingRule(array="wrapped", item_name="line")})
)

assert element_first.to_string() == (
    "<order><id>02138</id><note>hello</note><lines><item>A</item><item>B</item></lines></order>"
)
assert compact.to_string() == (
    '<order id="02138" note="hello"><lines><line>A</line><line>B</line></lines></order>'
)
```

A rule controls the node `kind` (`element`, `attribute`, `text`, or `none`),
expanded `name` and optional serialization `prefix`, array `wrapped`/unwrapped
behavior with item and wrapper names, null handling, emission `order`, and an
optional scalar `type` used only for lexical formatting. Invalid XML names and
characters XML 1.0 cannot represent raise `XmlMappingError` with both the JSON
Pointer and the resulting XML path; names are never silently sanitized.

The derived report identifies information the XML shape cannot retain. These
are warnings, not validation failures:

```python
from pyxsd.schemaless import XmlMapping, from_data

lossy = from_data({"id": None, "note": "hold"}).to_xml(XmlMapping.compact("order"))
assert lossy.to_string() == '<order note="hold"/>'
assert lossy.is_valid
assert lossy.report.warning_count == 1
assert lossy.report.issues[0].code == "null-attribute-omitted"
```

A null attribute or text node is omitted, which is indistinguishable from a
missing property. A `none` rule drops a scalar with a warning, an empty
container cannot be represented without a wrapper, and CDATA has text-node
semantics rather than a preserved lexical spelling. Fatal ambiguity or an
invalid name raises `XmlMappingError` instead of guessing.

### XSLT 2.0 and XML projection

XSLT 2.0 can consume the projected nodes through ordinary axes. The nodes are
untyped, because no XSD or RELAX NG validation established their types.
The same projection is also available when an XSLT 3.0 stylesheet is naturally
node-oriented.

```python
import io

from pyxsd.schemaless import XmlMapping, from_data
from pyxsd.xslt.processor import XsltProcessor

order = from_data({"id": "02138", "total": 10.50})
document = order.to_xml(XmlMapping.compact("order"))
stylesheet = XsltProcessor(language="2.0", xpath="2.0").compile(
    io.StringIO(
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="2.0">'
        '<xsl:output method="text"/>'
        '<xsl:template match="/"><xsl:value-of select="child::order/@id"/></xsl:template>'
        "</xsl:stylesheet>"
    )
)
rendered = stylesheet.transform(document).serialize()
assert rendered == "02138"
```

## JSON Schema

Passing `json_schema=` evaluates a real Draft 2020-12 schema: boolean schemas,
`$ref`, `$defs`, `$id`, applicators, `prefixItems`, and object property
applicators are handled by the evaluator rather than a local keyword subset.
Schema mappings, boolean schemas, and strict JSON text are accepted. An invalid
instance still supports native reads and native XDM; `require_valid()` raises
`ValidationError`.

```python
from pyxsd.schemaless import from_json

schema = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["id", "total"],
    "properties": {
        "id": {"type": "string"},
        "total": {"type": "number"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}
valid = from_json('{"id": "02138", "total": 10.50, "tags": ["a"]}', json_schema=schema)
invalid = from_json('{"id": 7, "total": "10.50"}', json_schema=schema)

assert valid.is_valid
assert not invalid.is_valid
assert invalid.report.issues[0].location.pointer == "/id"
assert invalid.report.issues[0].location.schema_pointer == "/properties/id/type"
assert invalid.to_data() == {"id": 7, "total": "10.50"}
```

Schema results guide typed reads and XDM conversion only after successful
validation. A mathematically integral number constrained as `integer` can be
exposed as `xs:integer`; other numbers can stay `xs:decimal`. `format` remains
an annotation unless format assertion is enabled:

```python
from pyxsd.schemaless import from_json

format_schema = {"type": "object", "properties": {"email": {"type": "string", "format": "email"}}}
annotated = from_json('{"email": "not-an-address"}', json_schema=format_schema)
asserted = from_json(
    '{"email": "not-an-address"}', json_schema=format_schema, format_assertion=True
)

assert annotated.is_valid
assert not asserted.is_valid
```

With `format_assertion=True`, pyxsd uses its own deterministic RFC 3339 checks
for `date`, `date-time`, and `time`. These checks do not require
`rfc3339-validator`. Format assertion never casts a formatted string without an
explicit `TypePolicy`.

### Reference security

Network retrieval is never implicit. Relative references use the schema's
`base_uri`; external resources must be supplied through `reference_registry` or
authorized and read by a `resource_policy`. That is the same trusted
resource-policy model used for schema resolution and documented in
{doc}`xml-security`. Unresolved references, required unsupported vocabularies,
unsupported dialects, and unauthorized retrieval raise `JsonSchemaError`
rather than appearing as instance validation failures.

## OpenAPI

`XmlMapping.from_openapi()` accepts one **already-loaded Schema Object** from an
OpenAPI Description: a mapping (or boolean schema) plus the reference context
needed to resolve it. It does not accept a whole OpenAPI Description, path
item, operation, or YAML document, and it is not a general OpenAPI client,
server, or complete Description validator.

```python
from pyxsd.schemaless import XmlMapping, from_data

schema_object = {
    "type": "object",
    "xml": {"name": "order"},
    "properties": {
        "id": {"type": "string", "xml": {"nodeType": "attribute"}},
        "note": {"type": "string", "xml": {"nodeType": "text"}},
        "lines": {"type": "array", "items": {"type": "string", "xml": {"name": "line"}}},
    },
}
xml = from_data({"id": "02138", "note": "hold", "lines": ["A", "B"]}).to_xml(
    XmlMapping.from_openapi(schema_object)
)

assert xml.root.attribute_lexical("id") == "02138"
assert xml.root.text_value() == "hold"
assert [node._name_ for node in xml.root.children] == ["line", "line"]
```

The selected schema is the root of that one object. Its XML Objects supply
`nodeType` (element, attribute, text, cdata, or none), `name`, `namespace`,
`prefix`, and the deprecated `attribute`/`wrapped` compatibility fields.
OpenAPI array and name defaults are honored, and an explicit rule overrides an
individual metadata field. An inline schema with no inferable name requires
`root_name` or `xml.name`. `base_uri`, `reference_registry`, `resource_policy`,
and `dialect` interpret the selected object in context; nested references are
resolved by the same evaluator. The schema must validate the instance before
schema-derived mapping applies. Conflicting XML metadata across successful
applicator branches is reported as ambiguous; `allOf` metadata combines only
when the result is unambiguous.

## XDM coherence and revalidation

An unedited document parsed from bytes supplies a source-complete, untyped XDM
view: comments, processing instructions, namespace nodes, and source spans
survive because they are captured from the same bytes. The editable lexical
tree does not store comments or processing instructions, so an edited document
cannot reuse that capture.

```python
import io

from pyxsd.schemaless import parse_xml
from pyxsd.xdm.errors import XdmInputError

doc = parse_xml(io.StringIO("<order><!--note--><line>A</line></order>"))
assert doc.to_xdm().source_index is doc.source_index
doc.root.children[0].set_text("B")

stale_error = None
try:
    doc.to_xdm()
except XdmInputError as error:
    stale_error = error
assert stale_error is not None

refreshed = doc.revalidate()
assert refreshed.to_string() == "<order><line>B</line></order>"
assert "<!--" not in refreshed.to_string()
```

`revalidate()` serializes and reparses the current lexical tree into a fresh
document with a fresh source index, node classes, and coherent XDM view. It
performs no schema validation. The new document is independent; the old
document and its cached XDM are not mutated. This is the documented fidelity
boundary: use a revalidated document for the current element/text projection,
or stay in XDM when source-complete document-centric XML must survive editing.

## Related documentation

- Node storage, editing, traversal, and export: {doc}`data-model`.
- Reports, issue codes, and locations: {doc}`validation`.
- Transform results and document transforms: {doc}`transforms/index`.
- Parser limits, remote authorization, and resource policies:
  {doc}`xml-security`.
- Existing schema-guided conversion and its five formats:
  {doc}`data-conversion`.
- Mixin selectors and precedence: {doc}`extensions`.
- Optional integration installs: {doc}`integrations`.
- Public signatures and reference classes: {doc}`api`.
