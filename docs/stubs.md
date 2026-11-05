# Type stubs for generated binding classes

Schema compilation creates classes at runtime. `Schema.stub()` describes
those classes for static type checkers or for inspection without instance XML.
Stubs describe declarations and construction APIs; some parsed complex-child
descriptors remain `None`. Retrieve those children with document queries.

## Generate and consume a stub

Using the `inventory.xsd` supplied in {doc}`quickstart`:

```sh
pyxsd --emit-stubs inventory.pyi -s inventory.xsd --namespaces strict
```

The Python equivalent is `schema.stub()`, returning text; save it with UTF-8
encoding. The full form contains imports of real pyxsd datatype classes,
generated class definitions with inheritance, and aliases for global elements.
It is a `.pyi` file, not an importable runtime module. Import its types only
under `TYPE_CHECKING` and cast the corresponding runtime object for your editor.
Inspect the emitted class names rather than guessing names for anonymous types.
The package's PEP 561 `py.typed` marker lets type checkers resolve datatype imports.

## Compact views and filtering

`schema.stub(compact=True)` returns a readable description, not Python source.
It uses one line per class/member, folds facet-only simple types onto their
members, and includes an alias legend and root list. On the CLI, add
`--stubs-compact` to `--emit-stubs`.

For a large schema, select declarations through `pyxsd.introspection`:

```python
from pyxsd.introspection import describe_component, find_components, related_components

# schema is the compiled Schema from the quickstart.
matches = find_components(schema, text="inventory", kind="element")
declaration = describe_component(schema, matches[0].id)
dependencies = related_components(schema, declaration.id, direction="uses", depth=2)
print(schema.stub(compact=True, component_ids=(declaration.id,), depth=1))
```

`component_ids` selects declarations and excludes unrelated root aliases.
`depth=None` follows all dependencies; `0` includes only selected bindings;
an empty tuple selects nothing. Explicitly selected simple types remain
visible in compact mode. Namespace filters use expanded-name namespaces;
`namespace=""` selects unqualified declarations.

## Annotations and runtime limits

- Repeated elements use `list[T]`. Optional members, including choice branches
  or optional ancestor groups, add `| None`. An absent repeated member reads
  as `None`, because its list is created on first binding.
- Required attributes use `T`; other attributes use `T | None`.
- Members with a default or fixed value use `T | str | None` plus a comment.
  An absent declaration-local default element can read as a lexical `str`;
  an absent fixed element reads `None`. Attribute defaults/fixed values are
  applied as typed values by the binder.
- Required members can also read `None` in invalid documents. An annotation
  is not proof that a document passed validation.
- Repeated descriptor assignment appends one item, not a list, and does not
  update the serialized tree. See {doc}`data-model` for mutation behavior.
- Union members, recursion back edges, unresolved declarations, and names
  unsuitable for dot access use `Any`, comments, or both. Mixed/wildcard/open
  content is described in comments rather than fully typed.

`stub()` does not reject a schema whose report contains errors. Call
`schema.require_valid()` first when invalid schemas should stop generation.

Two renderer limits also affect static checking: non-literal overlay parameter
defaults are emitted as their repr and may not type-check; schema types named
`Any` or `SchemaBase` collide with generated imports. These limits do not change
runtime XML validation.

## Regenerate after overlays

Stub rendering reads the live class table at call time. After loading an
overlay, regenerate the stub to include that class's members. With the CLI:

```sh
pyxsd --emit-stubs inventory.pyi -s inventory.xsd -c overlay.py
```

This command assumes you supplied `overlay.py`; see {doc}`architecture` for
the experimental overlay API and {doc}`extensions` for mixin registration.
Use {doc}`api` for introspection signatures and {doc}`cli` for flag behavior.
