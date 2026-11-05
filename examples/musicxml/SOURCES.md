# MusicXML example sources

Every external file the MusicXML examples consume is listed in
[`sources.toml`](sources.toml) with its origin URL, revision, SHA-256
digest, license, and role. Nothing listed here is committed to the
repository: `download_sources.py` verifies and installs into
`.cache/` (gitignored), and re-running it verifies the installed cache
without network access.

## Attribution and redistribution stance

| Section | Origin | License | Redistributed by this repo? |
| --- | --- | --- | --- |
| `musicxml-schema` | [W3C MusicXML 4.0](https://github.com/w3c-cg/musicxml) release zip | W3C Community Final Specification Agreement | No — download-only |

This family additionally reads the committed public-domain Bach chorale
(`instance.xml`) that already ships with the long-standing MusicXML
example.

## Verbatim license evidence

### W3C Music Notation Community Group (MusicXML 4.0)

From `schema/musicxml.xsd` in the release zip:

> Copyright © 2004-2021 the Contributors to the MusicXML Specification,
> published by the W3C Music Notation Community Group under the W3C
> Community Final Specification Agreement (FSA)

The zip carries no separate `LICENSE` file; the XSD header is the license
evidence. The schema's absolute `xml.xsd`/`xlink.xsd` imports are
rewritten to the zip's own bundled copies at install time (pristine bytes
are retained under `.cache/.../.pristine/` for verification).

## Data hygiene

- Instance documents are real upstream content: the committed chorale
  is a public-domain score, not personal data.
- Every download is digest-verified against `sources.toml` at install
  time, and re-verified against the same digests on later runs.
- If a license above changes or a digest stops matching, the downloader
  fails loudly rather than proceeding with unverified content.
