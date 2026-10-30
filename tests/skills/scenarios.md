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
