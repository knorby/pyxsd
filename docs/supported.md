# Supported features

pyxsd 1.0 implements a substantial, honest subset of XSD 1.0. This page
summarizes what is validated, with pointers to the regression corpus that
backing every claim (93 independently authored manifest-driven cases
inspired by the W3C XMLSchema1TestSuite and NIST datatype feature areas —
run
`uv run python tests/report_conformance.py` for the live pass-rate report).

## Built-in type lattice

All 45 XSD 1.0 built-in simple types are implemented with lexical
validation in `__new__` (`pyxsd.xsd_data_types`): the full string family
(string, normalizedString, token, Name, NCName, NMTOKEN, language, ID,
IDREF, ENTITY, anyURI, QName), all integer variants with correct ranges,
decimal, float/double (including `INF`/`-INF`/`NaN`), the date/time family
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
- `xs:union` (declared members and inline anonymous members)
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
  `schemaLocation` when the namespace is supplied), `xs:redefine` (single
  level)
- `xs:key`, `xs:unique`, `xs:keyref` with an XPath subset (child steps,
  `.//` descendants, `*`, `.`, `@attr`, multi-step paths)

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

Against the W3C XML Schema Test Suite, pyxsd passes **99.78%** of the XSD
1.1 profile and **99.61%** of the XSD 1.0 profile. A few XSD 1.0
differences are deliberately not switched on, because they live in shared
machinery (the built-in datatype constructors) or need case-specific
identity-constraint and name-resolution work rather than a clean version
switch:

- the 1.0-only lexical forms around year `0000` and the `+INF`/`-INF`
  spelling (the shared datatype constructors are version-agnostic);
- keyref cardinality (`idconstrdefs00301m`);
- the `st_name00401m`, `st_targetNS*`, and `targetns00101m` name-resolution
  cases and `addB187`.

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
  - Opt-in in namespaced mode (`ParseModes.NAMESPACED` / `--namespaces strict`); the default legacy mode keeps local-name matching. Namespace-qualified identity-constraint selectors and reporting an unbound prefix in an instance QName *value* are not implemented.
* - Wildcard namespace filtering
  - `processContents`, namespace lists
  - partial
  - Enforced in namespaced mode (including `wildcard-no-declaration` under `strict`); legacy mode passes undeclared content through permissively.
* - Full identity XPath
  - `xs:selector`/`xs:field` expressions
  - partial
  - Predicates, absolute paths, positional steps, and other axes are skipped with a warning.
* - List type derivation
  - `xs:list` user types
  - unsupported
  - Built-in list types work; user-defined list types are not generated.
* - Unions of list members
  - `xs:union` with list members
  - partial
  - Unions of atomic types work (including inline anonymous members).
* - Mixed content
  - `mixed="true"`
  - ignored
  - Interleaved text is not preserved or validated.
* - Remote schema hints
  - URL `schemaLocation`
  - partial
  - Local file system resolution only.
* - Notation
  - `xs:NOTATION`
  - unsupported
  - Not implemented.
* - Redefine chains
  - redefining an already-redefined component
  - partial
  - A single redefine level is supported.
* - Unique particle attribution
  - UPA checking
  - ignored
  - Content models are matched greedily in document order.
```

This table is maintained as machine-readable data in
`tests/conformance/unsupported.toml`, which also feeds the conformance
report.
