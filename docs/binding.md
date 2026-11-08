# Parse modes and binding policies

pyxsd validates XML and binds it to Python objects. A `BindingPolicy` controls
how binding retains or drops content that fails validation, and how instance
names are matched to schema components. A `ParseModes` preset is a named
combination of those fields. Neither changes what the report says: with the
same validation settings, strict and lax retention report the same problems.

## Separate the settings

Several independent options use the word “strict.” Choose each for its own
purpose:

| Choice | Default | What it controls |
| --- | --- | --- |
| XSD version | `xsd_version="1.1"` | Processor version and version-gated schema vocabulary. Use `"1.0"` when needed; see {doc}`supported` for coverage limits. |
| Namespace handling | `namespaces="strict"` | Expanded-name validation by default; explicit `"legacy"` keeps local-name compatibility. |
| Binding retention | `invalid_value="drop"` | Whether invalid lexical values are dropped or retained as raw strings; `ParseModes.LAX` also retains unresolved/undeclared content. |
| Facet checks | `facets="strict"` | Whether facets are enforced. Disabling them reduces validation; see {doc}`validation`. |
| XML parser profile | stdlib behavior | DOCTYPE/entity protections and optional structural limits; see {doc}`xml-security`. |
| Failure on report errors | Explicit check | Call `require_valid()` in Python or pass CLI `--strict`; neither is implied by a binding preset. |

Changing namespaces, XSD version, or facet checks can change findings.
`ParseModes.NAMESPACED` is the default binding policy, and it is not a
hardened XML parser profile. Parser protections remain a separate choice
documented in {doc}`xml-security`.

## Using a mode

`Schema.compile()` and the module-level `pyxsd.parse()` use
`ParseModes.NAMESPACED` when `mode` is omitted:

```python
from pyxsd import Schema

schema = Schema.compile("schema.xsd")
document = schema.parse("document.xml")
```

Pass `ParseModes.STRICT` or `ParseModes.LAX` explicitly when a document needs
the historical local-name behavior:

```python
from pyxsd import ParseModes, Schema

schema = Schema.compile("schema.xsd", mode=ParseModes.STRICT)
document = schema.parse("document.xml")
```

The CLI default is likewise namespace-strict. The compatibility choice is
explicit:

```console
$ pyxsd -i document.xml -s schema.xsd -o out.xml
$ pyxsd -i document.xml -s schema.xsd --namespaces legacy -o out.xml
```

`mode` accepts any `BindingPolicy`, so a preset is just a convenient
starting point:

```python
from pyxsd import ParseModes, Schema

mode = ParseModes.LAX_NAMESPACED.replace(whitespace="compat")
schema = Schema.compile("schema.xsd", mode=mode)
document = schema.parse("document.xml")
```

## Presets

| Preset | `invalid_value` | `unresolved_type` | `undeclared_content` | `whitespace` | `namespaces` |
| ------ | --------------- | ----------------- | -------------------- | ------------ | ------------ |
| `ParseModes.STRICT` | `drop` | `error` | `error` | `xsd` | `legacy` |
| `ParseModes.LAX` | `raw` | `generic` | `generic` | `xsd` | `legacy` |
| `ParseModes.NAMESPACED` (default) | `drop` | `error` | `error` | `xsd` | `strict` |
| `ParseModes.LAX_NAMESPACED` | `raw` | `generic` | `generic` | `xsd` | `strict` |

The first two presets preserve the pre-namespaced behavior and are kept for
callers that need local-name matching. `NAMESPACED` and `LAX_NAMESPACED` pair
that same retention choice with namespace-aware matching. `BindingPolicy()`
itself defaults to `NAMESPACED`; the legacy presets set
`namespaces="legacy"` explicitly.

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
  - `"strict"` \| `"legacy"`
  - `strict` (the default) performs namespace-aware validation:
    expanded-name component identity, form-default-aware instance matching,
    prefixed `type`/`ref`/`xsi:type` resolution, cross-namespace imports,
    wildcard namespace + `processContents` enforcement, and QName
    value-space identity. `legacy` keys schema components and instance
    nodes by local name, ignoring namespace URIs.
:::

## Namespaces

`ParseModes.NAMESPACED` makes namespace handling part of the default parse
contract. pyxsd records the in-scope namespace bindings while parsing, uses
expanded names for component identity and instance matching, and honors
`elementFormDefault` / `attributeFormDefault`. A root or child in the wrong
namespace is reported and is not bound as the declaration it resembles; for
example, a namespace mismatch at the root produces `unknown-root`.

Namespaces still compose with the other policy fields. Use
`ParseModes.LAX_NAMESPACED` when namespace-aware validation should retain
best-effort values:

```python
mode = ParseModes.LAX_NAMESPACED
schema = Schema.compile("schema.xsd", mode=mode)
document = schema.parse("document.xml")
```

A namespace-only `xs:import` can be satisfied with the
`Schema.compile(..., namespace_schemas=...)` mapping or a `schemaLocation`.

Legacy local-name matching remains available by opting in:

```python
from pyxsd import ParseModes, Schema

schema = Schema.compile("schema.xsd", mode=ParseModes.LAX)
```

That preset ignores namespace URIs while matching and retains best-effort
values. Use it only for documents that depend on the historical behavior.
The CLI equivalent is `--namespaces legacy`.

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
generated class (`getattr(cls, "_parseMode_", ParseModes.NAMESPACED)`), the
same channel already used for `_contentModel_` and `_derivation_`.

The WordprocessingML family's separate lax-binding example renders retained
content while leaving validation findings visible.
