# Changelog

All notable changes to pyxsd are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

For a narrative explanation of what changed between 0.1 and 1.0 — including a
complete breaking-changes table and step-by-step upgrade instructions — see the
[migration guide](https://pyxsd.knorby.com/migration-1.0.html).

## [Unreleased]

### Added

- Structured, bounded validation reports with JSON-safe issue details, source
  occurrence locations, and `Document.locate()` lookups.
- Resource-policy hooks for schema and instance reads, including virtual
  source layouts and policy-enforced parser admission.
- Native schema introspection with dependency queries and filtered stub
  rendering.
- Optional `pyxsd[mcp]` integration with a real stdio MCP server, private
  workspaces and immutable revisions, schema exploration, XML diagnostics,
  bounded XPath queries, exact issue explanations, artifacts, resources and
  prompts. Includes clean worker processes, bounded JSON IPC, leased schema
  caches, authenticated cursors and bounded worker protocols. Authenticated
  streamable HTTP adds fixed-issuer JWKS verification, subject/scoped ownership,
  protected resource metadata, host/origin controls and exact binary transfer.
  Execution remains disabled by default; trusted-local previews require explicit stdio opt-in.

### Fixed

- Schema binding indexes are released after each parse, preventing per-parse
  state from accumulating.

## [1.2.1] - 2026-10-20

### Added

- Python 3.15 support: 3.15 joins the tested range (3.11–3.15). Text-mode
  file I/O (CLI stub/transform output, transform-file loading, document and
  display writers) is now explicitly UTF-8 on every Python version, matching
  3.15's PEP 686 default — on Windows this changes decode behavior for
  transform files previously read in the locale encoding.
- Mapping results from transforms (including the 3.15 builtin `frozendict`)
  are written as JSON by the CLI and traversed by `pyxsd.tree.iter_tree`;
  previously only exact `dict` instances were recognized.

### Fixed

- Generated classes for declarations without a usable name no longer put a
  `None` key in the class namespace; Python 3.15 warns about non-string keys
  in a class `__dict__`. Such declarations bind under synthetic
  `_unnamed_element` / `_unnamed_attribute` keys (numbered on collision),
  marked as intentional so they log at debug rather than warning.

## [1.2.0] - 2026-10-18

### Added

- Mixin extensions attach your own classes to the classes a schema
  generates. An `Extensions` registry
  (`Schema.compile(..., extensions=ext)`) selects mixins by type name (a
  plain local name or a Clark name), namespace, kind (optionally
  `derived_from` an XSD base), instance path, or an arbitrary predicate,
  each usable as a decorator or a direct call; a later registration
  outranks an earlier one and a mixin matched by several selectors folds
  in once. Path selectors reach anonymous inline types and resolve per
  compile against the composed declaration tree. Mixins sit between the
  generated class and the XSD derivation bases in the MRO, so their
  methods override inherited members while the generated descriptors keep
  working. A mixin may define `__pyxsd_validate__(self)`, whose findings
  join the instance report: a plain string becomes an
  `extension-validation` error, a `ValidationIssue` keeps its own
  severity and code, and a raising hook is isolated to one
  `extension-error`. Registries are standalone — read at compile time
  with no global state and no runtime class patching. See
  `docs/extensions.md`.
- `Schema.stub()` emits a truthful type-stub surface for the classes a
  compiled schema creates at runtime: the full `.pyi` document with the
  consumption preamble, class blocks that mirror the XSD derivation, and
  root-element aliases, or the agent-oriented compact description
  (`Schema.stub(compact=True)`) with short alias spellings and a legend.
  Annotations model valid parsed documents, and a construct a stub
  cannot type truthfully (a union-typed member, a recursive back edge, an
  unresolvable declaration) degrades to `Any` with a comment. Rendering
  reads the live class table, so regenerating after an overlay is loaded
  shows its members, and it never requires a valid report. Also available
  as `pyxsd.stubs.emit_stub(schema, *, compact=False)`.
- CLI `--emit-stubs [PATH]` compiles the schema alone (no XML input is
  read) and writes its type stub to `PATH`, or to stdout when `PATH` is
  omitted; `--stubs-compact` selects the compact form. Both compose with
  `--mode`, `--namespaces`, `--xsd-version`, and
  `-c/--overlayClassesFile`.
- Temporal conversion methods on the XSD date/time types:
  `Date.to_date()`, `DateTime.to_datetime()`, and `Time.to_time()`. An
  explicit timezone offset yields an aware value and an absent one a
  naive value, fractional seconds keep microsecond precision, XSD's
  end-of-day `24:00:00` rolls `DateTime` to the next midnight while
  `Time` raises `ValueError`, and years outside 1..9999 raise
  `ValueError`.
- A `strict` mode and native value conversions for the remaining value
  types, with an elementpath complement per family. `strict=True` makes
  the lossy conversions raise instead: `Date.to_date()` raises instead
  of dropping the timezone offset, `to_datetime()` / `to_time()` /
  `to_timedelta()` raise instead of truncating a non-zero
  sub-microsecond fraction, and `QName.to_expanded_name()` raises
  instead of returning the lexical form of an unresolved value or the
  bare local name of a prefixed value whose prefix the context did not
  bind;
  unrepresentable values (years outside 1..9999, `Time("24:00:00")`,
  duration year/month fragments) raise `ValueError` with or without
  `strict`. `to_gregorian()` converts every temporal and `g*` calendar
  type to its `elementpath.datatypes` counterpart, offset-preserving,
  covering years outside 1..9999 (XSD 1.1 lexical
  year `0000` maps to elementpath's `-1`, which numbers years with no
  year zero) and the recurring `g*` periods, which have no stdlib
  representation; the temporal ones truncate a sub-microsecond fraction
  by default and raise under `strict`. The elementpath types are not
  `datetime` subclasses
  and never a fallback inside the stdlib methods. Durations convert
  through `to_timedelta()` (year/month fragments always raise —
  calendar durations are never approximated as 30-day months; a value
  outside `datetime.timedelta`'s range raises `ValueError`), the
  exact signed `YearMonthDuration.to_months()`, and `to_xsd_value()`
  (elementpath holds both the months and the seconds properties) for
  the durations `to_timedelta()` refuses, rounding a sub-microsecond
  fraction by default and raising under `strict`;
  `HexBinary` / `Base64Binary.to_bytes()` decode exactly, and
  `AnyURI.to_parsed_uri()` best-effort parses the literal text, since
  XSD's lexical space admits characters meant to be URI-escaped.
  Facet introspection: `XsdDataType.patterns()` returns the effective
  `xs:pattern` facets as compiled `re.Pattern` objects — one AND-of-OR
  tuple per restriction step, the same objects validation applies,
  translated from the XSD 1.1 pattern dialect by elementpath —
  `XsdDataType.enumerations()` returns the effective enumeration values
  after intersection across restriction steps as instances of the
  restriction's base datatype, and `pyxsd.facets.compile_pattern()`
  translates and compiles one standalone XSD pattern (raising
  `ValueError` on an illegal one). See `docs/data-model.md` for the
  conversion reference.
- The package ships the PEP 561 `py.typed` marker, so a generated full
  stub's imports of pyxsd classes (`SchemaBase`, the
  `pyxsd.xsd_data_types` classes) are statically analyzable by mypy and
  IDEs.

### Changed

- Compile and bind performance on large real-world schemas: repeated
  substitution-group head/member lookups are memoized per compile (FHIR's
  `fhir-single.xsd` compiles in about half the time), `attributeGroup`
  references resolve through a per-compile index, two per-class bind-time
  scans (`_inheritable_attribute_names` and `_getElements`) are cached,
  and a repeated single-node particle's end set is computed
  arithmetically instead of by an O(n²) frontier walk. Each change is
  behavior-preserving: no validation outcome, report, or pinned count
  moves.
- The test suite gained a real-world conformance corpus and a W3C XML
  Schema Test Suite gate on every pull request. `tests/conformance/`
  compiles five published schemas (FHIR R5, HL7 CDA R2, ECMA-376
  WordprocessingML and SpreadsheetML, MusicXML 4.0), fetched on demand
  and cached, pins each one's class and report-error counts per error
  code, and runs a 30 s compile tripwire with a benchmark artifact; the
  XSTS suite now runs on every PR against its checked-in baseline with a
  configurable per-case timeout (`PYXSD_XSTS_TIMEOUT`). See
  `tests/conformance/` and `tests/xsts/README.md`.
- CI reports test coverage to Codecov from the unit-test matrix, the
  xmlschema oracle, and the optional-integration adapters, and the README
  carries a Codecov coverage badge.

### Fixed

- Real-world schema compilation no longer reports spurious errors. On the
  five published schemas now exercised by the conformance corpus, pyxsd
  reported 748 findings where the `xmlschema` oracle reports none; every
  one was a pyxsd gap. Closed: a circular `xs:include` is no longer
  skipped wholesale; the `xml:` namespace (for `xml:lang`, `xml:space`,
  and `xml:base`) is known without a `schemaLocation`; cross-namespace
  `element`, `attribute`, and `group` references resolve through imports;
  a local name shared across namespaces no longer conflates types; and
  case-distinct type names are kept apart. Value-constraint checks keep
  built-in types authoritative while validating `ref`-form attribute-use
  defaults by the writing document. Identity comparisons for
  content-model particles, wildcard admission, open-content wildcards,
  `xs:all` extension overlap, default attribute-group uses, and
  attribute-use expanded names now use authored namespaces. Namespace
  adoption threads through nested `xs:redefine`/`xs:override` bases and
  chameleon includes, colon-containing (Clark) reference locals rebind
  correctly, and legacy mode binds prefixed references and namespaced
  instance attributes by their denoted namespace. With the fixes, all
  five corpus schemas compile with zero findings.

## [1.1.1] - 2026-10-14

### Added

- Bound child nodes now carry an optional, parse-produced `_tail_` attribute:
  the character data that followed the child in the source, recorded for
  every bound child whose source had following text; the writer and dict
  export use it only under a parent whose content admits character data. It
  is not part of the `pyxsd.XMLNode` protocol, and transform-made or
  hand-built trees need no changes — a missing `_tail_` simply means no
  recorded following text.

### Fixed

- Mixed-content character data is preserved instead of lost. For mixed
  (`mixed="true"`), `xs:anyType`, and wildcard pass-through content, an
  element's leading text binds verbatim in `_value_` (no stripping or
  splitting) and the text following each child is recorded as that child's
  `_tail_`; previously the leading text was stripped/split and inter-element
  text was dropped entirely. A mixed element now serializes inline without
  writer-injected whitespace, so ordered text and significant whitespace
  survive parse/serialize/parse. Element-only and simple-content output is
  byte-for-byte unchanged.
- `Document.xpath()`, `find()`, and `findall()` project `_value_`/`_tail_`
  onto `text`/`tail`, so mixed character data is queryable in document
  order. Element-only content's recorded layout whitespace now appears in
  text-valued queries too, so the projection is document-faithful rather
  than source-XPath-exact.
- `Document.to_dict()`/`to_json()` export all recorded character data of a
  character-data-admitting element under `$`, in document order (the leading
  text followed by every child's `_tail_`). The dict shape cannot represent
  text/element interleaving, so `to_string()` and `xpath()` are the faithful
  mixed-content views.

## [1.1.0] - 2026-10-11

### Added

- `RecordProjection.write_dataset` exports sequential local XML inputs to a new
  manifested Parquet dataset with fixed schemas, source/row provenance, and
  raise/report policies. Failed sources contribute no rows, including late
  projection failures. Includes manifest-directed reading and consumer examples.
- Sequential `Schema.iter_parse` with explicit local `DocumentSource` inputs,
  independent `ParseOutcome` records, and raise/report error policies.
  Includes a dependency-free validation example and lifecycle tests.
- Independent `pyxsd[pydantic]` and `pyxsd[arrow]` extras, retaining Python
  3.11+ support and the existing base dependency set.
- Dynamic standalone Pydantic models, schema-shaped document/node snapshots,
  and JSON Schema for the supported projected-data contract.
- Explicit Arrow record schemas, nested tables, typed empty selections, and
  row-batched Parquet output with atomic local-path replacement.
- Named Arrow scalar columns using `FieldSource` to read a selected record,
  its ancestors or the document root, with schema-derived types and exact
  occurrence associations. Includes an Arrow-only purchase-order-line example.
- Runnable data-workflow examples: DuckDB and Polars analytics over Arrow
  projections (`examples/analytics/`), and a Pydantic purchase-order
  assessment with exact Decimal arithmetic (`examples/pydantic/assess_orders.py`),
  documented in `docs/data-workflows.md` with executable command coverage.

### Fixed

- Contextual Arrow columns reuse active ancestor guards instead of rescanning
  shared root/order child collections for every row and column. Guard state
  remains extraction-local; documents must stay unchanged during iteration.
- Primitive XML output uses XSD lexical forms for list values, tiny decimals,
  and nonfinite floats instead of invalid Python spellings: list values
  serialize space-joined rather than as Python list reprs, and tiny decimals
  use plain notation (for example `0.00000000001`, not `1E-11`).
- `RecordProjection.write_parquet` preserves nested Arrow list field names,
  matching dataset part and `_schema.parquet` output for the same projection.

## [1.0.0] - 2026-09-22

pyxsd 1.0.0 is a ground-up modernization of the 2006 0.1 release. The library
was ported from Python 2.3 to Python 3.11+, reorganized into a `src/` layout,
rewritten with modern Python idioms, extended to cover substantially more of
XSD 1.0, and placed under a comprehensive test and conformance suite. It also
carries a full validation-correctness pass, opt-in XML Namespaces support with
binding/parse modes, and three real-world examples validated against their
official schemas.

### Added

- The public object model: a compiled schema is a `pyxsd.Schema`
  (`Schema.compile(xsd, mode=..., namespace_schemas=..., overlay=...)`),
  and each `schema.parse(xml)` binds one instance document into a
  `pyxsd.Document` carrying the merged `ValidationReport`
  (`document.report`), `is_valid`, `require_valid()`, `transform(fn)`,
  `revalidate()`, `walk()`, `write(dest)`, and `to_string()`. One
  compiled schema can serve any number of documents. The one-call
  shortcuts `pyxsd.parse(xml, xsd=...)` and `pyxsd.compile(xsd)` resolve
  schema hints from the instance exactly like the CLI. The schema's own
  findings are available at `schema.report` before any instance is
  parsed.
- `pyxsd.ValidationError` (a `PyXSDError` subclass) raised by
  `Schema.require_valid()` and `Document.require_valid()`; its `report`
  attribute holds every issue that failed the check.
- Transforms are callables in the library: `document.transform(fn)`
  applies any callable taking the tree root; one returning a tree root
  comes back as a new `Document` against the same schema; any other return
  value, including `None`, is returned unchanged. A returned document shares
  the pre-transform report;
  `document.revalidate()` re-parses the serialized tree for a report
  that reflects its current shape. `document.walk()` yields every node
  pre-order. On the command line the `-t 'Name(args)'` /
  `-T file` call-string syntax is unchanged.
- Full XSD 1.0 built-in type lattice: all 45 built-in datatypes (derivation
  families, temporal types, binary types, list types) with lexical validation.
- Facet enforcement for user-defined simpleTypes: `enumeration`, `pattern`,
  `length`, `minLength`, `maxLength`, `minInclusive`, `minExclusive`,
  `maxInclusive`, `maxExclusive`, `totalDigits`, `fractionDigits`, and
  `whiteSpace`. Restriction chains follow the XSD merge rules (patterns
  conjunct, enumerations intersect, bounds tighten); list types count items
  for the length family; enumeration and bounds compare XSD values rather
  than spellings; `whiteSpace` is applied before other facets. Facet
  applicability, facet-value validity, and pattern syntax are reported as
  schema-compilation errors. `pattern` uses the XSD 1.1 dialect through the
  new `elementpath` dependency (minimal, pure-Python, no compiled
  extensions), and expands the XML 1.1 `\i`/`\c` name-character classes
  including astral characters.
- `BindingPolicy.facets` (`"strict"` by default, `"off"` restores
  parsing-only behavior for data-mapping users).
- Facet enforcement reaches values bound through `simpleContent` complex
  types (inline and direct restrictions, and extensions), applies element
  `default`/`fixed` there, and fixes the crash when a `simpleContent`
  extension's base is a primitive such as `xs:int`.
- Value-space comparison for temporal datatypes preserves fractional
  seconds, distinguishes zoned from unzoned spellings while treating
  different offsets of the same instant as equal, normalizes `24:00:00`,
  and accepts year `0000` (XSD 1.1). Enumeration, bounds, and fixed-value
  checks use the corrected values.
- `+INF` is accepted as a lexical form of `xs:float` and `xs:double`
  (XSD 1.1).
- Structural schema elements: `xs:all`, `xs:union` (including inline member
  types), `xs:group` with references (with cycle detection), attribute group
  references, and permissive `xs:any` / `xs:anyAttribute` pass-through.
- Instance semantics: `minOccurs`/`maxOccurs` enforcement, nillable elements
  with `xsi:nil`, `default`/`fixed` for elements and attributes (with forced
  values), prohibited attributes, `xsi:type` dispatch on elements and roots,
  `abstract` elements and types, `block` on substitution heads, and substitution
  groups.
- Schema composition: `xs:include` (including chameleon schemas), `xs:import`,
  and `xs:redefine` with cycle and namespace checks.
- Identity constraints: `xs:key`, `xs:unique`, and `xs:keyref` with an XPath
  subset (`child`, `@attr`, `./step`, `.//descendant` steps), applied as a
  post-parse validation pass.
- `ValidationReport` — errors and warnings collected as structured
  `ValidationIssue` records with stable issue codes instead of printed
  messages. Each document reports at `document.report` (the schema's
  own findings at `schema.report`); CLI users get the rendered report
  on stderr and `--strict`.
- `XMLNode` runtime-checkable Protocol describing the instance-tree shape
  (`_name_`, `_attribs_`, `_children_`, `_value_`).
- Element and attribute descriptors support `__set_name__`, class-level
  access, and working programmatic assignment.
- Over 1,000 tests, including a 93-case independently authored, suite-inspired
  conformance corpus with a standalone pass-rate reporter
  (`tests/report_conformance.py`), plus a cross-check against the
  independent `xmlschema` library.
- Full type annotations; mypy runs in CI.
- Sphinx documentation site (MyST + furo) with quickstart, CLI reference,
  architecture notes, transform authoring guide, binding/parse modes,
  migration guide, and API reference; published at
  <https://pyxsd.knorby.com/>.
- Runnable end-to-end demo in `examples/demo/` (CLI and library styles) and
  a working custom-transform example.
- Real-world examples (`examples/musicxml/`, `examples/gpx/`,
  `examples/docx/`) that validate genuine documents against their official
  published schemas, fetched on demand by a local `download_schemas.py` (not
  committed; the tests skip when absent). `musicxml` validates a public-domain
  Bach chorale against the MusicXML 4.0 schema (which imports the XML and XLink
  namespaces); `gpx` validates a real ride against the official GPX 1.1 schema,
  including Garmin extension data admitted by `xs:any processContents="lax"`;
  `docx` validates a realistic `word/document.xml` against the full ECMA-376
  `wml.xsd` and renders style-aware Markdown covering multilevel lists, tables,
  hyperlinks, breaks/tabs, run properties, and `xml:space="preserve"`. All
  three run in `ParseModes.NAMESPACED`; the earlier no-namespace `ParseModes.LAX`
  docx demo is kept under `examples/docx/lax/`.
- PyPI publishing via a tag-driven release workflow with OIDC trusted
  publishing.
- `pyxsd.derivation`: XSD type-derivation compatibility used by `xsi:type`
  dispatch, honoring element/type `block` (`#all`, `extension`, `restriction`).
- `unexpected-element` and `circular-attributeGroup` validation codes.
- `ValidationIssue.phase` (`"schema"` or `"instance"`) and
  `ValidationReport.for_phase()`, so schema-compilation diagnostics can no
  longer be confused with instance diagnostics.
- Manifest `expected_values` assertions in the conformance corpus (defaults
  and folds are checked by value, not just by a clean report).
- `tests/test_oracle.py` cross-check against the independent `xmlschema`
  library (dev dependency; a required CI job, env-gated locally by
  `PYXSD_RUN_ORACLE=1`).
- `ParseModes` presets (`STRICT`, `LAX`, `NAMESPACED`) and the
  `BindingPolicy` dataclass, plus `Schema.compile(..., mode=...)` and CLI
  `--mode strict|lax` / `--namespaces strict|legacy`. Modes change only what
  is bound into the tree; the validation report stays strict. See
  `docs/binding.md`.
- Opt-in XML Namespaces support (`BindingPolicy.namespaces`, default
  `legacy`): namespace capture during parsing, expanded-name component
  identity,   `elementFormDefault` / `attributeFormDefault`-aware instance
  matching, prefixed `type`/`ref`/`xsi:type` resolution, cross-namespace
  `xs:import` (including `Schema.compile(namespace_schemas=...)`),
  wildcard
  namespace lists and `processContents`, QName value-space identity, and
  namespace-aware output writers.
- Validation codes `unknown-namespace-prefix`, `wildcard-no-declaration`,
  and `import-unresolved` (namespaced mode).
- A `namespaces/` category in the conformance corpus (form defaults,
  cross-namespace type/ref, `xsi:type`, wildcards, QName identity), run in
  namespaced mode through both the gating suite and the `xmlschema` oracle.
- `Schema.compile` accepts `xsd_version` (`"1.0"` or `"1.1"`, default
  `"1.1"`), exposed as `Schema.xsd_version`; the CLI gains
  `--xsd-version {1.0,1.1}`. This is the declared version `vc:*`
  conditional-inclusion selectors test against, so a schema compiled as
  1.0 drops a declaration carrying `vc:minVersion="1.1"` (XSD 1.1 §4.2.2).
- XSD 1.0 mode reports XSD 1.1-only vocabulary with `xsd11-construct`
  issues: the `assert`/`assertion`/`alternative`/`openContent`/`override`
  elements, the 1.1-only attributes (`notNamespace`, `notQName`,
  `defaultAttributes`, `defaultAttributesApply`, `inheritable`,
  `xpathDefaultNamespace`), and references to 1.1 built-in types
  (`dateTimeStamp`, `dayTimeDuration`, `yearMonthDuration`,
  `anyAtomicType`, `precisionDecimal`).
- XSD 1.0 mode enforces three 1.0/1.1 semantic differences: the 1.0
  `xs:all` 0..1 child-occurrence cap (`all-rule`), the 1.0 requirement
  that a union declare at least one member type (`declaration-child`), and
  the 1.1-only prohibition on `use="prohibited"` with `fixed`.
- `Document.to_dict()` / `Document.to_json()` export the bound tree as
  plain Python data (attributes under `@`, text under `$`, repeated
  children as lists), with a lexical-text mode and an `always_list`
  option. The shipped `ToDict` transform exposes the codec to
  `Document.transform` and to the CLI, which renders a dict result as
  JSON.
- `Document.xpath()` evaluates an XPath expression over the bound tree
  and returns the original bound nodes (scalars pass through);
  `Document.find()`/`findall()` accept ElementTree's path subset. Both
  support caller-supplied namespace prefixes and resolve to the same
  bound objects the object model uses.

### Changed

- Requires Python 3.11+ (was Python 2.3).
- Project metadata lives in `pyproject.toml` (hatchling build, src layout);
  `setup.py` removed.
- Package code moved to `src/pyxsd/` with snake_case module names throughout
  (`pyXSD.py` → `pyxsd/schema.py` + `pyxsd/document.py`,
  `xsdDataTypes.py` → `xsd_data_types.py`,
  etc.).
- Classes are generated with `types.new_class()`, class wiring runs through
  `SchemaBase.__init_subclass__()`, and descriptors bind via `__set_name__`.
- CLI is flag-based (`pyxsd -i instance.xml -s schema.xsd`); transform calls
  require parentheses (`-t 'PrintData()'`) and are parsed with `ast` instead
  of `eval`; stdin is read directly (no temp file); output files are properly
  flushed and closed; `--strict` exits 1 when validation errors are present.
- The pipeline no longer writes files the caller did not ask for; `_No_Output_`
  semantics are consistent, and `stdout` output works.
- The built-in type classes report their names in true XSD spelling
  (`base64Binary`, not `Base64Binary`) and register in a lookup table generated
  from the classes themselves.
- Instances honor XSD value semantics more faithfully: empty simple content
  reads as an empty string (not the literal string `'True'`), text is
  whitespace-collapsed per token-derived datatypes, and the document order of
  children is preserved.
- Compose include/redefine cycles are deduplicated and reported as a
  `compose-cycle` warning rather than a fatal error.
- The conformance runner validates the schema and instance phases
  independently, so an instance error can no longer mask a schema error.
- The `xsi_type` fixture and `xsi:type` corpus cases now use a genuinely
  derived type (the previous schemas were not valid XSD).
- The conformance corpus is described as independently authored, suite-inspired
  regression cases rather than copied W3C/NIST cases.
- The corpus now carries an optional `mode` field, and the `xmlschema`
  oracle runs every case under its declared binding policy so namespace,
  wildcard, and QName behavior is cross-checked rather than skipped.
- The crystallography transforms from 0.1 moved from `examples/transforms/`
  to `examples/legacy/` (their schemas and data are no longer distributed).
- GitHub Actions are pinned to the latest majors and the Pages deploy steps
  run only on the default branch, so pull requests build docs without
  requiring Pages.
- Element assignment now coerces a plain Python value through the declared
  datatype (as attribute assignment already did) and reports a bad lexical
  value instead of raising `TypeError`; a datatype instance of the wrong
  type or an arbitrary object still raises. This unifies the mutation
  policy across elements and attributes.
- The W3C XML Schema Test Suite harness gained `--enforce` (exit `2` on a
  baseline regression or a newly observed failure) and a dispatch-only
  GitHub Actions workflow that caches the corpus and gates both the XSD 1.1
  and XSD 1.0 profiles against their checked-in baselines.

### Removed

- The monolithic `PyXSD` pipeline class (and the `pyxsd.parser` module
  behind it), which parsed, validated, wrote, and transformed inside one
  constructor. Its roles are split across `Schema.compile`,
  `Schema.parse`, and the returned `Document`; see the migration guide
  for the call-by-call mapping. Breaking change.
- The `SendTreeToPyXSD` transform, which wrote the tree to a temporary
  file and re-fed it through the pipeline;
  `Document.revalidate()` does the same job in one call with no
  temporary files. Breaking change.
- The eight crystallography transforms (atom, vector, bravais lattice, cell
  sizer, coordinate viewer, format for visit, sphere cutter) moved out of the
  package to `examples/legacy/`; they are no longer installed with the
  library. Breaking change — see the migration guide.
- epydoc-based `doc/` tree and dead pyxsd.org links replaced by the Sphinx
  documentation site.

### Fixed

- Schema parsing could not even import on Python 3 (2015 formatting-tool
  syntax error in `schemaBase.py`).
- Generated classes double-registered inline complexType children, producing
  spurious "Order Error" reports.
- Element/attribute descriptor assignment silently discarded the assigned
  value; it now stores and returns it correctly.
- Transform instances were appended via `collection.append[instance]`
  (subscript instead of call) and transform output was written to the *input*
  file name (data loss).
- `xmlFileOutput=True` crashed; boolean output now uses the default file name.
- Transform calls with `**kwargs` expansion produced garbage arguments and now
  are rejected with a clear syntax error.
- Schema hints (`xsi:noNamespaceSchemaLocation`) resolved relative to the
  current directory instead of the data file's directory.
- Written documents with `xsi:` attributes lost the `xmlns:xsi` declaration;
  the writer now injects it so output reparses.
- Primitive-typed root elements crashed; they now parse, validate, and honor
  `xsi:nil`.
- Built-in datatypes: XSD whitespace handling (replace/collapse, XML
  whitespace only), IEEE binary32 semantics for `xs:float`, bounded timezones,
  `24:00:00`, year `0000`/leading zeros, ASCII-only digits, base64 pad-bit
  validation, list types requiring at least one item, explicit XML
  NameStartChar/NameChar ranges, `anyURI` permitting spaces, and value-space
  equality for `fixed` (hex case, list whitespace, equivalent timezones).
- Content models: a compiled particle tree now matches sequences, choices,
  `all`, nested compositors, repeated groups, and substitutions with complete
  consumption; unmatched and trailing children are reported instead of silently
  dropped, and complex-content restrictions no longer retain removed particles.
- Elements whose declared type is a derived simple type are constructed as
  values instead of crashing; primitive-typed roots share the child path's
  empty/default/fixed/value validation and `xsi:nil` rules.
- Identity constraints: tables are scoped to each element occurrence, fields
  compare XSD typed values (not lexical strings), multi-node fields and nilled
  fields are handled, and constraints on primitive declarations are walked.
- Composition: each parser owns its component table (no cross-parser leakage;
  an element and a type may share a name); element `ref`s inside groups resolve;
  nested `attributeGroup` references merge; group/attributeGroup single-level
  `redefine` rebinds inner references; namespace-only `xs:import` is allowed;
  legal include cycles are deduplicated.
- `xsi:type` overrides must be validly derived from the declared type and are
  rejected when blocked; substitution-group members use their own declaration's
  `nillable`/`default`/`fixed`/identity constraints rather than the head's.
- `final` is treated as a whitespace-separated token list.
- `xs:attribute ref="..."` resolves across namespaces, including global
  attributes declared in imported schemas; an unresolved reference reports
  `unknown-attributeRef` and an unresolved type base reports `unknown-type`
  instead of crashing during class construction.
- Unprefixed QName *values* (`type`, `base`, `ref`, `substitutionGroup`) use
  the in-scope default namespace, as XSD requires (previously real OOXML
  types such as `base="CT_Markup"` failed to resolve).
- `xml:space="preserve"` text keeps its significant leading/trailing and
  repeated whitespace; string/simple-content values are no longer stripped.
- Content-model matching is memoized, so large real-world schemas such as
  WordprocessingML no longer backtrack exponentially.
- Class generation iterates the parser-owned component table, so types that
  share a local name across imported namespaces (for example `CT_Color`) no
  longer shadow each other; type lookup resolves the QName before falling
  back to the local name.
- A `ref` site resolves through its target declaration for instance matching,
  so replacement attributes such as `r:id` and `xml:space` bind in the
  referring element's namespace.
- Numerous other latent bugs found by the test suite and conformance corpus
  (see the migration guide for the complete narrative).
- Assignment to an element or attribute descriptor now writes the value's
  lexical form through to the serialized tree, so `doc.root.attr = value`
  survives `to_string()`/`write()`/`revalidate()` (previously it was
  validated but silently dropped from the output).

## [0.1] - 2006-09-11

Initial release: Python 2.3 prototype developed at Oak Ridge National
Laboratory for crystallography XML tooling. See `docs/history/` for origins.
