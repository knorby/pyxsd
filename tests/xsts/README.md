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
`--filter SUBSTRING`, `--timeout SECONDS`, `--json`, `--list-only`.

A full XSD 1.1 run over 41,735 cases takes roughly three minutes on a modern
laptop with `--jobs 8`.

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

The suite is deliberately **not** wired into CI yet: it is slow, and pyxsd
does not yet pass it, so a required job would be permanently red. The intended
shape, once the pass rate makes it useful, is a nightly job sharded by
contributor that compares against the baseline. Revisit after the engine has
a stable, reviewed baseline.
