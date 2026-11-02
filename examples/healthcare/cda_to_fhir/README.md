# Scoped CDA → FHIR R4 XML

From the repository root:

```sh
uv run python examples/healthcare/download_sources.py
uv run python examples/healthcare/cda_to_fhir/convert.py examples/healthcare/documents/hl7-discharge-summary.xml --output-dir examples/healthcare/out
```

The unchanged [HL7 CC0 Discharge Summary](../documents/README.md) produces a document Bundle with **one Composition, one Patient, two Conditions, and five Observations** (a vital-sign panel and four measurements). The report records **19 unsupported sections**. The output names are `hl7-discharge-summary.fhir.xml` and `hl7-discharge-summary.report.json`. Repeating the command produces byte-identical XML. Multiple distinct input stems can be passed in one invocation; both schemas compile only once.

## What is mapped

- Document ID, title, type, effective time, and the first usable human author reference.
- Patient identifiers, name, gender, and a partial or full birth date.
- Problem List (`11450-4`): active `ACT` event acts with one non-negated, non-inverted `SUBJ` relationship that conducts context, and one event observation with one supported coded value.
- Vital Signs (`8716-3`): completed event panels and non-negated event measurements with one quantity each, coded names, units, and explicit effective times.

Resources are built with generated R4 classes, not string templates. Identity combines the document ID and occurrence path, so repeated source IDs do not collide. All resource references resolve inside the Bundle; Composition is first. Timestamp lexical padding is reported with the source precision; no timezone is invented.

## Limits and input policy

This is a teaching example, **not a clinical converter**. It does not map every field even within a supported entry. No clinical completeness, C-CDA template, US Core, or C-CDA on FHIR profile conformance is claimed. Fresh output validation checks **base FHIR R4 XSD only**, not FHIRPath invariants, terminology bindings, or Schematron. The [HL7 mapping guide](https://www.hl7.org/fhir/us/ccda/) informs the choice of R4, not a claim of guide compliance.

Unknown code systems, unsupported entry shapes/statuses, and `nullFlavor` values are recorded rather than invented or silently converted. Source findings remain in the report. Missing required document/patient/author values, malformed XML, unbound roots, and failed fresh output validation stop conversion before any output is written. A batch is validated entirely before publication.

Null acts/observations, explicit alternate subjects, proposed panels, and repeated observation values are outside this slice: the affected entry or measurement is excluded and reported. The converter does not override an explicit subject with the record-target patient or truncate multiple values. A null gender code is omitted even if its `code` attribute remains. Document identifiers need a recognized OID or hyphenated UUID root; local RUIDs are not invented global namespaces.

By default any source error stops conversion. After manually checking that a specific `order` or `unexpected-element` finding is outside the selected mapping, its exact printed `code:/expanded-name[index]/...` key can be passed with repeatable `--allow-source-issue` arguments. This is an explicit operator judgment, not proof that the finding is harmless. Do not allow a finding affecting mapped content. Other error classes cannot be bypassed. For real imperfect data start with [Bad Data Doctor](../bad_data_doctor/README.md); never delete extensions merely to obtain a clean report.

Generated files and downloaded schemas are ignored by Git. Do not use private patient records in this public example or upload them to an AI service.
