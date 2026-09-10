# Changelog

All notable changes to pyxsd are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

For a narrative explanation of what changed between 0.1 and 1.0 — including a
complete breaking-changes table and step-by-step upgrade instructions — see the
[migration guide](docs/migration-1.0.md).

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
- Over 600 tests, including a 54-case W3C-derived conformance corpus with a
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
