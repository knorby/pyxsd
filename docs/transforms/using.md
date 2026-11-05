# Using transforms

A transform is a callable that receives the bound tree root. It can edit that
tree, return a different root, or return a non-XML result such as a dictionary.

## Library use

This complete example uppercases the text of a note and writes checked XML:

```python
from io import StringIO

from pyxsd import Schema

schema = Schema.compile(
    StringIO("""
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="note"><xs:complexType><xs:sequence>
    <xs:element name="body" type="xs:string"/>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>
""")
)
schema.require_valid()
document = schema.parse(StringIO("<note><body>hello</body></note>"))
document.require_valid()


def uppercase(root):
    root._children_[0]._value_ = [value.upper() for value in root._children_[0]._value_]
    return root


updated = document.transform(uppercase)
checked = updated.revalidate()
checked.require_valid()
checked.write("out.xml")
```

A bound root result is wrapped in a new `Document` against the same schema.
That wrapper shares the original report and does not copy the tree: this
example also changes `document.root`. Other results, including `None`, are
returned unchanged. An in-place transform returning `None` leaves the original
document available, but its return value cannot be used as a `Document`.

Revalidate after value or structure edits to obtain current findings.
See {doc}`../validation` for the DTD evidence retained during revalidation.

## Built-in transforms

| Transform | What it does |
| --- | --- |
| `PrintData` | Writes a readable tree to a file or stdout. |
| `ToDict` | Returns a dictionary; the CLI writes mapping results as JSON. |

To call a class-based transform from Python, instantiate it with the root,
then call that instance:

```python
from pyxsd.transforms import ToDict

data = checked.transform(lambda root: ToDict(root)())
assert data["body"] == "HELLO"
```

## Transform syntax

The CLI resolves module entry points from call strings with literal arguments:

```sh
pyxsd -i input.xml -s schema.xsd -t 'PrintData() > PrintData()'
```

The entry-point name matches its module name, ignoring case and underscores
(`PrintData` resolves to `print_data` or `printData`). pyxsd searches its
installed transforms, the working directory, then the instance directory.
Imported transform code runs with your process's permissions; literal argument
parsing is not a sandbox.

Calls run in order. A tree result becomes the next stage's document; a
non-tree result does not replace that document. A final `None` produces no
transform output, but does not stop later calls. Use `-T FILE` instead of `-t`
for one call per line:

```text
PrintData()
ToDict()
```

## Bundled application transforms

The crystallography transforms under
[`examples/legacy/`](https://github.com/knorby/pyxsd/tree/develop/examples/legacy)
are historical reading material; their original schemas and data are not
distributed. For a runnable custom transform, follow {doc}`../demo`.

See {doc}`writing` to write a function or discoverable transform class, and
{doc}`class` for visitor helpers.
