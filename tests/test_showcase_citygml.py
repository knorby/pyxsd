"""CityGML showcase: offline subset fixtures plus cache-gated real models.

The synthetic CityGML subset exercises the renderer's contract without any
download: ring extraction from ``lod2Solid``/``boundedBy``, ``xlink:href``
surface members, ``OrientableSurface`` wrappers, ring closure, height
shading, deterministic SVG bytes, and the PNG plot.
"""

from __future__ import annotations

import re
from io import StringIO

import pytest
from examples.showcase.citygml import plan_svg
from examples.showcase.common.sources import SHOWCASE_ROOT

from pyxsd import ParseModes, Schema

CITYGML_SUBSET_SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:t="urn:example:citygml-subset"
           targetNamespace="urn:example:citygml-subset"
           elementFormDefault="qualified">
  <xs:simpleType name="DoubleList">
    <xs:list itemType="xs:double"/>
  </xs:simpleType>
  <xs:complexType name="LinearRingType">
    <xs:sequence><xs:element name="posList" type="t:DoubleList"/></xs:sequence>
  </xs:complexType>
  <xs:complexType name="RingPropertyType">
    <xs:sequence><xs:element name="LinearRing" type="t:LinearRingType"/></xs:sequence>
  </xs:complexType>
  <xs:complexType name="PolygonType">
    <xs:sequence><xs:element name="exterior" type="t:RingPropertyType"/></xs:sequence>
    <xs:attribute name="id" type="xs:string"/>
  </xs:complexType>
  <xs:complexType name="OrientableSurfaceType">
    <xs:sequence>
      <xs:element name="baseSurface" type="t:SurfacePropertyType" minOccurs="0"/>
    </xs:sequence>
    <xs:attribute name="orientation" type="xs:string"/>
  </xs:complexType>
  <xs:complexType name="SurfacePropertyType">
    <xs:choice minOccurs="0">
      <xs:element name="Polygon" type="t:PolygonType"/>
      <xs:element name="OrientableSurface" type="t:OrientableSurfaceType"/>
    </xs:choice>
    <xs:attribute name="href" type="xs:string"/>
  </xs:complexType>
  <xs:complexType name="CompositeSurfaceType">
    <xs:sequence>
      <xs:element name="surfaceMember" type="t:SurfacePropertyType" maxOccurs="unbounded"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="SolidType">
    <xs:sequence>
      <xs:element name="exterior">
        <xs:complexType>
          <xs:sequence><xs:element name="CompositeSurface" type="t:CompositeSurfaceType"/></xs:sequence>
        </xs:complexType>
      </xs:element>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="SolidPropertyType">
    <xs:sequence><xs:element name="Solid" type="t:SolidType"/></xs:sequence>
  </xs:complexType>
  <xs:complexType name="MultiSurfaceType">
    <xs:sequence>
      <xs:element name="surfaceMember" type="t:SurfacePropertyType" maxOccurs="unbounded"/>
    </xs:sequence>
  </xs:complexType>
  <xs:complexType name="MultiSurfacePropertyType">
    <xs:sequence><xs:element name="MultiSurface" type="t:MultiSurfaceType"/></xs:sequence>
  </xs:complexType>
  <xs:complexType name="WallSurfaceType">
    <xs:sequence><xs:element name="lod2MultiSurface" type="t:MultiSurfacePropertyType"/></xs:sequence>
  </xs:complexType>
  <xs:complexType name="BoundedByType">
    <xs:sequence><xs:element name="WallSurface" type="t:WallSurfaceType"/></xs:sequence>
  </xs:complexType>
  <xs:complexType name="BuildingPartType">
    <xs:sequence>
      <xs:element name="measuredHeight" type="xs:double" minOccurs="0"/>
      <xs:element name="lod2Solid" type="t:SolidPropertyType" minOccurs="0"/>
    </xs:sequence>
    <xs:attribute name="id" type="xs:string"/>
  </xs:complexType>
  <xs:complexType name="BuildingType">
    <xs:sequence>
      <xs:element name="measuredHeight" type="xs:double" minOccurs="0"/>
      <xs:element name="lod2Solid" type="t:SolidPropertyType" minOccurs="0"/>
      <xs:element name="boundedBy" type="t:BoundedByType" minOccurs="0" maxOccurs="unbounded"/>
      <xs:element name="BuildingPart" type="t:BuildingPartType" minOccurs="0"/>
    </xs:sequence>
    <xs:attribute name="id" type="xs:string"/>
  </xs:complexType>
  <xs:element name="CityModel">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="cityObjectMember" minOccurs="0" maxOccurs="unbounded">
          <xs:complexType>
            <xs:sequence><xs:element name="Building" type="t:BuildingType"/></xs:sequence>
          </xs:complexType>
        </xs:element>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

CITYGML_SUBSET_INSTANCE = """<?xml version="1.0" encoding="UTF-8"?>
<CityModel xmlns="urn:example:citygml-subset">
  <cityObjectMember>
    <Building id="short">
      <measuredHeight>3.0</measuredHeight>
      <lod2Solid>
        <Solid>
          <exterior>
            <CompositeSurface>
              <surfaceMember>
                <Polygon id="short-roof">
                  <exterior><LinearRing>
                    <posList>0 0 0 4 0 0 4 3 0 0 3 0</posList>
                  </LinearRing></exterior>
                </Polygon>
              </surfaceMember>
              <surfaceMember href="#shared-wall"/>
            </CompositeSurface>
          </exterior>
        </Solid>
      </lod2Solid>
      <boundedBy>
        <WallSurface>
          <lod2MultiSurface>
            <MultiSurface>
              <surfaceMember>
                <Polygon id="shared-wall">
                  <exterior><LinearRing>
                    <posList>0 0 0 0 3 0 0 3 3 0 0 3</posList>
                  </LinearRing></exterior>
                </Polygon>
              </surfaceMember>
            </MultiSurface>
          </lod2MultiSurface>
        </WallSurface>
      </boundedBy>
    </Building>
  </cityObjectMember>
  <cityObjectMember>
    <Building id="tall">
      <measuredHeight>12.0</measuredHeight>
      <lod2Solid>
        <Solid>
          <exterior>
            <CompositeSurface>
              <surfaceMember>
                <OrientableSurface orientation="-">
                  <baseSurface href="#tall-roof"/>
                </OrientableSurface>
              </surfaceMember>
            </CompositeSurface>
          </exterior>
        </Solid>
      </lod2Solid>
      <boundedBy>
        <WallSurface>
          <lod2MultiSurface>
            <MultiSurface>
              <surfaceMember>
                <Polygon id="tall-roof">
                  <exterior><LinearRing>
                    <posList>6 0 0 10 0 0 10 4 0 6 4 0 6 0 0</posList>
                  </LinearRing></exterior>
                </Polygon>
              </surfaceMember>
            </MultiSurface>
          </lod2MultiSurface>
        </WallSurface>
      </boundedBy>
    </Building>
  </cityObjectMember>
</CityModel>
"""

EMPTY_INSTANCE = (
    '<?xml version="1.0" encoding="UTF-8"?><CityModel xmlns="urn:example:citygml-subset"/>'
)

#: The OGC example models ship inside the CityGML schema archive.
CITYGML_BUILDING_DIR = (
    SHOWCASE_ROOT / ".cache" / "citygml-schemas" / "citygml" / "examples" / "2.0" / "building"
)
LOD2_SAMPLE = CITYGML_BUILDING_DIR / "Building_LOD2-EPSG25832.gml"
LOD1_SAMPLE = CITYGML_BUILDING_DIR / "Building_LOD1-EPSG25832.gml"
GARAGE_SAMPLE = CITYGML_BUILDING_DIR / "Building_and_garage_LOD2-EPSG25832.gml"


@pytest.fixture(scope="module")
def subset_schema():
    return Schema.compile(StringIO(CITYGML_SUBSET_SCHEMA), mode=ParseModes.NAMESPACED)


@pytest.fixture(scope="module")
def subset_document(subset_schema):
    return subset_schema.parse(StringIO(CITYGML_SUBSET_INSTANCE))


def _polygon_tags(svg: str) -> list[str]:
    return re.findall(r"<polygon\b[^>]*/>", svg)


def _point_count(tag: str) -> int:
    points = re.search(r'points="([^"]*)"', tag)
    assert points is not None
    return len(points.group(1).split())


def test_collect_buildings_rings_heights_and_references(subset_document):
    buildings = plan_svg.collect_buildings(subset_document)
    assert [building["id"] for building in buildings] == ["short", "tall"]
    assert [building["measured_height"] for building in buildings] == [3.0, 12.0]

    short, tall = buildings
    # Two surfaces: the inline roof plus the xlink-referenced wall.
    assert len(short["rings"]) == 2
    # One surface behind an OrientableSurface/baseSurface xlink chain.
    assert len(tall["rings"]) == 1

    for building in buildings:
        for ring in building["rings"]:
            assert len(ring) >= 4
            assert ring[0] == ring[-1], "rings must be closed"
            assert all(isinstance(value, float) for point in ring for value in point)


def test_collect_buildings_includes_building_parts(subset_schema):
    instance = """<?xml version="1.0" encoding="UTF-8"?>
<CityModel xmlns="urn:example:citygml-subset">
  <cityObjectMember>
    <Building id="host">
      <measuredHeight>5.0</measuredHeight>
      <BuildingPart id="annex"><measuredHeight>2.0</measuredHeight></BuildingPart>
    </Building>
  </cityObjectMember>
</CityModel>
"""
    document = subset_schema.parse(StringIO(instance))
    buildings = plan_svg.collect_buildings(document)
    assert [building["id"] for building in buildings] == ["host", "annex"]
    assert buildings[1]["measured_height"] == 2.0
    assert buildings[1]["rings"] == []  # no solid, no boundedBy geometry


def test_render_plan_draws_one_closed_polygon_per_surface(subset_document):
    svg = plan_svg.render_plan(subset_document)
    assert svg.startswith("<svg")
    assert 'fill="#f8fafc"' in svg
    tags = _polygon_tags(svg)
    assert len(tags) == 3
    assert all(_point_count(tag) >= 4 for tag in tags)
    assert all('stroke="#111827"' in tag for tag in tags)
    assert svg == plan_svg.render_plan(subset_document)
    assert svg == plan_svg.render_plan(subset_document, width=900)


def test_render_plan_shades_by_measured_height(subset_document):
    svg = plan_svg.render_plan(subset_document)
    fills: dict[str, set[str]] = {}
    for tag in _polygon_tags(svg):
        building = re.search(r'data-building="([^"]+)"', tag)
        fill = re.search(r'fill="([^"]+)"', tag)
        assert building is not None and fill is not None
        fills.setdefault(building.group(1), set()).add(fill.group(1))
    assert len(fills["short"]) == 1
    assert len(fills["tall"]) == 1
    assert fills["short"] != fills["tall"]


def test_render_plan_without_geometry_is_empty_but_valid(subset_schema):
    document = subset_schema.parse(StringIO(EMPTY_INSTANCE))
    svg = plan_svg.render_plan(document)
    assert "<svg" in svg
    assert "no building geometry" in svg
    assert "<polygon" not in svg


def test_missing_sample_reports_setup_hint(tmp_path, capsys):
    missing = tmp_path / "absent.gml"
    assert plan_svg.main([str(missing)]) == 2
    assert "download_sources.py" in capsys.readouterr().err


def test_sample_paths_are_ascii():
    """CI runs under LC_ALL=C on Linux; sample paths must stay ASCII."""
    for path in (LOD2_SAMPLE, LOD1_SAMPLE, GARAGE_SAMPLE):
        assert str(path).isascii()
