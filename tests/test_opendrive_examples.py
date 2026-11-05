"""Tests for the OpenDRIVE example family.

The offline tests run a synthetic, namespace-free OpenDRIVE subset through
pyxsd, then check the geometry sampler and lane offsetting analytically.
The cache-gated tests use the pinned real schemas and road networks fetched
by ``examples/opendrive/download_sources.py``.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from examples.opendrive import render_map
from examples.opendrive.common.schemas import compile_schema
from examples.opendrive.plot_profiles import plot_profiles, profile_samples

from pyxsd.schema import Schema

FAMILY_ROOT = Path(__file__).resolve().parents[1] / "examples" / "opendrive"
DATA_DIR = FAMILY_ROOT / ".cache" / "opendrive-data"
SCHEMA_DIR = FAMILY_ROOT / ".cache" / "opendrive-schemas"

requires_sources = pytest.mark.skipif(
    not (DATA_DIR / "Town01.xodr").is_file()
    or not (SCHEMA_DIR / "OpenDRIVE_1.4H.xsd").is_file()
    or not (SCHEMA_DIR / "opendrive_17_core.xsd").is_file(),
    reason="run `uv run python examples/opendrive/download_sources.py` to fetch OpenDRIVE sources",
)

# Only the pieces the renderer touches: geometry primitives, lane widths,
# elevation and superelevation. Namespace-free, like every real .xodr file.
SUBSET_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="widthType">
    <xs:attribute name="sOffset" type="xs:double" use="required"/>
    <xs:attribute name="a" type="xs:double" use="required"/>
    <xs:attribute name="b" type="xs:double" use="required"/>
    <xs:attribute name="c" type="xs:double" use="required"/>
    <xs:attribute name="d" type="xs:double" use="required"/>
  </xs:complexType>
  <xs:complexType name="laneOffsetType">
    <xs:attribute name="s" type="xs:double" use="required"/>
    <xs:attribute name="a" type="xs:double" use="required"/>
    <xs:attribute name="b" type="xs:double" use="required"/>
    <xs:attribute name="c" type="xs:double" use="required"/>
    <xs:attribute name="d" type="xs:double" use="required"/>
  </xs:complexType>
  <xs:complexType name="laneType">
    <xs:sequence>
      <xs:element name="width" type="widthType" minOccurs="0" maxOccurs="unbounded"/>
    </xs:sequence>
    <xs:attribute name="id" type="xs:int" use="required"/>
    <xs:attribute name="type" type="xs:string" use="required"/>
  </xs:complexType>
  <xs:complexType name="sideType">
    <xs:sequence>
      <xs:element name="lane" type="laneType" maxOccurs="unbounded"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="laneSectionType">
    <xs:sequence>
      <xs:element name="left" type="sideType" minOccurs="0"/>
      <xs:element name="center" type="sideType" minOccurs="0"/>
      <xs:element name="right" type="sideType" minOccurs="0"/>
    </xs:sequence>
    <xs:attribute name="s" type="xs:double" use="required"/>
  </xs:complexType>
  <xs:complexType name="lanesType">
    <xs:sequence>
      <xs:element name="laneOffset" type="laneOffsetType" minOccurs="0" maxOccurs="unbounded"/>
      <xs:element name="laneSection" type="laneSectionType" maxOccurs="unbounded"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="elevationType">
    <xs:attribute name="s" type="xs:double" use="required"/>
    <xs:attribute name="a" type="xs:double" use="required"/>
    <xs:attribute name="b" type="xs:double" use="required"/>
    <xs:attribute name="c" type="xs:double" use="required"/>
    <xs:attribute name="d" type="xs:double" use="required"/>
  </xs:complexType>
  <xs:complexType name="elevationProfileType">
    <xs:sequence>
      <xs:element name="elevation" type="elevationType" maxOccurs="unbounded"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="lateralProfileType">
    <xs:sequence>
      <xs:element name="superelevation" type="elevationType" maxOccurs="unbounded"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="poly3Type">
    <xs:attribute name="a" type="xs:double" use="required"/>
    <xs:attribute name="b" type="xs:double" use="required"/>
    <xs:attribute name="c" type="xs:double" use="required"/>
    <xs:attribute name="d" type="xs:double" use="required"/>
  </xs:complexType>
  <xs:complexType name="paramPoly3Type">
    <xs:attribute name="uA" type="xs:double" use="required"/>
    <xs:attribute name="uB" type="xs:double" use="required"/>
    <xs:attribute name="uC" type="xs:double" use="required"/>
    <xs:attribute name="uD" type="xs:double" use="required"/>
    <xs:attribute name="vA" type="xs:double" use="required"/>
    <xs:attribute name="vB" type="xs:double" use="required"/>
    <xs:attribute name="vC" type="xs:double" use="required"/>
    <xs:attribute name="vD" type="xs:double" use="required"/>
    <xs:attribute name="pRange" type="xs:string"/>
  </xs:complexType>
  <xs:complexType name="geometryType">
    <xs:choice>
      <xs:element name="line"/>
      <xs:element name="arc">
        <xs:complexType>
          <xs:attribute name="curvature" type="xs:double" use="required"/>
        </xs:complexType>
      </xs:element>
      <xs:element name="spiral">
        <xs:complexType>
          <xs:attribute name="curvStart" type="xs:double" use="required"/>
          <xs:attribute name="curvEnd" type="xs:double" use="required"/>
        </xs:complexType>
      </xs:element>
      <xs:element name="poly3" type="poly3Type"/>
      <xs:element name="paramPoly3" type="paramPoly3Type"/>
    </xs:choice>
    <xs:attribute name="s" type="xs:double" use="required"/>
    <xs:attribute name="x" type="xs:double" use="required"/>
    <xs:attribute name="y" type="xs:double" use="required"/>
    <xs:attribute name="hdg" type="xs:double" use="required"/>
    <xs:attribute name="length" type="xs:double" use="required"/>
  </xs:complexType>
  <xs:complexType name="planViewType">
    <xs:sequence>
      <xs:element name="geometry" type="geometryType" maxOccurs="unbounded"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="roadType">
    <xs:sequence>
      <xs:element name="planView" type="planViewType"/>
      <xs:element name="elevationProfile" type="elevationProfileType" minOccurs="0"/>
      <xs:element name="lateralProfile" type="lateralProfileType" minOccurs="0"/>
      <xs:element name="lanes" type="lanesType" minOccurs="0"/>
    </xs:sequence>
    <xs:attribute name="id" type="xs:string" use="required"/>
    <xs:attribute name="name" type="xs:string"/>
    <xs:attribute name="length" type="xs:double" use="required"/>
    <xs:attribute name="junction" type="xs:string"/>
  </xs:complexType>
  <xs:complexType name="headerType">
    <xs:attribute name="revMajor" type="xs:int"/>
    <xs:attribute name="revMinor" type="xs:int"/>
  </xs:complexType>
  <xs:element name="OpenDRIVE">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="header" type="headerType"/>
        <xs:element name="road" type="roadType" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

SUBSET_INSTANCE = """\
<OpenDRIVE>
  <header revMajor="1" revMinor="4"/>
  <road id="7" name="Straight" length="10" junction="-1">
    <planView>
      <geometry s="0" x="0" y="0" hdg="0" length="10"><line/></geometry>
    </planView>
    <elevationProfile>
      <elevation s="0" a="100" b="0.5" c="0.25" d="0.125"/>
    </elevationProfile>
    <lateralProfile>
      <superelevation s="0" a="0.05" b="0" c="0" d="0"/>
    </lateralProfile>
    <lanes>
      <laneSection s="0">
        <left>
          <lane id="1" type="driving">
            <width sOffset="0" a="2" b="0" c="0" d="0"/>
          </lane>
        </left>
        <center>
          <lane id="0" type="none"/>
        </center>
        <right>
          <lane id="-1" type="driving">
            <width sOffset="0" a="3" b="0" c="0" d="0"/>
          </lane>
        </right>
      </laneSection>
    </lanes>
  </road>
  <road id="8" name="Arc" length="10" junction="-1">
    <planView>
      <geometry s="0" x="0" y="0" hdg="0" length="10"><arc curvature="0.02"/></geometry>
    </planView>
  </road>
  <road id="9" name="Offset" length="4" junction="-1">
    <planView>
      <geometry s="0" x="0" y="0" hdg="0" length="4"><line/></geometry>
    </planView>
    <lanes>
      <laneOffset s="0" a="0.5" b="0" c="0" d="0"/>
      <laneSection s="0">
        <left>
          <lane id="1" type="driving">
            <width sOffset="0" a="2" b="0" c="0" d="0"/>
          </lane>
        </left>
      </laneSection>
    </lanes>
  </road>
</OpenDRIVE>
"""


def _parse_subset(tmp_path: Path):
    schema_path = tmp_path / "subset.xsd"
    schema_path.write_text(SUBSET_SCHEMA, encoding="utf-8")
    instance_path = tmp_path / "subset.xodr"
    instance_path.write_text(SUBSET_INSTANCE, encoding="utf-8")
    document = Schema.compile(schema_path).parse(instance_path)
    assert not document.report.has_errors, document.report.issues
    return document


def test_sample_geometry_straight_line_is_exact():
    points = render_map.sample_geometry({"primitive": "line", "x": 0, "y": 0, "hdg": 0}, 10, 1.0)
    assert points == [(float(index), 0.0) for index in range(11)]


def test_sample_geometry_arc_matches_analytic_circle():
    curvature = 1.0 / 50.0
    points = render_map.sample_geometry(
        {"primitive": "arc", "x": 0, "y": 0, "hdg": 0, "curvature": curvature}, 10, 1.0
    )
    expected_x = math.sin(10 * curvature) / curvature
    expected_y = (1 - math.cos(10 * curvature)) / curvature
    assert points[0] == (0.0, 0.0)
    assert points[-1] == pytest.approx((expected_x, expected_y), abs=1e-6)
    assert len(points) == 11


def test_sample_geometry_spiral_with_constant_curvature_matches_arc():
    curvature = 1.0 / 50.0
    spiral = render_map.sample_geometry(
        {
            "primitive": "spiral",
            "x": 0,
            "y": 0,
            "hdg": 0,
            "curvStart": curvature,
            "curvEnd": curvature,
        },
        10,
        1.0,
    )
    arc = render_map.sample_geometry(
        {"primitive": "arc", "x": 0, "y": 0, "hdg": 0, "curvature": curvature}, 10, 1.0
    )
    assert spiral[-1] == pytest.approx(arc[-1], abs=1e-6)


def test_sample_geometry_samples_polynomials():
    poly3 = render_map.sample_geometry(
        {"primitive": "poly3", "x": 0, "y": 0, "hdg": 0, "a": 0, "b": 1, "c": 0, "d": 0},
        10,
        1.0,
    )
    assert poly3[-1] == pytest.approx((10.0, 10.0))
    assert poly3[5] == pytest.approx((5.0, 5.0))

    normalized = render_map.sample_geometry(
        {
            "primitive": "paramPoly3",
            "x": 0,
            "y": 0,
            "hdg": 0,
            "uA": 0,
            "uB": 10,
            "uC": 0,
            "uD": 0,
            "vA": 0,
            "vB": 0,
            "vC": 0.5,
            "vD": 0,
            "pRange": "normalized",
        },
        10,
        1.0,
    )
    assert normalized[-1] == pytest.approx((10.0, 0.5))
    assert normalized[5] == pytest.approx((5.0, 0.125))

    arc_length = render_map.sample_geometry(
        {
            "primitive": "paramPoly3",
            "x": 0,
            "y": 0,
            "hdg": 0,
            "uA": 0,
            "uB": 1,
            "uC": 0,
            "uD": 0,
            "vA": 0,
            "vB": 1,
            "vC": 0,
            "vD": 0,
            "pRange": "arcLength",
        },
        10,
        1.0,
    )
    assert arc_length[-1] == pytest.approx((10.0, 10.0))


def test_sample_geometry_rejects_bad_input():
    with pytest.raises(ValueError, match="step must be positive"):
        render_map.sample_geometry({"primitive": "line"}, 10, 0)
    with pytest.raises(ValueError, match="unsupported"):
        render_map.sample_geometry({"primitive": "bezier"}, 10)


def test_road_paths_offsets_lane_edges_and_samples_arc(tmp_path):
    document = _parse_subset(tmp_path)
    paths = render_map.road_paths(document)
    assert [path["road_id"] for path in paths] == ["7", "8", "9"]

    straight = paths[0]
    assert straight["length"] == 10.0
    assert straight["centerline"] == [(float(index), 0.0) for index in range(11)]

    lanes = {lane["id"]: lane["points"] for lane in straight["lanes"]}
    assert set(lanes) == {1, -1}
    assert lanes[1] == [(float(index), 2.0) for index in range(11)]
    assert lanes[-1] == [(float(index), -3.0) for index in range(11)]

    curvature = 1.0 / 50.0
    arc = paths[1]
    assert arc["lanes"] == []
    assert arc["centerline"][-1] == pytest.approx(
        (math.sin(10 * curvature) / curvature, (1 - math.cos(10 * curvature)) / curvature),
        abs=1e-6,
    )

    # laneOffset bends the whole cross-section by its cubic value at s.
    offset_lanes = {lane["id"]: lane["points"] for lane in paths[2]["lanes"]}
    assert offset_lanes[1] == [(float(index), 2.5) for index in range(5)]


def test_profile_samples_evaluate_cubic_exactly(tmp_path):
    profiles = profile_samples(_parse_subset(tmp_path), step=1.0)
    assert len(profiles) == 1
    profile = profiles[0]
    assert profile["road_id"] == "7"
    elevation = dict(profile["elevation"])
    assert elevation[0.0] == 100.0
    assert elevation[2.0] == 103.0
    assert elevation[10.0] == 100.0 + 5.0 + 25.0 + 125.0
    assert dict(profile["superelevation"])[2.0] == 0.05


def test_render_map_labels_roads_and_is_deterministic(tmp_path):
    document = _parse_subset(tmp_path)
    first = render_map.render_map(document, width=800)
    second = render_map.render_map(document, width=800)

    assert first == second
    assert first.startswith("<svg")
    assert first.count('class="centerline"') == 3
    assert 'class="lane"' in first
    assert '<text class="road-label"' in first
    for road_id in ("7", "8", "9"):
        assert f">{road_id}</text>" in first


@requires_sources
def test_town01_parses_and_renders():
    schema = compile_schema()
    document = schema.parse(DATA_DIR / "Town01.xodr")
    assert document.report.error_count == 0

    paths = render_map.road_paths(document)
    assert len(paths) == 98
    assert all(path["centerline"] for path in paths)

    svg = render_map.render_map(document)
    assert svg.startswith("<svg")
    assert svg.count('class="centerline"') == 98


@requires_sources
def test_soderleden_parses_with_the_1_7_schema():
    schema = compile_schema("1.7")
    document = schema.parse(DATA_DIR / "soderleden.xodr")
    assert document.report.error_count == 0

    paths = render_map.road_paths(document)
    assert len(paths) == 5
    assert all(len(path["centerline"]) > 1 for path in paths)


@requires_sources
def test_profiles_render_to_png(tmp_path):
    pytest.importorskip("matplotlib")
    schema = compile_schema()
    document = schema.parse(DATA_DIR / "curves_elevation.xodr")
    assert document.report.error_count == 0

    dest = plot_profiles(document, tmp_path / "profiles.png")
    assert dest == tmp_path / "profiles.png"
    assert dest.is_file()
    assert dest.stat().st_size > 1000
    assert dest.read_bytes().startswith(b"\x89PNG")
