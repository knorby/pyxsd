"""Mutation-aware child collections and whole-revision invalidation.

Structural descriptors and child sequences are live views over the one
authoritative child list, while every lexical edit dirties the shared
revision.  A dirty revision keeps serving current lexical and structural
reads, but typed interpretations, annotations, and typed descriptors become
stale; the old immutable evidence remains readable.
"""

from __future__ import annotations

import gc
import io
import weakref
from typing import Any
from xml.etree import ElementTree as ET

import pytest

from pyxsd.relaxng import AlternativeValue, RelaxNGSchema, StaleInterpretationError
from pyxsd.relaxng.binding import ChildSequence
from pyxsd.relaxng.tree import RelaxNGElement
from pyxsd.relaxng.values import ListInterpretation

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"

MATRIX_BODY = (
    f'<element xmlns="{NS}" name="doc">'
    '<optional><attribute name="a"><text/></attribute></optional>'
    "<mixed><zeroOrMore><choice>"
    '<element name="item"><optional><attribute name="b"><text/></attribute></optional>'
    f'<choice><list><oneOrMore><data type="integer" datatypeLibrary="{XSD}"/>'
    f'</oneOrMore></list><data type="string" datatypeLibrary="{XSD}"/></choice>'
    "</element>"
    '<element name="other"><optional><attribute name="b"><text/></attribute></optional>'
    "<text/></element>"
    "</choice></zeroOrMore></mixed>"
    "</element>"
)

MATRIX_XML = '<doc a="1">head<item b="2">1 2</item>tail<other>y</other></doc>'

ITEM_TEXT_BODY = (
    f'<element xmlns="{NS}" name="doc"><zeroOrMore>'
    f'<element name="item"><data type="integer" datatypeLibrary="{XSD}"/></element>'
    "</zeroOrMore></element>"
)


def _compile(body: str = MATRIX_BODY) -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(io.StringIO(body))
    schema.require_valid()
    return schema


def _typed(xml: str, *, capture: str = "patterns") -> Any:
    document = _compile().parse(io.StringIO(xml), typed=True, capture=capture)
    assert document.is_valid
    assert document.binding_evidence is not None
    return document


def _new(
    name: str,
    node: RelaxNGElement,
    *,
    text: str | None = None,
    tail: str | None = None,
) -> RelaxNGElement:
    return RelaxNGElement._new(name, node.revision, text=text, tail=tail)


def _matrix() -> tuple[
    Any,
    RelaxNGElement,
    RelaxNGElement,
    RelaxNGElement,
    AlternativeValue,
    ListInterpretation,
    Any,
]:
    """Parse the matrix document and retain its old typed/annotation evidence."""
    document = _typed(MATRIX_XML)
    root = document.root
    item, other = root.children
    snapshot = item.typed_value
    assert isinstance(snapshot, AlternativeValue)
    list_candidate = next(
        candidate for candidate in snapshot.candidates if candidate.contract.kind == "list"
    )
    list_interpretation = snapshot.as_interpretation(list_candidate.candidate_id)
    assert isinstance(list_interpretation, ListInterpretation)
    annotation = document.annotations.for_node(item)
    return document, root, item, other, snapshot, list_interpretation, annotation


def _element_children(node: RelaxNGElement) -> tuple[RelaxNGElement, ...]:
    return tuple(item for item in node.iter_content() if isinstance(item, RelaxNGElement))


def _shape(element: ET.Element) -> Any:
    return (
        element.tag,
        dict(element.attrib),
        element.text,
        element.tail,
        [_shape(child) for child in element],
    )


def _assert_old_evidence_readable(
    document: Any,
    root: RelaxNGElement,
    item: RelaxNGElement,
    snapshot: AlternativeValue,
    list_interpretation: ListInterpretation,
    annotation: Any,
) -> None:
    """Old immutable evidence stays readable while current reads go stale."""
    assert document.interpretations_current is False
    assert root.revision.dirty is True
    with pytest.raises(StaleInterpretationError):
        _ = root.scalar_value()
    with pytest.raises(StaleInterpretationError):
        _ = root.a
    with pytest.raises(StaleInterpretationError):
        _ = item.scalar_value()
    with pytest.raises(StaleInterpretationError):
        _ = item.typed_value
    with pytest.raises(StaleInterpretationError):
        _ = item.b
    with pytest.raises(StaleInterpretationError):
        _ = document.annotations
    list_candidate = next(
        candidate for candidate in snapshot.candidates if candidate.contract.kind == "list"
    )
    assert snapshot.as_candidate(list_candidate.candidate_id) == [1, 2]
    assert list_interpretation.as_python() == [1, 2]
    assert tuple(candidate.contract.kind for candidate in snapshot.candidates) == (
        "list",
        "atomic",
    )
    assert tuple(
        snapshot.as_candidate(candidate.candidate_id) for candidate in snapshot.candidates
    ) == ([1, 2], "1 2")
    text_annotation = annotation.scalar[0]
    assert text_annotation.kind == "text"
    assert text_annotation.value.as_candidate(list_candidate.candidate_id) == [1, 2]
    assert [candidate.candidate_id for candidate in text_annotation.value.candidates] == [
        candidate.candidate_id for candidate in snapshot.candidates
    ]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        pytest.param(
            lambda doc, root, item, other, revision: root.set_text("new"),
            '<doc a="1">new<item b="2">1 2</item>tail<other>y</other></doc>',
            id="set-text",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: root.clear_text(),
            '<doc a="1"><item b="2">1 2</item>tail<other>y</other></doc>',
            id="clear-text",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: root.set_tail("root tail"),
            MATRIX_XML,
            id="set-root-tail",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: root.clear_tail(),
            MATRIX_XML,
            id="clear-root-tail",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: root.set_attribute("a", "9"),
            '<doc a="9">head<item b="2">1 2</item>tail<other>y</other></doc>',
            id="set-attribute",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: root.delete_attribute("a"),
            '<doc>head<item b="2">1 2</item>tail<other>y</other></doc>',
            id="delete-attribute",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: root.set_name("renamed"),
            '<renamed a="1">head<item b="2">1 2</item>tail<other>y</other></renamed>',
            id="set-name",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: root.set_namespace_bindings({"p": "urn:p"}),
            '<doc xmlns:p="urn:p" a="1">head<item b="2">1 2</item>tail<other>y</other></doc>',
            id="set-namespaces",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: root.set_base_uri(
                "https://example.test/base/"
            ),
            MATRIX_XML,
            id="set-base-uri",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: item.set_text("9"),
            '<doc a="1">head<item b="2">9</item>tail<other>y</other></doc>',
            id="descendant-set-text",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: item.clear_text(),
            '<doc a="1">head<item b="2"/>tail<other>y</other></doc>',
            id="descendant-clear-text",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: item.set_tail(" changed "),
            '<doc a="1">head<item b="2">1 2</item> changed <other>y</other></doc>',
            id="descendant-set-tail",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: item.clear_tail(),
            '<doc a="1">head<item b="2">1 2</item><other>y</other></doc>',
            id="descendant-clear-tail",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: item.set_attribute("b", "5"),
            '<doc a="1">head<item b="5">1 2</item>tail<other>y</other></doc>',
            id="descendant-set-attribute",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: item.delete_attribute("b"),
            '<doc a="1">head<item>1 2</item>tail<other>y</other></doc>',
            id="descendant-delete-attribute",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: item.set_name("other"),
            '<doc a="1">head<other b="2">1 2</other>tail<other>y</other></doc>',
            id="descendant-set-name",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: item.set_namespace_bindings({"p": "urn:p"}),
            '<doc a="1">head<item xmlns:p="urn:p" b="2">1 2</item>tail<other>y</other></doc>',
            id="descendant-set-namespaces",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: item.set_base_uri(
                "https://example.test/item/"
            ),
            MATRIX_XML,
            id="descendant-set-base-uri",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: item.append_child(
                _new("c", item, text="leaf")
            ),
            '<doc a="1">head<item b="2">1 2<c>leaf</c></item>tail<other>y</other></doc>',
            id="descendant-append-child",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: root.append_child(
                _new("item", root, text="3")
            ),
            '<doc a="1">head<item b="2">1 2</item>tail<other>y</other><item>3</item></doc>',
            id="append-child",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: root.insert_child(
                0, _new("other", root, text="z")
            ),
            '<doc a="1">head<other>z</other><item b="2">1 2</item>tail<other>y</other></doc>',
            id="insert-child",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: root.remove_child(0),
            '<doc a="1">head<other>y</other></doc>',
            id="remove-child",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: root.replace_child(
                0, _new("other", root, text="z")
            ),
            '<doc a="1">head<other>z</other><other>y</other></doc>',
            id="replace-child",
        ),
        pytest.param(
            lambda doc, root, item, other, revision: root.clear_children(),
            '<doc a="1">head</doc>',
            id="clear-children",
        ),
    ],
)
def test_mutation_matrix_invalidates_the_whole_revision(mutation: Any, expected: str) -> None:
    document, root, item, other, snapshot, list_interpretation, annotation = _matrix()
    old_report = document.report
    assert document.to_string() == MATRIX_XML
    assert document.interpretations_current is True

    mutation(document, root, item, other, root.revision)

    assert document.to_string() == expected
    actual_shape = _shape(root.to_etree())
    if root.tail_value() is not None:
        # A root tail is outside the document element and is not serialized.
        actual_shape = (*actual_shape[:3], None, actual_shape[4])
    assert actual_shape == _shape(ET.fromstring(expected))
    assert _element_children(root) == root.children
    assert document.report is old_report
    assert document.is_valid is True
    _assert_old_evidence_readable(document, root, item, snapshot, list_interpretation, annotation)
    if root.base_uri is not None:
        assert root.base_uri == "https://example.test/base/"
    if "p" in (root.namespaces or {}):
        assert root.namespaces == {"p": "urn:p"}


def test_distant_sibling_edit_dirties_the_shared_revision() -> None:
    schema = _compile(ITEM_TEXT_BODY)
    document = schema.parse(io.StringIO("<doc><item>1</item><item>2</item></doc>"), typed=True)
    assert document.is_valid
    first, second = document.root.children
    first_value = first.typed_value
    assert first_value == 1

    second.set_text("edited")

    assert document.interpretations_current is False
    assert document.root.revision.dirty is True
    assert first.text_value() == "1"
    assert second.text_value() == "edited"
    assert document.to_string() == "<doc><item>1</item><item>edited</item></doc>"
    with pytest.raises(StaleInterpretationError):
        _ = first.typed_value
    assert first_value == 1


class TestChildSequenceIdentity:
    def test_repeated_reads_share_one_live_view(self) -> None:
        document = _typed(MATRIX_XML, capture="none")
        root = document.root

        view = root.item

        assert isinstance(view, ChildSequence)
        assert root.item is view
        assert root["item"] is view
        assert type(root).item.__get__(root) is view
        assert root.other is not view
        assert root["other"] is not view

        added = _new("item", root, text="3")
        view.append(added)

        assert root.item is view
        assert root["item"] is view
        assert view[-1] is added
        assert root.children[1] is added
        assert root.children[-1] is root.other[0]
        assert len(view) == 2

    def test_view_cache_does_not_keep_a_dropped_view_alive(self) -> None:
        document = _typed(MATRIX_XML, capture="none")
        view = document.root.item
        reference = weakref.ref(view)

        del view
        gc.collect()

        assert reference() is None
        assert document.root.item is not None


class TestChildSequenceInsertion:
    def test_insert_and_append_keep_matching_order_inside_the_run(self) -> None:
        document = _typed("<doc><item>0</item><other>o</other><item>1</item></doc>", capture="none")
        root = document.root
        view = root.item

        first = _new("item", root, text="first")
        view.insert(0, first)
        assert [child.text_value() for child in view] == ["first", "0", "1"]
        assert root.children[2] is root.other[0]
        assert document.to_string() == (
            "<doc><item>first</item><item>0</item><other>o</other><item>1</item></doc>"
        )

        middle = _new("item", root, text="middle")
        view.insert(-1, middle)
        assert [child.text_value() for child in view] == ["first", "0", "middle", "1"]
        assert document.to_string() == (
            "<doc><item>first</item><item>0</item><other>o</other>"
            "<item>middle</item><item>1</item></doc>"
        )

        last = _new("item", root, text="last")
        view.insert(len(view), last)
        assert view[-1] is last

        appended = _new("item", root, text="appended")
        view.append(appended)
        assert view[-1] is appended
        assert root.children[-1] is appended

    def test_append_stays_before_unrelated_trailing_children(self) -> None:
        document = _typed("<doc><item>0</item><item>1</item><other>o</other></doc>", capture="none")
        root = document.root
        view = root.item

        added = _new("item", root, text="2")
        view.append(added)

        assert [child._name_ for child in root.children] == ["item", "item", "item", "other"]
        assert root.children[2] is added
        assert document.to_string() == (
            "<doc><item>0</item><item>1</item><item>2</item><other>o</other></doc>"
        )

    def test_insert_into_an_empty_sequence_appends_to_the_parent(self) -> None:
        document = _typed("<doc><other>o</other></doc>", capture="none")
        root = document.root
        view = root.item
        added = _new("item", root, text="3")

        view.insert(0, added)

        assert list(view) == [added]
        assert [child._name_ for child in root.children] == ["other", "item"]
        assert document.to_string() == "<doc><other>o</other><item>3</item></doc>"

    def test_insert_indexes_follow_sequence_semantics(self) -> None:
        document = _typed("<doc><item>0</item><item>1</item></doc>", capture="none")
        root = document.root
        view = root.item
        before = document.to_string()

        assert view[1].text_value() == "1"
        assert view[-1].text_value() == "1"
        for index in (2, -3):
            with pytest.raises(IndexError):
                _ = view[index]
        for index in (3, -3):
            with pytest.raises(IndexError):
                view.insert(index, _new("item", root, text="x"))
        with pytest.raises(TypeError):
            view.insert("0", _new("item", root, text="x"))  # type: ignore[arg-type]

        assert document.to_string() == before
        assert root.revision.dirty is False
        assert document.interpretations_current is True


class TestChildSequenceReplacement:
    def test_indexed_replacement_keeps_both_nodes_tails(self) -> None:
        document = _typed(
            "<doc><item>0</item>zero<other>o</other>one<item>1</item>two</doc>",
            capture="none",
        )
        root = document.root
        view = root.item
        old_first, old_second = view

        first = _new("item", root, text="9", tail="nine")
        view[0] = first
        assert old_first.parent is None
        assert old_first.tail_value() == "zero"
        assert first.tail_value() == "nine"
        assert document.to_string() == (
            "<doc><item>9</item>nine<other>o</other>one<item>1</item>two</doc>"
        )

        second = _new("item", root, text="8", tail="eight")
        view[-1] = second
        assert old_second.parent is None
        assert old_second.tail_value() == "two"
        assert second.tail_value() == "eight"
        assert document.to_string() == (
            "<doc><item>9</item>nine<other>o</other>one<item>8</item>eight</doc>"
        )
        assert root.revision.dirty is True


class TestChildSequenceSlices:
    def test_slice_assignment_replaces_the_view_segment_in_order(self) -> None:
        document = _typed(
            "<doc><item>0</item><other>o</other><item>1</item><other>p</other><item>2</item></doc>",
            capture="none",
        )
        root = document.root
        view = root.item

        view[1:3] = [
            _new("item", root, text="a"),
            _new("item", root, text="b"),
            _new("item", root, text="c"),
        ]

        assert [child.text_value() for child in view] == ["0", "a", "b", "c"]
        assert [child.text_value() for child in root.other] == ["o", "p"]
        assert [child._name_ for child in root.children] == [
            "item",
            "other",
            "item",
            "other",
            "item",
            "item",
        ]

    def test_slice_assignment_shrinks_and_inserts_at_view_indexes(self) -> None:
        document = _typed(
            "<doc><item>0</item><other>o</other><item>1</item><item>2</item></doc>",
            capture="none",
        )
        root = document.root
        view = root.item

        view[0:2] = [_new("item", root, text="a")]
        assert [child.text_value() for child in view] == ["a", "2"]
        assert [child._name_ for child in root.children] == ["item", "other", "item"]

        view[1:1] = [_new("item", root, text="inserted")]
        assert [child.text_value() for child in view] == ["a", "inserted", "2"]
        assert root.children[1] is root.other[0]
        assert document.to_string() == (
            "<doc><item>a</item><other>o</other><item>inserted</item><item>2</item></doc>"
        )

    def test_empty_slice_assignment_is_a_noop(self) -> None:
        document = _typed(MATRIX_XML, capture="none")
        root = document.root
        view = root.item
        before = document.to_string()
        before_children = root.children

        view[1:1] = []
        view[5:9] = []

        assert document.to_string() == before
        assert root.children == before_children
        assert root.revision.dirty is False
        assert document.interpretations_current is True

    def test_extended_slice_assignment_and_deletion(self) -> None:
        document = _typed(
            "<doc><item>0</item><other>o</other><item>1</item><other>p</other><item>2</item></doc>",
            capture="none",
        )
        root = document.root
        view = root.item

        view[::-1] = [
            _new("item", root, text="x"),
            _new("item", root, text="y"),
            _new("item", root, text="z"),
        ]
        assert [child.text_value() for child in view] == ["z", "y", "x"]
        assert [child.text_value() for child in root.other] == ["o", "p"]

        del view[::2]
        assert [child.text_value() for child in view] == ["y"]
        assert [child.text_value() for child in root.other] == ["o", "p"]
        assert document.to_string() == "<doc><other>o</other><item>y</item><other>p</other></doc>"


class TestChildSequenceDeletion:
    def test_deletion_and_pop_keep_removed_tails(self) -> None:
        document = _typed(
            "<doc><item>0</item>zero<other>o</other>one<item>1</item>two</doc>",
            capture="none",
        )
        root = document.root
        view = root.item
        first, second = view

        del view[0]

        assert first.parent is None
        assert first.tail_value() == "zero"
        assert [child.text_value() for child in view] == ["1"]
        assert document.to_string() == "<doc><other>o</other>one<item>1</item>two</doc>"

        removed = view.pop()
        assert removed is second
        assert removed.parent is None
        assert removed.tail_value() == "two"
        assert len(view) == 0
        assert document.to_string() == "<doc><other>o</other>one</doc>"
        with pytest.raises(IndexError):
            view.pop()

    def test_tree_clear_children_detaches_every_child(self) -> None:
        document = _typed(MATRIX_XML, capture="none")
        root = document.root
        item, other = root.children

        root.clear_children()

        assert root.children == ()
        assert item.parent is None
        assert other.parent is None
        assert item.tail_value() == "tail"
        assert other.tail_value() is None
        assert document.to_string() == '<doc a="1">head</doc>'


class TestChildSequenceAtomicFailures:
    def test_failed_sequence_edits_leave_the_tree_byte_identical(self) -> None:
        document = _typed('<doc a="1"><item>0</item><item>1</item></doc>', capture="none")
        root = document.root
        view = root.item
        foreign = _typed("<doc><item>x</item></doc>", capture="none").root.children[0]
        before = document.to_string()
        before_children = root.children
        before_parents = tuple(child.parent for child in before_children)
        before_tails = tuple(child.tail_value() for child in before_children)

        def unchanged() -> None:
            assert document.to_string() == before
            assert root.children == before_children
            assert tuple(child.parent for child in before_children) == before_parents
            assert tuple(child.tail_value() for child in before_children) == before_tails
            assert root.revision.dirty is False
            assert document.interpretations_current is True

        wrong_name = _new("other", root, text="wrong")
        attached = before_children[0]
        valid = _new("item", root, text="valid")

        for operation in (
            lambda: view.insert(0, foreign),
            lambda: view.insert(0, wrong_name),
            lambda: view.insert(0, attached),
            lambda: view.insert(0, root),
        ):
            with pytest.raises((TypeError, ValueError)):
                operation()
            unchanged()

        with pytest.raises(IndexError):
            view.insert(3, valid)
        unchanged()
        with pytest.raises(IndexError):
            view.insert(-3, valid)
        unchanged()

        with pytest.raises(ValueError):
            view[0] = wrong_name
        unchanged()
        with pytest.raises(ValueError):
            view[0] = foreign
        unchanged()
        with pytest.raises(ValueError):
            view[0] = attached
        unchanged()
        with pytest.raises(ValueError):
            view[0] = root
        unchanged()
        with pytest.raises(IndexError):
            view[2] = valid
        unchanged()
        with pytest.raises(IndexError):
            del view[2]
        unchanged()

        with pytest.raises(ValueError):
            view[0:2] = [valid, attached]
        assert valid.parent is None
        unchanged()
        with pytest.raises(ValueError):
            view[0:2] = [valid, wrong_name]
        assert valid.parent is None
        unchanged()
        with pytest.raises(ValueError):
            view[0:2] = [valid, foreign]
        assert valid.parent is None
        unchanged()
        with pytest.raises(ValueError):
            view[0:2] = [valid, valid]
        assert valid.parent is None
        unchanged()
        with pytest.raises(ValueError):
            view[0:2] = [valid, root]
        assert valid.parent is None
        unchanged()
        other_valid = _new("item", root, text="other-valid")
        with pytest.raises(ValueError):
            view[0:2:2] = [valid, other_valid]
        assert valid.parent is None
        assert other_valid.parent is None
        unchanged()
        with pytest.raises(ValueError):
            view[0:2:0] = [valid]
        assert valid.parent is None
        unchanged()
        with pytest.raises(ValueError):
            del view[0:2:0]
        unchanged()

        with pytest.raises(TypeError):
            view.insert("0", valid)  # type: ignore[arg-type]
        unchanged()
        with pytest.raises(TypeError):
            view[0:2] = [valid, "not an element"]  # type: ignore[list-item]
        assert valid.parent is None
        unchanged()


class TestChildSequenceNameChanges:
    def test_set_name_moves_a_child_between_live_sequences(self) -> None:
        document = _typed(MATRIX_XML, capture="none")
        root = document.root
        item, other = root.children
        item_view = root.item
        other_view = root.other

        assert list(item_view) == [item]
        assert list(other_view) == [other]

        item.set_name("other")

        assert root.item is item_view
        assert root.other is other_view
        assert list(item_view) == []
        assert list(other_view) == [item, other]
        assert document.to_string() == (
            '<doc a="1">head<other b="2">1 2</other>tail<other>y</other></doc>'
        )

        item.set_name("item")

        assert list(item_view) == [item]
        assert list(other_view) == [other]
        assert root.revision.dirty is True


class TestSequenceMutationInvalidation:
    def test_descriptor_sequence_insert_and_delete_stale_typed_reads(self) -> None:
        document, root, item, other, snapshot, list_interpretation, annotation = _matrix()
        old_report = document.report
        old_text = item.text_value()
        view = root.item

        added = _new("item", root, text="3 4")
        view.append(added)
        assert document.interpretations_current is False
        with pytest.raises(StaleInterpretationError):
            _ = item.typed_value
        with pytest.raises(StaleInterpretationError):
            _ = document.annotations

        removed = view.pop(0)
        assert removed is item
        assert removed.parent is None
        assert removed.text_value() == old_text
        assert removed.tail_value() == "tail"
        assert root.children == (added, other)

        assert document.report is old_report
        assert document.is_valid is True
        _assert_old_evidence_readable(
            document, root, item, snapshot, list_interpretation, annotation
        )

        fresh = document.revalidate()

        assert fresh is not document
        assert fresh.interpretations_current is True
        assert fresh.to_string() == '<doc a="1">head<item>3 4</item><other>y</other></doc>'
        assert fresh.root.item[0] is fresh.root.children[0]
        assert fresh.root.item[0].typed_value.as_candidate(
            next(
                candidate.candidate_id
                for candidate in fresh.root.item[0].typed_value.candidates
                if candidate.contract.kind == "list"
            )
        ) == [3, 4]
        assert document.interpretations_current is False
        with pytest.raises(StaleInterpretationError):
            _ = document.annotations


def test_mixed_content_text_and_tail_edits_preserve_exact_lexical_values() -> None:
    document = _typed(
        '<doc a="1">head<item b="2">1 2</item>tail<other>y</other>end</doc>',
        capture="none",
    )
    root = document.root
    item, other = root.children

    assert list(root.iter_content()) == ["head", item, "tail", other, "end"]

    item.set_text("9")
    item.set_tail(" changed ")

    assert item.text_value() == "9"
    assert item.tail_value() == " changed "
    assert other.tail_value() == "end"
    assert list(root.iter_content()) == ["head", item, " changed ", other, "end"]
    assert document.to_string() == (
        '<doc a="1">head<item b="2">9</item> changed <other>y</other>end</doc>'
    )

    replacement = _new("item", root, text="r", tail=" replacement ")
    root.item[0] = replacement

    assert item.parent is None
    assert item.text_value() == "9"
    assert item.tail_value() == " changed "
    assert replacement.parent is root
    assert replacement.tail_value() == " replacement "
    assert list(root.iter_content()) == ["head", replacement, " replacement ", other, "end"]
    assert document.to_string() == (
        '<doc a="1">head<item>r</item> replacement <other>y</other>end</doc>'
    )

    removed = root.item.pop()

    assert removed is replacement
    assert removed.parent is None
    assert removed.tail_value() == " replacement "
    assert list(root.iter_content()) == ["head", other, "end"]
    assert document.to_string() == '<doc a="1">head<other>y</other>end</doc>'


def test_structural_reads_stay_live_and_do_not_gate_on_typed_freshness() -> None:
    document = _typed(MATRIX_XML, capture="none")
    root = document.root
    item, other = root.children

    root.set_attribute("a", "2")
    item.set_text("7")

    assert root.children == (item, other)
    assert root.item[0] is item
    assert root.other[0] is other
    assert tuple(root.item) == (item,)
    assert root.attribute_lexical("a") == "2"
    assert item.text_value() == "7"
    assert item.attribute_lexical("b") == "2"
    assert _element_children(root) == (item, other)
    assert list(root.iter_content()) == ["head", item, "tail", other]

    stale = document
    fresh = stale.revalidate()

    assert fresh.interpretations_current is True
    assert fresh.to_string() == ('<doc a="2">head<item b="2">7</item>tail<other>y</other></doc>')
    assert fresh.root.scalar_value() is not None
    assert item.text_value() == "7"
