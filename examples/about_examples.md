# About the examples

Use the [example index](README.md) to choose a data source. Each family README
owns source fetching, supported schema versions, run commands, outputs, and
limitations. Scripts in the repository require a source checkout; installing
the package alone does not supply these directories.

## Application examples

GPX, WordprocessingML, and MusicXML use official schemas fetched by their
family's downloader. The separate [lax DOCX example](docx/lax/README.md) uses
deliberately imperfect input to illustrate best-effort binding. See the
[binding guide](../docs/binding.md) for retention limits.

## Healthcare workflows

The [healthcare README](healthcare/README.md) covers CDA/FHIR sources, conversion,
and MCP diagnosis. Base-XSD validity is separate from clinical/profile conformance.

## Rendering example families

The [rendering guide](../docs/rendering-examples.md) compares all rendering
families. CityGML, BPMN, OpenDRIVE, XRechnung, and MusicXML have pinned manifests,
license evidence, and family-local FastAPI servers. DOCX uses its own schema
fetcher and has no such server. There is no cross-family application server.

## Transform templates

| File | Pattern |
| --- | --- |
| [transform_template_fromTransform.py](transform_template_fromTransform.py) | Self-contained `Transform` subclass. |
| [transform_template_library.py](transform_template_library.py) | Reusable helper functions/classes. |
| [transform_template_fromLibrary.py](transform_template_fromLibrary.py) | Transform importing a sibling helper library. |

See [writing transforms](../docs/transforms/writing.md) for callable and class
conventions.

## Legacy application transforms (`legacy/`)

The crystallography transforms from pyxsd 0.1 are historical reading material;
their original data is no longer distributed and they are not installed with
the package. See [legacy/README.md](legacy/README.md) for their inventory.

## Sample transform file

[legacy/sampleTransformFile.txt](legacy/sampleTransformFile.txt) illustrates the
one-call-per-line format accepted by `-T` / `--transformFile`.
