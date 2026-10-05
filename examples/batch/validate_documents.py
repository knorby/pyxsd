"""Validate an explicit list of XML files against one compiled schema."""

from __future__ import annotations

import argparse
import traceback
from pathlib import Path

from pyxsd import ParseModes, Schema
from pyxsd.batch import DocumentSource


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("schema", type=Path, help="local XSD file")
    parser.add_argument("input_root", type=Path, help="root for relative source IDs")
    parser.add_argument("files", nargs="+", type=Path, help="explicit files relative to input_root")
    args = parser.parse_args(argv)
    try:
        root = args.input_root.resolve()
        sources = []
        for filename in args.files:
            path = (root / filename).resolve()
            if not path.is_relative_to(root):
                raise ValueError(f"file must be within the input root: {filename}")
            sources.append(DocumentSource(path.relative_to(root).as_posix(), path))
        sources.sort(key=lambda source: source.id)
        schema = Schema.compile(args.schema, mode=ParseModes.NAMESPACED)
        failed = False
        for outcome in schema.iter_parse(sources, errors="report"):
            label = f"{outcome.source.id}: {outcome.status}"
            if outcome.error is not None:
                print(f"{label} ({outcome.error.kind}): {outcome.error.message}")
            else:
                print(label)
                for issue in outcome.issues:
                    print(f"  {issue.phase}: {issue.format()}")
            failed |= outcome.status != "valid"
        return 1 if failed else 0
    except Exception:
        # Only the CLI boundary catches unexpected defects: the library
        # propagates them, and the traceback remains available to diagnose them.
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
