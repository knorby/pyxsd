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
uv run pytest --cov          # full suite with coverage
uvx ruff@0.16.6 check .      # lint
uvx ruff@0.16.6 format --check .
uv run mypy src/pyxsd        # type check
uv run python tests/report_conformance.py  # conformance report
```

`pre-commit install` runs the hooks on every commit: ruff lint and
format, file hygiene (whitespace, line endings, TOML/YAML validity,
large-file and merge-conflict checks), shellcheck, and secret detection.
They catch locally what CI would catch after you push, plus a few checks
CI does not run at all. The gitleaks hook needs its binary on PATH and
the trufflehog hook builds with the Go toolchain on first run. To run
every hook without installing the git hooks:
`uv run pre-commit run --all-files`.
Python 3.11 is the floor; CI tests 3.11–3.14.

## Pull requests

1. For a behavior change or new feature, open an issue first and keep
   the pull request focused on that one change.
2. Fork, branch from `develop`, and keep the diff minimal.
3. Pull requests must pass the same gates CI runs: ruff lint and format,
   mypy, and the full test suite.
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
(`MAJOR.MINOR.MICRO`). Maintainers cut a release by dating the
changelog's unreleased section and pushing a `vMAJOR.MINOR.MICRO` tag;
the tag-triggered workflow builds the distributions, publishes them to
PyPI, and creates the GitHub release with the artifacts attached.

## Contribution terms

By submitting a change to this repository (pull request, patch, or other
means) you agree that your contribution is provided under the project's
BSD 3-Clause license, and you confirm you have the right to submit it
under those terms. The project's BSD license, including its ORNL
provenance note, is preserved in the
[LICENSE](https://github.com/knorby/pyxsd/blob/develop/LICENSE) file.
