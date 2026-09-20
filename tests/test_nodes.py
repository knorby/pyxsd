"""Tests for the runtime node protocol (:mod:`pyxsd.nodes`)."""

from conftest import run_parser
from pyxsd import XMLNode
from pyxsd.transforms import Transform
from pyxsd.transforms.print_data import PrintData


class TestXMLNodeProtocol:
    def test_generated_instances_satisfy_the_protocol(self):
        doc = run_parser("primitives")
        root = doc.root
        assert isinstance(root, XMLNode)

    def test_synthetic_transform_nodes_satisfy_the_protocol(self):
        doc = run_parser("primitives")
        root = doc.root
        elemObj = Transform.makeElemObj(PrintData(root), "sample")
        assert isinstance(elemObj, XMLNode)
