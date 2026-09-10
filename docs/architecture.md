# Architecture

pyxsd's core idea, unchanged since 2006: **an XSD schema compiles into a set
of Python classes, and an XML document compiles into an instance tree of
those classes.** Everything else — validation, transforms, the writers —
operates on those two artifacts.

## Pipeline

1. **Schema load.** `PyXSD.parseXSD()` reads the schema (a file, path, or
   `ET.Element`), resolves `xs:include`/`xs:import`/`xs:redefine` by splicing
   the composed documents into the main tree *before* any class generation,
   and registers every top-level element as a descriptor in the global
   element-representative registry.

2. **Class generation.** Every schema component named in the registry
   generates a Python class:

   - **Simple types** resolve to one of the ~45 built-in types in
     `pyxsd.xsd_data_types` (validation happens in `__new__`) or to a
     generated class derived through `xs:restriction`/`xs:list`/`xs:union`.
   - **Complex types / elements** generate subclasses of `SchemaBase`
     through `types.new_class`, with `Element` and `Attribute` descriptors
     wired up via `__set_name__` and class metadata (`_elementNames_`,
     `_attributeNames_`) computed by `SchemaBase.__init_subclass__`.

   The generated class tree mirrors the derivation tree in your schema:
   `xs:extension` produces real subclassing, so `isinstance` checks and
   inherited descriptors work the way you'd expect.

3. **Instance construction.** `parseXML()` matches the document root against
   the global element declarations (honoring `xsi:type` dispatch and
   abstract-element rejection), then recursively builds instances. Text and
   attributes are validated against the declared types; undeclared children
   fall through wildcard (`xs:any`) declarations as generic nodes.
   Substitution-group members are accepted wherever their head is declared.
   Order/occurrence/identity checks run as post-passes.

4. **Transforms.** A transform pipeline — built-in `PrintData`,
   `SendTreeToPyXSD`, or user classes derived from `pyxsd.transforms.Transform`
   — is applied to the root instance. Transforms see a stable node shape
   (`_name_`, `_attribs_`, `_children_`, `_value_`; see {doc}`data-model`).

5. **Writers.** `XmlTreeWriter` serializes the (possibly transformed) tree
   back to XML, preserving document order and xsi declarations.

## The ElementRepresentative system

While the schema is being processed, every `xs:*` tag (`element`,
`complexType`, `sequence`, `attributeGroup`, …) becomes an
*ElementRepresentative* (ER) — a lightweight object that collects the
schema's own metadata (names, occurrence limits, type references, facets)
and links to its children ERs. ERs are the **compiler front end**: they
decide what class each component should get, resolve references (element
refs, group refs, substitution groups), and report schema-level errors.
The registry they populate is consulted during class generation and again
during instance construction (root matching, `xsi:type` resolution).

Key methods of the ER system:

- `ElementRepresentative.factory` — creates the right ER class for a tag,
  recursively processing children.
- `ElementRepresentative.registry` — global map of component name → ERs.
- `XsdType.clsFor` — the class-generation entry point for a type.

## Validation reporting

All non-fatal issues — from schema composition, instance validation, or
identity constraints — are recorded in one `ValidationReport` reachable at
`PyXSD.report`. Codes (e.g. `order`, `occurrence-min`, `identity-key`)
are stable strings suitable for grep-based CI checks; see {doc}`validation`.

## Overlay classes (experimental)

`-c file.py` loads a user module whose classes extend the generated ones by
subclassing `SchemaBase` — see `parser.loadClassFromFile`. This mechanism is
unchanged in spirit from 0.1 and remains experimental.
