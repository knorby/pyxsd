"""CityGML examples: offline subset fixtures plus cache-gated real models.

The synthetic CityGML subset exercises the renderer's contract without any
download: ring extraction from ``lod2Solid``/``boundedBy``, ``xlink:href``
surface members, ``OrientableSurface`` wrappers, ring closure, height
shading, deterministic SVG bytes, and the PNG plot.
"""

from __future__ import annotations

import re
from io import StringIO

import pytest
from examples.citygml import plan_svg, plot_city3d
from examples.citygml.common.schemas import compile_schema
from examples.citygml.common.sources import FAMILY_ROOT

from pyxsd import ParseModes, Schema


def test_cli_defaults_use_the_citygml_family_root():
    assert plan_svg.FAMILY_ROOT == FAMILY_ROOT
    assert plan_svg.OUT_DIR == FAMILY_ROOT / "out"
    assert plan_svg.DEFAULT_SAMPLE == (
        FAMILY_ROOT
        / ".cache/citygml-schemas/citygml/examples/2.0/building/Building_LOD2-EPSG25832.gml"
    )


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
    FAMILY_ROOT / ".cache" / "citygml-schemas" / "citygml" / "examples" / "2.0" / "building"
)
LOD2_SAMPLE = CITYGML_BUILDING_DIR / "Building_LOD2-EPSG25832.gml"
LOD1_SAMPLE = CITYGML_BUILDING_DIR / "Building_LOD1-EPSG25832.gml"
GARAGE_SAMPLE = CITYGML_BUILDING_DIR / "Building_and_garage_LOD2-EPSG25832.gml"

requires_cache = pytest.mark.skipif(
    not LOD2_SAMPLE.is_file(),
    reason=(
        "CityGML examples are not installed; run: "
        "uv run python examples/citygml/download_sources.py"
    ),
)


@pytest.fixture(scope="module")
def subset_schema():
    return Schema.compile(StringIO(CITYGML_SUBSET_SCHEMA), mode=ParseModes.NAMESPACED)


@pytest.fixture(scope="module")
def subset_document(subset_schema):
    return subset_schema.parse(StringIO(CITYGML_SUBSET_INSTANCE))


@pytest.fixture(scope="module")
def citygml_schema():
    return compile_schema()


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
    assert all('stroke="#000000"' in tag for tag in tags)
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


def test_plot_city_3d_writes_png(tmp_path, subset_document):
    pytest.importorskip("matplotlib")
    dest = tmp_path / "city3d.png"
    written = plot_city3d.plot_city_3d(subset_document, dest)
    assert written == dest
    assert dest.stat().st_size > 0
    assert dest.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


@requires_cache
def test_scripts_run_from_the_command_line(tmp_path):
    svg_path = tmp_path / "plan.svg"
    assert plan_svg.main([str(LOD2_SAMPLE), "-o", str(svg_path)]) == 0
    assert svg_path.read_text(encoding="utf-8").startswith("<svg")

    pytest.importorskip("matplotlib")
    png_path = tmp_path / "city3d.png"
    assert plot_city3d.main([str(LOD2_SAMPLE), "-o", str(png_path)]) == 0
    assert png_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_missing_sample_reports_setup_hint(tmp_path, capsys):
    missing = tmp_path / "absent.gml"
    assert plan_svg.main([str(missing)]) == 2
    assert "download_sources.py" in capsys.readouterr().err


@requires_cache
def test_real_lod2_building_geometry(citygml_schema):
    document = citygml_schema.parse(LOD2_SAMPLE)
    buildings = plan_svg.collect_buildings(document)
    assert len(buildings) >= 1
    building = buildings[0]
    assert len(building["rings"]) >= 6
    assert building["measured_height"] == pytest.approx(5.0)
    assert all(ring[0] == ring[-1] for ring in building["rings"])


@requires_cache
def test_real_lod2_known_schema_findings(citygml_schema):
    """Stock GML 3.1.1 closure findings, plus the instance's three errors."""
    report = citygml_schema.parse(LOD2_SAMPLE).report
    assert report.error_count <= 30
    codes = {issue.code for issue in report.issues}
    assert {"particle-restriction", "unknown-attributeRef", "order"} <= codes


@requires_cache
def test_real_lod1_building_geometry(citygml_schema):
    document = citygml_schema.parse(LOD1_SAMPLE)
    buildings = plan_svg.collect_buildings(document)
    assert len(buildings) >= 1
    assert len(buildings[0]["rings"]) >= 6
    assert buildings[0]["measured_height"] == pytest.approx(5.0)
    assert "<polygon" in plan_svg.render_plan(document)


@requires_cache
def test_real_garage_model_lists_building_part(citygml_schema):
    document = citygml_schema.parse(GARAGE_SAMPLE)
    buildings = plan_svg.collect_buildings(document)
    assert len(buildings) == 2
    assert buildings[1]["measured_height"] == pytest.approx(2.5)
    assert sum(len(building["rings"]) for building in buildings) >= 12


@requires_cache
def test_real_model_renders_svg_and_png(citygml_schema, tmp_path):
    document = citygml_schema.parse(LOD2_SAMPLE)
    svg = plan_svg.render_plan(document)
    assert "<svg" in svg
    assert "<polygon" in svg
    assert svg == plan_svg.render_plan(document)

    pytest.importorskip("matplotlib")
    dest = tmp_path / "lod2.png"
    plot_city3d.plot_city_3d(document, dest)
    assert dest.stat().st_size > 0


def test_sample_paths_are_ascii():
    """CI runs under LC_ALL=C on Linux; sample paths must stay ASCII."""
    for path in (LOD2_SAMPLE, LOD1_SAMPLE, GARAGE_SAMPLE):
        assert path.relative_to(FAMILY_ROOT).as_posix().isascii()


def test_single_schema_family_rejects_variants():
    from examples.citygml.common import schemas

    assert not hasattr(schemas, "ENTRIES")
    with pytest.raises(KeyError, match="single schema entry"):
        compile_schema("1.7")
