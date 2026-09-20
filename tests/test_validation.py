"""Unit tests for the validation reporting machinery and new enums."""

import pytest

from pyxsd.compositors import Compositor
from pyxsd.exceptions import PyXSDError, PyXSDWarning
from pyxsd.validation import (
    IssueSeverity,
    ValidationIssue,
    ValidationReport,
)


def _iter_er(er, found=None):
    """Every element representative in the tree rooted at *er*."""
    if found is None:
        found = []
    found.append(er)
    for child in getattr(er, "processedChildren", None) or []:
        if child is not None:
            _iter_er(child, found)
    return found


class TestIssueSeverity:
    def test_values(self):
        assert IssueSeverity.ERROR.value == "error"
        assert IssueSeverity.WARNING.value == "warning"


class TestValidationIssue:
    def test_format_without_element(self):
        issue = ValidationIssue(IssueSeverity.ERROR, "order", "expected 'quantity' elsewhere")
        assert issue.format() == "error: [order] expected 'quantity' elsewhere"

    def test_format_with_element(self):
        issue = ValidationIssue(
            IssueSeverity.WARNING, "schema", "bad minOccurs", element="itemType"
        )
        assert issue.format() == "warning: [schema] itemType: bad minOccurs"

    def test_issues_are_immutable(self):
        import dataclasses

        issue = ValidationIssue(IssueSeverity.ERROR, "order", "msg")
        with pytest.raises(dataclasses.FrozenInstanceError):
            issue.code = "other"


class TestValidationReport:
    def test_starts_empty(self):
        report = ValidationReport()
        assert len(report) == 0
        assert not report
        assert not report.has_errors
        assert report.issues == []

    def test_add_error(self):
        report = ValidationReport()
        report.add_error("boom", code="order", element="itemType")
        assert len(report) == 1
        assert report.has_errors
        issue = report.issues[0]
        assert issue.severity is IssueSeverity.ERROR
        assert issue.code == "order"
        assert issue.element == "itemType"

    def test_add_warning(self):
        report = ValidationReport()
        report.add_warning("careful", code="schema")
        assert report
        assert not report.has_errors
        assert report.warnings and not report.errors

    def test_errors_and_warnings_partition(self):
        report = ValidationReport()
        report.add_error("one", code="a")
        report.add_warning("two", code="b")
        report.add_error("three", code="c")
        assert [i.message for i in report.errors] == ["one", "three"]
        assert [i.message for i in report.warnings] == ["two"]
        assert len(report) == 3

    def test_iteration_yields_issues_in_order(self):
        report = ValidationReport()
        report.add_error("first", code="a")
        report.add_warning("second", code="b")
        assert [i.message for i in report] == ["first", "second"]

    def test_str_renders_header_and_lines(self):
        report = ValidationReport()
        report.add_error("boom", code="order", element="itemType")
        report.add_warning("careful", code="schema")
        rendered = str(report)
        assert rendered.splitlines()[0] == "pyxsd: 1 error, 1 warning"
        assert "  error: [order] itemType: boom" in rendered
        assert "  warning: [schema] careful" in rendered

    def test_str_pluralization(self):
        report = ValidationReport()
        report.add_error("a", code="x")
        report.add_error("b", code="y")
        assert str(report).splitlines()[0] == "pyxsd: 2 errors"


class TestExceptions:
    def test_hierarchy(self):
        assert issubclass(PyXSDError, Exception)
        assert issubclass(PyXSDWarning, UserWarning)


class TestCompositor:
    def test_members_compare_equal_to_lexical_names(self):
        assert Compositor.SEQUENCE == "sequence"
        assert Compositor.CHOICE == "choice"
        assert Compositor.ALL == "all"

    def test_lookup_from_tag_type(self):
        assert Compositor("sequence") is Compositor.SEQUENCE
        with pytest.raises(ValueError):
            Compositor("bogus")


class TestReportIntegration:
    def test_valid_fixtures_produce_empty_reports(self, tmp_path):
        from conftest import ALL_FIXTURES, run_parser

        for fixture in ALL_FIXTURES:
            doc = run_parser(fixture)
            assert not doc.report, fixture

    def test_post_binding_writes_log_instead_of_recording(self, caplog):
        """A descriptor write outside any parse logs; the schema report is untouched.

        The active-context binding report only exists during
        ``Schema.parse``, so a late write has no phase to attribute and
        must not retroactively invalidate an already-accepted schema.
        """
        import logging

        from conftest import run_parser

        doc = run_parser("primitives")
        root = doc.root
        descriptor = vars(type(root))["active"]
        before = len(doc.schema.report)
        with caplog.at_level(logging.ERROR, logger="pyxsd.element_representatives.attribute"):
            descriptor.__set__(root, "maybe")  # not a valid boolean lexical form
        assert len(doc.schema.report) == before
        assert "invalid value" in caplog.text


class TestSchemaPhaseRouting:
    """ER-layer lookup failures reach the owning report as schema issues."""

    @staticmethod
    def _er_tree(body):
        import xml.etree.ElementTree as ET

        from pyxsd.element_representatives.element_representative import (
            ElementRepresentative,
        )

        root = ET.fromstring(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">' + body + "</xs:schema>"
        )
        return ElementRepresentative.factory(root, None)

    @staticmethod
    def _report_context():
        from pyxsd.schema_context import SchemaContext, active_context, context_report
        from pyxsd.validation import ValidationReport

        report = ValidationReport()
        context = SchemaContext()
        return active_context(context), context_report(context, report), report

    def test_unknown_component_reference_reaches_schema_report(self):
        from io import StringIO

        import pyxsd

        xsd = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:element name="a"><xs:complexType>'
            '<xs:sequence><xs:element ref="nope"/></xs:sequence>'
            "</xs:complexType></xs:element></xs:schema>"
        )
        schema = pyxsd.Schema.compile(StringIO(xsd))
        codes = [i.code for i in schema.report]
        assert "unknown-component" in codes or any("nope" in (i.message) for i in schema.report)

    def test_failed_component_lookup_records_unknown_component(self):
        """A ``type`` naming no declaration records ``unknown-component``."""
        from io import StringIO

        import pyxsd

        xsd = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:complexType name="t"><xs:sequence>'
            '<xs:element name="x" type="noSuchType"/>'
            "</xs:sequence></xs:complexType>"
            '<xs:element name="a" type="t"/></xs:schema>'
        )
        schema = pyxsd.Schema.compile(StringIO(xsd))
        codes = [i.code for i in schema.report]
        assert "unknown-component" in codes
        assert all(i.phase == "schema" for i in schema.report)

    def test_unknown_type_reference_records_once_per_resolution(self):
        """The qualified lookup and its local-name fallback are one attempt.

        Report hygiene: one unknown ``type`` reference records a
        single ``unknown-component`` warning per resolution run — the
        pair used to record twice — and binding an instance adds at
        most one further record, in the parse's own report.
        """
        from io import StringIO

        import pyxsd

        xsd = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:complexType name="t"><xs:sequence>'
            '<xs:element name="x" type="noSuchType"/>'
            "</xs:sequence></xs:complexType>"
            '<xs:element name="a" type="t"/></xs:schema>'
        )
        schema = pyxsd.Schema.compile(StringIO(xsd))
        compile_issues = [i for i in schema.report if i.code == "unknown-component"]
        assert len(compile_issues) == 1
        assert compile_issues[0].element == "noSuchType"

        document = schema.parse(StringIO("<a><x/></a>"))
        merged = [i for i in document.report if i.code == "unknown-component"]
        # One record from the compile and one from the parse's own
        # resolution — never two within a single report.
        assert len(merged) == 2

    def test_unresolvable_union_member_records_only_unknown_type(self):
        """The union member site self-reports; the lookup stays silent."""
        from io import StringIO

        import pyxsd

        xsd = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:simpleType name="u"><xs:union memberTypes="noSuchMember"/>'
            "</xs:simpleType>"
            '<xs:element name="e" type="u"/></xs:schema>'
        )
        schema = pyxsd.Schema.compile(StringIO(xsd))
        codes = [i.code for i in schema.report]
        assert "unknown-component" not in codes
        assert codes.count("unknown-type") == 1

    def test_unresolvable_derivation_base_records_no_unknown_component(self):
        """The derivation site self-reports; the lookup stays silent."""
        from io import StringIO

        import pyxsd

        xsd = (
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<xs:complexType name="c"><xs:complexContent>'
            '<xs:extension base="noSuchBase">'
            '<xs:sequence><xs:element name="a"/></xs:sequence>'
            "</xs:extension></xs:complexContent></xs:complexType>"
            "</xs:schema>"
        )
        schema = pyxsd.Schema.compile(StringIO(xsd))
        codes = [i.code for i in schema.report]
        assert "unknown-component" not in codes
        assert codes.count("unknown-type") == 1

    def test_unresolvable_extension_base_records_unknown_base(self):
        """``Extension.addBaseToComplexType`` records on the active report."""
        extension = self._er_tree(
            "<xs:complexType name='t'><xs:complexContent>"
            "<xs:extension base='noSuchBase'>"
            "<xs:sequence><xs:element name='a'/></xs:sequence>"
            "</xs:extension></xs:complexContent></xs:complexType>"
        )
        extensions = [er for er in _iter_er(extension) if type(er).__name__ == "Extension"]
        enter, install, report = self._report_context()
        with enter, install:
            assert extensions[0].addBaseToComplexType() is None
        codes = [i.code for i in report]
        # The lookup inside the resolution stays silent (``warn=False``);
        # the extension's own failure is the single record.
        assert "unknown-base" in codes
        assert "unknown-component" not in codes
        base_issue = next(i for i in report if i.code == "unknown-base")
        assert base_issue.phase == "schema"
        assert base_issue.severity is IssueSeverity.WARNING

    def test_unbuildable_inline_union_member_records_union_member_skipped(self):
        """A skipped inline union member records on the active report."""
        from types import SimpleNamespace

        union = self._er_tree(
            "<xs:simpleType name='u'><xs:union memberTypes='xs:string'>"
            "<xs:simpleType><xs:restriction base='xs:string'/></xs:simpleType>"
            "</xs:union></xs:simpleType>"
        )
        simple_types = [er for er in _iter_er(union) if type(er).__name__ == "SimpleType"]
        u = next(er for er in simple_types if er.getName().startswith("u"))

        class _Unbuildable:
            name = "ghost-member"

            def clsFor(self, host):
                return None

        u.unionInline = [_Unbuildable()]
        enter, install, report = self._report_context()
        with enter, install:
            cls = u.makeUnionClass(SimpleNamespace(classes={}))
        assert cls is not None
        assert [i.code for i in report] == ["union-member-skipped"]
        assert report.issues[0].phase == "schema"
        assert report.issues[0].severity is IssueSeverity.WARNING


def test_validation_error_carries_report():
    from pyxsd.exceptions import PyXSDError, ValidationError
    from pyxsd.validation import ValidationReport

    report = ValidationReport()
    err = ValidationError("schema is invalid", report)
    assert isinstance(err, PyXSDError)
    assert err.report is report
    assert "invalid" in str(err)


def test_validation_error_exported_from_package_root():
    import pyxsd
    import pyxsd.exceptions

    assert "ValidationError" in pyxsd.__all__
    assert pyxsd.ValidationError is pyxsd.exceptions.ValidationError


def test_exception_taxonomy():
    import pyxsd
    import pyxsd.exceptions
    from pyxsd.exceptions import PyXSDError
    from pyxsd.namespaces import NamespaceError
    from pyxsd.xpath_subset import XPathError

    assert issubclass(NamespaceError, PyXSDError)
    assert issubclass(XPathError, PyXSDError)
    assert pyxsd.NamespaceError is pyxsd.exceptions.NamespaceError
    assert pyxsd.XPathError is pyxsd.exceptions.XPathError
    assert pyxsd.PyXSDWarning is pyxsd.exceptions.PyXSDWarning
    for name in ("PyXSDError", "PyXSDWarning", "ValidationError", "NamespaceError", "XPathError"):
        assert name in pyxsd.__all__
