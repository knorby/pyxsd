# Parse modes and binding

pyxsd does two jobs at once: it **validates** a document against its
schema, and it **maps** the document into Python objects. Those jobs can
pull in opposite directions. A report you can trust wants every problem
reported; a mapping tool wants usable objects even when a document is a
little off.

Parse modes resolve that tension by separating the two:

> **The validation report is always strict. A parse mode only changes
> what value is bound into the tree.**

No mode suppresses an issue. `PyXSD.report` looks the same in strict and
lax mode; what differs is whether an invalid or unrecognized piece of the
document is dropped or kept in a best-effort form.

## Using a mode

Pass a preset to `PyXSD`:

```python
from pyxsd import PyXSD, ParseModes

app = PyXSD("document.xml", xsdFile="schema.xsd", mode=ParseModes.LAX)
```

or from the CLI:

```console
$ pyxsd -i document.xml -s schema.xsd --mode lax -o out.xml
```

`mode` accepts any `BindingPolicy`, so a preset is just a convenient
starting point:

```python
from pyxsd import BindingPolicy

mode = ParseModes.LAX.replace(whitespace="compat")
app = PyXSD("document.xml", xsdFile="schema.xsd", mode=mode)
```

## Presets

| Preset | `invalid_value` | `unresolved_type` | `undeclared_content` | `whitespace` |
| ------ | --------------- | ----------------- | -------------------- | ------------ |
| `ParseModes.STRICT` (default) | `drop` | `error` | `error` | `xsd` |
| `ParseModes.LAX` | `raw` | `generic` | `generic` | `xsd` |

## Policy fields

:::{list-table}
:header-rows: 1

* - Field
  - Values
  - Effect
* - `invalid_value`
  - `"drop"` \| `"raw"`
  - `drop` binds `None` for a value that fails lexical validation (the
    problem is reported). `raw` binds the original text as a plain string,
    so no data is lost.
* - `unresolved_type`
  - `"error"` \| `"generic"`
  - An element whose declared type cannot be resolved is reported in both
    cases; `generic` also binds the subtree through the wildcard
    pass-through path instead of dropping it.
* - `undeclared_content`
  - `"error"` \| `"generic"`
  - An element that no content-model particle accepts is reported in both
    cases; `generic` also binds it so unrecognized markup survives.
* - `whitespace`
  - `"xsd"` \| `"compat"`
  - `xsd` applies XSD 1.0 whitespace processing (space, tab, CR, LF only).
    `compat` additionally folds other Unicode whitespace (for example
    NBSP), matching Python's own `str.strip`.
:::

## Why not just loosen the report?

Because the report is the contract. Code that checks
`app.report.has_errors` must not silently pass just because a caller
wanted lenient binding. Keeping reporting strict means one run of pyxsd
answers both questions — "is this valid?" and "what can I use?" — without
the answer to the first depending on the answer to the second.

## Extending the policy

`BindingPolicy` is a frozen dataclass and `ParseModes` is a namespace of
named instances, so new fields and presets can be added without changing
any call signature. New call sites should read the policy off the
generated class (`getattr(cls, "_parseMode_", ParseModes.STRICT)`), the
same channel already used for `_contentModel_` and `_derivation_`.

A worked example is `examples/docx/`, which parses a deliberately messy
`document.xml` subset under `ParseModes.LAX` and renders Markdown while
the report still lists every problem.
