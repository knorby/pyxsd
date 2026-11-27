"""One XDM adapter tree per document per transformation.

An adapter projects one XDM node onto elementpath, and node identity is object
identity, so two adapters for one node would make ``is``, document order, and
``union`` deduplication disagree.  These tests pin the reuse boundary: a
finalized document is adapted once and reused by every evaluation of one
transformation; an unfinalized builder tree is adapted per evaluation;
admission is capped by document count and by a node budget and never evicts;
and a reused adapter retains no bridge, environment, or document-order state
beyond the document it projects.

No test asserts elapsed time.  Reuse is proved with the registry's own counters
and adapter identity, never with a wall clock.
"""

from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from tests.xslt.support import stylesheet as _stylesheet

import pyxsd.xpath.engine as engine_module
from pyxsd.xdm import XdmAtomic
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xpath.engine import (
    XPathEngine,
    XPathParserCache,
    _node_order_key,
    _ValueBridge,
    _XdmAdapterRegistry,
    _XdmDocumentAdapter,
    _XdmElementAdapter,
)
from pyxsd.xpath.environment import ExpressionEnvironment, Focus, XPathStaticContext
from pyxsd.xslt import XsltProcessor

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
FN_NS = "http://www.w3.org/2005/xpath-functions"

SOURCE = b"<doc><b id='1'>x</b><b id='2'>y</b><b id='3'>z</b></doc>"


def _document(xml: bytes = SOURCE) -> XdmDocument:
    return XdmDocument.parse(xml)


def _adapter_count(document: XdmDocument) -> int:
    """Every adapter one document's tree needs, the document included."""
    count = 0
    stack: list[XdmNode] = [document]
    while stack:
        node = stack.pop()
        count += 1
        stack.extend(node.children)
        if node.kind is NodeKind.ELEMENT:
            stack.extend(node.attributes)
            stack.extend(node.namespace_nodes)
    return count


def _registry_recorder(monkeypatch: pytest.MonkeyPatch) -> list[_XdmAdapterRegistry]:
    """Record every ``_XdmAdapterRegistry`` a transformation creates."""
    recorded: list[_XdmAdapterRegistry] = []
    original = _XdmAdapterRegistry.__init__

    def record(self: _XdmAdapterRegistry, **options: Any) -> None:
        original(self, **options)
        recorded.append(self)

    monkeypatch.setattr(_XdmAdapterRegistry, "__init__", record)
    return recorded


def _bridge_recorder(monkeypatch: pytest.MonkeyPatch) -> list[_ValueBridge]:
    """Record every evaluation's bridge, in creation order."""
    recorded: list[_ValueBridge] = []
    original = _ValueBridge.__init__

    def record(self: _ValueBridge, *arguments: Any, **keywords: Any) -> None:
        original(self, *arguments, **keywords)
        recorded.append(self)

    monkeypatch.setattr(_ValueBridge, "__init__", record)
    return recorded


def _transform(body: str, source: bytes | None = SOURCE, **options: Any) -> str:
    compiled = XsltProcessor().compile(io.BytesIO(_stylesheet(body)))
    if source is None:
        result = compiled.transform(**options)
    else:
        result = compiled.transform(source=source, **options)
    return result.principal_tree.string_value


# ---------------------------------------------------------------------------
# §3.3/§7.2 — one adapter tree per registered document per transformation


def test_one_adapter_tree_serves_every_evaluation_of_a_transformation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registries = _registry_recorder(monkeypatch)
    body = """
    <xsl:variable name="d" select="/"/>
    <xsl:template match="/">
      <out>
        <xsl:value-of select="$d/doc/b[1] is /doc/b[1]"/>
        <xsl:text>|</xsl:text>
        <xsl:value-of select="count($d/doc/b | /doc/b)"/>
        <xsl:text>|</xsl:text>
        <xsl:value-of select="$d/doc/b[2] is $d/doc/b[2]"/>
      </out>
    </xsl:template>"""
    assert _transform(body) == "true|3|true"

    (registry,) = registries
    assert registry.documents_built == 1
    assert registry.documents_reused >= 1
    assert registry.document_count == 1
    assert registry.nodes_built == _adapter_count(_document()) - 1

    # The second arm is the same property at the engine boundary: two
    # evaluations over one registry hand back the very same adapter objects.
    bridges = _bridge_recorder(monkeypatch)
    engine = XPathEngine()
    compiled = engine.compile("count($d//b) + count(//b)")
    document = _document()
    registry = _XdmAdapterRegistry()
    environment = ExpressionEnvironment(
        variables={"d": document},
        documents={"urn:doc": document},
        adapter_registry=registry,
    )
    assert engine.evaluate(compiled, environment, focus=document) == (XdmAtomic.integer(6),)
    reused_after_first = registry.documents_reused
    assert reused_after_first >= 1
    assert engine.evaluate(compiled, environment, focus=document) == (XdmAtomic.integer(6),)
    first, second = bridges[-2:]
    assert first.adapt_item(document) is second.adapt_item(document)
    element = document.root_element.children[0]
    assert first.adapt_item(element) is second.adapt_item(element)
    assert registry.documents_built == 1
    assert registry.documents_reused > reused_after_first
    assert registry.nodes_built == _adapter_count(document) - 1


def test_cross_tree_node_order_is_total_across_parsed_documents() -> None:
    """Corresponding nodes of two parsed documents must not compare equal."""
    first = _document(b"<r><x>1</x></r>")
    second = _document(b"<r><x>2</x></r>")
    registry = _XdmAdapterRegistry()
    first_root = registry.document(first)
    second_root = registry.document(second)
    assert first_root is not None and second_root is not None
    assert _node_order_key(first_root) < _node_order_key(second_root)
    assert _node_order_key(first_root.children[0]) < _node_order_key(second_root.children[0])


def test_evaluation_does_not_adapt_an_unread_document() -> None:
    registry = _XdmAdapterRegistry()
    read = _document(b"<r/>")
    unread = _document(b"<unused/>")
    environment = ExpressionEnvironment(
        documents={"urn:read": read, "urn:unread": unread},
        adapter_registry=registry,
    )
    engine = XPathEngine()
    assert engine.evaluate(engine.compile("doc('urn:read')"), environment) == (read,)
    assert registry.lookup(read) is not None
    assert registry.lookup(unread) is None


def test_document_bindings_memoize_registry_and_ephemeral_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _XdmAdapterRegistry(max_documents=1)
    admitted = _document(b"<admitted/>")
    refused = _document(b"<refused/>")
    registered = registry.document(admitted)
    assert registered is not None
    static = XPathStaticContext()
    environment = ExpressionEnvironment(documents={"a": admitted, "r": refused})
    bridge = _ValueBridge(None, static, environment, registry=registry)
    original = bridge.adapt_document
    reads = []

    def observe(document):
        reads.append(document)
        return original(document)

    monkeypatch.setattr(bridge, "adapt_document", observe)
    context = engine_module._build_context(static, environment, Focus.absent(), bridge)
    documents = context.documents
    assert documents is not None
    assert len(documents) == 2
    assert tuple(documents) == ("a", "r")
    assert "a" in documents
    assert "missing" not in documents
    assert reads == []
    assert documents.get("missing") is None
    assert documents["a"] is documents["a"] is registered
    assert documents["r"] is documents["r"]
    assert documents["r"].xdm is refused
    assert registry.lookup(refused) is None
    assert reads == [admitted, refused]


def test_multi_document_set_operators_are_deterministic_and_total() -> None:
    """union, intersect, and except order nodes from different trees."""
    first = _document(b"<r><x>1</x></r>")
    second = _document(b"<r><x>2</x></r>")
    engine = XPathEngine("3.1")
    static = XPathStaticContext()
    environment = ExpressionEnvironment(documents={"a": first, "b": second})

    def values(expression: str) -> list[str]:
        compiled = engine.compile(expression, static)
        result = engine.evaluate(compiled, environment)
        return [item.string_value for item in result if isinstance(item, XdmNode)]

    union = "doc('a')/r/x | doc('b')/r/x"
    assert values(union) == ["1", "2"]
    assert values(f"({union}) intersect doc('b')/r/x") == ["2"]
    assert values(f"({union}) except doc('b')/r/x") == ["1"]


def test_descendant_path_step_orders_nodes_from_two_documents() -> None:
    first = _document(b"<r><x>1</x></r>")
    second = _document(b"<r><x>2</x></r>")
    engine = XPathEngine("3.1")
    compiled = engine.compile("(doc('a'), doc('b'))//x", XPathStaticContext())
    result = engine.evaluate(compiled, ExpressionEnvironment(documents={"a": first, "b": second}))
    assert [item.string_value for item in result if isinstance(item, XdmNode)] == ["1", "2"]


def test_union_deduplicates_nodes_reached_by_two_paths() -> None:
    body = """
    <xsl:variable name="d" select="/"/>
    <xsl:template match="/">
      <out>
        <xsl:value-of select="count($d//b | //b)"/>
        <xsl:text>|</xsl:text>
        <xsl:value-of select="count(//b | //b)"/>
        <xsl:text>|</xsl:text>
        <xsl:value-of select="count($d//b | $d//b | //b)"/>
        <xsl:text>|</xsl:text>
        <xsl:value-of select="count(//b[1] | /doc/b[1])"/>
      </out>
    </xsl:template>"""
    assert _transform(body) == "3|3|3|1"


def test_axis_order_survives_reuse() -> None:
    body = """
    <xsl:variable name="d" select="/"/>
    <xsl:template match="/">
      <out>
        <xsl:value-of select="count($d/doc/b[2]/preceding-sibling::b)"/>
        <xsl:text>|</xsl:text>
        <xsl:value-of select="count($d/doc/b[1]/following-sibling::b)"/>
        <xsl:text>|</xsl:text>
        <xsl:value-of select="count(/doc/b[1]/following::b)"/>
        <xsl:text>|</xsl:text>
        <xsl:value-of select="$d/doc/b[1] &lt;&lt; /doc/b[3]"/>
        <xsl:text>|</xsl:text>
        <xsl:value-of select="$d/doc/b[3] &gt;&gt; /doc/b[1]"/>
        <xsl:text>|</xsl:text>
        <xsl:value-of select="count($d/doc/b[2]/preceding-sibling::b)"/>
      </out>
    </xsl:template>"""
    assert _transform(body) == "1|2|2|true|true|1"


def test_position_and_last_do_not_cross_sequences() -> None:
    body = """
    <xsl:variable name="d" select="/"/>
    <xsl:template match="/">
      <out>
        <xsl:for-each select="$d/doc/b">
          <xsl:value-of select="concat(position(), '/', last(), ';')"/>
        </xsl:for-each>
        <xsl:text>|</xsl:text>
        <xsl:for-each select="$d/doc/b[1]">
          <xsl:value-of select="concat(position(), '/', last(), ';')"/>
        </xsl:for-each>
        <xsl:text>|</xsl:text>
        <xsl:for-each select="$d/doc/b">
          <xsl:value-of select="concat(position(), '/', last(), ';')"/>
        </xsl:for-each>
      </out>
    </xsl:template>"""
    assert _transform(body) == "1/3;2/3;3/3;|1/1;|1/3;2/3;3/3;"

    # The focus is the only place a sequence's position lives, so the same
    # compiled expression answers differently for a different sequence.
    engine = XPathEngine()
    compiled = engine.compile("concat(position(), '/', last())")
    document = _document()
    registry = _XdmAdapterRegistry()
    environment = ExpressionEnvironment(adapter_registry=registry)
    first = document.root_element
    observed = [
        engine.evaluate(
            compiled,
            environment,
            focus=Focus.singleton(node, position=position, size=3),
        )[0].lexical
        for position, node in enumerate(first.children, start=1)
    ]
    assert observed == ["1/3", "2/3", "3/3"]
    assert (
        engine.evaluate(compiled, environment, focus=Focus.singleton(first, position=1, size=1))[
            0
        ].lexical
        == "1/1"
    )
    # The focus's document is registered once, even though only children of it
    # are the context item.
    assert registry.documents_built == 1


# ---------------------------------------------------------------------------
# §5.2/§7.3 — finalized temporary trees are admitted; detached nodes are routed


def test_a_finalized_temporary_tree_is_registered_once_and_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registries = _registry_recorder(monkeypatch)
    body = """
    <xsl:variable name="t" as="element()">
      <t><b x="1"/><b x="2"/><b x="3"/></t>
    </xsl:variable>
    <xsl:template name="xsl:initial-template">
      <out>
        <xsl:value-of select="count($t/b)"/>
        <xsl:text>|</xsl:text>
        <xsl:value-of select="$t/b[1] is $t/b[1]"/>
        <xsl:text>|</xsl:text>
        <xsl:value-of select="count($t/b | $t/b)"/>
        <xsl:text>|</xsl:text>
        <xsl:value-of select="count($t/b[2]/preceding-sibling::b)"/>
        <xsl:text>|</xsl:text>
        <xsl:value-of select="$t/b[2]/@x"/>
        <xsl:text>|</xsl:text>
        <xsl:call-template name="probe">
          <xsl:with-param name="p" select="$t/b[2]"/>
        </xsl:call-template>
      </out>
    </xsl:template>
    <xsl:template name="probe">
      <xsl:param name="p"/>
      <xsl:value-of select="$p/@x"/>
      <xsl:text>:</xsl:text>
      <xsl:value-of select="count($p/preceding-sibling::b)"/>
    </xsl:template>"""
    assert (
        _transform(body, source=None, initial_template="xsl:initial-template") == "3|true|3|1|2|2:1"
    )

    # Delivery finalizes the temporary tree, so the registry admits it once and
    # every later evaluation reuses the same adapter tree.  The budget counts
    # the tree's child-axis nodes: t and the three b's (the tree has no
    # document node).
    assert len(registries) == 1
    registry = registries[0]
    assert registry.document_count == 1
    assert registry.documents_built == 1
    assert registry.nodes_built > 0
    assert registry.admitted_nodes == 4


def test_a_finalized_rootless_tree_reuses_its_adapters_across_bridges() -> None:
    from pyxsd.xslt.runtime.builder import _NodeFactory

    factory = _NodeFactory()
    root = factory.element("t")
    for _ in range(3):
        factory._append(root, factory.element("b"))
    factory.finalize_root(root)
    assert root.parent is None
    assert root.document.document_order == 0

    registry = _XdmAdapterRegistry()
    engine = XPathEngine()
    static = XPathStaticContext(variable_types={"p": "element()"})
    compiled = engine.compile("count($p/b)", static)

    first = ExpressionEnvironment(variables={"p": root}, adapter_registry=registry)
    assert engine.evaluate(compiled, first, focus=Focus.absent()) == (XdmAtomic.integer(3),)
    built = registry.nodes_built
    assert built > 0
    assert registry.document_count == 1
    assert registry.admitted_nodes == 4

    second = ExpressionEnvironment(variables={"p": root}, adapter_registry=registry)
    assert engine.evaluate(compiled, second, focus=Focus.absent()) == (XdmAtomic.integer(3),)
    assert registry.nodes_built == built

    # A descendant delivered without its root still admits the whole tree once.
    descendant_static = XPathStaticContext(variable_types={"p": "element()"})
    descendant_compiled = engine.compile("count($p/preceding-sibling::b)", descendant_static)
    middle = root.children[1]
    third = ExpressionEnvironment(variables={"p": middle}, adapter_registry=registry)
    assert engine.evaluate(descendant_compiled, third, focus=Focus.absent()) == (
        XdmAtomic.integer(1),
    )
    assert registry.nodes_built == built


def test_a_detached_descendant_is_adapted_from_its_root() -> None:
    """A node whose document does not contain it is reached through its root.

    The XSLT route above happens to adapt the temporary tree's root through the
    caller's variable frame, so this arm pins the fallback itself: the only
    variable in the environment is a middle child, so adapting it would leave
    its ancestors and siblings unknown unless the fallback adapts the root
    first.  This is the path that used to end in the engine's bare assertion.
    """
    from pyxsd.xslt.runtime.builder import _NodeFactory

    # A builder tree that was never finalized: an element-rooted tree whose
    # document was not appended to and not finalized.
    factory = _NodeFactory()
    root = factory.element("t")
    for index in (1, 2, 3):
        element = factory.element("b")
        factory.attach_attribute(element, factory.attribute("x", str(index), parent=element))
        factory._append(root, element)
    document = factory.document
    assert document.document_order == -1
    assert document.children == ()
    middle = root.children[1]

    registry = _XdmAdapterRegistry()
    engine = XPathEngine()
    static = XPathStaticContext(variable_types={"p": "element()"})
    compiled = engine.compile("concat($p/@x, ':', count($p/preceding-sibling::b))", static)
    environment = ExpressionEnvironment(variables={"p": middle}, adapter_registry=registry)
    assert engine.evaluate(compiled, environment, focus=Focus.absent()) == (
        XdmAtomic.string("2:1"),
    )
    assert registry.document_count == 0
    assert registry.nodes_built == 0

    # The same answer when the root is the variable and the descendant is the
    # focus: the two routes must not disagree about the tree they project.
    attached = ExpressionEnvironment(
        variables={"t": root, "p": middle}, adapter_registry=_XdmAdapterRegistry()
    )
    assert engine.evaluate(compiled, attached, focus=middle) == (XdmAtomic.string("2:1"),)


# ---------------------------------------------------------------------------
# §5.3 — one registry per invocation, never shared


def test_nested_transform_gets_its_own_adapter_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registries = _registry_recorder(monkeypatch)
    nested = (
        (
            "<xsl:stylesheet xmlns:xsl='" + XSLT_NS + "' version='3.0'>"
            "<xsl:template match='/'><out><xsl:value-of select='count(//b)'/></out>"
            "</xsl:template></xsl:stylesheet>"
        )
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    body = f"""
    <xsl:template match="/">
      <out>
        <xsl:value-of select="count(//b)"/>
        <xsl:text>|</xsl:text>
        <xsl:variable name="nested" as="xs:string">{nested}</xsl:variable>
        <xsl:value-of
            select="(transform(map{{'stylesheet-text': $nested, 'source-node': /}})?output)/out"/>
      </out>
    </xsl:template>"""
    compiled = XsltProcessor().compile(io.BytesIO(_stylesheet(body)))
    result = compiled.transform(source=SOURCE)
    assert result.principal_tree.string_value == "3|3"

    # One invocation per transformation: the caller's registry and the nested
    # one are distinct, and the same XDM document is adapted by both.  The
    # outer registry also adapts the nested result tree it consumes, so only
    # the inner count is exact.
    assert len(registries) == 2
    outer, inner = registries
    assert outer is not inner
    assert outer.documents_built >= 1
    assert inner.documents_built == 1


def test_concurrent_transformations_do_not_share_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = """
    <xsl:variable name="d" select="/"/>
    <xsl:template match="/">
      <out><xsl:value-of select="count($d//b | //b)"/></out>
    </xsl:template>"""
    compiled = XsltProcessor().compile(io.BytesIO(_stylesheet(body)))
    expected = compiled.transform(source=SOURCE).principal_tree.string_value
    assert expected == "3"

    registries = _registry_recorder(monkeypatch)
    barrier = threading.Barrier(2)
    failure: list[BaseException] = []

    def run() -> str:
        try:
            barrier.wait(timeout=30)
            return compiled.transform(source=SOURCE).principal_tree.string_value
        except BaseException as exc:  # a worker error must fail fast, not hang
            failure.append(exc)
            barrier.abort()
            raise

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(run), pool.submit(run)]]
    assert not failure
    assert results == [expected, expected]
    assert len(registries) == 2
    assert registries[0] is not registries[1]


# ---------------------------------------------------------------------------
# §9 item 6 — the CI-side reuse proof: counters, never a wall clock


def test_removing_the_registry_rebuilds_the_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    document = _document()
    engine = XPathEngine()
    compiled = engine.compile("count(//b)")
    expected = _adapter_count(document) - 1

    registry = _XdmAdapterRegistry()
    cached = ExpressionEnvironment(parser_cache=XPathParserCache(), adapter_registry=registry)
    for _ in range(5):
        assert engine.evaluate(compiled, cached, focus=document) == (XdmAtomic.integer(3),)
    assert registry.documents_built == 1
    assert registry.nodes_built == expected

    built = 0
    original = _XdmElementAdapter.__init__

    def count(self: _XdmElementAdapter, *arguments: Any, **keywords: Any) -> None:
        nonlocal built
        built += 1
        original(self, *arguments, **keywords)

    monkeypatch.setattr(_XdmElementAdapter, "__init__", count)
    plain = ExpressionEnvironment(parser_cache=XPathParserCache())
    for _ in range(5):
        assert engine.evaluate(compiled, plain, focus=document) == (XdmAtomic.integer(3),)
    elements = 1 + len(document.root_element.children)
    assert built == 5 * elements
    assert built > registry.nodes_built


# ---------------------------------------------------------------------------
# §7.5 — admission: finalized documents only, two caps, never an eviction


def test_registry_admission_is_finalized_documents_and_a_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finalized = _document(b"<a><b/></a>")
    unfinalized = XdmDocument(
        source_name="<temp>",
        source_index=finalized.source_index,
        document_uri=None,
        base_uri=None,
    )
    registry = _XdmAdapterRegistry()
    assert unfinalized.document_order == -1
    assert registry.document(unfinalized) is None
    assert registry.document_count == 0
    admitted = registry.document(finalized)
    assert isinstance(admitted, _XdmDocumentAdapter)
    assert registry.document(finalized) is admitted
    assert (registry.documents_built, registry.documents_reused) == (1, 1)
    # adapt_node is the build protocol and is idempotent: a node that is
    # already registered answers with the adapter its tree gave it.
    built = registry.adapt_node(finalized.root_element)
    assert registry.adapt_node(finalized.root_element) is built
    assert built is admitted.children[0]

    # A document cap refuses, never evicts: the admitted tree keeps its
    # identity and the refused document still evaluates correctly.
    capped = _XdmAdapterRegistry(max_documents=1)
    first, second = _document(b"<a><b/></a>"), _document(b"<c><d/></c>")
    assert capped.document(first) is not None
    assert capped.document(second) is None
    assert capped.document_count == 1
    engine = XPathEngine()
    compiled = engine.compile("count(//*)")
    environment = ExpressionEnvironment(adapter_registry=capped)
    assert engine.evaluate(compiled, environment, focus=second) == (XdmAtomic.integer(2),)
    assert engine.evaluate(compiled, environment, focus=first) == (XdmAtomic.integer(2),)
    assert capped.documents_built == 1

    # The node budget is checked before a tree is built; the boundary and the
    # ephemeral fallback are pinned in the next test.
    budget = _XdmAdapterRegistry(max_nodes=5)
    exact = _document(b"<a><b/>x<c/></a>")
    assert budget.document(exact) is not None
    assert budget.document(_document(b"<a><b/>x<c/></a>")) is None
    assert budget.document_count == 1
    assert engine.evaluate(compiled, ExpressionEnvironment(adapter_registry=budget), focus=exact)

    for bad in (True, 1.0, "8"):
        with pytest.raises(TypeError, match="max_documents"):
            _XdmAdapterRegistry(max_documents=bad)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="max_nodes"):
            _XdmAdapterRegistry(max_nodes=bad)  # type: ignore[arg-type]
    for count in (0, -1):
        with pytest.raises(ValueError, match="max_documents"):
            _XdmAdapterRegistry(max_documents=count)
        with pytest.raises(ValueError, match="max_nodes"):
            _XdmAdapterRegistry(max_nodes=count)

    # clear() only costs rebuilds; the refused document is admitted after it.
    capped.clear()
    assert capped.document_count == 0
    assert capped.document(second) is not None
    assert capped.documents_built == 2
    assert engine.evaluate(compiled, environment, focus=second) == (XdmAtomic.integer(2),)

    # A bad environment handle is a programming error, even for an empty focus.
    compiled = engine.compile("1 + 1")
    with pytest.raises(TypeError, match="adapter_registry"):
        engine.evaluate(compiled, ExpressionEnvironment(adapter_registry=object()))
    with pytest.raises(TypeError, match="adapter_registry"):
        engine.evaluate(
            compiled, ExpressionEnvironment(adapter_registry=object()), focus=Focus.empty()
        )
    # The None control is the pre-Stage-3 behaviour: a fresh tree per
    # evaluation, and the same answers.
    plain = ExpressionEnvironment()
    assert engine.evaluate(compiled, plain, focus=Focus.absent()) == (XdmAtomic.integer(2),)


def test_node_budget_refuses_at_the_boundary_and_evaluates_through_ephemeral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget admits exactly its count, refuses the next, and still works.

    The unit is the census's child-axis node count, so ``<a><b/>x</a>`` holds
    four: the document, the element, its text, and the child element.
    """
    four = _document(b"<a><b/>x</a>")
    three = _document(b"<a><b/></a>")
    two = _document(b"<a/>")
    two_other = _document(b"<c/>")

    # Exact boundary: the document count equals the budget, so it is admitted.
    exact = _XdmAdapterRegistry(max_nodes=4)
    assert exact.document(four) is not None
    assert exact.admitted_nodes == 4
    assert exact.document(three) is None
    assert exact.admitted_nodes == 4
    assert exact.document_count == 1

    registry = _XdmAdapterRegistry(max_nodes=6)
    assert registry.document(four) is not None
    assert registry.admitted_nodes == 4
    # 4 + 3 would cross the budget: refused, and nothing is evicted.
    assert registry.document(three) is None
    assert registry.document_count == 1
    assert registry.admitted_nodes == 4
    # Refusal is per document, not a stop: a smaller document that still fits
    # is admitted up to the cap.
    assert registry.document(two) is not None
    assert registry.admitted_nodes == 6
    assert registry.document(two_other) is None
    assert registry.admitted_nodes == 6

    # The refused document evaluates correctly through the ephemeral path: its
    # adapter is in the evaluation's ephemeral map, never in the registry.
    bridges = _bridge_recorder(monkeypatch)
    engine = XPathEngine()
    compiled = engine.compile("count(//*)")
    environment = ExpressionEnvironment(adapter_registry=registry)
    assert engine.evaluate(compiled, environment, focus=three) == (XdmAtomic.integer(2),)
    first_bridge = bridges[0]
    assert id(three) in first_bridge._ephemeral
    assert id(three) not in registry.nodes
    assert engine.evaluate(compiled, environment, focus=four) == (XdmAtomic.integer(2),)
    assert engine.evaluate(compiled, environment, focus=two) == (XdmAtomic.integer(1),)
    # Only the two admitted documents ever built a tree.
    assert registry.documents_built == 2
    assert registry.document_count == 2
    assert registry.admitted_nodes == 6


def test_rootless_admission_budgets_and_refusals() -> None:
    from pyxsd.xslt.runtime.builder import _NodeFactory

    factory = _NodeFactory()
    root = factory.element("t")
    factory._append(root, factory.element("b"))
    factory.finalize_root(root)
    assert root.document.document_order == 0

    registry = _XdmAdapterRegistry()
    admitted = registry.rootless(root)
    assert admitted is not None
    assert registry.rootless(root) is admitted
    assert (registry.documents_built, registry.documents_reused) == (1, 1)
    assert registry.document_count == 1
    assert registry.admitted_nodes == 2
    # The same document cannot be held as both an empty document view and a
    # rootless tree view.
    assert registry.document(root.document) is None
    assert registry.document_count == 1

    # A document view admitted first refuses the rootless view of the same
    # document.
    document_registry = _XdmAdapterRegistry()
    assert document_registry.document(root.document) is not None
    assert document_registry.rootless(root) is None
    assert document_registry.document_count == 1

    # A second root in an already-registered document is refused: the document
    # carries one registered view.
    shared = _NodeFactory()
    first_root, second_root = shared.element("t"), shared.element("u")
    shared.document._finalize_document_order(first_root)
    same_document = _XdmAdapterRegistry()
    assert same_document.rootless(first_root) is not None
    assert same_document.rootless(second_root) is None

    # An unfinalized tree is not registrable.
    unfinalized = _NodeFactory()
    assert registry.rootless(unfinalized.element("v")) is None

    # The document cap and the node budget refuse, never evict.
    capped = _XdmAdapterRegistry(max_documents=1)
    assert capped.rootless(root) is not None
    other = _NodeFactory()
    other_root = other.element("w")
    other.finalize_root(other_root)
    assert capped.rootless(other_root) is None
    assert capped.document_count == 1

    exact = _XdmAdapterRegistry(max_nodes=2)
    assert exact.rootless(root) is not None
    assert exact.admitted_nodes == 2
    small = _XdmAdapterRegistry(max_nodes=1)
    assert small.rootless(root) is None
    assert small.document_count == 0
    assert small.admitted_nodes == 0

    # clear() drops the tree view, so it can be admitted again.
    capped.clear()
    assert capped.document_count == 0
    assert capped.admitted_nodes == 0
    assert capped.rootless(other_root) is not None


def test_the_document_cap_counts_both_registry_views() -> None:
    from pyxsd.xslt.runtime.builder import _NodeFactory

    factory = _NodeFactory()
    root = factory.element("t")
    factory.finalize_root(root)

    # A rootless view at the cap refuses a second document view.
    rootless_first = _XdmAdapterRegistry(max_documents=1)
    assert rootless_first.rootless(root) is not None
    assert rootless_first.document(_document(b"<a/>")) is None
    assert rootless_first.document_count == 1

    # A document view at the cap refuses a second rootless view, unchanged.
    document_first = _XdmAdapterRegistry(max_documents=1)
    assert document_first.document(_document(b"<a/>")) is not None
    assert document_first.rootless(root) is None
    assert document_first.document_count == 1


# ---------------------------------------------------------------------------
# §3.2 — a reused adapter pins no evaluation state


def test_escaped_adapter_keeps_no_evaluation_state(monkeypatch: pytest.MonkeyPatch) -> None:
    document = _document()
    element = document.root_element.children[0]
    engine = XPathEngine()
    compiled = engine.compile("count(//b)")
    registry = _XdmAdapterRegistry()

    bridges = _bridge_recorder(monkeypatch)
    first_environment = ExpressionEnvironment(adapter_registry=registry)
    engine.evaluate(compiled, first_environment, focus=document)
    (first_bridge,) = bridges
    escaped_document = first_bridge.adapt_item(document)
    escaped_element = first_bridge.adapt_item(element)

    # The document adapter is the only one that ever held a factory; the field
    # is gone, so no adapter keeps the creating evaluation alive.
    assert "_factory" not in _XdmDocumentAdapter.__slots__
    assert not hasattr(escaped_document, "_factory")
    assert escaped_document.xdm is document

    second_environment = ExpressionEnvironment(adapter_registry=registry)
    engine.evaluate(compiled, second_environment, focus=document)
    second_bridge = bridges[-1]
    assert second_bridge is not first_bridge
    assert second_bridge.adapt_item(document) is escaped_document
    assert second_bridge.adapt_item(element) is escaped_element
    # Evaluations in between did not build a second tree of anything.
    assert registry.documents_built == 1
    assert registry.nodes_built == _adapter_count(document) - 1


# ---------------------------------------------------------------------------
# The identity/axis/union/position/temp-tree stylesheets, end to end


@pytest.mark.parametrize(
    ("body", "expected", "source"),
    [
        (
            """<xsl:variable name="d" select="/"/><xsl:template match="/"><out>
            <xsl:value-of select="count($d//b | //b)"/></out></xsl:template>""",
            "3",
            SOURCE,
        ),
        (
            """<xsl:variable name="d" select="/"/><xsl:template match="/"><out>
            <xsl:value-of select="$d/doc/b[1] is /doc/b[1]"/></out></xsl:template>""",
            "true",
            SOURCE,
        ),
        (
            """<xsl:variable name="d" select="/"/><xsl:template match="/"><out>
            <xsl:value-of select="/doc/b[1] &lt;&lt; /doc/b[2]"/></out></xsl:template>""",
            "true",
            SOURCE,
        ),
        (
            """<xsl:variable name="d" select="/"/><xsl:template match="/"><out>
            <xsl:value-of select="name($d/doc/b[3]/preceding-sibling::b[1])"/></out>
            </xsl:template>""",
            "b",
            SOURCE,
        ),
        (
            """<xsl:variable name="d" select="/"/><xsl:template match="/"><out>
            <xsl:for-each select="$d/doc/b | /doc">
            <xsl:value-of select="concat(position(), '/', last(), ';')"/></xsl:for-each></out>
            </xsl:template>""",
            "1/4;2/4;3/4;4/4;",
            SOURCE,
        ),
        (
            """<xsl:template match="/"><xsl:variable name="t" as="element()">
            <t><b/><b/></t></xsl:variable><out>
            <xsl:value-of select="count($t/b)"/></out></xsl:template>""",
            "2",
            b"<doc/>",
        ),
        (
            """<xsl:variable name="t" as="document-node()" select="/"/><xsl:template match="/">
            <out><xsl:value-of select="count($t//b)"/></out></xsl:template>""",
            "3",
            SOURCE,
        ),
        (
            """<xsl:variable name="d" select="/"/><xsl:template match="/"><out>
            <xsl:value-of select="count($d//b)"/><xsl:value-of select="count($d//b)"/>
            </out></xsl:template>""",
            "33",
            SOURCE,
        ),
    ],
)
def test_identity_stylesheets_are_stable_under_reuse(
    body: str, expected: str, source: bytes | None
) -> None:
    assert _transform(body, source=source) == expected


def test_the_engine_declares_no_registry_in_its_public_surface() -> None:
    assert "_XdmAdapterRegistry" not in engine_module.__all__
    static = XPathStaticContext()
    assert not hasattr(static, "adapter_registry")
    assert ExpressionEnvironment().adapter_registry is None
