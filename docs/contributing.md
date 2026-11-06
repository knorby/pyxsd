# Contributing

Thanks for your interest in pyxsd. Bug reports, feature requests, and
pull requests are all welcome. This page explains what each should
contain.

## Reporting issues

Please [open a GitHub issue](https://github.com/knorby/pyxsd/issues) for
bugs and feature requests. For bugs, include:

- a minimal reproduction: the schema and instance document, stripped to
  the smallest pair that shows the problem, ideally failing from a clean
  directory;
- the pyxsd version (`pyxsd --version`) and the Python version;
- what you expected and what happened instead.

Issue reports alone are not contributions under the license terms below,
but they are greatly appreciated.

## Development setup

```bash
git clone https://github.com/knorby/pyxsd
cd pyxsd
uv sync --group dev          # runtime + dev/test dependencies
uv run pytest --cov --cov-branch  # default suite with branch coverage
uvx ruff@0.16.6 check .      # lint
uvx ruff@0.16.6 format --check .
uv run mypy src/pyxsd        # type check
uv run python tests/report_conformance.py  # suite-inspired conformance report
uv run python -m tests.conformance.run     # real-schema conformance + benchmarks
PYXSD_RUN_XSTS=1 uv run pytest tests/test_xsts_suite.py  # W3C XML Schema Test Suite
```

`uv run pre-commit install` installs hooks that run on every commit: ruff lint and
format, file hygiene (whitespace, line endings, TOML/YAML validity,
large-file and merge-conflict checks), shellcheck, and secret detection.
They catch locally what CI would catch after you push, plus a few checks
CI does not run at all. The gitleaks hook needs its binary on PATH and
the trufflehog hook builds with the Go toolchain on first run. To run
every hook without installing the git hooks:
`uv run pre-commit run --all-files`.
Python 3.11 is the floor; CI tests 3.11–3.15.

### Optional corpora and validation gates

The default test run skips opt-in oracle and XSTS tests, and downloaded-schema
tests skip when their inputs are absent. A passing default run does not mean
those checks ran.

`tests/conformance/` compiles five published schemas (FHIR R5, HL7 CDA R2,
ECMA-376 WordprocessingML and SpreadsheetML, and MusicXML 4.0): fetch the
corpus once with `uv run python -m tests.conformance.fetch`, then
`uv run python -m tests.conformance.run` prints the pinned per-error-code
counts and a benchmark table. A 30 s compile tripwire runs inside the
normal suite (it skips cleanly without the corpus). Set `PYXSD_RUN_XSTS=1`
to run the W3C XML Schema Test Suite against its checked-in baseline (its
contract and baseline notes are in `tests/xsts/README.md`); the
`PYXSD_XSTS_TIMEOUT` environment variable raises the per-case budget, which
the slower CI runners need. Use `PYXSD_XSTS_TIMEOUT=180` locally to match CI
when running the full suite on a loaded machine. The `xmlschema` oracle is
enabled separately with `PYXSD_RUN_ORACLE=1`.

Coverage must meet the repository's 99% branch-coverage floor. Also inspect
`uv run coverage report --show-missing` against the lines you changed: the
overall floor does not establish patch coverage. Exercise each new condition's
branches, including failure paths, and assert the resulting behavior.

### Documentation checks

Install both groups so the same environment can build the site and run its
tests. From the repository root:

```bash
uv sync --group dev --group docs
uv run --no-sync sphinx-build -n -W --keep-going -b html docs docs/_build/html
uv run --no-sync python -m tests.docs_site docs/_build/html --base-url https://pyxsd.knorby.com/
PYXSD_DOCS_BUILD=docs/_build/html uv run --no-sync pytest tests/test_sphinx_site.py tests/test_docs_site.py
uv run --no-sync python tests/report_xsts_docs.py --check
```

The strict build checks API cross-references. The offline HTML checker checks
local files, anchors, canonical URLs, and entry-page descriptions. Site tests
also inspect the rendered copy-button configuration, API summaries, and channel
label. Point `PYXSD_DOCS_BUILD` at a freshly built directory; without it, the
real-site tests skip. Sphinx fixture tests require the docs group; the local
checker needs only Python's standard library.

Mark output-only code blocks with MyST's `:class: no-copy`; leave runnable code
and file contents copyable. Review copied console commands for prompt stripping
and preserved continuation lines. Keep API details at their existing anchors
rather than generating duplicate reference pages.

The HTML build also writes `llms.txt` and `llms-full.txt` at the site root. The
summary links to the raw MyST files in `_sources/`; the full file concatenates
those sources. Autodoc output is not expanded in the full file. Check both files
when changing source layout, page titles, or documentation URLs.

The separate **Docs links** workflow checks external URLs weekly and on manual
dispatch, retaining its linkcheck reports even on failure. External availability
does not gate pull requests. To run the same bounded check locally:

```bash
uv run --no-sync sphinx-build -W --keep-going -b linkcheck docs docs/_build/linkcheck
```

Fix references before adding exceptions. `nitpick_ignore` entries must be exact
role/target pairs with a reason; do not add regex suppressions. Add an exact URL
to `linkcheck_anchors_ignore_for_url` only after confirming its anchor is created
by JavaScript rather than present in the downloaded HTML, and explain why.
Timeouts and missing pages are failures to investigate, not reasons for a broad
ignore rule. A host that refuses every automated client may be listed exactly
in `linkcheck_ignore` with the evidence; prefer a working canonical URL or a
link without a fragile external fragment first.

Channel presentation is isolated in `docs/_templates/base.html` and
`docs/_static/docs.css`. A future site-matching template can replace those
files without changing guide content or URLs. The current label identifies
`develop` documentation and the package version; it does not claim a stable
documentation channel exists.

## Pull requests

1. For a behavior change or new feature, open an issue first and keep
   the pull request focused on that one change.
2. Fork, branch from `develop`, and keep the diff minimal.
3. Pull requests must pass the same gates CI runs: ruff lint and format,
   mypy, the full test suite, and the strict Sphinx documentation build.
4. Bug fixes come with a test that fails without the fix and passes with
   it. New public behavior comes with tests and, where applicable, a
   documentation update (schema features belong in {doc}`supported`).
5. Every commit in the history should build and pass the suite.

## Quality bar

The same bar applies whether the work was written by hand or with an AI
assistant:

- **Reproduce before fixing:** a test that fails without the change and
  passes with it is the only convincing bug report.
- **Verify claims:** run the gates locally and report real output in the
  pull request. Do not state that tests pass unless you ran them.
- **Keep the diff minimal:** no drive-by refactors, reformatting, or
  dependency bumps unrelated to the change.
- **Solve the reported problem:** no speculative options, abstractions,
  or features nobody asked for.
- **Understand every line you submit:** if an AI assistant wrote the
  code, you must be able to explain it in review. Fabricated APIs,
  invented results, and "should work" assertions are grounds for closing
  a pull request.
- **Update docs and `CHANGELOG.md`** when public behavior changes.

## Releases

pyxsd uses [semantic versioning](https://semver.org/spec/v2.0.0.html)
(`MAJOR.MINOR.MICRO`). Prepare releases on a branch from `develop`:

1. Review changes since the previous release. Complete `CHANGELOG.md`, move
   its entries into a dated version section using the intended publication
   date, and retain an empty Unreleased section. Describe compatibility with
   the previous published version, not intermediate development APIs.
2. Set `__version__` in `src/pyxsd/__init__.py` to the release version and run
   `uv lock` to refresh project metadata. Historical benchmark and XSTS
   provenance records keep the versions actually measured.
3. Run the quality gates above, plus the optional MCP and adapter tests.
   Check patch coverage as well as the overall floor. Require successful
   Python 3.11–3.15, minimum/current optional dependency, oracle, XSTS, and
   documentation checks on the exact release commit.
4. Build fresh archives with `uv build --out-dir /path/to/empty-dist`, then run
   `PYXSD_DIST_DIR=/path/to/empty-dist uv run --extra mcp pytest tests/test_distributions.py -q`.
   This extracts the source archive outside the checkout and runs its benchmark
   tests, and checks wheel skills and entry points. Also smoke-test installation
   from each archive in clean environments, including base-only and optional
   extras. The source archive includes benchmark fixtures but intentionally
   excludes `tests/xsts/corpus`: full XSTS runs require a repository checkout
   with the pinned submodule initialized.
5. Submit the preparation branch as a PR. After review, tag the verified
   release commit `vMAJOR.MINOR.MICRO` and push the tag only when publication
   is authorized. The tag must match the package version.

The tag-triggered workflow reruns core (including XSTS and oracle), MCP,
optional integration, and documentation workflows against the tagged commit.
Only after they succeed does it build and inspect distributions. Publication
uses the PyPI trusted publisher and the repository's `pypi` environment;
maintainers must configure its approval rules and PyPI trust separately.
The GitHub release is created only after successful PyPI publication, with
the dated changelog section as its release notes and both archives attached.
Finally, verify the published version, CLI entry points, and downloadable
artifacts. These steps publish packages; they do not create a stable docs
channel. The documentation site continues to follow `develop`.

## Contribution terms

By submitting a change to this repository (pull request, patch, or other
means) you agree that your contribution is provided under the project's
BSD 3-Clause license, and you confirm you have the right to submit it
under those terms. The project's BSD license, including its ORNL
provenance note, is preserved in the
[LICENSE](https://github.com/knorby/pyxsd/blob/develop/LICENSE) file.
