"""Tests for the OpenDRIVE showcase workflow.

The offline tests run a synthetic, namespace-free OpenDRIVE subset through
pyxsd, then check the geometry sampler and lane offsetting analytically.
The cache-gated tests use the pinned real schemas and road networks fetched
by ``examples/showcase/download_sources.py``.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from examples.showcase.opendrive import render_map

from pyxsd.schema import Schema

SHOWCASE_ROOT = Path(__file__).resolve().parents[1] / "examples" / "showcase"
DATA_DIR = SHOWCASE_ROOT / ".cache" / "opendrive-data"
SCHEMA_DIR = SHOWCASE_ROOT / ".cache" / "opendrive-schemas"

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


