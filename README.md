# pyxsd

[![CI](https://github.com/knorby/pyxsd/actions/workflows/ci.yml/badge.svg)](https://github.com/knorby/pyxsd/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/knorby/pyxsd/branch/develop/graph/badge.svg)](https://codecov.io/gh/knorby/pyxsd)
[![PyPI](https://img.shields.io/pypi/v/pyxsd)](https://pypi.org/project/pyxsd/)
[![Docs](https://img.shields.io/badge/docs-pyxsd.knorby.com-blue)](https://pyxsd.knorby.com/)
[![License](https://img.shields.io/badge/license-BSD--3--Clause-blue)](LICENSE)

pyxsd validates XML against XML Schema (XSD) and binds it to Python objects.
Compile a schema once, parse documents against it, then query, transform, or
export their data. Each document carries a structured validation report.

## Quickstart

Requires Python 3.11 or newer. Install into the environment running your code:

```sh
python -m pip install pyxsd
```

This example runs without any files or downloads:

```python
from io import StringIO

from pyxsd import Schema

schema = Schema.compile(
    StringIO("""
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="quantity" type="xs:positiveInteger"/>
</xs:schema>
""")
)
schema.require_valid()
document = schema.parse(StringIO("<quantity>42</quantity>"))
document.require_valid()
print(document.to_dict()["$"])  # 42
```

Compilation generates Python classes at runtime; no separate code-generation
step is needed. Parsing records recoverable validation errors rather than
raising them automatically. `require_valid()` raises `ValidationError` when
the report contains errors.

The [quickstart](https://pyxsd.knorby.com/quickstart.html) supplies a complete
file-based example with queries, edits, and fresh validation. The same install
also provides a CLI:

```sh
pyxsd -i inventory.xml -s inventory.xsd --namespaces strict --strict
```

Use that command with your own files or the quickstart's fixtures. `--strict`
makes validation errors fail the command; a bare run does not write XML.

## Work with your data

- [Validate and diagnose XML](https://pyxsd.knorby.com/validation.html): inspect
  issue codes, phases, and exact source occurrences.
- [Query and edit documents](https://pyxsd.knorby.com/quickstart.html#query-the-document):
  use XPath or ElementTree paths, export dictionaries/JSON, and revalidate edits.
- [Write transforms](https://pyxsd.knorby.com/transforms/writing.html): apply
  Python functions or reusable transform classes to the bound tree.
- [Process multiple documents](https://pyxsd.knorby.com/batch.html): reuse a
  compiled schema sequentially and choose whether to stop or report failures.
- [Connect Pydantic and Arrow](https://pyxsd.knorby.com/integrations.html):
  generate access models or schema-derived tables and Parquet with optional extras.
- [Use MCP tools](https://pyxsd.knorby.com/mcp.html): explore schemas and inspect
  XML diagnostics through a local stdio server. Transform execution is disabled
  by default; trusted-local previews require explicit opt-in.

For runnable applications, browse the [examples](examples/README.md): GPX
statistics, document and diagram rendering, healthcare conversion and diagnosis,
and analytical workflows. Each family documents its inputs and setup.

## Scope and limits

pyxsd supports XSD 1.1 with an optional XSD 1.0 mode. Namespace-aware binding
is opt-in (`ParseModes.NAMESPACED` or `--namespaces strict`); the default matches
local names for compatibility. See [binding modes](https://pyxsd.knorby.com/binding.html),
[supported features and gaps](https://pyxsd.knorby.com/supported.html), and
[W3C test-suite results](https://pyxsd.knorby.com/xsts.html) before choosing it
for a particular schema.

Parsing materializes the XML tree. Validation does not repair invalid input,
and the Python object tree is not a lossless source representation. Read the
[data model](https://pyxsd.knorby.com/data-model.html) for access and export limits.
For untrusted input, configure parser protections and resource limits as
described in [XML security](https://pyxsd.knorby.com/xml-security.html).

## Documentation and contributing

[Full documentation](https://pyxsd.knorby.com/) ·
[Python API](https://pyxsd.knorby.com/api.html) ·
[CLI reference](https://pyxsd.knorby.com/cli.html) ·
[Contributing](https://pyxsd.knorby.com/contributing.html) ·
[Changelog](CHANGELOG.md)

The documentation site follows `develop` and may describe unreleased behavior.
For release-specific sources, select the matching tag in this repository.

pyxsd began at Oak Ridge National Laboratory in 2006 and was revived for
Python 3 in 2026. See the [project history](https://pyxsd.knorby.com/history/origins.html).

## License

BSD 3-Clause. [LICENSE](LICENSE) includes the original ORNL provenance note.
