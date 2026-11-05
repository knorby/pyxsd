# CityGML example sources

Every external file the CityGML examples consume is listed in
[`sources.toml`](sources.toml) with its origin URL, revision, SHA-256
digest, license, and role. Nothing listed here is committed to the
repository: `download_sources.py` verifies and installs into
`.cache/` (gitignored), and re-running it verifies the installed cache
without network access.

## Attribution and redistribution stance

| Section | Origin | License | Redistributed by this repo? |
| --- | --- | --- | --- |
| `citygml-schema` | [OGC CityGML 2.0 schema archive](https://schemas.opengis.net/citygml/citygml-2_0_0.zip) | OGC copyright notice (below) | No — download-only |
| `citygml-gml` | [schemas.opengis.net](https://schemas.opengis.net/) GML 3.1.1 closure, w3.org `xml.xsd`/`xlink.xsd`, [OASIS xAL](https://docs.oasis-open.org/election/external/xAL.xsd) | OGC / W3C / OASIS notices in each file | No — download-only; byte-identical files, digests pinned |

## Verbatim license evidence

### OGC (CityGML schemas, GML 3.1.1 closure)

From `https://schemas.opengis.net/citygml/2.0/ReadMe.txt`:

> The CityGML 2.0.0 standard is defined in the OGC document 12-019.
> Policies, Procedures, Terms, and Conditions of OGC(r) are available
> http://www.opengeospatial.org/ogc/legal/ .
> Copyright (c) 2012 Open Geospatial Consortium.

OGC publishes no SPDX identifier for the schema files. Long-standing
practice (citygml4j, 3D City Database, val3dity, and others) treats the
files as redistributable verbatim with the notice kept; this project takes
the more conservative route of fetching at run time and never
redistributing. Do not label these files CC0 or public domain.

## Data hygiene

- Instance documents are real upstream content: the OGC LOD1/LOD2
  buildings are example datasets, not personal data.
- Every download is digest-verified against `sources.toml` at install
  time, and re-verified against the same digests on later runs.
- If a license above changes or a digest stops matching, the downloader
  fails loudly rather than proceeding with unverified content.
