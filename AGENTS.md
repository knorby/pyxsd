# Agent guidance for pyxsd

## Example outputs carry no library markers

- Rendered artifacts (SVG, HTML, PDF, PNG, WAV, console output) must not
  contain pyxsd branding, "rendered by" comments, generator notices, or any
  other library marker — not in comments, visible text, or document/PDF
  metadata. The output belongs to the user's document.
- Docs and examples describe what a workflow does, never what technology it
  "replaces": no "XSLT replacement" / browser-XSLT framing anywhere.

## Examples organize by data source

- Each data-source family owns its source fetching, schema baseline,
  documentation, and multiple example uses.
- Servers, plots, renderers, and other uses live inside that family; do not
  create cross-family showcase applications or registries.
- Self-contained duplication between families is preferred over a shared
  example umbrella.

## Documentation matches the project, not the work stream

- A docs page named for a capability (rendering, data workflows, batch)
  covers every example with that capability, not just the ones the current
  change adds. Survey `examples/` and the existing docs before writing or
  renaming; a broader name on a narrower page is a scoping bug.
- Docs state what exists and give commands that run. Link a family README
  instead of restating it; a docs page earns its place with what only it can
  add — cross-family patterns, the boundary with the library, navigation.
- If a proposed page or section would only echo one change's output or say
  what its README already says, it is slop: extend the existing page or the
  README instead of writing it.

## Every change needs tests that cover its added lines

- Overall branch coverage has an enforced floor (`fail_under = 99` in
  `pyproject.toml`). Passing the floor does **not** prove a change is
  covered: Codecov evaluates *patch* coverage on the PR diff, and every
  uncovered added line is a defect.
- Before committing, measure the diff, not just the suite:
  `uv run pytest --cov --cov-branch`, then compare
  `uv run coverage report --show-missing` (or `htmlcov/`) against the lines
  you added. A new condition needs a test for each arm — a happy-path test
  usually executes only one.
- Cover defensive branches too: collision loops, fallback keys, error
  paths, and version-gated code each need a case that actually reaches
  them. Verify by mutation when in doubt: revert the change and confirm the
  new test fails.
- Tests must assert behavior, not merely execute a line. If a line is
  unreachable, delete it instead of contriving a test to touch it.
- New tests run in CI on every supported version (currently 3.11–3.15). A
  test that passes only on the newest interpreter or only on macOS is a CI
  failure.

## XSTS conformance

- Increasing the XSTS pass rate is a general project goal. Every durable
  improvement counts, whether intentional or incidental. When a change
  reproducibly flips a case to `pass`, update only that case's baseline
  entry in the same commit and keep every previously passing case passing.
  Follow the baseline contract in `tests/test_xsts_suite.py`; use repeated
  runs to distinguish durable improvements from machine-speed flakiness.
- Keep improvements cheap to land. After reviewing and updating the affected
  baseline entries, run `uv run python tests/report_xsts_docs.py` and commit
  the regenerated fragments with the fix. A case moving from `fail` to `pass`
  must require no disposition cleanup or edits to current-count assertions.
  Dispositions apply only to current failures; resolved cases leave the active
  report automatically. Pin reporting behavior with synthetic fixtures and
  structural invariants, not the current failure totals.

## Do not depend on the developer machine's locale or filesystem

Some jobs run under `LC_ALL=C` with `PYTHONUTF8=0`. On Linux the filesystem
encoding is then ASCII even when `PYTHONIOENCODING` keeps standard streams
UTF-8, so keep non-ASCII text in file *contents*, never in test file
*paths*. macOS uses UTF-8 for the filesystem regardless of locale, so such
a test passes locally and fails in CI.

## Concurrency

One `Schema` must not be parsed concurrently or reentrantly; concurrent
compiles and parses on separate `Schema` objects are supported. Threaded
tests must use a barrier and abort it on a worker exception (see
`tests/test_threading.py`) so a regression fails fast instead of hanging.
