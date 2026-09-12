# Contributing

Thanks for your interest in pyxsd!

## Reporting issues

Please [open a GitHub issue](https://github.com/knorby/pyxsd/issues) for
bugs and feature requests. Include the schema and instance document (or a
minimal reproduction), the pyxsd version (`pyxsd --version`), and the
Python version. Bug reports alone are not contributions under the terms
below — but they are greatly appreciated.

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

`pre-commit install` sets up the repo's lint/secret hooks. Python 3.11 is
the floor; CI tests 3.11–3.14.

## Pull requests

1. Fork, make a feature branch, keep changes focused.
2. Tests, type hints, and lint must pass (the same gates CI runs).
3. Public-facing behavior changes should include tests and, where
   applicable, a note in the documentation (schema features belong in
   {doc}`supported` and the conformance corpus).
4. Every commit in the history should build and pass the suite.

## Contribution terms

By submitting a change to this repository (pull request, patch, or other
means) you agree that your contribution is provided under the project's
BSD 3-Clause license, and you confirm you have the right to submit it
under those terms. The project's BSD license, including its ORNL
provenance note, is preserved in the
[LICENSE](https://github.com/knorby/pyxsd/blob/develop/LICENSE) file.

## Project direction

The 1.0 roadmap (XSD coverage tiers, conformance corpus, docs) has been
delivered; ongoing direction is tracked in the issue tracker and
{file}`CHANGELOG.md`. Working design notes are kept outside the published
docs tree. The 0.1 TODO list is of historical interest only — most of it
(Python 3, test suite, `metaclass`-style construction) is now done; the
regex machinery it called for backs built-in lexical checking, but
facets declared on user-defined simpleTypes are still not enforced, as
{doc}`supported` records.
