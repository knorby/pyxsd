"""Command-line entry point for pyxsd."""

import sys

from pyxsd.cli import main as xml_main


def main(argv: list[str] | None = None) -> int | None:
    """Run XML processing, or the optional server via ``pyxsd mcp``."""
    arguments = sys.argv[1:] if argv is None else argv
    if arguments[:1] == ["mcp"]:
        from pyxsd.mcp.__main__ import main as mcp_main

        return mcp_main(arguments[1:])
    xml_main(arguments)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
