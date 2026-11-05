# OpenDRIVE example sources

Every external file the OpenDRIVE examples consume is listed in
[`sources.toml`](sources.toml) with its origin URL, revision, SHA-256
digest, license, and role. Nothing listed here is committed to the
repository: `download_sources.py` verifies and installs into
`.cache/` (gitignored), and re-running it verifies the installed cache
without network access.

## Attribution and redistribution stance

| Section | Origin | License | Redistributed by this repo? |
| --- | --- | --- | --- |
| `opendrive-schema` | ASAM OpenDRIVE XSDs via [asam-ev/qc-opendrive](https://github.com/asam-ev/qc-opendrive) @ `8baf0a5` | MPL-2.0; © ASAM e.V. | No — download-only |
| `opendrive-data` | [carla-simulator/opendrive-test-files](https://github.com/carla-simulator/opendrive-test-files) @ `5929b67` (MIT), [esmini](https://github.com/esmini/esmini) `resources/xodr` @ `d1d4423` (MPL-2.0) | MIT / MPL-2.0 | No — download-only |

## Verbatim license evidence

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

## Data hygiene

- Instance documents are real upstream content: the CARLA and esmini
  networks are simulator test roads, not personal data.
- Every download is digest-verified against `sources.toml` at install
  time, and re-verified against the same digests on later runs.
- If a license above changes or a digest stops matching, the downloader
  fails loudly rather than proceeding with unverified content.
