"""Schema-aware assertion evaluation and the ruled context-item reading.

A ``schema-element``/``schema-attribute`` kind test decides a node's declared
type, which exists only in the XDM layer: the harness's elementpath trees are
built from serialized XML, where every node is ``xs:untyped``.  Those
expressions alone therefore take the engine route with the case's declared
schema pool.  Every other assertion keeps the elementpath substrate.

``ResultSnapshot.result_items`` is the principal result the runner's
``$result`` names — the raw item sequence, and the delivered tree only when
that sequence is empty (``runner/assert.xsl:180-195``).  The rule is pinned
here because the engine route is where it is implemented exactly.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
from tests.xslt import suite
from tests.xslt.assertions import (
    AssertionGap,
    AssertionXPath,
    ResultSnapshot,
    _evaluate_schema_aware,
    _parser,
    _result_context_item,
    _uses_schema_kind_test,
    assert_case,
    evaluate_assertion,
)
from tests.xslt.model import Environment, Invocation
from tests.xslt.model import TestCase as CatalogCase
from tests.xslt.outcomes import Outcome
from tests.xslt.support import stylesheet, transform

from pyxsd.xdm.document import parse_typed_document
from pyxsd.xdm.nodes import NodeKind, XdmDocument
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xslt.results import ResultEntry
from pyxsd.xslt.runtime.builder import ResultBuilder

INVOICE = "urn:example:invoice"

INVOICE_XSD = f"""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    targetNamespace="{INVOICE}" xmlns:inv="{INVOICE}"
    elementFormDefault="qualified">
  <xs:element name="Invoice" type="inv:InvoiceType"/>
  <xs:complexType name="InvoiceType">
    <xs:sequence>
      <xs:element name="amount" type="xs:decimal"/>
    </xs:sequence>
  </xs:complexType>
</xs:schema>"""

INVOICE_XML = f'<Invoice xmlns="{INVOICE}"><amount>1</amount></Invoice>'.encode()

SCHEMA_ELEMENT_ASSERTION = (
    "<assert>(/) instance of document-node(schema-element(inv:Invoice))</assert>"
)


def _case(assertion: str = SCHEMA_ELEMENT_ASSERTION) -> CatalogCase:
    scope = (("inv", INVOICE),)
    return CatalogCase(
        name="schema/assertion",
        local_name="assertion",
        test_set="schema",
        description="",
        source=PurePosixPath("schema/assertion.xsl"),
        environment=Environment(),
        invocation=Invocation(),
        result_xml=f'<result xmlns="http://www.w3.org/2012/10/xslt-test-catalog">{assertion}</result>',
        # One scope per element: the <result> wrapper and the <assert> child.
        namespace_scopes=(scope, scope),
    )


@pytest.fixture
def invoice_pool(tmp_path: Path) -> SchemaPool:
    schema = tmp_path / "invoice.xsd"
    schema.write_text(INVOICE_XSD, encoding="utf-8")
    return SchemaPool.from_imports([schema], xsd_version="1.1")


def _typed_document(pool: SchemaPool) -> XdmDocument:
    return parse_typed_document(INVOICE_XML, schema_pool=pool, validation="strict")


def test_a_schema_element_assertion_reads_the_case_pool(invoice_pool: SchemaPool) -> None:
    document = _typed_document(invoice_pool)

    result = assert_case(
        _case(),
        ResultSnapshot(result_items=(document,)),
        schema=lambda: invoice_pool,
    )

    assert result.outcome is Outcome.PASS


def test_a_schema_element_assertion_rejects_an_untyped_result(invoice_pool: SchemaPool) -> None:
    # The same markup without validation keeps xs:untyped, so the kind test
    # must reject it even though the element name matches.  A base without
    # schema-aware kind tests matches by name and wrongly passes.
    untyped = XdmDocument.parse(INVOICE_XML)

    result = assert_case(
        _case(),
        ResultSnapshot(result_items=(untyped,)),
        schema=lambda: invoice_pool,
    )

    assert result.outcome is Outcome.FAIL


def test_a_schema_element_assertion_without_a_pool_stays_a_gap(
    invoice_pool: SchemaPool,
) -> None:
    document = _typed_document(invoice_pool)
    snapshot = ResultSnapshot(result_items=(document,))

    absent = assert_case(_case(), snapshot)
    empty_provider = assert_case(_case(), snapshot, schema=lambda: None)

    assert absent.outcome is Outcome.ADAPTER_GAP
    assert empty_provider.outcome is Outcome.ADAPTER_GAP
    assert "XPST0008" in absent.message


def test_a_missing_declaration_in_a_present_pool_is_a_gap(
    invoice_pool: SchemaPool,
) -> None:
    # The pool exists but does not declare the named element.  That is the
    # static XPST0008 the engine raises, not a silent `false`: the family stays
    # an explicit adapter gap.
    document = _typed_document(invoice_pool)

    result = assert_case(
        _case("<assert>(/) instance of document-node(schema-element(inv:Missing))</assert>"),
        ResultSnapshot(result_items=(document,)),
        schema=lambda: invoice_pool,
    )

    assert result.outcome is Outcome.ADAPTER_GAP
    assert "[err:XPST0008]" in result.message
    assert (
        "no schema-element declaration 'inv:Missing' is in the in-scope schema definitions"
        in result.message
    )


def test_the_schema_aware_context_item_rule(invoice_pool: SchemaPool) -> None:
    document = _typed_document(invoice_pool)

    single = assert_case(
        _case(),
        ResultSnapshot(result_items=(document,)),
        schema=lambda: invoice_pool,
    )
    multiple = assert_case(
        _case(),
        ResultSnapshot(result_items=(document, document)),
        schema=lambda: invoice_pool,
    )
    empty = assert_case(
        _case(),
        ResultSnapshot(result_items=()),
        schema=lambda: invoice_pool,
    )

    # One item: decided over the delivered principal result.
    assert single.outcome is Outcome.PASS
    # Several: the runner passes no context item, so this stays a gap.
    assert multiple.outcome is Outcome.ADAPTER_GAP
    assert "multi-item" in multiple.message
    # None: an empty document node, which the kind test rejects.
    assert empty.outcome is Outcome.FAIL


def test_the_engine_arm_returns_one_value_or_a_sequence(invoice_pool: SchemaPool) -> None:
    # An empty item sequence focuses an empty document node, not an absent
    # focus: count(.) is one document, not the empty sequence.
    assert _evaluate_schema_aware("count(.)", (), invoice_pool, ()) == 1
    assert _evaluate_schema_aware("'a'", (), invoice_pool, ()) == "a"
    assert _evaluate_schema_aware("(1, 2)", (), invoice_pool, ()) == (1, 2)
    # The reference binds $result to the empty sequence for an empty result
    # while the focus is $empty-doc; only the focus is substituted.
    assert bool(_evaluate_schema_aware("empty($result)", (), invoice_pool, ()))
    with pytest.raises(AssertionGap, match="multi-item"):
        _evaluate_schema_aware("true()", (), invoice_pool, (1, 2))


def test_the_result_variable_is_the_context_item(invoice_pool: SchemaPool) -> None:
    # Document delivery hands the snapshot a parentless element: the content
    # of the final result tree, not a tree of its own.  The context item is
    # then that tree's document, and $result must name the same node — the
    # reference binds both to $result, so the two kind tests agree.
    typed = _typed_document(invoice_pool)
    delivered = ResultBuilder.document().clone(typed.root_element)
    assert delivered.kind is NodeKind.ELEMENT
    assert delivered.parent is None
    assert delivered.annotation.type_name == f"{{{INVOICE}}}InvoiceType"

    snapshot = ResultSnapshot(result_items=(delivered,))
    via_variable = assert_case(
        _case("<assert>$result instance of document-node(schema-element(inv:Invoice))</assert>"),
        snapshot,
        schema=lambda: invoice_pool,
    )
    via_context = assert_case(
        _case("<assert>(/) instance of document-node(schema-element(inv:Invoice))</assert>"),
        snapshot,
        schema=lambda: invoice_pool,
    )

    assert via_variable.outcome is Outcome.PASS
    assert via_context.outcome is Outcome.PASS


def test_only_schema_kind_tests_take_the_engine_route() -> None:
    parser = _parser(())

    assert _uses_schema_kind_test(
        parser.parse("(/) instance of document-node(schema-element(doc))")
    )
    assert _uses_schema_kind_test(parser.parse("/* instance of schema-attribute(a)"))
    assert not _uses_schema_kind_test(parser.parse("element(*, xs:untyped)"))
    assert not _uses_schema_kind_test(parser.parse("not(@a)"))
    assert not _uses_schema_kind_test(parser.parse("/out"))

    def provider() -> None:
        raise AssertionError("the provider is resolved for an ordinary assertion")

    assert evaluate_assertion(AssertionXPath("true()"), schema=provider)
    assert evaluate_assertion(
        AssertionXPath("/out = 'x'"),
        result=ResultSnapshot(raw=("x",), xml="<out>x</out>"),
        schema=provider,
    )


def test_the_engine_context_item_is_the_delivered_final_result_tree() -> None:
    # Document delivery builds a final result tree, so the delivered principal
    # is its document node.  The engine hands the harness the pre-tree
    # sequence, whose single typed element is the tree's content; focusing it
    # directly would make `/` a non-document root.
    result = transform(stylesheet('<xsl:template match="/"><out/></xsl:template>'), b"<in/>")
    (item,) = result.principal_entry.raw
    assert item.kind is NodeKind.ELEMENT
    assert item.parent is None

    document = _result_context_item((item,))
    assert document.kind is NodeKind.DOCUMENT
    assert document.children[0].name == "out"

    parsed = XdmDocument.parse(b"<out/>")
    assert _result_context_item((parsed,)) is parsed
    assert _result_context_item((parsed.root_element,)) is parsed.root_element
    assert _result_context_item((1,)) == 1
    assert _result_context_item(()).kind is NodeKind.DOCUMENT


def test_the_snapshot_exposes_the_delivered_items_raw_first() -> None:
    tree = XdmDocument.parse(b"<delivered><z/></delivered>")
    element_document = XdmDocument.parse(b"<out>x</out>")
    element = element_document.root_element

    element_view = suite._snapshot_entry(ResultEntry(raw=(element,)))
    assert element_view.result_items == (element,)
    assert element_view.xml == "<out>x</out>"
    assert element_view.raw == ("x",)

    document_view = suite._snapshot_entry(ResultEntry(raw=(), tree=tree))
    assert document_view.result_items == (tree,)
    assert document_view.raw == ()
    assert document_view.xml == suite._document_xml(tree)

    # Both present (the schema-validation shape): the raw items win, and the
    # serialization still describes the delivered tree.
    both_view = suite._snapshot_entry(ResultEntry(raw=(element,), tree=tree))
    assert both_view.result_items == (element,)
    assert both_view.xml == suite._document_xml(tree)

    empty_view = suite._snapshot_entry(ResultEntry(raw=()))
    assert empty_view.result_items == ()
    assert empty_view.raw == ()
    assert empty_view.xml is None

    # A node kind with no document serialization, and several items, keep
    # today's comparison view.
    text = element_document.root_element.children[0]
    text_view = suite._snapshot_entry(ResultEntry(raw=(text,)))
    assert text_view.result_items == (text,)
    assert text_view.xml is None
    multi_view = suite._snapshot_entry(ResultEntry(raw=(element, element)))
    assert multi_view.xml is None


def test_node_xml_serializes_an_element_and_defers_for_a_document() -> None:
    document = XdmDocument.parse(b"<out>x</out>")

    assert suite._node_xml(document.root_element) == "<out>x</out>"
    assert suite._node_xml(document) == suite._document_xml(document)


def test_the_assertion_xml_view_keeps_lexical_prefixes_and_declarations() -> None:
    """The comparison view is the engine's own markup, not ElementTree's.

    ``_xml_element`` used to rebuild an ElementTree whose serializer invented
    ``ns0``/``ns1`` prefixes and dropped every namespace declaration, so an
    expected ``p:``/``q:`` prefix could never match.  The assertion view now
    carries the tree's lexical prefixes and in-scope declarations verbatim.
    """
    document = XdmDocument.parse(
        b'<p:out xmlns:p="urn:p"><p:item xmlns="urn:d"/><q:x xmlns:q="urn:q" q:a="1"/></p:out>'
    )
    expected = (
        '<p:out xmlns:p="urn:p"><p:item xmlns="urn:d"/><q:x xmlns:q="urn:q" q:a="1"/></p:out>'
    )

    assert suite._document_xml(document) == expected
    assert suite._node_xml(document.root_element) == expected
    assert suite._snapshot_entry(ResultEntry(raw=(), tree=document)).xml == expected
