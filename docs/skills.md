# Skills

pyxsd packages tested workflow guidance as portable skills. Each skill is a
plain directory — `SKILL.md` plus reference files — exportable to any agent
environment without the optional MCP dependencies installed.

## Packaged skills

- **pyxsd-development** — build Python bindings from XML Schemas: compile once,
  parse many, repair exact occurrences, and revalidate fresh.
- **pyxsd-xml-doctor** — diagnose XML validation failures: read constraint
  evidence, fix the exact occurrence, and revalidate the whole document.
- **pyxsd-mcp** — drive pyxsd through MCP: workspaces, validation with exact
  occurrences, durable jobs, comparisons, and exports with immutable artifacts.

## Using skills

List the packaged skills and export one to a directory your agent reads:

```bash
python -m pyxsd.skills list
python -m pyxsd.skills export pyxsd-xml-doctor ./agent-skills
```

Exports are deterministic byte-for-byte: the same skill always exports the
same bytes, so caches and reviews stay stable. Each export is self-contained
— `SKILL.md` plus the shared `references/` directory — and never follows
symlinks, overwrites an existing target, or writes outside the destination
you name. There is no global home-directory install.

The Python API mirrors the CLI:

```python
from pyxsd.skills import export_skill, list_skills

for skill in list_skills():
    print(skill.name, "-", skill.description)
export_skill("pyxsd-xml-doctor", destination_dir)
```

Skill bodies teach the workflows the test suite actually verifies: compiling
a schema once and parsing many documents, locating the exact repeated-node
occurrence an issue points at, repairing it, and revalidating the whole
document fresh (a stale `require_valid` verdict describes the old bytes).
The MCP skill covers workspaces, cursor-paginated issues, durable jobs,
schema comparison, and immutable artifact exports — and how each falls back
to the Python/CLI workflow when the server is not installed.

## Evaluation status

Baseline pressure scenarios live in `tests/skills/scenarios.md`; they were
recorded before the guidance was drafted. Skill efficacy against live models
is **not evaluated** in this repository — running those trials requires
separate, explicit approval, and no implicit model spend is authorized here.
