# Architecture

pyxsd's core idea, unchanged since 2006: **an XSD schema compiles into a set
of Python classes, and an XML document compiles into an instance tree of
those classes.** Everything else — validation, transforms, the writers —
operates on those two artifacts.

## Pipeline

1. **Schema compile.** `Schema.compile()` reads the schema (a file, path,
   or `ET.Element`), resolves `xs:include`/`xs:import`/`xs:redefine` by
   splicing the composed documents into the main tree *before* any class
   generation, and registers every top-level element as a descriptor in
   the element-representative registry. Dependencies are resolved against
   the including document and read through the active resource policy (see
   below).

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

3. **Instance binding.** `Schema.parse()` parses the instance under the
   selected parser policy and limits, matches the document root
   against the global element declarations (honoring `xsi:type` dispatch
   and abstract-element rejection), then recursively builds instances.
   Text and attributes are validated against the declared types;
   undeclared children fall through wildcard (`xs:any`) declarations as
   generic nodes. Substitution-group members are accepted wherever their
   head is declared. Order/occurrence/identity checks run as post-passes,
   and the bound tree comes back as a `Document` carrying the merged
   report.

4. **Transforms.** `Document.transform()` applies a callable — a plain
   function, or a user class derived from `pyxsd.transforms.Transform`
   (the CLI resolves `-t 'Name(args)'` strings to the same callables) —
   to the root instance. Transforms see a stable node shape
   (`_name_`, `_attribs_`, `_children_`, `_value_`; see {doc}`data-model`).

5. **Writers.** `Document.write()` serializes the (possibly transformed)
   tree with `XmlTreeWriter`, preserving document order and xsi
   declarations. Serialization is deterministic: the writer emits no
   generated timestamp, so writing the same tree twice produces the same
   bytes. `writeHeaderInfo()` is a no-op subclass hook, not a default
   header (#48).

## Resource resolution and the parser boundary

Composition and instance loading share one reference model,
`pyxsd.resource_refs`. A source is classified as a local path, a `<memory>`
stream, or an absolute URI. `resource_base()` gives a document's canonical
identity (resolved local path, remote URI, or `<memory>`), and
`resolve_reference()` resolves a dependency against the *including document*,
not its directory: a remote base follows RFC 3986 URI semantics, a local base
uses filesystem semantics, and a `file:` URI stays a remote reference that the
default loader refuses. The canonical base also drives composition-cycle
detection.

A `ResourcePolicy` sits between that model and the filesystem or network:
`read()` supplies original bytes plus a canonical source ID, and `parse()`
returns the ElementTree and records namespace bindings. The default loader is
local-only. `RemoteResourcePolicy` is the opt-in HTTPS implementation; it
authorizes each root, same-origin/cross-origin dependency, instance hint, and
redirect against finite per-compilation budgets. Python overlays are never
retrieved remotely.

The parser roles are:

- **Semantic parsing.** `parse_with_namespaces` uses
  `xml.etree.ElementTree.iterparse` with the selected `XmlParserPolicy`.
  The default is the stdlib parser; `protected()` and `.strict()` switch to
  defusedxml and the shared Expat declaration guards. `XmlLimits` bounds the
  bytes, element depth, and start-tag count. This tree is what binding,
  validation, transforms, XPath, and the writer use.
- **DTD declaration collection.** When the compiled schema can validate
  `ENTITY`/`ENTITIES`, `pyxsd.dtd_declarations.collect_document_declarations`
  uses raw Expat to collect unparsed-entity declarations from the instance
  prolog/internal subset, stopping at the first element start. Internal
  parameter entities are expanded; external parameter entities are skipped
  without retrieval. It builds no second tree and does not validate the
  document body or DTD. Reopenable or rewindable sources supply a prolog
  reread; non-seekable streams passed to
  compiled `Schema.parse()` use prolog collection and replay. Unrelated
  schemas skip this pass. Collection preserves the selected declaration
  guards and installs no external fetcher. Missing provenance and external
  subsets limit the evidence; see {doc}`validation`.
- **Source indexing.** `pyxsd.source_spans.SourceIndex` runs a separate raw
  `xml.parsers.expat` pass over the original bytes to record element paths and
  exact half-open byte spans. It never installs an external-resource fetcher,
  builds no second tree, and applies the same declaration policy and,
  when supplied, structural limits. It rejects every entity reference it cannot
  expand, so it never accepts a document the semantic parser rejected. The MCP
  server uses it for `result: "subtree"` so the returned bytes are an exact
  slice of the captured source rather than a serialization of the bound tree.

None of these parser roles is a sandbox. Path confinement, authentication, and
timeouts belong to the application policy or the surrounding process boundary;
see {doc}`xml-security`.

## The ElementRepresentative system

While the schema is being processed, every `xs:*` tag (`element`,
`complexType`, `sequence`, `attributeGroup`, …) becomes an
*ElementRepresentative* (ER) — a lightweight object that collects the
schema's own metadata (names, occurrence limits, type references, facets)
and links to its children ERs. ERs are the **compiler front end**: they
decide what class each component should get, resolve references (element
refs, group refs, substitution groups), and report schema-level errors.
The registry they populate is consulted during class generation and again
during instance binding (root matching, `xsi:type` resolution).

Key methods of the ER system:

- `ElementRepresentative.factory` — creates the right ER class for a tag,
  recursively processing children.
- `ElementRepresentative.registry` — global map of component name → ERs.
- `XsdType.clsFor` — the class-generation entry point for a type.

## Validation reporting

All non-fatal issues — from schema composition, instance validation, or
identity constraints — are recorded in a `ValidationReport`: the
schema's own findings at `Schema.report`, and the merged run (schema
issues plus the document's) at `Document.report`. Codes (e.g. `order`,
`occurrence-min`, `identity-key`) are stable strings suitable for
grep-based CI checks; see {doc}`validation`.

## Overlay classes (experimental)

`-c file.py` (or `Schema.compile(..., overlay=...)`) loads a user module
whose classes extend the generated ones by subclassing `SchemaBase` —
see `pyxsd.schema.load_overlay_classes`. This mechanism is unchanged in
spirit from 0.1 and remains experimental.
