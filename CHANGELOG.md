# Changelog

All notable changes to pyxsd are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

For a narrative explanation of what changed between 0.1 and 1.0 — including a
complete breaking-changes table and step-by-step upgrade instructions — see the
[migration guide](docs/migration-1.0.md).

## [Unreleased]

A validation-correctness pass over the 1.0.0 feature set. Public API and
round-trip output are unchanged except where the previous behavior was
provably wrong.

### Added

- `pyxsd.derivation`: XSD type-derivation compatibility used by `xsi:type`
  dispatch, honoring element/type `block` (`#all`, `extension`, `restriction`).
- `unexpected-element` and `circular-attributeGroup` validation codes.
- `ValidationIssue.phase` (`"schema"` or `"instance"`) and
  `ValidationReport.for_phase()`, so schema-compilation diagnostics can no
  longer be confused with instance diagnostics.
- Manifest `expected_values` assertions in the conformance corpus (defaults
  and folds are checked by value, not just by a clean report).
- Opt-in, non-gating `tests/test_oracle.py` cross-check against the
  independent `xmlschema` library (dev dependency; skipped unless
  `PYXSD_RUN_ORACLE=1`).
- `ParseModes` presets (`STRICT`, `LAX`) and the `BindingPolicy` dataclass,
  plus `PyXSD(mode=...)` and CLI `--mode strict|lax`. Modes change only what
  is bound into the tree; the validation report stays strict. See
  `docs/binding.md`.
- `examples/musicxml/`, `examples/gpx/`, and `examples/docx/`: real-format
  schema/instance/transform examples with end-to-end tests. The `docx`
  example renders Markdown under `ParseModes.LAX` from a deliberately messy
  document.

### Fixed

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

### Changed

- Compose include/redefine cycles are deduplicated and reported as a
  `compose-cycle` warning rather than a fatal error.
- The conformance runner validates the schema and instance phases
  independently, so an instance error can no longer mask a schema error.
- The `xsi_type` fixture and `xsi:type` corpus cases now use a genuinely
  derived type (the previous schemas were not valid XSD).
- The conformance corpus is described as independently authored, suite-inspired
  regression cases rather than copied W3C/NIST cases.
- The crystallography transforms from 0.1 moved from `examples/transforms/`
  to `examples/legacy/` (their schemas and data are no longer distributed).
- GitHub Actions are pinned to the latest majors and the Pages deploy steps
  run only on `main`, so pull requests build docs without requiring Pages.
- Local design notes under `docs/superpowers/` are no longer tracked.

## [1.0.0] - 2026-09-10

pyxsd 1.0.0 is a ground-up modernization of the 2006 0.1 release. The library
was ported from Python 2.3 to Python 3.11+, reorganized into a `src/` layout,
rewritten with modern Python idioms, extended to cover substantially more of
XSD 1.0, and placed under a comprehensive test and conformance suite.

### Added

- Full XSD 1.0 built-in type lattice: all 45 built-in datatypes (derivation
  families, temporal types, binary types, list types) with lexical validation.
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
  messages. Library users get `parser.report`; CLI users get `--strict`.
- `XMLNode` runtime-checkable Protocol describing the instance-tree shape
  (`_name_`, `_attribs_`, `_children_`, `_value_`).
- Element and attribute descriptors support `__set_name__`, class-level
  access, and working programmatic assignment.
- Over 600 tests, including a 54-case suite-inspired conformance corpus with a
  standalone pass-rate reporter (`tests/report_conformance.py`).
- Full type annotations; mypy runs in CI.
- Sphinx documentation site (MyST + furo) with quickstart, CLI reference,
  architecture notes, transform authoring guide, migration guide, and API
  reference; published via GitHub Pages.
- Runnable end-to-end demo in `examples/demo/` (CLI and library styles) and
  a working custom-transform example.
- PyPI publishing via a tag-driven release workflow with OIDC trusted
  publishing.

### Changed

- Requires Python 3.11+ (was Python 2.3).
- Project metadata lives in `pyproject.toml` (hatchling build, src layout);
  `setup.py` removed.
- Package code moved to `src/pyxsd/` with snake_case module names throughout
  (`pyXSD.py` → `pyxsd/parser.py`, `xsdDataTypes.py` → `xsd_data_types.py`,
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

### Removed

- The eight crystallography transforms (atom, vector, bravais lattice, cell
  sizer, coordinate viewer, format for visit, sphere cutter) moved out of the
  package to `examples/transforms/`; they are no longer installed with the
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
- `SendTreeToPyXSD` leaked temporary files into the working directory.
- Schema hints (`xsi:noNamespaceSchemaLocation`) resolved relative to the
  current directory instead of the data file's directory.
- Written documents with `xsi:` attributes lost the `xmlns:xsi` declaration;
  the writer now injects it so output reparses.
- Primitive-typed root elements crashed; they now parse, validate, and honor
  `xsi:nil`.
- Numerous other latent bugs found by the test suite and conformance corpus
  (see the migration guide for the complete narrative).

## [0.1] - 2006-09-11

Initial release: Python 2.3 prototype developed at Oak Ridge National
Laboratory for crystallography XML tooling. See `docs/history/` for origins.
