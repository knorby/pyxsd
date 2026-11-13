"""Immutable Python extension functions at the XSLT boundary.

The registry is a value: registering a function returns a new registry, a
compiled stylesheet keeps the snapshot it was compiled with, and a later
registration never changes compiled expressions, availability answers, or an
already running transformation.  Callbacks receive typed host values, never a
stylesheet-identified Python module.
"""

from __future__ import annotations

import concurrent.futures
import io
import threading
from typing import Any

import pytest
from tests.xslt.support import stylesheet

from pyxsd.xdm import XdmArray, XdmAtomic, XdmFunction, XdmMap
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xpath.engine import XPathEngine
from pyxsd.xpath.environment import ExpressionEnvironment, XPathStaticContext
from pyxsd.xslt import (
    ExtensionSignature,
    XsltDynamicError,
    XsltExtensions,
    XsltProcessor,
    no_args,
    xs_integer_to_integer,
)
from pyxsd.xslt.errors import XsltStaticError

APP = "urn:app"


def app(local: str) -> str:
    return f"{{{APP}}}{local}"


def double(value: int) -> int:
    return value * 2


def late() -> str:
    return "late"


def compile_body(
    body: str,
    *,
    extensions: XsltExtensions | None = None,
    namespaces: dict[str, str] | None = None,
) -> Any:
    source = stylesheet(
        body,
        namespaces={"app": APP} if namespaces is None else namespaces,
    )
    return XsltProcessor().compile(io.BytesIO(source), extensions=extensions)


def initial(select: str) -> str:
    return (
        '<xsl:template name="xsl:initial-template">'
        f'<xsl:sequence select="{select}"/>'
        "</xsl:template>"
    )


def test_extension_snapshot_and_typed_conversion() -> None:
    extensions = XsltExtensions().function(app("double"), 1, xs_integer_to_integer)(double)
    compiled = compile_body(initial("app:double(4)"), extensions=extensions)
    extensions2 = extensions.function(app("late"), 0, no_args)(late)

    assert compiled.transform(delivery="raw").principal == (XdmAtomic.integer(8),)
    assert not compiled.function_available(app("late"), 0)
    assert compiled.function_available(app("double"), 1)
    assert extensions2.function_available(app("late"), 0)
    assert not extensions.function_available(app("late"), 0)

    late_compiled = compile_body(initial("app:late()"), extensions=extensions2)
    assert late_compiled.transform(delivery="raw").principal == (XdmAtomic.string("late"),)


def test_compile_rejects_a_non_registry_extension_value() -> None:
    source = stylesheet(initial("1"), namespaces={"app": APP})
    with pytest.raises(TypeError, match="XsltExtensions"):
        XsltProcessor().compile(io.BytesIO(source), extensions=object())  # type: ignore[arg-type]


def test_compiled_snapshot_rejects_a_non_registry_value() -> None:
    from dataclasses import replace

    compiled = compile_body(initial("1"), extensions=XsltExtensions())
    with pytest.raises(TypeError, match="XsltExtensions"):
        replace(compiled, extensions=object())


def test_registry_is_immutable_and_freezes_its_mappings() -> None:
    empty = XsltExtensions()
    extensions = empty.function(app("double"), 1, xs_integer_to_integer)(double)

    assert empty.functions == {}
    assert tuple(extensions.functions) == ((app("double"), 1),)
    with pytest.raises(TypeError):
        extensions.functions[(app("other"), 0)] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        extensions.collations["urn:app:other"] = None  # type: ignore[index]

    with pytest.raises(ValueError, match="already registered"):
        extensions.register_function(app("double"), 1, double, signature=xs_integer_to_integer)


def test_registration_rejects_bad_names_arities_and_signatures() -> None:
    with pytest.raises(ValueError, match="expanded name"):
        XsltExtensions().register_function("double", 1, double)
    with pytest.raises(ValueError, match="expanded name"):
        XsltExtensions().register_function("{urn:app", 1, double)
    with pytest.raises(ValueError, match="expanded name"):
        XsltExtensions().register_function("Q{urn:app", 1, double)
    with pytest.raises(ValueError, match="arity"):
        XsltExtensions().register_function(app("f"), -1, double)
    with pytest.raises(TypeError, match="callable"):
        XsltExtensions().register_function(app("f"), 0, object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="parameters"):
        XsltExtensions().register_function(app("f"), 1, double)
    with pytest.raises(ValueError, match="unsupported"):
        XsltExtensions().register_function(
            app("f"),
            1,
            double,
            signature=ExtensionSignature(("bogus()",), "xs:integer"),
        )
    with pytest.raises(ValueError, match="unsupported"):
        XsltExtensions().register_function(
            app("f"),
            0,
            late,
            signature=ExtensionSignature((), "bogus()"),
        )


def test_prefixed_registration_resolves_against_stylesheet_namespaces() -> None:
    extensions = XsltExtensions().register_function(
        "app:double", 1, double, signature=xs_integer_to_integer
    )
    compiled = compile_body(initial("app:double(5)"), extensions=extensions)

    assert compiled.transform(delivery="raw").principal == (XdmAtomic.integer(10),)
    assert compiled.function_available(app("double"), 1)
    assert extensions.function_available("app:double", 1)


def test_unbound_registration_prefix_is_a_static_error() -> None:
    extensions = XsltExtensions().register_function(
        "other:double", 1, double, signature=xs_integer_to_integer
    )

    with pytest.raises(XsltStaticError) as caught:
        compile_body(initial("1"), extensions=extensions)
    assert caught.value.code == "XTSE0280"


def test_extension_conflicting_with_a_stylesheet_function_is_static() -> None:
    extensions = XsltExtensions().function(app("double"), 1, xs_integer_to_integer)(double)
    body = (
        '<xsl:function name="app:double" as="xs:integer">'
        '<xsl:param name="value" as="xs:integer"/>'
        '<xsl:sequence select="$value * 100"/>'
        "</xsl:function>" + initial("app:double(4)")
    )

    with pytest.raises(XsltStaticError) as caught:
        compile_body(body, extensions=extensions)
    assert caught.value.code == "XTSE0010"


def test_function_available_uses_the_compiled_snapshot() -> None:
    extensions = XsltExtensions().function(app("double"), 1, xs_integer_to_integer)(double)
    compiled = compile_body(
        initial(
            "string-join(("
            "function-available('app:double', 1), "
            "function-available('app:late', 0), "
            "function-available('app:double')"
            "), ':')"
        ),
        extensions=extensions,
    )
    assert compiled.transform(delivery="raw").principal == (XdmAtomic.string("true:false:true"),)


def test_arity_and_runtime_type_errors_are_xty0004() -> None:
    extensions = XsltExtensions().function(app("double"), 1, xs_integer_to_integer)(double)
    compiled = compile_body(initial("app:double('four')"), extensions=extensions)

    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(delivery="raw")
    assert caught.value.code == "XPTY0004"


def compile_uri_stylesheet(extensions: XsltExtensions) -> Any:
    return compile_body(initial("app:uri()"), extensions=extensions)


def test_declared_result_is_converted_to_any_uri() -> None:
    extensions = XsltExtensions().register_function(
        "{urn:app}uri",
        0,
        lambda: "urn:example",
        signature=ExtensionSignature((), "xs:anyURI"),
    )
    result = compile_uri_stylesheet(extensions=extensions).transform()
    assert result.principal[0].type_name == "{http://www.w3.org/2001/XMLSchema}anyURI"
    assert result.principal[0].lexical == "urn:example"


def test_declared_float_result_is_converted_through_a_stylesheet() -> None:
    extensions = XsltExtensions().register_function(
        app("signal"),
        0,
        lambda: 1.5,
        signature=ExtensionSignature((), "xs:float"),
    )
    compiled = compile_body(initial("app:signal()"), extensions=extensions)

    principal = compiled.transform(delivery="raw").principal
    assert principal[0].type_name == "{http://www.w3.org/2001/XMLSchema}float"
    assert principal[0] == XdmAtomic.float(1.5)


def test_uncastable_declared_result_is_xpty0004_through_a_stylesheet() -> None:
    extensions = XsltExtensions().register_function(
        app("stamp"),
        0,
        lambda: "not-a-date",
        signature=ExtensionSignature((), "xs:date"),
    )
    compiled = compile_body(initial("app:stamp()"), extensions=extensions)

    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(delivery="raw")
    assert caught.value.code == "XPTY0004"


def test_wrong_declared_result_cardinality_is_xpty0004_through_a_stylesheet() -> None:
    def pair() -> tuple[int, int]:
        return (1, 2)

    extensions = XsltExtensions().register_function(
        app("pair"),
        0,
        pair,
        signature=ExtensionSignature((), "xs:integer"),
    )
    compiled = compile_body(initial("app:pair()"), extensions=extensions)

    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(delivery="raw")
    assert caught.value.code == "XPTY0004"


def test_declared_node_and_map_results_keep_their_identity() -> None:
    document = XdmDocument.parse(b"<root/>")
    mapping = XdmMap.from_mapping({"a": 1})

    node_extensions = XsltExtensions().register_function(
        app("root"),
        0,
        lambda: document.root_element,
        signature=ExtensionSignature((), "node()"),
    )
    node_compiled = compile_body(initial("app:root()"), extensions=node_extensions)
    assert node_compiled.transform(delivery="raw").principal == (document.root_element,)

    map_extensions = XsltExtensions().register_function(
        app("table"),
        0,
        lambda: mapping,
        signature=ExtensionSignature((), "map(*)"),
    )
    map_compiled = compile_body(initial("app:table()"), extensions=map_extensions)
    delivered = map_compiled.transform(delivery="raw").principal
    assert isinstance(delivered[0], XdmMap)
    assert delivered[0].size == 1
    assert delivered[0].get(XdmAtomic.string("a")) == (XdmAtomic.integer(1),)


def test_none_result_is_never_the_empty_sequence() -> None:
    def none_result(value: int) -> None:
        return None

    extensions = XsltExtensions().function(app("none"), 1, xs_integer_to_integer)(none_result)
    compiled = compile_body(initial("app:none(1)"), extensions=extensions)

    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(delivery="raw")
    assert caught.value.code == "XPTY0004"
    assert "None" in str(caught.value)

    def empty_result(value: int) -> tuple[()]:
        del value
        return ()

    empty = XsltExtensions().register_function(
        app("empty"),
        1,
        empty_result,
        signature=ExtensionSignature(("xs:integer",), "xs:integer?"),
    )
    assert (
        compile_body(initial("app:empty(1)"), extensions=empty).transform(delivery="raw").principal
        == ()
    )


def test_callback_exceptions_preserve_cause_and_disable_catching() -> None:
    def boom(value: int) -> int:
        raise RuntimeError(f"callback exploded on {value}")

    extensions = XsltExtensions().function(app("boom"), 1, xs_integer_to_integer)(boom)
    compiled = compile_body(initial("app:boom(3)"), extensions=extensions)

    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(delivery="raw")
    error = caught.value
    assert error.code is None
    assert error.allow_catch is False
    assert isinstance(error.cause, RuntimeError)
    assert error.__cause__ is error.cause
    assert "callback exploded on 3" in str(error)


def test_contextual_and_non_contextual_callbacks_are_distinct() -> None:
    seen: list[Any] = []

    def contextual(context_item: Any, value: str) -> str:
        seen.append(context_item)
        return f"{context_item.local_name}:{value}"

    def plain(value: str) -> str:
        seen.append(("plain", value))
        return value.upper()

    contextual_extensions = XsltExtensions().register_function(
        app("describe"),
        1,
        contextual,
        signature=ExtensionSignature(("xs:string",), "xs:string"),
        contextual=True,
    )
    plain_extensions = XsltExtensions().register_function(
        app("describe"),
        1,
        plain,
        signature=ExtensionSignature(("xs:string",), "xs:string"),
    )

    contextual_compiled = compile_body(
        '<xsl:template match="root"><xsl:sequence select="app:describe(\'x\')"/></xsl:template>',
        extensions=contextual_extensions,
    )
    plain_compiled = compile_body(
        '<xsl:template match="root"><xsl:sequence select="app:describe(\'x\')"/></xsl:template>',
        extensions=plain_extensions,
    )
    source = XdmDocument.parse(b"<root/>")

    assert contextual_compiled.transform(source=source, delivery="raw").principal == (
        XdmAtomic.string("root:x"),
    )
    assert plain_compiled.transform(source=source, delivery="raw").principal == (
        XdmAtomic.string("X"),
    )
    assert [item.local_name for item in seen if not isinstance(item, tuple)] == ["root"]


def test_contextual_extension_sees_an_absent_context_item() -> None:
    seen: list[Any] = []

    def contextual(context_item: Any) -> str:
        seen.append(context_item)
        return "gone" if context_item is None else "present"

    extensions = XsltExtensions().register_function(
        app("context"),
        0,
        contextual,
        signature=ExtensionSignature((), "xs:string"),
        contextual=True,
    )
    compiled = compile_body(
        '<xsl:template name="xsl:initial-template">'
        '<xsl:sequence select="app:context()"/>'
        "</xsl:template>",
        extensions=extensions,
    )

    assert compiled.transform(delivery="raw").principal == (XdmAtomic.string("gone"),)
    assert seen == [None]


def test_contextual_extension_observes_the_live_predicate_focus() -> None:
    seen: list[Any] = []

    def current(context_item: Any, value: Any) -> Any:
        del value
        seen.append(context_item)
        return context_item

    extensions = XsltExtensions().register_function(
        app("cur"),
        1,
        current,
        signature=ExtensionSignature(("item()",), "item()"),
        contextual=True,
    )
    compiled = compile_body(
        '<xsl:template name="xsl:initial-template">'
        '<xsl:sequence select="(1, 2, 3)[app:cur(.) = 2]"/>'
        "</xsl:template>",
        extensions=extensions,
    )

    assert compiled.transform(delivery="raw").principal == (XdmAtomic.integer(2),)
    assert seen == [
        XdmAtomic.integer(1),
        XdmAtomic.integer(2),
        XdmAtomic.integer(3),
    ]


def compile_current_item_stylesheet(select: str, seen: list[Any] | None = None) -> Any:
    """Compile a stylesheet whose contextual ``app:current-item`` echoes the focus."""

    def current_item(context_item: Any) -> Any:
        if seen is not None:
            seen.append(context_item)
        return context_item

    extensions = XsltExtensions().register_function(
        app("current-item"),
        0,
        current_item,
        signature=ExtensionSignature((), "item()"),
        contextual=True,
    )
    return compile_body(initial(select), extensions=extensions)


def test_contextual_function_item_observes_each_predicate_item() -> None:
    result = compile_current_item_stylesheet(
        "let $f := app:current-item#0 return (1, 2, 3)[$f() = 2]"
    ).transform()
    assert result.principal == (XdmAtomic.integer(2),)


def test_contextual_function_item_records_each_predicate_item() -> None:
    seen: list[Any] = []
    result = compile_current_item_stylesheet(
        "let $f := app:current-item#0 return (1, 2, 3)[$f() = 2]", seen
    ).transform()
    assert result.principal == (XdmAtomic.integer(2),)
    assert seen == [
        XdmAtomic.integer(1),
        XdmAtomic.integer(2),
        XdmAtomic.integer(3),
    ]


def test_saved_function_item_observes_each_path_step_item() -> None:
    seen: list[Any] = []

    def current_item(context_item: Any) -> Any:
        seen.append(context_item)
        return context_item

    extensions = XsltExtensions().register_function(
        app("current-item"),
        0,
        current_item,
        signature=ExtensionSignature((), "item()"),
        contextual=True,
    )
    body = (
        '<xsl:template match="root">'
        '<xsl:sequence select="let $f := app:current-item#0 return */$f()"/>'
        "</xsl:template>"
    )
    compiled = compile_body(body, extensions=extensions)
    source = XdmDocument.parse(b"<root><a>1</a><b>2</b></root>")

    result = compiled.transform(source=source, delivery="raw").principal
    assert [item.local_name for item in result] == ["a", "b"]
    assert [item.local_name for item in seen] == ["a", "b"]


def test_contextual_function_item_with_absent_context_item_sees_none() -> None:
    seen: list[Any] = []

    def focus_state(context_item: Any) -> str:
        seen.append(context_item)
        return "absent" if context_item is None else "present"

    extensions = XsltExtensions().register_function(
        app("focus-state"),
        0,
        focus_state,
        signature=ExtensionSignature((), "xs:string"),
        contextual=True,
    )
    compiled = compile_body(
        initial("let $f := app:focus-state#0 return $f()"),
        extensions=extensions,
    )

    assert compiled.transform(delivery="raw").principal == (XdmAtomic.string("absent"),)
    assert seen == [None]


def test_extension_and_stylesheet_function_share_a_name_across_arities() -> None:
    extensions = XsltExtensions().function(app("f"), 1, xs_integer_to_integer)(double)
    body = (
        '<xsl:function name="app:f" as="xs:integer">'
        '<xsl:param name="left" as="xs:integer"/>'
        '<xsl:param name="right" as="xs:integer"/>'
        '<xsl:sequence select="$left + $right"/>'
        "</xsl:function>" + initial("string-join((string(app:f(2)), string(app:f(2, 3))), ':')")
    )
    compiled = compile_body(body, extensions=extensions)

    assert compiled.function_available(app("f"), 1) is True
    assert compiled.function_available(app("f"), 2) is True
    assert compiled.transform(delivery="raw").principal == (XdmAtomic.string("4:5"),)


def test_function_available_agrees_for_lexical_and_expanded_names() -> None:
    extensions = XsltExtensions().function(app("double"), 1, xs_integer_to_integer)(double)
    compiled = compile_body(
        '<xsl:function name="app:sum" as="xs:integer">'
        '<xsl:param name="left" as="xs:integer"/>'
        '<xsl:param name="right" as="xs:integer"/>'
        '<xsl:sequence select="$left + $right"/>'
        "</xsl:function>" + initial("app:double(2)"),
        extensions=extensions,
    )

    assert compiled.function_available("app:double", 1) is True
    assert compiled.function_available(app("double"), 1) is True
    assert compiled.function_available("app:sum", 2) is True
    assert compiled.function_available(app("sum"), 2) is True
    assert compiled.function_available("app:double", 2) is False
    assert compiled.function_available(app("double"), 2) is False
    assert compiled.function_available("app:absent", 1) is False
    assert compiled.function_available(app("absent"), 1) is False
    assert compiled.function_available("missing:prefix", 1) is False


def test_registry_function_available_accepts_expanded_and_lexical_names() -> None:
    extensions = XsltExtensions().function(app("double"), 1, xs_integer_to_integer)(double)
    prefixed = extensions.register_function("app:late", 0, late, no_args)

    assert extensions.function_available(app("double")) is True
    assert extensions.function_available(app("double"), 0) is False
    assert extensions.function_available(app("absent")) is False
    assert prefixed.function_available("app:late") is True
    assert prefixed.function_available("app:late", 0) is True
    assert prefixed.function_available("") is False
    assert prefixed.function_available("{broken") is False
    assert prefixed.function_available("app:") is False


def test_duplicate_resolution_is_a_static_error() -> None:
    extensions = (
        XsltExtensions()
        .register_function("app:double", 1, double, xs_integer_to_integer)
        .register_function(app("double"), 1, double, xs_integer_to_integer)
    )

    with pytest.raises(XsltStaticError) as caught:
        compile_body(initial("1"), extensions=extensions)
    assert caught.value.code == "XTSE0010"


def test_extension_backed_stylesheet_uses_the_full_evaluator() -> None:
    calls: list[int] = []

    def double_count(value: int) -> int:
        calls.append(value)
        return value * 2

    extensions = XsltExtensions().function(app("double"), 1, xs_integer_to_integer)(double_count)
    compiled = compile_body(initial("app:double(4)"), extensions=extensions)

    first = compiled.transform(delivery="raw")
    second = compiled.transform(delivery="raw")

    assert first.principal == second.principal == (XdmAtomic.integer(8),)
    assert calls == [4, 4]


def test_concurrent_invocations_use_separate_compiled_snapshots() -> None:
    def double(value: int) -> int:
        return value * 2

    def triple(value: int) -> int:
        return value * 3

    first = XsltExtensions().function(app("scale"), 1, xs_integer_to_integer)(double)
    second = XsltExtensions().function(app("scale"), 1, xs_integer_to_integer)(triple)
    first_compiled = compile_body(initial("app:scale(5)"), extensions=first)
    second_compiled = compile_body(initial("app:scale(5)"), extensions=second)
    assert first_compiled.function_available(app("scale"), 1)
    assert second_compiled.function_available(app("scale"), 1)

    count = 8
    barrier = threading.Barrier(count)

    def run(index: int) -> tuple[Any, ...]:
        compiled = first_compiled if index % 2 == 0 else second_compiled
        barrier.wait()
        return compiled.transform(delivery="raw").principal

    with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(run, index) for index in range(count)]
        results = [future.result() for future in futures]

    assert results == [
        (XdmAtomic.integer(10),) if index % 2 == 0 else (XdmAtomic.integer(15),)
        for index in range(count)
    ]


def test_standalone_engine_evaluates_extension_targets() -> None:
    extensions = XsltExtensions().function(app("double"), 1, xs_integer_to_integer)(double)
    static = XPathStaticContext(namespaces={"app": APP}, functions={app("double"): 1})
    engine = XPathEngine("3.1")
    compiled = engine.compile("app:double(6)", static)

    result = engine.evaluate(
        compiled,
        ExpressionEnvironment(
            functions={app("double"): extensions.runtime_targets()[app("double")]}
        ),
    )

    assert result == (XdmAtomic.integer(12),)


def test_host_target_base_and_bridge_validation() -> None:
    from pyxsd.xpath.functions import XdmFunctionTarget
    from pyxsd.xslt.runtime.functions import XsltContextBridge

    base = XdmFunctionTarget()
    with pytest.raises(NotImplementedError):
        base.invoke_xdm(())
    with pytest.raises(NotImplementedError):
        base(1)

    with pytest.raises(TypeError, match="non-empty"):
        XsltContextBridge(object(), extension_functions=frozenset({("", 0)}))
    with pytest.raises(TypeError, match="non-negative"):
        XsltContextBridge(object(), extension_functions=frozenset({(app("f"), -1)}))


def test_extension_values_cross_the_boundary_unchanged() -> None:
    node = XdmDocument.parse(b"<root><child/></root>")
    mapping = XdmMap.from_mapping({"a": 1})
    array = XdmArray((1, 2))
    function = XdmFunction(
        lambda arguments, environment, closure: XdmAtomic.string("called"),
        arity=0,
    )

    def collect(*values: Any) -> tuple[Any, ...]:
        return values

    extensions = XsltExtensions().register_function(
        app("collect"),
        4,
        collect,
        signature=ExtensionSignature(("node()", "map(*)", "array(*)", "function(*)"), "item()*"),
    )
    target = extensions.runtime_targets()[app("collect")]

    assert target.invoke_xdm(((node,), (mapping,), (array,), (function,))) == (
        node,
        mapping,
        array,
        function,
    )
