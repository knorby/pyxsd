# Upstream repositories and attribution

These examples consume the original repositories through pinned, SHA-256-verified
archives. The cache preserves their full trees, upstream documentation, and license
files. No upstream XML is vendored into the example workflows.

- [HL7/C-CDA-Examples](https://github.com/HL7/C-CDA-Examples), revision
  `ad5007abd912a45bbd04ce96e871c70954c2b2c2`: CC0-1.0. Includes both full
  documents and fragments. The named defaults select
  `Documents/Discharge Summary/Discharge_Summary.xml` and
  `Documents/CCD/CCD 2/CCD.xml` from this repository.
- [onc-healthit/2015-certification-ccda-testdata](https://github.com/onc-healthit/2015-certification-ccda-testdata),
  revision `4ac99c52ac99c998055c72c3120c52b10a29ebe7`: CC0. This is a certification
  test corpus, not a guarantee that every document passes the selected base schema.
- [chb/sample_ccdas](https://github.com/chb/sample_ccdas), revision
  `aca04c6c8febfa9b462543e7024f9bbfa4a9996b`: redistribution terms are unclear.
  Download and inspect locally; review upstream terms before sharing any files.

[`sources.toml`](sources.toml) records archive hashes, revisions, schema sources,
and the exact default-document URLs and hashes. Every discovery record carries
its repository, revision, license, and upstream file URL. Fetching again verifies
cached file digests; `--force` replaces a damaged cache from its pinned archive.

The only redistributed sample XML consists of two unchanged HL7 CC0 test fixtures,
with separate [test-fixture attribution](../../tests/fixtures/healthcare/README.md).
Those fixtures keep tests offline and are not used by the example commands.
