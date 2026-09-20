"""Revalidation observability tests.

Revalidation inside the old send-tree transform used to construct a parser
object and discard it, so its validation report vanished: callers could
not see problems introduced by a transformed tree, and the temporary
file backing the reparse was never closed. ``Document.revalidate()``
replaces that flow: the re-parsed document carries a fresh report for
the tree's current shape, and no temporary file is involved.
"""

from conftest import fixture_dir
from pyxsd.schema import Schema

SCHEMA = fixture_dir("primitives") / "schema.xsd"
INSTANCE = fixture_dir("primitives") / "instance.xml"


def make_doc():
    """A document for the primitives fixture, holding a clean tree."""
    return Schema.compile(SCHEMA).parse(INSTANCE)


def codes(report):
    return [issue.code for issue in report]


def corrupt_count(tree):
    """Sets an invalid value on the tree's ``count`` leaf.

    The bound tree stores leaf values as single-element lists.
    """
    tree.count._value_ = ["not-an-int"]


class TestRevalidationReport:
    def test_inner_errors_exposed_on_revalidated_document(self):
        """The revalidation report survives on the returned document."""
        doc = make_doc()
        corrupt_count(doc.root)
        again = doc.revalidate()
        assert again is not doc
        assert again.report is not None
        assert "value" in codes(again.report)
        assert all(issue.phase == "instance" for issue in again.report)

    def test_clean_tree_has_empty_report(self):
        again = make_doc().revalidate()
        assert len(again.report) == 0


class TestFindingsVisibility:
    def test_inner_issues_reach_the_callers_document(self):
        """Findings from a transformed tree reach the document the
        caller inspects: the old outer-parser report merge is now
        ``again.report``."""
        doc = make_doc()
        doc.transform(corrupt_count)  # in-place mutation passes through
        again = doc.revalidate()
        assert "value" in codes(again.report)
