"""SendTreeToPyXSD observability tests.

Revalidation inside SendTreeToPyXSD used to construct a PyXSD object
and discard it, so its validation report vanished: CLI --strict (which
inspects the outer run's report) could not see problems introduced by a
transformed tree, and the temporary file backing the reparse was never
closed.
"""

from conftest import fixture_dir, run_parser
from pyxsd.binding import ParseModes
from pyxsd.transforms.send_tree_to_pyxsd import SendTreeToPyXSD
from pyxsd.validation import ValidationReport

XSD = str(fixture_dir("primitives") / "schema.xsd")


def make_parser_and_invalid_tree():
    """A parser for the primitives fixture whose tree holds a bad value."""
    parser = run_parser("primitives")
    root = parser.schemaRootInstance
    # The bound tree stores leaf values as single-element lists.
    root.count._value_ = ["not-an-int"]
    return parser, root


def codes(report):
    return [issue.code for issue in report]


class TestTransformReport:
    def test_inner_errors_exposed_on_transform(self, tmp_path, monkeypatch):
        """The revalidation report survives on the transform object."""
        monkeypatch.chdir(tmp_path)
        _, root = make_parser_and_invalid_tree()
        transformer = SendTreeToPyXSD(root)
        result = transformer(xsdFile=XSD)
        assert result is root
        assert transformer.report is not None
        assert "value" in codes(transformer.report)

    def test_clean_tree_has_empty_report(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        parser = run_parser("primitives")
        transformer = SendTreeToPyXSD(parser.schemaRootInstance)
        transformer(xsdFile=XSD)
        assert len(transformer.report) == 0

    def test_temp_input_stream_is_closed(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        parser = run_parser("primitives")
        transformer = SendTreeToPyXSD(parser.schemaRootInstance)
        transformer(xsdFile=XSD)
        assert transformer._xmlInput is not None
        assert transformer._xmlInput.closed


class TestReportPropagation:
    def test_inner_issues_reach_outer_parser_report(self, tmp_path, monkeypatch):
        """parser.transform() merges a reparse's issues into its report."""
        monkeypatch.chdir(tmp_path)
        parser, root = make_parser_and_invalid_tree()
        parser.transform([f"SendTreeToPyXSD(xsdFile='{XSD}')"], root)
        assert "value" in codes(parser.report)

    def test_same_report_is_absorbed_once(self):
        """Merging the same report twice does not duplicate issues."""
        parser, _ = make_parser_and_invalid_tree()
        inner = ValidationReport()
        inner.add_error("bad value", code="value")
        parser._absorbTransformReport(inner)
        parser._absorbTransformReport(inner)
        assert len([i for i in parser.report if i.code == "value"]) == 1

    def test_absorb_none_and_self_are_noops(self):
        parser, _ = make_parser_and_invalid_tree()
        before = len(parser.report)
        parser._absorbTransformReport(None)
        parser._absorbTransformReport(parser.report)
        assert len(parser.report) == before


class TestParserContextInheritance:
    def test_schema_and_mode_inherited_from_outer_parser(self, tmp_path, monkeypatch):
        """With no explicit xsdFile the outer run's schema is reused."""
        monkeypatch.chdir(tmp_path)
        parser, root = make_parser_and_invalid_tree()
        parser.transform(["SendTreeToPyXSD()"], root)
        # The invalid value must have been seen: the reparse used the
        # inherited schema.
        assert "value" in codes(parser.report)

    def test_mode_is_inherited(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        parser, root = make_parser_and_invalid_tree()
        assert parser.mode is ParseModes.STRICT
        transformer = SendTreeToPyXSD(root)
        transformer.outerParser = parser
        transformer()  # no xsdFile, no mode
        assert transformer.parser.mode is ParseModes.STRICT
