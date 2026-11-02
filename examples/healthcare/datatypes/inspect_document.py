"""Print a small neutral JSON view of a CDA document."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from pyxsd import Document, XmlParserPolicy

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from examples.healthcare.common.cda_datatypes import CDA, attr, child, children
from examples.healthcare.common.schemas import compile_cda


def inspect(document: Document) -> dict[str, object]:
    role = child(child(document.root, "recordTarget"), "patientRole")
    patient = child(role, "patient")
    birth = child(patient, "birthTime")
    view = {
        "patient": {
            "identifiers": [asdict(n.as_identifier()) for n in children(role, "id")],
            "birth_date": birth.as_datetime().lexical if birth else None,
            "gender": {"F": "female", "M": "male", "UN": "other"}.get(
                attr(child(patient, "administrativeGenderCode"), "code")
            ),
        },
        "conditions": [],
        "vital_signs": [],
    }
    for section in document.findall(f".//{{{CDA}}}section"):
        code = attr(child(section, "code"), "code")
        if code == "11450-4":
            for entry in children(section, "entry"):
                act = child(entry, "act")
                for relation in children(act, "entryRelationship"):
                    value = child(child(relation, "observation"), "value")
                    if value is not None and hasattr(value, "as_coding"):
                        view["conditions"].append(asdict(value.as_coding()))
        elif code == "8716-3":
            for entry in children(section, "entry"):
                for component in children(child(entry, "organizer"), "component"):
                    observation = child(component, "observation")
                    value = child(observation, "value")
                    if value is not None and hasattr(value, "as_quantity"):
                        quantity = value.as_quantity()
                        view["vital_signs"].append(
                            {
                                "code": attr(child(observation, "code"), "code"),
                                "value": str(quantity.value)
                                if quantity.value is not None
                                else None,
                                "unit": quantity.unit,
                                "null_flavor": quantity.null_flavor,
                            }
                        )
    return view


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=Path)
    args = parser.parse_args(argv)
    schema = compile_cda()
    document = schema.parse(args.document, xml_policy=XmlParserPolicy.protected())
    print(json.dumps(inspect(document), sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
