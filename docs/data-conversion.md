# Data conversion

```{eval-rst}
.. meta::
   :description: Convert bound XML to and from five JSON formats, with explicit scalar, namespace, fidelity, and document validation rules.
```

`Document.to_data()` exports the bound tree as JSON-safe Python values.
`Document.to_json()` encodes the same values as JSON text. Choose a format:

- `format="pyxsd"` (the default) keeps the native {doc}`data-model`
  structure: it absorbs the root name and groups children by name.
- `format="pyxsd-json"` is pyxsd's schema-driven format. It retains the root
  name and namespaces, and uses the XSD rather than observed child counts to
  choose scalar, object, or array layouts. Child order and mixed text positions
  are preserved where grouped formats would lose them.
- `format="parker"` ([Parker][parker]) absorbs the root, omits attributes,
  and groups children by local name.
- `format="badgerfish"` ([BadgerFish][badgerfish]) retains the root,
  attributes, and namespace bindings in a grouped structure.
- `format="jsonml"` ([JsonML][jsonml]) uses nested arrays to retain tags,
  attributes, and text/element order.

These exports need no optional dependencies. The {doc}`integrations`
provide a separate Pydantic model projection and Arrow/Parquet records;
their aliases and presence rules are not these document formats.

`Schema.from_data()` and `Schema.from_json()` import all five formats into
an ordinary `Document`. Import defaults to `format="pyxsd-json"`, unlike
export's native `"pyxsd"` default; select the format explicitly when changing
between them.

`Schema.json_schema()` constructs a Draft 2020-12 description for any of
these formats directly from the compiled XSD, without a sample document.

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

## Import data or JSON

Both methods return a `Document` and have these signatures (`JsonValue` is
a JSON-compatible Python value):

```text
Schema.from_data(
    self, data: JsonValue, *, format: FormatName = "pyxsd-json",
    element: str | None = None, namespaces: Mapping[str, str] | None = None,
    fidelity: Fidelity = "strict",
) -> Document

Schema.from_json(
    self, source: str | bytes | bytearray, *, format: FormatName = "pyxsd-json",
    element: str | None = None, namespaces: Mapping[str, str] | None = None,
    fidelity: Fidelity = "strict",
) -> Document
```

`from_data()` accepts dictionaries with string keys, lists, strings,
booleans, integers, finite floats, and `None`, subject to the selected
format's root, layout, and scalar rules below. `from_json()` accepts JSON
content as `str`, UTF-8 `bytes`, or `bytearray`. It does not open a filename,
URL, `Path`, or file-like object; unsupported source types raise `TypeError`.
Read files explicitly with
`Path("data.json").read_text(encoding="utf-8")` or `.read_bytes()`, then pass
the result to `from_json()`.

JSON parsing rejects duplicate object keys at any depth, malformed JSON,
non-UTF-8 bytes, and bare `NaN`, `Infinity`, or `-Infinity` tokens. XSD
non-finite values instead use the JSON strings `"NaN"`, `"INF"`, and
`"-INF"`.
Direct Python input rejects non-finite floats too, including nested values.

Import defaults to `fidelity="strict"`, unlike export's `"best-effort"`.
Only the [import fallbacks](#import-fidelity) below are permitted in best
effort; it does not coerce wrong scalar types, repair containers, discard
content, or suppress validation. Unknown format or fidelity names raise
`ValueError`. Neither import method accepts `typed` or `always_list`;
supplying them raises `TypeError`.

### Round trip through a document

Continuing the dates example above:

```python
restored = schema.from_data(data)
restored.require_valid()
assert restored.root is not None
assert [str(date) for date in restored.root.date] == ["2026-01-01", "2026-02-01"]
assert restored.to_data(format="pyxsd-json") == data

for source in (text, text.encode("utf-8"), bytearray(text.encode("utf-8"))):
    assert schema.from_json(source).to_data(format="pyxsd-json") == data

xml = restored.to_string()
reparsed = schema.parse(StringIO(xml))
reparsed.require_valid()
assert reparsed.to_data(format="pyxsd-json") == data
checked = restored.revalidate()
checked.require_valid()
assert checked.to_data(format="pyxsd-json") == data

for format in ("pyxsd", "parker", "badgerfish", "jsonml"):
    exported = document.to_data(format=format, fidelity="strict")
    imported = schema.from_data(exported, format=format, element="dates")
    imported.require_valid()
    assert imported.to_data(format=format, fidelity="strict") == exported
    assert schema.from_json(json.dumps(exported), format=format).is_valid
```

Import builds an ElementTree, records its namespace frames, and calls the
normal `Schema.parse()` binder. The result exposes generated classes,
datatype values, defaults, validation reports, transforms, and XML output
just like an XML-parsed document. Empty lexical content can activate an
element default; omitted attributes receive the binder's normal effective
defaults. See {doc}`data-model` for attribute presence versus defaults.

Round trips preserve represented values and order, not source bytes or
every input spelling. Defaults may replace empty input, adjacent mixed-text
strings merge, and export normalizes layout and scalar spellings.

### Root selection

- `pyxsd-json` requires a single-entry root object whose key is an EQName
  (`Q{uri}local`) or unnamespaced local name, not a lexical prefix or Clark
  name. The wrapper is required even with only one global declaration.
- BadgerFish also requires a single-entry root object, but its key is a
  lexical QName resolved against its namespace declarations.
- JsonML requires a nonempty array starting with a lexical tag string.
- Native and Parker absorb the root. Supply `element` when there is not
  exactly one concrete global root; otherwise selection raises
  `root-ambiguous`. A native mapping is content, not a root wrapper.

An empty or multi-entry root object in the root-carrying formats raises
`root-ambiguous`; a wrong outer container raises `container-invalid`.

The optional `element` selects an absorbed root or confirms a carried one.
It accepts a local name, Clark name (`{uri}local`), or EQName. A local
selection must resolve unambiguously among global declarations
(`name-ambiguous` otherwise).
For a carried root, selecting a substitution head also accepts its valid
substitution members; a contradictory selection raises `root-mismatch`.
`element` never renames a carried root. An unknown root without a
contradictory selection reaches the binder's `unknown-root` report and can
return a document whose `root` is `None`.

### Schema-driven layouts and scalar input

Import reads the `pyxsd-json` layouts described below:

- Simple values use the fixed scalar policy, including decimal strings
  and XSD-list arrays. A boolean is not an integer, and `1.0` is not an
  integer input. There is no general lexical-string coercion: `"42"` does
  not stand in for an integer JSON number.
- List layouts create one child per array item, retaining each child's
  attributes. Non-nil list-plus objects require their child-name array,
  including `[]` when empty.
- Record properties become children in schema order, regardless of JSON
  object order. Sequence arrays retain supplied child order and
  interleaving. Attribute maps must precede content; each child map has
  exactly one entry.
- Mixed arrays retain leading text, child elements, and tails. Adjacent
  strings merge into one text region. In element-only sequence input,
  whitespace-only strings are discarded; non-whitespace strings reach
  normal content validation rather than disappearing.
- `None`/`null` requests nil. Object and ordered layouts also accept their
  `#content: null` markers as shown below. Nil cannot carry other content;
  a non-nillable target is a validation problem, not a conversion failure.

An explicit `@xsi:type` EQName selects the type before layout decoding,
including promotion to a plus layout. Substitution members use their own
declarations. Conditional type assignment happens in the normal binder;
the subsequent export uses the selected effective type's layout.

### Conventional input

The [native](#native-pyxsd-structure), [Parker](#parker-structure),
[BadgerFish](#badgerfish-structure), and [JsonML](#jsonml-structure) sections
below define their structures. Scalar input follows the
[scalar table](#scalar-values): for example, decimals require strings,
booleans are not integers, and `1.0` is not integer input. Native lexical
strings are the compatibility exception. Import adds these rules:

- **Native:** accepts both typed values and `typed=False` lexical strings,
  including integer/boolean strings and space-separated XSD lists. Boolean
  content also accepts legacy integer `0`/`1`, which native export retains;
  other integers are not boolean inputs. A simple
  root may be a bare scalar or `{"$": value}`. Grouped child arrays accept
  native `always_list=True` output, including one-item arrays. Local child
  and attribute names resolve through a unique schema declaration; ambiguous
  locals raise `name-ambiguous` in both modes. Clark names identify the
  namespace directly; unresolved locals remain unqualified for validation.
  Ordinary prefixed keys are not accepted. `@xsi:type` and `@xsi:nil` (or
  their Clark-key equivalents) are exceptions: the former takes a lexical
  QName; the latter accepts a boolean or lexical string. A true nil marker
  permits other attributes but conflicts with any `$` or child property.
- **Parker:** `{}` supplies empty element-only content, `""` an empty simple
  string, `[]` an empty XSD list, and `null` always requests nil. Children use
  local names only: prefixed/Clark keys and `@` attributes are `name-invalid`.
  `$` is accepted only for mixed content. A local child must resolve uniquely
  through the schema; unresolved or ambiguous names require the warned
  best-effort fallback below. No attributes are constructed; defaults still
  apply, and missing required attributes remain validation errors.
- **BadgerFish:** accepts scalar leaves or `$`-wrapped text, `@` attributes,
  and grouped children. Repeatable child properties accept occurrence arrays
  (including `[]`) or a single non-array occurrence; export normalizes a
  single occurrence to an array. `@xsi:nil` requires a JSON boolean and its namespace
  binding. When true, it retains other attributes but conflicts with any `$`
  or child property, even empty content. Bare `null` is also accepted as nil;
  export uses the explicit attribute marker. `@xsi:type` takes a lexical QName
  and selects the actual type before decoding content.
- **JsonML:** reads attributes only from the optional object at index 1.
  Remaining nested arrays, direct text, and tails retain supplied order,
  including mixed whitespace; adjacent strings merge. Whitespace-only
  element-only text is discarded, while other text reaches validation.
  Simple content accepts one typed scalar item, or one array-valued item for
  an XSD list type, rather than interpreting it as a child tag. A sole `null`
  content item requests nil; combining nil with other content raises
  `marker-conflict`. The `xsi:type` attribute takes a lexical QName.

For a repeatable **list-valued** child, nested arrays separate occurrences
from list items: `{"refs": [["a", "b"], ["c"]]}` represents two elements.
BadgerFish always uses the outer occurrence array, including `[]` for zero
occurrences. Native and Parker accept nonempty nested occurrence arrays;
their flat arrays (including `[]`) remain ambiguous between one list value
and repeated elements and raise `container-invalid` in both modes. Omit the
property for zero occurrences. Native `always_list=True` supplies the outer
array even for one occurrence. For a non-repeatable list-valued child, the
array is one scalar value; native also accepts its one-item outer wrapper.

Native element `null` needs special care: a non-nillable target becomes
empty content; a nillable target becomes nil when empty content is not
possible. If both empty and nil are possible, strict raises
`empty-nil-ambiguous`; best effort warns and chooses nil. Empty-content
possibility depends on the schema, including defaults, required attributes,
and optional content. To request empty content explicitly, use `{}` or
`{"$": ""}` (`{"$": null}` also means empty text, not nil). Use
`{"@xsi:nil": true}` for an explicit nil request.

### Import fidelity

Native, Parker, and BadgerFish reconstruct grouped children in schema order
for supported models, including fixed sequences with distinct child names
and `xs:all` records. Array items retain occurrence order within their group.
Strict raises for each loss below; best effort emits `PyXSDWarning` and
applies only the listed fallback:

| Format | Code | Import fallback |
| --- | --- | --- |
| Native, Parker, BadgerFish | `order-ambiguous` | Use mapping iteration order and expand arrays at their property position; do not invent interleaving. |
| Native, Parker, BadgerFish | `mixed-content` | Put concatenated `$` text before the grouped children; lost text positions cannot be restored. |
| Parker | `name-ambiguous` | Leave an unresolved or ambiguous child name unqualified for normal validation. |
| Native, Parker, BadgerFish, JsonML | `namespace-unresolved` | Leave an unresolved lexical QName value to the binder. BadgerFish and JsonML also do this for unresolved names and `xsi:type`. |
| Native | `empty-nil-ambiguous` | Interpret ambiguous element `null` as nil. |

Repeated models allowing several child names can require the ordering
fallback even if a particular object looks ordered. When multiple distinct
child names are present, grouped input also conservatively rejects a present
expanded child name with multiple possible content-model positions, such as
`a` in `a?, b, a?`, even if occurrence counts could force one allocation.
Strict raises `order-ambiguous`; best effort warns and uses mapping order.
Use `pyxsd-json` or JsonML when explicit interleaving is needed.

Mixed content with multiple distinct child names also loses child order;
text-only mixed content needs no positional fallback. JsonML represents order
directly and needs neither ordering nor mixed-text fallback. `pyxsd-json` has
no import fallbacks: both fidelities use the same decoding rules without loss
warnings.

### Namespaces and QName values

A `namespaces` mapping seeds the root's inherited prefix-to-URI frame for
every format. Native and Parker need it for prefixed QName values; it does
not enable prefixed child keys. Native resolves `@xsi:type` when its prefix
is bound; an unresolved type prefix instead reaches the binder's
`unknown-namespace-prefix` report, even in strict mode.

BadgerFish merges `@xmlns` declarations (`$` denotes the default namespace);
JsonML merges `xmlns` and `xmlns:prefix` from the attribute object. Nested
declarations override inherited bindings and apply to the element carrying
them, including its own tag, and its descendants. Each BadgerFish occurrence
in a repeated array has its own frame; sibling declarations do not leak.
Unprefixed attributes
never use the default namespace. Required bindings for names, QName values,
and `xsi:type` must resolve in strict mode. Import accepts supplied prefixes
and default namespaces; the deterministic explicit prefixes described
below are export choices, not input requirements.

In `pyxsd-json`, element and attribute keys use EQNames, including wildcard
names; `namespaces` does not change that contract. QName values, QName-list
items, and `@xsi:type` values can carry their
namespace directly as EQNames. Import translates these to lexical XML
QNames and records inherited/generated bindings on every element before
binding. Existing suitable prefixes are reused; generated prefixes avoid
collisions. Original prefix spelling is not preserved.

This complete example needs no caller-supplied prefix declarations:

```python
from io import StringIO

import pyxsd

qname_schema = pyxsd.compile(
    StringIO("""
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           targetNamespace="urn:example">
  <xs:element name="ref" type="xs:QName"/>
</xs:schema>
""")
)
qname_data = {"Q{urn:example}ref": "Q{urn:values}item"}
qname_document = qname_schema.from_data(qname_data, element="{urn:example}ref")
qname_document.require_valid()
assert qname_document.to_data(format="pyxsd-json") == qname_data
qname_copy = qname_schema.from_json(qname_document.to_json(format="pyxsd-json"))
qname_copy.require_valid()
assert qname_copy.to_data(format="pyxsd-json") == qname_data

qname_reparsed = qname_schema.parse(StringIO(qname_document.to_string()))
qname_reparsed.require_valid()
assert qname_reparsed.to_data(format="pyxsd-json") == qname_data
```

QName JSON round trips retain expanded names (namespace URI and local name).
In namespace-aware mode, `to_string()` also preserves resolved QName values
in element content, declared attributes, XSD lists, and selected union members, for both imported
and XML-parsed documents. The writer declares namespaces used only by these
values and re-spells the values with its selected prefixes, including values
originally resolved through a default namespace. Prefixes and source bytes
are not preserved; compare expanded names rather than lexical spellings.
Unresolved QName values pass through unchanged; the writer does not invent
missing namespace bindings.
Actual types selected by `xsi:type` also contribute value-only namespaces;
the XML writer declares those namespaces and re-spells the type marker.
This also applies to redundant native markers equal to the declared type.
An unresolved marker is retained, not repaired by guessing the bound class.

The same schema must not import or parse concurrently or reentrantly.
Separate `Schema` objects can compile and parse concurrently.

### Conversion errors versus validation reports

In every format, conversion raises `PyXSDError` when a trustworthy tree
cannot be built: for example, duplicate JSON keys (`json-duplicate-key`),
malformed JSON (`json-invalid`), invalid names (`name-invalid`), wrong containers
(`container-invalid`), wrong scalar types (`scalar-type-invalid`), or
conflicting nil/content markers (`marker-conflict`). Diagnostics include a
reason code, selected format, and JSON Pointer path; the empty pointer identifies the whole
input. JSON parsing errors use that empty pointer. Best-effort warnings
carry reason codes and paths too; they do not become validation issues.
Ambiguous root selection, malformed names, wrong containers/scalar types,
and duplicate or conflicting markers always raise in either fidelity mode.

Correctly shaped content still undergoes XSD validation. Invalid lexical
strings for string-represented datatypes, missing required content,
unknown children or attributes in decodable containers, unresolved actual
types, and non-nillable nil requests reach `Document.report`. Parker names
must first resolve or use its warned fallback. Import does not drop unknown
content or automatically call `require_valid()`.
Inspect the report, or call `require_valid()` to raise `ValidationError`.

Using the dates schema from the first example:

```python
from pyxsd.exceptions import PyXSDError, ValidationError

try:
    schema.from_data({"dates": [5]})
except PyXSDError as error:
    assert "scalar-type-invalid at '/dates/0'" in str(error)
else:
    raise AssertionError("A date requires a JSON string")

invalid = schema.from_data({"dates": ["not-a-date"]})
assert invalid.root is not None
assert invalid.report.has_errors
try:
    invalid.require_valid()
except ValidationError:
    pass
else:
    raise AssertionError("Invalid date content must fail validation")
```

## Construct a JSON Schema

`Schema.json_schema(*, format="pyxsd-json", element=None, fidelity="strict",
typed=True, always_list=False)` returns a JSON-safe dictionary. Generation
needs no optional dependency. To check data with the separate
[`jsonschema`](https://python-jsonschema.readthedocs.io/en/stable/) package,
install it with `pip install jsonschema`.

Continuing the dates example:

```python
from jsonschema import Draft202012Validator

for format in ("pyxsd", "pyxsd-json", "parker", "badgerfish", "jsonml"):
    contract = schema.json_schema(format=format, element="dates")
    Draft202012Validator.check_schema(contract)
    exported = document.to_data(format=format, fidelity="strict")
    Draft202012Validator(contract).validate(exported)

native_contract = schema.json_schema(format="pyxsd", element="dates", typed=False, always_list=True)
Draft202012Validator(native_contract).validate(
    document.to_data(format="pyxsd", typed=False, always_list=True)
)
```

### Roots and export options

`element` accepts the same local, Clark, and EQName selections as import;
selecting a substitution head includes its permitted members. An ambiguous
local name raises `name-ambiguous`; malformed names raise `name-invalid`.
Without `element`, root-carrying `pyxsd-json`, BadgerFish, and JsonML describe
all concrete global roots, using `anyOf` for alternatives. No concrete roots
produces a schema that rejects every value. Native and Parker instead require
exactly one concrete global root or an explicit selection (`root-ambiguous`
otherwise). An unknown explicit selection yields a general format shape,
not evidence that the XSD declares that root.

Match native `typed` and `always_list` to the intended export. Lexical mode
describes strings, including space-separated XSD lists; `always_list=True`
requires arrays for child properties. Other formats reject non-default values
for these options with `ValueError`. There is no `namespaces` override.

The schemas describe the [structures below](#native-pyxsd-structure), not
every spelling import accepts. In particular, native primitive roots have
their `$` wrapper, BadgerFish schema-repeatable children use arrays and its
nil marker is boolean, and JsonML fixes tags with `prefixItems`. Generated
namespace-declaration properties describe root-scoped export, not every
nested namespace frame accepted on import. Native `$` text inside an object
is lexical even in typed mode; BadgerFish `$` simple content stays typed.
Parker keeps empty element-only `{}` distinct from nil `null`.

### Metadata and validation limits

Every result includes `$schema: "https://json-schema.org/draft/2020-12/schema"`,
`x-pyxsd-format`, and `x-pyxsd-validation` with `level: "structural"` and a
sorted `unvalidated` list. Only `pyxsd-json` adds `x-pyxsd-profile-version: 1`.
Reusable and recursive shapes use deterministic `$defs`/`$ref`; annotations
such as `x-pyxsd-qname`, `x-pyxsd-type`, and `x-pyxsd-layout` identify their
XSD origin or representation.

Generation encodes scalar types, expressible bounds, enumerations, lengths,
and tested pattern equivalents. It discloses remaining rules as applicable:
`xsd-particle-order`, `xsd-mixed-order`, `xsd-wildcard-namespace`,
`xsd-conditional-type`, `xsd-identity-constraints`, `xsd-assertions`,
`xsd-regex-dialect`, and `xsd-facet`. Native typed null ambiguity adds
`native-null-ambiguity`; Parker QName context loss adds `namespace-lost`.
Lexical QName schemas carry `x-pyxsd-namespace-context: true`; matching their
pattern does not resolve their prefixes. JSON Schema `format` and
`contentEncoding`, when present, are annotations unless a validator enforces
them.

The description is deliberately broader than the conversion rules in places.
Sequence/mixed item alternatives do not enforce every particle, cardinality,
or attribute-first rule. JsonML's optional attribute object shifts content
positions; tag and item schemas do not fully validate that placement.
Native and BadgerFish merge effective-type properties without expressing
every dependency between an actual type, required attributes, text, and nil.
For nillable parents, their child properties are not unconditionally required:
a nilled element has no children. Required attributes remain required.
Even an empty `unvalidated` list is not a promise of complete XSD validation
or import acceptance. Import with the matching format, inspect the report,
and call `require_valid()` for the normal XSD checks.

Both generation fidelities currently produce the same description for
supported compiled schemas, without loss warnings. `fidelity="strict"`
does not make JSON Schema an XSD validator, nor does successful generation
guarantee that strict export or import can represent a particular document.

This API is separate from the optional {doc}`integrations`:
`ModelSet` models' `model_json_schema()` describes Pydantic's aliases,
presence rules, and supported projection shapes, not these five document
formats. Neither projection changes the other's contract.

## Scalar values

With the default `typed=True`, all five formats use these JSON-safe values:

| Bound value | JSON value |
| --- | --- |
| Boolean | Native: integer `1` or `0`; other formats: `true` or `false` |
| Integer and integer-derived types | Integer number |
| Decimal | String, never a binary float; new formats use fixed-point; native retains legacy `str()` spelling |
| Finite float or double | Number |
| Non-finite float or double | `"INF"`, `"-INF"`, or `"NaN"` |
| XSD list value | Array, including zero or one item |
| QName | EQName string in `pyxsd-json`; lexical QName in the other formats |
| Other atomic values, including dates, durations, binary values, and URIs | Datatype lexical string |

Python data uses `bool` and `None` where JSON uses booleans and `null`.
`to_json()` uses `allow_nan=False` and no fallback serializer.
Unions follow the selected member's scalar contract recursively, including
QName namespaces, list items, numeric values, and non-finite spellings.
Import selects a member using its required JSON representation and XSD
lexical validity; JSON Schema describes the member representations as alternatives.

Native `typed=False` retains lexical text, including the space-separated
string for an XSD list value. `to_dict()` remains the raw-value interface:
it keeps bound Python/XSD scalar objects instead of normalizing them for
JSON. Native JSON keeps its existing structure and decimal/date/QName
strings and Boolean `0`/`1`, with two scalar changes: non-finite numbers use the strings above,
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
QName scalar values also use EQNames; on export, unresolved QName namespace
context raises `namespace-unresolved`, even in best-effort mode. Prefer the default
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
Consumers must understand this schema-typed extension to distinguish list
values from nested element arrays; the array shape alone is insufficient.
Empty simple string content is `["root", ""]`; empty complex content is
`["root"]`. Nil adds one `null` content item after any attributes, with no
other content: `["root", {"id": "r"}, null]`. It does not add `xsi:nil`.
An explicit actual type uses an `xsi:type` attribute with a lexical QName.

## Lexical namespaces in BadgerFish and JsonML

Both formats synthesize deterministic lexical QNames rather than preserving
source prefixes. Export and JSON Schema generation use the same compiled-schema
closure of declared types, conditional alternatives, and `xsi:type` target
types. Namespace URIs reachable through that closure receive sorted `ns0`,
`ns1`, … prefixes, including reservations for absent declarations and unselected
types; numbering does not depend on which types an instance uses.
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

## Interoperability

Format names do not guarantee identical output across implementations.
The per-format contract tests in `tests/test_data_export.py` compare selected
fixtures with **xmlschema 4.3.2**, a development-only dependency. Those results
are comparison evidence, not normative definitions or proof of general
conformance. The export rules on this page define pyxsd's profiles:

- **Native `pyxsd` and `pyxsd-json`:** xmlschema has no equivalent profiles.
  Explicit expected-output fixtures pin native grouped mixed content and
  profile 1's ordered sequence arrays instead.
- **Parker:** the tested simple, repeated, ordered, nilled, and local-name
  projections agree. Intentional differences include keeping mixed text
  under `$` in best effort rather than dropping it, and using `{}` for empty
  complex content where xmlschema's tested empty leaf uses `null`.
  Matching projections can still emit pyxsd `attributes-lost` warnings where
  xmlschema silently drops attributes; strict pyxsd export rejects that loss.
- **BadgerFish:** the recursive fixture agrees, but pyxsd emits simple leaves
  directly as typed scalars where xmlschema wraps them under `$`. pyxsd uses
  boolean `@xsi:nil` and concatenates mixed text under `$` in best effort;
  xmlschema's fixtures use lexical `"true"` and numbered `$1`/`$2` text keys.
- **JsonML:** simple, attributed, repeated, ordered, and recursive fixtures
  agree. pyxsd preserves mixed whitespace that xmlschema trims in the tested
  fixture, and uses one `null` content item for nil rather than an `xsi:nil`
  attribute. Consumers also need the schema-typed extension described above.

In all three conventional formats, pyxsd keeps decimal `1.20` as `"1.20"`
where xmlschema's comparison fixtures emit the number `1.2`. BadgerFish and
JsonML use explicit deterministic prefixes with root-scoped declarations;
the namespaced comparison fixtures use a default namespace in xmlschema.
Check these scalar, nil, namespace, and mixed-content choices before passing
exports to another implementation. Strict fidelity does not switch profiles
or guarantee that another implementation accepts the result.

## Ordering and fidelity

Export is deterministic for the same bound tree. In `pyxsd-json`, repeated,
sequence, and mixed arrays follow document order. Object layouts emit an
explicit type marker first, then declared attributes in declaration order,
then sorted wildcard attributes; record children follow schema order.
Sequence/mixed attribute maps sort their keys instead, because attribute
order is not semantic.

The following fidelity rules describe **export**; see
[import fidelity](#import-fidelity) for reconstruction fallbacks.
`fidelity` concerns representation, not XSD validity:

- Native `best-effort` keeps the existing grouped shape without new warnings.
- Native `strict` raises `PyXSDError` for empty/nil ambiguity
  (`empty-nil-ambiguous`), mixed text/element positions (`mixed-content`),
  interleaved repeated names (`order-ambiguous`), and namespaced elements
  reduced to local names by legacy binding (`namespace-lost`). Namespace-dependent
  or unresolved QName values in content, lists, attributes, or selected union
  members also raise `namespace-lost`: native data carries no bindings for them.
  An explicit
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

JsonML diagnostics are relative to the output array, not an XML tag path.
A root container error uses `""`; a root attribute named `ref` uses `/1/ref`.
Content starts at `/1` without an attribute object or `/2` with one, including
an object added solely for root namespace declarations. A child at `/1` has
its own attribute at `/1/1/ref`; its tag name is not a pointer segment.
Object-key tokens escape `~` as `~0` and `/` as `~1`.

Unknown format or fidelity names and inapplicable options raise `ValueError`.

[parker]: https://wiki.open311.org/JSON_and_XML_Conversion/#the-parker-convention
[badgerfish]: http://www.sklar.com/badgerfish/
[jsonml]: https://jsonml.org/
