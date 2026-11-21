"""Stylesheet-function coercion, keys, and stable generated identifiers."""

from __future__ import annotations

import io
from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest
from tests.xslt.support import (
    element_children,
)
from tests.xslt.support import (
    stylesheet as _stylesheet,
)
from tests.xslt.support import (
    transform as _transform,
)

from pyxsd.source_spans import SourceIndex
from pyxsd.xdm import XdmAtomic
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.compiler.packages import PackageResolver
from pyxsd.xslt.errors import ERROR_NAMESPACE, XsltError, XsltStaticError
from pyxsd.xslt.processor import _ABSENT, _document_nodes, _Invocation, _prepare_invocation

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
MY_NS = "urn:my"

stylesheet = partial(_stylesheet, namespaces={"my": MY_NS})
transform = partial(_transform, delivery="raw")


def error_code(call: Callable[[], object]) -> str | None:
    with pytest.raises(XsltError) as caught:
        call()
    return caught.value.code


def test_function_argument_and_result_conversion() -> None:
    converted = stylesheet(
        """
        <xsl:function name="my:f" as="xs:integer">
          <xsl:param name="value" as="xs:integer"/>
          <xsl:sequence select="$value + 1"/>
        </xsl:function>
        <xsl:template name="main">
          <xsl:sequence select="my:f('41') instance of xs:integer"/>
          <xsl:sequence select="my:f(41)"/>
        </xsl:template>
        """
    )
    result = transform(converted, initial_template="main")
    assert result.principal == (XdmAtomic.boolean(True), XdmAtomic.integer(42))

    result_conversion = stylesheet(
        """
        <xsl:function name="my:answer" as="xs:string">
          <xsl:sequence select="42"/>
        </xsl:function>
        <xsl:template name="main">
          <xsl:sequence select="my:answer() instance of xs:string"/>
        </xsl:template>
        """
    )
    assert transform(result_conversion, initial_template="main").principal == (
        XdmAtomic.boolean(True),
    )


def test_function_argument_and_result_type_errors() -> None:
    argument = stylesheet(
        """
        <xsl:function name="my:f" as="xs:integer">
          <xsl:param name="value" as="xs:integer"/>
          <xsl:sequence select="$value"/>
        </xsl:function>
        <xsl:template name="main"><xsl:sequence select="my:f('not-an-integer')"/></xsl:template>
        """
    )
    assert error_code(lambda: transform(argument, initial_template="main")) == "XTTE0790"

    result = stylesheet(
        """
        <xsl:function name="my:f" as="xs:integer">
          <xsl:param name="value"/>
          <xsl:sequence select="'not-an-integer'"/>
        </xsl:function>
        <xsl:template name="main"><xsl:sequence select="my:f(1)"/></xsl:template>
        """
    )
    assert error_code(lambda: transform(result, initial_template="main")) == "XTTE0780"


def test_function_overloads_and_initial_function_entry() -> None:
    source = stylesheet(
        """
        <xsl:function name="my:over" as="xs:string" visibility="public">
          <xsl:param name="a"/>
          <xsl:sequence select="'one'"/>
        </xsl:function>
        <xsl:function name="my:over" as="xs:string" visibility="public">
          <xsl:param name="a"/>
          <xsl:param name="b"/>
          <xsl:sequence select="'two'"/>
        </xsl:function>
        <xsl:template name="main">
          <xsl:sequence select="my:over(1), my:over(1, 2)"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.string("one"),
        XdmAtomic.string("two"),
    )
    assert transform(
        source,
        initial_function="my:over",
        initial_function_arguments=(1, 2),
    ).principal == (XdmAtomic.string("two"),)


def test_generate_id_is_stable_and_unique_within_one_invocation() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence
            select="generate-id(/*), generate-id(/*),
                    generate-id(/*/item[1]), generate-id(/*/item[2]),
                    generate-id(())"/>
        </xsl:template>
        """
    )
    result = transform(source, b"<doc><item/><item/></doc>", initial_template="main")
    identifiers = [item.lexical for item in result.principal]
    assert identifiers[0] == identifiers[1]
    assert identifiers[2] != identifiers[3]
    assert identifiers[4] == ""
    assert all(value.startswith("ID") and value[2:].isdigit() for value in identifiers if value)

    across_expressions = stylesheet(
        """
        <xsl:template match="/">
          <xsl:value-of select="generate-id(/*/item)"/>
          <xsl:text>|</xsl:text>
          <xsl:value-of select="generate-id(/*/item)"/>
          <xsl:text>|</xsl:text>
          <xsl:value-of select="generate-id()"/>
        </xsl:template>
        """
    )
    tree = transform(across_expressions, b"<doc><item/></doc>", delivery="document")
    first, second, context = tree.principal_tree.string_value.split("|")
    assert first == second
    assert context.startswith("ID")


def test_generate_id_rejects_non_node_arguments() -> None:
    source = stylesheet(
        """
        <xsl:template name="main"><xsl:sequence select="generate-id(1)"/></xsl:template>
        """
    )
    with pytest.raises(XsltError) as caught:
        transform(source, initial_template="main")
    assert caught.value.code == "XPTY0004"


def test_key_index_is_lazy_until_the_key_function_is_used() -> None:
    unused = stylesheet(
        """
        <xsl:key name="k" match="item" use="error()"/>
        <xsl:template name="main"><out/></xsl:template>
        """
    )
    assert transform(unused, b"<doc><item/></doc>", initial_template="main").principal

    used = stylesheet(
        """
        <xsl:key name="k" match="item" use="error()"/>
        <xsl:template name="main">
          <xsl:sequence select="count(key('k', 'x'))"/>
        </xsl:template>
        """
    )
    with pytest.raises(Exception) as caught:
        transform(used, b"<doc><item/></doc>", initial_template="main")
    assert (
        str(getattr(caught.value, "code", "")).endswith("FOER0000")
        or getattr(caught.value, "code", None) is None
    )


def test_key_index_returns_document_order_and_supports_value_sequences() -> None:
    source = stylesheet(
        """
        <xsl:key name="k" match="item" use="@id"/>
        <xsl:template name="main">
          <xsl:sequence
            select="string-join(for $node in key('k', 'a') return string($node), ','),
                    string-join(for $node in key('k', ('a', 'b')) return string($node), ','),
                    count(key('k', 'missing'))"/>
        </xsl:template>
        """
    )
    result = transform(
        source,
        b'<doc><item id="a">one</item><item id="b">two</item><item id="a">three</item></doc>',
        initial_template="main",
    )
    assert result.principal == (
        XdmAtomic.string("one,three"),
        XdmAtomic.string("one,two,three"),
        XdmAtomic.integer(0),
    )


def test_key_index_accepts_untyped_and_numeric_lookups() -> None:
    source = stylesheet(
        """
        <xsl:key name="k" match="item" use="@n"/>
        <xsl:template name="main">
          <xsl:sequence select="count(key('k', 1)), count(key('k', '1'))"/>
        </xsl:template>
        """
    )
    assert transform(source, b'<doc><item n="1"/></doc>', initial_template="main").principal == (
        XdmAtomic.integer(1),
        XdmAtomic.integer(1),
    )


def test_key_names_are_expanded_and_matches_can_be_attributes() -> None:
    source = stylesheet(
        """
        <xsl:key name="my:k" match="@code" use="."/>
        <xsl:template name="main">
          <xsl:sequence select="count(key('my:k', 'x'))"/>
        </xsl:template>
        """
    )
    assert transform(source, b'<doc><item code="x"/></doc>', initial_template="main").principal == (
        XdmAtomic.integer(1),
    )


def test_key_declaration_requires_names_and_expressions_at_compile_time() -> None:
    missing_use = stylesheet('<xsl:key name="k" match="item"/>')
    with pytest.raises(XsltStaticError):
        XsltProcessor().compile(io.BytesIO(missing_use))


def test_generate_id_argument_and_context_error_arms() -> None:
    duplicate = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="generate-id((/*, /*))"/>
        </xsl:template>
        """
    )
    assert (
        error_code(lambda: transform(duplicate, b"<doc/>", initial_template="main")) == "XPTY0004"
    )

    absent = stylesheet(
        """
        <xsl:template name="main"><xsl:sequence select="generate-id()"/></xsl:template>
        """
    )
    assert error_code(lambda: transform(absent, initial_template="main")) == "XPDY0002"

    non_node = stylesheet(
        """
        <xsl:template name="main"><xsl:sequence select="generate-id()"/></xsl:template>
        """
    )
    assert (
        error_code(
            lambda: transform(
                non_node,
                initial_template="main",
                global_context_item=XdmAtomic.integer(1),
            )
        )
        == "XPTY0004"
    )


def test_key_error_arms_composite_keys_and_function_values() -> None:
    unknown = stylesheet(
        """
        <xsl:template name="main"><xsl:sequence select="count(key('missing', 'x'))"/></xsl:template>
        """
    )
    assert error_code(lambda: transform(unknown, initial_template="main")) == "XTDE1260"

    bad_name = stylesheet(
        """
        <xsl:key name="k" match="item" use="@id"/>
        <xsl:template name="main"><xsl:sequence select="count(key(/*, 'x'))"/></xsl:template>
        """
    )
    assert error_code(lambda: transform(bad_name, b"<doc/>", initial_template="main")) == "XPTY0004"

    no_document = stylesheet(
        """
        <xsl:key name="k" match="item" use="@id"/>
        <xsl:template name="main"><xsl:sequence select="count(key('k', 'x'))"/></xsl:template>
        """
    )
    assert error_code(lambda: transform(no_document, initial_template="main")) == "XTDE1270"

    global_document = stylesheet(
        """
        <xsl:key name="k" match="item" use="@id"/>
        <xsl:function name="my:count-key" as="xs:integer">
          <xsl:sequence select="count(key('k', 'x'))"/>
        </xsl:function>
        <xsl:template name="main"><xsl:sequence select="my:count-key()"/></xsl:template>
        """
    )
    assert (
        error_code(
            lambda: transform(
                global_document,
                b'<doc><item id="x"/></doc>',
                initial_template="main",
            )
        )
        == "XTDE1270"
    )

    composite = stylesheet(
        """
        <xsl:key name="ck" match="item" use="(@a, @b)" composite="yes"/>
        <xsl:template name="main">
          <xsl:sequence select="count(key('ck', ('1', '2')))"/>
        </xsl:template>
        """
    )
    assert transform(
        composite,
        b'<doc><item a="1" b="2"/><item a="1" b="3"/></doc>',
        initial_template="main",
    ).principal == (XdmAtomic.integer(1),)

    function_value = stylesheet(
        """
        <xsl:key name="k" match="item" use="@id"/>
        <xsl:template name="main">
          <xsl:sequence select="count(key('k', upper-case#1))"/>
        </xsl:template>
        """
    )
    assert (
        error_code(
            lambda: transform(
                function_value,
                b'<doc><item id="x"/></doc>',
                initial_template="main",
            )
        )
        == "FOTY0013"
    )


def test_a_self_referential_key_definition_raises_XTDE0640() -> None:
    source = stylesheet(
        """
        <xsl:key name="k" match="item" use="key('k', @id)"/>
        <xsl:template name="main">
          <xsl:sequence select="count(key('k', 'a'))"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltError) as caught:
        transform(source, b'<doc><item id="a"/></doc>', initial_template="main")
    assert caught.value.code == "XTDE0640"
    assert "'k'" in str(caught.value)


def test_mutually_recursive_key_definitions_raise_XTDE0640() -> None:
    source = stylesheet(
        """
        <xsl:key name="k1" match="item" use="key('k2', @id)"/>
        <xsl:key name="k2" match="item" use="key('k1', @id)"/>
        <xsl:template name="main">
          <xsl:sequence select="count(key('k1', 'a'))"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltError) as caught:
        transform(source, b'<doc><item id="a"/></doc>', initial_template="main")
    assert caught.value.code == "XTDE0640"
    # The lookup starts from k1, so k1 is the definition that closes the cycle.
    assert "'k1'" in str(caught.value)


def test_a_non_circular_key_chain_still_evaluates() -> None:
    source = stylesheet(
        """
        <xsl:key name="k2" match="item" use="@code"/>
        <xsl:key name="k1" match="item" use="concat('p', key('k2', @code)/@code)"/>
        <xsl:template name="main">
          <xsl:sequence select="string(key('k1', 'px')/@id), count(key('k1', 'pz'))"/>
        </xsl:template>
        """
    )
    result = transform(
        source,
        b'<doc><item id="a" code="x"/><item id="b" code="y"/></doc>',
        initial_template="main",
    )
    assert result.principal == (XdmAtomic.string("a"), XdmAtomic.integer(0))


def test_a_second_lookup_reuses_the_completed_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builds: list[str] = []
    original = _Invocation._key_index

    def counting(self, definitions, document, package_name):
        key = (id(document), package_name, definitions[0].name)
        cached = self.key_indexes.get(key)
        if cached is None or self.key_index_documents.get(id(document)) is not document:
            builds.append(definitions[0].name)
        return original(self, definitions, document, package_name)

    monkeypatch.setattr(_Invocation, "_key_index", counting)
    source = stylesheet(
        """
        <xsl:key name="k" match="item" use="@id"/>
        <xsl:template name="main">
          <xsl:sequence select="count(key('k', 'a')), count(key('k', 'b'))"/>
        </xsl:template>
        """
    )
    result = transform(
        source,
        b'<doc><item id="a"/><item id="b"/></doc>',
        initial_template="main",
    )
    assert result.principal == (XdmAtomic.integer(1), XdmAtomic.integer(1))
    assert builds == ["k"]


def test_the_same_key_against_another_document_is_not_circular() -> None:
    source = stylesheet(
        """
        <xsl:variable name="other" as="document-node()">
          <xsl:document><x id="a"/></xsl:document>
        </xsl:variable>
        <xsl:key name="k" match="item | x"
                 use="if (exists(self::x)) then @id else key('k', @id, $other)/@id"/>
        <xsl:template name="main">
          <xsl:sequence select="local-name(key('k', 'a')), local-name(key('k', 'a', $other))"/>
        </xsl:template>
        """
    )
    result = transform(source, b'<doc><item id="a"/></doc>', initial_template="main")
    assert result.principal == (XdmAtomic.string("item"), XdmAtomic.string("x"))


def test_a_pattern_key_lookup_selects_the_keyed_nodes() -> None:
    source = stylesheet(
        """
        <xsl:key name="k" match="b" use="@code"/>
        <xsl:template match="/">
          <out><xsl:apply-templates select="doc/a"/></out>
        </xsl:template>
        <xsl:template match="a[key('k', @code)]"><hit n="{@code}"/></xsl:template>
        <xsl:template match="a"><miss n="{@code}"/></xsl:template>
        """
    )
    result = transform(source, b'<doc><a code="x"/><a code="z"/><b code="x"/><b code="w"/></doc>')
    (out,) = result.principal
    assert [
        (
            child.local_name,
            [(attribute.name, attribute.string_value) for attribute in child.attributes],
        )
        for child in element_children(out)
    ] == [
        ("hit", [("n", "x")]),
        ("miss", [("n", "z")]),
    ]


def test_a_rooted_key_pattern_selects_the_keyed_nodes() -> None:
    source = stylesheet(
        """
        <xsl:key name="k1" match="b" use="@code"/>
        <xsl:key name="k2" match="key('k1', 'x')" use="@id"/>
        <xsl:template match="/">
          <out n="{count(key('k2', 'id8'))}" code="{key('k2', 'id8')/@code}"/>
        </xsl:template>
        """
    )
    result = transform(source, b'<doc><b id="id8" code="x"/><b id="id9" code="y"/></doc>')
    (out,) = result.principal
    assert [(attribute.name, attribute.string_value) for attribute in out.attributes] == [
        ("n", "1"),
        ("code", "x"),
    ]


def test_a_pattern_key_lookup_uses_the_third_argument_document() -> None:
    source = stylesheet(
        """
        <xsl:variable name="other" as="document-node()">
          <xsl:document><b id="id8" code="x"/></xsl:document>
        </xsl:variable>
        <xsl:key name="k1" match="b" use="@code"/>
        <xsl:key name="k2" match="key('k1', 'x', $other)" use="@id"/>
        <xsl:template name="main">
          <xsl:sequence select="count(key('k2', 'id8', $other))"/>
        </xsl:template>
        """
    )
    result = transform(source, b'<doc><b id="id8" code="z"/></doc>', initial_template="main")
    assert result.principal == (XdmAtomic.integer(1),)


def test_a_circular_key_inside_a_pattern_predicate_raises_XTDE0640() -> None:
    source = stylesheet(
        """
        <xsl:key name="k" match="item[key('k', @id)]" use="@id"/>
        <xsl:template name="main">
          <xsl:sequence select="count(key('k', 'a'))"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltError) as caught:
        transform(source, b'<doc><item id="a"/></doc>', initial_template="main")
    assert caught.value.code == "XTDE0640"


def test_a_circular_key_in_a_rooted_pattern_raises_XTDE0640() -> None:
    source = stylesheet(
        """
        <xsl:key name="k" match="key('k', 'a')" use="@id"/>
        <xsl:template name="main">
          <xsl:sequence select="count(key('k', 'a'))"/>
        </xsl:template>
        """
    )
    with pytest.raises(XsltError) as caught:
        transform(source, b'<doc><item id="a"/></doc>', initial_template="main")
    assert caught.value.code == "XTDE0640"


def test_a_non_circular_pattern_error_is_still_a_non_match() -> None:
    source = stylesheet(
        """
        <xsl:key name="k" match="item[1 div 0]" use="@id"/>
        <xsl:template name="main">
          <xsl:sequence select="count(key('k', 'a'))"/>
        </xsl:template>
        """
    )
    result = transform(source, b'<doc><item id="a"/></doc>', initial_template="main")
    assert result.principal == (XdmAtomic.integer(0),)


def test_a_forged_circularity_code_from_a_message_is_masked() -> None:
    """Only engine-detected circularity overrides the pattern masking rule."""
    source = _stylesheet(
        """
        <xsl:key name="k" match="item[my:forge(.)]" use="@id"/>
        <xsl:function name="my:forge" as="xs:boolean">
          <xsl:param name="node"/>
          <xsl:message terminate="yes" error-code="err:XTDE0640">forged</xsl:message>
        </xsl:function>
        <xsl:template name="main">
          <xsl:sequence select="count(key('k', 'a'))"/>
        </xsl:template>
        """,
        namespaces={"my": MY_NS, "err": ERROR_NAMESPACE},
    )
    result = transform(source, b'<doc><item id="a"/></doc>', initial_template="main")
    # XSLT 3.0 section 5.5.4 masks a non-circular dynamic error even when it
    # carries the circularity code; the unbuilt index stays empty.
    assert result.principal == (XdmAtomic.integer(0),)


def test_a_forged_circularity_code_outside_a_pattern_is_catchable() -> None:
    source = _stylesheet(
        """
        <xsl:function name="my:forge" as="xs:boolean">
          <xsl:param name="node"/>
          <xsl:message terminate="yes" error-code="err:XTDE0640">forged</xsl:message>
        </xsl:function>
        <xsl:template name="main">
          <xsl:try>
            <xsl:sequence select="my:forge(.)"/>
            <xsl:catch><caught code="{$err:code}"/></xsl:catch>
          </xsl:try>
        </xsl:template>
        """,
        namespaces={"my": MY_NS, "err": ERROR_NAMESPACE},
    )
    result = transform(source, b"<doc/>", initial_template="main")
    (caught,) = result.principal
    assert caught.local_name == "caught"
    assert [(attribute.name, attribute.string_value) for attribute in caught.attributes] == [
        ("code", "XTDE0640")
    ]


def _key_invocation(source: bytes) -> _Invocation:
    compiled = XsltProcessor().compile(io.BytesIO(source))
    options = _prepare_invocation(
        compiled,
        source=_ABSENT,
        parameters=None,
        global_context_item=_ABSENT,
        initial_match_selection=_ABSENT,
        template_parameters=None,
        tunnel_parameters=None,
        initial_template="main",
        initial_mode=None,
        initial_function=None,
        initial_function_arguments=(),
        output_uri=None,
        delivery="raw",
    )
    return _Invocation(compiled, options)


def test_a_failed_key_build_leaves_no_guard() -> None:
    failing = XsltProcessor().compile(
        io.BytesIO(
            stylesheet(
                """
                <xsl:key name="k" match="item" use="error()"/>
                <xsl:template name="main"><out/></xsl:template>
                """
            )
        )
    )
    invocation = _key_invocation(
        stylesheet(
            """
            <xsl:key name="k" match="item" use="@id"/>
            <xsl:template name="main"><out/></xsl:template>
            """
        )
    )
    document = XdmDocument.parse(b'<doc><item id="a"/></doc>')
    with pytest.raises(XsltError):
        invocation._key_index(failing._keys[None], document, None)
    index = invocation._key_index(invocation.stylesheet._keys[None], document, None)
    assert {node.local_name for node in index["a"]} == {"item"}
    assert invocation._key_builds == {}


def test_environment_function_overrides_are_validated_and_contextual() -> None:
    from pyxsd.xpath.engine import XPathEngine, _expanded_variable_name
    from pyxsd.xpath.environment import ExpressionEnvironment, XPathStaticContext

    parsed = XPathEngine("3.1").compile("if (false()) then $broken else '$not-a-reference'")
    assert parsed.variable_names == frozenset({"broken"})
    prefixed = XPathEngine("3.1").compile(
        "$my:value",
        XPathStaticContext(namespaces={"my": MY_NS}),
    )
    assert prefixed.variable_names == frozenset({f"{{{MY_NS}}}value"})
    eqname = XPathEngine("3.1").compile("$Q{urn:my}value")
    assert eqname.variable_names == frozenset({"{urn:my}value"})
    no_namespace = XPathEngine("3.1").compile("$Q{}value")
    assert no_namespace.variable_names == frozenset({"value"})
    static = XPathStaticContext(namespaces={"my": MY_NS})
    assert _expanded_variable_name("Q{urn:my}value", static) == "{urn:my}value"
    assert _expanded_variable_name("Q{}value", static) == "value"

    with pytest.raises(TypeError, match="function override"):
        ExpressionEnvironment(function_overrides={"fn:x": 1})  # type: ignore[dict-item]

    xpath2 = XPathEngine("2.0")
    compiled = xpath2.compile("1 + 1")
    environment = ExpressionEnvironment(
        function_overrides={
            "{http://www.w3.org/2005/xpath-functions}generate-id": (lambda item, arguments: ())
        }
    )
    assert xpath2.evaluate(compiled, environment) == (XdmAtomic.integer(2),)

    xpath31 = XPathEngine("3.1")
    generate = xpath31.compile("generate-id()")
    absent = ExpressionEnvironment(
        function_overrides={
            "{http://www.w3.org/2005/xpath-functions}generate-id": (
                lambda item, arguments: (XdmAtomic.string("fallback"),)
            )
        }
    )
    assert xpath31.evaluate(generate, absent) == (XdmAtomic.string("fallback"),)


def _package(name: str, body: str) -> str:
    return (
        f'<xsl:package name="{name}" package-version="1.0" '
        f'xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" xmlns:my="{MY_NS}" version="3.0">'
        f"{body}</xsl:package>"
    )


def test_private_library_functions_resolve_in_their_own_package(tmp_path: Path) -> None:
    library = tmp_path / "library.xsl"
    library.write_text(
        _package(
            "urn:library",
            """
            <xsl:mode name="my:m" visibility="public"/>
            <xsl:function name="my:secret" as="xs:string">
              <xsl:sequence select="'private'"/>
            </xsl:function>
            <xsl:template match="item" mode="my:m">
              <xsl:value-of select="my:secret()"/>
            </xsl:template>
            """,
        ),
        encoding="utf-8",
    )
    principal = tmp_path / "principal.xsl"
    principal.write_text(
        _package(
            "urn:principal",
            """
            <xsl:use-package name="urn:library" package-version="1.0">
              <xsl:accept component="mode" names="my:m" visibility="public"/>
            </xsl:use-package>
            <xsl:template name="xsl:initial-template" visibility="public">
              <out><xsl:apply-templates select="/*/item" mode="my:m"/></out>
            </xsl:template>
            """,
        ),
        encoding="utf-8",
    )
    compiled = XsltProcessor(resolver=PackageResolver.from_sources(library)).compile(principal)
    result = compiled.transform(
        source=b"<doc><item/></doc>",
        initial_template="xsl:initial-template",
        delivery="document",
    )
    assert result.principal_tree.string_value == "private"


def test_document_node_traversal_handles_an_empty_document() -> None:
    empty = XdmDocument(
        source_name="<memory>",
        source_index=SourceIndex("<memory>", b"<unused/>"),
        document_uri=None,
        base_uri=None,
    )
    assert list(_document_nodes(empty)) == [empty]


def test_original_function_and_variable_in_overrides(tmp_path: Path) -> None:
    library = tmp_path / "library.xsl"
    library.write_text(
        _package(
            "urn:library",
            """
            <xsl:variable name="my:number" select="1" visibility="public"/>
            <xsl:function name="my:label" as="xs:string" visibility="public">
              <xsl:param name="value" as="xs:string"/>
              <xsl:sequence select="concat('base:', $value)"/>
            </xsl:function>
            """,
        ),
        encoding="utf-8",
    )
    principal = tmp_path / "principal.xsl"
    principal.write_text(
        _package(
            "urn:principal",
            """
            <xsl:use-package name="urn:library" package-version="1.0">
              <xsl:override>
                <xsl:variable name="my:number" visibility="public" select="$xsl:original + 1"/>
                <xsl:function name="my:label" as="xs:string" visibility="public">
                  <xsl:param name="value" as="xs:string"/>
                  <xsl:sequence select="concat('over:', xsl:original($value))"/>
                </xsl:function>
              </xsl:override>
            </xsl:use-package>
            <xsl:template name="xsl:initial-template" visibility="public">
              <out><xsl:sequence select="my:label('x')"/></out>
              <number><xsl:sequence select="$my:number"/></number>
            </xsl:template>
            """,
        ),
        encoding="utf-8",
    )
    compiled = XsltProcessor(resolver=PackageResolver.from_sources(library)).compile(principal)
    result = compiled.transform(delivery="document")
    out, number = element_children(result.principal_tree)
    assert out.string_value == "over:base:x"
    assert number.string_value == "2"


def test_key_match_predicate_resolves_lazy_global_before_or_after_use() -> None:
    plain = stylesheet(
        """
        <xsl:variable name="flag" select="true()"/>
        <xsl:key name="k" match="item[$flag]" use="@id"/>
        <xsl:template name="main">
          <xsl:sequence select="count(key('k', 'a'))"/>
        </xsl:template>
        """
    )
    assert transform(plain, b'<doc><item id="a"/></doc>', initial_template="main").principal == (
        XdmAtomic.integer(1),
    )

    evaluated_first = stylesheet(
        """
        <xsl:variable name="flag" select="true()"/>
        <xsl:key name="k" match="item[$flag]" use="@id"/>
        <xsl:template name="main">
          <xsl:sequence select="boolean($flag)"/>
          <xsl:sequence select="count(key('k', 'a'))"/>
          <xsl:sequence select="count(key('k', 'a'))"/>
        </xsl:template>
        """
    )
    assert transform(
        evaluated_first,
        b'<doc><item id="a"/></doc>',
        initial_template="main",
    ).principal == (
        XdmAtomic.boolean(True),
        XdmAtomic.integer(1),
        XdmAtomic.integer(1),
    )


def test_original_function_body_runs_nested_iteration(tmp_path: Path) -> None:
    library = tmp_path / "library-iteration.xsl"
    library.write_text(
        _package(
            "urn:library-iteration",
            """
            <xsl:function name="my:label" as="xs:string" visibility="public">
              <xsl:param name="value" as="xs:string"/>
              <xsl:sequence select="concat('base:', $value)"/>
            </xsl:function>
            """,
        ),
        encoding="utf-8",
    )
    principal = tmp_path / "principal-iteration.xsl"
    principal.write_text(
        _package(
            "urn:principal-iteration",
            """
            <xsl:use-package name="urn:library-iteration" package-version="1.0">
              <xsl:override>
                <xsl:function name="my:label" as="xs:string" visibility="public">
                  <xsl:param name="value" as="xs:string"/>
                  <xsl:variable name="groups" as="xs:string*">
                    <xsl:for-each-group select="(1, 2, 3)" group-by=". mod 2">
                      <xsl:sequence select="string-join(current-group() ! string(.), ',')"/>
                    </xsl:for-each-group>
                  </xsl:variable>
                  <xsl:sequence
                      select="concat(string-join($groups, '|'), ':', xsl:original($value))"/>
                </xsl:function>
              </xsl:override>
            </xsl:use-package>
            <xsl:template name="xsl:initial-template" visibility="public">
              <xsl:sequence select="my:label('x')"/>
            </xsl:template>
            """,
        ),
        encoding="utf-8",
    )
    compiled = XsltProcessor(resolver=PackageResolver.from_sources(library)).compile(principal)
    result = compiled.transform(delivery="raw")
    assert result.principal == (XdmAtomic.string("1,3|2:base:x"),)


def test_library_key_match_predicate_uses_library_lazy_global(tmp_path: Path) -> None:
    library = tmp_path / "library.xsl"
    library.write_text(
        _package(
            "urn:library",
            """
            <xsl:variable name="my:flag" select="true()" visibility="private"/>
            <xsl:key name="my:k" match="item[$my:flag]" use="@id"/>
            <xsl:template name="my:start" visibility="public">
              <out><xsl:sequence select="count(key('my:k', 'a'))"/></out>
            </xsl:template>
            """,
        ),
        encoding="utf-8",
    )
    principal = tmp_path / "principal.xsl"
    principal.write_text(
        _package(
            "urn:principal",
            """
            <xsl:use-package name="urn:library" package-version="1.0">
              <xsl:accept component="template" names="my:start" visibility="public"/>
            </xsl:use-package>
            <xsl:template name="xsl:initial-template" visibility="public">
              <xsl:call-template name="my:start"/>
            </xsl:template>
            """,
        ),
        encoding="utf-8",
    )
    compiled = XsltProcessor(resolver=PackageResolver.from_sources(library)).compile(principal)
    result = compiled.transform(
        source=b'<doc><item id="a"/></doc>',
        initial_template="xsl:initial-template",
        delivery="document",
    )
    out = element_children(result.principal_tree)[0]
    assert out.string_value == "1"


# ---------------------------------------------------------------------------
# Namespace functions and lexical names (c.5)


def test_in_scope_prefixes_and_namespace_uri_use_the_element_scope() -> None:
    source = XdmDocument.parse(
        b'<r xmlns="urn:default" xmlns:p="urn:p" xmlns:q="urn:q"><child xmlns:q="urn:q2"/></r>'
    )
    body = stylesheet(
        """
        <xsl:template match="/">
          <xsl:sequence select="string-join(sort(in-scope-prefixes(/*)), ',')"/>
          <xsl:sequence select="namespace-uri-for-prefix('p', /*)"/>
          <xsl:sequence select="namespace-uri-for-prefix('', /*)"/>
          <xsl:sequence select="count(namespace-uri-for-prefix('missing', /*))"/>
          <xsl:sequence select="namespace-uri-for-prefix('q', /*/*[1])"/>
        </xsl:template>
        """
    )
    result = transform(body, input_source=source)
    assert result.principal == (
        XdmAtomic.string(",p,q,xml"),
        XdmAtomic.string("urn:p"),
        XdmAtomic.string("urn:default"),
        XdmAtomic.integer(0),
        XdmAtomic.string("urn:q2"),
    )


def test_name_uses_a_prefix_for_namespaced_attributes() -> None:
    source = XdmDocument.parse(b'<r xmlns="urn:p" xmlns:p="urn:p" p:a="1"><p:child/></r>')
    body = stylesheet(
        """
        <xsl:template match="/">
          <xsl:sequence select="name(/*/@*[1])"/>
          <xsl:sequence select="name(/*/p:child)"/>
          <xsl:sequence select="local-name(/*/p:child)"/>
          <xsl:sequence select="namespace-uri(/*/p:child)"/>
        </xsl:template>
        """,
        namespaces={"p": "urn:p"},
    )
    result = transform(body, input_source=source)
    assert result.principal == (
        XdmAtomic.string("p:a"),
        XdmAtomic.string("p:child"),
        XdmAtomic.string("child"),
        XdmAtomic.string("urn:p"),
    )


def test_namespace_functions_reject_a_non_element_argument() -> None:
    body = stylesheet(
        """
        <xsl:template match="/">
          <xsl:sequence select="in-scope-prefixes(/)"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(body, input_source=b"<r/>")) == "XPTY0004"

    uri_body = stylesheet(
        """
        <xsl:template match="/">
          <xsl:sequence select="namespace-uri-for-prefix('p', /)"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(uri_body, input_source=b"<r/>")) == "XPTY0004"

    atomic_body = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="in-scope-prefixes('text')"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(atomic_body, initial_template="main")) == "XPTY0004"
