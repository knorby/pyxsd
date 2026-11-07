"""Projection guidance searches real declaration graphs with bounded evidence."""

from io import StringIO

import pytest

pytest.importorskip("pydantic")

from pyxsd.binding import ParseModes
from pyxsd.mcp.projection_guidance import candidate_details, resolve_global_element
from pyxsd.schema import Schema

XS = "http://www.w3.org/2001/XMLSchema"


def _schema(text, mode=ParseModes.STRICT):
    return Schema.compile(StringIO(text), mode=mode)


def _model_prepare(schema):
    from pyxsd.integrations.pydantic import models

    registry = models(schema)

    def prepare(element, path):
        return registry.model_for(element=element, path=path)

    return prepare


MIXED_LINES = f"""<xs:schema xmlns:xs="{XS}" targetNamespace="urn:test"
  elementFormDefault="qualified">
  <xs:element name="root"><xs:complexType mixed="true"><xs:sequence>
    <xs:element name="line" type="xs:string" maxOccurs="unbounded"/>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>"""


def test_candidates_follow_mixed_root_to_projectable_leaf():
    schema = _schema(MIXED_LINES, mode=ParseModes.NAMESPACED)
    details = candidate_details(
        schema, element="{urn:test}root", path=(), prepare=_model_prepare(schema)
    )
    assert details == {
        "element": "{urn:test}root",
        "path": [],
        "projection_candidates": [
            {"element": "{urn:test}root", "path": ["{urn:test}line"], "repeated": True}
        ],
        "searched_declarations": 1,
        "search_truncated": False,
    }
    for candidate in details["projection_candidates"]:
        assert _model_prepare(schema)(details["element"], tuple(candidate["path"]))


def test_candidates_resolve_bare_global_names():
    schema = _schema(MIXED_LINES, mode=ParseModes.NAMESPACED)
    assert resolve_global_element(schema, "root") == "{urn:test}root"
    assert resolve_global_element(schema, "element:{urn:test}root") == "{urn:test}root"
    details = candidate_details(schema, element="root", path=(), prepare=_model_prepare(schema))
    assert details["element"] == "{urn:test}root"
    assert details["projection_candidates"][0]["element"] == "{urn:test}root"


def test_candidate_paths_use_each_modes_instance_spelling():
    qualified = f"""<xs:schema xmlns:xs="{XS}" targetNamespace="urn:test"
      elementFormDefault="qualified">
      <xs:element name="root"><xs:complexType mixed="true"><xs:sequence>
        <xs:element name="line" type="xs:string"/>
      </xs:sequence></xs:complexType></xs:element>
    </xs:schema>"""
    strict = _schema(qualified, mode=ParseModes.NAMESPACED)
    legacy = _schema(qualified, mode=ParseModes.STRICT)
    strict_details = candidate_details(
        strict, element="{urn:test}root", path=(), prepare=_model_prepare(strict)
    )
    legacy_details = candidate_details(
        legacy, element="{urn:test}root", path=(), prepare=_model_prepare(legacy)
    )
    assert strict_details["projection_candidates"][0]["path"] == ["{urn:test}line"]
    assert legacy_details["projection_candidates"][0]["path"] == ["line"]


MIXED_BRANCHES = f"""<xs:schema xmlns:xs="{XS}">
  <xs:element name="root"><xs:complexType mixed="true"><xs:sequence>
    <xs:element name="zeta" type="xs:string"/>
    <xs:element name="row"><xs:complexType><xs:sequence>
      <xs:element name="amount" type="xs:decimal"/>
    </xs:sequence></xs:complexType></xs:element>
    <xs:element name="alpha" type="xs:string"/>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>"""


def test_candidates_keep_declaration_order_and_complex_and_primitive_leaves():
    schema = _schema(MIXED_BRANCHES)
    details = candidate_details(schema, element="root", path=(), prepare=_model_prepare(schema))
    assert [item["path"] for item in details["projection_candidates"]] == [
        ["zeta"],
        ["row"],
        ["alpha"],
        ["row", "amount"],
    ]
    assert [item["repeated"] for item in details["projection_candidates"]] == [
        False,
        False,
        False,
        False,
    ]


REF_ROOT = f"""<xs:schema xmlns:xs="{XS}">
  <xs:element name="root"><xs:complexType mixed="true"><xs:sequence>
    <xs:element ref="line" maxOccurs="unbounded"/>
  </xs:sequence></xs:complexType></xs:element>
  <xs:element name="line"><xs:complexType><xs:sequence>
    <xs:element name="amount" type="xs:decimal"/>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>"""


def test_candidates_resolve_element_refs_to_their_target_declarations():
    schema = _schema(REF_ROOT)
    details = candidate_details(schema, element="root", path=(), prepare=_model_prepare(schema))
    assert details["projection_candidates"] == [
        {"element": "root", "path": ["line"], "repeated": True},
        {"element": "root", "path": ["line", "amount"], "repeated": False},
    ]


AMBIGUOUS_A = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
  targetNamespace="urn:a"><xs:element name="same" type="xs:string"/></xs:schema>"""
AMBIGUOUS_B = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
  targetNamespace="urn:b"><xs:element name="same" type="xs:string"/></xs:schema>"""
AMBIGUOUS_ROOT = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
  xmlns:a="urn:a" xmlns:b="urn:b">
  <xs:import namespace="urn:a" schemaLocation="a.xsd"/>
  <xs:import namespace="urn:b" schemaLocation="b.xsd"/>
  <xs:element name="root"><xs:complexType mixed="true"><xs:sequence>
    <xs:element name="wrapper"><xs:complexType><xs:sequence>
      <xs:element ref="a:same"/><xs:element ref="b:same"/>
    </xs:sequence></xs:complexType></xs:element>
    <xs:element name="leaf" type="xs:string"/>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>"""


def test_candidates_skip_ambiguous_declaration_positions(tmp_path):
    (tmp_path / "a.xsd").write_text(AMBIGUOUS_A, encoding="utf-8")
    (tmp_path / "b.xsd").write_text(AMBIGUOUS_B, encoding="utf-8")
    (tmp_path / "root.xsd").write_text(AMBIGUOUS_ROOT, encoding="utf-8")
    schema = Schema.compile(str(tmp_path / "root.xsd"))
    details = candidate_details(schema, element="root", path=(), prepare=_model_prepare(schema))
    assert [item["path"] for item in details["projection_candidates"]] == [["leaf"]]
    assert details["search_truncated"] is False


RECURSIVE_GRAPH = f"""<xs:schema xmlns:xs="{XS}">
  <xs:complexType name="Node"><xs:sequence>
    <xs:element name="node" type="Node" minOccurs="0"/>
    <xs:element name="leaf" type="xs:string"/>
  </xs:sequence></xs:complexType>
  <xs:element name="root" type="Node"/>
</xs:schema>"""


def test_candidates_terminate_on_recursive_type_graphs():
    schema = _schema(RECURSIVE_GRAPH)
    details = candidate_details(schema, element="root", path=(), prepare=_model_prepare(schema))
    assert details["projection_candidates"] == [
        {"element": "root", "path": ["leaf"], "repeated": False}
    ]
    assert details["search_truncated"] is False


def test_candidates_are_empty_for_wildcard_only_root():
    schema = _schema(
        f"""<xs:schema xmlns:xs="{XS}">
          <xs:element name="root"><xs:complexType mixed="true"><xs:sequence>
            <xs:any/>
          </xs:sequence></xs:complexType></xs:element>
        </xs:schema>"""
    )
    details = candidate_details(schema, element="root", path=(), prepare=_model_prepare(schema))
    assert details["projection_candidates"] == []
    assert details["searched_declarations"] == 0
    assert details["search_truncated"] is False


def _chain_schema(depth):
    wrappers = []
    for index in range(depth):
        child = (
            f'<xs:element name="leaf{index + 1}" type="xs:string"/>'
            if index == depth - 1
            else f'<xs:element name="level{index + 1}" type="Wrap{index + 1}"/>'
        )
        wrappers.append(
            f'<xs:complexType name="Wrap{index}" mixed="true"><xs:sequence>{child}'
            f"</xs:sequence></xs:complexType>"
        )
    return f"""<xs:schema xmlns:xs="{XS}">
      {"".join(wrappers)}
      <xs:element name="root" type="Wrap0"/>
    </xs:schema>"""


def test_search_reports_depth_cap_as_truncated():
    from pyxsd.mcp.config import Limits

    schema = _schema(_chain_schema(9))
    details = candidate_details(
        schema,
        element="root",
        path=(),
        prepare=_model_prepare(schema),
        limits=Limits(enforced=True),
    )
    assert details["projection_candidates"] == []
    assert details["search_truncated"] is True


def test_search_reports_visit_cap_as_truncated_without_candidates():
    from pyxsd.mcp.config import Limits

    children = "".join(f'<xs:element name="w{index:03d}" type="Wrap"/>' for index in range(101))
    schema = _schema(
        f"""<xs:schema xmlns:xs="{XS}">
          <xs:complexType name="Wrap" mixed="true"><xs:sequence><xs:any/>
          </xs:sequence></xs:complexType>
          <xs:element name="root"><xs:complexType mixed="true"><xs:sequence>
            {children}
          </xs:sequence></xs:complexType></xs:element>
        </xs:schema>"""
    )
    details = candidate_details(
        schema,
        element="root",
        path=(),
        prepare=_model_prepare(schema),
        limits=Limits(enforced=True),
    )
    assert details["projection_candidates"] == []
    assert details["searched_declarations"] == 100
    assert details["search_truncated"] is True


def test_search_reports_candidate_cap_as_truncated():
    from pyxsd.mcp.config import Limits

    children = "".join(
        f'<xs:element name="line{index:02d}" type="xs:string"/>' for index in range(11)
    )
    schema = _schema(
        f"""<xs:schema xmlns:xs="{XS}">
          <xs:element name="root"><xs:complexType mixed="true"><xs:sequence>
            {children}
          </xs:sequence></xs:complexType></xs:element>
        </xs:schema>"""
    )
    details = candidate_details(
        schema,
        element="root",
        path=(),
        prepare=_model_prepare(schema),
        limits=Limits(enforced=True),
    )
    assert len(details["projection_candidates"]) == 10
    assert details["search_truncated"] is True


def test_candidates_search_below_a_requested_nested_route():
    schema = _schema(MIXED_BRANCHES)
    details = candidate_details(
        schema,
        element="root",
        path=("row",),
        prepare=_model_prepare(schema),
    )
    assert details["path"] == ["row"]
    assert details["projection_candidates"] == [
        {"element": "root", "path": ["row", "amount"], "repeated": False}
    ]
    assert details["search_truncated"] is False


def test_failed_nested_path_searches_from_its_resolvable_parent():
    schema = _schema(MIXED_BRANCHES)
    details = candidate_details(
        schema,
        element="root",
        path=("row", "missing"),
        prepare=_model_prepare(schema),
    )
    assert details["path"] == ["row", "missing"]
    assert details["projection_candidates"] == [
        {"element": "root", "path": ["row", "amount"], "repeated": False}
    ]
    assert details["search_truncated"] is False


def test_failed_local_path_searches_from_its_parent_route():
    schema = _schema(MIXED_BRANCHES)
    details = candidate_details(
        schema,
        element="root",
        path=("missing",),
        prepare=_model_prepare(schema),
    )
    assert details["path"] == ["missing"]
    assert [item["path"] for item in details["projection_candidates"]] == [
        ["zeta"],
        ["row"],
        ["alpha"],
        ["row", "amount"],
    ]
    assert details["search_truncated"] is False
