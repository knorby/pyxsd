# pyxsd compared with xmlschema

[`xmlschema`](https://github.com/sissaschool/xmlschema) is the established
Python library for XML Schema work, and the natural reference point for
pyxsd. This page describes where the two overlap, where they differ, and
which is the better fit for a given task. It is a design comparison, not a
benchmark; the numbers quoted are from each project's own conformance
runs and are explained in [Supported features](supported.md).

## At a glance

| | pyxsd | xmlschema |
|---|---|---|
| XSD support | 1.1 processor, opt-in 1.0 mode (`Schema.compile(xsd_version=)`, `--xsd-version`) | Full 1.0 and 1.1 (`XMLSchema10` / `XMLSchema11`; `xmlschema.XMLSchema` is 1.0) |
| Runtime dependency | `elementpath` + `defusedxml` (both pure Python; `defusedxml` is loaded only for opt-in parser profiles) | `elementpath` only (pure Python) |
| XML parser | stdlib `xml.etree.ElementTree` by default; opt-in `XmlParserPolicy.protected()` / `.strict()` use defusedxml; no external DTD/entity fetching in any built-in profile | stdlib `ElementTree` (with optional lxml injection for instance resources) |
| Primary model | validate, bind to a schema-defined Python object tree, query, export, transform | decode/encode XML↔Python/JSON, validate, inspect schema components |
| Instance → Python | `Document` bound tree of generated classes; `to_dict()` / `to_json()` | converters to nested `dict`, JSON, and `DataElement` bindings |
| Python/JSON → XML | not provided (transforms can build a `Document`) | `encode()`, `from_json()`, converter classes |
| Static code generation | no (classes are generated in memory per compiled `Schema`) | yes, Jinja2 templates |
| XPath/lookup | `Document.xpath()` / `find()` / `findall()` over a projected bound tree; `elementpath` used internally for XSD regular expressions and identity/assertion XPath, but there is no public schema-component XPath API | XPath-based schema component API; `ElementPathMixin` on components and `XMLResource` |
| Transform framework | callable transforms over the bound tree, addressable by class name from the CLI | converters plus code generation (no general transform hook) |
| Validation output | `ValidationReport` with stable short codes and phases | exception hierarchy plus `iter_errors()` |
| Lazy / incremental | no | `XMLResource` lazy modes (`lazy`, `thin_lazy`) |
| Resource hardening | opt-in parser profiles (`protected` / `strict`), `XmlLimits`, and an opt-in HTTPS remote schema policy with finite budgets; not a sandbox | entity-forbidding parser, `allow`/`defuse` resource policy, timeouts, processing limits |
| Remote schema download | opt-in `RemoteResourcePolicy`: HTTPS only, explicit origin/hint rules, finite per-compilation budgets, no persistent cache; local-only default | yes, with an offline cache |
| WSDL | no | yes (WSDL 1.1) |
| Maturity | modern 1.x line; smaller user base | mature, widely deployed, large user base |

## Where pyxsd is a good fit

- **Runtime binding without code generation.** A schema is compiled once and
  reused; each parse produces a `Document` whose nodes are typed Python
  objects (descriptors for declared elements and attributes, XSD datatypes
  for simple values). There is no build step and no generated source tree.
- **Validation that continues instead of stopping.** pyxsd collects issues on
  a `ValidationReport` with stable codes rather than raising on the first
  error, and `Document.is_valid` / `require_valid()` make the policy explicit.
- **Callable transforms.** A transform is any callable over the bound tree,
  and the CLI can address a transform by class name (`-t 'ToDict()'`). This
  suits pipeline code that post-processes a validated document.
- **A small, pure-Python base.** The base install depends on `elementpath`
  and `defusedxml`, has no compiled extensions, and parses locally by default.
  `defusedxml` is loaded only when a parser profile selects it. Optional extras
  add their own dependencies. The only built-in remote retrieval is an explicit
  `RemoteResourcePolicy`, and a custom `ResourcePolicy` is trusted application
  code; audit any network or file access you implement there.
- **Direct query and export.** `xpath` / `find` / `findall` and
  `to_dict` / `to_json` cover the "validate, then pull values out" pattern
  without a converter configuration step.

## Where xmlschema is a better fit

- **Encoding and JSON round-tripping.** `xmlschema` encodes Python data and
  JSON back to XML, with converter classes (`ParkerConverter`,
  `BadgerFishConverter`, `JsonMLConverter`, `ColumnarConverter`,
  `DataElementConverter`, and more). pyxsd does not implement encode; a
  transform can construct XML, but that is not the same tool.
- **Lazy and incremental processing.** `XMLResource` with `lazy` /
  `thin_lazy` supports large documents and streaming, which pyxsd does not.
- **Resource controls and hardening.** `xmlschema` exposes a broader
  resource-policy layer (`allow` / `defuse`), timeouts, processing limits, and
  remote download with an offline cache. pyxsd now offers opt-in parser profiles
  (`protected` / `strict`), `XmlLimits`, and an HTTPS-only
  `RemoteResourcePolicy` with finite budgets, but it has no offline cache,
  lazy/streaming mode, lxml injection, or general per-parse timeout, and its
  resource policy is not an `allow`/`defuse` sandbox. The default remains the
  unfettered stdlib parser for compatibility. None of these controls is a
  sandbox.
- **Static code generation and WSDL.** Generators and WSDL 1.1 processing
  have no pyxsd equivalent.
- **Schema-component inspection.** `xmlschema`'s XPath-based component API
  and global maps are deeper for tooling that interrogates the schema itself.
- **Maturity.** `xmlschema` has years of production use, a large body of
  documentation, and a broad user base. pyxsd's modern 1.x object model is
  younger and has a smaller user base; the known limitations below matter
  when choosing between them.

## Honest limitations of pyxsd

- **Maturity.** The modern 1.x API has less field hardening than
  `xmlschema`.
- **No encode.** There is no Python/JSON→XML serializer beyond writing a
  parsed `Document` back out.
- **Legacy namespace mode is the default binding policy.** Strict namespace
  handling is a distinct opt-in mode (`ParseModes.NAMESPACED`), which
  surprises users who expect qualified names by default.
- **Mixed content is preserved but not a structured-export citizen.**
  Interleaved text is kept verbatim in the bound tree (leading text in
  `_value_`, child tails in `_tail_`) and survives serialization and
  XPath queries, but the dict export can only concatenate the character
  data under `$`, and the Arrow/Pydantic integrations reject mixed
  content.
- **DTD is internal-subset only, and the default is unobserved.** Declarations
  beyond what expat exposes (notably unparsed `NDATA` entities) are not tracked;
  external subsets are never fetched. The stdlib default neither processes nor
  reports a doctype declaration. The opt-in `protected()` / `strict()` profiles
  can forbid entity declarations or all DOCTYPEs, but that is a parser choice,
  not DTD validation.
- **One parse at a time per `Schema`.** The compiled schema holds per-parse
  indexes on a shared host, so concurrent parses against the same `Schema`
  are not supported.
- **XML 1.1 documents are not fully supported** (an expat limitation),
  though schema version *mode* 1.0/1.1 is.

## Using them together

The two are complements in a test suite: pyxsd's own conformance harness runs
`xmlschema` as an oracle over the W3C XSTS corpus and compares each case's
verdict, so the libraries are exercised side by side rather than treated as
mutually exclusive. If both are installed, `xmlschema`'s oracle can be
enabled with `PYXSD_RUN_ORACLE=1` for the conformance run.

## See also

- [Supported features](supported.md) — pyxsd's coverage and known gaps.
- [Data model](data-model.md) — the bound tree, `to_dict`/`to_json`, and the
  projection used by `xpath`/`find`.
- [xmlschema documentation](https://xmlschema.readthedocs.io/) — the
  authoritative description of the features attributed to it above.
