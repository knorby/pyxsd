# Healthcare examples

Real C-CDA documents, one pinned source manifest, three pyxsd workflows:

1. [Datatype tour](datatypes/README.md): identifiers, partial timestamps, codes, and exact decimal quantities through compile-time mixins.
2. [CDA → FHIR R4](cda_to_fhir/README.md): generated bindings on both sides, deterministic resource IDs, an unsupported-content report, and fresh output validation.
3. [Bad Data Doctor](bad_data_doctor/README.md): MCP validation, paged diagnostics, exact occurrence inspection, and reviewed candidate edits outside the server.

## Setup

Run from a pyxsd checkout with Python 3.11+ and `uv`:

```sh
uv run python examples/healthcare/download_sources.py
```

This verifies and installs CDA R2 and **FHIR R4 (4.0.1)** schemas below `.cache/`. Repeating the command verifies the installed cache without network access. Use `--force` to replace a corrupt cache. Source URLs, revisions, licenses, and SHA-256 digests are in [`sources.toml`](sources.toml); the default documents are committed unchanged with [CC0 provenance](documents/README.md). Schema ZIPs and generated output are not committed. The examples never use the conformance-test cache.

Optional `--include-onc` fetches the pinned ONC certification corpus. `--include-vendor` fetches the pinned vendor corpus only to your local cache; its redistribution terms remain unclear. Neither option is needed for the default workflows. Review optional inputs before using or sharing them.

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
