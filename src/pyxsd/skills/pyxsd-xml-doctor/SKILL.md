---
name: pyxsd-xml-doctor
description: Diagnose XML validation failures with pyxsd — read constraint evidence, fix the exact occurrence, and revalidate the whole document.
---

# XML doctor workflow

Use this skill when an XML document fails validation and you must explain or
repair it.

## Diagnosis procedure

1. **Separate schema errors from instance findings.** If
   `Schema.compile(...)` raises, the schema itself is broken: fix the schema
   first and never report an instance verdict from an uncompilable schema.
   Instance findings come from `schema.parse(...).report`.

2. **Read the constraint evidence.** Each issue carries its code and the
   facet that produced it. Cite the actual constraint; never invent advice.

   ```python
   report = schema.parse("facet.xml").report
   issue = report.issues[0]
   assert issue.code == "value"  # lexical violation
   assert issue.constraint.kind == "pattern"
   assert "[A-Z]{3}-[0-9]{4}" in issue.constraint.expected[0]
   ```

3. **Common codes**:
   - `pattern`, `totalDigits`, `enumeration` — lexical facet violations; the
     constraint dict holds the real facet value.
   - `missing-attribute` — a required attribute absent from one exact element.
   - `order`, `declaration-child` — child sequence violations against the
     content model.
   - `unknown-root` — the document root matches no global declaration; the
     root is unbindable, so query nothing from that tree.

4. **Fix the exact occurrence.** Compile the schema with
   `mode=ParseModes.LAX`, then reach the precise node among repeated
   same-name siblings via `document.findall("./item")` (repeated members
   live in `_children_`, not named attributes). Repairing the first match is
   the classic wrong-node bug.

5. **Revalidate the whole document fresh.** Serialize the edited tree and
   parse it again with the same schema. The new report is the verdict; a
   stale `require_valid` from before the edit proves nothing.

   ```python
   from pyxsd import Schema
   from pyxsd.binding import ParseModes

   schema = Schema.compile("schema.xsd", mode=ParseModes.LAX)
   parsed = schema.parse("doc.xml")
   node = parsed.findall("./item")[1]  # the exact offending occurrence
   node.sku = "ABC-2222"
   fixed = Path("fixed.xml")
   fixed.write_text(parsed.to_string(), encoding="utf-8")
   assert schema.parse(str(fixed)).report.error_count == 0
   ```

6. **Missing attribute repair.** `missing-attribute` points at one element.
   Set the attribute on that occurrence only, then revalidate. Adding it to
   every sibling hides the one-node nature of the defect.

7. **Mixed content caution.** In mixed complexTypes, stray text is legal; do
   not "clean" it away. When re-serializing, preserve text order and warn the
   user that whitespace fidelity may matter downstream.

## No optional dependencies

Diagnosis needs only core pyxsd. The `pyxsd[mcp]` server offers the same
verdicts as tools (`pyxsd_validate_document`, `pyxsd_explain_issue`) with
cursor-paginated issues, but the Python API here always works.
