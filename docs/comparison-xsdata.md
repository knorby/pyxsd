# pyxsd compared with xsdata

```{eval-rst}
.. meta::
   :description: Compare pyxsd and xsdata for XSD validation, runtime and generated Python bindings, XML and JSON serialization, diagnostics, and MCP workflows.
```

Choose pyxsd when XSD validation, structured findings, and a schema-defined
Python object tree belong in the same runtime workflow. Choose
[`xsdata`](https://github.com/tefra/xsdata) when generated dataclasses,
binding metadata, and XML or JSON serialization are the deliverables you
want to maintain. Both libraries bind XML to Python objects, but they start
from different application models.

pyxsd compiles XSD declarations into classes at runtime. It validates and
binds an instance to those classes, then supports queries, transforms, and
exports from the resulting `Document`. xsdata generates Python dataclasses
and binding metadata from schemas or other inputs; its runtime parsers bind
XML or JSON into those classes and its serializers write them back out.

This page compares interfaces and evidence, not performance. The W3C results
below come from different harness operations and populations. They are not
two scores on one validator benchmark.

## At a glance

| Task or property | pyxsd | xsdata |
| --- | --- | --- |
| Primary model | Compile XSD at runtime, validate XML, and bind a schema-defined `Document` tree | Generate dataclasses and binding metadata; parse and serialize XML or JSON objects |
| Binding source | Runtime-generated classes; optional `.pyi` stubs for editors and type checkers | Generated Python source is the normal binding artifact |
| XSD versions | XSD 1.1 by default; explicit XSD 1.0 mode | Generator input includes XSD 1.0 and 1.1; see its documentation for supported constructs |
| Validation | Direct schema and instance validation with a `ValidationReport`, stable issue codes, phases, and optional occurrence locations | Data binding and generation are the primary APIs; its native W3C harness delegates output validation to xmlschema |
| Parsing | Bind against the compiled schema without a generated application module | Bind to a target class or discover one through the model context; native and lxml handlers are available |
| XML output | Write an existing bound tree; call `revalidate()` explicitly after edits | Serialize objects with namespace, wrapper, and mixed-content controls; native and lxml writers are available |
| JSON | `to_json()` exports a bound document | JSON parsers and serializers bind generated models in both directions |
| Generator inputs | XSD | XSD, DTD, WSDL 1.1/SOAP 1.1, XML, and JSON |
| Application extensions | Compile-time mixins and validation hooks on schema-derived classes | Ordinary generated dataclasses and configurable binding metadata |
| Data retention and repair | Strict or lax binding modes separate retained data from validation findings | Wildcard and generic models, plus unknown-property controls, support configurable binding behavior |
| Agent interface | CLI, Python API, and an optional documented local stdio MCP server | Official documentation presents a Python API and CLI |
| Other integrations | Optional schema-derived Pydantic models and Arrow/Parquet projections | XML, JSON, and Python source serialization through documented handlers, writers, and converters |

For exact pyxsd coverage, see {doc}`supported`, {doc}`validation`, and
{doc}`binding`. For xsdata's model-generation and binding options, use its
[documentation](https://xsdata.readthedocs.io/).

## Where pyxsd helps

### Validation and binding share one result

A normal pyxsd parse checks the XML against the compiled schema and returns
bound objects with XSD-derived types. Its report retains recoverable schema
and instance findings instead of reducing validation to a Boolean or one
exception. `Document.locate()` connects supported findings to exact source
occurrences, while `require_valid()` gives a pipeline an explicit validity
gate.

Lax binding can retain more input for inspection and repair without hiding
findings. After programmatic edits, `revalidate()` produces a freshly checked
document and report. This combination suits applications that need to inspect
invalid input, not just accept or reject it. See {doc}`validation` and
{doc}`data-model`.

### The schema is the runtime application model

pyxsd needs no generated application module. XSD type derivation becomes
Python inheritance, simple values use XSD datatype classes, and mixins attach
application behavior when the schema compiles. Queries return nodes from the
same bound tree that transforms consume. Recompiling a changed schema replaces
the runtime model; optional {doc}`stubs` describe it to static tools.

This differs from a workflow built around checking generated dataclasses into
a project. Prefer pyxsd when schema-driven validation and object behavior
should stay together at runtime. Prefer checked-in source when reviewing and
distributing that source is itself a requirement.

### Agents can inspect bounded evidence

The optional {doc}`mcp` server exposes local schema compilation, validation,
queries, transforms, and exports over stdio. Its structured reports, exact
occurrences, paging, bounded responses, and retained artifacts let an agent
follow a finding without loading an entire document into context.

The standalone {doc}`cli` covers shell pipelines and CI without requiring an
MCP dependency. pyxsd also packages skills for its Python, CLI, and MCP
workflows; see {doc}`skills`. xsdata's official documentation presents its
Python API and CLI. This page makes no claim about third-party xsdata agent
integrations.

## Where xsdata is a better fit

- **Generated dataclasses as source.** xsdata's generator produces explicit
  classes and field metadata to inspect, review, version, and distribute.
  pyxsd generates its implementation classes at runtime and emits typing
  stubs, not an equivalent static application model.
- **XML and JSON object serialization.** xsdata documents parsers and
  serializers for both formats, including native and lxml handlers and
  writers. pyxsd exports a bound document to JSON but does not provide the
  same general object-codec API.
- **More generator inputs.** xsdata can generate models from XSD, DTD, WSDL
  1.1/SOAP 1.1, XML samples, and JSON samples. pyxsd's compiler consumes XSD.
- **Configurable model generation.** xsdata's generator and binding metadata
  suit projects that need a tailored source model rather than classes derived
  directly during each schema compilation.
- **Wildcard-oriented binding.** xsdata documents wildcard and generic models,
  XInclude processing, and unknown-property controls. Evaluate those features
  against your schema instead of treating pyxsd's validation report and lax
  retention as the same mechanism.

## W3C xsdtests evidence

Both projects' harnesses pin W3C xsdtests revision
[`7bc3365c652a322f3d762021b3879eb92dae7e30`](https://github.com/w3c/xsdtests/tree/7bc3365c652a322f3d762021b3879eb92dae7e30).
They do not execute the same operation or score the same population.

| Evidence | Operation and denominator | Observed result |
| --- | --- | --- |
| pyxsd checked-in XSD 1.1 main-suite baseline | Compare catalogued expected schema and instance validity verdicts; agreement is `100 * pass / (pass + fail)` | 41,530 pass, 75 fail, 41,605 checked: **99.8197% agreement**; 41,735 total emitted cases |
| xsdata native XSD-mode run | Import committed generated models, parse and serialize valid binding cases, then validate serialized output with xmlschema; native rate is `100 * passed / (passed + failed)` | 14,600 passed, 5 failed, 87 skipped: **99.9658% native pass/(pass+fail)**; 14,692 collected tests |

**Do not rank these percentages.** pyxsd's number is expected-verdict
agreement over its checked XSTS population. xsdata's number is a native
binding-harness pass rate over a smaller, differently filtered population.
No shared-case differential was run.

The [xsdata harness README][harness-readme] says that about 12,000
invalid-schema tests are ignored because xsdata is "not really a validator."
Its XSD mode uses committed generated models rather than running the
generator. The [harness implementation][harness-utils] constructs an
xmlschema `XMLSchema10` or `XMLSchema11` validator for serialized output. The
README also warns that the check does not establish that every source element
and value survived. Consequently, this run is evidence about the tested
binding path, not standalone xsdata schema validation or full round-trip
fidelity.

### Reproduction and provenance

The xsdata run used `xsdata-w3c-tests` commit
[`6b845568ea42a2e9540cffe07c4cc48dbad86b7c`][harness-readme] and installed
xsdata 26.10 from commit
[`17ca8c8e754e9a9a7d92827a1f40cc24e4065e4e`][xsdata-readme]. The environment
was Python 3.13.11 on macOS arm64 with pytest 9.2.0, pytest-xdist 3.8.0,
xmlschema 4.3.2, and lxml 6.1.3. Following the upstream command, `--runxfail`
reported xfail cases as ordinary results:

```bash
git clone --recurse-submodules https://github.com/tefra/xsdata-w3c-tests.git
cd xsdata-w3c-tests
uv venv .venv --python 3.13
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m pytest -n 4 --mode xsd tests --runxfail
```

The five failures and 87 skips remained failures and skips; they were not
removed from the reported result. The skips comprised 71 original instances
that failed xmlschema validation and 16 schemas that xmlschema could not
parse. The JUnit report independently recorded 14,692 tests, five failures,
zero errors, and 87 skips.

The pyxsd figures come from the existing {doc}`xsts` baseline, produced with
pyxsd 1.2.1 on 2026-11-01; the suite was not rerun for this comparison. Its
non-verdict outcomes remain outside the checked denominator. Read that page
for the case-selection, scoring, and baseline-update contract.

Neither result measures performance, memory use, diagnostic quality, complete
PSVI behavior, security, W3C certification, or correctness on untested inputs.

## Limitations of pyxsd

- **No static dataclass generator.** Optional stubs support static analysis;
  they do not replace xsdata's generated application source.
- **No WSDL or DTD model generator.** pyxsd compiles XSD, not the wider set of
  xsdata generator inputs.
- **No general JSON-to-object binding API.** `to_dict()` and `to_json()` export
  a bound XML document; they are not xsdata-style bidirectional JSON codecs.
- **A full bound tree is retained.** pyxsd does not provide xsdata's choice of
  native and lxml parsing handlers or a general streaming object-binding API.
  One `Schema` also supports only one parse at a time.
- **Serialization is not a fidelity guarantee.** Writing a bound pyxsd tree
  emits XML from the current model, not a byte-for-byte copy of its source.
  Revalidate after edits and test the constructs your application uses.

## See also

- {doc}`why-pyxsd` — the schema-driven workflow and its intended uses.
- {doc}`xsts` — pyxsd's XSTS methodology and reviewed results.
- {doc}`mcp` — the optional local agent interface and its response limits.
- {doc}`comparison-xmlschema` — converter-based validation and data bindings.
- {doc}`comparison-lxml` — general XML trees and objectify bindings.
- [xsdata documentation](https://xsdata.readthedocs.io/) — authoritative
  generator, parser, serializer, and configuration guidance.

[harness-readme]: https://github.com/tefra/xsdata-w3c-tests/blob/6b845568ea42a2e9540cffe07c4cc48dbad86b7c/README.md
[harness-utils]: https://github.com/tefra/xsdata-w3c-tests/blob/6b845568ea42a2e9540cffe07c4cc48dbad86b7c/tests/utils.py
[xsdata-readme]: https://github.com/tefra/xsdata/blob/17ca8c8e754e9a9a7d92827a1f40cc24e4065e4e/README.md
