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

A `SchemaBase` root result (as in this complex-root example) is wrapped in a
new `Document` against the same schema.
That wrapper shares the original report and does not copy the tree: this
example also changes `document.root`. Other results, including `None`, are
returned unchanged. That includes scalar roots backed by primitive datatype
classes rather than `SchemaBase`. An in-place transform returning `None` leaves the original
document available, but its return value cannot be used as a `Document`.

Revalidate after value or structure edits to obtain current findings.
See {doc}`../validation` for the DTD evidence retained during revalidation.

## Built-in transforms

| Transform | What it does |
| --- | --- |
| `PrintData` | Writes a readable tree to a file or stdout. |
| `ToDict` | Returns a dictionary; the CLI writes mapping results as JSON. |
| `ToData` | Returns JSON-safe data in a selected format; the CLI writes it as JSON. |

To call a class-based transform from Python, instantiate it with the root,
then call that instance:

```python
from pyxsd.transforms import ToDict

data = checked.transform(lambda root: ToDict(root)())
assert data["body"] == "HELLO"
```

### Export JSON-safe data with `ToData`

`ToData(root=None, *, format="pyxsd", fidelity="best-effort", typed=True,
always_list=False)` uses the same export contract as `Document.to_data()`.
It returns Python data, not encoded JSON text:

```python
from pyxsd.transforms import ToData

data = checked.transform(ToData(format="jsonml"))
assert data == ["note", ["body", "HELLO"]]
assert data == checked.to_data(format="jsonml")

# Supply a bound root, not a Document, when calling the transform directly.
assert ToData(checked.root, format="jsonml")() == data
assert ToData(format="jsonml")(checked.root) == data

# Call keywords override construction options.
assert checked.transform(ToData(format="badgerfish"), format="jsonml") == data
```

- `format` accepts `"pyxsd"` (native grouped data), `"pyxsd-json"`
  (schema-driven, root-preserving data), `"parker"`, `"badgerfish"`, or
  `"jsonml"` (nested arrays).
- `fidelity="strict"` rejects representation losses where the selected
  format cannot retain the bound data. The default `"best-effort"` uses
  that format's documented fallbacks; it does not guarantee lossless output.
  Fidelity is not XSD validation.
- `typed=True` converts bound scalars to JSON-safe values. Native
  `typed=False` keeps lexical strings; `always_list=True` wraps each native
  child value in an array. These options are native-only: `typed=False` or
  `always_list=True` with another format raises `ValueError`.

Unknown format or fidelity names also raise `ValueError`. Export reads the
current bound tree without revalidating it; a missing bound root raises
`PyXSDError`. See {doc}`../data-conversion` for the format shapes, scalar
policy, and per-format fidelity rules.

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
non-tree result does not replace that document. A `None` result does not stop
later calls; if the final call returns `None`, the CLI writes JSON `null`.
Use `-T FILE` instead of `-t`
for one call per line:

```text
PrintData()
ToDict()
```

### JSON results from the CLI

Pass `ToData` options as literal keyword arguments in the transform call:

```sh
pyxsd -i input.xml -s schema.xsd -t 'ToData(format="jsonml")'
pyxsd -i input.xml -s schema.xsd -t 'ToData(format="pyxsd-json", fidelity="strict")' -o result.json
```

The first command writes a JSON array to stdout. For the note example above,
the result is `["note", ["body", "HELLO"]]`. The second writes JSON to
`result.json`; omitting `-o` writes to stdout.

Parker can return a top-level scalar or `null`, not just an object or array:

```sh
pyxsd -i simple.xml -s schema.xsd -t 'ToData(format="parker")'
pyxsd -i nil.xml -s schema.xsd -t 'ToData(format="parker")'
```

With `simple.xml` containing `<simple>42</simple>` and `simple` declared as
`xs:int`, the first command writes `42`. With `nil.xml` containing
`<nillable xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:nil="true"/>`
and `nillable` declared as a nillable `xs:string`, the second writes `null`.

This output handling also applies to custom transforms. A final mapping,
list, string, integer, float, boolean, or `None` is serialized with
`json.dumps(..., indent=2, default=str)`; strings are quoted and booleans
become `true` or `false`. A document result writes XML. Other result types
write no transform output. `ToData` converts non-finite floats to the
JSON strings `"INF"`, `"-INF"`, and `"NaN"`; arbitrary transforms returning
non-finite Python floats instead use the serializer's non-standard
`Infinity`, `-Infinity`, and `NaN` tokens.

## Bundled application transforms

The crystallography transforms under
[`examples/legacy/`](https://github.com/knorby/pyxsd/tree/develop/examples/legacy)
are historical reading material; their original schemas and data are not
distributed. For a runnable custom transform, follow {doc}`../demo`.

See {doc}`writing` to write a function or discoverable transform class, and
{doc}`class` for visitor helpers.
