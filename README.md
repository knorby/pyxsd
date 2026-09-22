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

- **Minimal dependencies** — one small, pure-Python package (`elementpath`)
  for XSD regular expressions and XPath queries; no compiled extensions
- **Schema-compiled classes** — your schema becomes real Python classes;
  `xs:extension` becomes real subclassing
- **XSD 1.1 processor** — with an optional XSD 1.0 mode
  (`Schema.compile(xsd, xsd_version="1.0")` / `--xsd-version 1.0`) that
  applies the 1.0 vocabulary gate and semantic differences
- **Lax validation** — bad documents still build a tree; every issue is a
  code-tagged entry in a `ValidationReport` (`--strict` and
  `require_valid()` make it a CI failure)
- **Query and export** — `Document.xpath` / `find` / `findall` return the
  original bound nodes, and `Document.to_dict` / `to_json` export plain
  Python data (also as the `ToDict` transform)
- **Transform pipeline** — apply plain callables or `Transform` classes to
  the bound tree (`document.transform(fn)`); chain them on the CLI
  (`PrintData() > PrintData()`)
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
import pyxsd

schema = pyxsd.Schema.compile("inventory.xsd")
schema.require_valid()

document = schema.parse("inventory.xml")
document.require_valid()


def normalize_units(root): ...


updated = document.transform(normalize_units)
updated.write("normalized.xml")
```

`Schema.compile` builds the schema once and generates its Python classes;
`schema.parse` binds an instance into a `Document` whose `report` holds
every issue found. A transform is any callable taking the tree root —
see the [quickstart](https://pyxsd.knorby.com/quickstart.html) and the
[full documentation](https://pyxsd.knorby.com/) for more.

## What it validates

All 45 XSD 1.0 built-in types with lexical validation; sequence/choice/all
content models; groups and attributeGroups (with refs); wildcards;
unions (including inline members); substitution groups; element refs;
`xsi:type` dispatch; `xsi:nil`; default/fixed; abstract/final; include /
import / redefine; `key`/`unique`/`keyref` with an XPath subset. Opt-in
namespace-aware validation (`--namespaces strict` / `ParseModes.NAMESPACED`)
handles `targetNamespace`, form defaults, cross-namespace imports, wildcard
namespace/`processContents`, and QName values. Facets on user-defined
simpleTypes are enforced (enumeration, pattern, length family, bounds,
digits, and whiteSpace), including values bound through `simpleContent`
complex types. Known
gaps are tabulated in the
[supported-features page](https://pyxsd.knorby.com/supported.html).

Backed by the W3C XML Schema Test Suite (xsdtests): pyxsd passes **99.78%**
of the XSD 1.1 profile and **99.61%** of the XSD 1.0 profile. For context,
the `xmlschema` library — the standard Python library in this space, and
pyxsd's conformance oracle — passes 99.75% (1.1) and 99.77% (1.0), but
declines (does not attempt) substantially more cases, so the percentages
are not directly comparable. Residual gaps are tabulated on the
[supported-features page](https://pyxsd.knorby.com/supported.html).

## Status

pyxsd was written at Oak Ridge National Laboratory in 2006 (see
[history](https://pyxsd.knorby.com/history/origins.html)),
abandoned around 2008, and revived as a Python 3 project in 2026.
Version 1.0.0 is the first release of the modernized library.

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
