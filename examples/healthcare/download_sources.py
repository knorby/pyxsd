"""Fetch verified CDA R2 and FHIR R4 schemas (optional corpora are download-only)."""

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
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    for path in fetch_sources(**vars(args)):
        print(f"verified: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
