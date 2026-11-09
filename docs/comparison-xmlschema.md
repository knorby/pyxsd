# pyxsd compared with xmlschema

```{eval-rst}
.. meta::
   :description: Compare pyxsd and xmlschema for runtime XML bindings, validation reports, Python and JSON conversion, schema-derived exports, and code generation.
```

Choose pyxsd when you want a schema-defined Python object tree that you can
query, extend, transform, and export. Choose
[`xmlschema`](https://github.com/sissaschool/xmlschema) when converter-based
XML/Python/JSON encoding and decoding, lazy processing, or WSDL support is
central to your application. Both validate XSD 1.0 and 1.1 and offer runtime
bindings. pyxsd makes runtime class generation its primary model: the current
schema supplies extensible classes directly, with no generated binding module
to regenerate or drift out of sync.

This is an API and design comparison, not a performance benchmark. For
validity-verdict evidence, {doc}`xsts` records the shared corpus, each
engine's checked denominator, and reviewed baselines. For pyxsd's broader
case, read {doc}`why-pyxsd`.

## At a glance

| | pyxsd | xmlschema |
|---|---|---|
| XSD support | 1.1 default; explicit 1.0 mode (`xsd_version="1.0"`); coverage in {doc}`supported` | 1.0 and 1.1 (`XMLSchema10` / `XMLSchema11`; `xmlschema.XMLSchema` is 1.0) |
| Runtime dependency | `elementpath` + `defusedxml` (both pure Python; `defusedxml` is loaded only for opt-in parser profiles) | `elementpath` only (pure Python) |
| XML parser | stdlib `xml.etree.ElementTree` by default; opt-in `XmlParserPolicy.protected()` / `.strict()` use defusedxml; no external DTD/entity fetching in any built-in profile | stdlib `ElementTree` (with optional lxml injection for instance resources) |
| Primary model | validate, bind to a schema-defined Python object tree, query, export, transform | decode/encode XML↔Python/JSON, validate, inspect schema components |
| Instance → Python | `Document` tree of runtime-generated classes; raw `to_dict()` or JSON-safe `to_data()` / `to_json()` | Converters to nested `dict`, JSON, `DataElement`, and runtime-generated data bindings |
| Python/JSON → XML | `Schema.from_data()` / `from_json()` bind five formats into a `Document`; `to_string()` / `write()` serialize it | `encode()`, `from_json()`, converter classes |
| JSON Schema | `Schema.json_schema()` describes all five export formats as Draft 2020-12; explicit XSD validation limits | Converter-based Python/JSON contracts; consult its API for supported exports |
| Binding source files | None required; classes come directly from the compiled XSD; optional `.pyi` stubs describe them | Runtime data bindings available; optional static generation with Jinja2 templates |
| Application extensions | Compile-time mixins with methods and validation hooks | Custom converters and data-binding classes |
| Pydantic / Arrow | Optional schema-derived Pydantic models and Arrow/Parquet record projections | Python/JSON conversion; no equivalent built-in adapters documented |
| XPath/lookup | `Document.xpath()` / `find()` / `findall()` return bound nodes; declaration introspection is separate | XPath-based schema component API; `ElementPathMixin` on components and `XMLResource` |
| Transform workflow | Python callables over the bound tree, available through the CLI | Custom converters and ordinary Python processing of decoded data |
| Validation output | `ValidationReport` with stable codes, phases, and optional occurrence locations | Exception hierarchy, `iter_errors()`, and lax decoding with collected errors |
| Lazy / incremental | no | `XMLResource` lazy modes (`lazy`, `thin_lazy`) |
| Resource hardening | opt-in parser profiles (`protected` / `strict`), `XmlLimits`, and an opt-in HTTPS remote schema policy with finite budgets; not a sandbox | entity-forbidding parser, `allow`/`defuse` resource policy, timeouts, processing limits |
| Remote schema download | opt-in `RemoteResourcePolicy`: HTTPS only, explicit origin/hint rules, finite per-compilation budgets, no persistent cache; local-only default | yes, with an offline cache |
| WSDL | no | yes (WSDL 1.1) |

(where-pyxsd-is-a-good-fit)=

## Where pyxsd helps

### A schema-driven application model

pyxsd's normal compile/parse path produces typed Python objects. XSD type
derivation becomes class inheritance, simple values use XSD datatype
classes, and mixins add application methods and validation hooks at compile
time. Queries return the original bound nodes, so selection and application
logic work on the same objects. See {doc}`extensions` and {doc}`data-model`.

This is an advantage of the runtime model, not a reduction in what the
application objects can do. Generated binding source adds an artifact that can
drift from its XSD until someone regenerates it. pyxsd instead compiles the
current schema into classes, while mixins keep application methods and
validation hooks separate from the schema-derived structure. There are no
generated class bodies to edit or preserve across regeneration.

Compile the XSD once and reuse the resulting schema across documents. When the
schema changes, recompile it instead of regenerating and distributing binding
modules. Optional {doc}`stubs` describe those runtime classes for editors and
type checkers without becoming a separate runtime implementation.

`xmlschema` also supports runtime-generated bindings through
`DataBindingConverter`, in addition to `DataElementConverter` and static
Jinja2 generation. The choice is therefore between binding APIs and
workflows, not between having Python objects and doing without them. Its
[data bindings documentation][xmlschema-bindings] describes that model.

### Inspection and repair with explicit validation

pyxsd keeps recoverable findings on `ValidationReport` and lets the caller
choose where `require_valid()` should stop a pipeline. Stable issue codes,
phase tags, and optional indexed source locations support automated triage;
`Document.locate()` can connect a finding to its occurrence. Lax binding
retention keeps more data available for repair without suppressing findings
under the same validation settings. See {doc}`validation` and {doc}`binding`.

`xmlschema` also collects errors through `iter_errors()` and lax validation.
pyxsd's benefit is the report attached to the bound document and the
separation between data retention and error policy, not an exclusive ability
to report multiple errors. In both libraries, fatal input failures can
prevent a usable result.

### A direct route to application output

Use `to_dict()` / `to_data()` / `to_json()` for structured extraction, or apply a Python
callable through `Document.transform()`. The CLI can invoke the same
transform classes. After changing a tree, `revalidate()` produces a fresh
document and report before writing. See {doc}`transforms/using`.

The optional {doc}`integrations` derive Pydantic models and Arrow record
schemas from XSD before any instance XML exists. This suits applications
that need a declared data contract or typed Parquet output rather than a
sample-inferred mapping. The projections have their own supported-shape
limits and do not replace full XML validation.

### Document data contracts

pyxsd exports and imports native `pyxsd`, schema-driven `pyxsd-json`, Parker,
BadgerFish, and JsonML data. `Schema.from_data()` / `from_json()` reconstruct
an XML tree and run the normal binder, returning generated classes and a
`Document.report`; call `require_valid()` when invalid content must stop the
pipeline. Export defaults to native `pyxsd` and best effort, while import
defaults to `pyxsd-json` and strict fidelity. Fidelity governs representation
loss, not XSD validity. Ordered and mixed content retain their positions in
`pyxsd-json` and JsonML; grouped formats have explicit limits.

`Schema.json_schema()` constructs Draft 2020-12 descriptions for all five
export formats before any instance exists. It records unvalidated XSD rules
under `x-pyxsd-validation`; structural acceptance is neither full XSD
validation nor a guarantee of import acceptance. Native export options must
match generation, and root-absorbed formats need an unambiguous selection.
The optional Pydantic models' JSON Schema describes a separate projection.
See {doc}`data-conversion` for examples and the precise limits.

`xmlschema` offers converter classes and configurable encoding/decoding.
Shared convention names do not promise interchangeable JSON. pyxsd's tested
profiles differ in decimal strings, nil markers, namespace prefixes, and
mixed text. Parker comparisons may produce identical data while pyxsd emits
`attributes-lost` warnings for attributes xmlschema silently drops; strict
pyxsd export rejects that loss. The pinned interoperability fixtures on the
data-conversion page are evidence for those cases, not general conformance.

## Where xmlschema is a better fit

- **Converter customization and breadth.** `xmlschema` encodes Python data and
  JSON back to XML, with converter classes (`ParkerConverter`,
  `BadgerFishConverter`, `JsonMLConverter`, `ColumnarConverter`,
  `DataElementConverter`, and more), plus custom converter subclasses.
  pyxsd exposes five fixed document formats through its import/export APIs,
  not a caller-configurable converter framework.
- **Lazy and incremental processing.** `XMLResource` with `lazy` /
  `thin_lazy` supports large documents and streaming, which pyxsd does not.
- **Resource controls and hardening.** `xmlschema` exposes a broader
  resource-policy layer (`allow` / `defuse`), timeouts, processing limits, and
  remote download with an offline cache. pyxsd has opt-in parser profiles,
  `XmlLimits`, and budgeted HTTPS schema retrieval, but no persistent cache,
  lxml parser injection, or general per-parse timeout. Its default parser is
  the stdlib parser; local-only loading does not confine filesystem access.
  See {doc}`xml-security` before processing untrusted inputs.
- **Generated source as a deliverable.** Jinja2 generation suits projects
  that specifically need source modules to inspect or distribute. pyxsd
  instead builds its implementation classes at runtime and optionally emits
  typing stubs; it does not need static source to provide bindings.
- **WSDL 1.1 processing.** pyxsd does not provide a WSDL API.
- **XPath over schema components.** `xmlschema` has an XPath-based component
  API and global maps. pyxsd has declaration introspection, dependency
  traversal, and filtered stubs, but no public schema-component XPath API.
  See {doc}`api` for the available introspection surface.

(honest-limitations-of-pyxsd)=

## Limitations of pyxsd

- **Namespace-correct documents are the default expectation.** The default
  `ParseModes.NAMESPACED` policy rejects documents that only matched by local
  name; `ParseModes.STRICT`, `ParseModes.LAX`, and `--namespaces legacy` are
  the explicit compatibility choices.
- **Grouped exports lose mixed-content positions.**
  Interleaved text is kept verbatim in the bound tree (leading text in
  `_value_`, child tails in `_tail_`) and survives serialization and
  XPath queries, but the dict export can only concatenate the character
  data under `$`. Use `pyxsd-json` or JsonML for ordered mixed export;
  the Arrow/Pydantic integrations reject mixed content.
- **DTD-dependent checks have limited evidence.** `ENTITY`/`ENTITIES` checks
  depend on available unparsed-entity declarations; external subsets are not
  fetched. See [DTD evidence and input limitations](validation.md#dtd-dependent-entity-and-entities-values).
  Parser profiles are separate and do not provide DTD validation; see
  {doc}`xml-security`.
- **One parse at a time per `Schema`.** The compiled schema holds per-parse
  indexes on a shared host, so concurrent parses against the same `Schema`
  are not supported.
- **XML 1.1 documents are not fully supported** (an expat limitation),
  though schema version *mode* 1.0/1.1 is.

## Using them together

pyxsd uses xmlschema for conversion fixtures as described above, and in two
distinct validation test roles:

- **Manifest differential oracle.** `tests/test_oracle.py`, enabled by
  `PYXSD_RUN_ORACLE=1`, compares the engines' schema and available instance
  verdicts over the curated manifest. It selects `XMLSchema11` only for cases
  marked `xsd11`; other cases use the XSD 1.0 class. Documented case-specific
  divergences are skipped, while unexpected exceptions are harness failures,
  not agreement.
- **Full-XSTS comparison engine.** `tests/report_xsts.py --engine both` scores
  each engine independently against the pinned main-suite expectations and
  selected profile, retaining its adapter gaps and other non-verdict outcomes.
  The XSD 1.1 baseline records both engines; the XSD 1.0 baseline currently
  records pyxsd only. `PYXSD_RUN_ORACLE` does not select this runner's engines.

Neither role makes xmlschema normative truth. A disagreement prompts
specification-based adjudication; it does not prove that either engine or the
corpus is wrong. The required XSTS PR gate checks the recorded baseline, not
pairwise agreement. See [Two distinct xmlschema roles](xsts.md#two-distinct-xmlschema-roles)
for the populations, skips, scoring rules, and reproduction commands.

## See also

- {doc}`supported` — pyxsd's feature inventory and known gaps.
- {doc}`xsts` — verdict-agreement methodology, provenance, and results.
- {doc}`xsd-validation` — XML/XSD versions and validation terminology.
- {doc}`comparison-libxml2` — C-engine scope and preliminary XSD 1.0 evidence.
- {doc}`comparison-lxml` — general XML/HTML tree APIs and objectify bindings.
- [Data model](data-model.md) — the bound tree, `to_dict`/`to_json`, and the
  projection used by `xpath`/`find`.
- [xmlschema documentation](https://xmlschema.readthedocs.io/en/stable/) — the
   authoritative description of the features attributed to it above.

[xmlschema-bindings]: https://xmlschema.readthedocs.io/en/stable/api.html#xmlschema.DataBindingConverter
