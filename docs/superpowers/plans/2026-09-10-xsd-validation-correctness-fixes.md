# XSD Validation Correctness Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or
> superpowers:subagent-driven-development to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make pyxsd's validator correct for the XSD 1.0 features it claims to
support — every claimed feature accepts valid documents and rejects invalid ones,
verified by a trustworthy conformance corpus.

**Architecture:** Introduce a compiled schema-semantics layer (particle tree +
content kind + derivation method) built once per generated class and consumed by
a unified, declaration-aware binding path. Public API (generated classes,
descriptors, transforms, writers) unchanged. The datatype layer is fixed
independently: lexical parsing, XSD whitespace normalization, semantic value, and
comparison become distinct concerns.

**Tech Stack:** Python 3.11+ stdlib only at runtime (unchanged); dev-only
`xmlschema` for non-gating oracle cross-checks; pytest.

**Spec:** `docs/superpowers/plans/2026-09-09-pyxsd-1.0.0-modernization.md`
(phases 6–10) and `docs/supported.md` (the support contract).

## Finding index (from the code review)

| ID | Finding | Fixed in |
|----|---------|----------|
| R1 | Schema-derived simple types crash on dispatch | Task 5 |
| R2 | Closed content models accept unmatched/trailing children | Task 4 |
| R3 | Flattened compositors break particle occurrence semantics | Task 4 |
| R4 | Group ref mutates shared declarations | Task 3 |
| R5 | `xsi:type` bypasses derivation compatibility and `block` | Task 7 |
| R6 | Primitive roots skip empty/fixed/default validation | Task 6 |
| R7 | Nil handling inconsistent | Task 6 |
| R8 | Complex restriction retains base particles | Task 3 |
| R9 | Identity values global instead of occurrence-scoped | Task 11 |
| R10 | Identity compares lexical strings, not typed values | Task 11 |
| R11 | Identity field cardinality/nil holes | Task 11 |
| R12 | Substitution members validated with head's constraints | Task 6 |
| R13 | Datatype whitespace / float32 / temporal / equality / lists / base64 / names / anyURI | Tasks 8–10 |
| R14 | Composition registry pollution, group refs, nested attrGroups, group redefine, include cycles, `final` token lists | Tasks 3, 12 |
| R15 | Runner phase blindness, wrong expectations, no value assertions, no provenance | Tasks 1–2, 13 |

## Global Constraints

- Runtime dependencies: **zero** (stdlib only). `xmlschema` is dev-group only
  and non-gating.
- Python floor 3.11; CI matrix 3.11–3.14.
- Gates per task: `uv run pytest --cov=pyxsd`, `uvx ruff@0.16.6 check .`,
  `uvx ruff@0.16.6 format --check .`, `uv run mypy src/pyxsd`.
- Never push; work stays on `fix/validation-correctness`.
- Public API and round-trip output behavior must not change except where a
  finding proves current behavior wrong; each change gets a CHANGELOG entry.

---

## Task 0: CI action versions + Pages deploy (DONE — commit 2650c97)

`astral-sh/setup-uv` has no floating `v10` tag; pin to `v10.1.0` in all
workflows. Docs deploy switched to `configure-pages` + `upload-pages-artifact`
+ `deploy-pages`.

## Task 1: Make the conformance runner phase-aware

**Files:** `src/pyxsd/validation.py`, `src/pyxsd/parser.py`,
`src/pyxsd/schema_base.py`, `tests/conformance_runner.py`,
`tests/test_conformance_runner.py` (new).

Add `ValidationIssue.phase: str` (`"schema"` | `"instance"`, default
`"instance"`), an optional `phase=` argument on `add_error`/`add_warning`, and a
`ValidationReport.for_phase(phase)` filter. `PyXSD` tags its schema-compilation
diagnostics with `phase="schema"`. `run_case` validates the schema phase (when
`schema_valid`) and the instance phase independently; `expected_codes` are
matched within the relevant phase.

## Task 2: Correct the false test expectations

`tests/test_xsd_data_types.py` — flip the XSD-incorrect expectations (anyURI
space rejection, midnight rejection, normalizedString rejection, empty built-in
lists accepted) and mark still-unfixed ones `xfail(strict=True, reason="R13 …")`.
Add manifest support for `expected_values` in `conformance_runner.py` so
behavior-bearing cases (defaults) assert mapped values, not just a clean report.

## Task 3: Compiled content model — particle tree IR

`src/pyxsd/content_model.py` (new): `Particle` dataclass
(`kind`, `min_occurs`, `max_occurs`, `children`, `element`), `ContentKind` enum
(`EMPTY`, `SIMPLE`, `ELEMENT_ONLY`, `MIXED`), `compile_content_model(er)`.
`complex_type.py` compiles at class creation; `SchemaBase.__init_subclass__`
stores `_contentModel_` / `_contentKind_`. Delete `_foldRefOccurrences` mutation
(R4): group references compile to fresh particles with reference-site bounds.
`xsd_type.py` parses `final`/`block` as `frozenset[str]` token sets (R14).

## Task 4: Recursive particle matcher

`content_model.py`: `match_particles(particle, children, report)`. Recursive
descent, complete consumption, every unmatched child reported
(`unexpected-element`). `addElementsTo` consumes the matcher; the three flat
order checkers are deleted (R2, R3). Substitution/wildcard stay as particle
annotations.

## Task 5: Explicit content kind — fix simple-type dispatch

Replace `issubclass(cls, SchemaBase)` dispatch with `_contentKind_` in
`_addChildInstance`, `_primitiveForElement`, and root dispatch. Generated simple
types carry `_contentKind_ = SIMPLE` and `_simpleBase_` (R1).

## Task 6: Unified, declaration-aware binding

One `_bind_element(declaration, xml_node, match_result)` shared by root and
child paths, ordering: nil → nilled empty-content check → xsi:nil on
non-nillable → simple/empty value validation → default/fixed → attributes.
Fixes R6, R7, R12.

## Task 7: `xsi:type` derivation compatibility and `block`

`src/pyxsd/derivation.py::is_validly_derived(override, declared, blocked)` using
the recorded XSD derivation chain. Applied at root and child dispatch (R5).

## Task 8: XSD whitespace processing

`_ws_replace` / `_ws_collapse` over exactly `\t\n\r `; applied by
NormalizedString/Token and collapse types before validation; union applies
collapse before trying members.

## Task 9: Datatype value semantics

`Float` binary32 via `struct`; reject `+INF`; hexBinary/base64 as bytes; list
types as token tuples; temporal types compare aware-normalized. Retain
`.lexical` for original text.

## Task 10: Datatype lexical gaps

Built-in lists require ≥1 item; base64 pad-bit validation; `24:00:00` accepted;
timezone bounded to ±14:00; year 0000/leading-zero rejected; ASCII-only digits;
explicit XML NameStartChar/NameChar ranges; `anyURI` accepts spaces per XSD.

## Task 11: Identity constraints

Tables keyed `(constraint, owner_instance)`; typed field values; field
cardinality 0/1/many; nilled field ⇒ no value; traversal over all bound element
nodes (R9–R11).

## Task 12: Composition and parser-local registry

Module-level `registry` replaced by a parser-owned `ComponentTable` keyed
`(kind, expanded_name)`; import `schemaLocation` optional; include cycles deduped
rather than rejected; group/attributeGroup redefine; refs resolved inside groups;
nested attributeGroup refs; `conftest` stops clearing global state (R14).

## Task 13: Corpus provenance + dev-only oracle

`cases.toml` gains `source` and `expected_values`; `xmlschema` dev dependency;
`tests/test_oracle.py` marked non-gating, cross-checks validity without failing
CI.

## Task 14: Docs + CHANGELOG alignment

Reconcile `docs/supported.md` with validated behavior; CHANGELOG entries per
behavior change.
