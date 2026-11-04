# musicxml example sources

Every external file the musicxml examples consume is listed in
[`sources.toml`](sources.toml) with its origin URL, revision, SHA-256
digest, license, and role. Nothing listed here is committed to the
repository: `download_sources.py` verifies and installs into
`.cache/` (gitignored), and re-running it verifies the installed cache
without network access.

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
