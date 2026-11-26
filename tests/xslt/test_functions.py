"""Stylesheet-function coercion, keys, and stable generated identifiers."""

from __future__ import annotations

import io
from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest
from elementpath import XPathContext
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
from pyxsd.xdm.document import parse_typed_document
from pyxsd.xdm.nodes import NodeKind, XdmDocument
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xpath.engine import XPathEngine, _build_parser
from pyxsd.xpath.environment import ExpressionEnvironment, Focus, XPathStaticContext
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


def test_function_conversion_keeps_a_subtype_value_unchanged() -> None:
    """``as="xs:decimal"`` accepts an xs:integer without changing its type."""
    source = stylesheet(
        """
        <xsl:variable name="v" select="xs:integer(-300)" as="xs:decimal"/>
        <xsl:template name="main">
          <xsl:sequence select="$v instance of xs:integer"/>
          <xsl:sequence select="$v instance of xs:decimal"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
    )


def test_boolean_values_keep_their_xsd_type_identity_across_the_bridge() -> None:
    """A bound xs:boolean reaches expressions as xs:boolean, not xs:integer.

    ``pyxsd.xsd_data_types.Boolean`` is an ``int`` subclass for the XSD value
    space, but Python ``bool`` is the bridge's xs:boolean.  Handing the payload
    through unchanged made a typed variable report
    ``$b instance of xs:boolean`` as false and ``instance of xs:integer`` as
    true.
    """
    source = stylesheet(
        """
        <xsl:variable name="typed" select="xs:boolean('true')" as="xs:boolean"/>
        <xsl:variable name="computed" select="1 = 1" as="xs:boolean"/>
        <xsl:template name="main">
          <xsl:sequence select="$typed instance of xs:boolean"/>
          <xsl:sequence select="$typed instance of xs:integer"/>
          <xsl:sequence select="$computed instance of xs:boolean"/>
          <xsl:sequence select="$computed instance of xs:integer"/>
        </xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(False),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(False),
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


def test_key_lookup_does_not_match_a_typed_key_lexically() -> None:
    """A string lookup never matches an integer key value (XSLT §9.11)."""
    source = stylesheet(
        """
        <xsl:key name="k" match="item" use="xs:integer(@n)"/>
        <xsl:template name="main">
          <xsl:sequence
            select="count(key('k', 4)),
                    count(key('k', '4')),
                    count(key('k', xs:untypedAtomic('4')))"/>
        </xsl:template>
        """
    )
    assert transform(source, b'<doc><item n="4"/></doc>', initial_template="main").principal == (
        XdmAtomic.integer(1),
        XdmAtomic.integer(0),
        XdmAtomic.integer(1),
    )


def test_key_lookup_date_and_untyped_typing() -> None:
    """Only untypedAtomic is cast to the key's date type; xs:string is not."""
    source = stylesheet(
        """
        <xsl:key name="k" match="item" use="xs:date(@d)"/>
        <xsl:template name="main">
          <xsl:sequence
            select="count(key('k', '2000-01-01')),
                    count(key('k', xs:untypedAtomic('2000-01-01'))),
                    count(key('k', xs:date('2000-01-01')))"/>
        </xsl:template>
        """
    )
    assert transform(
        source, b'<doc><item d="2000-01-01"/></doc>', initial_template="main"
    ).principal == (
        XdmAtomic.integer(0),
        XdmAtomic.integer(1),
        XdmAtomic.integer(1),
    )


def test_key_lookup_boolean_and_integer_do_not_collapse() -> None:
    """Python's ``True == 1`` must not become an XSLT key match."""
    source = stylesheet(
        """
        <xsl:key name="k" match="item" use="xs:boolean(@b)"/>
        <xsl:template name="main">
          <xsl:sequence
            select="count(key('k', 1)), count(key('k', true())), count(key('k', 'true'))"/>
        </xsl:template>
        """
    )
    assert transform(source, b'<doc><item b="true"/></doc>', initial_template="main").principal == (
        XdmAtomic.integer(0),
        XdmAtomic.integer(1),
        XdmAtomic.integer(0),
    )


def test_composite_key_lookup_uses_typed_equality() -> None:
    source = stylesheet(
        """
        <xsl:key name="k" match="item" use="xs:integer(@a), xs:integer(@b)" composite="yes"/>
        <xsl:template name="main">
          <xsl:sequence
            select="count(key('k', (1, 2))),
                    count(key('k', ('1', '2'))),
                    count(key('k', (1, xs:untypedAtomic('2')))),
                    count(key('k', (1, 2, 3)))"/>
        </xsl:template>
        """
    )
    assert transform(
        source, b'<doc><item a="1" b="2"/></doc>', initial_template="main"
    ).principal == (
        XdmAtomic.integer(1),
        XdmAtomic.integer(0),
        XdmAtomic.integer(1),
        XdmAtomic.integer(0),
    )


def test_key_atomic_equality_casts_both_untyped_directions() -> None:
    from pyxsd.xslt.processor import _key_atomic_equal

    untyped = XdmAtomic.untyped_atomic("4")
    integer = XdmAtomic.integer(4)
    assert _key_atomic_equal(untyped, XdmAtomic.untyped_atomic("4"))
    assert _key_atomic_equal(untyped, integer)
    assert _key_atomic_equal(integer, untyped)
    assert not _key_atomic_equal(XdmAtomic.untyped_atomic("x"), integer)


def test_key_declarations_disagreeing_on_composite_are_a_static_error() -> None:
    source = stylesheet(
        """
        <xsl:key name="k" match="item" use="@a" composite="yes"/>
        <xsl:key name="k" match="item" use="@b" composite="no"/>
        <xsl:template name="main"><out/></xsl:template>
        """
    )
    with pytest.raises(XsltStaticError) as caught:
        XsltProcessor().compile(io.BytesIO(source))
    assert caught.value.code == "XTSE1222"


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

    missing_match = stylesheet('<xsl:key name="k" use="."/>')
    assert error_code(lambda: XsltProcessor().compile(io.BytesIO(missing_match))) == "XTSE0010"

    use_with_text = stylesheet('<xsl:key name="k" match="item" use="@id">text</xsl:key>')
    assert error_code(lambda: XsltProcessor().compile(io.BytesIO(use_with_text))) == "XTSE1205"

    use_with_element = stylesheet(
        '<xsl:key name="k" match="item" use="@id"><xsl:template match="/"/></xsl:key>'
    )
    assert error_code(lambda: XsltProcessor().compile(io.BytesIO(use_with_element))) == "XTSE0010"


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

    node_name = stylesheet(
        """
        <xsl:key name="k" match="item" use="@id"/>
        <xsl:template name="main"><xsl:sequence select="count(key(/*, 'x'))"/></xsl:template>
        """
    )
    assert (
        error_code(lambda: transform(node_name, b"<doc/>", initial_template="main")) == "XTDE1260"
    )

    multiple_names = stylesheet(
        """
        <xsl:key name="k" match="item" use="@id"/>
        <xsl:template name="main"><xsl:sequence select="count(key((1, 2), 'x'))"/></xsl:template>
        """
    )
    assert (
        error_code(lambda: transform(multiple_names, b"<doc/>", initial_template="main"))
        == "XPTY0004"
    )

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
    lookup = XdmAtomic.untyped_atomic("a")
    assert {node.local_name for node in index.nodes_for((lookup,), composite=False)} == {"item"}
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
        XdmAtomic.any_uri("urn:p"),
        XdmAtomic.any_uri("urn:default"),
        XdmAtomic.integer(0),
        XdmAtomic.any_uri("urn:q2"),
    )
    assert all(
        item.type_name == f"{{{XSD_NS}}}anyURI"
        for item in (result.principal[1], result.principal[2], result.principal[4])
    )


def test_namespace_uri_for_prefix_uses_function_conversion_for_its_argument() -> None:
    source = XdmDocument.parse(b'<r xmlns:p="urn:p"><prefix>p</prefix></r>')
    body = stylesheet(
        """
        <xsl:template match="/">
          <xsl:sequence select="namespace-uri-for-prefix(/*/prefix, /*)"/>
          <xsl:sequence select="namespace-uri-for-prefix(xs:untypedAtomic('p'), /*)"/>
          <xsl:sequence select="namespace-uri-for-prefix(xs:anyURI('p'), /*)"/>
          <xsl:sequence select="count(namespace-uri-for-prefix((), /*))"/>
        </xsl:template>
        """
    )
    result = transform(body, input_source=source)
    assert result.principal == (
        XdmAtomic.any_uri("urn:p"),
        XdmAtomic.any_uri("urn:p"),
        XdmAtomic.any_uri("urn:p"),
        XdmAtomic.integer(0),
    )
    assert result.principal[0].type_name == f"{{{XSD_NS}}}anyURI"
    assert result.principal[1].type_name == f"{{{XSD_NS}}}anyURI"


def test_id_reads_dtd_and_xml_id_declarations() -> None:
    source = (
        b"<!DOCTYPE tee [<!ELEMENT tee (s*)><!ELEMENT s EMPTY>"
        b"<!ATTLIST s id ID #REQUIRED>]>"
        b'<tee><s id="a"/><s id="b"/><s id="c"/><s xml:id="d"/></tee>'
    )
    source_sheet = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="id('b')/@id/string()"/>
          <xsl:sequence select="id('d')/name()"/>
          <xsl:sequence select="id(//s[1]/@id)/@id/string()"/>
          <xsl:sequence select="count(id(('c', 'a')))"/>
          <xsl:sequence select="count(id('missing'))"/>
          <xsl:sequence select="count(id(()))"/>
          <xsl:sequence select="element-with-id('c')/@id/string()"/>
        </xsl:template>
        """
    )
    result = transform(source_sheet, initial_template="main", input_source=source)
    assert result.principal == (
        XdmAtomic.string("b"),
        XdmAtomic.string("s"),
        XdmAtomic.string("a"),
        XdmAtomic.integer(2),
        XdmAtomic.integer(0),
        XdmAtomic.integer(0),
        XdmAtomic.string("c"),
    )


def test_element_with_id_returns_the_parent_of_an_id_valued_element(tmp_path: Path) -> None:
    """A schema ID on a child element identifies the parent for element-with-id."""
    schema = tmp_path / "id-child.xsd"
    schema.write_text(
        """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
             targetNamespace="urn:idchild" xmlns:t="urn:idchild"
             elementFormDefault="qualified">
             <xs:element name="table">
               <xs:complexType><xs:sequence>
                 <xs:element name="row" maxOccurs="unbounded">
                   <xs:complexType><xs:sequence>
                     <xs:element name="id" type="xs:ID"/>
                     <xs:element name="value" type="xs:string"/>
                   </xs:sequence></xs:complexType>
                 </xs:element>
               </xs:sequence></xs:complexType>
             </xs:element>
           </xs:schema>""",
        encoding="utf-8",
    )
    pool = SchemaPool.from_imports([schema], xsd_version="1.1")
    document = parse_typed_document(
        b"<t:table xmlns:t='urn:idchild'><t:row><t:id>A</t:id><t:value>ALPHA</t:value>"
        b"</t:row><t:row><t:id>C</t:id><t:value>GAMMA</t:value></t:row></t:table>",
        schema_pool=pool,
        validation="strict",
    )
    body = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="id('C')/local-name()"/>
          <xsl:sequence select="element-with-id('C')/local-name()"/>
          <xsl:sequence select="string(id('C')/parent::*/*[2])"/>
          <xsl:sequence select="string(element-with-id('C')/*[2])"/>
        </xsl:template>
        """
    )
    result = transform(body, input_source=document, initial_template="main", validation="preserve")
    assert result.principal == (
        XdmAtomic.string("id"),
        XdmAtomic.string("row"),
        XdmAtomic.string("GAMMA"),
        XdmAtomic.string("GAMMA"),
    )


def test_idref_returns_the_bearing_node_and_does_not_tokenize() -> None:
    """fn:idref returns the IDREF attribute and compares one string per value."""
    source = (
        b"<!DOCTYPE t04 [<!ELEMENT t04 (a*)><!ELEMENT a EMPTY>"
        b"<!ATTLIST a id ID #REQUIRED><!ATTLIST a ref IDREF #IMPLIED>]>"
        b'<t04><a id="a"/><a id="b" ref="c"/><a id="c" ref="d"/>'
        b'<a id="d" ref="a"/></t04>'
    )
    body = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence
            select="string-join(for $n in idref('a') return name($n) || ':' || $n/../@id, ',')"/>
          <xsl:sequence select="count(idref('a '))"/>
          <xsl:sequence select="count(idref('a b'))"/>
          <xsl:sequence select="string-join(for $n in idref(('c', 'd')) return $n/../@id, ',')"/>
        </xsl:template>
        """
    )
    result = transform(body, input_source=source, initial_template="main")
    assert result.principal == (
        XdmAtomic.string("ref:d"),
        XdmAtomic.integer(0),
        XdmAtomic.integer(0),
        XdmAtomic.string("b,c"),
    )


def test_idref_returns_an_idref_valued_element(tmp_path: Path) -> None:
    """An element whose own typed value is an IDREF is returned itself."""
    schema = tmp_path / "idref-element.xsd"
    schema.write_text(
        """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
             <xs:element name="r"><xs:complexType><xs:sequence>
               <xs:element name="id" type="xs:ID"/>
               <xs:element name="ref" type="xs:IDREF"/>
             </xs:sequence></xs:complexType></xs:element>
           </xs:schema>""",
        encoding="utf-8",
    )
    pool = SchemaPool.from_imports([schema], xsd_version="1.0")
    document = parse_typed_document(
        b"<r><id>a</id><ref>a</ref></r>", schema_pool=pool, validation="strict"
    )
    body = stylesheet(
        '<xsl:template name="main"><xsl:sequence select="idref(\'a\')/local-name()"/>'
        "</xsl:template>"
    )
    result = transform(body, input_source=document, initial_template="main", validation="preserve")
    assert result.principal == (XdmAtomic.string("ref"),)


def test_idref_error_arms() -> None:
    source = (
        b"<!DOCTYPE t04 [<!ELEMENT t04 (a*)><!ELEMENT a EMPTY>"
        b"<!ATTLIST a id ID #REQUIRED><!ATTLIST a ref IDREF #IMPLIED>]>"
        b'<t04><a id="a"/><a id="b" ref="a"/></t04>'
    )
    bad_item = stylesheet(
        '<xsl:template name="main"><xsl:sequence select="idref(map{})"/></xsl:template>'
    )
    assert error_code(lambda: transform(bad_item, source, initial_template="main")) == "XPTY0004"

    bad_node = stylesheet(
        "<xsl:template name=\"main\"><xsl:sequence select=\"idref('a', 'x')\"/></xsl:template>"
    )
    assert error_code(lambda: transform(bad_node, source, initial_template="main")) == "XPTY0004"

    no_context = stylesheet(
        '<xsl:template name="main"><xsl:sequence select="idref(\'a\')"/></xsl:template>'
    )
    assert error_code(lambda: transform(no_context, initial_template="main")) == "XPDY0002"

    empty = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="count(idref(()))"/>
          <xsl:sequence select="count(idref(/t04/a/@ref))"/>
          <xsl:sequence select="count(idref('a', /))"/>
        </xsl:template>
        """
    )
    assert transform(empty, source, initial_template="main").principal == (
        XdmAtomic.integer(0),
        XdmAtomic.integer(1),
        XdmAtomic.integer(1),
    )


def test_id_error_arms_and_dynamic_lookup() -> None:
    source = (
        b"<!DOCTYPE tee [<!ELEMENT tee (s*)><!ELEMENT s EMPTY>"
        b'<!ATTLIST s id ID #REQUIRED>]><tee><s id="a"/></tee>'
    )
    no_context = stylesheet(
        '<xsl:template name="main"><xsl:sequence select="id(\'a\')"/></xsl:template>'
    )
    assert error_code(lambda: transform(no_context, initial_template="main")) == "XPDY0002"

    bad_node = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="id('a', 'not-a-node')"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(bad_node, source, initial_template="main")) == "XPTY0004"

    bad_item = stylesheet(
        '<xsl:template name="main"><xsl:sequence select="id(map{})"/></xsl:template>'
    )
    assert (
        error_code(lambda: transform(bad_item, source, initial_template="main", delivery="raw"))
        == "XPTY0004"
    )

    dynamic = stylesheet(
        """
        <xsl:function name="my:lookup" as="element()*">
          <xsl:param name="n" as="node()"/>
          <xsl:sequence select="id('a', $n)"/>
        </xsl:function>
        <xsl:template name="main">
          <xsl:variable name="f" select="fn:id#2"/>
          <xsl:variable name="g" select="fn:element-with-id#2"/>
          <xsl:sequence select="my:lookup(/)/@id/string()"/>
          <xsl:sequence select="$f('a', /)/@id/string()"/>
          <xsl:sequence select="$g('a', /)/@id/string()"/>
        </xsl:template>
        """
    )
    assert transform(dynamic, source, initial_template="main").principal == (
        XdmAtomic.string("a"),
        XdmAtomic.string("a"),
        XdmAtomic.string("a"),
    )


def test_id_lookup_must_be_callable() -> None:
    from pyxsd.xslt.runtime.functions import XsltContextBridge

    with pytest.raises(TypeError, match="id_lookup"):
        XsltContextBridge(object(), id_lookup=1)  # type: ignore[arg-type]


def test_key_rejects_element_rooted_trees_for_both_argument_forms() -> None:
    two_argument = stylesheet(
        """
        <xsl:key name="k" match="*" use="'pqr'"/>
        <xsl:template name="main">
          <xsl:variable name="e" as="element()"><e/></xsl:variable>
          <xsl:for-each select="$e">
            <xsl:sequence select="key('k', 'abc')"/>
          </xsl:for-each>
        </xsl:template>
        """
    )
    three_argument = stylesheet(
        """
        <xsl:key name="k" match="*" use="'pqr'"/>
        <xsl:template name="main">
          <xsl:variable name="e" as="element()"><e/></xsl:variable>
          <xsl:variable name="d"><e/></xsl:variable>
          <xsl:for-each select="$d">
            <xsl:sequence select="key('k', 'abc', $e)"/>
          </xsl:for-each>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(two_argument, initial_template="main")) == "XTDE1270"
    assert error_code(lambda: transform(three_argument, initial_template="main")) == "XTDE1270"


def test_key_third_argument_limits_the_indexed_subtree() -> None:
    source = b"<doc><a><item n='1'/></a><b><item n='1'/></b></doc>"
    source_sheet = stylesheet(
        """
        <xsl:key name="k" match="item" use="@n"/>
        <xsl:template name="main">
          <xsl:variable name="sub" select="//a"/>
          <xsl:sequence select="count(key('k', '1', $sub))"/>
          <xsl:sequence select="count(key('k', '1'))"/>
        </xsl:template>
        """
    )
    result = transform(source_sheet, initial_template="main", input_source=source)
    assert result.principal == (XdmAtomic.integer(1), XdmAtomic.integer(2))


def test_id_in_a_pattern_matches_a_dtd_declared_id() -> None:
    source = (
        b"<!DOCTYPE tee [<!ELEMENT tee (s*)><!ELEMENT s EMPTY>"
        b'<!ATTLIST s id ID #REQUIRED>]><tee><s id="a"/><s id="c"/><s id="d"/></tee>'
    )
    source_sheet = stylesheet(
        """
        <xsl:template match="/"><out><xsl:apply-templates select="tee/s"/></out></xsl:template>
        <xsl:template match="id('c')"><hit/></xsl:template>
        <xsl:template match="s"><miss/></xsl:template>
        """
    )
    result = transform(source_sheet, input_source=source, delivery="document")
    out = result.principal_tree.root_element
    assert [child.local_name for child in element_children(out)] == ["miss", "hit", "miss"]


def test_key_accepts_a_sequence_constructor_and_a_node_key_name() -> None:
    source = b"<doc><item>2</item><item>3</item><item>4</item></doc>"
    source_sheet = stylesheet(
        """
        <xsl:variable name="keyspace" as="xs:string">k</xsl:variable>
        <xsl:key name="k" match="item">
          <xsl:sequence select="."/>
        </xsl:key>
        <xsl:template name="main">
          <xsl:sequence select="count(key($keyspace, 3))"/>
          <xsl:sequence select="key($keyspace, 3)/string()"/>
        </xsl:template>
        """
    )
    result = transform(source_sheet, initial_template="main", input_source=source)
    assert result.principal == (XdmAtomic.integer(1), XdmAtomic.string("3"))


def test_namespace_uri_for_prefix_rejects_a_non_string_prefix() -> None:
    source = XdmDocument.parse(b'<r xmlns:p="urn:p"/>')

    def code(expression: str) -> str | None:
        body = stylesheet(
            f"""
            <xsl:template match="/">
              <xsl:sequence select="{expression}"/>
            </xsl:template>
            """
        )
        return error_code(lambda: transform(body, input_source=source))

    assert code("namespace-uri-for-prefix(1, /*)") == "XPTY0004"
    assert code("namespace-uri-for-prefix(true(), /*)") == "XPTY0004"
    assert code("namespace-uri-for-prefix(('p', 'q'), /*)") == "XPTY0004"


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


def test_attribute_name_falls_back_when_the_stored_prefix_is_absent() -> None:
    from pyxsd.xpath.engine import _adapter_node_name
    from pyxsd.xslt.runtime.builder import _NodeFactory

    factory = _NodeFactory()
    element = factory.element("r", namespaces={"p": "urn:p", "q": "urn:p"})
    attribute = factory.attribute(
        "{urn:p}a",
        "1",
        parent=element,
        prefix="",
        in_scope_namespaces=element.in_scope_namespaces,
    )
    name = _adapter_node_name(attribute, is_attribute=True)
    assert name is not None
    assert name.qname == "p:a"


def test_name_keeps_the_stored_prefix_when_two_prefixes_share_a_uri() -> None:
    source = XdmDocument.parse(b'<r xmlns:q="urn:p" xmlns:p="urn:p" p:a="1"/>')
    body = stylesheet(
        """
        <xsl:template match="/">
          <xsl:sequence select="name(/*/@*[1])"/>
          <xsl:sequence select="name(/*/@*[1]) = 'p:a'"/>
        </xsl:template>
        """
    )
    result = transform(body, input_source=source)
    assert result.principal == (XdmAtomic.string("p:a"), XdmAtomic.boolean(True))


def test_node_name_uses_the_nodes_lexical_prefix_and_namespace() -> None:
    source = XdmDocument.parse(
        b'<e xmlns:my="urn:my" att="x" my:att="v"><my:g/><!--c--><?pi x?></e>'
    )
    body = stylesheet(
        """
        <xsl:template match="/">
          <xsl:sequence select="prefix-from-QName(node-name(/*/@my:att))"/>
          <xsl:sequence select="namespace-uri-from-QName(node-name(/*/@my:att))"/>
          <xsl:sequence select="local-name-from-QName(node-name(/*/@my:att))"/>
          <xsl:sequence select="string(node-name(/*/my:g))"/>
          <xsl:sequence select="string(node-name(/*/@att))"/>
          <xsl:sequence select="empty(node-name(/*/text()[1]))"/>
          <xsl:sequence select="empty(node-name(/*/comment()))"/>
          <xsl:sequence select="local-name-from-QName(node-name(/*/processing-instruction()))"/>
          <xsl:sequence select="empty(prefix-from-QName(node-name(/*/processing-instruction())))"/>
          <xsl:sequence select="namespace-uri-from-QName(node-name(/*/processing-instruction())) = ''"/>
        </xsl:template>
        """
    )
    result = transform(body, input_source=source)
    assert result.principal == (
        XdmAtomic.string("my"),
        XdmAtomic.string("urn:my"),
        XdmAtomic.string("att"),
        XdmAtomic.string("my:g"),
        XdmAtomic.string("att"),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
        XdmAtomic.string("pi"),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
    )


def test_resolve_qname_atomizes_nodes_and_uses_the_element_scope() -> None:
    source = XdmDocument.parse(
        b'<doc xmlns="urn:d" xmlns:my="urn:my"><chap xmlns:other="urn:o" my:att="my:local"/></doc>'
    )
    body = stylesheet(
        """
        <xsl:template match="/">
          <xsl:sequence select="namespace-uri-from-QName(resolve-QName(/*/*/@my:att, /*))"/>
          <xsl:sequence select="local-name-from-QName(resolve-QName(/*/*/@my:att, /*))"/>
          <xsl:sequence select="empty(prefix-from-QName(resolve-QName('local', /*/*)))"/>
          <xsl:sequence select="namespace-uri-from-QName(resolve-QName('local', /*/*))"/>
          <xsl:sequence select="prefix-from-QName(resolve-QName('other:x', /*/*))"/>
          <xsl:sequence select="empty(resolve-QName((), /*))"/>
        </xsl:template>
        """
    )
    result = transform(body, input_source=source)
    assert result.principal == (
        XdmAtomic.string("urn:my"),
        XdmAtomic.string("local"),
        XdmAtomic.boolean(True),
        XdmAtomic.string("urn:d"),
        XdmAtomic.string("other"),
        XdmAtomic.boolean(True),
    )


def test_node_name_returns_empty_without_a_name_and_uses_the_context_item() -> None:
    source = XdmDocument.parse(b'<e xmlns:p="urn:p"/>')
    body = stylesheet(
        """
        <xsl:template match="/">
          <xsl:sequence select="empty(node-name())"/>
          <xsl:sequence select="empty(node-name(()))"/>
          <xsl:sequence select="string(node-name(/*/namespace::p))"/>
          <xsl:sequence select="namespace-uri-from-QName(node-name(/*/namespace::p)) = ''"/>
        </xsl:template>
        """
    )
    result = transform(body, input_source=source)
    assert result.principal == (
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
        XdmAtomic.string("p"),
        XdmAtomic.boolean(True),
    )

    standalone = stylesheet(
        '<xsl:template name="main"><xsl:sequence select="node-name()"/></xsl:template>'
    )
    assert error_code(lambda: transform(standalone, initial_template="main")) == "XPDY0002"

    atomic = stylesheet(
        """
        <xsl:template match="/">
          <xsl:sequence select="node-name(1)"/>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(atomic, input_source=source)) == "XPTY0004"

    atomic_context = stylesheet(
        """
        <xsl:template match="/">
          <xsl:for-each select="1">
            <xsl:sequence select="node-name()"/>
          </xsl:for-each>
        </xsl:template>
        """
    )
    assert error_code(lambda: transform(atomic_context, input_source=source)) == "XPTY0004"

    default_namespace = stylesheet(
        """
        <xsl:template match="/">
          <xsl:sequence select="empty(node-name(/*/namespace::*[name() = '']))"/>
        </xsl:template>
        """
    )
    result = transform(default_namespace, input_source=b'<r xmlns="urn:d"/>')
    assert result.principal == (XdmAtomic.boolean(True),)


def test_resolve_qname_reports_unbound_and_invalid_lexical_names() -> None:
    source = XdmDocument.parse(b'<r xmlns:p="urn:p"/>')

    def code(expression: str) -> str | None:
        body = stylesheet(
            f"""
            <xsl:template match="/">
              <xsl:sequence select="{expression}"/>
            </xsl:template>
            """
        )
        return error_code(lambda: transform(body, input_source=source))

    assert code("resolve-QName('missing:x', /*)") == "FONS0004"
    assert code("resolve-QName('1bad', /*)") == "FOCA0002"
    assert code("resolve-QName('a:b:c', /*)") == "FOCA0002"
    assert code("resolve-QName(1, /*)") == "XPTY0004"
    assert code("resolve-QName((1, 2), /*)") == "XPTY0004"
    assert code("resolve-QName(map{}, /*)") == "XPTY0004"
    assert code("resolve-QName('p:x', 'not a node')") == "XPTY0004"
    assert code("resolve-QName('x', /)") == "XPTY0004"
    assert code("resolve-QName((), 'not a node')") == "XPTY0004"
    assert code("resolve-QName((), /)") == "XPTY0004"


TYPED_NS = "urn:example:typed-arguments"


def _typed_argument_stylesheet(expression: str) -> bytes:
    schema = f"""
    <xs:schema xmlns:xs="{XSD_NS}" xmlns:t="{TYPED_NS}" targetNamespace="{TYPED_NS}"
        elementFormDefault="qualified">
      <xs:simpleType name="Tokens"><xs:list itemType="xs:string"/></xs:simpleType>
      <xs:complexType name="Holder">
        <xs:sequence><xs:element name="empty-list" type="t:Tokens" minOccurs="0"/></xs:sequence>
        <xs:attribute name="num" type="xs:int"/>
        <xs:attribute name="qn" type="xs:QName"/>
        <xs:attribute name="label" type="xs:string"/>
      </xs:complexType>
      <xs:element name="holder" type="t:Holder"/>
    </xs:schema>
    """
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" xmlns:t="{TYPED_NS}" '
        f'xmlns:p="urn:p" version="3.0">'
        f"<xsl:import-schema>{schema}</xsl:import-schema>"
        f'<xsl:template name="main"><xsl:sequence select="{expression}"/></xsl:template>'
        "</xsl:stylesheet>"
    ).encode()


def _typed_argument_result(expression: str):
    compiled = XsltProcessor(validation="strict").compile(
        io.BytesIO(_typed_argument_stylesheet(expression))
    )
    return compiled.transform(
        (
            f'<t:holder xmlns:t="{TYPED_NS}" xmlns:p="urn:p" num="5" qn="p:local" '
            f'label="p:local"><t:empty-list/></t:holder>'
        ).encode(),
        initial_template="main",
        delivery="raw",
    )


def test_resolve_qname_uses_the_argument_nodes_typed_value() -> None:
    def code(expression: str) -> str | None:
        return error_code(lambda: _typed_argument_result(expression))

    # A schema-typed integer or QName is not a string after atomization, so
    # function conversion rejects it instead of parsing its string value.
    assert code("resolve-QName(/*/@num, /*)") == "XPTY0004"
    assert code("resolve-QName(/*/@qn, /*)") == "XPTY0004"

    # An empty atomized typed value stays an empty optional string.
    assert _typed_argument_result("empty(resolve-QName(/*/t:empty-list, /*))").principal == (
        XdmAtomic.boolean(True),
    )

    # A schema-typed xs:string node still converts normally.
    result = _typed_argument_result(
        "concat(prefix-from-QName(resolve-QName(/*/@label, /*)), ':',"
        " local-name-from-QName(resolve-QName(/*/@label, /*)))"
    )
    assert result.principal == (XdmAtomic.string("p:local"),)


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


def test_atomic_to_elementpath_keeps_xsd_type_identity() -> None:
    from elementpath.datatypes import (
        AnyURI,
        Date,
        Duration,
        Float,
        Int,
        QName,
        XsdToken,
    )

    from pyxsd import xsd_data_types as xsd
    from pyxsd.xpath.functions import atomic_to_elementpath
    from pyxsd.xsd_data_types import qname_context

    assert atomic_to_elementpath(XdmAtomic.boolean(True)) is True
    assert isinstance(atomic_to_elementpath(XdmAtomic.typed(xsd.Int(3), xsd.Int)), Int)
    assert isinstance(atomic_to_elementpath(XdmAtomic.typed(xsd.Float(1.5), xsd.Float)), Float)
    assert isinstance(atomic_to_elementpath(XdmAtomic.typed(xsd.Token("a"), xsd.Token)), XsdToken)
    assert isinstance(
        atomic_to_elementpath(XdmAtomic.typed(xsd.AnyURI("urn:x"), xsd.AnyURI)), AnyURI
    )
    assert isinstance(
        atomic_to_elementpath(XdmAtomic.typed(xsd.Date("1999-05-31"), xsd.Date)), Date
    )
    assert isinstance(
        atomic_to_elementpath(XdmAtomic.typed(xsd.Duration("P1Y"), xsd.Duration)), Duration
    )

    with qname_context({"p": "urn:x"}):
        resolved = XdmAtomic.typed(xsd.QName("p:local"), xsd.QName)
    bridged = atomic_to_elementpath(resolved)
    assert isinstance(bridged, QName)
    assert bridged.namespace == "urn:x"
    assert bridged.local_name == "local"

    # An unresolved QName has no namespace to build elementpath's value from.
    unresolved = XdmAtomic.typed(xsd.QName("local"), xsd.QName)
    assert atomic_to_elementpath(unresolved) is unresolved.value


def test_atomic_to_elementpath_leaves_custom_types_and_host_payloads_alone() -> None:
    from pyxsd import xsd_data_types as xsd
    from pyxsd.xpath.functions import atomic_to_elementpath

    class _CustomString(xsd.String):
        name = "custom"
        _targetNamespace_ = "urn:test"

    custom = XdmAtomic(_CustomString("value"), "{urn:test}custom", _CustomString)
    assert atomic_to_elementpath(custom) is custom.value

    # A payload that is not an elementpath atomic converts through its lexical form.
    plain = XdmAtomic("plain", "{urn:test}payload")
    assert atomic_to_elementpath(plain) == "plain"
    payload = XdmAtomic(b"plain", "{urn:test}payload")
    assert atomic_to_elementpath(payload) == "b'plain'"


def test_fn_string_uses_the_fo_canonical_numeric_forms() -> None:
    source = stylesheet(
        """
        <xsl:template name="main">
          <xsl:value-of select="string(true())"/>
          <xsl:text>|</xsl:text>
          <xsl:value-of select="string(xs:double(7000000))"/>
          <xsl:text>|</xsl:text>
          <xsl:value-of select="string(xs:float(-1.75e-3))"/>
          <xsl:text>|</xsl:text>
          <xsl:value-of select="string(xs:decimal('2.50'))"/>
        </xsl:template>
        """
    )
    assert (
        "".join(item.string_value for item in transform(source, initial_template="main").principal)
        == "true|7.0E6|-0.00175|2.5"
    )


def test_format_number_atomizes_a_node_or_result_tree_fragment_argument() -> None:
    source = stylesheet(
        """
        <xsl:variable name="pattern">#,###.00</xsl:variable>
        <xsl:template name="main">
          <xsl:value-of select="format-number(1234.78, $pattern)"/>
          <xsl:text>|</xsl:text>
          <xsl:value-of select="format-number(1234.78, //pattern)"/>
        </xsl:template>
        """
    )
    raw = _transform(
        source,
        input_source=b"<doc><pattern>#,###.00</pattern></doc>",
        initial_template="main",
    )
    assert "".join(item.string_value for item in raw.principal) == "1,234.78|1,234.78"

    native = XsltProcessor(language="1.0", xpath="1.0", schema_aware=False)
    compiled = native.compile(io.BytesIO(source))
    assert (
        "".join(
            item.string_value
            for item in compiled.transform(
                source=b"<doc><pattern>#,###.00</pattern></doc>",
                initial_template="main",
                delivery="raw",
            ).principal
        )
        == "1,234.78|1,234.78"
    )

    # The number argument takes the same conversion: a native 1.0 content
    # variable is a result tree fragment whose string value is the number.
    fragment = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="number">12.5</xsl:variable>
          <xsl:value-of select="format-number($number, '#.0')"/>
        </xsl:template>
        """,
        version="1.0",
    )
    native_fragment = native.compile(io.BytesIO(fragment))
    assert (
        "".join(
            item.string_value
            for item in native_fragment.transform(initial_template="main", delivery="raw").principal
        )
        == "12.5"
    )

    # A picture that is a sequence or a function item is still a type error.
    for picture in ("('#', '0')", "function () { '#' }"):
        invalid = stylesheet(
            f'<xsl:template name="main">'
            f'<xsl:value-of select="format-number(1, {picture})"/>'
            "</xsl:template>"
        )
        with pytest.raises(XsltError) as caught:
            transform(invalid, initial_template="main")
        assert caught.value.code == "XPTY0004"


# ---------------------------------------------------------------------------
# Date/time components and arithmetic


def test_seconds_from_date_time_keeps_the_fractional_precision() -> None:
    body = """
        <xsl:template name="main">
          <xsl:value-of select="
              seconds-from-dateTime(xs:dateTime('2002-06-15T12:27:30.012+03:00')),
              seconds-from-time(xs:time('12:27:30.45'))" separator="|"/>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert [item.string_value for item in result.principal] == ["30.012|30.45"]


def test_date_time_difference_keeps_a_negative_fraction_exact() -> None:
    body = """
        <xsl:template name="main">
          <xsl:sequence select="xs:time('00:11:15.3Z') - xs:time('00:12:00Z')"/>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert result.principal[0].string_value == "-PT44.7S"


def test_extended_year_date_difference_is_a_day_time_duration() -> None:
    """The extended-year branch converts its timedelta to duration seconds."""
    body = """
        <xsl:template name="main">
          <xsl:sequence select="xs:date('-0001-01-01') - xs:date('-0002-01-01')"/>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert result.principal[0].string_value == "P365D"


def test_day_time_duration_plus_date_time_is_symmetric() -> None:
    body = """
        <xsl:template name="main">
          <xsl:sequence select="
              xs:dayTimeDuration('P5DT4H0M0.5S') + xs:dateTime('2004-02-29T00:12:00Z')"/>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert result.principal[0].string_value == "2004-03-05T04:12:00.5Z"


def test_date_arithmetic_overflow_reports_fodt0001() -> None:
    body = """
        <xsl:template name="main">
          <xsl:sequence select="xs:date('2003-12-07') + xs:yearMonthDuration('P9999Y')"/>
        </xsl:template>
    """
    assert error_code(lambda: transform(stylesheet(body), initial_template="main")) == "FODT0001"


def test_date_time_variable_keeps_its_timezone_after_a_difference() -> None:
    """A difference must not assign the implicit timezone to the operands."""
    body = """
        <xsl:template name="main">
          <xsl:for-each select="1 to 1">
            <xsl:variable name="n" select="xs:date('2004-03-01') + xs:dayTimeDuration('P1D')"/>
            <xsl:value-of select="string($n), string($n - xs:date('1901-01-06')), string($n)"
                          separator="|"/>
          </xsl:for-each>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert result.principal[0].string_value == "2004-03-02|P37676D|2004-03-02"


def test_concat_and_string_join_use_the_canonical_string_conversion() -> None:
    """Both spelling functions agree with ``fn:string`` for numeric values."""
    body = """
        <xsl:template name="main">
          <xsl:value-of select="
              concat(xs:double(1000000), '+', xs:double('1e-13'), '+', xs:float(-0.00175)),
              string-join((xs:double(1000000), xs:double('1e-13')), '+')" separator="|"/>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert result.principal[0].string_value == ("1.0E6+1.0E-13+-0.00175|1.0E6+1.0E-13")


def test_replace_uses_the_fo_replacement_string_grammar() -> None:
    """Escaped backslashes and missing group references follow F&O."""
    body = r"""
        <xsl:template name="main">
          <xsl:value-of select="
              replace('a?b', '\?', '\\$0\\'),
              replace('abc', '(a)(b)', '[$1][$3][$25]')" separator="|"/>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert result.principal[0].string_value == r"a\?\b|[a][][b5]c"
    invalid = r"""
        <xsl:template name="main">
          <xsl:value-of select="replace('input', '(unparsed)', '\n')"/>
        </xsl:template>
    """
    assert (
        error_code(lambda: transform(stylesheet(invalid, version="2.0"), initial_template="main"))
        == "FORX0004"
    )


def test_instance_of_document_node_applies_occurrence_and_element_test() -> None:
    """A document-node item test matches each item, not the outer focus."""
    body = """
        <xsl:variable name="a"><a/></xsl:variable>
        <xsl:variable name="b"><b/></xsl:variable>
        <xsl:template name="main">
          <out>
            <xsl:value-of separator="|" select="
                ($a, $b) instance of document-node(element(*))*,
                ($a, $b) instance of document-node(element(*))+,
                ($a, $b) instance of document-node(element(*))?,
                ($a, $b) instance of document-node(element(a))*"/>
          </out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert result.principal[0].string_value == "true|true|false|false"


def test_value_of_separator_joins_text_nodes_from_different_parents() -> None:
    """The default separator applies between separate text-node items."""
    body = """
        <xsl:template name="main">
          <out><xsl:value-of select="/doc/a/text() | /doc/b/text()"/></out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b"<doc><a>x</a><b>y</b></doc>",
        initial_template="main",
        delivery="document",
    )
    assert result.principal_tree.root_element.string_value == "x y"


def test_descendant_path_returns_document_order() -> None:
    """``//text()`` interleaves an element's text with its descendants' text."""
    body = """
        <xsl:template name="main">
          <out><xsl:value-of select="
              string-join(for $t in /doc//text() return string($t), '|')"/></out>
        </xsl:template>
    """
    result = transform(
        stylesheet(body),
        b"<doc>a<e>b</e>c<f>d</f>e</doc>",
        initial_template="main",
        delivery="document",
    )
    assert result.principal_tree.root_element.string_value == "a|b|c|d|e"


def test_as_coercion_preserves_and_promotes_schema_types(tmp_path: Path) -> None:
    """A subtype is substituted; a numeric promotion produces the base type."""
    (tmp_path / "types.xsd").write_text(
        """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
               targetNamespace="urn:types" xmlns:t="urn:types"
               elementFormDefault="qualified">
             <xs:simpleType name="myint">
               <xs:restriction base="xs:int"><xs:minInclusive value="0"/></xs:restriction>
             </xs:simpleType>
             <xs:element name="root" type="t:myint"/>
           </xs:schema>""",
        encoding="utf-8",
    )
    (tmp_path / "types.xsl").write_text(
        """<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
               xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:t="urn:types"
               version="2.0">
             <xsl:import-schema namespace="urn:types" schema-location="types.xsd"/>
             <xsl:variable name="sub" as="xs:decimal" select="/t:root"/>
             <xsl:variable name="promoted" as="xs:double" select="/t:root"/>
             <xsl:template name="main"><out>
               <a><xsl:value-of select="$sub instance of t:myint"/>
                  <xsl:value-of select="$sub instance of xs:decimal"/></a>
               <b><xsl:value-of select="$promoted instance of t:myint"/>
                  <xsl:value-of select="$promoted instance of xs:double"/></b>
             </out></xsl:template>
           </xsl:stylesheet>""",
        encoding="utf-8",
    )
    compiled = XsltProcessor(language="2.0", xpath="2.0", schema_aware=True).compile(
        tmp_path / "types.xsl"
    )
    result = compiled.transform(
        source=io.BytesIO(b'<t:root xmlns:t="urn:types">5</t:root>'),
        initial_template="main",
        delivery="document",
        validation="strict",
    )
    values = [
        element.string_value for element in element_children(result.principal_tree.root_element)
    ]
    assert values == ["truetrue", "falsetrue"]


def test_value_of_discards_zero_length_text_nodes() -> None:
    """An empty text item contributes neither a value nor a separator."""
    body = """
        <xsl:variable name="empty" as="text()"><xsl:text/></xsl:variable>
        <xsl:template name="main">
          <out><xsl:value-of select="('a', $empty, 'b')" separator="|"/></out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert result.principal[0].string_value == "a|b"


def test_instance_of_an_inline_cast_uses_the_base_sequence_type_test() -> None:
    """A cast inside one expression has no bridge annotation to consult."""
    body = """
        <xsl:template name="main">
          <out><xsl:value-of select="(8 cast as xs:int) instance of xs:int"/></out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert result.principal[0].string_value == "true"


def test_instance_of_a_typed_element_uses_the_annotation_matcher() -> None:
    """``element(*, T)`` is a type-annotation test, not a value-space test."""
    body = """
        <xsl:variable name="e"><a/></xsl:variable>
        <xsl:template name="main">
          <out><xsl:value-of select="$e/a instance of element(*, xs:anyType)"/>
               <xsl:value-of select="$e/a instance of element(*, xs:string)"/></out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert result.principal[0].string_value == "truefalse"


def test_replace_with_q_flag_keeps_backslash_and_dollar_literal() -> None:
    """A literal pattern protects the replacement from group expansion."""
    body = """
        <xsl:template name="main">
          <out><xsl:value-of select="replace('a.b', '.', '\\$0', 'q')"/></out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert result.principal[0].string_value == "a\\$0b"


def test_seconds_from_an_empty_sequence_is_empty() -> None:
    """A missing date/time argument yields an empty sequence, not an error."""
    body = """
        <xsl:template name="main">
          <out><xsl:value-of select="seconds-from-dateTime(()), seconds-from-time(())"
                               separator="|"/></out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert result.principal[0].string_value == ""


def test_descendant_text_orders_items_across_documents() -> None:
    """``//`` keeps each document's own order when the sequence spans trees."""
    body = """
        <xsl:variable name="a">first</xsl:variable>
        <xsl:variable name="b">second</xsl:variable>
        <xsl:template name="main">
          <out><xsl:value-of select="($a, $b)//text()" separator="|"/></out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert result.principal[0].string_value == "first|second"


def test_concat_and_replace_cover_empty_and_invalid_arguments() -> None:
    """Empty concat items, regex flags, and bad replacements use F&O codes."""
    body = """
        <xsl:template name="main">
          <out><xsl:value-of select="concat((), 'x')"/></out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert result.principal[0].string_value == "x"

    for expression, code in (
        ("replace('Abc', 'b', 'x', 'i')", None),
        ("replace('a', 'a', 'x', 'z')", "FORX0001"),
        ("replace('a', '.*', 'x')", "FORX0003"),
        ("replace('a', 'a', '\\')", "FORX0004"),
        ("replace('a', 'a', '$x')", "FORX0004"),
        ("replace('a', '[', 'x')", "FORX0002"),
    ):
        body = (
            '<xsl:template name="main">'
            f'<out><xsl:value-of select="{expression}"/></out>'
            "</xsl:template>"
        )
        if code is None:
            result = transform(stylesheet(body), initial_template="main")
            assert result.principal[0].string_value == "Axc"
        else:
            assert (
                error_code(lambda body=body: transform(stylesheet(body), initial_template="main"))
                == code
            )

    non_strict = """
        <xsl:template name="main">
          <out><xsl:value-of select="replace('a', 'a', '\\q')"/></out>
        </xsl:template>
    """
    result = transform(stylesheet(non_strict), initial_template="main")
    assert result.principal[0].string_value == "\\q"

    absent_group = """
        <xsl:template name="main">
          <out><xsl:value-of select="replace('a', '(a)', '[$9]')"/></out>
        </xsl:template>
    """
    result = transform(stylesheet(absent_group), initial_template="main")
    assert result.principal[0].string_value == "[]"

    leading_group = """
        <xsl:template name="main">
          <out><xsl:value-of select="replace('ab', '(a)(b)', '$1x$2')"/></out>
        </xsl:template>
    """
    result = transform(stylesheet(leading_group), initial_template="main")
    assert result.principal[0].string_value == "axb"


def test_seconds_without_a_fraction_return_an_integer() -> None:
    body = """
        <xsl:template name="main">
          <out><xsl:value-of select="seconds-from-dateTime(xs:dateTime('2002-06-15T12:27:30'))"/>
               <xsl:value-of select="seconds-from-time(xs:time('12:27:30'))"/></out>
        </xsl:template>
    """
    result = transform(stylesheet(body), initial_template="main")
    assert result.principal[0].string_value == "3030"


def test_arithmetic_operand_and_error_paths() -> None:
    """Date/date addition, incompatible operands, and division errors report codes."""
    for expression, code in (
        ("xs:date('2003-01-01') + xs:date('2003-01-02')", "XPTY0004"),
        ("xs:date('2003-01-01') + 1", "XPTY0004"),
        ("1 div 0", "FOAR0001"),
        ("2 - 1", None),
    ):
        body = f'<xsl:template name="main"><xsl:value-of select="{expression}"/></xsl:template>'
        if code is None:
            result = transform(stylesheet(body), initial_template="main")
            assert result.principal[0].string_value == "1"
        else:
            assert (
                error_code(lambda body=body: transform(stylesheet(body), initial_template="main"))
                == code
            )

    for expression in (
        "xs:dayTimeDuration('P1D') + xs:float(1.5)",
        "xs:float(1.5) + xs:dayTimeDuration('P1D')",
    ):
        body = f'<xsl:template name="main"><xsl:value-of select="{expression}"/></xsl:template>'
        assert (
            error_code(lambda body=body: transform(stylesheet(body), initial_template="main"))
            == "XPTY0004"
        )

    overflow = (
        '<xsl:template name="main">'
        "<xsl:value-of select=\"xs:dayTimeDuration('P99999999999999D') + "
        "xs:dayTimeDuration('P99999999999999D')\"/>"
        "</xsl:template>"
    )
    assert (
        error_code(lambda: transform(stylesheet(overflow), initial_template="main")) == "FODT0002"
    )

    for expression in (
        "xs:float(1.5) + xs:decimal('1.5')",
        "xs:decimal('1.5') + xs:float(1.5)",
    ):
        body = (
            '<xsl:template name="main">'
            f'<out><xsl:value-of select="{expression}"/></out>'
            "</xsl:template>"
        )
        result = transform(stylesheet(body), initial_template="main")
        assert result.principal[0].string_value == "3"

    element = (
        '<xsl:template match="/"><out><xsl:value-of select="/doc/a + 1"/></out></xsl:template>'
    )
    result = transform(stylesheet(element), b"<doc><a>2</a></doc>", delivery="document")
    assert result.principal_tree.root_element.string_value == "3"

    empty = '<xsl:template name="main"><out><xsl:value-of select="() + 1"/></out></xsl:template>'
    result = transform(stylesheet(empty), initial_template="main")
    assert result.principal[0].string_value == ""


def test_copy_of_returns_a_deep_copy_with_fresh_node_identity() -> None:
    """XSLT 3.0 section 18.3: copy-of copies each item of the sequence."""
    body = stylesheet(
        """
        <xsl:template match="/">
          <xsl:sequence select="(
            copy-of(/doc) is /doc,
            count(copy-of(/doc/*)) = 1,
            deep-equal(copy-of(/doc), /doc),
            copy-of(/doc/x/self::x)/@a
          )"/>
        </xsl:template>
        """
    )
    result = transform(body, input_source=b'<doc a="1"><x a="2">t</x></doc>')

    assert result.principal[0] == XdmAtomic.boolean(False)
    assert result.principal[1] == XdmAtomic.boolean(True)
    assert result.principal[2] == XdmAtomic.boolean(True)
    copied = result.principal[3]
    assert copied.string_value == "2"
    assert copied.parent is not None and copied.parent.kind is NodeKind.ELEMENT
    # The attribute is copied alone: its new parent is the copy, not the source.
    assert copied.parent.parent is None


def test_copy_of_defaults_to_the_context_item() -> None:
    body = stylesheet(
        """
        <xsl:template match="/doc/x">
          <xsl:sequence select="(name(copy-of()), copy-of() is .)"/>
        </xsl:template>
        """
    )
    result = transform(body, input_source=b"<doc><x>t</x></doc>")

    assert result.principal == (XdmAtomic.string("x"), XdmAtomic.boolean(False))


def test_copy_of_leaves_atomic_and_function_items_unchanged() -> None:
    body = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="(
            copy-of((1, 'a', true())),
            let $f := copy-of(abs#1) return (local-name-from-QName(function-name($f)),
                                              function-arity($f),
                                              $f(-2))
          )"/>
        </xsl:template>
        """
    )
    result = transform(body, initial_template="main", delivery="raw")

    assert [item.string_value for item in result.principal[:3]] == ["1", "a", "true"]
    assert result.principal[3] == XdmAtomic.string("abs")
    assert result.principal[4].string_value == "1"
    assert result.principal[5].string_value == "2"


def test_copy_of_without_a_context_item_is_xpdy0002() -> None:
    body = stylesheet(
        """
        <xsl:function name="my:copy">
          <xsl:param name="n"/>
          <xsl:sequence select="copy-of()"/>
        </xsl:function>
        <xsl:template match="/">
          <xsl:sequence select="my:copy(.)"/>
        </xsl:template>
        """,
        namespaces={"my": MY_NS},
    )
    assert error_code(lambda: transform(body, input_source=b"<doc/>")) == "XPDY0002"


def test_copy_of_a_parentless_namespace_sensitive_attribute_is_xtte0950() -> None:
    """copy-of always preserves; a parentless QName attribute cannot keep it."""
    with pytest.raises(XsltError) as caught:
        _typed_argument_result("copy-of(/*/@qn)")
    assert caught.value.code == "XTTE0950"


SNAPSHOT_SOURCE = b'<doc id="d"><a id="1"><b>deep</b><c>side</c></a><x>sib</x></doc>'


def test_snapshot_of_a_document_node_is_a_deep_copy() -> None:
    """XSLT 3.0 section 18.4: a parentless node behaves like copy-of."""
    body = stylesheet(
        """
        <xsl:template match="/">
          <xsl:sequence select="(snapshot(/) is /, deep-equal(snapshot(/), /))"/>
        </xsl:template>
        """
    )
    result = transform(body, input_source=SNAPSHOT_SOURCE)

    assert result.principal == (XdmAtomic.boolean(False), XdmAtomic.boolean(True))


def test_snapshot_of_a_child_element_keeps_only_its_ancestor_chain() -> None:
    body = stylesheet(
        """
        <xsl:template match="/">
          <xsl:variable name="s" select="snapshot(/doc/a/b)"/>
          <xsl:sequence select="(
            name($s),
            count($s/ancestor::node()) = 3,
            count($s/ancestor::doc) = 1,
            count($s/ancestor::doc/a) = 1,
            count($s/ancestor::doc/a/x) = 0,
            snapshot()/*/name() = 'doc',
            count($s/preceding-sibling::node()) = 0,
            count($s/following-sibling::node()) = 0,
            count($s/parent::a/@id) = 1,
            count($s/parent::a/child::node()) = 1,
            $s/parent::a/parent::doc/@id
          )"/>
        </xsl:template>
        """
    )
    result = transform(body, input_source=SNAPSHOT_SOURCE)

    assert [item.string_value for item in result.principal] == [
        "b",
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
        "true",
        "d",
    ]


def test_snapshot_of_an_attribute_grafts_a_shallow_parent() -> None:
    body = stylesheet(
        """
        <xsl:template match="/">
          <xsl:variable name="s" select="snapshot(/doc/a/@id)"/>
          <xsl:sequence select="(
            $s instance of attribute(id),
            $s/string(),
            count($s/parent::a) = 1,
            count($s/parent::a/child::node()) = 0,
            count($s/parent::a/@*) = 1,
            count($s/parent::a/parent::doc) = 1
          )"/>
        </xsl:template>
        """
    )
    result = transform(body, input_source=SNAPSHOT_SOURCE)

    assert [item.string_value for item in result.principal] == [
        "true",
        "1",
        "true",
        "true",
        "true",
        "true",
    ]


def test_snapshot_of_a_namespace_node_keeps_its_element_parent() -> None:
    body = stylesheet(
        """
        <xsl:template match="/">
          <xsl:variable name="s" select="snapshot(/doc/a/namespace::my)"/>
          <xsl:sequence select="(
            $s instance of namespace-node(),
            local-name($s),
            string($s),
            count($s/parent::a) = 1,
            count($s/parent::a/child::node()) = 0
          )"/>
        </xsl:template>
        """,
        namespaces={"my": MY_NS},
    )
    namespaced = b'<doc xmlns:my="urn:my"><a id="1"><b>deep</b></a></doc>'
    result = transform(body, input_source=namespaced)

    assert [item.string_value for item in result.principal] == [
        "true",
        "my",
        MY_NS,
        "true",
        "true",
    ]


def test_snapshot_leaves_atomic_and_function_items_unchanged() -> None:
    body = stylesheet(
        """
        <xsl:template name="main">
          <xsl:sequence select="(snapshot(1), snapshot('a'), local-name-from-QName(
            function-name(let $f := snapshot(abs#1) return $f)))"/>
        </xsl:template>
        """
    )
    result = transform(body, initial_template="main", delivery="raw")

    assert [item.string_value for item in result.principal] == ["1", "a", "abs"]


def test_snapshot_without_a_context_item_is_xpdy0002() -> None:
    body = stylesheet(
        """
        <xsl:function name="my:copy">
          <xsl:param name="n"/>
          <xsl:sequence select="snapshot()"/>
        </xsl:function>
        <xsl:template match="/">
          <xsl:sequence select="my:copy(.)"/>
        </xsl:template>
        """,
        namespaces={"my": MY_NS},
    )
    assert error_code(lambda: transform(body, input_source=b"<doc/>")) == "XPDY0002"


def test_root_returns_the_tree_root_of_a_node_outside_the_focus() -> None:
    """F&O 3.1 section 13.5 defines root($arg) as its ancestor-or-self root."""
    body = stylesheet(
        """
        <xsl:template match="/">
          <xsl:variable name="copy" select="copy-of(/doc/a)"/>
          <xsl:sequence select="(
            $copy/b/root() is $copy,
            root($copy/b) is $copy,
            root(/) is /,
            $copy/b/root()/name() = 'a',
            empty(root(()))
          )"/>
        </xsl:template>
        """
    )
    result = transform(body, input_source=SNAPSHOT_SOURCE)

    assert result.principal == (
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
    )


def test_root_argument_and_context_errors() -> None:
    def code(body: str) -> str | None:
        return error_code(lambda: transform(stylesheet(body), input_source=b"<doc/>"))

    assert code('<xsl:template match="/"><xsl:value-of select="root(1)"/></xsl:template>') == (
        "XPTY0004"
    )
    assert (
        code('<xsl:template match="/"><xsl:value-of select="root((/, /doc))"/></xsl:template>')
        == "XPTY0004"
    )
    absent_context = stylesheet(
        """
        <xsl:function name="my:r"><xsl:param name="n"/>
          <xsl:sequence select="root()"/>
        </xsl:function>
        <xsl:template match="/"><xsl:sequence select="my:r(.)"/></xsl:template>
        """,
        namespaces={"my": MY_NS},
    )
    assert error_code(lambda: transform(absent_context, input_source=b"<doc/>")) == "XPDY0002"


def test_snapshot_of_a_node_in_a_parentless_tree_stops_at_its_root() -> None:
    """A copy's top ancestor is an element when the source tree has no document."""
    body = stylesheet(
        """
        <xsl:template match="/">
          <xsl:variable name="tree" select="copy-of(/doc/a)"/>
          <xsl:variable name="s" select="snapshot($tree/b)"/>
          <xsl:sequence select="(
            $s/name(),
            $s/root() is $s/parent::a,
            count($s/ancestor::node()) = 1,
            count($s/ancestor::doc) = 0,
            count($s/parent::a/ancestor::doc) = 0,
            $s/parent::a/@id
          )"/>
        </xsl:template>
        """
    )
    result = transform(body, input_source=SNAPSHOT_SOURCE)

    assert [item.string_value for item in result.principal] == [
        "b",
        "true",
        "true",
        "true",
        "true",
        "1",
    ]


def test_parse_xml_returns_an_engine_document_node() -> None:
    """F&O 3.1 section 14.7.1: parse-xml builds an XDM document, not empty.

    The returned document must survive the bridge into an XSLT variable: the
    substrate builds its own tree, which the XDM conversion cannot represent,
    so an unhandled result was silently dropped.
    """
    body = stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="doc" select="parse-xml('&lt;doc a=&quot;1&quot;&gt;&lt;x/&gt;&lt;x/&gt;&lt;/doc&gt;')"/>
          <xsl:sequence select="(
            $doc instance of document-node(element(doc)),
            count($doc//x) = 2,
            name($doc/*),
            string($doc/*/@a),
            empty(parse-xml(()))
          )"/>
        </xsl:template>
        """
    )
    result = transform(body, initial_template="main", delivery="raw")

    assert result.principal == (
        XdmAtomic.boolean(True),
        XdmAtomic.boolean(True),
        XdmAtomic.string("doc"),
        XdmAtomic.string("1"),
        XdmAtomic.boolean(True),
    )

    direct = stylesheet(
        '<xsl:template name="main">'
        '<xsl:sequence select="name(parse-xml(&quot;&lt;doc&gt;&lt;x/&gt;&lt;/doc&gt;&quot;)/*)"/>'
        "</xsl:template>"
    )
    assert transform(direct, initial_template="main", delivery="raw").principal == (
        XdmAtomic.string("doc"),
    )


def test_format_date_family_rejects_string_values_without_a_cast() -> None:
    """Only xs:untypedAtomic receives the cast; xs:string is XPTY0004."""
    for expression in (
        "format-date('2000-01-01', '[Y]')",
        "format-time('01:02:03', '[H01]')",
        "format-dateTime('2000-01-01T01:02:03', '[Y]')",
    ):
        body = f'<xsl:template name="main"><xsl:value-of select="{expression}"/></xsl:template>'
        assert error_code(
            lambda body=body: transform(stylesheet(body), initial_template="main")
        ) == ("XPTY0004")

    accepted = stylesheet(
        """
        <xsl:template name="main"><xsl:value-of
          select="format-date(xs:untypedAtomic('2000-01-01'), '[Y]')"/></xsl:template>
        """
    )
    assert transform(accepted, initial_template="main").principal[0].string_value == "2000"

    bad_cast = stylesheet(
        """
        <xsl:template name="main"><xsl:value-of
          select="format-date(xs:untypedAtomic('not-a-date'), '[Y]')"/></xsl:template>
        """
    )
    assert error_code(lambda: transform(bad_cast, initial_template="main")) == "FORG0001"


def test_parse_xml_error_codes() -> None:
    def code(expression: str) -> str | None:
        body = f'<xsl:template name="main"><xsl:sequence select="{expression}"/></xsl:template>'
        return error_code(lambda: transform(stylesheet(body), initial_template="main"))

    assert code("parse-xml('&lt;broken')") == "FODC0006"
    assert code("parse-xml(1)") == "XPTY0004"
    assert code("parse-xml(('&lt;a/&gt;', '&lt;b/&gt;'))") == "XPTY0004"


def test_format_integer_empty_atomization_formats_as_empty_string(tmp_path: Path) -> None:
    """An element-only typed value has no atomic value, not an IndexError."""
    schema = tmp_path / "element-only.xsd"
    schema.write_text(
        """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
             <xs:element name="a">
               <xs:complexType>
                 <xs:sequence><xs:element name="b" type="xs:string"/></xs:sequence>
               </xs:complexType>
             </xs:element>
           </xs:schema>""",
        encoding="utf-8",
    )
    pool = SchemaPool.from_imports([schema], xsd_version="1.1")
    document = parse_typed_document(b"<a><b>x</b></a>", schema_pool=pool, validation="strict")
    engine = XPathEngine("3.1")
    compiled = engine.compile("format-integer(., 'A')", XPathStaticContext())

    result = engine.evaluate(
        compiled, ExpressionEnvironment(), focus=Focus(item=document.root_element)
    )

    assert result[0].string_value == ""


def test_format_integer_rejects_one_node_atomizing_to_several_values(tmp_path: Path) -> None:
    """A list-typed element atomizes to two values; the first is not formatted."""
    schema = tmp_path / "list.xsd"
    schema.write_text(
        """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
             <xs:element name="a">
               <xs:simpleType><xs:list itemType="xs:integer"/></xs:simpleType>
             </xs:element>
           </xs:schema>""",
        encoding="utf-8",
    )
    pool = SchemaPool.from_imports([schema], xsd_version="1.0")
    document = parse_typed_document(b"<a>1 2</a>", schema_pool=pool, validation="strict")
    body = stylesheet(
        """
        <xsl:template name="main">
          <xsl:value-of select="format-integer((/a)[1], '1')"/>
        </xsl:template>
        """
    )
    assert (
        error_code(
            lambda: transform(
                body, input_source=document, initial_template="main", validation="preserve"
            )
        )
        == "XPTY0004"
    )


def test_format_integer_keeps_its_compiled_argument() -> None:
    """Evaluating one compiled token must not replace its operand."""
    parser = _build_parser("3.1", XPathStaticContext(), key=True)
    token = parser.parse("format-integer($n, 'A')")
    operand = token._items[0]

    assert token.evaluate(XPathContext(item="", variables={"n": 1})) == "A"
    assert token.evaluate(XPathContext(item="", variables={"n": 2})) == "B"
    assert token._items[0] is operand
