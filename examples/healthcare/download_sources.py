"""Fetch verified schemas and the HL7 repository; optionally fetch ONC and vendor repos."""

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.healthcare.common.sources import fetch_sources


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-onc", action="store_true")
    parser.add_argument("--include-vendor", action="store_true")
    parser.add_argument(
        "--all-corpora", action="store_true", help="also fetch ONC and vendor repositories"
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.all_corpora:
        args.include_onc = args.include_vendor = True
    del args.all_corpora
    for path in fetch_sources(**vars(args)):
        print(f"verified: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
