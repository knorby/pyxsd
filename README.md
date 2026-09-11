# pyxsd

[![CI](https://github.com/knorby/pyxsd/actions/workflows/ci.yml/badge.svg)](https://github.com/knorby/pyxsd/actions/workflows/ci.yml)
[![Docs](https://github.com/knorby/pyxsd/actions/workflows/docs.yml/badge.svg)](https://github.com/knorby/pyxsd/actions/workflows/docs.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://pypi.org/project/pyxsd/)
[![PyPI](https://img.shields.io/pypi/v/pyxsd)](https://pypi.org/project/pyxsd/)
[![Docs](https://img.shields.io/badge/docs-pyxsd.knorby.com-blue)](https://pyxsd.knorby.com/)
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

See the [quickstart](https://pyxsd.knorby.com/quickstart.html) and the
[full documentation](https://pyxsd.knorby.com/) for more.

## What it validates

All 45 XSD 1.0 built-in types with lexical validation; sequence/choice/all
content models; groups and attributeGroups (with refs); wildcards;
unions (including inline members); substitution groups; element refs;
`xsi:type` dispatch; `xsi:nil`; default/fixed; abstract/final; include /
import / redefine; `key`/`unique`/`keyref` with an XPath subset. Opt-in
namespace-aware validation (`--namespaces strict` / `ParseModes.NAMESPACED`)
handles `targetNamespace`, form defaults, cross-namespace imports, wildcard
namespace/`processContents`, and QName values. Known
gaps are tabulated in the
[supported-features page](https://pyxsd.knorby.com/supported.html)
— notably, facets on user-defined simpleTypes are parsed but not enforced.

Backed by a 65-case conformance corpus of independently authored,
suite-inspired regression cases (see the
[supported-features page](https://pyxsd.knorby.com/supported.html)).

## Status

pyxsd was written at Oak Ridge National Laboratory in 2006 (see
[history](https://pyxsd.knorby.com/history/origins.html)),
abandoned around 2008, and revived as a Python 3 project in 2026.
Version 1.0 is the first release of the modernized library.

## Documentation

Full documentation is published at **<https://pyxsd.knorby.com/>**:

- [Quickstart](https://pyxsd.knorby.com/quickstart.html)
- [CLI reference](https://pyxsd.knorby.com/cli.html)
- [Architecture](https://pyxsd.knorby.com/architecture.html)
- [Data model](https://pyxsd.knorby.com/data-model.html)
- [Validation and issue codes](https://pyxsd.knorby.com/validation.html)
- [Parse modes](https://pyxsd.knorby.com/binding.html)
- [Transforms](https://pyxsd.knorby.com/transforms/)
- [Supported features](https://pyxsd.knorby.com/supported.html)
- [API reference](https://pyxsd.knorby.com/api.html)
- [Migrating from 0.1](https://pyxsd.knorby.com/migration-1.0.html)
- [Project history](https://pyxsd.knorby.com/history/origins.html)
- [Contributing](https://pyxsd.knorby.com/contributing.html)

The Sphinx sources live in `docs/`; build them locally with
`uv run sphinx-build -b html docs docs/_build/html`.

## License

BSD 3-Clause. See [LICENSE](LICENSE) — it preserves the original ORNL
provenance note.
