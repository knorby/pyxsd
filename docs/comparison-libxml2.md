# pyxsd compared with libxml2

[libxml2][libxml2-readme] is an XML toolkit implemented in C. Its build
documentation identifies the schema module as **XML Schemas 1.0** support;
**XSD 1.1 is unsupported**. The upstream [schema API header][schema-header]
also describes its XML Schemas implementation as incomplete. Neither the
presence of a schema API nor a high test-suite agreement rate establishes
complete standards conformance.

pyxsd defaults to XSD 1.1 and offers an explicit XSD 1.0 mode. This page
compares scope and interfaces, then records one dated validity-verdict probe.
It is not a performance benchmark or a ranking of all releases.

## Scope, APIs, and resources

| Dimension | pyxsd | libxml2 |
| --- | --- | --- |
| Implementation | Pure Python base with `elementpath` and `defusedxml` | C library; capabilities depend on build configuration |
| XSD modes | XSD 1.1 default; optional `xsd_version="1.0"` | XSD 1.0; XSD 1.1 unsupported |
| Main model | Compile a schema, bind instances to generated Python classes, query/export/transform the bound tree | Compile a schema and validate XML through native tree, file, or streaming/SAX interfaces |
| Validation interface | `Schema.report`, `Document.report`, and `ValidationReport` | Schema parser/validation contexts, return codes, and error callbacks |
| Streaming | No lazy/streaming instance mode | `xmlSchemaValidateStream` and `xmlSchemaSAXPlug`, alongside `xmlSchemaValidateDoc` |
| Resource model | Local-only default; trusted custom `ResourcePolicy` or opt-in HTTPS `RemoteResourcePolicy` | Parser options, resource loaders, and XML catalogs; available controls depend on release and integration |

libxml2's [schema API][schema-header] and [catalog documentation][catalog-api]
describe its native interfaces. Those interfaces are not pyxsd's generated
Python binding model. A Python wrapper can add its own APIs and loading
policies; this comparison does not measure such wrappers.

Resource policy must be evaluated separately from schema validity. pyxsd's
default can read local schema paths named by input; local-only is not path
confinement. Its parser protections and finite budgets are opt-in, and it is
not a sandbox; see {doc}`xml-security`. For libxml2, inspect the exact deployed
release, parser options, loaders, catalogs, and wrapper rather than assuming a
network or filesystem policy from a validity result. The probe below disabled
network access; that is a property of the probe, not a statement about every
libxml2 application.

## Preliminary macOS libxml2 2.9.13 probe

**Measured 2026-10-31; preliminary evidence about this build and adapter only.**
The local probe used macOS system libxml2 **2.9.13** through its C schema API,
alongside pyxsd **1.2.1** at source revision
`d79079fedb4f880a1e4df46981dedf52beab9c14`. Both used the XSD 1.0 case catalog
and outcome rules for the main `suite.xml` at
[`w3c/xsdtests` revision `7bc3365c652a322f3d762021b3879eb92dae7e30`][corpus]:
41,735 emitted cases per engine. This does not include `extra-suite.xml`.

Network access was disabled. Local catalog mappings supplied only W3C's
standard `xml.xsd` and `xlink.xsd`, so those resources did not depend on live
HTTP retrieval. Schema compilation calls that did not return were isolated
at the process level.

| Outcome | libxml2 2.9.13 probe | Paired pyxsd 1.2.1 run |
| --- | ---: | ---: |
| `pass` | 38,783 | 39,285 |
| `fail` | 618 | 153 |
| Checked (`pass + fail`) | 39,401 | 39,438 |
| `adapter-gap` | 56 | 24 |
| `timeout` | 5 | 0 |
| `not-applicable` | 2,258 | 2,258 |
| `not-checkable` | 15 | 15 |
| False accepts | 490 | 121 |
| False rejects | 128 | 32 |

Raw checked-case agreement for **this libxml2 probe** was
**98.4315% = 100 × 38,783 / 39,401**, rounded to four decimal places. Current
pyxsd agreement and baseline provenance remain in the generated tables in
{doc}`xsts`, not in a second hand-maintained headline here.

The checked denominators differ because adapter gaps and timeouts differ.
Neither is a validity verdict, and neither enters `pass + fail`. Failure
directions compare an engine's actual verdict with the selected corpus
expectation; they are not determined by what the other engine says.

Pairing the same case IDs provides additional evidence:

| Paired outcome | Cases |
| --- | ---: |
| Both engines match the corpus (`pass` / `pass`) | 38,729 |
| pyxsd matches, libxml2 disagrees (`pass` / `fail`) | 519 |
| libxml2 matches, pyxsd disagrees (`fail` / `pass`) | 53 |
| Both engines disagree (`fail` / `fail`) | 95 |

These rows cover only cases checked by both engines, not either engine's full
denominator. Here, `pass` means agreement, **not acceptance of the document**;
correct rejection of an invalid case is also a pass. The 53 cases where
libxml2 matches and pyxsd disagrees are useful residual-adjudication inputs,
but neither implementation is normative truth. Disputes need the corpus
metadata and the relevant [XSD specification][xsd10].

### Timeout and reproducibility limits

The probe recorded five timeouts across `particlesZ012`, `particlesZ015`, and
`particlesZ020`, including dependent instances, after libxml2 schema
compilation did not return. A durable adapter needs **per-case process-level
timeouts**: a Python exception or signal cannot reliably interrupt a blocking
C call. These probe timeouts are not invalid-schema verdicts or evidence about
every libxml2 release.

The probe is not a maintained, pinned-build comparison gate. A durable
measurement needs a pinned libxml2 build and maintained adapter with the same
catalog/applicability and schema-bundle rules, an offline standard-resource
catalog, per-case process timeouts, and recorded build/run provenance. No
libxml2 runtime dependency or default adapter is added to pyxsd.

## What was not measured

| Dimension | Evidence from this comparison |
| --- | --- |
| Top-level schema/instance verdicts | Preliminary XSD 1.0 probe above |
| Throughput, latency, or memory use | Not measured |
| Diagnostic quality or source-location accuracy | Not measured |
| PSVI correctness or completeness | Not measured |
| Security or resistance to malicious inputs | Not measured |
| libxml2 XSD 1.1 agreement | Unsupported, not a zero score |
| Other libxml2 releases, Xerces, or Saxon | Not measured |

C versus Python, or streaming versus tree processing, does not establish a
performance advantage without a separate workload and benchmark. Likewise,
XSTS top-level verdict agreement does not test complete PSVI output,
diagnostics, or security. The [upstream suite README][xsts-readme] states the
verdict-only scope. Do not extrapolate the 2.9.13 result to newer builds,
wrappers, or XSD 1.1.

## See also

- {doc}`xsts` — shared methodology, raw outcomes, and current generated results.
- {doc}`xsd-validation` — validation phases, language versions, and PSVI limits.
- {doc}`supported` — pyxsd's feature inventory and known gaps.
- {doc}`comparison-xmlschema` — Python API and feature-selection comparison.
- {doc}`xml-security` — pyxsd parser and resource-policy boundaries.

[libxml2-readme]: https://github.com/GNOME/libxml2/blob/master/README.md
[schema-header]: https://github.com/GNOME/libxml2/blob/master/include/libxml/xmlschemas.h
[catalog-api]: https://gnome.pages.gitlab.gnome.org/libxml2/html/catalog_8h.html
[corpus]: https://github.com/w3c/xsdtests/tree/7bc3365c652a322f3d762021b3879eb92dae7e30
[xsts-readme]: https://github.com/w3c/xsdtests/blob/7bc3365c652a322f3d762021b3879eb92dae7e30/README.md#running-the-tests
[xsd10]: https://www.w3.org/TR/xmlschema-1/
