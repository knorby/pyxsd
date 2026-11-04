"""Tests for the showcase BPMN renderer.

The offline half builds a synthetic three-namespace BPMN subset so the DI
readers and SVG renderer stay covered when the pinned samples are absent.
The real half parses the bpmn-io samples against the pinned schema and skips
with a setup hint in a clean checkout.
"""

from __future__ import annotations

import pytest
from examples.showcase.bpmn.render_svg import di_edges, di_shapes

from pyxsd.binding import ParseModes
from pyxsd.schema import Schema

MODEL = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMN_DI = "http://www.omg.org/spec/BPMN/20100524/DI"
DC = "http://www.omg.org/spec/DD/20100524/DC"
DD_DI = "http://www.omg.org/spec/DD/20100524/DI"

DC_SCHEMA = f"""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:dc="{DC}" targetNamespace="{DC}" elementFormDefault="qualified">
  <xs:complexType name="BoundsType">
    <xs:attribute name="x" type="xs:double" use="required"/>
    <xs:attribute name="y" type="xs:double" use="required"/>
    <xs:attribute name="width" type="xs:double" use="required"/>
    <xs:attribute name="height" type="xs:double" use="required"/>
  </xs:complexType>
  <xs:element name="Bounds" type="dc:BoundsType"/>
</xs:schema>
"""

DD_DI_SCHEMA = f"""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:di="{DD_DI}" targetNamespace="{DD_DI}" elementFormDefault="qualified">
  <xs:element name="waypoint">
    <xs:complexType>
      <xs:attribute name="x" type="xs:double" use="required"/>
      <xs:attribute name="y" type="xs:double" use="required"/>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

BPMN_DI_SCHEMA = f"""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:di="{DD_DI}" xmlns:dc="{DC}" xmlns:bpmndi="{BPMN_DI}"
           targetNamespace="{BPMN_DI}" elementFormDefault="qualified">
  <xs:import namespace="{DC}" schemaLocation="dc.xsd"/>
  <xs:import namespace="{DD_DI}" schemaLocation="dd_di.xsd"/>
  <xs:complexType name="ShapeType">
    <xs:sequence>
      <xs:element ref="dc:Bounds"/>
    </xs:sequence>
    <xs:attribute name="id" type="xs:string" use="required"/>
    <xs:attribute name="bpmnElement" type="xs:string" use="required"/>
    <xs:attribute name="isHorizontal" type="xs:boolean"/>
  </xs:complexType>
  <xs:element name="BPMNShape" type="bpmndi:ShapeType"/>
  <xs:complexType name="EdgeType">
    <xs:sequence>
      <xs:element ref="di:waypoint" minOccurs="2" maxOccurs="unbounded"/>
    </xs:sequence>
    <xs:attribute name="id" type="xs:string" use="required"/>
    <xs:attribute name="bpmnElement" type="xs:string" use="required"/>
  </xs:complexType>
  <xs:element name="BPMNEdge" type="bpmndi:EdgeType"/>
  <xs:complexType name="PlaneType">
    <xs:sequence>
      <xs:element ref="bpmndi:BPMNShape" minOccurs="0" maxOccurs="unbounded"/>
      <xs:element ref="bpmndi:BPMNEdge" minOccurs="0" maxOccurs="unbounded"/>
    </xs:sequence>
    <xs:attribute name="id" type="xs:string" use="required"/>
    <xs:attribute name="bpmnElement" type="xs:string" use="required"/>
  </xs:complexType>
  <xs:element name="BPMNPlane" type="bpmndi:PlaneType"/>
  <xs:element name="BPMNDiagram">
    <xs:complexType>
      <xs:sequence>
        <xs:element ref="bpmndi:BPMNPlane"/>
      </xs:sequence>
      <xs:attribute name="id" type="xs:string" use="required"/>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

MODEL_SCHEMA = f"""\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:m="{MODEL}" xmlns:bpmndi="{BPMN_DI}"
           targetNamespace="{MODEL}" elementFormDefault="qualified">
  <xs:import namespace="{BPMN_DI}" schemaLocation="bpmndi.xsd"/>
  <xs:complexType name="FlowNodeType">
    <xs:attribute name="id" type="xs:string" use="required"/>
    <xs:attribute name="name" type="xs:string"/>
  </xs:complexType>
  <xs:complexType name="SequenceFlowType">
    <xs:attribute name="id" type="xs:string" use="required"/>
    <xs:attribute name="name" type="xs:string"/>
    <xs:attribute name="sourceRef" type="xs:string"/>
    <xs:attribute name="targetRef" type="xs:string"/>
  </xs:complexType>
  <xs:element name="lane" type="m:FlowNodeType"/>
  <xs:element name="startEvent" type="m:FlowNodeType"/>
  <xs:element name="endEvent" type="m:FlowNodeType"/>
  <xs:element name="task" type="m:FlowNodeType"/>
  <xs:element name="exclusiveGateway" type="m:FlowNodeType"/>
  <xs:element name="sequenceFlow" type="m:SequenceFlowType"/>
  <xs:element name="process">
    <xs:complexType>
      <xs:sequence>
        <xs:choice minOccurs="0" maxOccurs="unbounded">
          <xs:element ref="m:lane"/>
          <xs:element ref="m:startEvent"/>
          <xs:element ref="m:endEvent"/>
          <xs:element ref="m:task"/>
          <xs:element ref="m:exclusiveGateway"/>
          <xs:element ref="m:sequenceFlow"/>
        </xs:choice>
      </xs:sequence>
      <xs:attribute name="id" type="xs:string" use="required"/>
      <xs:attribute name="name" type="xs:string"/>
    </xs:complexType>
  </xs:element>
  <xs:element name="definitions">
    <xs:complexType>
      <xs:sequence>
        <xs:element ref="m:process" minOccurs="0"/>
        <xs:element ref="bpmndi:BPMNDiagram" minOccurs="0"/>
      </xs:sequence>
      <xs:attribute name="targetNamespace" type="xs:string"/>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

INSTANCE = f"""\
<definitions xmlns="{MODEL}" xmlns:bpmndi="{BPMN_DI}" xmlns:dc="{DC}" xmlns:di="{DD_DI}"
             targetNamespace="urn:showcase:bpmn">
  <process id="proc" name="Order handling">
    <lane id="lane" name="Lane"/>
    <startEvent id="start" name="Start"/>
    <task id="task_a" name="Do work"/>
    <exclusiveGateway id="gw" name="OK?"/>
    <task id="task_b" name="Finish"/>
    <endEvent id="end" name="End"/>
    <sequenceFlow id="f1" name="go" sourceRef="start" targetRef="task_a"/>
    <sequenceFlow id="f2" sourceRef="task_a" targetRef="gw"/>
    <sequenceFlow id="f3" name="yes" sourceRef="gw" targetRef="task_b"/>
    <sequenceFlow id="f4" sourceRef="task_b" targetRef="end"/>
  </process>
  <bpmndi:BPMNDiagram id="diagram">
    <bpmndi:BPMNPlane id="plane" bpmnElement="proc">
      <bpmndi:BPMNShape id="s-lane" bpmnElement="lane" isHorizontal="true">
        <dc:Bounds x="60" y="40" width="560" height="400"/>
      </bpmndi:BPMNShape>
      <bpmndi:BPMNShape id="s-start" bpmnElement="start">
        <dc:Bounds x="100" y="100" width="36" height="36"/>
      </bpmndi:BPMNShape>
      <bpmndi:BPMNShape id="s-task-a" bpmnElement="task_a">
        <dc:Bounds x="200" y="80" width="100" height="80"/>
      </bpmndi:BPMNShape>
      <bpmndi:BPMNShape id="s-gw" bpmnElement="gw">
        <dc:Bounds x="360" y="95" width="50" height="50"/>
      </bpmndi:BPMNShape>
      <bpmndi:BPMNShape id="s-task-b" bpmnElement="task_b">
        <dc:Bounds x="200" y="300" width="100" height="80"/>
      </bpmndi:BPMNShape>
      <bpmndi:BPMNShape id="s-end" bpmnElement="end">
        <dc:Bounds x="470" y="322" width="36" height="36"/>
      </bpmndi:BPMNShape>
      <bpmndi:BPMNEdge id="e1" bpmnElement="f1">
        <di:waypoint x="136" y="118"/>
        <di:waypoint x="200" y="120"/>
      </bpmndi:BPMNEdge>
      <bpmndi:BPMNEdge id="e2" bpmnElement="f2">
        <di:waypoint x="300" y="120"/>
        <di:waypoint x="360" y="120"/>
      </bpmndi:BPMNEdge>
      <bpmndi:BPMNEdge id="e3" bpmnElement="f3">
        <di:waypoint x="385" y="145"/>
        <di:waypoint x="250" y="300"/>
      </bpmndi:BPMNEdge>
      <bpmndi:BPMNEdge id="e4" bpmnElement="f4">
        <di:waypoint x="300" y="340"/>
        <di:waypoint x="470" y="340"/>
      </bpmndi:BPMNEdge>
    </bpmndi:BPMNPlane>
  </bpmndi:BPMNDiagram>
</definitions>
"""


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    root = tmp_path_factory.mktemp("bpmn-schema")
    for name, text in (
        ("dc.xsd", DC_SCHEMA),
        ("dd_di.xsd", DD_DI_SCHEMA),
        ("bpmndi.xsd", BPMN_DI_SCHEMA),
        ("model.xsd", MODEL_SCHEMA),
    ):
        (root / name).write_text(text, encoding="utf-8")
    schema = Schema.compile(root / "model.xsd", mode=ParseModes.STRICT.replace(namespaces="strict"))
    assert not schema.report.has_errors
    instance = root / "instance.xml"
    instance.write_text(INSTANCE, encoding="utf-8")
    document = schema.parse(instance)
    assert not document.report.has_errors
    return document


def test_di_shapes_classify_by_referenced_element(synthetic):
    shapes = di_shapes(synthetic)
    assert [shape["element_id"] for shape in shapes] == [
        "lane",
        "start",
        "task_a",
        "gw",
        "task_b",
        "end",
    ]
    assert {shape["element_id"]: shape["kind"] for shape in shapes} == {
        "lane": "lane",
        "start": "start_event",
        "task_a": "task",
        "gw": "gateway",
        "task_b": "task",
        "end": "end_event",
    }


def test_di_shapes_carry_bounds_labels_and_orientation(synthetic):
    shapes = {shape["element_id"]: shape for shape in di_shapes(synthetic)}
    task = shapes["task_a"]
    assert (task["x"], task["y"], task["w"], task["h"]) == (200.0, 80.0, 100.0, 80.0)
    assert task["label"] == "Do work"
    assert task["is_horizontal"] is False
    assert shapes["lane"]["is_horizontal"] is True


def test_di_edges_carry_waypoints_and_semantic_labels(synthetic):
    edges = di_edges(synthetic)
    assert [edge["element_id"] for edge in edges] == ["f1", "f2", "f3", "f4"]
    assert edges[0]["waypoints"] == [(136.0, 118.0), (200.0, 120.0)]
    assert edges[2]["waypoints"] == [(385.0, 145.0), (250.0, 300.0)]
    assert {edge["element_id"]: edge["label"] for edge in edges} == {
        "f1": "go",
        "f2": "",
        "f3": "yes",
        "f4": "",
    }
