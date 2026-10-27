"""``python -m pyxsd.skills`` — list and export packaged guidance.

Works without the optional MCP dependencies installed.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pyxsd.skills",
        description="List packaged pyxsd skills or export one to a directory.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Print packaged skill names and descriptions.")
    export = commands.add_parser("export", help="Copy a skill directory to DEST.")
    export.add_argument("name", help="Packaged skill name.")
    export.add_argument(
        "destination", type=Path, help="Existing directory to export into (never home)."
    )
    arguments = parser.parse_args(argv)

    from . import export_skill, list_skills

    if arguments.command == "list":
        for skill in list_skills():
            print(f"{skill.name}: {skill.description}")
        return 0
    try:
        exported = export_skill(arguments.name, arguments.destination)
    except KeyError:
        known = ", ".join(skill.name for skill in list_skills())
        print(f"Unknown skill {arguments.name!r}. Known skills: {known}", file=sys.stderr)
        return 2
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        print(f"Cannot export skill: {exc}", file=sys.stderr)
        return 2
    print(str(exported))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
