"""End-to-end pyxsd demo, run as a library.

Run from this directory (or the repository root) with::

    uv run python examples/demo/demo.py

It parses ``instance.xml`` against ``schema.xsd`` using the full pyxsd
pipeline (parse -> validate -> transform -> write), applies the local
``CountElements`` transform, prints the validation report, and writes
the transformed tree to ``transformed.xml``.

The equivalent command line is::

    pyxsd -i instance.xml -o transformed.xml -t 'CountElements()'
"""

from __future__ import annotations

import sys
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent


def main() -> int:
    # Import pyxsd (installed in the uv environment; falls back to the
    # repository src/ tree when run from a checkout without install).
    from pyxsd import PyXSD

    parser = PyXSD(
        xmlFileInput=DEMO_DIR / "instance.xml",
        xmlFileOutput=False,  # keep the parsed tree off disk
        transforms=["CountElements()"],  # resolved from this directory
        transformOutputName=str(DEMO_DIR / "transformed.xml"),
    )

    report = parser.report
    print(f"\nValidation: {report}")
    for issue in report.issues:
        print("  " + issue.format())

    root = parser.schemaRootInstance
    print(f"\nRoot instance: {type(root).__name__} named {root._name_!r}")
    for child in root._children_:
        name = child._attribs_.get("id", "-")
        print(f"  {child._name_} id={name}")

    print(f"\nTransformed tree written to {DEMO_DIR / 'transformed.xml'}")
    return 1 if report.has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
