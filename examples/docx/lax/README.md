# docx (WordprocessingML) lax example

> The real, namespaced example lives one directory up in
> [`../README.md`](../README.md). This directory keeps the smaller
> no-namespace stand-in that demonstrates lax binding.

A real office format, parsed the messy way. This example takes a small
**hand-authored, no-namespace stand-in** for the `word/document.xml`
part of a `.docx` file and converts it to Markdown, demonstrating the
lax parse mode.

## Why a stand-in schema

Real OOXML uses the `wordprocessingml/2006/main` namespace and the huge
`wml.xsd`. pyxsd validates documents in the default/no-namespace style,
and this example keeps to the local element names (`document`, `body`,
`p`, `r`, `t`, `pPr`, `pStyle`, `rPr`, `b`, `i`, `u`, `sz`) so the
schema stays readable. The instance document is intentionally **messy**:

- `bookmarkStart` is not in the subset, and
- one run's `sz` is `not-a-number`, not a positive integer.

## Run it

From this directory:

```console
$ uv run pyxsd -i instance.xml -s schema.xsd --mode lax \
      -k -o /dev/null -t "ToMarkdown('document.md')"
$ cat document.md
```

`ToMarkdown` maps `Heading1`–`Heading3` to `#`/`##`/`###`, `ListBullet`
to `-`, and run properties `b`/`i` to `**`/`*`.

## The lax point

The validation report is the same in either mode — `bookmarkStart` is
reported as an unexpected element and the bad `sz` as an invalid value.
The difference is what lands in the tree:

| | strict | lax (`--mode lax`) |
| --- | --- | --- |
| `bookmarkStart` | dropped | bound as a generic node |
| `sz` value | dropped | raw string `"not-a-number"` |

That is what lets a mapping tool keep working on documents that do not
exactly match the schema. See `docs/binding.md` for the parse modes.

`expected.md` is the reference output and is checked by
`tests/test_example_applications.py`.
