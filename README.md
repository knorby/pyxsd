# pyxsd

[![CI](https://github.com/knorby/pyxsd/actions/workflows/ci.yml/badge.svg)](https://github.com/knorby/pyxsd/actions/workflows/ci.yml)
[![Docs](https://github.com/knorby/pyxsd/actions/workflows/docs.yml/badge.svg)](https://github.com/knorby/pyxsd/actions/workflows/docs.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://pypi.org/project/pyxsd/)
[![PyPI](https://img.shields.io/pypi/v/pyxsd)](https://pypi.org/project/pyxsd/)
[![License](https://img.shields.io/badge/license-BSD--3--Clause-blue)](LICENSE)

**pyxsd** maps XML documents into Python object trees according to an XML
Schema (XSD), reports non-fatal validation issues, runs user-defined
*transforms*, and writes the tree back out as XML.

- **Zero runtime dependencies** — the standard library is enough
- **Schema-compiled classes** — your schema becomes real Python classes;
  `xs:extension` becomes real subclassing
- **Lax validation** — bad documents still build a tree; every issue is a
  code-tagged entry in a `ValidationReport` (`--strict` makes it a CI
  failure)
- **Transform pipeline** — chain small Python classes over the tree
  (`PrintData() > SendTreeToPyXSD() > PrintData()`)
- A niche-but-real niche: runtime XML↔Python binding *plus* a transform
  framework, without pulling in `lxml` or generating static code

## Quickstart

```bash
pip install pyxsd
```

Validate and parse from the command line:

```bash
# schema is located from the instance's schemaLocation hints
pyxsd -i inventory.xml --strict

# write the parsed tree and apply a transform
pyxsd -i inventory.xml -k -o parsed.xml -t 'PrintData()'
```

Or as a library:

```python
from pyxsd import PyXSD

parser = PyXSD(xmlFileInput="inventory.xml", xsdFile="inventory.xsd", xmlFileOutput=False)
root = parser.schemaRootInstance
for issue in parser.report.issues:
    print(issue.format())
```

See the [quickstart](https://github.com/knorby/pyxsd/blob/main/docs/quickstart.md)
and [full documentation](https://github.com/knorby/pyxsd#documentation)
for more.

## What it validates

All 45 XSD 1.0 built-in types with lexical validation; sequence/choice/all
content models; groups and attributeGroups (with refs); wildcards;
unions (including inline members); substitution groups; element refs;
`xsi:type` dispatch; `xsi:nil`; default/fixed; abstract/final; include /
import / redefine; `key`/`unique`/`keyref` with an XPath subset. Known
gaps are tabulated in the
[supported-features page](https://github.com/knorby/pyxsd/blob/main/docs/supported.md)
— notably, facets on user-defined simpleTypes are parsed but not enforced.

Backed by a 54-case conformance corpus derived from the W3C
XMLSchema1TestSuite and NIST datatype test areas.

## Status

pyxsd was written at Oak Ridge National Laboratory in 2006 (see
[history](https://github.com/knorby/pyxsd/blob/main/docs/history/origins.md)),
abandoned around 2008, and revived as a Python 3 project in 2026.
Version 1.0 is the first release of the modernized library.

## Documentation

- [Quickstart](docs/quickstart.md)
- [CLI reference](docs/cli.md)
- [Architecture](docs/architecture.md)
- [Data model](docs/data-model.md)
- [Validation and issue codes](docs/validation.md)
- [Transforms](docs/transforms/index.md)
- [Supported features](docs/supported.md)
- [API reference](docs/api.md)
- [Migrating from 0.1](docs/migration-1.0.md)
- [Project history](docs/history/origins.md)
- [Contributing](docs/contributing.md)

(Sphinx sources in `docs/`; build with `uv run sphinx-build -b html docs docs/_build/html`.)

## License

BSD 3-Clause. See [LICENSE](LICENSE) — it preserves the original ORNL
provenance note.
