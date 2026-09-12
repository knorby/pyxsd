# Quickstart

pyxsd maps an XML document into a Python object tree according to its XML
Schema (XSD), reports validation issues, runs user-defined *transforms*, and
writes the tree back out as XML. It has **no third-party runtime
dependencies** — the Python standard library is enough.

## Requirements

- Python 3.11 or newer (3.11–3.14 tested)
- No runtime dependencies

## Installation

```bash
pip install pyxsd
```

or with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install pyxsd
```

## Command line

Given a schema and an instance document:

```bash
pyxsd --inputXml inventory.xml
```

pyxsd locates the schema from the instance's `xsi:schemaLocation`-style hints
(or you can pass `-s schema.xsd`), builds the object tree, and validates it.
By default nothing is written: a bare run is a validation pass. Pass `-k` to
write the parsed tree, or give a transform with `-t`; transform output goes
to standard output by default. Use `-o FILE` (or `-o stdout`) to choose the
destination explicitly, or `-d` for the default filename
(`<input>Transformed.xml`). Useful flags:

```bash
# Validate strictly: exit 1 if the report contains errors
pyxsd -i inventory.xml -s schema.xsd --strict

# Write the parsed tree to a file, then apply a transform
pyxsd -i inventory.xml -s schema.xsd -k -o transformed.xml -t 'PrintData()'

# Chain transforms with '>'
pyxsd -i inventory.xml -t 'PrintData() > PrintData()'
```

See {doc}`cli` for the complete flag reference and {doc}`transforms/index`
for transform syntax.

## Library use

Constructing `PyXSD` runs the whole pipeline eagerly — parse, validate,
write, and transform all happen in `__init__` — so a validation-only run
looks like this:

```python
from pyxsd import PyXSD

parser = PyXSD(
    xmlFileInput="inventory.xml",
    xsdFile="schema.xsd",
    xmlFileOutput=False,  # don't write the parsed tree to disk
)

root = parser.schemaRootInstance  # the parsed tree's root instance

if parser.report.has_errors:
    for issue in parser.report.issues:
        print(issue.format())
```

With `transformOutputName` set (or `transforms` given), the transformed
tree is written to that file — or to stdout when the value is
`"stdout"`.

The root instance of the tree walks with `pyxsd.transforms.iter_tree` or
the `walk`/visitor helpers in {doc}`transforms/class`; the node shape is
documented in {doc}`data-model`.

## What pyxsd validates

pyxsd is a *lax* validator: it builds the tree even when the document is
invalid and records non-fatal issues in a {doc}`validation` report rather
than aborting. Features supported in 1.0 (built-in type lattice, sequence /
choice / all content models, groups, wildcards, substitution groups,
`xsi:type` dispatch, nil/default/fixed, identity constraints, schema
composition) and known gaps are tabulated in {doc}`supported`.

## Next steps

- {doc}`architecture` — how the ER system generates classes from your schema
- {doc}`transforms/writing` — write your own transforms
- {doc}`migration-1.0` — coming from pyxsd 0.1?
