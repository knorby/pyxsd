# Data model

Every node of a pyxsd object tree — whether generated from a schema or
synthesized by a transform — exposes the same four metadata attributes. The
`_*_` naming convention prevents collisions with element or attribute names
in your schemas (a schema can legally declare an element called `name`, so
`name` on an instance always refers to *your* data, never to metadata).

The shape is codified by the `pyxsd.XMLNode` runtime-checkable
`typing.Protocol`.

## Node structure

| Attribute | Type | Meaning |
| --------- | ---- | ------- |
| `_name_` | `str` | The element name as it appears in the document. |
| `_attribs_` | `dict[str, str]` | Attributes keyed by the name as it should appear in the document. Values are stored lexically; `str()` must produce the correct output. |
| `_children_` | `list` | Child instances of the same node shape, **in document order**. Empty list if the element has no children. |
| `_value_` | `list[str] \| None` | Non-element content as a list of strings (usually one entry), or `None` for elements with no text content. |

Notes:

- `None` as `_value_` is meaningful: it represents an element nilled with
  `xsi:nil="true"`, and the writer will emit the nil attribute.
- Attribute *defaults* applied by validation land in the instance `__dict__`
  (typed values), not in `_attribs_`, so round-trip output stays faithful.
- Repeated elements (`maxOccurs` > 1) store every occurrence in
  `_children_` in document order.
- A bound child may carry an optional, parse-produced `_tail_`: the
  character data that followed the child in the source, whitespace
  included. It is recorded whenever the source had such text; the writer
  and dict export use it only under a parent whose content admits
  character data (mixed content, `xs:anyType`, wildcard pass-through),
  while the XPath projection mirrors every recorded tail. It is **not**
  part of the `XMLNode` protocol: transform-made trees and hand-built
  nodes need no changes — a missing `_tail_` simply means no recorded
  following text.

## Writers

`XmlTreeWriter(root, output)` serializes any tree of nodes shaped as above.
Transform developers commonly use `Transform.makeElemObj(name)` to mint a
fresh node with this exact structure, and `makeCommentElem(text)` for
comments.

## Dict and JSON export

`Document.to_dict()` exports the bound tree as plain Python data, and
`Document.to_json()` encodes that with the standard `json` module. The
shipped `ToDict` transform exposes the same codec to `Document.transform`
and the CLI (`--transform ToDict()`), which renders a dict result as
JSON.

| Input | Dict key/value |
| ----- | -------------- |
| Child element | Key = the child name in Clark notation when namespace-qualified, otherwise the local name. |
| Repeated child | A list of values (a single occurrence is the value itself). |
| `always_list=True` | Every child value is a list, even a single occurrence. |
| Attribute | Key = `"@"` + the document attribute name. |
| Element text | The scalar value for a text-only element; the `"$"` key when the element also has attributes or children. |
| Nilled element | `None`. |
| `typed=False` | Lexical text instead of typed Python values. |

```python
doc.to_dict()  # {"@a": 1, "item": [1, 2], "note": "hi"}
doc.to_dict(typed=False)
doc.to_json(indent=2)
```

The export reflects post-parse descriptor assignment (the write-through
behaviour above), because it reads the same lexical containers the writer
serializes. For an element whose content admits character data (mixed
content, `xs:anyType`, wildcard pass-through), `$` holds all of the
element's recorded character data in document order: the verbatim leading
text followed by every child's `_tail_`, concatenated. The dict shape
cannot represent text/element interleaving, so `to_string()` and `xpath()`
are the faithful mixed-content views. The remaining known loss matches the
node model: a wildcard attribute has only its lexical form.

## XPath projection

`Document.xpath()`, `find()`, and `findall()` evaluate over a projection
of the bound model: an element's `text` mirrors its `_value_` and a
child's `tail` mirrors its `_tail_`, so mixed character data is queryable
in document order. The projection is **document-faithful, not
source-XPath-exact**: for element-only content, recorded layout whitespace
(the element tails between block children) is part of the projection's
text, so a text-valued query such as `string(/a)` includes it. Parsing
does not preserve comment nodes, and an element with `_value_ is None`
projects `text=None`.

The Arrow/Pydantic dataset integrations are a different, value-oriented
boundary: their row text projection (`integrations/_values.py`) reads
`_value_` only, so it does not include child `_tail_` values for mixed
content (mixed content is outside the integrations' supported subset).
That is a documented boundary, not a claim of lossless mixed text — use
`to_string()` or `xpath()` when full mixed character data is needed.

## Generated classes

Class-level access to descriptors is intentional: `item_cls.name` returns
the *descriptor*, not the metadata, and the helpful `__getattr__` on
generated classes raises an `AttributeError` listing the declared elements
and attributes when you misspell something. Instance-level access goes
through the descriptors, which validate types on assignment.

Assignment is not validation-only: assigning a value to an element or
attribute descriptor also writes the value's XSD lexical form through to
the container the writer serializes, so a later `to_string()`, `write()`,
or `revalidate()` reflects the assignment. Repeated element descriptors
are the exception — a repeated element has no single unambiguous target
node, so assignment updates the instance dictionary only and the write-out
is unchanged. Assigning a *bound node* directly (as internal binding does)
is not a user-facing operation and does not go through this path.

Elements and attributes now share one assignment policy. A plain Python
value (a string, int, float, bool, or `decimal.Decimal`) assigned to a
simple-typed element is coerced through the declared datatype, exactly as
attribute assignment already coerced plain values. A value that fails the
declared type's validation is recorded in the report during a parse and
logged outside one, and it is stored as given without touching the
serialized tree — assignment never raises for a bad *lexical* value.
Assigning a datatype instance of the wrong type, or an arbitrary object,
still raises `TypeError`.

### Name collisions: elements and attributes with the same name

A complex type can legally declare an element and an attribute with the
same name (`<xs:element name="code"/>` and `<xs:attribute name="code"/>`
in one type). Both declarations stay fully active — each is matched,
validated, and bound — but they cannot share one Python attribute, so
the accessors are disambiguated:

- The **attribute keeps the natural name** (`item.code`).
- The **element's accessor gains an `_element` suffix**
  (`item.code_element`). If a real declaration already occupies that
  name too, a numeric suffix is used (`code_element_2`, `code_element_3`,
  …).

Only the Python accessor name changes. Child order in `_children_`,
validation, identity constraints, and round-trip output are identical to
a non-colliding schema.

## Type stubs

A compiled schema's classes exist only at runtime, so `Schema.stub()`
(`pyxsd.stubs.emit_stub`) renders their typing surface as text. The full
form is importable-quality `.pyi` source with three parts:

- a **preamble**: the provenance line (pyxsd version and schema source),
  the consumption idiom (`if TYPE_CHECKING: from schema_stubs import
  ...`), and the semantics policy — annotations model valid parsed
  documents, absent optional members (including absent repeated members)
  read as `None`, and repeated-element assignment appends a single item;
- one **class block** per generated class. Inheritance mirrors the XSD
  derivation; element and attribute members carry the union annotations
  above, with everything the annotation cannot say (a `default`'s lexical
  fallback, `nillable`, aliased `xml:` names, facets, degradations) in
  trailing comments; simple types render as their datatype subclass with
  facet comments; a construct that cannot be typed truthfully degrades to
  `Any` with a comment;
- the **root aliases**: the global elements as module-level entries
  (`orders: type[Orders]`), the valid document roots.

The compact form (`compact=True`) is a two-line header plus one line per
class and member: the header gives the provenance and an alias legend
mapping short spellings back to the pyxsd classes they denote
(`str=String`, `int=Int`, `date=Date`, ...), facet-only simple types are
folded onto the members that use them, and a trailing `# roots:` comment
lists the global elements. It is a readable description, not Python
source. See {doc}`api` for both modes and the truthfulness contract, and
{doc}`cli` for `--emit-stubs` / `--stubs-compact`.

## Value conversions

The temporal and calendar types, the durations, the binary types,
`QName`, and `AnyURI` are validated `str` subclasses, and each converts
to the Python value it resembles. The conversions follow one principle:
a method that returns a standard-library value converts exactly what
that type can represent and raises `ValueError` for what it cannot,
while a second method per family returns the `elementpath.datatypes`
counterpart, which holds the full XSD value space. Where a conversion
would otherwise lose information silently, it accepts `strict=True` to
raise instead.

### Conversion reference

| XSD type(s) | Method | Target type | Lossy by default | With `strict=True` |
| ----------- | ------ | ----------- | ---------------- | ------------------ |
| `xs:date` | `Date.to_date()` | `datetime.date` | Timezone offset dropped | Raises instead of dropping it |
| `xs:dateTime` | `DateTime.to_datetime()` | `datetime.datetime` | Sub-microsecond fraction truncated | Raises on a non-zero fraction |
| `xs:time` | `Time.to_time()` | `datetime.time` | Sub-microsecond fraction truncated | Raises on a non-zero fraction |
| `xs:date` | `Date.to_gregorian()` | `elementpath.datatypes.Date` | Nothing (total) | — |
| `xs:dateTime` | `DateTime.to_gregorian()` | `elementpath.datatypes.DateTime` | Sub-microsecond fraction truncated | Raises on a non-zero fraction |
| `xs:time` | `Time.to_gregorian()` | `elementpath.datatypes.Time` | Sub-microsecond fraction truncated | Raises on a non-zero fraction |
| `xs:gYear` | `GYear.to_gregorian()` | `elementpath.datatypes.GregorianYear` | Nothing (total) | — |
| `xs:gYearMonth` | `GYearMonth.to_gregorian()` | `elementpath.datatypes.GregorianYearMonth` | Nothing (total) | — |
| `xs:gMonth` | `GMonth.to_gregorian()` | `elementpath.datatypes.GregorianMonth` | Nothing (total) | — |
| `xs:gMonthDay` | `GMonthDay.to_gregorian()` | `elementpath.datatypes.GregorianMonthDay` | Nothing (total) | — |
| `xs:gDay` | `GDay.to_gregorian()` | `elementpath.datatypes.GregorianDay` | Nothing (total) | — |
| `xs:duration` | `Duration.to_timedelta()` | `datetime.timedelta` | Sub-microsecond fraction truncated; year/month fragments always raise | Raises on a non-zero fraction |
| `xs:dayTimeDuration` | `DayTimeDuration.to_timedelta()` | `datetime.timedelta` | Sub-microsecond fraction truncated | Raises on a non-zero fraction |
| `xs:yearMonthDuration` | `YearMonthDuration.to_months()` | `int` | Nothing (exact) | — |
| `xs:duration` | `Duration.to_xsd_value()` | `elementpath.datatypes.Duration` | Sub-microsecond fraction rounded | Raises on a non-zero fraction |
| `xs:yearMonthDuration` | `YearMonthDuration.to_xsd_value()` | `elementpath.datatypes.YearMonthDuration` | Nothing (full fidelity) | — |
| `xs:dayTimeDuration` | `DayTimeDuration.to_xsd_value()` | `elementpath.datatypes.DayTimeDuration` | Sub-microsecond fraction rounded | Raises on a non-zero fraction |
| `xs:hexBinary` | `HexBinary.to_bytes()` | `bytes` | Nothing (exact) | — |
| `xs:base64Binary` | `Base64Binary.to_bytes()` | `bytes` | Nothing (exact) | — |
| `xs:QName` | `QName.to_expanded_name()` | `str` (Clark notation) | Lexical form when unresolved; bare local name for an unbound prefix | Raises instead (unresolved or unbound prefix) |
| `xs:anyURI` | `AnyURI.to_parsed_uri()` | `urllib.parse.ParseResult` | Nothing; best-effort over the literal text | — |

How to read the table:

- A `—` in the last column means the method takes no `strict`
  parameter: the conversion is total, so there is no silent loss for
  `strict` to reject (`to_parsed_uri` is best-effort by nature — see
  its subsection).
- The `xs:dateTime` conversions are inherited by `xs:dateTimeStamp`
  (`DateTimeStamp` derives from `DateTime`).
- Values the target type cannot represent at all — years outside
  1..9999 for the stdlib conversions, `Time("24:00:00")`, duration
  year/month fragments, and a value outside the target's numeric range
  (an oversized duration) — raise `ValueError` naming the lexical
  value regardless of `strict`; `strict` only turns silent loss into
  an error.
- Facet introspection (`XsdDataType.patterns()`,
  `XsdDataType.enumerations()`, and `pyxsd.facets.compile_pattern()`)
  is not a value conversion; it has its own subsection below.

### Dates, times, and dateTimes

`Date`, `DateTime`, and `Time` are validated `str` subclasses; each also
converts to the standard library type it resembles:

```python
DateTime("2006-08-30T14:30:00+05:30").to_datetime()
# datetime.datetime(2006, 8, 30, 14, 30, tzinfo=datetime.timezone(datetime.timedelta(seconds=19800)))
DateTime("2006-08-30T24:00:00").to_datetime()  # datetime.datetime(2006, 8, 31, 0, 0)
Date("2006-08-30").to_date()  # datetime.date(2006, 8, 30)
Time("14:30:00.5").to_time()  # datetime.time(14, 30, 0, 500000)
```

The conversion contract follows the lexical value's XSD semantics:

- an explicit timezone offset becomes a `datetime.timezone` (the value
  is aware); an absent offset yields a naive value; `Date.to_date()`
  discards the offset, because `datetime.date` carries no timezone;
- fractional seconds are preserved to microsecond precision, and a
  finer fraction is truncated;
- `DateTime`'s end-of-day spelling `24:00:00` rolls over to midnight of
  the following day (on `9999-12-31` the rollover leaves the `datetime`
  range and raises), while `Time` with `24:00:00` raises `ValueError` —
  it has no time-of-day value;
- XSD's year `0000`, negative (BCE) years, and extended years above
  9999 have no `datetime` representation and raise `ValueError` naming
  the lexical value.

Use the stdlib conversions for any value inside the `datetime` year
range, and pass `strict=True` when silent loss is unacceptable:
`to_date` then raises instead of dropping the timezone offset, and
`to_datetime` / `to_time` raise instead of truncating a non-zero
sub-microsecond fraction. `strict` never rescues an unrepresentable
value — the raise rules above apply either way. For years outside the
stdlib range, use `to_gregorian()` instead.

### Calendar types and out-of-range years: `to_gregorian()`

`to_gregorian()` converts to the value's `elementpath.datatypes`
counterpart, preserving a timezone offset in the lexical form (the
elementpath types model XSD's optional timezone, unlike
`datetime.date`). It is the conversion for the two groups the standard
library cannot hold:

- `xs:date` and `xs:dateTime` values with years outside 1..9999 —
  XSD's year `0000`, BCE years, extended years above 9999. `xs:time`
  carries no year component, so its `to_gregorian()` simply preserves
  the time-of-day value and any timezone offset;
- the `g*` calendar types (`gYear`, `gYearMonth`, `gMonth`, `gMonthDay`,
  `gDay`), which are **recurring periods** — a year, the 30th of some
  month — not points on a calendar. They have no stdlib representation
  at all, so `to_gregorian()` is their only conversion.

Mind the year numbering: XSD 1.1 writes 1 BCE as lexical year `0000`,
while elementpath numbers years with no year zero, so lexical `0000`
maps to elementpath year `-1`.

elementpath holds at most microsecond-resolution fractions, so
`DateTime.to_gregorian()` and `Time.to_gregorian()` truncate a finer
fraction by default and raise `ValueError` naming the lexical value
under `strict=True`. `Date.to_gregorian()` and the `g*` conversions
have no fractional component and take no `strict` parameter. A value
outside the target's range raises `ValueError` naming the lexical
value.

The elementpath types are **not** `datetime` subclasses, and
`to_gregorian()` is never a fallback inside `to_date` / `to_datetime` /
`to_time`: those raise `ValueError` on out-of-range values rather than
silently returning a different type. Call `to_gregorian()` explicitly
when the wide-range value is what you want.

### Durations

A duration splits into a calendar part (years, months) and a day/time
part (days, hours, minutes, seconds), and the conversion methods split
along the same line:

- `Duration.to_timedelta()` (inherited by `DayTimeDuration`) converts
  the day/time part to a `datetime.timedelta`. A value carrying a year
  or month fragment **always raises `ValueError`**: a calendar
  duration's length depends on the date it is applied to, and it is
  never approximated — there is no 30-days-per-month assumption. A
  fractional second finer than a microsecond is truncated, a
  non-zero one raises with `strict=True`, and a leading `-` negates
  the result. A value outside `datetime.timedelta`'s range raises
  `ValueError` naming the lexical value. `DayTimeDuration` cannot carry
  year/month fragments at all, so its `to_timedelta` is total apart
  from sub-microsecond fractions and the timedelta range.
  `YearMonthDuration` inherits the method but its value
  space is year/month-only, so the call always raises for it — use
  `to_months()`.
- `YearMonthDuration.to_months()` returns the signed exact number of
  months (`P1Y2M` → `14`, `-P34Y233M` → `-641`). The conversion is
  exact, so it takes no `strict`; and the standard library has no
  calendar arithmetic, so a `timedelta` cannot express this value at
  all.
- `to_xsd_value()` returns the value as an elementpath `Duration`, the
  representation that keeps both the months and the seconds properties, so
  the calendar part `to_timedelta` refuses (year/month values) survives.
  elementpath holds at most microsecond-resolution fractions, so a
  finer second fraction is rounded by default; pass `strict=True` to
  raise `ValueError` naming the lexical value instead. A value outside
  the target's range raises `ValueError` naming the lexical value.
  `YearMonthDuration.to_xsd_value()` and
  `DayTimeDuration.to_xsd_value()` return the correspondingly precise
  elementpath subclasses. These elementpath durations are
  **not `datetime.timedelta` subclasses** — they are a separate value
  type with XSD duration ordering.

Use `to_timedelta` for arithmetic against `datetime` values (day/time
durations only), `to_months` for interval math on year/month
durations, and `to_xsd_value` when the value needs the calendar part or
XSD duration ordering (comparison, further elementpath processing) —
noting that it rounds a sub-microsecond fraction unless `strict=True`.

### Binary data

`HexBinary.to_bytes()` and `Base64Binary.to_bytes()` return the
decoded `bytes`. Both conversions are exact — the constructor has
already validated the lexical form (including base64's pad bits), so
nothing can be lost and there is no `strict` mode.

### QNames

`QName.to_expanded_name()` returns the value in Clark notation
(`{uri}local`) for use as a namespace-aware key. Resolution happens at
construction, not at conversion: the parser sets the ambient
`pyxsd.xsd_data_types.qname_context` (the in-scope prefix → namespace
URI bindings) around instance binding, and the value records the URI
its prefix was bound to then — the conversion takes no context
argument, so a value's expansion is fixed by how it was built:

- a resolved value whose prefix is bound to a namespace URI becomes
  `{uri}local`; a resolved unprefixed name with no default namespace
  becomes the bare local name;
- the default is best-effort: a value constructed without a
  `qname_context` returns its lexical form, and a prefixed name whose
  prefix is missing from the context returns the bare local name, its
  prefix dropped. **This includes every value bound in the default
  (legacy) namespace mode** — only strict namespace mode resolves
  `xs:QName` values, so `--namespaces strict` /
  `ParseModes.NAMESPACED` is what makes expanded names available from
  parsed documents;
- with `strict=True`, either inaccurate case raises `ValueError`
  naming the lexical value instead: an unresolved value (one built
  without a `qname_context`), or a prefixed value whose prefix the
  active context did not bind. A resolved unprefixed name with no
  default namespace never raises — its bare local name is complete.

Hand-built values resolve too, by constructing inside the context
manager (`with qname_context({"xsd": ...}): QName("xsd:schema")`).

Use `to_expanded_name` where a consumer keys on Clark notation
(ElementTree's `find` and most XML APIs do); the value is itself a
`str`, so the lexical form is available directly where that is what
you want.

### anyURI

`AnyURI.to_parsed_uri()` returns the lexical text run through
`urllib.parse.urlparse` as a `urllib.parse.ParseResult`. The parse is
best-effort by nature: XSD's `anyURI` lexical space admits characters
that are meant to be URI-escaped (spaces, for example), so the result
reflects the literal text — the path of `http://example.com/a b` is
`/a b`. Percent-encode the components yourself if a consumer needs a
well-formed URI; the lexical value stays authoritative either way.

### Facet introspection

A type's effective facets are introspectable as classmethods on any
generated or simple type:

- `XsdDataType.patterns()` returns
  `tuple[tuple[re.Pattern[str], ...], ...]`: one entry per restriction
  step, every step of which must hold (AND), each inner tuple holding
  that step's sibling `xs:pattern` facets, any of which may match
  (OR) — exactly the merge rule validation applies. The compiled
  `re.Pattern` objects are the same ones validation matches,
  translated by elementpath using the schema's selected XSD processor
  version (`xsd_version="1.0"` or `"1.1"`); the translation embeds its own
  anchors, so a pattern matches the whole value however you call it.
- `XsdDataType.enumerations()` returns `tuple[Any, ...]`: the
  effective enumeration values after intersection across restriction
  steps, as instances of the restriction's base datatype.

Both return `()` when the type declares no such facets. Use them to
mirror a schema's constraints in your own checks, or to show a caller
the exact patterns a rejected value had to match.
`pyxsd.facets.compile_pattern(text, *, xsd_version="1.1")` compiles one
standalone XSD pattern through the same translation, raising `ValueError`
when translation or compilation rejects it. Pass `xsd_version="1.0"` to
select the 1.0 grammar; schema compilation passes its selected version
through `build_constraints` to this helper. Version selection does not
establish complete regex conformance; see {doc}`xsts` for remaining
disagreements.

### Static typing

Full-mode stubs annotate members with these classes (`DateTime`, not
`datetime.datetime`; `Duration`, not `datetime.timedelta`), so the
conversion methods are visible to mypy and IDEs wherever a member is
typed with one of them.
