# Agent guidance for pyxsd

## Every change needs tests that cover its added lines

- Overall branch coverage has an enforced floor (`fail_under = 98` in
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
