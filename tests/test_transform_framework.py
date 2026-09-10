"""Tests for the transform framework and the shipped standard transforms."""

import sys
import xml.etree.ElementTree as ET

import pytest

from conftest import canonicalize, fixture_dir, run_parser
from pyxsd.transforms.displayer import Displayer
from pyxsd.transforms.print_data import PrintData
from pyxsd.transforms.send_tree_to_pyxsd import SendTreeToPyXSD
from pyxsd.transforms.transform import Transform


class Walker(Transform):
    """Minimal concrete subclass so framework methods can be exercised."""

    def __init__(self):
        pass


class _ConcreteDisplayer(Displayer):
    """Displayer is abstract through Transform; instantiate via subclass."""

    def __init__(self):
        pass


@pytest.fixture
def walker():
    return Walker()


def make_tree():
    """Build a small synthetic instance tree.

    root
      |- alpha (value "one", attr kind="x")
      |    |- beta (value "two")
      |- alpha (value "three")
    """
    transform = Walker()
    root = transform.makeElemObj("root")
    root._value_ = None
    first = transform.makeElemObj("alpha")
    first._value_ = ["one"]
    first._attribs_ = {"kind": "x"}
    beta = transform.makeElemObj("beta")
    beta._value_ = ["two"]
    first._children_.append(beta)
    second = transform.makeElemObj("alpha")
    second._value_ = ["three"]
    root._children_.extend([first, second])
    return root


class TestTransformBase:
    def test_base_class_cannot_be_instantiated(self):
        with pytest.raises(TypeError, match="abstract"):
            Transform()

    def test_make_elem_obj_shape(self, walker):
        obj = walker.makeElemObj("thing")
        assert obj._name_ == "thing"
        assert obj._children_ == []
        assert obj._attribs_ == {}
        assert obj._value_ is None

    def test_make_comment_elem(self, walker):
        comment = walker.makeCommentElem("a note")
        assert comment._name_ == "_comment_"
        assert comment._value_ == "a note"

    def test_walk_visits_every_node(self, walker):
        root = make_tree()
        visited = []
        walker.walk(root, lambda inst, attrs, elems: visited.append(inst._name_))
        assert visited == ["root", "alpha", "beta", "alpha"]

    def test_walk_descends_into_lists_and_dicts(self, walker):
        root = make_tree()
        visited = []
        walker.walk({"key": [root, root]}, lambda inst, attrs, elems: visited.append(inst._name_))
        assert visited == ["root", "alpha", "beta", "alpha"] * 2

    def test_walk_skips_non_tree_objects(self, walker):
        visited = []
        walker.walk([1, "two", None], lambda inst, attrs, elems: visited.append(inst))
        assert visited == []

    def test_get_instances_by_class_name(self, walker):
        root = make_tree()
        collected = walker.getInstancesByClassName(root)
        # All synthetic nodes are instances of per-call classes that all
        # share the __name__ 'ElemObjClass', so they land under one key.
        # Walk order: root, then each child depth-first.
        assert [c._value_ for c in collected["ElemObjClass"]] == [
            None,
            ["one"],
            ["two"],
            ["three"],
        ]

    def test_get_all_sub_elements(self, walker):
        root = make_tree()
        sub = walker.getAllSubElements(root)
        assert sorted(k for k in sub if k) == ["alpha", "beta"]

    def test_get_elements_by_name(self, walker):
        root = make_tree()
        alphas = walker.getElementsByName(root, "alpha")
        assert [a._value_ for a in alphas] == [["one"], ["three"]]

    def test_find(self, walker):
        root = make_tree()
        assert walker.find("beta", root)._value_ == ["two"]
        assert walker.find("missing", root) is None

    def test_find_all(self, walker):
        root = make_tree()
        assert [a._value_ for a in walker.findAll("alpha", root)] == [["one"], ["three"]]
        assert walker.findAll("missing", root) is None


class TestDisplayer:
    def test_open_file_defaults_to_stdout(self):
        displayer = _ConcreteDisplayer()
        assert displayer.openFile(None) is sys.stdout

    def test_open_file_by_name(self, tmp_path):
        displayer = _ConcreteDisplayer()
        target = tmp_path / "out.xml"
        handle = displayer.openFile(str(target))
        handle.write("x")
        handle.close()
        assert target.read_text() == "x"


class TestPrintData:
    def test_writes_tree_to_file(self, tmp_path):
        root = make_tree()
        target = tmp_path / "printed.xml"
        result = PrintData(root)(str(target))
        assert result is root  # transforms return the (possibly new) root
        parsed = ET.parse(target)
        assert canonicalize(parsed.getroot())[0] == "root"
        assert [child.tag for child in parsed.getroot()] == ["alpha", "alpha"]

    def test_writes_tree_to_stdout(self, capsys):
        root = make_tree()
        PrintData(root)()
        out = capsys.readouterr().out
        assert "<root>" in out
        assert "one" in out


class TestSendTreeToPyXSD:
    def test_reparse_round_trip(self, tmp_path, monkeypatch):
        """The written tree is fed back into a fresh PyXSD parse."""
        monkeypatch.chdir(tmp_path)
        parser = run_parser("inventory")
        root = parser.parseXML()
        result = SendTreeToPyXSD(root)(
            xsdFile=str(fixture_dir("inventory") / "schema.xsd"),
            xmlFileOutput="_No_Output_",
        )
        assert result is root
        # Regression: the default transform-output filename was once
        # assigned to the parsed-output variable, leaking
        # 'tempFileTransformed.xml' into the working directory.
        assert list(tmp_path.iterdir()) == []


class TestTransformIntegration:
    def test_transform_pipeline_via_parser(self, tmp_path):
        """The parser runs loaded transforms in order and writes output."""
        output = tmp_path / "transformed.xml"
        run_parser(
            "inventory",
            xmlFileOutput=False,
            transformOutputName=str(output),
            transforms=["PrintData()"],
        )
        root = ET.parse(output).getroot()
        assert root.tag == "inventory"
        assert [item.attrib["id"] for item in root] == ["a1", "a2"]
