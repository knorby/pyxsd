# Healthcare examples

Real C-CDA documents, one pinned source manifest, three pyxsd workflows:

1. [Datatype tour](datatypes/README.md): identifiers, partial timestamps, codes, and exact decimal quantities through compile-time mixins.
2. [CDA → FHIR R4](cda_to_fhir/README.md): generated bindings on both sides, deterministic resource IDs, an unsupported-content report, and fresh output validation.
3. [Bad Data Doctor](bad_data_doctor/README.md): MCP validation, paged diagnostics, exact occurrence inspection, and reviewed candidate edits outside the server.

## Setup

Run from a pyxsd checkout with Python 3.11+ and `uv`:

```sh
uv run python examples/healthcare/download_sources.py --all-corpora
```

This verifies and installs CDA R2 and **FHIR R4 (4.0.1)** schemas and all three complete upstream repositories below `.cache/`. Repeating the command verifies the installed cache without network access. Use `--force` to replace a corrupt cache. Source URLs, revisions, licenses, and SHA-256 digests are in [`sources.toml`](sources.toml), with [attribution and redistribution notes](SOURCES.md). Downloaded repositories, schemas, and generated output are not committed. The examples never use the conformance-test cache.

Omit `--all-corpora` to fetch only schemas and the HL7 repository. `--include-onc` and `--include-vendor` also work independently. Vendor redistribution terms remain unclear: the repository is downloaded locally, never redistributed by these examples. Review inputs before using or sharing them.

## Discover and use repository inputs

```sh
# List complete CDA documents from all three repositories, with attribution.
uv run python examples/healthcare/corpus.py --repo all
# Explore fragments, non-CDA XML, and malformed/unsafe inputs too.
uv run python examples/healthcare/corpus.py --repo hl7 --kind all --match allergy
uv run python examples/healthcare/corpus.py --repo onc --kind document
uv run python examples/healthcare/corpus.py --repo vendor --kind document
```

Each JSON record includes a `selector`, upstream URL, revision, license, root name, and input kind. Discovery is not schema validation: a `document` has the namespace-qualified CDA root, not a clean conformance verdict. Unsafe or oversized inputs are reported, not expanded. CDA-like fragments without a default namespace are also classified as fragments, never promoted to complete documents or silently given a namespace. Snippets with unbound prefixes remain malformed XML with their parser error. Fragments are listed separately because they cannot be parsed as complete CDA documents.

Pass a selector verbatim to any workflow; quote it when the path contains spaces:

```sh
uv run python examples/healthcare/datatypes/inspect_document.py "hl7:Documents/Discharge Summary/Discharge_Summary.xml"
uv run python examples/healthcare/cda_to_fhir/convert.py "hl7:Documents/Discharge Summary/Discharge_Summary.xml" --output-dir examples/healthcare/out
uv run --extra mcp python examples/healthcare/bad_data_doctor/doctor.py --document "hl7:Documents/CCD/CCD 2/CCD.xml"
```

Use `onc:<path>` or `vendor:<path>` selectors from the inventory in exactly the same way. Explicit local paths work too, including files in an existing clone. The converter remains a narrow mapping and may reject other documents; use the doctor to inspect source errors rather than expecting every corpus input to convert.

The named defaults `discharge` and `ccd-with-issues` select unchanged files **inside the fetched HL7 repository**, not bundled example copies. Tests alone retain two attributed offline fixtures under `tests/fixtures/healthcare/`; the examples do not read them.

The default Discharge Summary maps to nine resources: Composition, Patient, two Conditions, and five Observations (one panel, four measurements). Nineteen other sections are reported as unsupported. The imperfect CCD supplies order and extension findings for diagnosis, not a document to “fix” by deleting unknown markup.

## Validation boundary

**These are teaching examples, not clinical software.** Base CDA XSD validation is not C-CDA template/Schematron validation. Fresh base FHIR R4 XSD validation is not FHIRPath, terminology, US Core, or C-CDA on FHIR profile validation. The conversion is deliberately incomplete; a successful Bundle does not establish clinical completeness. Do not use private patient data in this public example or send it to an AI service.

After setup, datatype inspection and conversion work offline with base pyxsd dependencies. The doctor needs the optional `mcp` extra but no AI key or trusted-local execution. See each workflow for its exact command and limitations.

## Tests

```sh
uv run pytest tests/test_healthcare_*.py -q
uv run --extra mcp pytest tests/test_healthcare_mcp.py -q
```

Offline tests cover pinned document integrity, safe download installation, datatype semantics, mapping guards, and protocol behavior. Real-schema tests skip with setup instructions if schemas are absent. See [`common/`](common/README.md) for the shared helper boundaries.
