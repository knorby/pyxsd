"""Tests for parse modes and lax binding.

A parse mode changes only what value is bound into the tree; the
validation report is always strict. These tests pin both halves of that
promise for every policy field, plus the CLI switch.
"""

from io import StringIO

import pytest

import pyxsd
from conftest import fixture_dir
from pyxsd.binding import BindingPolicy, ParseModes
from pyxsd.cli import main
from pyxsd.schema import Schema

XSD = "http://www.w3.org/2001/XMLSchema"


def _schema(body: str) -> str:
    return f'<xs:schema xmlns:xs="{XSD}">{body}</xs:schema>'


TARGETED_XSD = f"""<xs:schema xmlns:xs="{XSD}" targetNamespace="urn:expected"
    elementFormDefault="qualified">
  <xs:element name="root" type="xs:string"/>
</xs:schema>"""
WRONG_NAMESPACE_XML = '<root xmlns="urn:other">text</root>'


def _parse(schema_body, instance_text, tmp_path, mode=ParseModes.STRICT):
    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(_schema(schema_body))
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text(instance_text)
    return Schema.compile(str(schema_path), mode=mode).parse(str(instance_path))


def _codes(doc):
    return [issue.code for issue in doc.report.issues]


class TestPolicyModel:
    def test_default_policy_is_namespaced(self):
        assert BindingPolicy() == ParseModes.NAMESPACED
        assert BindingPolicy().namespaces == "strict"

    def test_strict_preset_keeps_legacy_namespaces(self):
        policy = ParseModes.STRICT
        assert policy.invalid_value == "drop"
        assert policy.unresolved_type == "error"
        assert policy.undeclared_content == "error"
        assert policy.whitespace == "xsd"
        assert policy.namespaces == "legacy"

    def test_lax_preset_keeps_legacy_namespaces(self):
        policy = ParseModes.LAX
        assert policy.invalid_value == "raw"
        assert policy.unresolved_type == "generic"
        assert policy.undeclared_content == "generic"
        assert policy.whitespace == "xsd"
        assert policy.namespaces == "legacy"

    def test_namespaced_preset_is_strict_binding_with_strict_namespaces(self):
        policy = ParseModes.NAMESPACED
        assert policy.namespaces == "strict"
        # Namespace handling is the only difference from plain strict.
        assert policy.invalid_value == "drop"
        assert policy.unresolved_type == "error"
        assert policy.undeclared_content == "error"
        assert policy.whitespace == "xsd"

    def test_lax_namespaced_preset(self):
        policy = ParseModes.LAX_NAMESPACED
        assert policy == ParseModes.LAX.replace(namespaces="strict")
        assert policy.namespaces == "strict"
        assert policy.invalid_value == "raw"
        assert policy.unresolved_type == "generic"
        assert policy.undeclared_content == "generic"
        assert policy.whitespace == "xsd"

    def test_namespaces_can_be_composed_with_replace(self):
        policy = ParseModes.LAX_NAMESPACED.replace(whitespace="compat")
        assert policy.namespaces == "strict"
        assert policy.invalid_value == "raw"
        assert policy.whitespace == "compat"

    def test_replace_returns_a_modified_copy(self):
        base = ParseModes.STRICT
        custom = base.replace(whitespace="compat")
        assert custom.whitespace == "compat"
        # The original preset is untouched.
        assert base.whitespace == "xsd"
        assert custom.invalid_value == base.invalid_value


class TestNamespaceDefaults:
    @pytest.mark.parametrize("entry_point", ["schema", "module"])
    def test_omitted_mode_uses_namespaced_binding(self, entry_point):
        if entry_point == "schema":
            document = Schema.compile(StringIO(TARGETED_XSD)).parse(StringIO(WRONG_NAMESPACE_XML))
        else:
            document = pyxsd.parse(
                StringIO(WRONG_NAMESPACE_XML),
                xsd=StringIO(TARGETED_XSD),
            )
        assert document.root is None
        assert "unknown-root" in _codes(document)

    def test_explicit_namespaced_preset_matches_the_default(self):
        document = Schema.compile(StringIO(TARGETED_XSD), mode=ParseModes.NAMESPACED).parse(
            StringIO(WRONG_NAMESPACE_XML)
        )
        assert document.root is None
        assert "unknown-root" in _codes(document)

    def test_explicit_strict_preset_retains_legacy_binding(self):
        document = Schema.compile(StringIO(TARGETED_XSD), mode=ParseModes.STRICT).parse(
            StringIO(WRONG_NAMESPACE_XML)
        )
        assert str(document.root) == "text"
        assert _codes(document) == []


class TestInvalidValueBinding:
    ROOT_INT = '<xs:element name="r" type="xs:int"/>'

    def test_default_mode_drops_an_invalid_no_namespace_value(self):
        document = Schema.compile(StringIO(_schema(self.ROOT_INT))).parse(StringIO("<r>abc</r>"))
        assert "value" in _codes(document)
        assert str(document.root) == "0"

    def test_lax_namespaced_binds_a_raw_value(self, tmp_path):
        document = _parse(
            self.ROOT_INT,
            "<r>abc</r>",
            tmp_path,
            mode=ParseModes.LAX_NAMESPACED,
        )
        assert "value" in _codes(document)
        assert str(document.root) == "abc"

    def test_strict_drops_an_invalid_root_value(self, tmp_path):
        doc = _parse(self.ROOT_INT, "<r>abc</r>", tmp_path)
        assert "value" in _codes(doc)
        assert doc.report.has_errors
        # The stand-in for an invalid int is the unvalidated zero.
        assert str(doc.root) == "0"

    def test_lax_binds_the_raw_root_value(self, tmp_path):
        doc = _parse(self.ROOT_INT, "<r>abc</r>", tmp_path, mode=ParseModes.LAX)
        assert "value" in _codes(doc)
        assert doc.report.has_errors
        assert str(doc.root) == "abc"

    def test_lax_preserves_the_original_spelling(self, tmp_path):
        doc = _parse(self.ROOT_INT, "<r> a </r>", tmp_path, mode=ParseModes.LAX)
        assert str(doc.root) == " a "

    CHILD_INT = (
        '<xs:element name="r"><xs:complexType><xs:sequence>'
        '<xs:element name="a" type="xs:int"/>'
        "</xs:sequence></xs:complexType></xs:element>"
    )

    def test_strict_drops_an_invalid_child(self, tmp_path):
        doc = _parse(self.CHILD_INT, "<r><a>abc</a></r>", tmp_path)
        assert "value" in _codes(doc)
        root = doc.root
        assert "a" not in root.__dict__
        assert root._children_ == []

    def test_lax_binds_the_raw_child(self, tmp_path):
        doc = _parse(self.CHILD_INT, "<r><a>abc</a></r>", tmp_path, mode=ParseModes.LAX)
        assert "value" in _codes(doc)
        child = doc.root.a
        assert child._name_ == "a"
        assert str(child) == "abc"


class TestGenericBinding:
    UNRESOLVED = (
        '<xs:element name="r"><xs:complexType><xs:sequence>'
        '<xs:element name="a" type="Nope"/>'
        "</xs:sequence></xs:complexType></xs:element>"
    )

    def test_strict_drops_an_unresolved_subtree(self, tmp_path):
        doc = _parse(self.UNRESOLVED, "<r><a>x</a></r>", tmp_path)
        assert "unknown-type" in _codes(doc)
        assert doc.root._children_ == []

    def test_lax_binds_an_unresolved_subtree_generically(self, tmp_path):
        doc = _parse(self.UNRESOLVED, "<r><a>x</a></r>", tmp_path, mode=ParseModes.LAX)
        assert "unknown-type" in _codes(doc)
        names = [child._name_ for child in doc.root._children_]
        assert names == ["a"]

    SEQUENCE_A = (
        '<xs:element name="r"><xs:complexType><xs:sequence>'
        '<xs:element name="a" type="xs:string"/>'
        "</xs:sequence></xs:complexType></xs:element>"
    )

    def test_strict_drops_undeclared_content(self, tmp_path):
        doc = _parse(self.SEQUENCE_A, "<r><a/><b/></r>", tmp_path)
        assert "unexpected-element" in _codes(doc)
        names = [child._name_ for child in doc.root._children_]
        assert names == ["a"]

    def test_lax_binds_undeclared_content_generically(self, tmp_path):
        doc = _parse(self.SEQUENCE_A, "<r><a/><b/></r>", tmp_path, mode=ParseModes.LAX)
        assert "unexpected-element" in _codes(doc)
        names = [child._name_ for child in doc.root._children_]
        assert names == ["a", "b"]


class TestWhitespaceCompatibility:
    ROOT_INT = '<xs:element name="r" type="xs:integer"/>'
    NBSP_INT = "<r>\u00a01\u00a0</r>"

    def test_xsd_whitespace_rejects_nbsp_padding(self, tmp_path):
        doc = _parse(self.ROOT_INT, self.NBSP_INT, tmp_path)
        assert "value" in _codes(doc)

    def test_lax_preset_keeps_xsd_whitespace(self, tmp_path):
        doc = _parse(self.ROOT_INT, self.NBSP_INT, tmp_path, mode=ParseModes.LAX)
        assert "value" in _codes(doc)

    def test_compat_whitespace_folds_nbsp(self, tmp_path):
        doc = _parse(
            self.ROOT_INT,
            self.NBSP_INT,
            tmp_path,
            mode=BindingPolicy(whitespace="compat"),
        )
        assert _codes(doc) == []
        assert int(doc.root) == 1


class TestReportStaysStrictUnderLax:
    def test_every_problem_is_still_reported(self, tmp_path):
        schema = (
            '<xs:element name="r"><xs:complexType><xs:sequence>'
            '<xs:element name="a" type="xs:int"/>'
            '<xs:element name="b" type="xs:string"/>'
            "</xs:sequence></xs:complexType></xs:element>"
        )
        doc = _parse(schema, "<r><a>bad</a><x/></r>", tmp_path, mode=ParseModes.LAX)
        codes = _codes(doc)
        assert "value" in codes
        assert "unexpected-element" in codes or "order" in codes
        assert doc.report.has_errors


class TestCliModeFlag:
    def _stage(self, tmp_path):
        for name in ("schema.xsd", "instance.xml"):
            target = tmp_path / name
            target.write_text((fixture_dir("inventory") / name).read_text())
        return tmp_path / "instance.xml"

    def test_lax_mode_is_accepted(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._stage(tmp_path)
        main(["-i", "instance.xml", "--mode", "lax", "-o", "/dev/null"])

    def test_unknown_mode_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._stage(tmp_path)
        with pytest.raises(SystemExit):
            main(["-i", "instance.xml", "--mode", "bogus", "-o", "/dev/null"])

    def test_strict_namespaces_flag_is_accepted(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._stage(tmp_path)
        main(["-i", "instance.xml", "--namespaces", "strict", "-o", "/dev/null"])

    def test_default_namespaces_are_strict(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "schema.xsd").write_text(TARGETED_XSD)
        (tmp_path / "instance.xml").write_text(WRONG_NAMESPACE_XML)
        with pytest.raises(SystemExit) as exited:
            main(
                [
                    "-i",
                    "instance.xml",
                    "-s",
                    "schema.xsd",
                    "--strict",
                    "-o",
                    "/dev/null",
                ]
            )
        assert exited.value.code == 1

    def test_legacy_namespaces_flag_retains_local_name_binding(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "schema.xsd").write_text(TARGETED_XSD)
        (tmp_path / "instance.xml").write_text(WRONG_NAMESPACE_XML)
        main(
            [
                "-i",
                "instance.xml",
                "-s",
                "schema.xsd",
                "--namespaces",
                "legacy",
                "--strict",
                "-o",
                "/dev/null",
            ]
        )

    def test_unknown_namespaces_flag_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._stage(tmp_path)
        with pytest.raises(SystemExit):
            main(["-i", "instance.xml", "--namespaces", "bogus", "-o", "/dev/null"])
