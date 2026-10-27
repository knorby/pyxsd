# Shared reference: exact occurrences and fresh revalidation

## Same-name sibling indexes

`ValidationIssue.location.path` entries carry `{name, namespace, index}` where
`index` is the one-based position among same-name siblings. The second
`<item>` among three is index 2. Location identifies an **occurrence**; a
local name alone identifies only a declaration.

```python
document = schema.parse("doc.xml")
issue = document.report.issues[0]
node = document.locate(issue.location)  # the exact offending occurrence
```

## Why stale reports lie

`document.report` describes the tree as parsed. After you mutate the tree,
that report is stale: a document that was invalid may now be valid, and vice
versa. The only verdict that counts is a fresh parse of the serialized
result:

```python
from pyxsd import Schema
from pyxsd.binding import ParseModes

schema = Schema.compile("schema.xsd", mode=ParseModes.LAX)
parsed = schema.parse("doc.xml")
parsed.findall("./item")[1].sku = "ABC-2222"
fixed = Path("fixed.xml")
fixed.write_text(parsed.to_string(), encoding="utf-8")
assert schema.parse(str(fixed)).report.error_count == 0
```

`require_valid()` on the pre-edit report would have described the old bytes.
Revalidation is cheap and is the only proof.

## Repeated elements

Elements with `maxOccurs > 1` store every occurrence in `_children_` in
document order — there is no named list attribute. Reach occurrences with
`document.findall("./item")`; single-occurrence children bind as named
attributes. The same-name sibling index in an issue location identifies the
exact occurrence to repair.

## Minimal semantic edits

Edit bound values only. Serialization round-trips everything else —
ordering, attributes, whitespace in mixed content. Compare
`document.to_string()` before and after when fidelity matters, and write
output only through an explicit `Document.write(path)` call.
