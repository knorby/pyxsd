# Command-line reference

```
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
| `-p FILE` | `--parsedXml`, `--parsedOutput` | Filename for the parsed (pre-transform) output. Default: input filename + `Parsed`. |
| `-k` | `--ParsedFile` | Write the parsed tree even when transforms are given. Off by default. |
| `-o FILE` | `--transformOutput` | Output after parsing and transforming. **stdout** by default. `stdout` may also be given explicitly as the value. |
| `-d` | `--useDefaultFile` | Use the default filename for transformed output instead of stdout. |
| `-t CALLS` | `--transform` | Transform call(s): `Class(args...)`, chained with `>` when multiple. |
| `-T FILE` | `--transformFile` | File with transform calls, one per line. Mutually exclusive with `-t`. |
| `-c FILE` | `--overlayClassesFile` | *Experimental.* Overlay class file that extends/overrides generated classes. |
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

## Examples

```bash
# Round-trip: parse and write the parsed tree back out
pyxsd -i inventory.xml -k -o parsed.xml

# Validate against an explicit schema, strict mode (CI-friendly)
pyxsd -i inventory.xml -s inventory.xsd --strict

# Transform pipeline: both transforms run, final tree to stdout
pyxsd -i inventory.xml -t 'PrintData() > PrintData()'

# Transform calls from a file
pyxsd -i inventory.xml -T transforms.txt
```
