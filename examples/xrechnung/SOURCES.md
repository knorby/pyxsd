# xrechnung example sources

Every external file the xrechnung examples consume is listed in
[`sources.toml`](sources.toml) with its origin URL, revision, SHA-256
digest, license, and role. Nothing listed here is committed to the
repository: `download_sources.py` verifies and installs into
`.cache/` (gitignored), and re-running it verifies the installed cache
without network access.

### OASIS (UBL 2.3)

From `UBL-Invoice-2.3.xsd`:

> Copyright (c) OASIS Open 2021. All Rights Reserved.

The trailing grant in the same file permits copying and furnishing to
others "without restriction of any kind" as long as the copyright notice
and that paragraph travel with the copies, while the document itself may
not be modified. The schemas are installed byte-identical; the only
rewrite anywhere in this family is on CityGML/MusicXML `schemaLocation`
URLs, never on UBL files.

### ASAM (OpenDRIVE schemas)

From `asam-ev/qc-opendrive` `LICENSE`:

> Mozilla Public License Version 2.0
> Copyright (C) 2024 ASAM e.V.

ASAM's own repository redistributes the per-version OpenDRIVE XSDs under
MPL-2.0; pulling from there means ASAM is the redistributor. MPL-2.0 is
file-level copyleft — another reason these files are fetched, not
committed.

### CARLA (OpenDRIVE sample networks)

From `opendrive-test-files` `LICENSE`:

> MIT License
> Copyright (c) 2019 Computer Vision Center (CVC) at the Universitat
> Autonoma de Barcelona (UAB).

### esmini (OpenDRIVE sample networks)

Repository license: Mozilla Public License 2.0 (GitHub API
`"spdx_id": "MPL-2.0"`). The specific `.xodr` files used here are fetched
from the pinned revision with their digests recorded.

### bpmn.io (BPMN diagrams)

`bpmn-js` ships an MIT `LICENSE`; `bpmn-js-examples` states MIT in its
README. The Camunda schema mirror is Apache-2.0; the BPMN 2.0 XSDs
themselves are © Object Management Group — OMG's own site terms do not
clearly permit redistribution, which is why the mirror (where Apache-2.0
applies to the repository contents) is the pinned origin and the files
are still never committed.

### W3C Music Notation Community Group (MusicXML 4.0)

From `schema/musicxml.xsd` in the release zip:

> Copyright © 2004-2021 the Contributors to the MusicXML Specification,
> published by the W3C Music Notation Community Group under the W3C
> Community Final Specification Agreement (FSA)

The zip carries no separate `LICENSE` file; the XSD header is the license
evidence. The schema's absolute `xml.xsd`/`xlink.xsd` imports are
rewritten to the zip's own bundled copies at install time (pristine bytes
are retained under `.cache/.../.pristine/` for verification).

### KoSIT (XRechnung test suite)

Repository license: Apache-2.0 (`LICENSE` in `itplr-kosit/xrechnung-testsuite`).

## Data hygiene

- Instance documents are real upstream content. The XRechnung invoices
  are test fixtures authored for conformance testing, not personal data.
- Every download is digest-verified against `sources.toml` at install
  time, and re-verified against the same digests on later runs.
- If a license above changes or a digest stops matching, the downloader
  fails loudly rather than proceeding with unverified content.
