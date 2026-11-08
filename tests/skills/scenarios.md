# Skill evaluation scenarios (baseline, recorded before guidance drafting)

These scenarios define the pressure cases the packaged skills must survive.
They were written before the skill bodies so the guidance is driven by the
cases, not the cases by the guidance. Skill efficacy against live models is
**NOT EVALUATED** in this repository; running them with real agents requires
separate, user-approved trials. No implicit model spend is authorized here.

## pyxsd-development

1. **Unfamiliar schema**: given a large unseen XSD, produce an overview, find
   the element that carries a specific attribute, and render a compact stub
   before a full stub. Correct guidance reuses one compiled `Schema` for all
   parses instead of recompiling per document.
2. **Namespace collisions**: two schemas with clashing prefixes and a shared
   target namespace; the guided workflow must keep expansions and bindings
   correct rather than string-matching prefixes.
3. **Repeated-node repair**: a document with three sibling errors where only
   the second occurrence is wrong; the skill must locate the exact occurrence
   (same-name sibling index), repair it, and revalidate the whole document
   fresh rather than trusting the stale report.

## pyxsd-xml-doctor

4. **Facet failure**: a pattern/totalDigits violation; the explanation must
   cite the constraint evidence (the actual facet value), not invent advice.
5. **Missing attribute**: a required attribute absent on one of many elements;
   the skill must point at the exact occurrence and add it, then revalidate.
6. **Invalid schema**: a broken schema document; guidance must separate schema
   compilation errors from instance validation findings and never report an
   instance verdict from an uncompilable schema.
7. **Mixed content**: stray character data in a mixed complexType; the skill
   must warn that ordering/whitespace fidelity matters when re-serializing.

## pyxsd-mcp

8. **MCP discovery**: a client with only stdio must create a workspace, upload
   files, validate, and follow an issue to its exact occurrence via cursor
   pagination; guidance must not assume execution or HTTP features.
9. **Cross-schema migration**: compare an old and new schema with a corpus,
   preview a transform, and publish the retained candidate; the skill must
   keep potential impact separate from observed corpus regressions and state
   that no-regressions applies only to the tested corpus and options.
10. **Missing-dependency fallback**: with `pyxsd[mcp]` not installed, the same
    tasks must fall back to the Python/CLI workflow without claiming MCP
    features are available.

## pyxsd-cli

These scenarios were recorded before the CLI skill was drafted. They exercise
retrieval and application of CLI guidance without requiring an MCP server.

11. **CI validity status**: validate an invalid document in automation. The
    workflow must use `--strict` and distinguish a validation failure from a
    fatal input error, while recognizing that a run without `--strict` can exit
    zero despite validation findings.
12. **Stream and file boundaries**: read XML from stdin with an explicit schema,
    keep diagnostics on stderr, and direct data output to stdout or a named file
    without creating a temporary input or overwriting the source document.
13. **Schema-only inspection**: request full and compact stubs from an explicit
    schema. The workflow must not read an instance or describe successful stub
    emission as proof that the schema has no findings.
14. **Trusted transform pipeline**: chain known transforms with `>` or read calls
    from a transform file, retain pre-transform XML when requested, and state
    that transform and overlay Python runs with the user's privileges.
15. **Capability routing**: when a task needs structured reports, exact source
    occurrences, programmatic tree edits, or retained artifacts, select the
    Python API or MCP rather than inventing CLI flags.
