# BPMN example sources

Every external file the BPMN examples consume is listed in
[`sources.toml`](sources.toml) with its origin URL, revision, SHA-256
digest, license, and role. Nothing listed here is committed to the
repository: `download_sources.py` verifies and installs into
`.cache/` (gitignored), and re-running it verifies the installed cache
without network access.

## Attribution and redistribution stance

| Section | Origin | License | Redistributed by this repo? |
| --- | --- | --- | --- |
| `bpmn-schema` | OMG BPMN 2.0 XSDs via the [Camunda mirror](https://github.com/camunda/camunda-bpm-platform) @ `ee4826e5` | Apache-2.0 (mirror); schemas © Object Management Group | No — download-only |
| `bpmn-data` | [bpmn-js-examples](https://github.com/bpmn-io/bpmn-js-examples) @ `e6a99f5`, [bpmn-js](https://github.com/bpmn-io/bpmn-js) @ `e7b7435` | MIT | No — download-only |

## Verbatim license evidence

### bpmn.io (BPMN diagrams)

`bpmn-js` ships an MIT `LICENSE`; `bpmn-js-examples` states MIT in its
README. The Camunda schema mirror is Apache-2.0; the BPMN 2.0 XSDs
themselves are © Object Management Group — OMG's own site terms do not
clearly permit redistribution, which is why the mirror (where Apache-2.0
applies to the repository contents) is the pinned origin and the files
are still never committed.

## Data hygiene

- Instance documents are real upstream content: the bpmn.io diagrams
  are demo fixtures, not personal data.
- Every download is digest-verified against `sources.toml` at install
  time, and re-verified against the same digests on later runs.
- If a license above changes or a digest stops matching, the downloader
  fails loudly rather than proceeding with unverified content.
