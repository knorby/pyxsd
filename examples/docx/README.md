# docx (WordprocessingML) example

Parse a realistic `word/document.xml` against the real ECMA-376
WordprocessingML schema and render it as style-aware Markdown. This is the
`examples/` proof that opt-in namespace support handles a large, genuine
schema — the full OOXML schema set is roughly 26 files and 700 KB.

## Fetch the schema

The ECMA-376 schemas are redistributable but large, so they are not committed.
Download them once:

```console
$ uv run python download_schemas.py
```

This writes all 26 Transitional `.xsd` files into `schemas/` (gitignored).
The end-to-end test skips itself when `schemas/wml.xsd` is missing, so a fresh
checkout stays green before the download.

## Run it

From this directory:

```console
$ uv run pyxsd -i document.xml -s schemas/wml.xsd --namespaces strict \
      -k -o /dev/null -t "ToMarkdown('document.md')"
$ cat document.md
```

Or from anywhere, using the library API:

```python
from pyxsd import ParseModes, Schema
from pyxsd.cli import resolve_transform_class

schema = Schema.compile("examples/docx/schemas/wml.xsd", mode=ParseModes.NAMESPACED)
schema.require_valid()
document = schema.parse("examples/docx/document.xml")

ToMarkdown = resolve_transform_class("ToMarkdown", search_paths="examples/docx")
document.transform(lambda root: ToMarkdown(root)("document.md"))
```

`ToMarkdown` maps `Heading1`–`Heading3` to `#`/`##`/`###`, `ListNumber` /
`ListBullet` to indented ordered/unordered items, `w:tbl` to a GitHub pipe
table, `w:hyperlink` to `[text](relationship-id)`, and run properties
`b`/`i`/`u`/`color`/`sz` to `**`/`*`/`<u>`/`<span style="...">`.

The reference output is `expected.md`, checked by
`tests/test_example_applications.py`.

## What the document covers

`document.xml` is a small but dense release-notes document exercising the
parts of WordprocessingML a mapping tool actually meets:

- headings (`pStyle`), multilevel numbered and bulleted lists (`numPr`);
- inline runs with bold, underline, color, and font size;
- a hyperlink with a relationship `r:id`;
- a table with borders, a grid, and cell widths;
- tabs (`w:tab`), line breaks (`w:br`), and `xml:space="preserve"`.

## Namespaced validation

The schema is loaded with `elementFormDefault`/`attributeFormDefault`
`qualified` and a default namespace, so element and attribute names are matched
by expanded name, and unprefixed QName *values* such as `base="CT_Markup"`
resolve against the in-scope default namespace. `r:id` resolves through the
relationships namespace; `xml:space` resolves through the built-in XML
namespace.

Loading the full schema reports five `compose-cycle` **warnings** and no
errors. WordprocessingML's drawing dependency includes itself more than once;
pyxsd deduplicates the repeated include and continues.

## The lax variant

`lax/` keeps the earlier, smaller demonstration: a hand-authored
no-namespace stand-in for `word/document.xml` with a deliberately messy
instance, parsed in `ParseModes.LAX` to show that binding can continue past a
reported problem. See [`lax/README.md`](lax/README.md) and
[`docs/binding.md`](../../docs/binding.md).
