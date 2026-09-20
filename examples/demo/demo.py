"""End-to-end pyxsd demo, run as a library.

Run from this directory (or the repository root) with::

    uv run python examples/demo/demo.py

It parses ``instance.xml`` against ``schema.xsd``, applies the local
``CountElements`` transform through the document API, prints the
validation report, and writes the transformed tree to
``transformed.xml``.

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
    from pyxsd.cli import parse_transform_call, resolve_transform_class
    from pyxsd.document import Document
    from pyxsd.schema import Schema

    document = Schema.compile(DEMO_DIR / "schema.xsd").parse(DEMO_DIR / "instance.xml")

    report = document.report
    print(f"\nValidation: {report}")
    for issue in report.issues:
        print("  " + issue.format())

    root = document.root
    print(f"\nRoot instance: {type(root).__name__} named {root._name_!r}")
    for child in root._children_:
        name = child._attribs_.get("id", "-")
        print(f"  {child._name_} id={name}")

    transform_name, args, kwargs = parse_transform_call("CountElements()")
    transform_cls = resolve_transform_class(transform_name, search_paths=[DEMO_DIR])
    result = document.transform(lambda tree: transform_cls(tree)(*args, **kwargs))
    if isinstance(result, Document):
        document = result
    document.write(DEMO_DIR / "transformed.xml")

    print(f"\nTransformed tree written to {DEMO_DIR / 'transformed.xml'}")
    return 1 if report.has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
