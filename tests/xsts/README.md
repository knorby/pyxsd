# W3C XML Schema Test Suite integration

This package runs the official [W3C XML Schema Test Suite][suite]
(`w3c/xsdtests`) against pyxsd, with
[`xmlschema`](https://xmlschema.readthedocs.io/) as a full-XSTS comparison engine.
It is test infrastructure: it does not change `src/pyxsd/`.

The public [XSTS methodology and results guide](../../docs/xsts.md) is the
authoritative explanation of selection, adapters, raw checked-case agreement,
residual dispositions, the extra-suite boundary, and both xmlschema roles.
This README keeps the contributor commands and baseline-update contract.

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
    --baseline tests/xsts/baseline-xsd11.toml --timeout 180

# Regenerate the baseline after reviewing the differences
uv run python tests/report_xsts.py --profile xsd11 --timeout 180 \
    --write-baseline tests/xsts/baseline-xsd11.toml
```

Useful flags: `--jobs N` (default: one per CPU), `--engine pyxsd|xmlschema|both`,
`--filter SUBSTRING`, `--timeout SECONDS`, `--json`, `--list-only`,
`--enforce`.

A full XSD 1.1 run over 41,735 cases takes roughly three minutes on a modern
laptop with `--jobs 8`. Full runs are timing-sensitive: `particlesZ036_b1`
now passes in roughly 38 s on a modern laptop, which is close to the 60 s
default budget. Use `--timeout 180` (as CI does) when matching CI or running
on a loaded machine. A timeout is a harness outcome, not a verdict; a case
that crosses the budget should be profiled rather than silently re-budgeted.

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
  namespaced principal documents and includes chameleons, in listed order.
  Documents another member already composes are not loaded twice. Documents
  are referenced by absolute path so their own relative includes still resolve.
- **No schema.** The reporting runner and PR gate enable pyxsd's permissive
  wrapper synthesis for instance-only groups: normally the root is declared
  as `xs:anyType`; a resolvable schema hint that already declares it prevents
  a duplicate declaration. Synthesis is an adapter approximation, not a new
  corpus case. The xmlschema adapter does not synthesize; those groups remain
  `adapter-gap` for that engine.
- **Comparison engine.** The `xmlschema` result is compared to the suite's
  expectations too. Where the comparison engine disagrees with the suite,
  that is an oracle-erratum candidate, not proof the suite is wrong.

## Baseline

`baseline-xsd11.toml` records the outcome of every case under both engines.
It is how the suite becomes a regression gate without requiring pyxsd to be
correct everywhere. Any change — a pass turning into a fail, or a known fail
turning into a pass — fails the `--baseline` comparison until the baseline is
intentionally regenerated.

An ordinary local report without `--baseline` does not fail just because it
contains failures. The required PR test asserts an exact baseline match for
both engines; the dispatch workflow uses the looser `--enforce` rule below.

Regenerate only after reviewing the diff. A new pass is usually good news but
it changes what future runs treat as the status quo.

The baseline is a large generated file; review it by looking at the change
count and the `regression`/`improvement` breakdown, not by reading every line.

## Run metadata

Every run records how it was produced, both in `--json` output (under
`provenance`) and in baselines written with `--write-baseline` (under
`[meta]`): `profile`, `suite_file`, `case_count`, `complete`, `engines`,
`timeout_seconds`, `jobs`, `command`, `source_revision`, `corpus_revision`,
`python_version`, `platform`, `engine_versions`, and `run_completed_at`.

- `source_revision` is the repository commit; `corpus_revision` is the pinned
  submodule commit recorded for `tests/xsts/corpus`.
- `command` is rebuilt from the raw argument list, so a slice can be re-run
  exactly as it was recorded.
- `engine_versions` lists `name==version` strings: pyxsd and elementpath
  always, and xmlschema only when it was selected and installed.
- `run_completed_at` is the UTC timestamp captured when the run finished.
  Timestamps come from the run, never from report generation, so rebuilding
  output from a baseline stays deterministic.

`complete` is true only for a run with no `--filter` and no `--limit` — the
whole `suite.xml` profile (41,735 cases per profile). A filtered or limited
slice records `complete = false` with its own `case_count` and `command`.
Writing a baseline from a slice is still allowed; the `complete = false`
metadata is what identifies it as partial, so prefer regenerating the
checked-in baselines from complete runs.

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

The XSD 1.1 gate runs on every PR: the `test` job in
[`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) executes
`tests/test_xsts_suite.py` with `PYXSD_RUN_XSTS=1` after the coverage run
(about three minutes locally; no coverage instrumentation, to keep it fast).
The XSD 1.0 profile and ad-hoc slices stay on the dispatch-only
[`.github/workflows/xsts.yml`](../../.github/workflows/xsts.yml) workflow
(Actions → xsts → Run workflow). Choose one profile or both, and
optionally cap the case count with `limit`.

The workflow runs `--engine pyxsd`, `--enforce`, and `--timeout 180` against
each checked-in baseline. `--enforce` exits `2` when a recorded pass no longer passes
(a regression) or a newly observed case is a failure, and `0` otherwise. It
ignores cases the run did not observe (a `--limit` slice, or an engine that
was not selected), so a partial run does not fail on the thousands of
un-run keys; the ordinary `--baseline` comparison still reports every change
and exits `1`.

The corpus submodule is cached by its pinned commit, so repeat dispatches skip
the clone. Regenerate a baseline locally only after reviewing the diff:

```bash
uv run python tests/report_xsts.py --profile xsd11 --engine both --timeout 180 \
    --write-baseline tests/xsts/baseline-xsd11.toml
uv run python tests/report_xsts.py --profile xsd10 --engine pyxsd --timeout 180 \
    --write-baseline tests/xsts/baseline-xsd10.toml
```

`baseline-xsd11.toml` records both engines and must be regenerated with
`--engine both` (a pyxsd-only rewrite would drop every recorded xmlschema
entry); `baseline-xsd10.toml` records pyxsd only. The dispatch gate may still
enforce either baseline with `--engine pyxsd`: enforcement ignores unobserved
keys, so the recorded oracle entries still anchor the comparison.
