# Supported features

pyxsd is an XSD 1.1 processor with an optional XSD 1.0 mode
(`Schema.compile(xsd, xsd_version="1.0")` / `--xsd-version 1.0`; see
{doc}`binding` and the version section below). This page summarizes what is
validated, and what is deliberately not.

Correctness is measured against the W3C XML Schema Test Suite (xsdtests):
pyxsd passes **99.78%** of the XSD 1.1 profile and **99.61%** of the XSD 1.0
profile. A separate manifest-driven regression corpus backs the integration
paths (`uv run python tests/report_conformance.py` for the live report, and
`uv run python tests/report_xsts.py` for the test suite).

Parser profiles, structural limits, remote schema rules, and the untrusted-input
boundary are covered in {doc}`xml-security`. The defaults remain stdlib parsing
and local-only loading.

## Built-in type lattice

All 45 XSD 1.0 built-in datatypes are implemented with lexical
validation in `__new__` (`pyxsd.xsd_data_types`): the full string family
(string, normalizedString, token, Name, NCName, NMTOKEN, language, ID,
IDREF, ENTITY, anyURI, QName, NOTATION), all integer variants with correct
ranges, decimal, float/double (including `INF`/`-INF`/`NaN`), the date/time family
(date, dateTime, time, duration, gYear, gYearMonth, gMonth, gMonthDay,
gDay), base64Binary, hexBinary, anySimpleType/anyType, and the list types
NMTOKENS/IDREFS/ENTITIES.

## Structural schema elements

- `xs:sequence`, `xs:choice`, `xs:all` content models with order and
  occurrence checking
- `xs:group` definitions and references (nested, with cycle detection);
  a reference's `minOccurs`/`maxOccurs` stays with that reference and
  never affects other uses of the shared group
- `xs:attributeGroup` definitions and references
- `xs:any` / `xs:anyAttribute` wildcards with namespace lists
  (`##any`, `##other`, `##local`, `##targetNamespace`, explicit URIs) and
  `processContents` (`strict` / `lax` / `skip`), enforced in namespaced
  mode; wildcard particles take part in sequence order and occurrence
  matching, and each matched child is bound through the particle that
  admitted it — position decides, not the child's name (including
  wildcards contributed by a group)
- repeated declarations and inherited extension declarations validate
  each occurrence through the particle that consumed it, so `fixed` and
  type constraints stay with the declaration they were written on
- element and attribute declarations sharing a name: both are kept — the
  attribute keeps the natural accessor and the element is exposed under
  a collision-safe alias (`<name>_element`, with a numeric suffix when
  that is taken)
- XML Namespaces in namespaced mode: `targetNamespace`,
  `elementFormDefault` / `attributeFormDefault` (including the source
  schema's defaults for imported components and explicit `form`
  overrides), prefixed type/`ref` QNames, cross-namespace `xs:import`,
  and QName value identity
- `xs:list` with named or inline item types and per-item facet validation;
  item types may be atomic or unions without list members
- `xs:union` with declared or inline anonymous members, including nested
  unions and list members
- `xs:notation` declarations and `xs:NOTATION` restrictions with
  enumeration validation
- element references (`ref`) and substitution groups
- complex content `xs:extension` / `xs:restriction` derivation with `final`
  enforcement

## Instance semantics

- `minOccurs`/`maxOccurs` enforcement (including `unbounded`)
- `xsi:nil` on nillable elements: a nilled element must be completely
  empty (whitespace-only text and child elements are reported), may not
  carry a `fixed` value, still has its attributes validated, and is
  preserved as nil in output
- `default` and `fixed` for attributes and simple-content elements
- `xsi:type` dynamic dispatch on child elements and the document root
- `abstract` elements and types; `block`/`prohibited` restrictions
- root dispatch: both complex-typed and primitive-typed document roots
- expanded-name matching of elements and attributes against form defaults
  (namespaced mode)

## Composition and identity

- `xs:include` (chameleon schemas supported), `xs:import` (cross-namespace
  components in namespaced mode, including imports without a
  `schemaLocation` when the namespace is supplied), and `xs:redefine`,
  including chained redefinitions
- `xs:key`, `xs:unique`, `xs:keyref` with the XSD identity-constraint XPath
  subset: namespace-qualified child and attribute steps, `.//` descendants,
  wildcards, `.`, union paths, and `xpathDefaultNamespace`

## Validation reporting

Non-fatal, code-tagged issue reporting via `Schema.report` and
`Document.report` (see {doc}`validation`), plus `--strict` CI-friendly
exit codes, `require_valid()`, and the `ValidationReport` API for
library users. `Schema.compile(..., mode=...)` / `--mode`
selects how invalid or unrecognized content is **bound** (strict vs. lax)
without changing what is **reported** — see {doc}`binding`. The same policy
carries a `namespaces` field: `Schema.compile(...,
mode=ParseModes.NAMESPACED)` or `--namespaces strict` turns on
namespace-aware validation (the default legacy behavior is unchanged).

## Extending and typing generated classes

- **Mixin extensions** attach user classes to the schema's generated
  types at compile time (`Schema.compile(..., extensions=...)`), selected
  by type name, namespace, kind, instance path, or predicate, with an
  optional `__pyxsd_validate__` hook whose findings join the instance
  report (see {doc}`extensions`). There is no post-compile class patching
  and no builtin-type extension.
- **Type-stub emission** (`Schema.stub()` / `pyxsd --emit-stubs`) renders
  a `.pyi` typing surface, or a compact agent-oriented view, for the
  classes a compiled schema creates at runtime; a construct that cannot be
  typed truthfully degrades to `Any` (see {doc}`api`).
- **Native value conversions and facet introspection** on the XSD value
  types — temporal (`to_date` / `to_datetime` / `to_time`, `to_gregorian`),
  duration, binary, QName, and URI conversions, plus
  `XsdDataType.patterns()` / `enumerations()` — see {doc}`data-model`.

## XSD versions (1.0 and 1.1)

The processor defaults to XSD 1.1 and accepts an explicit version:
`Schema.compile(xsd, xsd_version="1.0")`, or `--xsd-version 1.0` on the
CLI. Version selection applies the conditional-inclusion rules
(`vc:minVersion` / `vc:maxVersion` / `vc:typeAvailable` and friends) at
the declared version, and in 1.0 mode it reports XSD 1.1-only vocabulary
(`assert`, `assertion`, `alternative`, `openContent`, `override`,
`notNamespace`, `notQName`, `defaultAttributes`, `inheritable`,
`xpathDefaultNamespace`, `dateTimeStamp`, `dayTimeDuration`,
`yearMonthDuration`, `anyAtomicType`) as the schema error
`xsd11-construct`. Three 1.0/1.1 semantic differences are also enforced:
the 1.0 `xs:all` 0..1 child-occurrence cap (`all-rule`), the 1.0
requirement that a union declare at least one member type
(`declaration-child`), and the 1.1 prohibition on `use="prohibited"`
together with `fixed`.

Against the W3C XML Schema Test Suite, pyxsd passes 99.78% of the XSD
1.1 profile and 99.61% of the 1.0 profile (see above). A few XSD 1.0
differences are deliberately not switched on, because they live in shared
machinery (the built-in datatype constructors) or need case-specific
identity-constraint and name-resolution work rather than a clean version
switch:

- the 1.0-only lexical forms around year `0000` and the `+INF`/`-INF`
  spelling (the shared datatype constructors are version-agnostic);
- keyref cardinality under 1.0;
- several 1.0 name-resolution edge cases in cross-namespace references.

These are recorded as residual XSD 1.0 test-suite deltas rather than
missing features; they are general correctness edges that the 1.0 profile
happens to surface.

## Known gaps

```{list-table}
:header-rows: 1

* - Feature
  - Construct
  - Status
  - Note
* - Namespace-aware schemas
  - `targetNamespace`, `elementFormDefault=qualified`
  - partial
  - Opt-in in namespaced mode (`ParseModes.NAMESPACED` / `--namespaces strict`); the default legacy mode keeps local-name matching.
* - Wildcard namespace filtering
  - `processContents`, namespace lists
  - partial
  - Enforced in namespaced mode (including `wildcard-no-declaration` under `strict`); legacy mode passes undeclared content through permissively.
* - General XPath in identity constraints
  - Expressions outside the XSD selector/field subset
  - unsupported
  - The XSD subset is supported. Predicates, absolute paths, additional axes, and function calls are reported and skipped.
* - Mixed content
  - `mixed="true"`
  - partial
  - Character data is preserved verbatim in the bound tree — leading text in `_value_`, text after each child in that child's `_tail_` — and mixed elements serialize inline, so ordered text and significant whitespace round-trip. Declared children and occurrence limits are still validated; mixed content is not permission for arbitrary elements. Structured exports are narrower: dict export concatenates all recorded character data under `$` because it cannot represent text/element interleaving, and the Arrow/Pydantic integration projections reject mixed content.
* - Remote schema hints
  - URL `schemaLocation`
  - partial
  - The default loader resolves local filesystem paths only and never fetches a URL. `RemoteResourcePolicy` adds opt-in HTTPS-only schema loading with explicit root/origin rules, `allow_remote_hints` for an instance-selected schema, `allow_local_remote` for a local schema's remote dependency, and finite budgets; there is no persistent cache.
* - Unique particle attribution
  - UPA checking with XSD 1.1 wildcard exclusions
  - partial
  - Effective content models are checked, including inherited models and substitution groups. Wildcards carrying `notQName` or `notNamespace` exclusions are skipped.
* - DTD entities
  - `ENTITY` / `ENTITIES` values
  - partial
  - Validation considers the internal DTD subset only, and only as far as expat's built-in entity expansion and default-attribute handling go. No built-in profile retrieves an external subset or external entity. The stdlib default keeps that behavior for speed and compatibility and neither fetches nor reports a DOCTYPE declaration; `XmlParserPolicy.protected()` forbids entity declarations and external DOCTYPE identifiers, and `.strict()` forbids any DOCTYPE. Unparsed-entity (`NDATA`) declarations are not tracked, so an `ENTITY` value referring to one can be accepted when it should be rejected.
* - XML 1.1 documents
  - `<?xml version="1.1"?>`
  - unsupported
  - The stdlib expat parser behind `ElementTree` stops at XML 1.0 syntax (XML 1.1 name characters and end-of-line rules are not handled).
* - Concurrent parses per schema
  - parsing in parallel from one `Schema`
  - unsupported
  - A compiled `Schema` keeps per-parse parent/type indexes on its shared host. Parse one document at a time per schema instance; compile a separate `Schema` (or serialize calls) to parallelize.
```

This table is maintained as machine-readable data in
`tests/conformance/unsupported.toml`, which also feeds the conformance
report.

```{note}
**Free-threaded builds.** pyxsd is pure Python and runs on free-threaded
Python (the `3.15t` / `3.14t` builds) without changes. The
concurrent-parses-per-schema rule above is a hard requirement there:
the GIL hid many violations on regular builds, while a free-threaded build
runs them for real. Concurrent compiles and concurrent parses on *separate*
`Schema` objects remain supported. Compiled extras (`pydantic`, `pyarrow`)
may re-enable the GIL when imported if their wheels do not yet support
free-threading; that is a performance note, not a failure.
```
