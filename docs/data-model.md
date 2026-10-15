# Data model

Every node of a pyxsd object tree — whether generated from a schema or
synthesized by a transform — exposes the same four dunder-prefixed
attributes. The double-underscore-free `_*_` naming prevents collisions with
element or attribute names in your schemas (a schema can legally declare an
element called `name`, so `name` on an instance always refers to *your*
data, never to metadata).

The shape is codified by the `pyxsd.XMLNode` runtime-checkable
`typing.Protocol`.

## Node structure

| Attribute | Type | Meaning |
| --------- | ---- | ------- |
| `_name_` | `str` | The element name as it appears in the document. |
| `_attribs_` | `dict[str, str]` | Attributes keyed by the name as it should appear in the document. Values are stored lexically; `str()` must produce the correct output. |
| `_children_` | `list` | Child instances of the same node shape, **in document order**. Empty list if the element has no children. |
| `_value_` | `list[str] \| None` | Non-element content as a list of strings (usually one entry), or `None` for elements with no text content. |

Notes:

- `None` as `_value_` is meaningful: it represents an element nilled with
  `xsi:nil="true"`, and the writer will emit the nil attribute.
- Attribute *defaults* applied by validation land in the instance `__dict__`
  (typed values), not in `_attribs_`, so round-trip output stays faithful.
- Repeated elements (`maxOccurs` > 1) store every occurrence in
  `_children_` in document order.
- A bound child may carry an optional, parse-produced `_tail_`: the
  character data that followed the child within a parent whose content
  admits character data (mixed content, `xs:anyType`, wildcard
  pass-through). The attribute exists only when the source held such text,
  whitespace included. It is **not** part of the `XMLNode` protocol:
  transform-made trees and hand-built nodes need no changes — a missing
  `_tail_` simply means no recorded following text.

## Writers

`XmlTreeWriter(root, output)` serializes any tree of nodes shaped as above.
Transform developers commonly use `Transform.makeElemObj(name)` to mint a
fresh node with this exact structure, and `makeCommentElem(text)` for
comments.

## Dict and JSON export

`Document.to_dict()` exports the bound tree as plain Python data, and
`Document.to_json()` encodes that with the standard `json` module. The
shipped `ToDict` transform exposes the same codec to `Document.transform`
and the CLI (`--transform ToDict()`), which renders a dict result as
JSON.

| Input | Dict key/value |
| ----- | -------------- |
| Child element | Key = the child name in Clark notation when namespace-qualified, otherwise the local name. |
| Repeated child | A list of values (a single occurrence is the value itself). |
| `always_list=True` | Every child value is a list, even a single occurrence. |
| Attribute | Key = `"@"` + the document attribute name. |
| Element text | The scalar value for a text-only element; the `"$"` key when the element also has attributes or children. |
| Nilled element | `None`. |
| `typed=False` | Lexical text instead of typed Python values. |

```python
doc.to_dict()  # {"@a": 1, "item": [1, 2], "note": "hi"}
doc.to_dict(typed=False)
doc.to_json(indent=2)
```

The export reflects post-parse descriptor assignment (the write-through
behaviour above), because it reads the same lexical containers the writer
serializes. For an element whose content admits character data (mixed
content, `xs:anyType`, wildcard pass-through), `$` holds all of the
element's recorded character data in document order: the verbatim leading
text followed by every child's `_tail_`, concatenated. The dict shape
cannot represent text/element interleaving, so `to_string()` and `xpath()`
are the faithful mixed-content views. The remaining known loss matches the
node model: a wildcard attribute has only its lexical form.

## XPath projection

`Document.xpath()`, `find()`, and `findall()` evaluate over a projection
of the bound model: an element's `text` mirrors its `_value_` and a
child's `tail` mirrors its `_tail_`, so mixed character data is queryable
in document order. The projection is **document-faithful, not
source-XPath-exact**: for element-only content, recorded layout whitespace
(the element tails between block children) is part of the projection's
text, so a text-valued query such as `string(/a)` includes it. Comment
nodes are not projected, and an element with `_value_ is None` projects
`text=None`.

The Arrow/Pydantic dataset integrations are a different, value-oriented
boundary: their row text projection (`integrations/_values.py`) reads
`_value_` only, so it does not include child `_tail_` values for mixed
content (mixed content is outside the integrations' supported subset).
That is a documented boundary, not a claim of lossless mixed text — use
`to_string()` or `xpath()` when full mixed character data is needed.

## Generated classes

Class-level access to descriptors is intentional: `item_cls.name` returns
the *descriptor*, not the metadata, and the helpful `__getattr__` on
generated classes raises an `AttributeError` listing the declared elements
and attributes when you misspell something. Instance-level access goes
through the descriptors, which validate types on assignment.

Assignment is not validation-only: assigning a value to an element or
attribute descriptor also writes the value's XSD lexical form through to
the container the writer serializes, so a later `to_string()`, `write()`,
or `revalidate()` reflects the assignment. Repeated element descriptors
are the exception — a repeated element has no single unambiguous target
node, so assignment updates the instance dictionary only and the write-out
is unchanged. Assigning a *bound node* directly (as internal binding does)
is not a user-facing operation and does not go through this path.

Elements and attributes now share one assignment policy. A plain Python
value (a string, int, float, bool, or `decimal.Decimal`) assigned to a
simple-typed element is coerced through the declared datatype, exactly as
attribute assignment already coerced plain values. A value that fails the
declared type's validation is recorded in the report during a parse and
logged outside one, and it is stored as given without touching the
serialized tree — assignment never raises for a bad *lexical* value.
Assigning a datatype instance of the wrong type, or an arbitrary object,
still raises `TypeError`.

### Name collisions: elements and attributes with the same name

A complex type can legally declare an element and an attribute with the
same name (`<xs:element name="code"/>` and `<xs:attribute name="code"/>`
in one type). Both declarations stay fully active — each is matched,
validated, and bound — but they cannot share one Python attribute, so
the accessors are disambiguated:

- The **attribute keeps the natural name** (`item.code`).
- The **element's accessor gains an `_element` suffix**
  (`item.code_element`). If a real declaration already occupies that
  name too, a numeric suffix is used (`code_element_2`, `code_element_3`,
  …).

Only the Python accessor name changes. Child order in `_children_`,
validation, identity constraints, and round-trip output are identical to
a non-colliding schema.
