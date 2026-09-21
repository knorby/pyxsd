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

Assignment is not validation-only: assigning a value to an element or
attribute descriptor also writes the value's XSD lexical form through to
the container the writer serializes, so a later `to_string()`, `write()`,
or `revalidate()` reflects the assignment. Repeated element descriptors
are the exception — a repeated element has no single unambiguous target
node, so assignment updates the instance dictionary only and the write-out
is unchanged. Assigning a *bound node* directly (as internal binding does)
is not a user-facing operation and does not go through this path.

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
