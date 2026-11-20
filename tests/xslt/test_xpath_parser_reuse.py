"""One XPath parser per static configuration per transformation.

Every cached bucket is keyed by the complete static context a parser bakes in,
and every dynamic-context component is supplied per evaluation through the
bucket's bridge holder.  These tests pin the reuse boundary (§4.7 of the stage
brief), the per-evaluation override and declared-function targets, the
reentrancy save/restore, and the per-transformation cache lifetime.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

import pytest
from tests.xslt.support import stylesheet as _stylesheet

from pyxsd.schema import Schema
from pyxsd.xdm import XdmAtomic, XdmFunction
from pyxsd.xdm.document import parse_typed_document
from pyxsd.xdm.nodes import XdmNode
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xpath.engine import XPathEngine, XPathParserCache
from pyxsd.xpath.environment import ExpressionEnvironment, Focus, XPathStaticContext
from pyxsd.xpath.functions import ContextFunction
from pyxsd.xslt import XsltProcessor

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
FN_NS = "http://www.w3.org/2005/xpath-functions"
TEST_NS = "urn:test"
SUP = "urn:example:parser-reuse"

SCHEMA = f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{SUP}"
    xmlns:s="{SUP}" elementFormDefault="qualified">
  <xs:simpleType name="Plain"><xs:restriction base="xs:string"/></xs:simpleType>
  <xs:element name="doc" type="s:Plain"/>
</xs:schema>"""


def _only_bucket(cache: XPathParserCache) -> Any:
    (bucket,) = cache._buckets.values()
    return bucket


def _cache_recorder(monkeypatch: pytest.MonkeyPatch) -> list[XPathParserCache]:
    """Record every ``XPathParserCache`` a transformation creates."""
    recorded: list[XPathParserCache] = []
    original = XPathParserCache.__init__

    def record(self: XPathParserCache, *, max_buckets: int = 256) -> None:
        original(self, max_buckets=max_buckets)
        recorded.append(self)

    monkeypatch.setattr(XPathParserCache, "__init__", record)
    return recorded


def _schema_pool() -> SchemaPool:
    compiled = Schema.compile(io.StringIO(SCHEMA))
    assert not compiled.report.has_errors, compiled.report
    return SchemaPool.from_schema(compiled)


# ---------------------------------------------------------------------------
# §4.4/§4.7 — reuse within one static configuration


def test_repeated_evaluation_reuses_one_parser_and_returns_identical_results() -> None:
    engine = XPathEngine()
    static = XPathStaticContext(variable_types={"n": "xs:integer"})
    compiled = engine.compile("$n + .", static)
    cache = XPathParserCache()

    cached_results = []
    parser = None
    tokenizer = None
    for index in range(1, 51):
        cached_results.append(
            engine.evaluate(
                compiled,
                ExpressionEnvironment(
                    variables={"n": XdmAtomic.integer(index)}, parser_cache=cache
                ),
                focus=XdmAtomic.integer(index * 10),
            )
        )
        if index == 1:
            parser = _only_bucket(cache).parser
            tokenizer = parser.tokenizer
            assert tokenizer is not None
    uncached_results = [
        engine.evaluate(
            compiled,
            ExpressionEnvironment(variables={"n": XdmAtomic.integer(index)}),
            focus=XdmAtomic.integer(index * 10),
        )
        for index in range(1, 51)
    ]

    assert cached_results == uncached_results
    assert [result[0].value for result in cached_results] == [index * 11 for index in range(1, 51)]
    assert cache.buckets_built == 1
    assert cache.buckets_reused == 49
    assert cache.bucket_count == 1
    # Reuse neither rebuilds the parser nor re-registers a token: the parser
    # captured after evaluation 1 is still the bucket's parser after the
    # fiftieth.  Parser identity is the discriminator — ``Parser.tokenizer`` is
    # a class-level compiled pattern shared through ``re``'s cache, so it stays
    # the same object even across a rebuild; asserting it too keeps the brief's
    # "no ``_register`` on the hot path" claim next to the identity that proves
    # the parser was never replaced.
    reused_parser = _only_bucket(cache).parser
    assert reused_parser is parser
    assert reused_parser.tokenizer is tokenizer

    # The cache-absent arm is today's behaviour, and a non-cache handle is a
    # programming error rather than a silently ignored resource — including
    # when the focus is empty and the evaluation would return immediately.
    with pytest.raises(TypeError, match="parser_cache"):
        engine.evaluate(compiled, ExpressionEnvironment(parser_cache=object()))
    with pytest.raises(TypeError, match="parser_cache"):
        engine.evaluate(compiled, ExpressionEnvironment(parser_cache=object()), focus=Focus.empty())


def test_parser_cache_separates_every_static_configuration() -> None:
    pool_a = _schema_pool()
    pool_b = _schema_pool()
    assert pool_a is not pool_b

    engines = {"3.1": XPathEngine("3.1"), "3.0": XPathEngine("3.0")}
    flag_engines = {"true": XPathEngine("3.1"), "false": XPathEngine("3.1", xslt_functions=False)}
    base = XPathStaticContext(namespaces={"m": TEST_NS})

    def current(item: Any, arguments: Any) -> Any:
        del arguments
        return (item,) if item is not None else ()

    def generate_id(item: Any, arguments: Any) -> Any:
        del arguments
        return (XdmAtomic.string("id"),)

    overrides_current = {f"{{{FN_NS}}}current": ContextFunction(current)}
    overrides_generate = {f"{{{FN_NS}}}generate-id": ContextFunction(generate_id)}
    declares_current = replace(base, functions={"fn:current": 0})

    # (label, engine, static context, environment, source, expected answer)
    configurations: list[
        tuple[str, XPathEngine, XPathStaticContext, ExpressionEnvironment, str, str]
    ] = [
        ("base", engines["3.1"], base, ExpressionEnvironment(), "1 + 1", "2"),
        (
            "namespaces",
            engines["3.1"],
            replace(base, namespaces={"m": TEST_NS, "p": "urn:p"}),
            ExpressionEnvironment(),
            "namespace-uri-from-QName(xs:QName('p:f'))",
            "urn:p",
        ),
        (
            "default_namespace",
            engines["3.1"],
            replace(base, default_namespace="urn:default"),
            ExpressionEnvironment(),
            "1 + 1",
            "2",
        ),
        (
            "variable_types",
            engines["3.1"],
            replace(base, variable_types={"v": "xs:integer"}),
            ExpressionEnvironment(),
            "1 + 1",
            "2",
        ),
        (
            "declared_function_arity_one",
            engines["3.1"],
            replace(base, functions={"m:f": 1}),
            ExpressionEnvironment(functions={"m:f": lambda value: value + 1}),
            "m:f(1)",
            "2",
        ),
        (
            "declared_function_arity_two",
            engines["3.1"],
            replace(base, functions={"m:f": 2}),
            ExpressionEnvironment(functions={"m:f": lambda left, right: left + right}),
            "m:f(1, 2)",
            "3",
        ),
        (
            "schema_a",
            engines["3.1"],
            replace(base, schema=pool_a),
            ExpressionEnvironment(),
            "1 + 1",
            "2",
        ),
        (
            "schema_b",
            engines["3.1"],
            replace(base, schema=pool_b),
            ExpressionEnvironment(),
            "1 + 1",
            "2",
        ),
        (
            "static_base_uri",
            engines["3.1"],
            replace(base, base_uri="urn:static"),
            ExpressionEnvironment(),
            "1 + 1",
            "2",
        ),
        (
            "environment_base_uri",
            engines["3.1"],
            base,
            ExpressionEnvironment(base_uri="urn:environment"),
            "1 + 1",
            "2",
        ),
        (
            "default_collation",
            engines["3.1"],
            replace(base, default_collation="urn:collation"),
            ExpressionEnvironment(),
            "1 + 1",
            "2",
        ),
        (
            "xsd_version",
            engines["3.1"],
            replace(base, xsd_version="1.0"),
            ExpressionEnvironment(),
            "1 + 1",
            "2",
        ),
        (
            "xslt_version",
            engines["3.1"],
            replace(base, xslt_version="3.0"),
            ExpressionEnvironment(),
            "1 + 1",
            "2",
        ),
        (
            "decimal_formats_dot",
            engines["3.1"],
            base,
            ExpressionEnvironment(decimal_formats={None: {"decimal-separator": "."}}),
            "format-number(1.5, '0.0')",
            "1.5",
        ),
        (
            "decimal_formats_comma",
            engines["3.1"],
            base,
            ExpressionEnvironment(decimal_formats={None: {"decimal-separator": ","}}),
            "format-number(1.5, '0,0')",
            "1,5",
        ),
        (
            "override_current",
            engines["3.1"],
            declares_current,
            ExpressionEnvironment(function_overrides=overrides_current),
            "1 + 1",
            "2",
        ),
        (
            "override_generate_id",
            engines["3.1"],
            base,
            ExpressionEnvironment(function_overrides=overrides_generate),
            "1 + 1",
            "2",
        ),
        ("xpath_version", engines["3.0"], base, ExpressionEnvironment(), "1 + 1", "2"),
        (
            "xslt_functions",
            flag_engines["false"],
            base,
            ExpressionEnvironment(),
            "1 + 1",
            "2",
        ),
    ]

    cache = XPathParserCache()
    for seen, (label, engine, static, environment, source, expected) in enumerate(
        configurations, start=1
    ):
        compiled = engine.compile(source, static)
        observed = engine.evaluate(
            compiled, replace(environment, parser_cache=cache), focus=Focus.absent()
        )
        assert observed[0].lexical == expected, label
        assert cache.bucket_count == seen, label

    assert cache.buckets_built == len(configurations)
    assert len({id(bucket.parser) for bucket in cache._buckets.values()}) == len(configurations)

    # Every configuration is answered by its own bucket, so a second pass is a
    # pure reuse and never a rebuild.
    for label, engine, static, environment, source, expected in configurations:
        compiled = engine.compile(source, static)
        observed = engine.evaluate(
            compiled, replace(environment, parser_cache=cache), focus=Focus.absent()
        )
        assert observed[0].lexical == expected, label
    assert cache.buckets_built == len(configurations)
    assert cache.buckets_reused == len(configurations)

    # Eviction and clear() only cost a rebuild, never correctness.
    small = XPathParserCache(max_buckets=1)
    for _, engine, static, environment, source, expected in configurations[:2]:
        compiled = engine.compile(source, static)
        assert (
            engine.evaluate(
                compiled, replace(environment, parser_cache=small), focus=Focus.absent()
            )[0].lexical
            == expected
        )
    assert small.buckets_built == 2
    assert small.bucket_count == 1
    small.clear()
    assert small.bucket_count == 0
    compiled = engines["3.1"].compile("1 + 1", base)
    assert (
        engines["3.1"]
        .evaluate(compiled, ExpressionEnvironment(parser_cache=small), focus=Focus.absent())[0]
        .lexical
        == "2"
    )
    assert small.buckets_built == 3


def test_parser_cache_separates_schema_pools_and_function_tables_by_values() -> None:
    """A key that omits one component fails exactly this test's sub-case."""
    pool_a = _schema_pool()
    pool_b = _schema_pool()
    engine = XPathEngine()
    cache = XPathParserCache()

    namespace_a = XPathStaticContext(namespaces={"m": TEST_NS})
    namespace_b = XPathStaticContext(namespaces={"m": TEST_NS, "p": "urn:p"})
    for static in (namespace_a, namespace_b):
        compiled = engine.compile("1 + 1", static)
        assert (
            engine.evaluate(
                compiled, ExpressionEnvironment(parser_cache=cache), focus=Focus.absent()
            )[0].lexical
            == "2"
        )
    assert cache.buckets_built == 2

    for pool in (pool_a, pool_b):
        compiled = engine.compile("1 + 1", replace(namespace_a, schema=pool))
        assert (
            engine.evaluate(
                compiled, ExpressionEnvironment(parser_cache=cache), focus=Focus.absent()
            )[0].lexical
            == "2"
        )
    assert cache.buckets_built == 4

    for functions, source, answer in (
        ({"m:f": 1}, "m:f(41)", "42"),
        ({"m:f": 2}, "m:f(40, 2)", "42"),
    ):
        static = replace(namespace_a, functions=functions)
        compiled = engine.compile(source, static)
        implementation = (
            (lambda value: value + 1) if source == "m:f(41)" else (lambda left, right: left + right)
        )
        environment = ExpressionEnvironment(functions={"m:f": implementation}, parser_cache=cache)
        assert engine.evaluate(compiled, environment, focus=Focus.absent())[0].lexical == answer
    assert cache.buckets_built == 6

    for separator, picture, answer in ((".", "0.0", "1.5"), (",", "0,0", "1,5")):
        compiled = engine.compile(f"format-number(1.5, '{picture}')", namespace_a)
        environment = ExpressionEnvironment(
            decimal_formats={None: {"decimal-separator": separator}}, parser_cache=cache
        )
        observed = engine.evaluate(compiled, environment, focus=Focus.absent())
        assert observed[0].lexical == answer
        assert cache.bucket_count == cache.buckets_built
    assert cache.buckets_built == 8


def test_default_and_lexically_named_none_decimal_formats_key_apart() -> None:
    """``None`` and the QName ``"None"`` are distinct keys, not one key."""
    engine = XPathEngine()
    compiled = engine.compile("format-number(-1, '0')", XPathStaticContext())
    bang_is_default = {None: {"minus-sign": "!"}, "None": {"minus-sign": "-"}}
    dash_is_default = {None: {"minus-sign": "-"}, "None": {"minus-sign": "!"}}
    cache = XPathParserCache()
    environments = [
        ExpressionEnvironment(decimal_formats=formats, parser_cache=cache)
        for formats in (bang_is_default, dash_is_default)
    ]

    cached = [
        engine.evaluate(compiled, environment, focus=Focus.absent())[0].lexical
        for environment in environments
    ]
    uncached = [
        engine.evaluate(compiled, replace(environment, parser_cache=None), focus=Focus.absent())[
            0
        ].lexical
        for environment in environments
    ]

    assert cached == uncached == ["!1", "-1"]
    assert cache.bucket_count == 2
    assert cache.buckets_built == 2


# ---------------------------------------------------------------------------
# §5 — lifetime: one cache per transformation, never process-global


def test_parser_cache_is_per_transformation(monkeypatch: pytest.MonkeyPatch) -> None:
    import pyxsd.xpath.engine as engine_module

    caches = _cache_recorder(monkeypatch)
    calls: list[Any] = []
    original = engine_module._build_parser

    def counting(*arguments: Any, **keywords: Any) -> Any:
        calls.append(arguments)
        return original(*arguments, **keywords)

    monkeypatch.setattr(engine_module, "_build_parser", counting)

    body = (
        '<xsl:template name="main">'
        '<xsl:variable name="v" select="2"/>'
        '<xsl:sequence select="count((1, 2, 3))"/>'
        '<xsl:sequence select="count((1, 2, 3))"/>'
        '<xsl:sequence select="$v + 1"/>'
        "</xsl:template>"
    )
    compiled = XsltProcessor().compile(io.BytesIO(_stylesheet(body)))
    before = len(calls)
    for _ in range(3):
        result = compiled.transform(initial_template="main")
        assert tuple(item.lexical for item in result.principal) == ("3", "3", "3")
    built = len(calls) - before

    assert len(caches) == 3
    assert len({id(cache) for cache in caches}) == 3
    configurations = {cache.buckets_built for cache in caches}
    assert len(configurations) == 1
    (count,) = configurations
    assert count >= 1
    assert built == count * 3
    assert sum(cache.buckets_reused for cache in caches) > 0


# ---------------------------------------------------------------------------
# §6.2 — dynamic function targets stay per evaluation


def test_declared_function_resolves_its_target_per_evaluation() -> None:
    engine = XPathEngine()
    static = XPathStaticContext(namespaces={"m": TEST_NS}, functions={"m:f": 1})
    compiled = engine.compile("m:f(1)", static)
    cache = XPathParserCache()

    first = engine.evaluate(
        compiled,
        ExpressionEnvironment(functions={"m:f": lambda value: value + 1}, parser_cache=cache),
        focus=Focus.absent(),
    )
    second = engine.evaluate(
        compiled,
        ExpressionEnvironment(functions={"m:f": lambda value: value + 100}, parser_cache=cache),
        focus=Focus.absent(),
    )
    assert first == (XdmAtomic.integer(2),)
    assert second == (XdmAtomic.integer(101),)
    assert cache.buckets_built == 1
    assert cache.buckets_reused == 1

    with pytest.raises(RuntimeError, match="has no environment implementation"):
        engine.evaluate(compiled, ExpressionEnvironment(parser_cache=cache), focus=Focus.absent())

    # A function item created under one target keeps working after the
    # evaluation that created it has ended, against that evaluation's target.
    reference = engine.compile("m:f#1", static)
    items = engine.evaluate(
        reference,
        ExpressionEnvironment(functions={"m:f": lambda value: value + 5}, parser_cache=cache),
        focus=Focus.absent(),
    )
    assert len(items) == 1
    escaped = items[0]
    assert isinstance(escaped, XdmFunction)
    assert escaped.invoke((XdmAtomic.integer(1),)) == (XdmAtomic.integer(6),)


def test_context_function_override_follows_the_focus_across_reused_evaluations() -> None:
    engine = XPathEngine()
    static = XPathStaticContext(
        namespaces={"m": TEST_NS},
        functions={"m:nested": 0, "fn:current": 0},
    )
    cache = XPathParserCache()

    def overrides(prefix: str) -> dict[str, ContextFunction]:
        def current(item: Any, arguments: Any) -> Any:
            del arguments
            return () if item is None else (XdmAtomic.string(f"{prefix}:{item.name}"),)

        def generate_id(item: Any, arguments: Any) -> Any:
            del arguments
            return () if item is None else (XdmAtomic.string(f"id-{prefix}:{item.name}"),)

        return {
            f"{{{FN_NS}}}current": ContextFunction(current),
            f"{{{FN_NS}}}generate-id": ContextFunction(generate_id),
        }

    source = "concat(string(current()), '|', generate-id(), '|', position())"
    outer_compiled = engine.compile(source, static)
    inner_compiled = engine.compile("string(current())", static)
    outer_overrides = overrides("outer")
    inner_overrides = overrides("inner")

    document = parse_typed_document(_document_xml(), schema_pool=None)
    first, second = document.root_element.children[0], document.root_element.children[1]

    def evaluate_outer(node: XdmNode, position: int) -> Any:
        return engine.evaluate(
            outer_compiled,
            ExpressionEnvironment(function_overrides=outer_overrides, parser_cache=cache),
            focus=Focus.singleton(node, position=position, size=2),
        )

    assert evaluate_outer(first, 1) == (XdmAtomic.string("outer:a|id-outer:a|1"),)
    assert evaluate_outer(second, 2) == (XdmAtomic.string("outer:b|id-outer:b|2"),)
    assert cache.buckets_built == 1

    nested_results: list[Any] = []

    def nested() -> Any:
        nested_results.append(
            engine.evaluate(
                inner_compiled,
                ExpressionEnvironment(function_overrides=inner_overrides, parser_cache=cache),
                focus=second,
            )
        )
        return nested_results[-1]

    compiled = engine.compile(
        "concat(string(current()), '|', m:nested(), '|', string(current()))", static
    )
    result = engine.evaluate(
        compiled,
        ExpressionEnvironment(
            functions={"m:nested": nested},
            function_overrides=outer_overrides,
            parser_cache=cache,
        ),
        focus=first,
    )
    # The nested evaluation sees its own override table ...
    assert nested_results == [(XdmAtomic.string("inner:b"),)]
    # ... and the enclosing evaluation keeps its own afterwards.
    assert result == (XdmAtomic.string("outer:a|inner:b|outer:a"),)
    assert cache.buckets_built == 1


def _document_xml() -> bytes:
    return b"<doc><a>1</a><b>2</b></doc>"


# ---------------------------------------------------------------------------
# §5.1 — a nested fn:transform is a separate dynamic context


def test_nested_transform_does_not_inherit_the_outer_parser_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caches = _cache_recorder(monkeypatch)
    nested_sheet = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0">'
        '<xsl:template name="xsl:initial-template">'
        '<nested><xsl:value-of select="string(1 + 1)"/></nested>'
        "</xsl:template></xsl:stylesheet>"
    )
    outer_body = (
        '<xsl:template name="main"><xsl:param name="options"/>'
        '<xsl:sequence select="transform($options)"/></xsl:template>'
    )
    compiled = XsltProcessor().compile(io.BytesIO(_stylesheet(outer_body)))
    options = _options_map({"stylesheet-text": nested_sheet, "delivery-format": "raw"})
    outer = compiled.transform(
        initial_template="main",
        delivery="raw",
        template_parameters={"options": options},
    )
    assert len(outer.principal) == 1
    nested_output = outer.principal[0].get(XdmAtomic.string("output"))
    assert isinstance(nested_output, tuple) and len(nested_output) == 1
    assert nested_output[0].local_name == "nested"
    assert nested_output[0].string_value == "2"

    assert len(caches) == 2
    outer_cache, inner_cache = caches
    assert outer_cache is not inner_cache
    assert outer_cache.buckets_built >= 1
    assert inner_cache.buckets_built >= 1
    assert inner_cache.bucket_count == inner_cache.buckets_built


def _options_map(options: Mapping[str, Any]) -> Any:
    from pyxsd.xdm import XdmMap

    return XdmMap.from_mapping(options)


# ---------------------------------------------------------------------------
# §6.1 — schema kind tests are bound per bucket


def test_schema_kind_tests_resolve_after_parser_reuse() -> None:
    pool = _schema_pool()
    body = (
        '<xsl:sequence select="count(schema-element(s:doc))"/>'
        '<xsl:sequence select="count(element(*, s:Plain))"/>'
    )
    source = f'<s:doc xmlns:s="{SUP}">x</s:doc>'.encode()
    sheet = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:s="{SUP}" version="3.0">'
        f'<xsl:template name="main">{body}{body}</xsl:template></xsl:stylesheet>'
    ).encode()
    compiled = XsltProcessor().compile(io.BytesIO(sheet), schema=pool)
    result = compiled.transform(io.BytesIO(source), initial_template="main", validation="strict")
    assert tuple(item.lexical for item in result.principal) == ("1", "1", "1", "1")

    engine = XPathEngine()
    static_a = XPathStaticContext(namespaces={"s": SUP}, schema=pool, xsd_version="1.0")
    static_b = replace(static_a, schema=_schema_pool())
    expression = "count(schema-element(s:doc))"
    compiled_a = engine.compile(expression, static_a)
    compiled_b = engine.compile(expression, static_b)
    document = parse_typed_document(source, schema_pool=pool, validation="strict")
    cache = XPathParserCache()

    api_a = engine.evaluate(compiled_a, ExpressionEnvironment(parser_cache=cache), focus=document)
    assert api_a == (XdmAtomic.integer(1),)
    assert cache.buckets_built == 1
    api_b = engine.evaluate(compiled_b, ExpressionEnvironment(parser_cache=cache), focus=document)
    assert api_b == (XdmAtomic.integer(1),)
    assert cache.buckets_built == 2


# ---------------------------------------------------------------------------
# §5.2 — unrelated transformations never share a cache


def test_concurrent_transformations_keep_independent_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caches = _cache_recorder(monkeypatch)
    body = (
        '<xsl:template match="/"><out><xsl:value-of select="count(doc/item)"/></out></xsl:template>'
    )
    compiled = XsltProcessor().compile(io.BytesIO(_stylesheet(body)))
    source = b"<doc><item/><item/></doc>"
    expected = compiled.transform(source).principal_tree.string_value
    assert expected == "2"

    barrier = threading.Barrier(2)

    def worker() -> str:
        try:
            barrier.wait()
            return compiled.transform(source).principal_tree.string_value
        except BaseException:
            barrier.abort()
            raise

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker) for _ in range(2)]
        results = [future.result() for future in futures]

    assert results == [expected, expected]
    assert len(caches) == 3
    assert len({id(cache) for cache in caches}) == 3
    assert all(cache.buckets_built >= 1 for cache in caches)


# ---------------------------------------------------------------------------
# The key itself


def test_parser_key_lists_every_static_component() -> None:
    from pyxsd.xpath.engine import _parser_key

    static = XPathStaticContext(
        namespaces={"m": TEST_NS},
        default_namespace="urn:default",
        variable_types={"v": "xs:integer"},
        functions={"m:f": 1, "m:g": (1, 2)},
        schema=None,
        base_uri="urn:static",
        default_collation="urn:collation",
        xsd_version="1.1",
        xslt_version="3.0",
    )
    environment = ExpressionEnvironment(
        base_uri="urn:environment",
        decimal_formats={None: {"decimal-separator": "."}},
    )
    key = _parser_key("3.1", True, static, environment)
    assert key[0] == "3.1"
    assert key[1] is True
    assert key[2] == (("m", TEST_NS),)
    assert key[3] == "urn:default"
    assert key[4] == (("v", "xs:integer"),)
    assert key[5] == (("m:f", 1), ("m:g", (1, 2)))
    assert key[7] == "urn:static"
    assert key[8] == "urn:environment"
    assert key[9] == "urn:collation"
    assert key[10] == "1.1"
    assert key[11] == "3.0"
    assert key[12] == (((True, ""), (("decimal-separator", "."),)),)
    assert key[13] == frozenset()

    tagged = _parser_key(
        "3.1",
        True,
        static,
        ExpressionEnvironment(
            decimal_formats={
                "None": {"zero-digit": "0"},
                None: {"decimal-separator": "."},
            }
        ),
    )
    assert tagged[12] == (
        ((False, "None"), (("zero-digit", "0"),)),
        ((True, ""), (("decimal-separator", "."),)),
    )

    unordered = XPathStaticContext(namespaces={"a": "urn:a", "b": "urn:b"})
    assert _parser_key("3.1", False, unordered, ExpressionEnvironment()) == _parser_key(
        "3.1",
        False,
        XPathStaticContext(namespaces={"b": "urn:b", "a": "urn:a"}),
        ExpressionEnvironment(),
    )


def test_parser_cache_rejects_invalid_bounds() -> None:
    with pytest.raises(TypeError, match="max_buckets"):
        XPathParserCache(max_buckets=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at least 1"):
        XPathParserCache(max_buckets=0)


def test_sequence_of_overrides_maps_by_symbol() -> None:
    """Two lexical spellings of one symbol: the later target wins."""
    engine = XPathEngine()
    static = XPathStaticContext(namespaces={"m": TEST_NS}, functions={"fn:current": 0})

    def current(item: Any, arguments: Any) -> Any:
        del arguments
        return () if item is None else (XdmAtomic.string(item.name),)

    def first(item: Any, arguments: Any) -> Any:
        del item, arguments
        return (XdmAtomic.string("first"),)

    overrides = {
        f"{{{FN_NS}}}current": ContextFunction(current),
        "fn:current": ContextFunction(first),
    }
    compiled = engine.compile("string(current())", static)
    cache = XPathParserCache()
    document = parse_typed_document(_document_xml(), schema_pool=None)
    result = engine.evaluate(
        compiled,
        ExpressionEnvironment(function_overrides=overrides, parser_cache=cache),
        focus=document.root_element.children[0],
    )
    assert result == (XdmAtomic.string("first"),)
    assert cache.buckets_built == 1


def test_override_token_dispatches_on_the_runtime_target_kind() -> None:
    """One bucket serves a symbol whose targets change kind between evaluations."""
    engine = XPathEngine()
    static = XPathStaticContext(namespaces={"m": TEST_NS}, functions={"fn:current": 0})
    compiled = engine.compile("string(current())", static)
    cache = XPathParserCache()
    document = parse_typed_document(_document_xml(), schema_pool=None)
    node = document.root_element.children[0]

    def contextual(item: Any, arguments: Any) -> Any:
        del arguments
        return () if item is None else (XdmAtomic.string(f"context:{item.name}"),)

    def plain(item: Any, arguments: Any) -> Any:
        del item, arguments
        return (XdmAtomic.string("plain"),)

    contextual_environment = ExpressionEnvironment(
        function_overrides={f"{{{FN_NS}}}current": ContextFunction(contextual)},
        parser_cache=cache,
    )
    plain_environment = ExpressionEnvironment(
        function_overrides={f"{{{FN_NS}}}current": plain}, parser_cache=cache
    )

    assert engine.evaluate(compiled, contextual_environment, focus=node) == (
        XdmAtomic.string("context:a"),
    )
    assert engine.evaluate(compiled, plain_environment, focus=node) == (XdmAtomic.string("plain"),)
    assert engine.evaluate(compiled, contextual_environment, focus=node) == (
        XdmAtomic.string("context:a"),
    )
    assert cache.buckets_built == 1
    assert cache.buckets_reused == 2


def test_bucket_rebuilds_when_a_key_collides_on_a_different_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A schema-pool identity mismatch rebuilds instead of sharing the bucket."""
    import pyxsd.xpath.engine as engine_module

    monkeypatch.setattr(engine_module, "_parser_key", lambda *arguments, **keywords: ("same",))
    engine = XPathEngine()
    static_a = XPathStaticContext(namespaces={"m": TEST_NS}, schema=_schema_pool())
    static_b = replace(static_a, schema=_schema_pool())
    cache = XPathParserCache()

    for static in (static_a, static_b):
        compiled = engine.compile("1 + 1", static)
        assert (
            engine.evaluate(
                compiled, ExpressionEnvironment(parser_cache=cache), focus=Focus.absent()
            )[0].lexical
            == "2"
        )

    assert cache.bucket_count == 1
    assert cache.buckets_built == 2
    assert cache.buckets_reused == 0
    assert _only_bucket(cache).static_context.schema is static_b.schema


def test_function_item_without_a_bucket_parser_keeps_its_token_context() -> None:
    """A token from another parser has no bucket slots to restore."""
    from elementpath import XPathContext
    from elementpath.xpath3 import XPath31Parser

    from pyxsd.xpath.engine import _build_parser, _ValueBridge

    elementpath_parser = XPath31Parser()
    token = elementpath_parser.parse("concat#2").evaluate(XPathContext(item=""))
    static = XPathStaticContext()
    bridge = _ValueBridge(_build_parser("3.1", static, key=False), static, ExpressionEnvironment())
    bridge._context = XPathContext(item="")

    item = bridge._function_item(token)
    assert item.invoke((XdmAtomic.string("a"), XdmAtomic.string("b"))) == (XdmAtomic.string("ab"),)


def test_plain_override_function_item_keeps_its_dynamic_call() -> None:
    """A plain-callable override keeps the replaced token's dynamic behavior."""
    engine = XPathEngine()
    static = XPathStaticContext(namespaces={"fn": FN_NS}, functions={"fn:current": 0})
    compiled = engine.compile("string(current#0())", static)
    cache = XPathParserCache()

    def plain(item: Any, arguments: Any) -> Any:
        del arguments
        return () if item is None else (XdmAtomic.string(f"plain:{item.name}"),)

    document = parse_typed_document(_document_xml(), schema_pool=None)
    result = engine.evaluate(
        compiled,
        ExpressionEnvironment(
            function_overrides={f"{{{FN_NS}}}current": plain}, parser_cache=cache
        ),
        focus=document.root_element.children[0],
    )
    assert result == (XdmAtomic.string("plain:a"),)
    assert cache.buckets_built == 1


def test_duplicate_override_spellings_install_one_class_per_symbol() -> None:
    """Two lexical spellings of one symbol must not chain override classes."""
    engine = XPathEngine()
    static = XPathStaticContext(functions={"fn:current": 0})

    def current(item: Any, arguments: Any) -> Any:
        del item, arguments
        return (XdmAtomic.string("p"),)

    overrides = {f"{{{FN_NS}}}current": current, "fn:current": current}
    environments = [
        engine.compile("string(current())", static),
        engine.compile("string(current#0())", static),
    ]
    for compiled in environments:
        assert engine.evaluate(
            compiled,
            ExpressionEnvironment(function_overrides=overrides),
            focus=Focus.absent(),
        ) == (XdmAtomic.string("p"),)


def test_duplicate_spelling_bucket_is_not_poisoned() -> None:
    """The key canonicalises both spellings to one bucket; neither poisons it.

    A duplicate-spelling installation used to chain the second override class
    onto the first, so the bucket a duplicate-spelling evaluation built raised
    ``RecursionError`` for every later evaluation of that ``frozenset`` key.
    """
    engine = XPathEngine()
    static = XPathStaticContext(functions={"fn:current": 0})
    compiled = engine.compile("string(current#0())", static)

    def first(item: Any, arguments: Any) -> Any:
        del item, arguments
        return (XdmAtomic.string("p"),)

    def second(item: Any, arguments: Any) -> Any:
        del item, arguments
        return (XdmAtomic.string("q"),)

    single: dict[str, Any] = {f"{{{FN_NS}}}current": first}
    duplicate: dict[str, Any] = {f"{{{FN_NS}}}current": first, "fn:current": second}

    for sequence in (
        ((single, "p"), (duplicate, "q"), (single, "p")),
        ((duplicate, "q"), (single, "p")),
    ):
        cache = XPathParserCache()
        for overrides, expected in sequence:
            assert engine.evaluate(
                compiled,
                ExpressionEnvironment(function_overrides=overrides, parser_cache=cache),
                focus=Focus.absent(),
            ) == (XdmAtomic.string(expected),)
        assert cache.buckets_built == 1


def test_plain_override_does_not_inherit_the_replaced_token_select() -> None:
    """A base that defines ``select`` without ``evaluate`` cannot bypass the override.

    ``fn:trace`` is such a base.  In argument position the token is read through
    ``select``: before the override class stopped inheriting the replaced token,
    that read ran the base's ``select`` and returned the traced value instead of
    the override's result, while the ``trace#2(…)`` call form already reached the
    override.
    """
    engine = XPathEngine()
    calls: list[tuple[Any, tuple[Any, ...]]] = []

    def replacement(item: Any, arguments: Any) -> Any:
        calls.append((item, arguments))
        return (XdmAtomic.string("t"),)

    overrides = {f"{{{FN_NS}}}trace": replacement}
    for source in ("string(trace(1, 'l'))", "string(trace#2(1, 'l'))"):
        compiled = engine.compile(source)
        assert engine.evaluate(
            compiled,
            ExpressionEnvironment(function_overrides=overrides),
            focus=Focus.absent(),
        ) == (XdmAtomic.string("t"),), source
    assert len(calls) == 2


def test_context_function_token_without_a_bridge_has_no_evaluation_context() -> None:
    from types import SimpleNamespace

    from elementpath.xpath3 import XPath31Parser

    from pyxsd.xpath.functions import ContextFunctionToken

    def direct(item: Any, arguments: Any) -> Any:
        del item, arguments
        return ()

    token = ContextFunctionToken(XPath31Parser())
    token.holder = SimpleNamespace(
        overrides={"context-function": ContextFunction(direct)}, bridge=None
    )
    with pytest.raises(RuntimeError, match="no evaluation context"):
        token.evaluate(None)
