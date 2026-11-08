---
name: pyxsd-cli
description: Use when an agent needs the pyxsd command line or shell for XML/XSD validation, CI status checks, schema inspection, stub generation, transforms, or stdin/stdout pipelines, especially when MCP is unavailable or unnecessary.
---

# pyxsd CLI workflow

Use the installed `pyxsd` console script to validate and bind one XML document,
run transforms, or inspect a schema. `python -m pyxsd` is equivalent. Neither
form requires MCP or its optional dependencies.

Start with `pyxsd --version` and consult `pyxsd --help` for the installed
release. The examples below use `document.xml` and `schema.xsd`; substitute real
paths and quote transform calls for the shell.

## Quick reference

| Task | Command |
| --- | --- |
| Validate with an explicit schema | `pyxsd -i document.xml -s schema.xsd --strict` |
| Validate using the instance's schema hints | `pyxsd -i document.xml --strict` |
| Read XML from stdin | `pyxsd -s schema.xsd --strict < document.xml` |
| Write the parsed tree before any transforms | `pyxsd -i document.xml -s schema.xsd -k -p parsed.xml` |
| Export a bound tree as JSON | `pyxsd -i document.xml -s schema.xsd -t 'ToDict()' -o data.json` |
| Run a transform chain | `pyxsd -i document.xml -s schema.xsd -t 'PrintData() > PrintData()'` |
| Read transform calls from a file | `pyxsd -i document.xml -s schema.xsd -T transforms.txt` |
| Emit a full typing stub to stdout | `pyxsd --emit-stubs -s schema.xsd` |
| Emit a compact stub to a file | `pyxsd --emit-stubs schema.pyi --stubs-compact -s schema.xsd` |

## Validate and interpret the result

1. Pass `-s schema.xsd` when the schema location is known. Without `-s`, pyxsd
   reads `xsi:noNamespaceSchemaLocation` or `xsi:schemaLocation` from the
   instance and resolves the hint relative to the instance file. A missing hint
   is a fatal error; a malformed hint produces a `schema-hint` warning.
2. Read validation findings from stderr. A nonempty report does not by itself
   make the process fail.
3. Add `--strict` when automation needs invalid content to produce exit status
   1. Without `--strict`, exit status 0 means the run completed, even when the
   report contains validation errors. Do not present that status alone as a
   validity verdict.
4. Separate failure classes. Exit 1 also covers unreadable input, malformed XML,
   a missing schema, and other fatal processing errors. Argparse usage errors
   exit 2. Inspect stderr before reporting a cause.

`--mode` and `--strict` control different things. Binding mode `strict` is the
default and drops invalid values after reporting them; `--mode lax` retains
best-effort values where supported while keeping the validation findings.
`--strict` controls only the process status.

Choose `--namespaces strict` (the default) for expanded-name matching. Use
`--namespaces legacy` only for documents that intentionally match by local name
while ignoring namespace URIs. Select `--xsd-version 1.0` for an XSD 1.0
processor; the default is 1.1. These are schema/binding choices, not output
formats.

## Control inputs and outputs

- XML input defaults to stdin. Prefer an explicit `-s` for streams; relative
  schema hints in a stream resolve against the current working directory.
- Validation reports go to stderr. `-q` suppresses logging, not validation
  issues; `-v` enables debug logging. The two flags are mutually exclusive.
- With no transforms, use `-k` to write the parsed tree. `-p PATH` chooses its
  location; otherwise a file input gets `<stem>Parsed.xml` beside the source.
  For stdin, supply `-p` explicitly.
- Transform output defaults to stdout. Use `-o PATH` for a named file or
  `-d` for the default `<stem>Transformed.xml`; stream input with `-d` writes
  `output.xml` in the current directory.
- A final `Document` result writes XML. A mapping result, such as `ToDict()`,
  writes JSON. Other final Python values produce no final output file.
- Choose a new output path rather than overwriting the source document. Review
  generated content before replacing project files.

## Run transforms

Use `-t 'Class(args...)'` for literal arguments and chain top-level calls with
`>`. Alternatively, put calls in a UTF-8 text file and pass `-T FILE`; `-t` and
`-T` are mutually exclusive. `-k` retains the pre-transform tree alongside the
transform output.

Transform resolution imports Python modules from pyxsd, the working directory,
and the instance directory. Overlay files supplied with `-c` are also Python.
Run only trusted transform and overlay code: it executes with the user's
privileges and is not sandboxed. Literal-only call parsing does not make the
imported implementation safe.

## Inspect a schema without an instance

Use `--emit-stubs [PATH]` with `-s`. Omit `PATH` for stdout; add
`--stubs-compact` for the shorter agent-oriented description. Compilation flags
such as `--mode`, `--namespaces`, `--xsd-version`, and `-c` still apply.

Stub mode reads no XML instance and prints no validation report. Recoverable
schema findings do not prevent emission, and `--strict` has no effect in this
mode. Successful stub output is not a schema-validity verdict. Use the Python
API's `Schema.require_valid()` when schema findings must stop a workflow.

## Choose Python or MCP when the CLI ends

The CLI prints a human-readable report but does not expose structured report
objects, exact source-occurrence lookup, or programmatic tree editing. Use the
Python API for those tasks, for parser hardening and resource policies, and for
fresh `Document.revalidate()` checks after edits. Use MCP when a client needs
workspace inputs, paginated issues, exact captured subtrees, comparisons, or
retained artifacts. Do not invent CLI flags for those capabilities.
