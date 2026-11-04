"""Real document inspection, including repeated bound values."""

import json

from tests.healthcare_support import cda as cda
from tests.healthcare_support import document_path

from pyxsd import XmlParserPolicy


def test_inspection_uses_actual_problem_and_vital_values(cda, capsys, monkeypatch):
    from examples.healthcare.datatypes import inspect_document

    document = cda.parse(document_path("discharge"), xml_policy=XmlParserPolicy.protected())
    view = inspect_document.inspect(document)
    assert view["patient"]["birth_date"] == "2005-05-01"
    assert view["patient"]["gender"] == "female"
    assert view["patient"]["identifiers"][0]["value"] == "998991"
    assert [c["display"] for c in view["conditions"]] == ["Dark stools", "Appendicitis"]
    assert [(v["code"], v["value"], v["unit"]) for v in view["vital_signs"]] == [
        ("8302-2", "177", "cm"),
        ("3141-9", "86", "kg"),
        ("8480-6", "80", "mm[Hg]"),
        ("8462-4", "55", "mm[Hg]"),
    ]
    monkeypatch.setattr(inspect_document, "compile_cda", lambda: cda)
    assert inspect_document.main([str(document_path("discharge"))]) == 0
    assert json.loads(capsys.readouterr().out) == json.loads(json.dumps(view))
