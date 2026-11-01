# XSTS methodology and results

pyxsd measures agreement with the W3C XML Schema Test Suite (XSTS) on
catalogued schema and instance verdicts. This page defines the corpus,
processor profiles, adapters, outcomes, and baseline gates behind the results.
For the distinction between XML parsing, schema compilation, instance
assessment, and binding, start with {doc}`xsd-validation`.

**The headline is raw agreement on checked cases:**

```text
checked = pass + fail
agreement on checked cases = 100 * pass / checked
```

A known or disputed failure remains a failure in this calculation. There is
no adjusted rate, best-of-profile result, or combined XSD 1.0/1.1 rate.

## Corpus and case selection

The corpus is the `tests/xsts/corpus` git submodule, pinned to
[`w3c/xsdtests` revision `7bc3365c652a322f3d762021b3879eb92dae7e30`][corpus].
Only its main [`suite.xml`][main-suite] supplies the headline cases. The
catalogue emits **41,735 cases per profile: 15,378 schema cases and 26,357
instance cases**, including cases that the selected profile cannot check.
These are test cases, not a count of XSD features or unique schema documents.

The harness follows test-set references, groups, schema tests, and instance
tests. A case ID is `set/group/test`; baseline keys add engine and kind:
`engine:set/group/test:schema` or `engine:set/group/test:instance`.
Non-applicable cases remain in the emitted inventory and outcome totals;
they do not enter `pass + fail`.

### Profiles and expectations

| Profile | Claimed version tokens | pyxsd compilation | Full-XSTS xmlschema class |
| --- | --- | --- | --- |
| `xsd11` (default) | `1.1` only | `xsd_version="1.1"` | `XMLSchema11` |
| `xsd10` | `1.0` only | `xsd_version="1.0"` | `XMLSchema10` |

The selection rules are:

1. At each test-set, group, and test level, an absent version list imposes no
   restriction. Tokens within a list are a **disjunction**: any claimed token
   suffices. Each enclosing level must also apply.
2. On `expected/@version`, tokens are a **conjunction**: the profile must claim
   every listed token. Exactly one matching version-tagged expectation takes
   precedence over an unversioned expectation.
3. If no tagged expectation matches, exactly one unversioned expectation is the
   fallback. If only unmatched tagged expectations exist, the case is
   `not-applicable`. Missing expectations or ambiguous matches are
   `metadata-error`, not guessed verdicts.
4. `valid` means expected valid; `invalid` and `invalid-latent` mean expected
   invalid. Other values, including `notKnown`, `indeterminate`,
   `implementation-defined`, and `implementation-dependent`, are non-Boolean
   and therefore `not-checkable` when the adapter otherwise completes.

The profiles do not claim additional dimensions such as Unicode-version
tokens or `full-xpath-in-CTA`. A case gated exclusively to an unclaimed
dimension is `not-applicable`, not an omitted test. XML 1.0/1.1 parsing and
XSD 1.0/1.1 processor modes are independent choices; see
{doc}`xsd-validation`.

## What the adapters execute

The pyxsd adapter uses `ParseModes.NAMESPACED` and compiles with the selected
profile's matching `xsd_version`. A schema case observes schema-phase
error-severity findings. An instance case compiles its group schema and
observes instance-phase error-severity findings after binding. Warnings alone
do not make either phase invalid. A `PyXSDError` supplies an invalid verdict
for the affected phase; an unexpected exception is a harness-level `error`,
not evidence of invalidity.

An instance verdict requires a successfully compiled schema. If the group
schema fails to compile, the instance result is `adapter-gap`, not a false
accept or false reject. The separate schema case still tests schema legality.
Malformed or unreadable instance input is treated as an invalid instance when
the supplied schema compiles; the adapter probes the schema separately when
necessary to distinguish those phases. See {doc}`validation` for the reports
that the adapter reduces to these verdicts.

### Schema composition and synthesized context

- **One schema document:** use its path directly.
- **Several schema documents:** write an otherwise-empty driver schema that
  imports namespaced principal documents and includes no-target-namespace
  documents, preserving their listed order. Documents already composed by
  another listed member through include/import/redefine/override are not
  loaded twice. A composition cycle uses its first-listed member as the load
  representative. Absolute locations preserve each source document's own
  relative-reference base.
- **No supplied schema:** the reporting runner and PR gate enable pyxsd's
  permissive-schema synthesis for instance-only groups. The wrapper normally
  declares the instance root as `xs:anyType` in its namespace, allowing
  `xsi:type` and built-in datatypes to be checked. If a resolvable instance
  schema hint already supplies that root declaration, the wrapper omits it to
  avoid a duplicate; hinted-schema compilation failures are not hidden.
  Synthesis is an adapter approximation, not an extra corpus schema case.
  The xmlschema adapter does not enable synthesis, so a no-schema group is
  an `adapter-gap` for that engine.

For pyxsd instance runs, schema-location hints are resolved relative to the
instance and forwarded to compilation. If an advisory hint collides with a
supplied schema that already declares the root, the adapter can retry a
temporary copy without those hints. It does not strip hints from synthesized
contexts. Missing resources or unrepresentable bundles can prevent adapter
execution; they are not silently discarded from the report.

Synthetic loading context is not an authored import in another document.
pyxsd audits cross-namespace XSD component references against the referencing
document's own direct imports and effective target namespace, with built-in
vocabulary exemptions. Neither a driver's import nor a caller mapping or
instance hint satisfies that document's import duty; a missing import is
`import-required`. Wildcard `notQName` is intentionally not audited;
annotation payloads, foreign markup, QName-valued defaults/fixed values, and
XPath expressions are also excluded. A reference that resolves to no namespace
is allowed regardless of authored imports, matching pyxsd's lenient bare-name
lookup, so no-namespace enforcement is incomplete. The audit leaves global
component resolution unchanged, rather than isolating each document's
component table.

### Time budgets

The reporting CLI defaults to `--timeout 60`; CI uses `--timeout 180` for
slower or loaded runners. Workers run batches in separate processes with
separate scratch directories. Driver operations use wall-clock `SIGALRM`
timers where available; this is not a process-killing sandbox or a portable
hard limit on all setup work. Platforms without `SIGALRM` skip that timer.
The xmlschema adapter times compilation and instance checking separately.

A reported `timeout` supplies no validity verdict. It remains outside the
checked denominator and visible in the outcome totals. Investigate a
budget crossing and record the timeout, machine, and worker count; do not
silently change the budget to improve agreement. An unexpected error likewise
does not become a verdict.

## Outcome definitions

Every case has one outcome per selected engine:

| Outcome | Meaning | In checked denominator? |
| --- | --- | --- |
| `pass` | Observed Boolean verdict matches the selected corpus expectation. | Yes |
| `fail` | Observed Boolean verdict contradicts that expectation. | Yes |
| `not-applicable` | Version-token or expectation selection excludes the case for this profile. | No |
| `not-checkable` | Selected expectation is non-Boolean; it is not forced into valid/invalid. | No |
| `adapter-gap` | Adapter cannot drive the case, for example an unusable schema context or unsupported bundle. | No |
| `metadata-error` | Catalogue expectations are missing or ambiguous. | No |
| `timeout` | Driver reports that the operation exceeded its time budget. | No |
| `error` | Unexpected engine/harness exception, missing instance file, or absent required verdict. | No |

Metadata errors are resolved before execution, then applicability. For an
applicable case, adapter gaps, reported timeouts, and errors take precedence
over expectation comparison. Only a completed, checkable Boolean comparison
produces `pass` or `fail`.

A **false accept** is an observed valid verdict with an invalid expectation;
a **false reject** is an observed invalid verdict with a valid expectation.
These directions come from the selected expectation and observed verdict,
never from a disposition or another engine's answer.

## Interpreting residual failures

The generated tables at the end of this page keep three facts separate:

1. **Observed outcome:** the raw baseline `pass`, `fail`, or non-verdict outcome.
2. **Cross-profile expectation relation:** how the corpus's selected expectations
   relate under XSD 1.0 and 1.1, independent of observed outcomes.
3. **Disposition:** reviewed explanatory metadata, such as an implementation
   gap, version-switch candidate, platform limit, queried corpus, spec-correct
   divergence, or adapter limitation. A case without reviewed classification
   remains visibly `unclassified`.

The generated cross-profile table separates intentional corpus version splits
from same-expectation and reverse-split cases. An intentional split expects
invalid in 1.0 and valid in 1.1 and is a candidate for version-specific
implementation work. A same-expectation case cannot inherit that label merely
because its observed outcomes differ. A reverse split needs its selected 1.0
verdict. Every case in these views remains a raw XSD 1.0 failure while it is
listed.

The generated census lists explicit case IDs, classification counts, and
unclassified cases. Disposition validation rejects nonexistent cases,
duplicate claims, missing baseline results, and unsupported categories. It
accepts a case whose outcome has improved; that case leaves the active census
without a disposition edit. Classification neither changes applicability nor
removes failures from `pass + fail`. Regenerate these tables after every
reviewed baseline update so the published counts remain current.

### Type-only document roots

The type-only-root cases define named types but no global element declaration
matching the document root. Their `xsi:type` supplies a governing
type. [XSD 1.1 Structures §3.3.4.6][root-assessment] permits strict assessment
against a known governing type even when there is no governing element
declaration. This is not permission to accept every undeclared root.

The residual mechanism was an `unknown-root` error before pyxsd's existing
declared-root `xsi:type` dispatch could run, producing false rejects.
Supporting this case shape requires type-only root assessment and binding
without inventing element-declaration properties such as nillability or
defaults. Ordinary `xsi:type` handling on declared roots is a different path.
The generated census shows any cases that still fail in either profile.

### DTD-dependent ENTITY and ENTITIES values

[`xs:ENTITY`][entity] values must name unparsed entities declared in the
instance document's DTD; [`xs:ENTITIES`][entities] is a nonempty list of those
values. An `NCName` lexical check alone cannot establish that requirement.
The earlier false accepts came from missing declaration information, not
corpus errors. pyxsd now lazily collects unparsed-entity declarations from
the instance prolog/internal subset when the compiled schema can validate
these types, including derived types and defaults. Complete evidence rejects
undeclared names; no DOCTYPE means an empty, complete set. Unrelated schemas
skip the declaration-collection parse. An unparsed entity declaration names
an external resource, but checking its name does not fetch it.

External subsets and external parameter entities remain unfetched and make
the evidence incomplete: missing names stay lexical-only, with one advisory
when a declaration check is skipped. Internal parameter entities are expanded,
so declarations they carry are collected. Caller-supplied trees without pyxsd
source provenance, custom-policy parser results, and non-seekable instance
streams passed to top-level `pyxsd.parse()`
remain lexical-only too, even with an explicit schema. Compiled `Schema.parse()`
supports prolog replay for those streams. `Document.revalidate()` carries the
original parse's declaration evidence across the serialized reparse, so a
declared value stays valid; a document parsed without evidence stays
lexical-only. See {doc}`validation` for provenance and reread limits.
Collection preserves security handlers and the default, protected, and strict
profiles. This is neither DTD validation nor a change to
entity expansion or parser hardening; see {doc}`xsd-validation` and
{doc}`xml-security`. Resolved cases leave the generated active census
automatically; remaining limitations are not claimed fixed.

### Other reviewed mechanisms

Pattern-facet compilation now uses the selected processor version's regex
grammar, rejecting XSD 1.1-only forms in 1.0 mode. Direct
`pyxsd.facets.compile_pattern()` calls still default to 1.1. Compare the
[1.0][regex10] and [1.1][regex11] definitions; this fixes the reviewed grammar
version-switch mechanism, not every regex disagreement. The queried
astral-plane regex cases link to [W3C bug 4113][bug4113]; challenged metadata
is not permission to rewrite
its expected verdict. The XML 1.1 cases reflect the Expat lexer limitation,
independent of the XSD processor mode. Each mechanism keeps its recorded raw
outcome; agreement by another engine does not settle a specification dispute.

Direct-import enforcement likewise does not fix adjacent composition defects.
Cross-document duplicate globals, redefine/override merge and restriction
gaps, import-target namespace mismatch (`schF6`), and no-namespace type binding
(`schF2.v`, `schG2.v`) remain limitations. The generated census below is the
source for current residual case IDs and counts, rather than a fixed total
in this prose.

## Why extra-suite.xml is separate

Upstream's [`extra-suite.xml`][extra-suite] retains tests removed from the main
suite. At the pinned revision it emits 215 case IDs: 92 duplicate the main
introspection set, and 123 are distinct historical `precisionDecimal` cases
(54 schema and 69 instance). Those unique cases are not XSD 1.0 cases; 122
apply under the current XSD 1.1 token rules.

`precisionDecimal` was removed before the final XSD 1.1 Recommendation and is
described separately in a [W3C Working Group Note][precision-decimal] as an
implementation-defined datatype, not a required built-in type. These historical
cases are outside every headline denominator. Their omission is neither deferred
main-suite testing nor missing main-suite coverage. They stay optional unless
a separate historical-compatibility goal emerges, with a separate report and
no combined rate. The current reporting CLI selects `suite.xml`; it does not
offer an `--suite-file extra-suite.xml` option.

## Two distinct xmlschema roles

Here, **oracle means a differential signal, not normative truth**. The XSD
Recommendations and case-specific specification analysis remain the authority.

### Curated manifest differential oracle

`tests/test_oracle.py` compares pyxsd and xmlschema over 390 curated manifest
cases, including 184 with instances and 122 marked XSD 1.1. It compares schema
verdicts and, when both are available, instance verdicts. Required CI enables
it with `PYXSD_RUN_ORACLE=1`. Only manifest cases marked `xsd11` use
`XMLSchema11`; the others use xmlschema's XSD 1.0 class.

Fourteen case-specific divergences have written reasons and are skipped.
Unexpected exceptions on either side are harness failures, never agreement.
This suite detects unexpected implementation disagreement; it does not prove
that either implementation satisfies the specification.

### Full-XSTS comparison engine

The XSTS runner can instead score xmlschema against the same main-suite
expectations and profile rules as pyxsd, with its own adapter outcomes. The
XSD 1.1 baseline records both engines; the XSD 1.0 baseline records pyxsd only.
An xmlschema disagreement with the corpus is an oracle-erratum candidate,
not proof the corpus is wrong. Different adapter gaps also mean different
checked denominators, shown explicitly below.

The PR gate checks recorded XSTS outcomes against the baseline; it does not
require pyxsd to agree with xmlschema's verdict on every case. The generated
engine columns are scoped measurements, not an engine ranking.

See {doc}`comparison-xmlschema` for Python API and feature tradeoffs, and
{doc}`comparison-libxml2` for engine scope and a separately dated preliminary
XSD 1.0 probe. That probe is not part of the generated pyxsd headline.

## Reproducing reports and baseline gates

Run from the repository root after installing development dependencies and
initializing the corpus submodule. Use a clean bytecode cache for measurements
to avoid stale `.pyc` files after same-length source edits:

```bash
git submodule update --init tests/xsts/corpus
export PYTHONPYCACHEPREFIX=$(mktemp -d)

# Exploratory slice: not a full-profile measurement.
uv run python tests/report_xsts.py --profile xsd11 --limit 500

# Full comparison with every baseline change surfaced.
uv run python tests/report_xsts.py --profile xsd11 --engine both --timeout 180 \
    --baseline tests/xsts/baseline-xsd11.toml

# Dispatch-style regression gate, observing pyxsd only.
uv run python tests/report_xsts.py --profile xsd10 --engine pyxsd --timeout 180 \
    --baseline tests/xsts/baseline-xsd10.toml --enforce

# Check that committed documentation matches its source data; no engine run.
uv run python tests/report_xsts_docs.py --check
```

- **Ordinary local report:** without a baseline, reports current outcomes and
  returns zero even when known failures exist. `--filter`, `--limit`, and
  `--list-only` support exploration; a slice is not the headline population.
- **Baseline diff:** `--baseline` reports changed, new, and removed keys and
  exits `1` on any difference, including improvements and keys absent from a
  partial run. This is stricter than a regression-only gate.
- **Enforcement:** `--enforce` requires `--baseline`. It exits `2` if a recorded
  pass no longer passes, or a newly observed key is `fail`, `adapter-gap`,
  `timeout`, or `error`; otherwise it exits zero. It ignores unobserved keys,
  improvements, and changes among existing non-pass outcomes. New
  `metadata-error` results are reported but do not themselves trigger this
  enforcement rule. A green enforcement run is not an all-pass claim.
- **Required XSD 1.1 PR gate:** `tests/test_xsts_suite.py` runs with
  `PYXSD_RUN_XSTS=1` and `PYXSD_XSTS_TIMEOUT=180` after the coverage run in CI.
  It executes both engines and asserts an exact match to the XSD 1.1 baseline,
  including improvements. This is not the looser CLI `--enforce` rule.
- **Dispatch-only workflow:** XSD 1.0 and on-demand XSD 1.1 reports/slices use
  `.github/workflows/xsts.yml`. Choose either profile or both and an optional
  limit. Each uses `--engine pyxsd --enforce --timeout 180` against its own
  baseline and uploads JSON; “both” means separate profiles, not a combined rate.

Review baseline differences before accepting them. Regenerate XSD 1.1 with
`--engine both` so the recorded xmlschema entries are retained; regenerate
XSD 1.0 with `--engine pyxsd`. Exact commands and contributor details are in
the [harness README][harness-readme].

JSON reports carry run provenance, full per-engine outcomes, kind splits,
failure directions, and baseline changes when a baseline is supplied. Run
metadata records the suite file, profile, completeness, case count, revisions,
versions, Python/platform, timeout, jobs, command, and completion timestamp.
Filtered or limited runs record `complete = false`.

The tables below are generated from **reviewed checked-in baselines**, not a
live run. Per-case baseline reviews do not establish that every entry came
from a new full run. Full-run metadata, when present, retains that run's
provenance; absent provenance is explicitly reported as not recorded. The
documentation generator never substitutes its own wall-clock timestamp.

## Measurement limits

The [upstream README][upstream-readme] limits XSTS to top-level validity
outcomes. These results establish neither W3C certification nor complete XSD
feature or interaction coverage, complete PSVI, diagnostic quality, API
completeness, performance, security, or correctness on untested inputs.
Parser policy, adapters, version selection, and non-verdict outcomes define
the measurement's scope. See {doc}`xsd-validation` for the concepts and
{doc}`xml-security` for handling untrusted XML.

```{include} generated/xsts-results.md
:start-after: <!-- generated by: uv run python tests/report_xsts_docs.py -->
```

[corpus]: https://github.com/w3c/xsdtests/tree/7bc3365c652a322f3d762021b3879eb92dae7e30
[main-suite]: https://github.com/w3c/xsdtests/blob/7bc3365c652a322f3d762021b3879eb92dae7e30/suite.xml
[upstream-readme]: https://github.com/w3c/xsdtests/blob/7bc3365c652a322f3d762021b3879eb92dae7e30/README.md#running-the-tests
[extra-suite]: https://github.com/w3c/xsdtests/blob/7bc3365c652a322f3d762021b3879eb92dae7e30/extra-suite.xml
[harness-readme]: https://github.com/knorby/pyxsd/blob/develop/tests/xsts/README.md
[root-assessment]: https://www.w3.org/TR/xmlschema11-1/structures.html#cvc-assess-elt
[entity]: https://www.w3.org/TR/xmlschema11-2/#ENTITY
[entities]: https://www.w3.org/TR/xmlschema11-2/#ENTITIES
[regex10]: https://www.w3.org/TR/xmlschema-2/#regexs
[regex11]: https://www.w3.org/TR/xmlschema11-2/#regexs
[bug4113]: https://www.w3.org/Bugs/Public/show_bug.cgi?id=4113
[precision-decimal]: https://www.w3.org/TR/xsd-precisionDecimal/
