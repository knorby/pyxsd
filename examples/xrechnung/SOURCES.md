# XRechnung example sources

Every external file the XRechnung examples consume is listed in
[`sources.toml`](sources.toml) with its origin URL, revision, SHA-256
digest, license, and role. Nothing listed here is committed to the
repository: `download_sources.py` verifies and installs into
`.cache/` (gitignored), and re-running it verifies the installed cache
without network access.

## Attribution and redistribution stance

| Section | Origin | License | Redistributed by this repo? |
| --- | --- | --- | --- |
| `ubl-schema` | [OASIS UBL 2.3 OS](https://docs.oasis-open.org/ubl/os-UBL-2.3/) XSD closure | OASIS copyright notice (below) | No — download-only |
| `ubl-data` | [KoSIT XRechnung test suite](https://github.com/itplr-kosit/xrechnung-testsuite) tag `v2026-08-31` | Apache-2.0 | No — download-only |

## Verbatim license evidence

### OASIS (UBL 2.3)

From `UBL-Invoice-2.3.xsd`:

> Copyright (c) OASIS Open 2021. All Rights Reserved.

The trailing grant in the same file permits copying and furnishing to
others "without restriction of any kind" as long as the copyright notice
and that paragraph travel with the copies, while the document itself may
not be modified. The schemas are installed byte-identical; no UBL file is ever
rewritten (the CityGML and MusicXML families localize their
`schemaLocation` URLs; see their `SOURCES.md`).

### KoSIT (XRechnung test suite)

Repository license: Apache-2.0 (`LICENSE` in `itplr-kosit/xrechnung-testsuite`).

## Data hygiene

- Instance documents are real upstream content. The XRechnung invoices
  are test fixtures authored for conformance testing, not personal data.
- Every download is digest-verified against `sources.toml` at install
  time, and re-verified against the same digests on later runs.
- If a license above changes or a digest stops matching, the downloader
  fails loudly rather than proceeding with unverified content.
