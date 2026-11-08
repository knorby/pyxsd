# Data conversion

```{eval-rst}
.. meta::
   :description: Export bound XML as JSON-safe Python data or JSON text in native, schema-driven, Parker, BadgerFish, or JsonML formats with explicit fidelity rules.
```

`Document.to_data()` exports the bound tree as JSON-safe Python values.
`Document.to_json()` encodes the same values as JSON text. Choose a format:

- `format="pyxsd"` (the default) keeps the native {doc}`data-model`
  structure: it absorbs the root name and groups children by name.
- `format="pyxsd-json"` retains the root name and uses the compiled schema
  to select scalar, object, or array layouts. Ordered and mixed content
  keep their positions.
- `format="parker"` absorbs the root, omits attributes, and groups children
  by local name.
- `format="badgerfish"` retains the root, attributes, and namespace bindings
  in a grouped structure.
- `format="jsonml"` uses nested arrays to retain tags, attributes, and
  text/element order.

These exports need no optional dependencies. The {doc}`integrations`
provide a separate Pydantic model projection and Arrow/Parquet records;
their aliases and presence rules are not these document formats.

## Export a document

This complete example uses in-memory schema and XML inputs:

```python
import json
from io import StringIO

import pyxsd

schema = pyxsd.compile(
    StringIO("""
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="dates">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="date" type="xs:date"
                    minOccurs="0" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
""")
)
document = schema.parse(StringIO("<dates><date>2026-01-01</date><date>2026-02-01</date></dates>"))
document.require_valid()

assert document.to_data() == {"date": ["2026-01-01", "2026-02-01"]}
data = document.to_data(format="pyxsd-json")
assert data == {"dates": ["2026-01-01", "2026-02-01"]}
text = document.to_json(format="pyxsd-json", indent=2)
assert json.loads(text) == data
assert document.to_data(format="parker", fidelity="strict") == {
    "date": ["2026-01-01", "2026-02-01"]
}
assert document.to_data(format="badgerfish", fidelity="strict") == {
    "dates": {"date": ["2026-01-01", "2026-02-01"]}
}
assert document.to_data(format="jsonml", fidelity="strict") == [
    "dates",
    ["date", "2026-01-01"],
    ["date", "2026-02-01"],
]
```

Both methods accept keyword-only `format`, `fidelity="best-effort"`,
`typed=True`, and `always_list=False`. `to_json()` also accepts `indent=None`.
`typed` and `always_list` are native-only options: `typed=False` or
`always_list=True` with any other format raises `ValueError`.

Export reads the current bound tree; it neither revalidates it nor requires
a valid report. Call `require_valid()` when validity is a prerequisite, or
follow {doc}`quickstart`'s `revalidate()` workflow after edits. A document
without a bound root raises `PyXSDError`.

## Scalar values

With the default `typed=True`, all five formats use these JSON-safe values:

| Bound value | JSON value |
| --- | --- |
| Boolean | `true` or `false` |
| Integer and integer-derived types | Integer number |
| Decimal | Fixed-point string, never a binary float; `1.20` stays `"1.20"` |
| Finite float or double | Number |
| Non-finite float or double | `"INF"`, `"-INF"`, or `"NaN"` |
| XSD list value | Array, including zero or one item |
| QName | EQName string in `pyxsd-json`; lexical QName in the other formats |
| Other atomic values, including dates, durations, binary values, and URIs | Datatype lexical string |

Python data uses `bool` and `None` where JSON uses booleans and `null`.
`to_json()` uses `allow_nan=False` and no fallback serializer.

Native `typed=False` retains lexical text, including the space-separated
string for an XSD list value. `to_dict()` remains the raw-value interface:
it keeps bound Python/XSD scalar objects instead of normalizing them for
JSON. Native JSON keeps its existing structure and decimal/date/QName
strings, with two scalar changes: non-finite numbers use the strings above,
and typed XSD list values use arrays rather than lexical strings.

## Native `pyxsd` structure

The native structure is shared with `to_dict()`:

- The root name is absorbed. A scalar or empty root uses `{"$": value}`.
- Children become properties named with the bound local or Clark name
  (`{uri}local`), according to namespace mode.
- One child occurrence is a scalar or object; repeated occurrences with
  the same name form an array. `always_list=True` wraps every child value
  in an array, but does not create properties for absent children.
- Attributes use `@name`. Text-only elements become scalar values; text
  alongside attributes or children uses `$`.
- Direct mixed text and child tails are concatenated under `$`. Their
  positions relative to children are lost. Grouping repeated children also
  loses interleaving such as `a, b, a`.

A typed empty complex leaf without attributes or children becomes `None`,
as can a nilled leaf without attributes. Native `null` therefore does not
uniquely identify nil. An actual `xsi:nil` attribute remains an `@` property;
for example, a nilled empty child can be
`{"empty": {"@xsi:nil": "true"}}` in legacy namespace mode, rather than
`{"empty": null}`. Use `pyxsd-json` for a dedicated nil marker.

## Schema-driven `pyxsd-json` structure

This is profile 1. The root is always a single-entry object. Every
namespaced element or attribute uses an EQName: `Q{uri}local`; an
unnamespaced name stays `local`. Attributes add `@`, for example
`@Q{urn:example}id`. Namespace declarations and source prefixes are not data.
QName scalar values also use EQNames; unresolved QName namespace context
raises `namespace-unresolved`, even in best-effort mode. Prefer the default
namespace-aware binding; see {doc}`binding` for namespace modes.

The effective bound type selects each layout, including types selected by
substitution or `xsi:type`. Observed child counts never select the layout.
An optional attribute declared by the type still selects an attribute-capable
layout when absent; an attribute wildcard does too.

### Empty and simple content

| Effective content | Without attributes | With declared attributes or an attribute wildcard |
| --- | --- | --- |
| Empty | `{"root": ""}` | `{"root": {"@id": "r"}}` |
| Simple | `{"root": "text"}` | `{"root": {"@id": "r", "#content": "text"}}` |

These are the `empty`, `empty-plus`, `simple`, and `simple-plus` layouts.
Simple content follows the scalar policy, so it can also be a number,
boolean, or XSD list array. Absent attributes are omitted: empty-plus with
no actual attributes is `{}`, while simple-plus still has `#content`.
Declared attributes use bound typed values; wildcard attributes retain
lexical strings. Synthesized attribute defaults are not added to the actual
attribute map.

### Repeated children: list and list-plus

An element-only type with one possible repeatable child name and no
wildcards uses `list` without attributes, or `list-plus` with attributes.
Computed maximum cardinality must exceed one. A list item is the child's
complete converted value, including its attributes and nested content.

```json
{"dates": ["2026-01-01", "2026-02-01"]}
```

List-plus keeps the child name as an array property:

```json
{"dates": {"@source": "known", "date": ["2026-01-01"]}}
```

Both arrays stay arrays with one occurrence. With none, list is `[]` and
list-plus still includes `"date": []`. A repeated-child layout is distinct
from an XSD list-valued scalar; nested arrays are possible.

### Unordered children: record

A closed, wildcard-free element-only model uses `record` when sibling
order is insignificant and every possible child name occurs at most once.
`xs:all` is the principal case:

```json
{"person": {"@id": "p1", "name": "Ada", "age": 37}}
```

Optional absent children are omitted. Attributes precede child properties;
children follow schema declaration order. Object property order carries no
XML ordering semantics here.

### Ordered children: sequence

Other element-only models use `sequence`, including ordered models with
unique child names, choices with possible repetition, and element wildcards
or open content. A nonempty attribute map comes first, followed by a
singleton child map for every occurrence:

```json
{"section": [
  {"@id": "s1"},
  {"head": "Introduction"},
  {"p": "First"},
  {"note": "Aside"},
  {"p": "Second"}
]}
```

The array preserves bound child order and interleaving. Whitespace-only
layout text is discarded; non-whitespace element-only sequence text raises
`container-invalid`.

### Mixed content

Mixed content uses an array with an optional leading attribute map,
direct-text strings, and singleton child maps. Each child's tail follows
that child, with whitespace preserved:

```json
{"para": [{"@id": "p1"}, "This is a ", {"i": "fine"}, " mess!"]}
```

This retains recorded text/element positions, not source text-node
segmentation, comments, or original namespace prefixes. It is not a
source-exact XML representation.

### Explicit actual types

When an actual `xsi:type` attribute selects a bound type different from the
declared type, export adds the reserved `@xsi:type` key with an EQName value:

```json
{"root": {"@xsi:type": "Q{http://www.w3.org/2001/XMLSchema}int", "#content": 5}}
```

A redundant `xsi:type` equal to the declared type is omitted. The marker
promotes empty, simple, or list content to its plus layout. For sequence and
mixed layouts it belongs in the leading attribute map.

### Nil, empty, and absent

In `pyxsd-json`, `null` means nil, not absence or empty content. `xsi:nil`
is represented by the layout's content marker, never an ordinary attribute:

| Layout | Nilled value, inside the root object |
| --- | --- |
| Empty, simple, list | `null` |
| Empty-plus, simple-plus, list-plus, record | `{"@id": "r", "#content": null}` (attributes only if present) |
| Sequence, mixed | `[{"@id": "r"}, {"#content": null}]` (attribute map only if nonempty) |

Nilled object layouts omit child properties, including the list-plus child
array. Nilled arrays contain only the optional attribute map and nil marker.
An absent child has no property or array entry. An empty string is `""`;
an empty repeated-child container is `[]`; an empty record or attribute
object is `{}`. Defaults already applied by binding are exported as bound
values, not as their original empty lexical input.

## Parker structure

Parker absorbs the root without a `$` wrapper: a simple root is its scalar
value, a nilled root is `null`, and empty element-only content is `{}`.
Children use local-name properties. Only actual repeats become arrays;
one occurrence stays a scalar or object even when the schema permits more.
Absent children have no property.

```json
{"date": ["2026-01-01", "2026-02-01"]}
```

Parker omits attributes and namespace declarations. It can retain
unambiguous local names, but not namespace-dependent QName values or
ambiguous names. Strict mode rejects those losses; best effort warns and
uses the fallbacks below. Text-only mixed content becomes a scalar. With
children, best effort concatenates leading text and child tails under `$`,
losing their positions:

```json
{"$": "This is a  mess!", "i": "fine"}
```

Empty simple string content is `""`, an empty XSD list value is `[]`, and
nil is `null`; absence means an omitted child property. Parker's nil marker
does not require keeping the source `xsi:nil` attribute.

## BadgerFish structure

BadgerFish retains the root as a single-entry object. Attributes use `@`
plus a lexical name. A simple element without attributes is a scalar;
with attributes, its text uses `$`:

```json
{"root": {"@id": "r", "$": "text"}}
```

Children become lexical-name properties. A schema-repeatable child uses an
array whenever present, even for one occurrence. Absent children, including
repeatable ones, have no property. Actual repeats of a non-repeatable name
also become an array rather than discarding values.

```json
{"dates": {"date": ["2026-01-01"]}}
```

Empty complex content is an attribute object, or `{}` without attributes.
Empty simple string content is `""`; an empty XSD list value is `[]`.
Nil uses `"@xsi:nil": true`, not `null` or `$`, and retains other attributes
but no text or children:

```json
{"root": {
  "@xmlns": {"xsi": "http://www.w3.org/2001/XMLSchema-instance"},
  "@xsi:nil": true,
  "@id": "r"
}}
```

An explicit actual type uses `@xsi:type` with a lexical QName value; a
redundant type equal to the declared type is omitted. Namespace declarations
use `@xmlns` on the root value. If that value would be a scalar or XSD list
array, declarations require wrapping it under `$`:

```json
{"ns0:root": {"@xmlns": {"ns0": "urn:example"}, "$": "x"}}
```

Mixed text with children and unreconstructable child order use the same
grouping fallbacks as Parker, with warnings in best-effort mode. BadgerFish
does not add an ordered-array extension.

## JsonML structure

JsonML returns an array, not a root object: `[tag, attributes?, ...content]`.
Child elements are nested JsonML arrays. Attribute names have no `@` prefix;
the attribute object is omitted when empty. Repetition never changes this
shape: every child occurrence has its own array, and absent children have
no entry.

```json
["para", {"id": "p1"}, "This is a ", ["i", "fine"], " mess!"]
```

Direct text, children, and child tails retain their bound positions. Mixed
whitespace is preserved; whitespace-only element-only layout text is
discarded. Like `pyxsd-json`, this retains bound content, not source text-node
segmentation, comments, or original prefixes.

Simple content uses the common typed scalar policy. **Typed content items
are a pyxsd extension to JsonML**, not a claim that lexical-only JsonML
consumers accept them. Numbers and booleans remain typed, decimals remain
strings, and an XSD list value occupies one array-valued content item:

```json
["root", ["a", "b"]]
```

Here the schema identifies the content as an XSD list, not a child tag `a`.
Empty simple string content is `["root", ""]`; empty complex content is
`["root"]`. Nil adds one `null` content item after any attributes, with no
other content: `["root", {"id": "r"}, null]`. It does not add `xsi:nil`.
An explicit actual type uses an `xsi:type` attribute with a lexical QName.

## Lexical namespaces in BadgerFish and JsonML

Both formats synthesize deterministic lexical QNames rather than preserving
source prefixes. Schema-known namespace URIs receive sorted `ns0`, `ns1`,
… prefixes, including reservations for optional absent declarations.
Wildcard-only names and QName-value-only namespaces use stable
`ns_<hex(uri)>` prefixes. Unnamespaced names stay local; `xsi` is declared
when used, while `xml` is implicitly bound.

Only used bindings are emitted, sorted by prefix, once at the root for the
whole subtree. BadgerFish uses `@xmlns`; JsonML adds `xmlns:prefix` keys to
the root attribute object:

```json
["ns0:root", {"xmlns:ns0": "urn:example"}, ["ns0:child", "v"]]
```

Export uses explicit prefixes, not a default namespace (`$` inside a
BadgerFish `@xmlns` map). QName values and `xsi:type` reuse these bindings,
including namespaces used only in values. An unresolved QName raises
`namespace-unresolved` in both fidelity modes. Prefer namespace-aware
binding so QName values retain their namespace context.

## Ordering and fidelity

Export is deterministic for the same bound tree. In `pyxsd-json`, repeated,
sequence, and mixed arrays follow document order. Object layouts emit an
explicit type marker first, then declared attributes in declaration order,
then sorted wildcard attributes; record children follow schema order.
Sequence/mixed attribute maps sort their keys instead, because attribute
order is not semantic.

`fidelity` concerns representation, not XSD validity:

- Native `best-effort` keeps the existing grouped shape without new warnings.
- Native `strict` raises `PyXSDError` for empty/nil ambiguity
  (`empty-nil-ambiguous`), mixed text/element positions (`mixed-content`),
  interleaved repeated names (`order-ambiguous`), and namespaced elements
  reduced to local names by legacy binding (`namespace-lost`). An explicit
  nil attribute is representable and need not raise.
- `pyxsd-json` defines no best-effort fallback. Both modes return the same
  representable data and raise for content incompatible with the selected
  layout, such as unexpected attributes on a scalar, repeated record
  children, or a nilled element carrying content. They do not silently drop
  that content to fit the layout.
- Parker and BadgerFish raise `PyXSDError` for the losses below in strict
  mode. Best effort applies the listed fallback and emits `PyXSDWarning`
  for each reported loss.
- JsonML defines no representational-loss fallback: both modes preserve
  ordered/mixed content and return the same data without loss warnings.

| Format | Loss code | Best-effort fallback |
| --- | --- | --- |
| Parker | `attributes-lost` | Omit actual ordinary attributes and any required explicit type marker. |
| Parker | `namespace-lost` | Keep a namespace-dependent or unresolved QName's lexical spelling without its context. |
| Parker | `name-ambiguous` | Keep the local child key; colliding values share an array rather than selecting a declaration. |
| Parker | `root-ambiguous` | Keep root absorption without resolving which concrete global root shares the local name. |
| Parker, BadgerFish | `mixed-content` | Group children and concatenate leading text and tails under `$`. |
| Parker, BadgerFish | `order-ambiguous` | Group children in schema order, then wildcard extras in first-observed order; retain occurrence order within each group. |

The grouped-order audit detects interleaved repeats (`a, b, a`), child
order that differs from the schema's reconstructable order, and mixed
content with more than one distinct child name. Ordered unique children
that follow schema order need not raise. Text-only mixed content does not
lose positions and need not raise either.

Parker, BadgerFish, and JsonML reject incompatible bound containers with
`container-invalid` in either mode: for example, children in simple/empty
content, non-whitespace text in element-only content, or content on a
nilled element. Best effort is not permission to silently discard invalid
content. BadgerFish and JsonML export actual attributes with bound typed
values where available, otherwise lexical strings; binding-synthesized
attribute defaults are not added to the actual attribute map.

Conversion errors carry reason codes such as `container-invalid` and a
JSON Pointer path (RFC 6901); loss warnings carry them too. The empty pointer
denotes the whole value.
Unknown format or fidelity names and inapplicable options raise `ValueError`.
