# Command-line reference

```{code-block} text
:class: no-copy

pyxsd [options]
```

`pyxsd` is the console script installed by the package (equivalent to
`python -m pyxsd`).

## Flags

| Flag | Long form(s) | Meaning |
| ---- | ------------ | ------- |
| `--version` | | Print the version and exit. |
| `-i FILE` | `--inputXml` | Filename of the XML document to read in. Reads from **stdin** by default. |
| `-s FILE` | `--inputXsd`, `--schema` | Filename of the XSD schema. By default pyxsd tries to determine the location from the instance document (schema hints). |
| `-p FILE` | `--parsedXml`, `--parsedOutput` | Filename for parsed (pre-transform) output when `-k` is enabled. Default: input stem + `Parsed.xml` beside the input. |
| `-k` | `--ParsedFile` | Write the parsed tree even when transforms are given. Off by default. |
| `-o FILE` | `--transformOutput` | Output after parsing and transforming. **stdout** by default. `stdout` may also be given explicitly as the value. |
| `-d` | `--useDefaultFile` | Use the default filename for transformed output instead of stdout. |
| `-t CALLS` | `--transform` | Transform call(s): `Class(args...)`, chained with `>` when multiple. |
| `-T FILE` | `--transformFile` | File with transform calls, one per line. Mutually exclusive with `-t`. |
| `-c FILE` | `--overlayClassesFile` | *Experimental.* Overlay class file that extends/overrides generated classes. |
| `--mode` | | Binding mode: `strict` (default) reports invalid values and drops them; `lax` retains best-effort values (raw strings, generic subtrees), subject to fidelity limits. See {doc}`binding`. |
| `--namespaces` | | Namespace handling: `legacy` (default) matches by local name and ignores namespace URIs; `strict` resolves QNames and matches by expanded name. See {doc}`binding`. |
| `--xsd-version VERSION` | | XSD version to process: `1.1` (default) or `1.0`. In `1.0` mode the XSD 1.1-only vocabulary is rejected (`xsd11-construct`) and the 1.0/1.1 semantic differences are applied. See {doc}`supported`. |
| `--emit-stubs [PATH]` | | Compile the schema and write a `.pyi` type stub for its generated classes to `PATH` — or to **stdout** when `PATH` is omitted — then exit without reading any XML input. Requires `-s`. See {doc}`api`. |
| `--stubs-compact` | | Emit the compact, agent-oriented stub description instead of the full `.pyi` document. Requires `--emit-stubs`. |
| `-v` | `--verbose` | Verbose logging (DEBUG). |
| `-q` | `--quiet` | Quiet logging (CRITICAL). Validation issues are still printed. `-v` and `-q` are mutually exclusive. |
| `--strict` | | Exit with status 1 if the validation report contains errors. |

## Exit codes

- `0` — run completed (validation issues are non-fatal).
- `1` — `--strict` and the report contains errors, or a fatal error occurred
  (missing schema, unreadable input, malformed schema).
- `2` — command-line usage error (argparse).

## Schema location

When `-s` is omitted, pyxsd reads the instance document first and looks for
schema hints — a `xsi:noNamespaceSchemaLocation` attribute or a
`xsi:schemaLocation` pair — and resolves the schema path relative to the
instance file. A malformed hint produces a warning (`schema-hint` code); no
hint at all produces an error unless `-s` is given.

## Type stubs

```sh
pyxsd --emit-stubs inventory.pyi -s inventory.xsd
```

This schema-only mode reads no instance and prints no report. It writes full
`.pyi` text, or a compact description with `--stubs-compact`; omitting the output
path writes to stdout. Compilation options (`--mode`, `--namespaces`,
`--xsd-version`, and `-c`) apply normally. `-s` is required, and
`--stubs-compact` requires `--emit-stubs`; either misuse exits `2`.

Unreadable schemas, failed compilation, and unwritable output exit `1`.
Recoverable schema findings do not prevent emission, and `--strict` has no
effect in this mode. Use `Schema.require_valid()` through the Python API if
schema findings must stop generation. See {doc}`stubs` for consumption and
annotation limits rather than treating a stub as a validity verdict.

## Examples

Create `inventory.xsd` and `inventory.xml` from {doc}`quickstart`. Run these
commands from their directory. For the `-T` command, first save this as
`transforms.txt`:

```text
PrintData()
```

```bash
# Round-trip: parse and write the parsed tree back out
pyxsd -i inventory.xml -s inventory.xsd --namespaces strict -k -p parsed.xml

# Validate against an explicit schema, strict mode (CI-friendly)
pyxsd -i inventory.xml -s inventory.xsd --namespaces strict --strict

# Transform pipeline: both transforms run, final tree to stdout
pyxsd -i inventory.xml -s inventory.xsd --namespaces strict -t 'PrintData() > PrintData()'

# Transform calls from a file
pyxsd -i inventory.xml -s inventory.xsd --namespaces strict -T transforms.txt

# Validate an XSD 1.0 schema under the 1.0 processor
pyxsd -i inventory.xml -s inventory.xsd --namespaces strict --xsd-version 1.0 --strict

# Export the bound tree as JSON (the ToDict transform; a dict result is
# written as JSON to the transform output)
pyxsd -i inventory.xml -s inventory.xsd --namespaces strict -t 'ToDict()'
```
