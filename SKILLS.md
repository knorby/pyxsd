# pyxsd skills

Portable, tested workflow guidance packaged with pyxsd. Skills are plain
directories: `SKILL.md` plus reference files, exportable anywhere without the
optional MCP dependencies.

| Skill | Purpose |
| --- | --- |
| `pyxsd-development` | Build Python bindings from XML Schemas — compile once, parse many, repair exact occurrences, revalidate fresh. |
| `pyxsd-xml-doctor` | Diagnose validation failures — read constraint evidence, fix the exact occurrence, revalidate the whole document. |
| `pyxsd-mcp` | Drive pyxsd through local stdio MCP — session inputs, paginated issues, direct results, transform previews, exports, and retained local outputs. |

## Listing and exporting

```bash
python -m pyxsd.skills list
python -m pyxsd.skills export pyxsd-xml-doctor ./skills
```

`export` copies the skill directory plus the shared `references/` into
`DEST/<name>/`. Exports are deterministic byte-for-byte, self-contained, and
never follow symlinks or overwrite existing targets. The same assets are
available through MCP resources and the official skills extension.

## Install with npx skills

With Node.js 22.20 or newer, the [Vercel Skills CLI](https://github.com/vercel-labs/skills)
discovers all three skills from the repository root. Discovery is read-only:

```bash
npx skills add https://github.com/knorby/pyxsd --list
```

Install them into your project for OpenCode:

```bash
npx skills add https://github.com/knorby/pyxsd -a opencode
```

Add `-g` for a user-global installation instead. The Python export above remains
available when Node.js is not installed.

Python API: `pyxsd.skills.list_skills()` and
`pyxsd.skills.export_skill(name, destination)`.

See the [skills documentation](https://pyxsd.knorby.com/skills.html) for the
full workflow guidance.
