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

## Writers

`XmlTreeWriter(root, output)` serializes any tree of nodes shaped as above.
Transform developers commonly use `Transform.makeElemObj(name)` to mint a
fresh node with this exact structure, and `makeCommentElem(text)` for
comments.

## Generated classes

Class-level access to descriptors is intentional: `item_cls.name` returns
the *descriptor*, not the metadata, and the helpful `__getattr__` on
generated classes raises an `AttributeError` listing the declared elements
and attributes when you misspell something. Instance-level access goes
through the descriptors, which validate types on assignment.
