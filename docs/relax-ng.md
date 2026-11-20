# Native RELAX NG support

```{eval-rst}
.. meta::
   :description: Compile, validate, query, convert, and project native RELAX NG 1.0 schemas with pyxsd, including alternatives, annotations, models, records, comparison, and deliberate exclusions.
```

`pyxsd.relaxng` is a native RELAX NG 1.0 XML-syntax processor. It compiles
grammars into a pattern graph, validates instances with a derivative validator,
and keeps one authoritative lexical XML tree. It does not translate RELAX NG
into XSD, invent global declarations, or route RELAX NG work through the XSD
compiler. It shares pyxsd's reports, resource policies, XML limits, XPath,
writers, transforms, batch orchestration, data conversion, and optional
integrations where their semantics are defined.

```{note}
RELAX NG is a separate schema language with a separate compiler and document
type. `pyxsd.compile` / `Schema` remain the XSD entry points. Use
`pyxsd.relaxng.compile` / `RelaxNGSchema` for RELAX NG.
```

(relaxng-quick-start)=

## Quick start

The compiler accepts a local path or a readable text/binary stream. Remote
resources require an authorizing `resource_policy`; an already-parsed
`Element` is not a schema source because it cannot carry parser evidence.
`RelaxNGSchema.parse` accepts a path, a readable stream, or a caller-trusted
`Element`.

```python
from io import StringIO

from pyxsd import relaxng
from pyxsd.relaxng import StaleInterpretationError

SCHEMA = """<grammar xmlns="http://relaxng.org/ns/structure/1.0">
  <start>
    <element name="record">
      <attribute name="id">
        <data type="integer"
              datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/>
      </attribute>
      <group>
        <element name="title"><text/></element>
        <zeroOrMore><element name="tag"><text/></element></zeroOrMore>
      </group>
    </element>
  </start>
</grammar>"""

XML = '<record id="7"><title>Rain</title><tag>weather</tag><tag>field</tag></record>'

schema = relaxng.compile(StringIO(SCHEMA))
assert schema.report.diagnostics_complete is True
schema.require_valid()

document = schema.parse(StringIO(XML), capture="patterns")
assert document.typed is True
assert document.is_valid
assert document.root.id == 7
assert document.root.title.text_value() == "Rain"
assert str(document.root.title.scalar_value()) == "Rain"
assert [tag.text_value() for tag in document.root.tag] == ["weather", "field"]
assert document.xpath("string(/record/title)") == "Rain"

# Edits update the lexical tree immediately and make old interpretations stale.
document.root.title.set_text("Sun")
assert document.interpretations_current is False
assert document.to_string() == (
    '<record id="7"><title>Sun</title><tag>weather</tag><tag>field</tag></record>'
)
try:
    document.annotations
except StaleInterpretationError:
    pass
else:
    raise AssertionError("an edited revision has stale annotations")

fresh = document.revalidate()
assert fresh.is_valid
assert fresh.interpretations_current is True
assert fresh.root.title.text_value() == "Sun"
```

The complete compilation contract is:

```text
pyxsd.relaxng.compile(
    source, *, datatype_libraries=None, resource_policy=None,
    xml_policy=None, xml_limits=None, limits=None,
) -> RelaxNGSchema

RelaxNGSchema.parse(
    xml, *, xml_policy=None, xml_limits=None, typed=True,
    capture="none", capture_xdm=False, limits=None,
) -> RelaxNGDocument
```

`typed=True` is the default and gives a valid document generated projections
for stable routes, scalar access, and complete binding evidence. `typed=False`
is validation-only parsing: it returns the generic lexical tree, writers,
XPath, XDM, and lexical conversion without typed accessors, annotations, or
projections. It never means "partially typed." `capture="patterns"` retains
public pattern provenance and requires `typed=True`; the combination with
`typed=False` raises `ValueError` before input is read. `capture_xdm=True`
retains an unannotated XML-tree view; it is not an XSD schema-annotation view.

A schema with compilation errors rejects instance parsing before consuming
input. Each parse returns a fresh report containing frozen schema findings
followed by instance findings. Malformed XML raises `PyXSDError`; grammar
mismatches are reported on the returned document. Call `require_valid()` when
findings must stop processing.

## Validation, recovery, and completeness

Instance findings use stable codes such as `unexpected-element`,
`unexpected-attribute`, `missing-attribute`, `missing-content`,
`invalid-scalar`, `unexpected-content`, and `invalid-structure`. Findings
identify exact source occurrences where available; a missing child identifies
its container, and `location.precision` says `exact` or `container`.
`Document.locate(location)` resolves a parse-specific address to a
`NodeLookup` with status `bound`, `attribute`, or `missing`; an address from a
different parse is `missing`, never a positional guess.

Recovery collects every independent case in source order. Recovered states do
not make a document valid, and invalid typed documents expose no authoritative
scalar or annotation claims. A validation-only document never pretends capture
ran: `document.annotations` raises a capability error.

Diagnostic retention is independently bounded. `RelaxNGLimits(max_diagnostics=...)`
truncates retained issues but keeps their counts. A truncated report has
`diagnostics_complete` set to `False` and a nonzero `suppressed_count`; the
truncated report stays invalid. Truncation never raises a limit error and never
turns an invalid document into a valid one.

```python
from io import StringIO

from pyxsd import relaxng
from pyxsd.relaxng import RelaxNGLimitError, RelaxNGLimits

SCHEMA = """<grammar xmlns="http://relaxng.org/ns/structure/1.0">
  <start>
    <element name="value">
      <choice>
        <data type="integer"
              datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/>
        <data type="string"
              datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/>
      </choice>
    </element>
  </start>
</grammar>"""

schema = relaxng.compile(StringIO(SCHEMA))

limited_report = schema.parse(
    StringIO("<value><extra/></value>"),
    limits=RelaxNGLimits(max_diagnostics=0),
)
assert limited_report.is_valid is False
assert limited_report.report.diagnostics_complete is False
payload = limited_report.report.to_dict()
assert payload["diagnostics_complete"] is False
assert payload["suppressed_count"] == payload["counts"]["error"] > 0
assert payload["issues"] == []

limited_capture = schema.parse(
    StringIO("<value>12</value>"),
    capture="patterns",
    limits=RelaxNGLimits(max_capture_entries=0),
)
assert limited_capture.annotations.complete is False
assert limited_capture.annotations.for_node(limited_capture.root).complete is False
assert str(limited_capture.root.scalar_value()) == "12"

try:
    relaxng.compile(StringIO(SCHEMA), limits=RelaxNGLimits(max_pattern_nodes=0))
except RelaxNGLimitError as exc:
    assert "max_pattern_nodes" in str(exc)
else:
    raise AssertionError("a work-budget exhaustion must raise RelaxNGLimitError")
```

A capture budget truncates only the optional public annotation table. Retained
entries are a sound subset, every queried node reports `complete=False`, and
typed values and candidate identities are unaffected. Mandatory binding
evidence has no truncation mode: exhausting it raises `RelaxNGLimitError`
instead of returning guessed typed evidence.

## Mutable tree, typed access, and revision state

`RelaxNGDocument` subclasses `Document`; its `schema` is a `RelaxNGSchema` and
its root is a `RelaxNGElement`. The lexical tree is authoritative and stores
expanded names, lexical attribute and character values, child order, mixed
text/tail order, and recorded namespace and base-URI context. Generated
projection classes are views over that tree, not a second object graph.

- `set_text(lexical)` and `set_attribute(expanded_name, lexical)` accept
  strings and never choose a datatype. Child collections, insertion, removal,
  attribute deletion, names, text/tails, namespace bindings, and base URIs are
  mutation-aware.
- A uniquely typed scalar descriptor accepts an ordinary Python value when its
  compiled contract has one supported encoding. Encoding happens before the
  tree changes, and an encoding error leaves it unchanged. Encoding does not
  certify whole-document validity.
- At an ambiguous scalar position, assignment accepts an `AlternativeValue`
  snapshot or an explicit lexical edit, never a bare Python scalar. A snapshot
  contributes lexical text and namespace context, never old validation
  authority.
- Any successful mutation marks the whole revision dirty. `report` and
  `is_valid` remain the preceding validation snapshot;
  `interpretations_current` reports freshness. Writers and lexical traversal
  read the current tree without revalidation. Typed scalar reads and
  `document.annotations` raise `StaleInterpretationError` while dirty.
- `revalidate()` parses an independent clone with the same schema, typed mode,
  capture setting, and instance limits. It returns fresh nodes, a fresh report,
  fresh evidence and annotations, and new source addresses; it never refreshes
  the old document or its immutable snapshots.

`write`, `to_string`, `to_xdm`, `transform`, `xpath`, `walk`, `find`, and
`findall` share the ordinary document behavior. Ordinary XPath reads the bound
lexical tree. RELAX NG pattern annotations may inform Python transforms through
`document.annotations`, but they are not XSD type annotations in XDM.

## Scalar alternatives, lists, and annotations

A scalar position with one schema-defined, decodable atomic contract returns
its ordinary Python value. A position with several possible contracts returns
an immutable, non-preferential `AlternativeValue`, even when a particular
lexical value satisfies only one. A single nondecodable contract, including
plain `text`, also returns the wrapper; validation never silently becomes a
string conversion. `str(value)` is the original lexical form and does not claim
that the schema selected `xsd:string`.

```python
from io import StringIO

from pyxsd import relaxng
from pyxsd.relaxng import (
    AlternativeValue,
    AmbiguousValueError,
    ListInterpretation,
)

SCHEMA = """<grammar xmlns="http://relaxng.org/ns/structure/1.0">
  <start>
    <element name="value">
      <choice>
        <data type="integer"
              datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/>
        <data type="string"
              datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/>
      </choice>
    </element>
  </start>
</grammar>"""

schema = relaxng.compile(StringIO(SCHEMA))
document = schema.parse(StringIO("<value>12</value>"), capture="patterns")
value = document.root.scalar_value()

assert isinstance(value, AlternativeValue)
assert value.lexical == "12"
assert str(value) == "12"
assert len(value.candidates) == 2
assert {candidate.contract.type_name for candidate in value.candidates} == {
    "integer",
    "string",
}
assert value.as_type("xsd:integer") == 12
assert value.as_type("xsd:string") == "12"
for candidate in value.candidates:
    assert str(value.as_candidate(candidate.candidate_id)) == "12"
try:
    value.as_python()
except AmbiguousValueError:
    pass
else:
    raise AssertionError("ambiguous values require an explicit selection")

annotations = document.annotations
assert annotations.complete is True
node_info = annotations.for_node(document.root)
assert node_info.complete is True
assert node_info.patterns
assert "element0" in node_info.definitions
assert len(node_info.scalar) == 1
scalar = node_info.scalar[0]
assert scalar.kind == "text"
assert scalar.content_index == 0
assert scalar.name is None
assert tuple(c.candidate_id for c in scalar.value.candidates) == tuple(
    c.candidate_id for c in value.candidates
)

LIST_SCHEMA = """<grammar xmlns="http://relaxng.org/ns/structure/1.0">
  <start>
    <element name="point">
      <list>
        <choice>
          <data type="integer"
                datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/>
          <data type="string"
                datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/>
        </choice>
        <data type="integer"
              datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/>
      </list>
    </element>
  </start>
</grammar>"""

list_schema = relaxng.compile(StringIO(LIST_SCHEMA))
list_document = list_schema.parse(StringIO("<point>12 34</point>"))
list_value = list_document.root.scalar_value()
assert isinstance(list_value, AlternativeValue)
assert len(list_value.interpretations) == 1
interpretation = list_value.interpretations[0]
assert isinstance(interpretation, ListInterpretation)
assert interpretation.lexical == "12 34"
assert interpretation.shape == "interpretation"
assert interpretation.as_types(("xsd:integer", "xsd:integer")) == (12, 34)
try:
    list_value.as_python()
except AmbiguousValueError:
    pass
else:
    raise AssertionError("correlated token alternatives require as_types()")
```

`AlternativeValue` keeps `lexical`, candidate contracts, optional decoded
values, its datatype context, grammar fingerprint, and parse revision.
Candidate IDs are deterministic opaque identifiers scoped to the compiled
grammar and datatype-library identities; they are not class names,
prefix-dependent datatype spellings, or whole-derivation IDs. Candidates order
by ID without preference semantics. `as_type` selects an atomic datatype, not
a grammar branch: `"xsd:integer"` means the RELAX NG XSD datatype-library URI,
never an instance prefix binding; a general selector is
`(library_uri, type_name)`.

RELAX NG `list` is not one atomic datatype. A fixed heterogeneous list can
project to a typed tuple and a homogeneous repeated item to a typed list.
Optional, repeating, or ambiguous token assignments expose a
`ListInterpretation` with the original lexical string, XML-whitespace token
spans, per-token marginal candidates, and a compact constraint graph.
`as_types(selectors)` succeeds only when the whole assignment occurs in a
retained successful list derivation; it rejects cross-product combinations the
grammar forbids. `as_python()` succeeds only when one complete decodable
interpretation remains. An empty permitted list is valid and has no token
groups.

`capture="patterns"` returns a `NodeAnnotations` view per node with `patterns`,
`definitions`, `scalar`, and `complete`. There is deliberately no singular
`matched_pattern` or `matched_type`: named definitions are reusable grammar
patterns, not XSD types. Validation never depends on annotation capture, and a
truncated capture table never changes typed values or candidate identities.

## Structural projections and stubs

Compilation builds one finite projection plan shared by generated binding,
stubs, data conversion, JSON Schema, Pydantic, and Arrow. A route is stable
when every reachable assignment admits one schema-static public contract
without choosing an interpretation or discarding XML structure. Stable routes
receive generated classes; incompatible subroutes use generic
`RelaxNGElement` nodes while stable parents and siblings keep their
projections. Instance values never choose a class layout.

`RelaxNGSchema.projection` is the immutable route graph.
`RelaxNGSchema.projection_classes` is the generated-class factory for the plan,
so repeated parses share class identity. `RelaxNGSchema.dependencies` is the
canonical resource closure. `RelaxNGSchema.stub(*, compact=False,
component_ids=None, depth=None)` emits a full `.pyi` typing surface or the
compact agent view; component IDs filter routes and unknown IDs raise
`KeyError`. Stub emission never calls `require_valid()`; a schema with
compilation errors emits an explanatory comment instead.

Introspection lives in `pyxsd.relaxng.introspection`:

- `describe_schema(schema)` returns starts, components, and dependencies.
- `find_components(schema, *, text=None, kind=None, namespace=None)` filters
  the compiled grammar inventory.
- `describe_component(schema, component_id)` returns one exact component.
- `related_components(schema, component_id, *, direction="uses"|"used_by",
  depth=...)` traverses dependency edges.

Component IDs cover normalized grammar semantics and dependency-content
identities, never source paths, object addresses, processing hash seeds, or
mapping insertion order. Relocating an identical resource closure changes
locations, not IDs. There are no XSD-only fields such as global declarations,
Python bindings, or defaults.

```python
from io import StringIO

from pyxsd import relaxng
from pyxsd.relaxng.comparison import compare_relaxng_structure
from pyxsd.relaxng.introspection import (
    describe_schema,
    find_components,
    related_components,
)

BASE = """<grammar xmlns="http://relaxng.org/ns/structure/1.0">
  <start><element name="note"><text/></element></start>
</grammar>"""

CHANGED = """<grammar xmlns="http://relaxng.org/ns/structure/1.0">
  <start>
    <element name="note">
      <attribute name="lang"><text/></attribute>
      <text/>
    </element>
  </start>
</grammar>"""

base = relaxng.compile(StringIO(BASE))
changed = relaxng.compile(StringIO(CHANGED))

description = describe_schema(base)
assert description.start_ids
assert {component.kind for component in description.components} >= {
    "grammar",
    "define",
    "pattern",
    "name-class",
    "route",
    "resource",
}
routes = find_components(base, kind="route")
assert len(routes) == 1
assert routes[0].name == "note"
assert related_components(base, description.start_ids[0], direction="uses", depth=2)

same = relaxng.compile(StringIO(BASE))
empty = compare_relaxng_structure(base, same)
assert empty["totals"] == {"added": 0, "removed": 0, "changed": 0}

diff = compare_relaxng_structure(base, changed)
assert diff["status"] == "compared"
assert diff["totals"]["added"] > 0
assert "projections" in diff["categories"]
assert "proves neither language equivalence nor inclusion" in diff["statement"]

stub = base.stub(compact=True)
assert "class Note" in stub
assert "AlternativeValue" in stub
```

(relaxng-data-formats)=

## Data formats, tagged values, and JSON Schema

`RelaxNGDocument.to_dict`, `to_data`, and `to_json` plus
`RelaxNGSchema.from_data`, `from_json`, and `json_schema` support the same five
format names and options as their XSD counterparts. The shared structures,
fidelity rules, and format details stay in {doc}`data-conversion`; this page
covers only the RELAX NG-specific semantics.

The format names are `"pyxsd"`, `"pyxsd-json"`, `"parker"`, `"badgerfish"`,
and `"jsonml"`. Export defaults are `format="pyxsd"`,
`fidelity="best-effort"`, `typed=True`, and `always_list=False`; import
defaults are `format="pyxsd-json"` and `fidelity="strict"`.
`typed` and `always_list` are native-only: another format rejects a
nondefault value with `ValueError`, and imports accept neither option.

`pyxsd-json` uses a RELAX NG projection planner rather than XSD layout or
global declarations. `element=None` describes every root admitted by the start
pattern; an explicit local, Clark, or EQName spelling narrows and checks that
choice. Open start-name classes stay symbolic. A generic route uses the mixed
ordered envelope with optional leading attributes, text in place, and
singleton EQName child objects; its scalar slot uses `{"#content": ...}` even
without attributes. The layout is fixed by the compiled route, not selected
from observed JSON.

An `AlternativeValue` in native typed output and in the RELAX NG `pyxsd-json`
profile uses one versioned profile-1 tag: an object with exactly one `#value`
member, whose body has exactly `version`, `grammar`, `lexical`, `context`, and
`candidates`. `{"#value": ...}` is recognized only in scalar positions of the
selected RELAX NG contract. `version` is the integer `1`; `lexical` is the
complete scalar string; `context` has exactly `base_uri` and `namespaces`;
`candidates` is a nonempty, sorted, duplicate-free array of candidate-ID
strings. Decoded Python objects, proof graphs, and source locations are never
embedded. A tag has this shape (candidate IDs are placeholders):

```json
{
  "#value": {
    "version": 1,
    "grammar": "<compiled-grammar-id>",
    "lexical": "12",
    "context": {"base_uri": null, "namespaces": {}},
    "candidates": ["<candidate-id>", "<candidate-id>"]
  }
}
```

Import reconstructs lexical XML and context, validates the whole document, and
recomputes all candidates: supplied IDs are assertions, never filters or
permissions.

Conventional formats keep their existing scalar policy for unique
representable values. At ambiguous or nondecodable positions they carry
lexical text without invented metadata and reconstruct interpretations by
validation on import. BadgerFish and JsonML are generic XML-tree adapters;
Parker and native are grouped adapters with their documented fidelity losses.
Native `typed=False` and JsonML remain lexical escape routes for invalid or
edited trees; typed schema-directed export requires a valid, current revision.

`RelaxNGSchema.json_schema()` emits Draft 2020-12 for the selected RELAX NG
data contract, including tagged alternatives and every supported export option.
The RELAX NG profile advertises `x-pyxsd-profile: "relaxng"` and
`x-pyxsd-profile-version: 1`. The result is structural: grammar validity, name
classes, datatype contexts, candidate identity, and unexpressed ordering are
listed under `x-pyxsd-validation.unvalidated`. It is never full RELAX NG
validation, and successful schema generation does not by itself guarantee that
a particular import or strict export can represent a document.

A bare `null` is not defined for core RELAX NG: it is neither empty content nor
nil, and no importer adds `xsi:nil` to make an empty JSON value valid. The
profile uses `#content: ""` for empty scalar content and an attribute-only
object or sequence attribute map for empty structured content. `xsi:type` and
`xsi:nil` XML attributes are ordinary attribute data under RELAX NG; they
cannot select a layout, dispatch a type, or request nil. XSD-only actual-type
and nil shortcuts are rejected as markers.

```python
from io import StringIO

from pyxsd import relaxng

SCHEMA = """<grammar xmlns="http://relaxng.org/ns/structure/1.0">
  <start>
    <element name="value">
      <choice>
        <data type="integer"
              datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/>
        <data type="string"
              datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/>
      </choice>
    </element>
  </start>
</grammar>"""

schema = relaxng.compile(StringIO(SCHEMA))
document = schema.parse(StringIO("<value>12</value>"))

profile = document.to_data(format="pyxsd-json")
tag = profile["value"]["#value"]
assert tag["version"] == 1
assert set(tag) == {"version", "grammar", "lexical", "context", "candidates"}
assert tag["lexical"] == "12"
assert tag["context"] == {"base_uri": None, "namespaces": {}}
assert len(tag["candidates"]) == 2
assert tag["candidates"] == sorted(set(tag["candidates"]))

assert document.to_data(format="pyxsd", typed=False) == {"$": "12"}
assert document.to_data(format="pyxsd", typed=True) == {"$": profile["value"]}
assert document.to_data(format="parker", fidelity="strict") == "12"
assert document.to_data(format="badgerfish", fidelity="strict") == {"value": "12"}
assert document.to_data(format="jsonml", fidelity="strict") == ["value", "12"]

restored = schema.from_data(profile, format="pyxsd-json")
assert restored.is_valid
assert restored.to_string() == "<value>12</value>"

contract = schema.json_schema(format="pyxsd-json")
assert contract["$schema"] == "https://json-schema.org/draft/2020-12/schema"
assert contract["x-pyxsd-profile"] == "relaxng"
assert contract["x-pyxsd-profile-version"] == 1
assert contract["x-pyxsd-validation"]["level"] == "structural"
assert "relaxng-grammar-validity" in contract["x-pyxsd-validation"]["unvalidated"]

try:
    schema.from_data({"value": None}, format="pyxsd-json")
except Exception as exc:
    assert "bare null" in str(exc)
else:
    raise AssertionError("bare null must stay a conversion error")
```

## Pydantic models and Arrow records

The optional integrations accept `RelaxNGSchema` directly.
`pyxsd.integrations.pydantic.models(schema)` returns a `RelaxNGModelSet` with
`model_for`, `from_document`, `from_node`, and `to_document`. Models are built
from the compiled projection, before any instance exists; repeated resolution
of the same route returns the same class. Input/output aliases use the existing
conventions: expanded-name children, `@` plus expanded-name attributes, and `$`
for simple content. Independent model input supports `model_validate` and
`model_validate_json`, dumps, and `model_json_schema`; model validation checks
the selected route contract, not whole-document XML validity.

A start model can produce a document with `to_document(model)`. A local route
model describes only its selected subtree's content contract: ancestor,
sibling, and document-level constraints are not represented, its metadata says
so, and it cannot synthesize a document start. Use `from_node` for local
snapshots. Context-dependent lexical input uses Pydantic's validation
`context` argument as
`{"relaxng": {"namespaces": {...}, "base_uri": ...}}`, or the scalar tag or
snapshot's scoped context; process-global prefixes are never consulted.

`pyxsd.integrations.arrow.records(schema, *, element, path=(), columns=None)`
returns a `RelaxNGRecordProjection`. It compiles rows, columns, selectors, and
the fixed Arrow schema before any document is read, and supports `batches`,
`table`, `write_parquet`, `write_dataset`, and `dataset_metadata`. Unique
scalar columns use the normal Arrow projection mapping, including its
documented fallbacks; field metadata records each fallback. An ambiguous scalar
column
requires an explicit `FieldSource(datatype=(library_uri, type_name))` or
`FieldSource(candidate_id=...)`; `datatype` and `candidate_id` are mutually
exclusive. A datatype selector matching several distinct contracts requires a
candidate ID. Per-row selections come from current accepting evidence and are
jointly checked; a failed selection raises the existing projection error
instead of writing a null or silently changing the Arrow schema. Flat-format
and contextual route limits are representation limits, not whole-document
validity claims.

```python
# This block requires the pydantic and arrow extras.
from io import StringIO

from pyxsd import relaxng
from pyxsd.integrations.arrow import records
from pyxsd.integrations.pydantic import models
from pyxsd.integrations import IntegrationError

SCHEMA = """<grammar xmlns="http://relaxng.org/ns/structure/1.0">
  <start>
    <element name="catalog">
      <zeroOrMore>
        <element name="record">
          <attribute name="id">
            <data type="integer"
                  datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/>
          </attribute>
          <element name="title"><text/></element>
        </element>
      </zeroOrMore>
    </element>
  </start>
</grammar>"""

XML = (
    "<catalog>"
    '<record id="1"><title>Rain</title></record>'
    '<record id="2"><title>Field</title></record>'
    "</catalog>"
)

schema = relaxng.compile(StringIO(SCHEMA))
catalog = schema.parse(StringIO(XML))

registry = models(schema)
Catalog = registry.model_for(element="catalog")
Record = registry.model_for(element="catalog", path=("record",))

root_model = registry.from_document(catalog)
assert root_model.model_dump(by_alias=True) == {
    "record": [
        {"@id": 1, "title": "Rain"},
        {"@id": 2, "title": "Field"},
    ]
}
local_model = registry.from_node(catalog.root.children[0], element="catalog", path=("record",))
assert local_model.model_dump(by_alias=True) == {"@id": 1, "title": "Rain"}
assert Record.model_validate({"@id": 3, "title": "Sun"}).model_dump(by_alias=True) == {
    "@id": 3,
    "title": "Sun",
}
assert Record.model_validate_json('{"@id":3,"title":"Sun"}').model_dump(by_alias=True) == {
    "@id": 3,
    "title": "Sun",
}
assert registry.to_document(root_model).is_valid

try:
    registry.to_document(local_model)
except IntegrationError as exc:
    assert "local scope" in str(exc)
else:
    raise AssertionError("a local route cannot produce a document")

projection = records(schema, element="catalog", path=("record",))
table = projection.table(catalog, selector="record")
assert table.num_rows == 2
assert table.to_pylist() == [
    {"id": "1", "title": "Rain"},
    {"id": "2", "title": "Field"},
]
assert projection.dataset_metadata()["schema_format"] == "relaxng"
```

The Arrow integer column above is a string because Arrow has no arbitrary
precision integer type; its field metadata records the
`unbounded integer range` fallback. That is the existing Arrow projection
policy, not a RELAX NG datatype claim.

## Schema comparison

`pyxsd.relaxng.comparison.compare_relaxng_structure(old, new, *, detail_limit=None)`
compares two valid RELAX NG schemas structurally. The result has `status`,
`statement`, complete `added`/`removed`/`changed` ID lists, bounded
`changed_details`, `totals`, and category breakdowns for `starts`,
`definitions`, `patterns`, `name_classes`, `datatypes`, `dependencies`, and
`projections`. Source locations are evidence, not identity keys: relocating an
identical dependency closure without changing resolved pattern, name, or
datatype semantics yields no structural change. Normalization respects only
language-defined ordering.

This is structural bisimulation, not a semantic-equivalence or
language-inclusion decision. An empty diff means no difference in this
projection; it proves neither language equivalence nor inclusion and makes no
universal compatibility claim. Cross-language structural comparison is
explicitly **not comparable**, not an empty diff. MCP keeps that not-comparable
structural section separate from a corpus section: corpus impact validates
documents under both schemas regardless of language, so an XSD-to-RELAX NG
corpus comparison still works. With no corpus, comparison reports structural
evidence only.

## Command line

The CLI selects RELAX NG explicitly; a filename extension never selects the
language. RELAX NG has no instance schema-location hint, so `-s` is required.
Validation, parsed output, transforms through the shared document pipeline, and
stub emission work. `PrintData()` and `ToDict()` work on RELAX NG documents.
Native `ToData()` (the default `format="pyxsd"`) also works on RELAX NG and
writes the generic lexical grouped export; it does not produce RELAX NG typed
wrappers or tags. Use `RelaxNGDocument.to_data()` for typed or schema-directed
RELAX NG output. `fidelity="strict"` reports representation losses instead of
applying best-effort fallbacks. The schema-directed formats `"pyxsd-json"`,
`"parker"`, `"badgerfish"`, and `"jsonml"` are XSD-only and fail explicitly;
use `RelaxNGDocument.to_data(format=...)` or MCP `pyxsd_export_data` for RELAX
NG. The CLI has no separate JSON-input, model, or record commands.

Save these fixtures to a working directory:

```xml
<grammar xmlns="http://relaxng.org/ns/structure/1.0">
  <start>
    <element name="note"><text/></element>
  </start>
</grammar>
```

```xml
<note>hello</note>
```

```xml
<note><extra/></note>
```

```bash
pyxsd -i note.xml -s note.rng --schema-language relax-ng --strict
pyxsd -i note.xml -s note.rng --schema-language relax-ng -t 'PrintData()' -o printed.xml
pyxsd --emit-stubs -s note.rng --schema-language relax-ng --stubs-compact
pyxsd -i note.xml -s note.rng --schema-language relax-ng -t 'ToData()' -o data.json
pyxsd -i note.xml -s note.rng --schema-language relax-ng -t 'ToData(format="parker")' -o data.json
pyxsd -i broken.xml -s note.rng --schema-language relax-ng --strict
```

The final two commands are expected to exit `1`: the schema-directed `ToData`
case prints the RELAX NG boundary error, and the invalid document prints an
`unexpected-element` finding. The default values of `--mode`, `--namespaces`,
and `--xsd-version` are accepted; only non-default values are rejected with
RELAX NG. `-c/--overlayClassesFile` is always XSD-only and rejected. See
{doc}`cli` for all flags and exit codes.

## MCP

`SchemaRef.format` accepts `"relax-ng"`; filenames never select the language.
With that format, `pyxsd_validate_document`, `pyxsd_validate_documents`,
`pyxsd_inspect_schema`, `pyxsd_find_schema_components`, schema diagnostics,
stubs, `pyxsd_export_data`, `pyxsd_import_data`,
`pyxsd_document_json_schema`, `pyxsd_compare_schemas`, model inspection and
validation, record previews and exports, and ordinary transform previews use
RELAX NG adapters. `pyxsd_capabilities` reports RELAX NG under
`schema_languages`.

MCP validation parses lexical trees only. The capability metadata path
`schema_languages["relax-ng"]["mcp"]["validation"]` reports `typed: false` and
`capture: false`, so MCP does not expose `document.annotations` or typed scalar
wrappers directly. Conversion operations still use the public typed export path
internally. The capability metadata reports
`schema_languages["relax-ng"]["mcp"]["diagnostics"]["completeness"]` as
`"boolean"`; validation report objects expose the corresponding
`diagnostics_complete` and `suppressed_count` fields. Conversion reports all
five formats and both fidelities. Structural comparison is
same-language; cross-language structural sections are not comparable, and
corpus impact is separate.

Built-in and XSD datatype libraries are available over the wire.
Application-supplied Python factories are a Python-only capability: MCP never
accepts executable factory code or guesses a custom identity from a URI, and
unsupported custom-library requests fail explicitly. XSD-only compile options
are rejected before input is read. See {doc}`mcp` for the tool envelope,
artifacts, and session model.

## Resources, limits, and concurrency

`compile` retains its `resource_policy`, which owns every resource read,
including includes and external references. The `datatype_libraries` argument
maps library URIs to application-supplied Python registrations; built-in and
XSD libraries are registered by default. Without a policy, `xml_policy` and
`xml_limits` configure schema parsing; they cannot be combined with a policy.
Instance parses accept their own `xml_policy` and `xml_limits`, unless the
schema owns a trusted resource policy. The same trust and mutual-exclusion
rules as XSD compilation apply. See {doc}`xml-security` for profiles, remote
authorization, and the untrusted-input boundary.

`RelaxNGLimits` bounds pattern nodes, derivative states, memo entries, evidence
states and edges, scalar witnesses, binding summaries, capture entries,
projection routes, and diagnostic retention. `None` leaves a budget unlimited,
which is appropriate only for trusted inputs; applications processing
untrusted schemas or instances should set explicit budgets. A parse may
override instance-side budgets. Work-budget exhaustion raises
`RelaxNGLimitError`; it never reports semantic invalidity. A configured
`max_diagnostics` only bounds retention, and `max_capture_entries` truncates
only the public annotation table.

One `RelaxNGSchema` must not be parsed concurrently or reentrantly. A competing
parse raises `RuntimeError` immediately instead of waiting. Separate schema
objects are independent and may compile or parse concurrently. `iter_parse`
uses the shared sequential batch orchestration and parses each item
validation-only, so batch documents are generic; see {doc}`batch` for source
lifetimes and error policies.

## Release capability matrix

The following matrix is the release contract. Each row names the XSD-facing
capability, the required RELAX NG behavior, and the legitimate difference.

| XSD-facing capability | Required RELAX NG behavior | Legitimate difference |
| --- | --- | --- |
| Compile, parse, revalidate, batch | Equivalent lifecycle, source handling, bounded diagnostic recovery, and visible completeness | Separate compiler and explicit schema association |
| Mutable document and writers | One lexical tree, synchronized descriptors, XPath, transforms, XDM, and serialization | No XSD infoset changes; revision-scoped interpretation evidence |
| Typed Python access | Generated projections and descriptors for stable routes | Generic annotated nodes for structural ambiguity |
| Stubs | Full and compact stubs for every schema, with route filtering | Grammar IDs and `AlternativeValue` replace XSD type claims |
| Dictionary/data/JSON export | All five formats and both fidelity modes | Documented format losses; tagged scalar alternatives |
| Data/JSON import | All five import formats, then native RELAX NG validation | Start-pattern roots instead of global declarations |
| JSON Schema generation | Draft 2020-12 for every supported data contract | Cannot claim full grammar enforcement |
| Introspection and search | Describe, find, IDs, and dependency traversal | Grammar components instead of XSD components |
| Pydantic | Registry, standalone validation, dumps and JSON Schema, route selection, and document conversion | Alternative scalars and explicitly scoped local-route validation |
| Arrow, Parquet, and datasets | Equivalent projections, batches, tables, and writes | Explicit datatype selection for ambiguous scalar columns |
| Schema comparison | RELAX NG structural differences and same- or cross-language corpus impact | Cross-language structural comparison is not comparable |
| MCP and CLI | Every schema-neutral operation and capability reporting | XSD schema-aware typing is inapplicable; the core RELAX NG profile is explicit |

## Deliberate exclusions

These are accepted profile boundaries, not unfinished work and not excuses for
gaps in the matrix above:

- **Compact syntax.** Only RELAX NG XML syntax is implemented. A filename
  extension never selects compact syntax.
- **DTD Compatibility and direct DTD coverage.** RELAX NG DTD Compatibility is
  outside this profile, and there is no synthesized defaulting or ID/IDREF
  cross-reference layer. Existing DTD-related preprocessing is preserved but
  is not comprehensive DTD coverage. There are no synthesized defaults in the
  RELAX NG profile.
- **NVDL.** Compound-document dispatch is not part of this implementation.
- **Schema-aware XSLT and XPath typing.** Ordinary XPath reads the bound tree,
  but schema-aware XPath and schema-aware XSLT features remain XSD-only and do
  not accept RELAX NG annotations.
- **XSD translation.** There is no RELAX NG-to-XSD translation, no global
  declarations or fabricated XSD type ownership, and no `xsi:type`, `xsi:nil`,
  substitution-group, identity-constraint, or XSD default semantics. XSD-only
  actual-type and nil shortcuts are rejected as conversion markers.

Core RELAX NG has neither XSD defaulting nor its identity-constraint semantics.
Workflows that need direct DTD processing, DTD Compatibility, compact syntax,
NVDL, or XSD schema-aware typing should use an appropriate external processor;
pyxsd does not plan compatibility layers for these exclusions.

See {doc}`supported` for the XSD coverage page and {doc}`data-conversion` for
the shared five-format structures.
