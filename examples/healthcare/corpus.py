"""List repository inputs as JSON; selectors work in all three healthcare workflows."""

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.healthcare.common.corpus import REPOSITORIES, discover


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", choices=(*REPOSITORIES, "all"), default="hl7")
    parser.add_argument(
        "--kind", choices=("document", "fragment", "other", "malformed", "all"), default="document"
    )
    parser.add_argument(
        "--match", default="", help="case-insensitive substring of repository-relative path"
    )
    args = parser.parse_args(argv)
    try:
        records = [
            item
            for repo in (REPOSITORIES if args.repo == "all" else (args.repo,))
            for item in discover(repo)
        ]
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    records = [
        r
        for r in records
        if (args.kind == "all" or r["kind"] == args.kind)
        and args.match.casefold() in r["path"].casefold()
    ]
    print(json.dumps(records, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
