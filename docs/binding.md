# Parse modes and binding

pyxsd validates XML and binds it to Python objects. Binding retention controls
whether invalid values or undeclared content are dropped or retained; keeping
them does not make the document valid. With the same validation settings,
strict and lax retention report the same problems.

## Separate the settings

Several independent options use the word “strict.” Choose each for its own
purpose:

| Choice | Default | What it controls |
| --- | --- | --- |
| XSD version | `xsd_version="1.1"` | Processor version and version-gated schema vocabulary. Use `"1.0"` when needed; see {doc}`supported` for coverage limits. |
| Namespace handling | `namespaces="legacy"` | Local-name compatibility versus expanded-name validation. Use `ParseModes.NAMESPACED` for namespace-aware binding. |
| Binding retention | `invalid_value="drop"` | Whether invalid lexical values are dropped or retained as raw strings; `ParseModes.LAX` also retains unresolved/undeclared content. |
| Facet checks | `facets="strict"` | Whether facets are enforced. Disabling them reduces validation; see {doc}`validation`. |
| XML parser profile | stdlib behavior | DOCTYPE/entity protections and optional structural limits; see {doc}`xml-security`. |
| Failure on report errors | Explicit check | Call `require_valid()` in Python or pass CLI `--strict`; neither is implied by a binding preset. |

Changing namespaces, XSD version, or facet checks can change findings.
`ParseModes.STRICT` is a binding preset, not a hardened XML parser profile.

## Using a mode

Pass a preset to `Schema.compile`:

```python
from pyxsd import ParseModes, Schema

schema = Schema.compile("schema.xsd", mode=ParseModes.LAX)
document = schema.parse("document.xml")
```

or from the CLI:

```console
$ pyxsd -i document.xml -s schema.xsd --mode lax -o out.xml
$ pyxsd -i document.xml -s schema.xsd --namespaces strict -o out.xml
```

`mode` accepts any `BindingPolicy`, so a preset is just a convenient
starting point:

```python
from pyxsd import ParseModes, Schema

mode = ParseModes.LAX.replace(whitespace="compat")
schema = Schema.compile("schema.xsd", mode=mode)
document = schema.parse("document.xml")
```

## Presets

| Preset | `invalid_value` | `unresolved_type` | `undeclared_content` | `whitespace` | `namespaces` |
| ------ | --------------- | ----------------- | -------------------- | ------------ | ------------ |
| `ParseModes.STRICT` (default) | `drop` | `error` | `error` | `xsd` | `legacy` |
| `ParseModes.LAX` | `raw` | `generic` | `generic` | `xsd` | `legacy` |
| `ParseModes.NAMESPACED` | `drop` | `error` | `error` | `xsd` | `strict` |

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
     rather than the typed value. This does not make serialization lossless.
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
* - `namespaces`
  - `"legacy"` \| `"strict"`
  - `legacy` keys schema components and instance nodes by local name, the
    historical no-namespace behavior. `strict` performs namespace-aware
    validation: expanded-name component identity, form-default-aware
    instance matching, prefixed `type`/`ref`/`xsi:type` resolution,
    cross-namespace imports, wildcard namespace + `processContents`
    enforcement, and QName value-space identity. See `ParseModes.NAMESPACED`.
:::

## Namespaces

Namespace handling is a policy field, so it composes with the other
choices:

```python
# namespace-aware validation, but keep binding lenient
mode = ParseModes.LAX.replace(namespaces="strict")
schema = Schema.compile("schema.xsd", mode=mode)
document = schema.parse("document.xml")
```

The default is `legacy` for compatibility. A prefixed name is reduced to its local part
and namespaces are ignored during instance matching. In `strict` mode
pyxsd records the in-scope namespace bindings while parsing, uses
expanded names for component identity and instance matching, and honors
`elementFormDefault` / `attributeFormDefault`. A namespace-only
`xs:import` can be satisfied with the `Schema.compile(...,
namespace_schemas=...)` mapping or a `schemaLocation`.

See {doc}`quickstart` for namespace-aware queries and {doc}`schema-resolution`
for imports. The [WordprocessingML examples](https://github.com/knorby/pyxsd/tree/develop/examples/docx)
compare namespaced and lax binding on a larger document.

## Why not just loosen the report?

Code that checks `document.report.has_errors` must not silently pass because
a caller wants lenient binding. Retaining invalid data is useful for inspection
and repair; the report continues to identify the supported checks it failed.

## Extending the policy

`BindingPolicy` is a frozen dataclass and `ParseModes` is a namespace of
named instances, so new fields and presets can be added without changing
any call signature. New call sites should read the policy off the
generated class (`getattr(cls, "_parseMode_", ParseModes.STRICT)`), the
same channel already used for `_contentModel_` and `_derivation_`.

The WordprocessingML family's separate lax-binding example renders retained
content while leaving validation findings visible.
