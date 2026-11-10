"""Dynamic XPath evaluation with a complete static-context identity.

Every stylesheet and schema is written inline.  The tests assert behavior
through the public processor and through the dynamic compilation cache; none
depends on the pinned conformance corpus.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from elementpath.datatypes import QName

from pyxsd.schema import Schema
from pyxsd.xdm import XdmAtomic, XdmMap, as_xdm
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xpath.environment import ExpressionEnvironment, Focus
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltDynamicError, XsltError
from pyxsd.xslt.runtime.evaluate import (
    DynamicExpressionCache,
    StaticContextIdentity,
    compile_cache_key,
    evaluate_dynamic,
    with_params_variables,
)

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
MY_NS = "urn:my"
OTHER_NS = "urn:other"
ERROR_NS = "http://www.w3.org/2005/xqt-errors"


def stylesheet(body: str, *, attributes: str = "", namespaces: str = "") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:my="{MY_NS}" xmlns:other="{OTHER_NS}" version="3.0"'
        f"{attributes}{namespaces}>{body}</xsl:stylesheet>"
    ).encode()


def compiled(
    source: bytes,
    *,
    static_options: Any = None,
    schema: Any = None,
):
    return XsltProcessor(static_options=static_options or {}).compile(
        io.BytesIO(source), schema=schema
    )


def transform(
    source: bytes,
    input_source: bytes | None = None,
    *,
    delivery: str = "raw",
    **options: Any,
):
    stylesheet_ = compiled(source)
    if input_source is None:
        return stylesheet_.transform(delivery=delivery, **options)
    return stylesheet_.transform(source=input_source, delivery=delivery, **options)


def error_code(call: Any) -> str | None:
    with pytest.raises(XsltError) as caught:
        call()
    return caught.value.code


def identity(**overrides: Any) -> StaticContextIdentity:
    values: dict[str, Any] = {
        "xpath_version": "3.1",
        "namespaces": {"my": MY_NS, "xs": XSD_NS},
        "default_namespace": None,
        "base_uri": "http://example.com/base/",
        "variable_types": {"amount": "xs:integer"},
        "functions": {"{urn:my}twice": 1},
        "schema_aware": False,
        "default_collation": "http://www.w3.org/2005/xpath-functions/collation/codepoint",
        "xslt_version": "3.0",
        "package_name": None,
    }
    values.update(overrides)
    return StaticContextIdentity(**values)


# ---------------------------------------------------------------------------
# Static-context cache identity


def test_cache_key_covers_every_static_context_component() -> None:
    base = identity()
    assert compile_cache_key("same-text", base) == ("same-text", identity().key())
    assert compile_cache_key("same-text", base) != compile_cache_key("other-text", base)

    variants = [
        identity(namespaces={"my": OTHER_NS, "xs": XSD_NS}),
        identity(default_namespace="urn:def"),
        identity(base_uri="http://example.com/other/"),
        identity(variable_types={"amount": "xs:double"}),
        identity(functions={"{urn:my}twice": 2}),
        identity(schema_aware=True),
        identity(default_collation="urn:collation"),
        identity(xpath_version="3.0"),
        identity(xslt_version="2.0"),
        identity(package_name="urn:package"),
    ]
    for variant in variants:
        assert compile_cache_key("same-text", base) != compile_cache_key("same-text", variant)


def test_schema_identity_separates_equal_schema_shapes() -> None:
    first = SchemaPool()
    second = SchemaPool()
    left = identity(schema=first, schema_aware=True)
    right = identity(schema=second, schema_aware=True)
    assert compile_cache_key("1 + 1", left) != compile_cache_key("1 + 1", right)


def test_identity_builds_the_xpath_static_context() -> None:
    pool = SchemaPool()
    aware = identity(schema=pool, schema_aware=True)
    plain = identity(schema=pool, schema_aware=False)

    assert aware.xpath_static_context().schema is pool
    assert plain.xpath_static_context().schema is None
    assert aware.key() == identity(schema=pool, schema_aware=True).key()


# ---------------------------------------------------------------------------
# Bounded invocation-local cache


def test_dynamic_cache_is_bounded_and_reuses_only_identical_contexts() -> None:
    cache = DynamicExpressionCache(max_entries=2)
    first = identity()
    second = identity(
        namespaces={"my": OTHER_NS, "xs": XSD_NS},
        functions={"{urn:other}twice": 1},
    )

    one = cache.compile("1 + 1", first)
    assert cache.compile("1 + 1", first) is one
    assert cache.compile_count == 1

    cache.compile("1 + 1", second)
    assert cache.compile_count == 2
    cache.compile("2 + 2", first)
    assert cache.compile_count == 3
    assert len(cache) == 2

    cache.compile("1 + 1", first)
    assert cache.compile_count == 4
    assert len(cache) == 2

    with pytest.raises(ValueError):
        DynamicExpressionCache(max_entries=0)


def test_dynamic_cache_wraps_static_failures_as_xtde3160() -> None:
    cache = DynamicExpressionCache()
    with pytest.raises(XsltDynamicError) as caught:
        cache.compile("1 +", identity())
    assert caught.value.code == "XTDE3160"
    assert "1 +" in str(caught.value)


def test_evaluate_dynamic_accepts_a_compiled_expression() -> None:
    result = evaluate_dynamic(
        "2 + 2",
        identity(),
        ExpressionEnvironment(),
        focus=Focus.absent(),
        cache=DynamicExpressionCache(),
    )
    assert result == (XdmAtomic.integer(4),)


def test_evaluate_dynamic_preserves_runtime_error_codes() -> None:
    with pytest.raises(XsltDynamicError) as caught:
        evaluate_dynamic(
            "1 div 0",
            identity(),
            ExpressionEnvironment(),
            focus=Focus.absent(),
            cache=DynamicExpressionCache(),
        )
    assert caught.value.code == "FOAR0001"
    assert caught.value.__cause__ is not None


# ---------------------------------------------------------------------------
# Validation and defensive branches


def test_identity_validates_every_component() -> None:
    with pytest.raises(ValueError):
        identity(xpath_version="4.0")
    with pytest.raises(ValueError):
        identity(xslt_version="4.0")
    with pytest.raises(TypeError):
        identity(namespaces=[])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        identity(namespaces={"": "urn:empty"})
    with pytest.raises(TypeError):
        identity(namespaces={"my": 1})  # type: ignore[dict-item]
    with pytest.raises(TypeError):
        identity(variable_types={"amount": ""})
    with pytest.raises(TypeError):
        identity(variable_types={"amount": 1})  # type: ignore[dict-item]
    with pytest.raises(TypeError):
        identity(functions={"": 1})
    with pytest.raises(ValueError):
        identity(functions={"twice": 1})
    with pytest.raises(TypeError):
        identity(functions={"{urn:my}twice": True})
    with pytest.raises(ValueError):
        identity(functions={"{urn:my}twice": -1})
    with pytest.raises(ValueError):
        identity(functions={"{urn:my}twice": (2, 1)})
    with pytest.raises(ValueError):
        identity(functions={"{urn:my}twice": (1, 2, 3)})  # type: ignore[dict-item]
    with pytest.raises(TypeError):
        identity(default_namespace=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        identity(schema_aware="yes")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        identity(schema=object())  # type: ignore[arg-type]


def test_identity_accepts_lexical_function_names_and_range_arities() -> None:
    lexical = identity(functions={"my:twice": (1, 3)})
    assert lexical.functions["my:twice"] == (1, 3)
    assert lexical.xpath_static_context().functions["my:twice"] == (1, 3)
    assert isinstance(lexical.key(), tuple)

    unbound = identity(
        functions={"{urn:missing}twice": 1},
        namespaces={"my": MY_NS, "xs": XSD_NS},
    )
    assert "{urn:missing}twice" not in unbound.xpath_static_context().functions


def test_compile_cache_key_validates_its_inputs() -> None:
    with pytest.raises(TypeError):
        compile_cache_key(1, identity())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compile_cache_key("1 + 1", object())  # type: ignore[arg-type]


def test_cache_validation_properties_and_clear() -> None:
    with pytest.raises(TypeError):
        DynamicExpressionCache(max_entries=True)  # type: ignore[arg-type]
    cache = DynamicExpressionCache(max_entries=2)
    assert cache.max_entries == 2
    cache.compile("1 + 1", identity())
    assert len(cache) == 1
    cache.clear()
    assert len(cache) == 0


def test_expanded_qname_requires_a_qname_local_name() -> None:
    from types import SimpleNamespace

    from pyxsd.xslt.runtime.evaluate import _expanded_qname

    fake = SimpleNamespace(
        type_name="{http://www.w3.org/2001/XMLSchema}QName",
        value=SimpleNamespace(local_name=None),
    )
    assert _expanded_qname(fake) is None  # type: ignore[arg-type]


def test_evaluate_dynamic_validates_its_inputs() -> None:
    cache = DynamicExpressionCache()
    with pytest.raises(TypeError):
        evaluate_dynamic("1", identity(), object(), focus=Focus.absent(), cache=cache)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        evaluate_dynamic("1", identity(), ExpressionEnvironment(), focus=object(), cache=cache)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        evaluate_dynamic(
            "1", identity(), ExpressionEnvironment(), focus=Focus.absent(), cache=object()
        )  # type: ignore[arg-type]


def test_lexical_variable_type_satisfies_a_declared_expanded_reference() -> None:
    lexical = identity(variable_types={"my:amount": "xs:integer"})
    compiled_expression = DynamicExpressionCache().compile(
        "$my:amount",
        lexical,
    )
    assert compiled_expression.variable_names == frozenset({"{urn:my}amount"})


# ---------------------------------------------------------------------------
# with-params map extraction


def test_with_params_requires_a_single_qname_keyed_map() -> None:
    key = as_xdm(QName(MY_NS, "amount"))[0]
    mapped = XdmMap(((key, as_xdm(41)),))
    assert with_params_variables((mapped,)) == {"{urn:my}amount": (XdmAtomic.integer(41),)}

    for value in (
        (),
        (XdmAtomic.integer(1),),
        (mapped, mapped),
    ):
        with pytest.raises(XsltDynamicError) as caught:
            with_params_variables(value)
        assert caught.value.code == "XTTE3165"

    plain = XdmMap(((XdmAtomic.string("amount"), as_xdm(1)),))
    with pytest.raises(XsltDynamicError) as caught:
        with_params_variables((plain,))
    assert caught.value.code == "XTTE3165"


# ---------------------------------------------------------------------------
# Instruction behavior


def test_dynamic_evaluation_uses_restricted_context() -> None:
    source = stylesheet(
        """
        <xsl:param name="amount" as="xs:integer" select="0"/>
        <xsl:param name="expression" as="xs:string" required="yes"/>
        <xsl:template name="main">
          <xsl:evaluate xpath="$expression">
            <xsl:with-param name="amount" select="$amount"/>
          </xsl:evaluate>
        </xsl:template>
        """
    )
    result = transform(
        source,
        initial_template="main",
        parameters={"expression": "$amount + 1", "amount": 41},
    )
    assert result.principal == (XdmAtomic.integer(42),)


def test_hidden_variables_are_not_in_scope() -> None:
    local = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="secret" select="'hidden'"/>
          <xsl:evaluate xpath="'$secret'"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltDynamicError, match="XTDE3160"):
        transform(local, initial_template="main")

    private = stylesheet(
        """
        <xsl:variable name="secret" select="'hidden'" visibility="private"/>
        <xsl:template name="main">
          <xsl:evaluate xpath="'$secret'"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltDynamicError, match="XTDE3160"):
        transform(private, initial_template="main")

    exposed = stylesheet(
        """
        <xsl:variable name="secret" select="'hidden'"/>
        <xsl:template name="main">
          <xsl:evaluate xpath="'$secret'">
            <xsl:with-param name="secret" select="$secret"/>
          </xsl:evaluate>
        </xsl:template>
        """
    )
    assert transform(exposed, initial_template="main").principal == (XdmAtomic.string("hidden"),)


def test_context_item_focus_is_explicit_transferred_or_absent() -> None:
    present = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'xs:integer(.) + 1'" context-item="/*/n"/>
        </xsl:template>
        """
    )
    result = transform(present, b"<doc><n>41</n></doc>", initial_template="main")
    assert result.principal == (XdmAtomic.integer(42),)

    exhausted = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'1 + 1'" context-item="()"/>
        </xsl:template>
        """
    )
    assert transform(exhausted, initial_template="main").principal == (XdmAtomic.integer(2),)

    absent = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'1 + 1'"/>
          <xsl:evaluate xpath="'. + 1'"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(absent, initial_template="main")) == "XPDY0002"

    too_many = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'1'" context-item="/*/n"/>
        </xsl:template>
        """
    )
    assert (
        error_code(
            lambda: transform(too_many, b"<doc><n>1</n><n>2</n></doc>", initial_template="main")
        )
        == "XTTE3210"
    )


def test_context_item_may_be_an_atomic_or_map_item() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'. + 1'" context-item="42"/>
          <xsl:evaluate xpath="'map:get(., &quot;k&quot;)'" context-item="map{'k': 'value'}"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.integer(43),
        XdmAtomic.string("value"),
    )


def test_with_params_map_overrides_with_param_children() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'$amount + 1'" with-params="map:entry(xs:QName('amount'), 41)">
            <xsl:with-param name="amount" select="1"/>
          </xsl:evaluate>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.integer(42),)


def test_with_params_namespaced_variable_and_static_type() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'$my:amount + 1'"
                        with-params="map:entry(QName('urn:my', 'amount'), 41)"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.integer(42),)


def test_with_params_value_must_be_a_single_qname_map() -> None:
    wrong_type = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'1'" with-params="'not a map'"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(wrong_type, initial_template="main")) == "XTTE3165"

    wrong_key = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'1'" with-params="map:entry('amount', 41)"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(wrong_key, initial_template="main")) == "XTTE3165"


def test_with_param_values_are_coerced_and_typed() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'$amount instance of xs:integer'">
            <xsl:with-param name="amount" as="xs:integer" select="'41'"/>
          </xsl:evaluate>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.boolean(True),)

    invalid = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'1'">
            <xsl:with-param name="amount" as="xs:integer" select="'not a number'"/>
          </xsl:evaluate>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(invalid, initial_template="main")) == "XTTE0590"


def test_instruction_namespaces_define_the_target_static_context() -> None:
    source = stylesheet(
        """
        <xsl:function name="my:twice" as="xs:integer" visibility="public">
          <xsl:param name="value" as="xs:integer"/>
          <xsl:sequence select="$value * 2"/>
        </xsl:function>
        <xsl:template name="main">
          <xsl:evaluate xpath="'my:twice(2)'"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.integer(4),)


def test_dynamic_expression_local_bindings_are_not_hidden_variables() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'for $x in 1 to 2 return $x * 2'"/>
          <xsl:evaluate xpath="'let $x := 3 return $x + 1'"/>
          <xsl:evaluate xpath="'some $x in (1, 2) satisfies $x = 2'"/>
          <xsl:evaluate xpath="'function($x as xs:integer) as xs:integer { $x + 1 }(41)'"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.integer(2),
        XdmAtomic.integer(4),
        XdmAtomic.integer(4),
        XdmAtomic.boolean(True),
        XdmAtomic.integer(42),
    )


def test_private_and_unknown_functions_are_unavailable() -> None:
    private = stylesheet(
        """
        <xsl:function name="my:secret" as="xs:integer" visibility="private">
          <xsl:sequence select="1"/>
        </xsl:function>
        <xsl:template name="main">
          <xsl:evaluate xpath="'my:secret()'"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltDynamicError, match="XTDE3160"):
        transform(private, initial_template="main")

    unknown = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'system-property(&quot;xsl:version&quot;)'"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltDynamicError, match="XTDE3160"):
        transform(unknown, initial_template="main")

    key_function = stylesheet(
        """
        <xsl:key name="k" match="n" use="."/>
        <xsl:template name="main">
          <xsl:evaluate xpath="'key(&quot;k&quot;, &quot;1&quot;)'" context-item="/"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltDynamicError, match="XTDE3160"):
        transform(key_function, b"<doc><n>1</n></doc>", initial_template="main")


def test_unregistered_python_capability_is_not_available() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'py:evil()'"/>
        </xsl:template>
        """,
        namespaces=' xmlns:py="urn:python"',
    )
    with pytest.raises(XsltDynamicError, match="XTDE3160"):
        transform(source, initial_template="main")


def test_namespace_context_supplies_the_static_namespaces() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'namespace-uri(/*/*/n:item)'"
                        context-item="/"
                        namespace-context="/*/ns"/>
        </xsl:template>
        """
    )
    result = transform(
        source,
        b'<doc><ns xmlns:n="urn:n"><n:item/></ns></doc>',
        initial_template="main",
    )
    assert result.principal == (XdmAtomic.string("urn:n"),)


def test_namespace_context_must_be_a_single_node() -> None:
    for expression in ("'not a node'", "/*/ns", "()"):
        source = stylesheet(
            f"""
            <xsl:template name="main">
              <xsl:evaluate xpath="'1'" namespace-context="{expression}"/>
            </xsl:template>
            """
        )
        with pytest.raises(XsltDynamicError) as caught:
            transform(source, b"<doc><ns/><ns/></doc>", initial_template="main")
        assert caught.value.code == "XTTE3170"


def test_xpath_default_namespace_applies_to_the_target_expression() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'namespace-uri(/*)'" context-item="/"
                        xpath-default-namespace="urn:def"/>
        </xsl:template>
        """
    )
    result = transform(source, b'<root xmlns="urn:def"/>', initial_template="main")
    assert result.principal == (XdmAtomic.string("urn:def"),)


def test_base_uri_is_an_attribute_value_template() -> None:
    source = stylesheet(
        """
        <xsl:param name="base" select="'http://example.com/base/'"/>
        <xsl:template name="main">
          <xsl:evaluate xpath="'string(static-base-uri())'" base-uri="{$base}"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.string("http://example.com/base/"),
    )


def test_xpath_version_selects_the_target_parser() -> None:
    supported = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'array:size([1, 2])'" xpath-version="3.1"/>
        </xsl:template>
        """
    )
    assert transform(supported, initial_template="main").principal == (XdmAtomic.integer(2),)

    rejected = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'array:size([1, 2])'" xpath-version="3.0"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltDynamicError, match="XTDE3160"):
        transform(rejected, initial_template="main")

    invalid = stylesheet(
        """
        <xsl:template name="main"><xsl:evaluate xpath="'1'" xpath-version="4.0"/></xsl:template>
        """
    )
    assert error_code(lambda: compiled(invalid)) == "XTSE0020"


def test_as_attribute_applies_function_conversion() -> None:
    converted = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'1 + 1'" as="xs:string"/>
        </xsl:template>
        """
    )
    assert transform(converted, initial_template="main").principal == (XdmAtomic.string("2"),)

    invalid = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="&quot;'not a number'&quot;" as="xs:integer"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(invalid, initial_template="main")) == "XPTY0004"


def test_schema_awareness_controls_imported_constructor_functions() -> None:
    xsd = f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{MY_NS}"
        xmlns:my="{MY_NS}" elementFormDefault="qualified">
      <xs:simpleType name="Code">
        <xs:restriction base="xs:string"/>
      </xs:simpleType>
    </xs:schema>"""
    pool = SchemaPool.from_schema(Schema.compile(io.StringIO(xsd)))
    body = """
        <xsl:template name="main">
          <xsl:evaluate xpath="'my:Code(&quot;abc&quot;)'" schema-aware="yes"/>
        </xsl:template>
        """
    aware = stylesheet(body)
    result = compiled(aware, schema=pool).transform(initial_template="main", delivery="raw")
    assert len(result.principal) == 1
    assert isinstance(result.principal[0], XdmAtomic)
    assert result.principal[0].type_name == f"{{{MY_NS}}}Code"
    assert result.principal[0].lexical == "abc"

    plain = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'my:Code(&quot;abc&quot;)'" schema-aware="no"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltDynamicError, match="XTDE3160"):
        compiled(plain, schema=pool).transform(initial_template="main", delivery="raw")

    # Absent schema-aware means the built-in types, never the imported schema.
    defaulted = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'my:Code(&quot;abc&quot;)'"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltDynamicError, match="XTDE3160"):
        compiled(defaulted, schema=pool).transform(initial_template="main", delivery="raw")


def test_dynamic_resources_do_not_exceed_the_transformation() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'doc(&quot;file:///etc/hosts&quot;)'"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltDynamicError) as caught:
        transform(source, initial_template="main")
    assert caught.value.code == "FODC0002"


def test_same_expression_text_in_distinct_static_contexts_is_never_shared() -> None:
    source = stylesheet(
        """
        <xsl:function name="my:f" as="xs:string" visibility="public">
          <xsl:sequence select="'my'"/>
        </xsl:function>
        <xsl:function name="other:f" as="xs:string" visibility="public">
          <xsl:sequence select="'other'"/>
        </xsl:function>
        <xsl:template name="main">
          <xsl:evaluate xpath="'p:f()'" xmlns:p="urn:my"/>
          <xsl:evaluate xpath="'p:f()'" xmlns:p="urn:other"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.string("my"),
        XdmAtomic.string("other"),
    )


def test_public_function_overloads_form_one_dynamic_signature_range() -> None:
    source = stylesheet(
        """
        <xsl:function name="my:f" as="xs:integer" visibility="public">
          <xsl:sequence select="1"/>
        </xsl:function>
        <xsl:function name="my:f" as="xs:integer" visibility="public">
          <xsl:param name="value" as="xs:integer"/>
          <xsl:sequence select="$value + 1"/>
        </xsl:function>
        <xsl:template name="main">
          <xsl:evaluate xpath="'my:f(41)'"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.integer(42),)


def test_public_function_without_an_in_scope_prefix_is_not_registered() -> None:
    source = stylesheet(
        """
        <xsl:function name="unbound:f" as="xs:integer" visibility="public"
                      xmlns:unbound="urn:unbound">
          <xsl:sequence select="1"/>
        </xsl:function>
        <xsl:template name="main">
          <xsl:evaluate xpath="'1'"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.integer(1),)


def test_evaluate_content_is_limited_to_parameters_and_fallback() -> None:
    significant = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'1'">text</xsl:evaluate>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(significant)) == "XTSE0010"

    whitespace = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'1'">   </xsl:evaluate>
        </xsl:template>
        """
    )
    assert transform(whitespace, initial_template="main").principal == (XdmAtomic.integer(1),)

    missing_name = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'1'">
            <xsl:with-param select="1"/>
          </xsl:evaluate>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(missing_name)) == "XTSE0010"

    select_and_content = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'1'">
            <xsl:with-param name="value" select="1">text</xsl:with-param>
          </xsl:evaluate>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(select_and_content)) == "XTSE0620"


def test_invalid_static_options_are_rejected_by_the_instruction_that_uses_them() -> None:
    assertion = stylesheet(
        """
        <xsl:template name="main"><xsl:assert test="true()"/></xsl:template>
        """
    )
    with pytest.raises(TypeError):
        compiled(assertion, static_options={"assertions": "yes"})

    evaluation = stylesheet(
        """
        <xsl:template name="main"><xsl:evaluate xpath="'1'"/></xsl:template>
        """
    )
    with pytest.raises(TypeError):
        compiled(evaluation, static_options={"dynamic-evaluation": "yes"})


def test_xpath_version_cannot_exceed_the_processor_profile() -> None:
    from pyxsd.xslt.compiler.context import ProfileOptions, StaticContext
    from pyxsd.xslt.compiler.instructions import Attribute, Instruction
    from pyxsd.xslt.processor import _dynamic_xpath_version

    context = StaticContext(profile=ProfileOptions(processor_profile="xslt-2.0"))
    instruction = Instruction(
        name="{http://www.w3.org/1999/XSL/Transform}evaluate",
        attributes=(Attribute("xpath-version", "3.1"),),
    )
    with pytest.raises(XsltError) as caught:
        _dynamic_xpath_version(instruction, context)
    assert caught.value.code == "XTSE0020"


def test_with_param_value_may_come_from_its_sequence_constructor() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'$amount + 1'">
            <xsl:with-param name="amount"><xsl:sequence select="41"/></xsl:with-param>
          </xsl:evaluate>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.integer(42),)


def test_invalid_schema_aware_value_is_a_dynamic_error() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'1'" schema-aware="'maybe'"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(source, initial_template="main")) == "XTDE0030"


def test_namespace_context_supplies_a_default_element_namespace() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'namespace-uri(/*/*/item)'"
                        context-item="/"
                        namespace-context="/*/*"/>
        </xsl:template>
        """
    )
    result = transform(
        source,
        b'<doc><ns xmlns="urn:def"><item/></ns></doc>',
        initial_template="main",
    )
    assert result.principal == (XdmAtomic.string("urn:def"),)


def test_forwards_compatibility_requires_a_decimal_version() -> None:
    from pyxsd.xslt.processor import _forwards_compatible

    assert _forwards_compatible("4.0") is True
    assert _forwards_compatible("3.0") is False
    assert _forwards_compatible("not-a-version") is False


def test_engine_xslt_function_switch_is_typed_and_readable() -> None:
    from pyxsd.xpath.engine import XPathEngine

    assert XPathEngine().xslt_functions is True
    assert XPathEngine(xslt_functions=False).xslt_functions is False
    with pytest.raises(TypeError):
        XPathEngine(xslt_functions=1)  # type: ignore[arg-type]


def test_namespace_context_can_make_a_public_function_callable() -> None:
    source = stylesheet(
        """
        <xsl:function name="late:f" as="xs:string" visibility="public"
                      xmlns:late="urn:late">
          <xsl:sequence select="'late'"/>
        </xsl:function>
        <xsl:template name="main">
          <xsl:evaluate xpath="'late:f()'"
                        context-item="/"
                        namespace-context="/*/ns"/>
        </xsl:template>
        """
    )
    result = transform(
        source,
        b'<doc><ns xmlns:late="urn:late"/></doc>',
        initial_template="main",
    )
    assert result.principal == (XdmAtomic.string("late"),)


def test_dynamic_function_declarations_accept_lexical_names() -> None:
    from pyxsd.xslt.compiler.context import StaticContext, StaticFunction
    from pyxsd.xslt.processor import _dynamic_function_declarations

    context = StaticContext(
        namespaces={"my": MY_NS},
        functions={
            ("my:f", 1): StaticFunction(name="my:f", arity=1, visibility="public"),
            ("plain", 0): StaticFunction(name="plain", arity=0, visibility="public"),
        },
    )
    declarations = _dynamic_function_declarations(context)
    assert declarations == {"my:f": 1}


def test_dynamic_variable_names_leave_unbound_names_expanded() -> None:
    from pyxsd.xslt.processor import _dynamic_variable_names

    values = {"{urn:unbound}value": (XdmAtomic.integer(1),)}
    assert _dynamic_variable_names(values, {"my": MY_NS}) == values


def test_runtime_feature_switch_reads_the_processor_option() -> None:
    from types import SimpleNamespace

    from pyxsd.xslt.runtime.functions import XsltContextBridge

    absent = XsltContextBridge(
        SimpleNamespace(stylesheet=SimpleNamespace(processor=SimpleNamespace(static_options=[])))
    )  # type: ignore[arg-type]
    assert absent.system_property("Q{http://www.w3.org/1999/XSL/Transform}version") == "3.0"
    assert (
        absent.system_property("Q{http://www.w3.org/1999/XSL/Transform}supports-dynamic-evaluation")
        == "yes"
    )

    invalid = XsltContextBridge(
        SimpleNamespace(
            stylesheet=SimpleNamespace(
                processor=SimpleNamespace(static_options={"dynamic-evaluation": "yes"})
            )
        )
    )  # type: ignore[arg-type]
    with pytest.raises(XsltDynamicError) as caught:
        invalid.system_property(
            "Q{http://www.w3.org/1999/XSL/Transform}supports-dynamic-evaluation"
        )
    assert caught.value.code == "XTDE0030"


def test_disabled_dynamic_evaluation_uses_fallback_or_reports_xtde3175() -> None:
    fallback = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'1 + 1'">
            <xsl:fallback><xsl:sequence select="'fallback'"/></xsl:fallback>
          </xsl:evaluate>
        </xsl:template>
        """
    )
    result = compiled(fallback, static_options={"dynamic-evaluation": False}).transform(
        initial_template="main", delivery="raw"
    )
    assert result.principal == (XdmAtomic.string("fallback"),)

    missing = stylesheet(
        """
        <xsl:template name="main"><xsl:evaluate xpath="'1 + 1'"/></xsl:template>
        """
    )
    with pytest.raises(XsltDynamicError) as caught:
        compiled(missing, static_options={"dynamic-evaluation": False}).transform(
            initial_template="main", delivery="raw"
        )
    assert caught.value.code == "XTDE3175"


def test_runtime_availability_reports_the_real_feature_profile() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="system-property('xsl:supports-dynamic-evaluation')"/>
          <xsl:sequence select="element-available('xsl:evaluate')"/>
          <xsl:sequence select="element-available('xsl:if')"/>
          <xsl:sequence select="element-available('xsl:map')"/>
          <xsl:sequence select="element-available('xsl:assert')"/>
          <xsl:sequence select="element-available('xsl:where-populated')"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.string("yes"),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
    )

    disabled = compiled(source, static_options={"dynamic-evaluation": False}).transform(
        initial_template="main", delivery="raw"
    )
    assert disabled.principal == (
        XdmAtomic.string("no"),
        XdmAtomic.boolean(False),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
    )


def test_missing_xpath_attribute_is_a_static_error() -> None:
    source = stylesheet(
        """
        <xsl:template name="main"><xsl:evaluate/></xsl:template>
        """
    )
    assert error_code(lambda: compiled(source)) == "XTSE0010"


def test_fallback_children_are_ignored_when_dynamic_evaluation_is_available() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'1 + 1'">
            <xsl:fallback><xsl:sequence select="'fallback'"/></xsl:fallback>
          </xsl:evaluate>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.integer(2),)


def test_only_with_param_and_fallback_are_permitted_content() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:evaluate xpath="'1'">
            <xsl:sequence select="1"/>
          </xsl:evaluate>
        </xsl:template>
        """
    )
    assert error_code(lambda: compiled(source)) == "XTSE0010"


def test_error_namespace_code_is_normalized_for_assertions_and_dynamic_errors() -> None:
    # Keeps the error-name helpers exercised without depending on a real catch.
    from pyxsd.xslt.errors import expanded_error_code

    assert expanded_error_code("err:XTMM9001") == f"{{{ERROR_NS}}}XTMM9001"
