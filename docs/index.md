# pyxsd documentation

Documentation for **pyxsd**, a Python library and CLI that maps XML
documents into Python object trees according to an XML Schema (XSD). It
reports validation issues, queries and exports the bound tree (`xpath` /
`find` / `to_dict` / `to_json`), runs user-defined *transforms*, and writes
XML. The base install has one pure-Python dependency, `elementpath`. pyxsd
is an XSD 1.1 processor with an optional XSD 1.0 mode.

For explicit local input sequences, see {doc}`batch`: reuse one compiled
schema for parsing or export a new manifested Parquet dataset with the
optional Arrow adapter. To attach methods and validation checks to generated
classes, see {doc}`extensions`. The {doc}`api` covers schema introspection
and filtered stubs; {doc}`validation` covers resource policies and diagnostic
locations.

```{toctree}
:maxdepth: 2
:caption: Contents

quickstart
cli
demo
architecture
data-model
validation
batch
binding
extensions
transforms/index
supported
comparison-xmlschema
api
integrations
data-workflows
migration-1.0
history/index
contributing
```

```{toctree}
:maxdepth: 1
:caption: Project

License <https://github.com/knorby/pyxsd/blob/develop/LICENSE>
Source <https://github.com/knorby/pyxsd>
```

## Indices and tables

- {ref}`genindex`
- {ref}`modindex`
- {ref}`search`
