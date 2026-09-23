# W3C XML Schema Test Suite integration

This package runs the official [W3C XML Schema Test Suite][suite]
(`w3c/xsdtests`) against pyxsd, with
[`xmlschema`](https://xmlschema.readthedocs.io/) as an independent oracle.
It is test infrastructure: it does not change `src/pyxsd/`.

[suite]: https://github.com/w3c/xsdtests

## Corpus

The corpus is a git submodule pinned to a fixed commit:

```bash
git submodule update --init tests/xsts/corpus
```

It is **optional**. The rest of the test suite runs with `uv sync && pytest`
without it; the harness tests here use small synthetic catalogs and skip
gracefully when the corpus is absent.

The upstream files are never modified. They are made available under the
[W3C Document Notice and License][license]; see `corpus/00COPYRIGHT` and
`corpus/README.md` for the upstream terms. Our own harness code in this
directory follows the repository's license.

[license]: https://www.w3.org/Consortium/Legal/copyright-documents-19990405.html

## Running

The local runner supports two profiles. `xsd11` (default) claims the XSD 1.1
version token and pairs with `xmlschema.XMLSchema11`; `xsd10` claims XSD 1.0.

```bash
# Quick look at a slice
uv run python tests/report_xsts.py --profile xsd11 --limit 500

# Full run, parallel across CPUs, comparing against the checked-in baseline
uv run python tests/report_xsts.py --profile xsd11 \
    --baseline tests/xsts/baseline-xsd11.toml

# Regenerate the baseline after reviewing the differences
uv run python tests/report_xsts.py --profile xsd11 \
    --write-baseline tests/xsts/baseline-xsd11.toml
```

Useful flags: `--jobs N` (default: one per CPU), `--engine pyxsd|xmlschema|both`,
`--filter SUBSTRING`, `--timeout SECONDS`, `--json`, `--list-only`,
`--enforce`.

A full XSD 1.1 run over 41,735 cases takes roughly three minutes on a modern
laptop with `--jobs 8`.

When re-running by hand, use a clean bytecode cache
(`PYTHONPYCACHEPREFIX=$(mktemp -d)`). A same-length source edit can leave a
header-valid but stale `.pyc` in place and silently invalidate a
measurement.

## How a case is judged

- **Applicability.** A test is `not-applicable` when no version dimension it
  is tagged with is claimed by the profile. Version tokens on sets, groups
  and tests are a disjunction; `expected/@version` is a conjunction.
- **Verdict.** For pyxsd, a phase is valid when it produced no `ERROR`
  issues. A `PyXSDError` is an invalid schema or instance; any other
  exception is a harness-level `error`, because a crash is not a verdict.
- **Expectations.** Where a test carries an unversioned expectation and a
  version-tagged one, the version-tagged one wins for a matching profile.
- **Multi-document schemas.** pyxsd takes one schema path, so a group listing
  several documents is driven by an otherwise-empty schema that imports
  namespaced documents and includes chameleons, in listed order. Documents
  are referenced by absolute path so their own relative includes still
  resolve.
- **No schema.** A group with only instance tests is an `adapter-gap`:
  pyxsd cannot validate without a schema.
- **Oracle.** The `xmlschema` result is compared to the suite's expectations
  too. Where the oracle disagrees with the suite, that is an oracle-erratum
  candidate, not proof the suite is wrong.

## Baseline

`baseline-xsd11.toml` records the outcome of every case under both engines.
It is how the suite becomes a regression gate without requiring pyxsd to be
correct everywhere. Any change — a pass turning into a fail, or a known fail
turning into a pass — fails the `--baseline` comparison until the baseline is
intentionally regenerated.

Regenerate only after reviewing the diff. A new pass is usually good news but
it changes what future runs treat as the status quo.

The baseline is a large generated file; review it by looking at the change
count and the `regression`/`improvement` breakdown, not by reading every line.

## Scope and honest limits

Passing this suite is evidence, not certification:

- The default profile claims XSD 1.1 only. Tests gated to other dimensions
  (Unicode versions, `full-xpath-in-CTA`) are `not-applicable` and counted
  separately.
- `invalid-latent` is treated as an invalid expectation.
- Non-Boolean outcomes (`notKnown`, `indeterminate`,
  `implementation-defined`, `implementation-dependent`) are `not-checkable`.
- The oracle's own disagreements are surfaced but not treated as truth.

## Continuous integration

The suite is deliberately **not** part of the required CI path: a full profile
takes a few minutes, so it runs from the dispatch-only
[`.github/workflows/xsts.yml`](../../.github/workflows/xsts.yml) workflow
instead (Actions → xsts → Run workflow). Choose one profile or both, and
optionally cap the case count with `limit`.

The workflow runs `--engine pyxsd` and `--enforce` against each checked-in
baseline. `--enforce` exits `2` when a recorded pass no longer passes
(a regression) or a newly observed case is a failure, and `0` otherwise. It
ignores cases the run did not observe (a `--limit` slice, or an engine that
was not selected), so a partial run does not fail on the thousands of
un-run keys; the ordinary `--baseline` comparison still reports every change
and exits `1`.

The corpus submodule is cached by its pinned commit, so repeat dispatches skip
the clone. Regenerate a baseline locally only after reviewing the diff:

```bash
uv run python tests/report_xsts.py --profile xsd11 --engine pyxsd \
    --write-baseline tests/xsts/baseline-xsd11.toml
uv run python tests/report_xsts.py --profile xsd10 --engine pyxsd \
    --write-baseline tests/xsts/baseline-xsd10.toml
```

`baseline-xsd11.toml` records both engines; `baseline-xsd10.toml` records
pyxsd only, so keep `--engine pyxsd` for the XSD 1.0 gate (or regenerate it
with both engines first).
