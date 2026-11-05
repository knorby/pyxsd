(pyxsd-documentation)=

# pyxsd: XML Schema validation and Python bindings

```{eval-rst}
.. meta::
   :description: Validate XML against XSD in Python, query and edit bound objects, inspect validation reports, and export records with pyxsd.
```

pyxsd validates XML against XSD and binds documents to Python objects.
Start with {doc}`quickstart` to parse, query, edit, and save a document with
supplied inputs. For a runnable custom transform, follow {doc}`demo`.

This site follows the `develop` branch and may include unreleased behavior.
Check `pyxsd --version`; for release-specific documentation sources, select
the matching tag in the [repository](https://github.com/knorby/pyxsd/tags).

## Choose a task

- Inspect errors and their source occurrences: {doc}`validation`.
- Understand namespaces and retained data: {doc}`binding` and {doc}`data-model`.
- Process several files: {doc}`batch`.
- Export Pydantic models, Arrow tables, or Parquet: {doc}`integrations`.
- Find an application workflow: {doc}`data-workflows` or {doc}`rendering-examples`.
- Work through an MCP client: {doc}`mcp`.

Read {doc}`supported` for XSD coverage and {doc}`xml-security` before
processing untrusted input. Validation, binding retention, and parser
protection are separate choices.

```{toctree}
:maxdepth: 1
:caption: Start here

quickstart
demo
comparison-xmlschema
comparison-libxml2
```

```{toctree}
:maxdepth: 1
:caption: Work with documents

binding
schema-resolution
data-model
validation
transforms/index
batch
extensions
stubs
```

```{toctree}
:maxdepth: 1
:caption: Connect tools and examples

integrations
data-workflows
rendering-examples
mcp
mcp-deployment
skills
```

```{toctree}
:maxdepth: 1
:caption: Reference and limits

api
cli
supported
xml-security
```

```{toctree}
:maxdepth: 1
:caption: Understand and contribute

xsd-validation
xsts
architecture
contributing
migration-1.0
history/index
```

```{toctree}
:maxdepth: 1
:caption: Project

License <https://github.com/knorby/pyxsd/blob/develop/LICENSE>
Source <https://github.com/knorby/pyxsd>
```

## Indices and tables

- {ref}`genindex`
- {ref}`search`
