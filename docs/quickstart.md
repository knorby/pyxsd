# Quickstart

pyxsd maps an XML document into a Python object tree according to its XML
Schema (XSD), reports validation issues, runs user-defined *transforms*, and
writes the tree back out as XML. Its only runtime dependency is
[elementpath](https://pypi.org/project/elementpath/) — pure Python, no
compiled extensions.

## Requirements

- Python 3.11 or newer (3.11–3.14 tested)
- One pure-Python dependency (`elementpath`, for XSD regular expressions)

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

Two objects do the work: a compiled {class}`~pyxsd.schema.Schema` and the
{class}`~pyxsd.document.Document` instances it produces. Compiling the
schema builds the Python classes; each parse binds one instance document:

```python
import pyxsd

schema = pyxsd.Schema.compile("inventory.xsd")
schema.require_valid()  # raises pyxsd.ValidationError if the schema is bad

document = schema.parse("inventory.xml")
document.require_valid()  # raises pyxsd.ValidationError if the document is bad
```

A failed check raises {class}`~pyxsd.exceptions.ValidationError`, whose
`report` attribute holds every issue found so far. Without it, the
document keeps going — pyxsd is a *lax* validator — and the report is
there when you want it:

```python
if document.report.has_errors:
    for issue in document.report.issues:
        print(issue.format())
```

`document.root` is the parsed tree's root instance. Transforms are plain
callables that take the root; one returning a tree root comes back as a
new `Document` you can write out:

```python
def normalize_units(root): ...


updated = document.transform(normalize_units)
updated.write("normalized.xml")
```

A transform that changes the tree's shape leaves the report describing
the tree as it was parsed; call `updated.revalidate()` to re-parse the
serialized tree and get a fresh report for its current shape. The tree
walks with `pyxsd.tree.iter_tree` or the `walk`/visitor helpers in
{doc}`transforms/class`; the node shape is documented in {doc}`data-model`.

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
