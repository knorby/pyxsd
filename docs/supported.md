# Supported features

pyxsd 1.0 implements a substantial, honest subset of XSD 1.0. This page
summarizes what is validated, with pointers to the regression corpus that
backing every claim (54 independently authored manifest-driven cases
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
- `xs:group` definitions and references (nested, with occurrence folding
  and cycle detection)
- `xs:attributeGroup` definitions and references
- `xs:any` / `xs:anyAttribute` wildcards (permissive pass-through)
- `xs:union` (declared members and inline anonymous members)
- element references (`ref`) and substitution groups
- complex content `xs:extension` / `xs:restriction` derivation with `final`
  enforcement

## Instance semantics

- `minOccurs`/`maxOccurs` enforcement (including `unbounded`)
- `xsi:nil` on nillable elements (nil instances preserved in output)
- `default` and `fixed` for attributes and simple-content elements
- `xsi:type` dynamic dispatch on child elements and the document root
- `abstract` elements and types; `block`/`prohibited` restrictions
- root dispatch: both complex-typed and primitive-typed document roots

## Composition and identity

- `xs:include` (chameleon schemas supported), `xs:import` (document-level
  namespace awareness), `xs:redefine` (single level)
- `xs:key`, `xs:unique`, `xs:keyref` with an XPath subset (child steps,
  `.//` descendants, `*`, `.`, `@attr`, multi-step paths)

## Validation reporting

Non-fatal, code-tagged issue reporting via `PyXSD.report` (see
{doc}`validation`), plus `--strict` CI-friendly exit codes and the
`ValidationReport` API for library users.

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
  - Documents are validated in the default/no-namespace style; include/import is namespace-aware at the document level only.
* - Facets on user simpleTypes
  - `enumeration`, `pattern`, `length`, …
  - ignored
  - Every built-in type's own lexical rules and whitespace mode are enforced, but facets declared on user-defined simpleTypes are not. `xs:QName` checks the lexical form only; prefixes are not resolved against a namespace context.
* - Wildcard namespace filtering
  - `processContents`, namespace lists
  - partial
  - Wildcards pass undeclared content through permissively.
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
