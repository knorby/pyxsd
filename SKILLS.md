# pyxsd skills

Portable, tested workflow guidance packaged with pyxsd. Skills are plain
directories: `SKILL.md` plus reference files, exportable anywhere without the
optional MCP dependencies.

| Skill | Purpose |
| --- | --- |
| `pyxsd-development` | Build Python bindings from XML Schemas — compile once, parse many, repair exact occurrences, revalidate fresh. |
| `pyxsd-xml-doctor` | Diagnose validation failures — read constraint evidence, fix the exact occurrence, revalidate the whole document. |
| `pyxsd-mcp` | Drive pyxsd through MCP — workspaces, paginated issues, durable jobs, comparisons, exports with immutable artifacts. |

## Listing and exporting

```bash
python -m pyxsd.skills list
python -m pyxsd.skills export pyxsd-xml-doctor ./skills
```

`export` copies the skill directory plus the shared `references/` into
`DEST/<name>/`. Exports are deterministic byte-for-byte, self-contained, and
never follow symlinks or overwrite existing targets. The same assets are
available through MCP in E5.

Python API: `pyxsd.skills.list_skills()` and
`pyxsd.skills.export_skill(name, destination)`.

See the [skills documentation](https://pyxsd.knorby.com/skills.html) for the
full workflow guidance.
