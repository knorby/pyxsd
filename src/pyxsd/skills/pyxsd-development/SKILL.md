---
name: pyxsd-development
description: Build Python bindings from XML Schemas with pyxsd — compile once, parse many, repair exact occurrences, and revalidate fresh.
---

# pyxsd development workflow

Use this skill whenever you map XML documents to Python objects with pyxsd.

## Core rules

1. **Compile once, parse many.** Build `Schema` one time per schema source and
   reuse it for every document parse. Recompiling per document wastes work and
   hides cache behavior.

   ```python
   from pyxsd import Schema

   schema = Schema.compile("catalog.xsd")
   for path in paths:
       document = schema.parse(path)  # same compiled schema
   ```

2. **Overview, then compact, then full.** Start with `schema.overview()`,
   render `schema.stub(compact=True)` to skim shapes, and only request the full
   stub (`schema.stub(compact=False)`) for the component you are editing. The
   full stub is much larger and includes constraint detail.

3. **Locate exact occurrences.** Repeated nodes are distinguished by their
   same-name sibling position. `Document.locate(issue.location)` returns the
   exact occurrence, not the first element with a matching name.

4. **Repair then revalidate fresh.** Compile with
   `mode=ParseModes.LAX` so best-effort values stay bound, edit the exact
   occurrence, serialize, and parse the result again. Never trust the
   pre-edit report — a stale `require_valid` verdict describes the old
   document.

   ```python
   from pyxsd import Schema
   from pyxsd.binding import ParseModes

   schema = Schema.compile("items.xsd", mode=ParseModes.LAX)
   parsed = schema.parse("broken.xml")
   occurrences = parsed.findall("./item")  # repeated members live here,
   occurrences[1].value = 2  # not in a named list attribute
   revised = Path("revised.xml")
   revised.write_text(parsed.to_string(), encoding="utf-8")
   verdict = schema.parse(str(revised))
   assert verdict.report.error_count == 0
   ```

5. **Make minimal semantic changes.** Edit only the bound values that must
   change. Preserves every other node, attribute, and ordering exactly; check
   with `to_string()` before and after when fidelity matters.

6. **Publish explicitly.** `Document.write(path)` writes only where you say.
   Nothing is written implicitly; there is no global output location.

## Repeated elements

Elements with `maxOccurs > 1` store every occurrence in `_children_` in
document order — there is no named list attribute. Reach occurrences with
`document.findall("./item")` or `document.root._children_`, and identify the
exact one via the same-name sibling index in an issue location. Single
occurrence children DO bind as named attributes.

## Namespaces

Bind by expanded name, never by prefix string. `targetNamespace` decides the
object attribute layout; prefixes are serialization detail. When two schemas
share a namespace, compile each separately and keep their documents apart.

## Mixed content

In `mixed="true"` complex types, character data surrounds child elements.
Re-serialization preserves ordering and whitespace; do not reorder or strip
text nodes, and warn when fidelity matters.

## Fallback

Everything here is core pyxsd: no optional dependency is required. When the
`pyxsd[mcp]` server is available you may prefer its tools, but the Python API
in this skill is always the fallback.
