"""Standalone, schema-aware XPath expression evaluation.

These tests exercise the production expression environment directly: no
stylesheet, transformation, initial template, result builder, or mode is
created.  The environment is immutable, every evaluation gets fresh parser and
dynamic-context state, and Task 7 XDM values are preserved end to end.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import io
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from elementpath import XPathContext
from elementpath.datatypes import UntypedAtomic
from elementpath.xpath3 import XPath31Parser
from elementpath.xpath_tokens import XPathArray, XPathMap
from tests.xslt.support import stylesheet as _stylesheet

import pyxsd.xpath.engine as engine_module
from pyxsd.schema import Schema
from pyxsd.xdm import XdmArray, XdmAtomic, XdmFunction, XdmMap, XdmSequence, as_xdm
from pyxsd.xdm.nodes import NodeKind
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xpath import continuation
from pyxsd.xpath.continuation import ContinuationMachine, XPathClosure
from pyxsd.xpath.engine import XPathEngine, _build_parser
from pyxsd.xpath.environment import (
    ExpressionEnvironment,
    Focus,
    FocusKind,
    XPathStaticContext,
)
from pyxsd.xpath.functions import HostFunctionToken
from pyxsd.xslt import XsltProcessor

XSD_NS = "http://www.w3.org/2001/XMLSchema"
MONEY = "urn:money"
STATIC_CONTEXT = XPathStaticContext(variable_types={"items": "item()*", "f": "function(*)"})
NODE_CONTEXT = XPathStaticContext()


def increment(value: Any) -> Any:
    return value + 1


def _document(xml: bytes = b"<root><item>1</item><item>3</item></root>") -> Any:
    from pyxsd.xdm.nodes import XdmDocument

    return XdmDocument.parse(xml)


def _error_code(exc: BaseException) -> str:
    return str(getattr(exc, "code", "")).rsplit(":", 1)[-1]


def _run_threaded(function: Any, count: int = 8) -> list[Any]:
    barrier = threading.Barrier(count)

    def guarded(index: int) -> Any:
        try:
            return function(index, barrier)
        except BaseException:
            barrier.abort()
            raise

    with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(guarded, index) for index in range(count)]
        return [future.result() for future in futures]


def _money_schema(tmp_path: Path) -> SchemaPool:
    path = tmp_path / "money.xsd"
    path.write_text(
        f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{MONEY}"
            xmlns:m="{MONEY}" elementFormDefault="qualified">
          <xs:simpleType name="Amount">
            <xs:restriction base="xs:decimal"><xs:totalDigits value="8"/></xs:restriction>
          </xs:simpleType>
          <xs:element name="payment" type="m:Amount"/>
        </xs:schema>""",
        encoding="utf-8",
    )
    return SchemaPool.from_imports([path])


# ---------------------------------------------------------------------------
# Standalone evaluation and invocation isolation


def test_environment_reentrancy() -> None:
    engine = XPathEngine("3.1")
    compiled = engine.compile("for-each($items, $f)", STATIC_CONTEXT)
    first = ExpressionEnvironment(variables={"items": (1, 2), "f": increment})
    second = ExpressionEnvironment(variables={"items": (3,), "f": increment})

    assert engine.evaluate(compiled, first) == (XdmAtomic.integer(2), XdmAtomic.integer(3))
    assert engine.evaluate(compiled, second) == (XdmAtomic.integer(4),)
    assert engine.evaluate(compiled, first) == (XdmAtomic.integer(2), XdmAtomic.integer(3))


def test_standalone_evaluation_needs_no_stylesheet_state() -> None:
    engine = XPathEngine("3.1")
    compiled = engine.compile("1 + 1", NODE_CONTEXT)

    assert engine.evaluate(compiled, ExpressionEnvironment()) == (XdmAtomic.integer(2),)
    assert not hasattr(engine_module, "Transformation")


def test_compiled_expression_is_immutable_and_reusable() -> None:
    engine = XPathEngine("3.1")
    compiled = engine.compile("$n + 1", XPathStaticContext(variable_types={"n": "xs:integer"}))

    assert compiled.source == "$n + 1"
    with pytest.raises(AttributeError):
        compiled.source = "changed"  # type: ignore[misc]
    assert engine.evaluate(compiled, ExpressionEnvironment(variables={"n": 1})) == (
        XdmAtomic.integer(2),
    )
    assert engine.evaluate(compiled, ExpressionEnvironment(variables={"n": 5})) == (
        XdmAtomic.integer(6),
    )


def test_versions_select_a_versioned_parser() -> None:
    engine = XPathEngine("3.0")
    assert engine.version == "3.0"
    assert engine.parser_class.__name__ == "XPath30Parser"

    engine = XPathEngine("2")
    assert engine.version == "2.0"
    assert engine.parser_class.__name__ == "XPath2Parser"

    engine = XPathEngine("1")
    assert engine.version == "1.0"
    assert engine.parser_class.__name__ == "_LegacyXPath1Parser"

    with pytest.raises(ValueError, match="version"):
        XPathEngine("4.0")


def test_continuation_subset_uses_the_task5_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = XPathEngine("3.1")
    static = XPathStaticContext(
        variable_types={"n": "xs:integer", "f": "function(xs:integer) as xs:integer"}
    )
    compiled = engine.compile("$f($n) + 1", static)
    assert compiled.continuation_plan is not None

    runs: list[Any] = []
    original_run = ContinuationMachine.run

    def spy_run(self: ContinuationMachine) -> Any:
        runs.append(self)
        return original_run(self)

    monkeypatch.setattr(engine_module.ContinuationMachine, "run", spy_run)
    result = engine.evaluate(compiled, ExpressionEnvironment(variables={"n": 2, "f": increment}))

    assert result == (XdmAtomic.integer(4),)
    assert len(runs) == 1


def test_engine_closure_does_not_nest_continuation_machines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = XPathEngine("3.1")
    closure = engine.evaluate(engine.compile("function($x) { $x + 1 }"), ExpressionEnvironment())[0]
    assert isinstance(closure, XdmFunction)
    static = XPathStaticContext(variable_types={"f": "function(xs:integer) as xs:integer"})
    compiled = engine.compile("$f(2)", static)
    assert compiled.continuation_plan is not None

    runs: list[Any] = []
    original_run = ContinuationMachine.run

    def spy_run(self: ContinuationMachine) -> Any:
        runs.append(self)
        return original_run(self)

    monkeypatch.setattr(engine_module.ContinuationMachine, "run", spy_run)
    result = engine.evaluate(compiled, ExpressionEnvironment(variables={"f": closure}))

    assert result == (XdmAtomic.integer(3),)
    assert len(runs) == 1


def test_non_subset_expression_falls_back_in_absence_of_a_plan() -> None:
    engine = XPathEngine("3.1")
    compiled = engine.compile("count((1, 2, 3))", NODE_CONTEXT)

    assert compiled.continuation_plan is None
    assert engine.evaluate(compiled, ExpressionEnvironment()) == (XdmAtomic.integer(3),)


# ---------------------------------------------------------------------------
# Focus


def test_absent_focus_is_distinct_from_empty_focus() -> None:
    engine = XPathEngine("3.1")
    compiled = engine.compile(".", NODE_CONTEXT)

    with pytest.raises(Exception) as caught:
        engine.evaluate(compiled, ExpressionEnvironment())
    assert _error_code(caught.value) == "XPDY0002"

    assert engine.evaluate(compiled, ExpressionEnvironment(), focus=()) == ()
    assert (
        engine.evaluate(engine.compile("1 + 1", NODE_CONTEXT), ExpressionEnvironment(), focus=())
        == ()
    )


def test_absent_focus_rejects_position_and_last() -> None:
    engine = XPathEngine("3.1")
    for source in ("position()", "last()"):
        compiled = engine.compile(source, NODE_CONTEXT)
        with pytest.raises(Exception) as caught:
            engine.evaluate(compiled, ExpressionEnvironment())
        assert _error_code(caught.value) == "XPDY0002"


def test_singleton_focus_and_position() -> None:
    document = _document()
    item = document.root_element.children[0]
    engine = XPathEngine("3.1")
    compiled = engine.compile("position(), last(), .", NODE_CONTEXT)

    result = engine.evaluate(
        compiled,
        ExpressionEnvironment(),
        focus=Focus(item=item, position=2, size=5),
    )

    assert result == (XdmAtomic.integer(2), XdmAtomic.integer(5), item)


def test_focus_rejects_ambiguous_sequence() -> None:
    engine = XPathEngine("3.1")
    compiled = engine.compile(".", NODE_CONTEXT)
    with pytest.raises(ValueError, match="at most one"):
        engine.evaluate(compiled, ExpressionEnvironment(), focus=("a", "b"))


def test_document_focus_and_nested_predicates() -> None:
    document = _document(b"<root><item>1</item><item>3</item><item>2</item></root>")
    engine = XPathEngine("3.1")
    compiled = engine.compile("item[. > 1][position() = 1]", NODE_CONTEXT)

    result = engine.evaluate(compiled, ExpressionEnvironment(), focus=document.root_element)

    assert result == (document.root_element.children[1],)


def test_node_result_kinds_and_identity() -> None:
    document = _document(b"<root a='v'>text<!--comment--><?pi data?></root>")
    engine = XPathEngine("3.1")
    compiled = engine.compile("node()", NODE_CONTEXT)

    result = engine.evaluate(compiled, ExpressionEnvironment(), focus=document.root_element)

    assert [item.kind for item in result] == [
        NodeKind.TEXT,
        NodeKind.COMMENT,
        NodeKind.PROCESSING_INSTRUCTION,
    ]
    assert result[0] is document.root_element.children[0]
    assert engine.evaluate(
        engine.compile("@a", NODE_CONTEXT), ExpressionEnvironment(), focus=document.root_element
    ) == (document.root_element.attributes[0],)


# ---------------------------------------------------------------------------
# Stable dynamic values


def test_current_date_time_and_implicit_timezone_are_stable() -> None:
    engine = XPathEngine("3.1")
    environment = ExpressionEnvironment(
        current_datetime=dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.UTC),
        implicit_timezone="+02:00",
    )
    date_time = engine.compile("current-dateTime()", NODE_CONTEXT)
    timezone = engine.compile("implicit-timezone()", NODE_CONTEXT)

    first = engine.evaluate(date_time, environment)
    second = engine.evaluate(date_time, environment)

    assert first == second
    assert str(first[0]) == "2024-01-02T03:04:05UTC"
    assert engine.evaluate(timezone, environment) == engine.evaluate(timezone, environment)
    assert str(engine.evaluate(timezone, environment)[0]) == "PT2H"


def test_environment_defaults_are_stable_and_immutable() -> None:
    environment = ExpressionEnvironment(variables={"n": 1})
    assert environment.current_datetime.tzinfo is environment.timezone
    assert environment.current_date_time is environment.current_datetime

    with pytest.raises(TypeError):
        environment.variables["n"] = 2  # type: ignore[index]
    with pytest.raises(AttributeError):
        environment.current_datetime = dt.datetime.now()  # type: ignore[misc]


def test_environment_validates_its_configuration() -> None:
    with pytest.raises(TypeError, match="current_datetime"):
        ExpressionEnvironment(current_datetime="now")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone"):
        ExpressionEnvironment(implicit_timezone="+20:00")
    with pytest.raises(TypeError, match="key index"):
        ExpressionEnvironment(key_indexes={"bad": "not a mapping"})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="document"):
        ExpressionEnvironment(documents={"urn:x": b"<x/>"})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="collection"):
        ExpressionEnvironment(collections={"urn:x": ("not a node",)})  # type: ignore[dict-item]


def test_a_key_lookup_is_a_resource_outside_environment_equality_and_repr() -> None:
    from dataclasses import replace

    def lookup(context_item: Any, arguments: tuple[XdmSequence, ...]) -> XdmSequence:
        return ()

    plain = ExpressionEnvironment()
    resourceful = replace(plain, key_lookup=lookup)
    assert resourceful.key_lookup is lookup
    assert resourceful == plain
    assert "key_lookup" not in repr(resourceful)
    with pytest.raises(TypeError, match="key_lookup"):
        ExpressionEnvironment(key_lookup=object())  # type: ignore[arg-type]


def test_static_context_is_immutable_and_validated() -> None:
    static = XPathStaticContext(
        namespaces={"m": "urn:test"},
        variable_types={"n": "xs:integer"},
        functions={"m:f": 1},
    )
    with pytest.raises(TypeError):
        static.namespaces["m"] = "urn:changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        static.variable_types["n"] = "xs:string"  # type: ignore[index]
    with pytest.raises(TypeError):
        static.functions["m:f"] = 2  # type: ignore[index]

    with pytest.raises(TypeError, match="namespace"):
        XPathStaticContext(namespaces={"m": 1})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="arity"):
        XPathStaticContext(functions={"m:f": -1})
    with pytest.raises(ValueError, match="prefix"):
        XPathStaticContext(functions={"unprefixed": 1})


# ---------------------------------------------------------------------------
# Resources


def test_resources_are_explicit_and_denied_by_default() -> None:
    engine = XPathEngine("3.1")
    doc = engine.compile("doc('file:///etc/passwd')", NODE_CONTEXT)
    text = engine.compile("unparsed-text('file:///etc/passwd')", NODE_CONTEXT)
    collection = engine.compile("collection('urn:missing')", NODE_CONTEXT)

    with pytest.raises(Exception) as caught:
        engine.evaluate(doc, ExpressionEnvironment())
    assert _error_code(caught.value) == "FODC0002"

    with pytest.raises(Exception) as caught:
        engine.evaluate(text, ExpressionEnvironment())
    assert _error_code(caught.value) == "FOUT1170"

    with pytest.raises(Exception) as caught:
        engine.evaluate(collection, ExpressionEnvironment())
    assert _error_code(caught.value) == "FODC0002"


def test_explicit_documents_resources_and_collections_are_available() -> None:
    document = _document(b"<root><item>value</item></root>")
    engine = XPathEngine("3.1")
    environment = ExpressionEnvironment(
        documents={"urn:doc": document},
        collections={"urn:collection": (document.root_element,)},
        text_resources={"urn:text": "hello"},
        default_collection=(document.root_element,),
    )

    assert engine.evaluate(
        engine.compile("string(doc('urn:doc')/root/item)", NODE_CONTEXT), environment
    ) == (XdmAtomic.string("value"),)
    assert engine.evaluate(
        engine.compile("unparsed-text('urn:text')", NODE_CONTEXT), environment
    ) == (XdmAtomic.string("hello"),)
    assert engine.evaluate(
        engine.compile("count(collection('urn:collection'))", NODE_CONTEXT), environment
    ) == (XdmAtomic.integer(1),)
    assert engine.evaluate(engine.compile("count(collection())", NODE_CONTEXT), environment) == (
        XdmAtomic.integer(1),
    )


# ---------------------------------------------------------------------------
# Host functions, keys, and schema constructors


def test_declared_host_function_is_invocation_local() -> None:
    static = XPathStaticContext(namespaces={"m": "urn:test"}, functions={"m:increment": 1})
    engine = XPathEngine("3.1")
    compiled = engine.compile(
        "m:increment($n)",
        XPathStaticContext(
            namespaces=static.namespaces,
            variable_types={"n": "xs:integer"},
            functions=static.functions,
        ),
    )
    first = ExpressionEnvironment(variables={"n": 1}, functions={"m:increment": increment})
    second = ExpressionEnvironment(variables={"n": 4}, functions={"m:increment": increment})

    assert engine.evaluate(compiled, first) == (XdmAtomic.integer(2),)
    assert engine.evaluate(compiled, second) == (XdmAtomic.integer(5),)


def test_xdm_function_variables_are_callable() -> None:
    function = XdmFunction(
        lambda arguments, environment, closure: as_xdm(arguments[0][0].value + 1), arity=1
    )
    engine = XPathEngine("3.1")
    compiled = engine.compile(
        "$f($n)",
        XPathStaticContext(
            variable_types={"n": "xs:integer", "f": "function(xs:integer) as xs:integer"}
        ),
    )

    assert engine.evaluate(compiled, ExpressionEnvironment(variables={"n": 2, "f": function})) == (
        XdmAtomic.integer(3),
    )


def test_host_key_indexes() -> None:
    document = _document(b"<root><item id='a'>one</item><item id='b'>two</item></root>")
    first, second = document.root_element.children
    engine = XPathEngine("3.1")
    compiled = engine.compile("key('by-id', 'a')", NODE_CONTEXT)
    environment = ExpressionEnvironment(key_indexes={"by-id": {"a": (first,), "b": (second,)}})

    assert engine.evaluate(compiled, environment) == (first,)
    assert engine.evaluate(
        engine.compile("key('by-id', ('a', 'b'))", NODE_CONTEXT), environment
    ) == (
        first,
        second,
    )


def test_schema_constructors_and_typed_values(tmp_path: Path) -> None:
    pool = _money_schema(tmp_path)
    static = XPathStaticContext(namespaces={"m": MONEY}, schema=pool)
    engine = XPathEngine("3.1")
    compiled = engine.compile("m:Amount('12.5')", static)

    result = engine.evaluate(compiled, ExpressionEnvironment())

    assert result[0].type_name == f"{{{MONEY}}}Amount"
    assert result[0].lexical == "12.5"


def test_schema_aware_document_typed_values(tmp_path: Path) -> None:
    schema_path = tmp_path / "money.xsd"
    schema_path.write_text(
        f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{MONEY}"
            xmlns:m="{MONEY}" elementFormDefault="qualified">
          <xs:element name="payment" type="xs:decimal"/>
        </xs:schema>""",
        encoding="utf-8",
    )
    schema = Schema.compile(io.StringIO(schema_path.read_text(encoding="utf-8")))
    document = schema.parse(
        io.BytesIO(f'<payment xmlns="{MONEY}">12.5</payment>'.encode()),
        capture_xdm=True,
    )
    pool = SchemaPool.from_schema(schema)
    engine = XPathEngine("3.1")
    compiled = engine.compile(
        "payment + 1", XPathStaticContext(schema=pool, default_namespace=MONEY)
    )

    result = engine.evaluate(
        compiled,
        ExpressionEnvironment(),
        focus=document.to_xdm(),
    )

    assert result == (XdmAtomic.decimal(Decimal("13.5")),)


# ---------------------------------------------------------------------------
# XDM result semantics


def test_xdm_sequences_do_not_collapse_strings_or_empty_sequences() -> None:
    engine = XPathEngine("3.1")
    assert engine.evaluate(engine.compile("()", NODE_CONTEXT), ExpressionEnvironment()) == ()
    assert engine.evaluate(engine.compile("'ab'", NODE_CONTEXT), ExpressionEnvironment()) == (
        XdmAtomic.string("ab"),
    )
    assert engine.evaluate(engine.compile("('a', 'b')", NODE_CONTEXT), ExpressionEnvironment()) == (
        XdmAtomic.string("a"),
        XdmAtomic.string("b"),
    )


def test_xdm_maps_arrays_and_function_items_are_preserved() -> None:
    engine = XPathEngine("3.1")
    map_result = engine.evaluate(
        engine.compile("map{'a': 1}", NODE_CONTEXT), ExpressionEnvironment()
    )
    array_result = engine.evaluate(engine.compile("[1, 2]", NODE_CONTEXT), ExpressionEnvironment())
    function_result = engine.evaluate(
        engine.compile("function($x) { $x + 1 }", NODE_CONTEXT), ExpressionEnvironment()
    )

    assert isinstance(map_result[0], XdmMap)
    assert map_result[0].get(XdmAtomic.string("a")) == (XdmAtomic.integer(1),)
    assert isinstance(array_result[0], XdmArray)
    assert array_result[0].get(2) == (XdmAtomic.integer(2),)
    assert isinstance(function_result[0], XdmFunction)
    assert function_result[0].invoke(((2,),)) == (XdmAtomic.integer(3),)


def test_function_results_are_copied_per_invocation() -> None:
    engine = XPathEngine("3.1")
    result = engine.evaluate(
        engine.compile(
            "function($x) { $x + $n }", XPathStaticContext(variable_types={"n": "xs:integer"})
        ),
        ExpressionEnvironment(variables={"n": 2}),
    )
    function = result[0]
    assert isinstance(function, XdmFunction)
    assert function.invoke(((3,),)) == (XdmAtomic.integer(5),)
    assert function.invoke(((4,),)) == (XdmAtomic.integer(6),)


# ---------------------------------------------------------------------------
# Context copies, concurrency, and parser isolation


def test_context_copies_do_not_mutate_the_environment() -> None:
    engine = XPathEngine("3.1")
    compiled = engine.compile("$n + 1", XPathStaticContext(variable_types={"n": "xs:integer"}))
    variables = {"n": 1}
    environment = ExpressionEnvironment(variables=variables)

    assert engine.evaluate(compiled, environment) == (XdmAtomic.integer(2),)
    assert engine.evaluate(compiled, environment) == (XdmAtomic.integer(2),)
    assert variables == {"n": 1}
    assert environment.variables is not variables


def test_concurrent_evaluation_uses_invocation_local_tokens() -> None:
    engine = XPathEngine("3.1")
    compiled = engine.compile(
        "for-each($items, $f)",
        XPathStaticContext(variable_types={"items": "item()*", "f": "function(*)"}),
    )
    environments = [
        ExpressionEnvironment(variables={"items": (index,), "f": increment}) for index in range(8)
    ]

    def evaluate(index: int, barrier: threading.Barrier) -> XdmSequence:
        barrier.wait()
        return engine.evaluate(compiled, environments[index])

    results = _run_threaded(evaluate)

    assert results == [(XdmAtomic.integer(index + 1),) for index in range(8)]


def test_parser_global_state_is_not_mutated() -> None:
    before = dict(XPath31Parser.symbol_table)
    before_signatures = dict(XPath31Parser.function_signatures)
    static = XPathStaticContext(namespaces={"m": "urn:test"}, functions={"m:increment": 1})
    engine = XPathEngine("3.1")
    compiled = engine.compile("m:increment(1)", static)
    engine.evaluate(compiled, ExpressionEnvironment(functions={"m:increment": increment}))

    assert XPath31Parser.symbol_table == before
    assert XPath31Parser.function_signatures == before_signatures

    with pytest.raises(Exception, match="unknown function"):
        engine.compile("m:increment(1)", XPathStaticContext(namespaces={"m": "urn:test"}))


def test_invalid_environment_and_compiled_types() -> None:
    engine = XPathEngine("3.1")
    compiled = engine.compile("1", NODE_CONTEXT)
    with pytest.raises(TypeError, match="ExpressionEnvironment"):
        engine.evaluate(compiled, object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="XPathStaticContext"):
        engine.compile("1", object())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Configuration and defensive contracts


def test_environment_rejects_non_mapping_inputs() -> None:
    with pytest.raises(TypeError, match="variables"):
        ExpressionEnvironment(variables=["n"])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="namespaces"):
        XPathStaticContext(namespaces=["m"])  # type: ignore[arg-type]


def test_environment_rejects_invalid_names_and_arity() -> None:
    with pytest.raises(TypeError, match="variable"):
        ExpressionEnvironment(variables={"": 1})
    with pytest.raises(TypeError, match="function"):
        ExpressionEnvironment(functions={"": increment})
    with pytest.raises(TypeError, match="function"):
        ExpressionEnvironment(functions={"m:f": object()})
    with pytest.raises(TypeError, match="key index"):
        ExpressionEnvironment(key_indexes={"": {}})
    with pytest.raises(TypeError, match="document"):
        ExpressionEnvironment(documents={"": _document()})
    with pytest.raises(TypeError, match="collection"):
        ExpressionEnvironment(collections={"": "bad"})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="text resource"):
        ExpressionEnvironment(text_resources={"": "x"})

    with pytest.raises(ValueError, match="arity"):
        XPathStaticContext(functions={"m:f": True})
    with pytest.raises(ValueError, match="arity"):
        XPathStaticContext(functions={"m:f": (2,)})
    with pytest.raises(ValueError, match="arity"):
        XPathStaticContext(functions={"m:f": (-1, None)})
    with pytest.raises(ValueError, match="arity"):
        XPathStaticContext(functions={"m:f": (2, 1)})
    with pytest.raises(ValueError, match="arity"):
        XPathStaticContext(functions={"m:f": "one"})


def test_static_context_rejects_invalid_optional_values() -> None:
    with pytest.raises(TypeError, match="variable"):
        XPathStaticContext(variable_types={"": "xs:string"})
    with pytest.raises(TypeError, match="variable types"):
        XPathStaticContext(variable_types={"n": 1})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="default_namespace"):
        XPathStaticContext(default_namespace=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="base_uri"):
        XPathStaticContext(base_uri=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="default_collation"):
        XPathStaticContext(default_collation=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="xsd_version"):
        XPathStaticContext(xsd_version="2.0")


def test_environment_rejects_invalid_resources_and_values() -> None:
    with pytest.raises(TypeError, match="key index"):
        ExpressionEnvironment(key_indexes={"k": {"a": "not nodes"}})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="XdmNode"):
        ExpressionEnvironment(key_indexes={"k": {"a": (object(),)}})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="collection"):
        ExpressionEnvironment(collections={"c": "not nodes"})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="XdmNode"):
        ExpressionEnvironment(collections={"c": (object(),)})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="default_collection"):
        ExpressionEnvironment(default_collection=(object(),))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="text resource"):
        ExpressionEnvironment(text_resources={"t": 1})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="current_datetime"):
        ExpressionEnvironment(current_datetime="now")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="base_uri"):
        ExpressionEnvironment(base_uri=1)  # type: ignore[arg-type]


def test_timezone_forms_and_naive_datetime() -> None:
    from datetime import timedelta
    from datetime import timezone as dt_timezone

    from elementpath.datatypes.datetime import Timezone

    named = Timezone(timedelta(hours=2))
    assert ExpressionEnvironment(implicit_timezone=named).timezone is named
    assert ExpressionEnvironment(implicit_timezone=timedelta(hours=3)).timezone.offset == timedelta(
        hours=3
    )
    assert ExpressionEnvironment(
        implicit_timezone=dt_timezone(timedelta(hours=4))
    ).timezone.offset == (timedelta(hours=4))
    naive = dt.datetime(2024, 1, 2, 3, 4, 5)
    environment = ExpressionEnvironment(current_datetime=naive, implicit_timezone="+01:00")
    assert environment.current_datetime.tzinfo is not None
    assert environment.current_date_time == environment.current_datetime
    assert environment.timezone.offset == timedelta(hours=1)

    with pytest.raises(ValueError, match="timezone"):
        ExpressionEnvironment(implicit_timezone=timedelta(hours=15))
    with pytest.raises(ValueError, match="timezone"):
        ExpressionEnvironment(implicit_timezone="not-a-zone")
    with pytest.raises(ValueError, match="timezone"):
        ExpressionEnvironment(implicit_timezone=42)  # type: ignore[arg-type]

    class _NoOffset(dt.tzinfo):
        def utcoffset(self, value: Any) -> Any:
            return None

        def dst(self, value: Any) -> Any:
            return None

    with pytest.raises(ValueError, match="timezone"):
        ExpressionEnvironment(implicit_timezone=_NoOffset())
    with pytest.raises(ValueError, match="timezone"):
        ExpressionEnvironment(implicit_timezone=dt_timezone(timedelta(hours=15)))


def test_focus_validation_and_kind_properties() -> None:
    item = _document().root_element
    assert Focus.absent().is_absent
    assert Focus.empty().is_empty
    assert not Focus.absent().is_empty
    assert not Focus.empty().is_absent

    with pytest.raises(TypeError, match="FocusKind"):
        Focus(kind="item")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="XdmItem"):
        Focus(item=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no context item"):
        Focus(kind=FocusKind.ABSENT, item=item)
    with pytest.raises(ValueError, match="position and size zero"):
        Focus(kind=FocusKind.EMPTY)
    with pytest.raises(ValueError, match="position/size"):
        Focus(item=item, position=0)
    with pytest.raises(ValueError, match="position/size"):
        Focus(item=item, position=3, size=2)


def test_static_context_name_helpers() -> None:
    static = XPathStaticContext(
        namespaces={"m": "urn:test", "": "urn:default"},
        functions={"m:f": 1, "{urn:test}g": 2, "m:h": (0, 2)},
    )
    assert static.function_arity("m:f") == 1
    assert static.function_arity("{urn:test}f") == 1
    assert static.function_arity("{urn:test}g") == 2
    assert static.function_arity("m:h") == (0, 2)
    assert static.function_arity("missing") is None
    assert static.function_arity("{urn:missing}f") is None
    expanded_only = XPathStaticContext(namespaces={"m": "urn:test"}, functions={"{urn:test}f": 1})
    assert expanded_only.function_arity("m:f") == 1
    assert static.expand_name("m:f") == "{urn:test}f"
    assert static.expand_name("f") is None
    assert static.expand_name("q:f") is None
    assert static.lexical_name("{urn:test}f") == "m:f"
    assert static.lexical_name("{urn:default}f") == "f"
    assert static.lexical_name("{urn:missing}f") is None
    assert static.lexical_name("m:f") == "m:f"


def test_engine_compiles_with_default_and_rejects_invalid_arguments() -> None:
    engine = XPathEngine()
    assert engine.compile("1").continuation_plan is not None
    with pytest.raises(ValueError, match="non-empty"):
        engine.compile("")
    with pytest.raises(ValueError, match="non-empty"):
        engine.compile("   ")
    with pytest.raises(TypeError, match="XPathStaticContext"):
        engine.compile("1", object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="different"):
        other = XPathEngine("3.0")
        other.evaluate(engine.compile("1"), ExpressionEnvironment())
    with pytest.raises(TypeError, match="CompiledExpression"):
        engine.evaluate(object(), ExpressionEnvironment())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ExpressionEnvironment"):
        engine.evaluate(engine.compile("1"), object())  # type: ignore[arg-type]


def test_compiled_expression_validation() -> None:
    from pyxsd.xpath.engine import CompiledExpression

    static = XPathStaticContext()
    with pytest.raises(ValueError, match="non-empty"):
        CompiledExpression("", static, "3.1")
    with pytest.raises(TypeError, match="static_context"):
        CompiledExpression("1", object(), "3.1")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="version"):
        CompiledExpression("1", static, "9.9")


def test_focus_forms_cover_every_resolution_arm() -> None:
    engine = XPathEngine()
    compiled = engine.compile("string(.)")
    document = _document(b"<root>text</root>")
    assert engine.evaluate(compiled, ExpressionEnvironment(), focus="value") == (
        XdmAtomic.string("value"),
    )
    assert engine.evaluate(compiled, ExpressionEnvironment(), focus=b"value") == (
        XdmAtomic.string("dmFsdWU="),
    )
    assert engine.evaluate(compiled, ExpressionEnvironment(), focus=("value",)) == (
        XdmAtomic.string("value"),
    )
    assert engine.evaluate(compiled, ExpressionEnvironment(), focus=XdmAtomic.integer(7)) == (
        XdmAtomic.string("7"),
    )
    with pytest.raises(ValueError, match="at most one"):
        engine.evaluate(compiled, ExpressionEnvironment(), focus=("a", ()))
    text = document.root_element.children[0]
    assert engine.evaluate(compiled, ExpressionEnvironment(), focus=text) == (
        XdmAtomic.string("text"),
    )


# ---------------------------------------------------------------------------
# Adapter protocol and value bridge


def test_node_adapter_protocol_surface() -> None:
    from pyxsd.xpath.engine import _build_parser, _ValueBridge

    document = _document(b'<root xmlns:p="urn:p" a="1"><item>2</item></root>')
    static = XPathStaticContext()
    parser = _build_parser("3.1", static, key=False)
    bridge = _ValueBridge(parser, static, ExpressionEnvironment())
    root = bridge.adapt_item(document.root_element)
    item = bridge.adapt_item(document.root_element.children[0])
    attribute = bridge.adapt_item(document.root_element.attributes[0])

    assert bridge.adapt_document(document) is root.tree.root_node
    assert root.namespace_nodes
    assert root.type_name.endswith("}untyped")
    assert root.nilled is False
    assert list(root.iter_typed_values) == [UntypedAtomic("2")]
    assert attribute.type_name.endswith("}untypedAtomic")
    assert list(attribute.iter_typed_values) == [UntypedAtomic("1")]
    assert attribute.base_uri == root.base_uri
    assert item.type_name.endswith("}untyped")
    assert list(item.iter_typed_values) == [UntypedAtomic("2")]
    document_adapter = bridge.adapt_document(document)
    assert bridge.adapt_node(item.xdm) is item
    assert bridge.adapt_node(document) is document_adapter
    assert bridge.adapt_node(document.root_element) is root
    assert bridge.adapt_item(root.xdm) is root
    with pytest.raises(ValueError, match="requires a parent"):
        from pyxsd.xdm.nodes import XdmNode

        detached = XdmNode.__new__(XdmNode)
        detached._kind = NodeKind.ATTRIBUTE  # type: ignore[attr-defined]
        bridge.adapt_node(detached)
    with pytest.raises(TypeError, match="not an XDM item"):
        bridge.adapt_item(object())  # type: ignore[arg-type]


def test_value_bridge_round_trips_maps_arrays_and_functions() -> None:
    from pyxsd.xpath.engine import _build_parser, _ValueBridge

    static = XPathStaticContext(variable_types={"m": "map(*)", "a": "array(*)"})
    parser = _build_parser("3.1", static, key=False)
    bridge = _ValueBridge(parser, static, ExpressionEnvironment())
    bridge._context = XPathContext(item="")

    map_value = XdmMap([(XdmAtomic.string("a"), (1, 2))])
    array_value = XdmArray([(1, 2), (3,)])
    ep_map = bridge.adapt_item(map_value)
    ep_array = bridge.adapt_item(array_value)
    assert isinstance(ep_map, XPathMap)
    assert isinstance(ep_array, XPathArray)
    round_trip_map = bridge.to_xdm_sequence(ep_map)[0]
    assert isinstance(round_trip_map, XdmMap)
    assert round_trip_map.entries == map_value.entries
    round_trip_array = bridge.to_xdm_sequence(ep_array)[0]
    assert isinstance(round_trip_array, XdmArray)
    assert round_trip_array.members == array_value.members
    assert bridge.to_xdm_sequence(None) == ()
    assert bridge.to_xdm_sequence(XdmAtomic.integer(1)) == (XdmAtomic.integer(1),)
    assert bridge.to_xdm_sequence((1, [2])) == (XdmAtomic.integer(1), XdmAtomic.integer(2))
    with pytest.raises(TypeError, match="cannot convert"):
        bridge.to_xdm_sequence(object())


def test_value_bridge_function_item_variants() -> None:

    from pyxsd.xpath.engine import _build_parser, _ValueBridge

    static = XPathStaticContext()
    parser = _build_parser("3.1", static, key=False)
    bridge = _ValueBridge(parser, static, ExpressionEnvironment())
    bridge._context = XPathContext(item="")
    named = parser.parse("fn:concat#2").evaluate()
    item = bridge.to_xdm_sequence(named)[0]
    assert isinstance(item, XdmFunction)
    assert item.invoke((("a",), ("b",))) == (XdmAtomic.string("ab"),)
    assert item.name == "{http://www.w3.org/2005/xpath-functions}concat"

    token = HostFunctionToken(
        parser,
        lambda value: value,
        item_converter=bridge.adapt_item,
        name="identity",
        arity=1,
    )
    converted = bridge.to_xdm_sequence(token)[0]
    assert isinstance(converted, XdmFunction)
    assert converted.invoke(((1,),)) == (XdmAtomic.integer(1),)

    target = XdmFunction(lambda arguments, environment, closure: arguments[0], arity=1)
    token = HostFunctionToken(parser, target, item_converter=bridge.adapt_item)
    assert bridge.to_xdm_sequence(token) == (target,)
    assert bridge.to_xdm_sequence(token).__class__ is tuple

    closure = continuation.evaluate(continuation.compile_expression("function($x){$x + 1}"), {})
    assert isinstance(closure, XPathClosure)
    converted = bridge.to_xdm_sequence(closure)[0]
    assert isinstance(converted, XdmFunction)
    assert converted.invoke(((2,),)) == (XdmAtomic.integer(3),)

    host = continuation.HostFunction(lambda: 9, name="nine", arity=0)
    converted = bridge.to_xdm_sequence(host)[0]
    assert isinstance(converted, XdmFunction)
    assert converted.invoke(()) == (XdmAtomic.integer(9),)


def test_extension_backed_host_tokens_resolve_the_call_site_focus() -> None:
    from types import SimpleNamespace

    from pyxsd.xpath.engine import _build_parser, _ValueBridge
    from pyxsd.xslt import ExtensionSignature, XsltExtensions

    seen: list[Any] = []

    def current_item(context_item: Any) -> Any:
        seen.append(context_item)
        return "hit" if context_item is not None else "absent"

    target = (
        XsltExtensions()
        .register_function(
            "{urn:app}cur",
            0,
            current_item,
            signature=ExtensionSignature((), "xs:string"),
            contextual=True,
        )
        .runtime_targets()["{urn:app}cur"]
    )
    static = XPathStaticContext()
    bridge = _ValueBridge(
        _build_parser("3.1", static, key=False),
        static,
        ExpressionEnvironment(variables={"cur": target, "plain": lambda: "plain"}),
    )

    # Only extension targets are focus-sensitive; plain callables stay unchanged.
    tokens = bridge.variable_values()
    assert tokens["plain"]._context_binder is None

    binder_token = tokens["cur"]
    assert binder_token._context_binder is not None
    # A token returns elementpath host values, not XDM items; the extension
    # callback receives the bound context item as one XDM item.
    assert binder_token.evaluate(SimpleNamespace(item=42)) == "hit"
    assert seen == [XdmAtomic.integer(42)]
    # A context item that converts to an empty sequence binds None, never an IndexError.
    assert binder_token.evaluate(SimpleNamespace(item=())) == "absent"
    # A call with no context at all binds None as well.
    assert binder_token.evaluate() == "absent"
    assert seen == [XdmAtomic.integer(42), None, None]

    engine = XPathEngine("3.1")
    compiled = engine.compile("(1, 2, 3)[$cur() = 'hit']", static)
    result = engine.evaluate(compiled, ExpressionEnvironment(variables={"cur": target}))
    assert result == (
        XdmAtomic.integer(1),
        XdmAtomic.integer(2),
        XdmAtomic.integer(3),
    )
    assert seen == [
        XdmAtomic.integer(42),
        None,
        None,
        XdmAtomic.integer(1),
        XdmAtomic.integer(2),
        XdmAtomic.integer(3),
    ]


def test_schema_pool_proxy_helpers(tmp_path: Path) -> None:
    from pyxsd.xpath.engine import _PoolSchemaProxy

    pool = _money_schema(tmp_path)
    proxy = _PoolSchemaProxy(pool, {"m": MONEY})
    assert proxy.xsd_version == "1.1"
    assert proxy.is_fully_valid()
    assert proxy.expand("{urn:x}T") == "{urn:x}T"
    assert proxy.expand("m:Amount") == f"{{{MONEY}}}Amount"
    assert proxy.expand("Amount") == "Amount"
    with pytest.raises(KeyError):
        proxy.expand("q:Amount")
    assert str(proxy.cast_as("12.5", "m:Amount")) == "12.5"
    assert proxy.is_instance("12.5", "m:Amount")
    assert not proxy.is_instance("nope", "m:Amount")
    with pytest.raises(KeyError):
        proxy.cast_as("1", "m:Missing")
    with pytest.raises(KeyError):
        proxy.is_instance("1", "m:Missing")
    assert proxy.get_type("m:Amount") is not None
    assert proxy.get_element(f"{{{MONEY}}}payment") is not None
    assert proxy.get_attribute("m:missing") is None
    assert proxy.get_substitution_group("m:missing") is None


def test_function_declaration_name_forms() -> None:
    engine = XPathEngine()
    expanded = XPathStaticContext(namespaces={"m": "urn:test"}, functions={"{urn:test}inc": 1})
    compiled = engine.compile("m:inc($n)", expanded)
    environment = ExpressionEnvironment(variables={"n": 1}, functions={"{urn:test}inc": increment})
    assert engine.evaluate(compiled, environment) == (XdmAtomic.integer(2),)

    lexical = XPathStaticContext(namespaces={"m": "urn:test"}, functions={"m:inc": 1})
    compiled = engine.compile("m:inc($n)", lexical)
    environment = ExpressionEnvironment(variables={"n": 2}, functions={"m:inc": increment})
    assert engine.evaluate(compiled, environment) == (XdmAtomic.integer(3),)


def test_function_declaration_errors_and_unavailable_target() -> None:
    engine = XPathEngine()
    with pytest.raises(ValueError, match="namespace prefix"):
        engine.compile("m:inc(1)", XPathStaticContext(functions={"m:inc": 1}))
    static = XPathStaticContext(namespaces={"m": "urn:test"}, functions={"m:inc": 1})
    compiled = engine.compile("m:inc($n)", static)
    with pytest.raises(RuntimeError, match="no environment implementation"):
        engine.evaluate(compiled, ExpressionEnvironment(variables={"n": 1}))
    with pytest.raises(ValueError, match="prefix"):
        from pyxsd.xpath.engine import _split_function_name

        _split_function_name("{urn:test}inc", XPathStaticContext(), {})
    with pytest.raises(ValueError, match="requires a namespace prefix"):
        from pyxsd.xpath.engine import _split_function_name

        _split_function_name("inc", static, {"m": "urn:test"})


def test_continuation_variable_conversion_and_fallback() -> None:
    from pyxsd.xpath.engine import _build_parser, _continuation_variables, _ValueBridge

    static = XPathStaticContext(variable_types={"n": "xs:integer", "f": "function(*)"})
    parser = _build_parser("3.1", static, key=False)
    bridge = _ValueBridge(parser, static, ExpressionEnvironment())
    document = _document()
    assert _continuation_variables(ExpressionEnvironment(variables={"n": (1, 2)}), bridge) == {
        "n": [1, 2]
    }
    assert _continuation_variables(ExpressionEnvironment(variables={"n": ()}), bridge) == {"n": []}
    assert _continuation_variables(
        ExpressionEnvironment(variables={"n": (XdmAtomic.integer(4),)}), bridge
    ) == {"n": 4}
    function = XdmFunction(lambda arguments, environment, closure: arguments[0], arity=1)
    converted = _continuation_variables(ExpressionEnvironment(variables={"f": function}), bridge)
    assert converted is not None
    assert converted["f"].arity == 1
    assert (
        _continuation_variables(
            ExpressionEnvironment(variables={"n": document.root_element}), bridge
        )
        is None
    )


def test_parser_builder_without_key_and_sequence_type_rejection() -> None:
    from pyxsd.xpath.engine import _build_parser

    static = XPathStaticContext()
    parser = _build_parser("3.1", static, key=False)
    assert "key" not in parser.symbol_table
    with pytest.raises(Exception, match="not allowed in XPath expression"):
        XPathEngine().compile("item()")


def test_arity_tuple_and_key_index_ordering() -> None:
    from pyxsd.xpath.engine import _arity_tuple

    assert _arity_tuple(2) == (2, 2)
    assert _arity_tuple((0, None)) == (0, None)

    document = _document(b"<root><item id='b'>two</item><item id='a'>one</item></root>")
    second, first = document.root_element.children
    engine = XPathEngine()
    compiled = engine.compile("key('by-id', 'a')")
    environment = ExpressionEnvironment(key_indexes={"by-id": {"a": (first, second)}})
    assert engine.evaluate(compiled, environment) == (second, first)
    assert engine.evaluate(engine.compile("key('missing', 'a')"), environment) == ()


# ---------------------------------------------------------------------------
# Host function and bridge branch contracts


def test_atomic_and_sequence_conversion_helpers() -> None:
    from pyxsd.xpath.functions import atomic_to_elementpath, sequence_to_elementpath

    assert atomic_to_elementpath(XdmAtomic.untyped_atomic("x")) == UntypedAtomic("x")
    assert atomic_to_elementpath(XdmAtomic.integer(2)) == 2
    assert atomic_to_elementpath(XdmAtomic("plain", "string", None)) == "plain"
    assert sequence_to_elementpath((), item_converter=lambda item: item) == []
    assert sequence_to_elementpath((XdmAtomic.integer(1),), item_converter=lambda item: item) == (
        XdmAtomic.integer(1)
    )
    assert sequence_to_elementpath(
        (XdmAtomic.integer(1), XdmAtomic.integer(2)), item_converter=lambda item: item
    ) == [XdmAtomic.integer(1), XdmAtomic.integer(2)]


def test_host_argument_shapes_and_invalid_target() -> None:
    from pyxsd.xpath.functions import call_host_function

    seen: list[Any] = []

    def capture(*arguments: Any) -> Any:
        seen.append(arguments)
        return 1

    document = _document()
    call_host_function(
        capture,
        [(), (XdmAtomic.integer(2),), (document.root_element,), (1, (2, 3))],
    )
    assert seen == [((), 2, document.root_element, (1, 2, 3))]
    assert seen[0][1] == 2
    assert seen[0][2] is document.root_element
    assert seen[0][3] == (1, 2, 3)
    with pytest.raises(TypeError, match="not a callable"):
        call_host_function(object(), ())


def test_function_item_arity_accepts_a_variadic_host_token() -> None:
    from pyxsd.xpath.engine import _build_parser, _function_item_arity

    parser = _build_parser("3.1", XPathStaticContext(), key=False)
    token = HostFunctionToken(
        parser,
        lambda *args: args,
        item_converter=lambda item: item,
    )
    assert token.nargs == (0, None)
    assert _function_item_arity(token) == (0, None)


def test_function_arity_signatures(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyxsd.xpath.functions import function_arity

    def exact(left: Any, right: Any) -> Any:
        return left + right

    def defaults(left: Any, right: Any = 1) -> Any:
        return left + right

    def varargs(*args: Any, **kwargs: Any) -> Any:
        return args

    def varargs_optional_keyword_only(*args: Any, optional: Any = 1) -> Any:
        return args

    def mixed_required_keyword_only(value: Any, *, required: Any) -> Any:
        return value

    def required_keyword_only(*, required: Any) -> Any:
        return required

    def varargs_required_keyword_only(*args: Any, required: Any) -> Any:
        return args

    assert function_arity(XdmFunction(lambda arguments, environment, closure: (), arity=2)) == (
        2,
        2,
    )
    assert function_arity(exact) == 2
    assert function_arity(defaults) == (1, 2)
    assert function_arity(varargs) == (0, None)
    assert function_arity(varargs_optional_keyword_only) == (0, None)
    with pytest.raises(TypeError, match="keyword-only"):
        function_arity(mixed_required_keyword_only)
    with pytest.raises(TypeError, match="keyword-only"):
        function_arity(required_keyword_only)
    with pytest.raises(TypeError, match="keyword-only"):
        function_arity(varargs_required_keyword_only)
    with pytest.raises(TypeError, match="not callable"):
        function_arity(object())

    monkeypatch.setattr("inspect.signature", lambda target: (_ for _ in ()).throw(ValueError("no")))
    assert function_arity(exact) == (0, None)


def test_host_function_token_protocol() -> None:
    from elementpath.xpath_tokens.tokens import ValueToken

    from pyxsd.xpath.engine import _build_parser
    from pyxsd.xpath.functions import clone_function_token

    parser = _build_parser("3.1", XPathStaticContext(), key=False)
    token = HostFunctionToken(parser, lambda value=1: value, item_converter=lambda item: item)
    assert token.arity == 0

    def mixed_required_keyword_only(value: Any, *, required: Any) -> Any:
        return value

    def required_keyword_only(*, required: Any) -> Any:
        return required

    for callable_value in (mixed_required_keyword_only, required_keyword_only):
        with pytest.raises(TypeError, match="keyword-only"):
            HostFunctionToken(parser, callable_value, item_converter=lambda item: item)
    assert "HostFunctionToken" in repr(token)
    assert token(2) == XdmAtomic.integer(2)
    token.append(ValueToken(parser, value=3))
    assert token.evaluate() == XdmAtomic.integer(3)

    exact_token = HostFunctionToken(
        parser, lambda value: value, item_converter=lambda item: item, arity=1
    )
    assert exact_token.arity == 1
    assert exact_token(parser.parse("1")) == XdmAtomic.integer(1)

    tuple_token = HostFunctionToken(
        parser,
        lambda *args: sum(args),
        item_converter=lambda item: item,
        arity=(1, 2),
    )
    assert tuple_token.arity == 1
    assert tuple_token(1, 2) == XdmAtomic.integer(3)
    with pytest.raises(Exception, match=r"expects 1\.\.2"):
        tuple_token(1, 2, 3)

    with pytest.raises(TypeError, match="callable or XdmFunction"):
        HostFunctionToken(parser, object(), item_converter=lambda item: item)
    with pytest.raises(TypeError, match="not callable"):
        clone_function_token(1)


def test_clone_function_token_detaches_children_and_variables() -> None:
    from pyxsd.xpath.engine import _build_parser
    from pyxsd.xpath.functions import clone_function_token

    parser = _build_parser("3.1", XPathStaticContext(variable_types={"n": "xs:integer"}), key=False)
    context = XPathContext(item="", variables={"n": 2})
    function = parser.parse("function($x){$x + $n}").evaluate(context)
    clone = clone_function_token(function)
    assert clone is not function
    assert clone(3, context=context) == 5
    assert function(4, context=context) == 6


def test_bridge_document_string_and_attribute_string() -> None:
    document = _document(b"<root a='value'>text</root>")
    engine = XPathEngine()
    assert engine.evaluate(
        engine.compile("string(/)"), ExpressionEnvironment(), focus=document
    ) == (XdmAtomic.string("text"),)
    assert engine.evaluate(
        engine.compile("string(@a)"), ExpressionEnvironment(), focus=document.root_element
    ) == (XdmAtomic.string("value"),)


def test_absent_focus_select_path_raises() -> None:
    engine = XPathEngine()
    compiled = engine.compile("count(.)")
    with pytest.raises(Exception) as caught:
        engine.evaluate(compiled, ExpressionEnvironment())
    assert _error_code(caught.value) == "XPDY0002"


def test_fresh_bridge_adapt_node_element_without_cache() -> None:
    from pyxsd.xpath.engine import _build_parser, _ValueBridge

    document = _document()
    bridge = _ValueBridge(
        _build_parser("3.1", XPathStaticContext(), key=False),
        XPathStaticContext(),
        ExpressionEnvironment(),
    )
    adapter = bridge.adapt_node(document.root_element)
    assert adapter.xdm is document
    with pytest.raises(TypeError, match="not an XDM item"):
        bridge.adapt_item(42)  # type: ignore[arg-type]


def test_bridge_adapts_map_values_and_function_items() -> None:
    from pyxsd.xpath.engine import _build_parser, _ValueBridge

    static = XPathStaticContext()
    bridge = _ValueBridge(_build_parser("3.1", static, key=False), static, ExpressionEnvironment())
    bridge._context = XPathContext(item="")
    function = XdmFunction(lambda arguments, environment, closure: (1,), arity=0)
    adapted = bridge.adapt_item(function)
    assert isinstance(adapted, HostFunctionToken)
    document = _document()
    map_value = XdmMap(
        [
            (XdmAtomic.string("node"), (document.root_element,)),
            (XdmAtomic.string("function"), (function,)),
        ]
    )
    assert isinstance(bridge.adapt_item(map_value), XPathMap)
    array_value = XdmArray([(map_value,), (function,)])
    assert isinstance(bridge.adapt_item(array_value), XPathArray)


def test_bridge_handles_anonymous_xsd_datatypes() -> None:
    from pyxsd import xsd_data_types as xsd
    from pyxsd.xpath.engine import _build_parser, _ValueBridge

    Anonymous = type("parent|kind", (xsd.String,), {})
    value = Anonymous("x")
    bridge = _ValueBridge(
        _build_parser("3.1", XPathStaticContext(), key=False),
        XPathStaticContext(),
        ExpressionEnvironment(),
    )
    result = bridge.to_xdm_sequence(value)
    assert result[0].lexical == "x"


def test_function_item_name_falls_back_to_the_token_qname() -> None:
    from pyxsd.xpath.engine import _build_parser, _ValueBridge

    parser = _build_parser("3.1", XPathStaticContext(), key=False)
    bridge = _ValueBridge(parser, XPathStaticContext(), ExpressionEnvironment())
    bridge._context = XPathContext(item="")
    token = parser.parse("fn:concat#2").evaluate()
    token.name = 1
    item = bridge.to_xdm_sequence(token)[0]
    assert isinstance(item, XdmFunction)
    assert item.name == "{http://www.w3.org/2005/xpath-functions}concat"

    anonymous = parser.parse("function($a){$a}").evaluate(XPathContext(item=""))
    item = bridge.to_xdm_sequence(anonymous)[0]
    assert isinstance(item, XdmFunction)
    assert item.name is None


def test_version_3_compiles_without_a_continuation_plan() -> None:
    engine = XPathEngine("3.0")
    compiled = engine.compile("1 + 1")
    assert compiled.continuation_plan is None
    assert engine.evaluate(compiled, ExpressionEnvironment()) == (XdmAtomic.integer(2),)


def test_continuation_plan_falls_back_for_node_variable() -> None:
    engine = XPathEngine()
    compiled = engine.compile("$n", XPathStaticContext(variable_types={"n": "element()"}))
    assert compiled.continuation_plan is not None
    document = _document()
    assert engine.evaluate(
        compiled, ExpressionEnvironment(variables={"n": document.root_element})
    ) == (document.root_element,)


def test_parser_instance_tokenizer_is_reused() -> None:
    from pyxsd.xpath.engine import _build_parser

    parser = _build_parser("3.1", XPathStaticContext(), key=False)
    parser.parse("1")
    tokenizer = parser.tokenizer
    parser.parse("2")
    assert parser.tokenizer is tokenizer


def test_schema_constructor_without_namespace_is_skipped(tmp_path: Path) -> None:
    from pyxsd.xpath.engine import _build_parser

    schema_path = tmp_path / "plain.xsd"
    schema_path.write_text(
        f'<xs:schema xmlns:xs="{XSD_NS}">'
        '<xs:simpleType name="Plain"><xs:restriction base="xs:string"/></xs:simpleType>'
        "</xs:schema>",
        encoding="utf-8",
    )
    pool = SchemaPool.from_imports([schema_path])
    parser = _build_parser("3.1", XPathStaticContext(schema=pool), key=False)
    assert "Plain" not in parser.symbol_table


def test_schema_constructor_registration_is_idempotent(tmp_path: Path) -> None:
    from pyxsd.xpath.engine import _build_parser, _register_schema_constructors

    pool = _money_schema(tmp_path)
    static = XPathStaticContext(namespaces={"m": MONEY}, schema=pool)
    parser = _build_parser("3.1", static, key=False)
    constructor = parser.symbol_table["m:Amount"]
    _register_schema_constructors(parser, static)
    assert parser.symbol_table["m:Amount"] is constructor


def test_function_target_expanded_and_lexical_lookups() -> None:
    from pyxsd.xpath.engine import _function_target

    lexical_declaration = XPathStaticContext(namespaces={"m": "urn:test"}, functions={"m:f": 1})
    assert _function_target(lexical_declaration, "m:f", {"{urn:test}f": increment}) is increment
    expanded_declaration = XPathStaticContext(
        namespaces={"m": "urn:test"}, functions={"{urn:test}f": 1}
    )
    assert _function_target(expanded_declaration, "{urn:test}f", {"m:f": increment}) is increment


def test_focus_sequence_with_nested_sequence_and_generator() -> None:
    engine = XPathEngine()
    compiled = engine.compile("string(.)")
    with pytest.raises(ValueError, match="exactly one"):
        engine.evaluate(compiled, ExpressionEnvironment(), focus=(["a", "b"],))
    with pytest.raises(ValueError, match="exactly one"):
        engine.evaluate(compiled, ExpressionEnvironment(), focus=(item for item in ("a", "b")))  # type: ignore[arg-type]


def test_continuation_variables_sequence_with_nested_function() -> None:
    from pyxsd.xpath.engine import _build_parser, _continuation_variables, _ValueBridge

    static = XPathStaticContext(variable_types={"f": "function(*)"})
    bridge = _ValueBridge(
        _build_parser("3.1", static, key=False),
        static,
        ExpressionEnvironment(),
    )
    function = XdmFunction(lambda arguments, environment, closure: (1,), arity=0)
    converted = _continuation_variables(
        ExpressionEnvironment(variables={"f": (function, XdmAtomic.integer(1))}), bridge
    )
    assert converted is not None
    assert len(converted["f"]) == 2


def test_partial_function_items_keep_invocation_state_isolated() -> None:
    engine = XPathEngine()
    result = engine.evaluate(engine.compile("fn:concat(?, '!')"), ExpressionEnvironment())
    function = result[0]
    assert isinstance(function, XdmFunction)
    assert function.arity == (1, 1)
    assert function.invoke((("a",),)) == (XdmAtomic.string("a!"),)
    assert function.invoke((("b",),)) == (XdmAtomic.string("b!"),)


def test_token_tree_clone_handles_non_tokens_and_map_values() -> None:
    from pyxsd.xpath.engine import _build_parser
    from pyxsd.xpath.functions import _clone_token_tree

    assert _clone_token_tree(42) == 42
    parser = _build_parser("3.1", XPathStaticContext(), key=False)
    map_token = parser.parse("map{'a': 1}")
    clone = _clone_token_tree(map_token)
    assert clone is not map_token
    assert getattr(clone, "_values", None) is not getattr(map_token, "_values", None)
    empty = XPath31Parser().parse("1")
    empty._items.clear()
    assert _clone_token_tree(empty)._items == []
    from elementpath.xpath_tokens.base import XPathToken

    bare = XPathToken.__new__(XPathToken)
    assert isinstance(_clone_token_tree(bare), XPathToken)


def test_zero_argument_function_item_retains_arity() -> None:
    engine = XPathEngine()
    result = engine.evaluate(engine.compile("fn:true#0"), ExpressionEnvironment())
    function = result[0]
    assert isinstance(function, XdmFunction)
    assert function.arity == (0, 0)
    assert function.invoke(()) == (XdmAtomic.boolean(True),)


def test_default_collation_applies_on_both_evaluator_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collation = "http://www.w3.org/2005/xpath-functions/collation/html-ascii-case-insensitive"
    static = XPathStaticContext(
        default_collation=collation,
        variable_types={"a": "xs:string", "b": "xs:string"},
    )
    engine = XPathEngine()
    continuation = engine.compile("$a = $b", static)
    fallback = engine.compile("compare($a, $b) = 0", static)
    assert continuation.continuation_plan is not None
    assert continuation.continuation_plan.default_collation == collation
    assert fallback.continuation_plan is None
    environment = ExpressionEnvironment(variables={"a": "A", "b": "a"})

    seen: list[str | None] = []
    original_run = ContinuationMachine.run

    def spy_run(self: ContinuationMachine) -> Any:
        seen.append(self._parser.default_collation)
        return original_run(self)

    monkeypatch.setattr(engine_module.ContinuationMachine, "run", spy_run)
    assert engine.evaluate(continuation, environment) == (XdmAtomic.boolean(False),)
    assert seen == [collation]
    assert engine.evaluate(fallback, environment) == (XdmAtomic.boolean(True),)

    default_engine = XPathEngine()
    default_static = XPathStaticContext(variable_types={"a": "xs:string", "b": "xs:string"})
    assert default_engine.evaluate(
        default_engine.compile("$a = $b", default_static),
        ExpressionEnvironment(variables={"a": "A", "b": "a"}),
    ) == (XdmAtomic.boolean(False),)


def test_environment_base_uri_drives_uri_resolution() -> None:
    engine = XPathEngine()
    static = XPathStaticContext(base_uri="https://static.example/base/")
    compiled = engine.compile("resolve-uri('x')", static)

    assert engine.evaluate(compiled, ExpressionEnvironment()) == (
        XdmAtomic.any_uri("https://static.example/base/x"),
    )
    assert engine.evaluate(
        compiled, ExpressionEnvironment(base_uri="https://dynamic.example/base/")
    ) == (XdmAtomic.any_uri("https://dynamic.example/base/x"),)


def test_key_accepts_xdm_atomic_index_keys() -> None:
    document = _document(b"<root><item id='a'>one</item></root>")
    first = document.root_element.children[0]
    engine = XPathEngine()
    environment = ExpressionEnvironment(key_indexes={"by-id": {XdmAtomic.string("a"): (first,)}})
    assert engine.evaluate(engine.compile("key('by-id', 'a')"), environment) == (first,)


def test_pool_schema_validity_consults_every_schema() -> None:
    from types import SimpleNamespace

    from pyxsd.xpath.engine import _PoolSchemaProxy

    valid = SimpleNamespace(is_fully_valid=lambda: True)
    invalid = SimpleNamespace(is_fully_valid=lambda: False)
    assert _PoolSchemaProxy(SimpleNamespace(schemas={"a": valid}), {}).is_fully_valid()
    assert not _PoolSchemaProxy(
        SimpleNamespace(schemas={"a": valid, "b": invalid}), {}
    ).is_fully_valid()


def test_declared_function_callback_requires_a_bridge() -> None:
    from pyxsd.xpath.engine import (
        _BridgeHolder,
        _build_parser,
        _register_declared_function,
    )

    parser_static = XPathStaticContext(namespaces={"m": "urn:test"})
    static = XPathStaticContext(namespaces={"m": "urn:test"}, functions={"m:inc": 1})
    parser = _build_parser("3.1", parser_static, key=False)
    holder = _BridgeHolder()
    _register_declared_function(parser, "m:inc", static, holder)
    with pytest.raises(RuntimeError, match="no evaluation context"):
        parser.symbol_table["inc"].callback(1)


def test_sequence_with_engine_closure_falls_back_to_elementpath() -> None:
    engine = XPathEngine()
    closure = engine.evaluate(engine.compile("function($x) { $x + 1 }"), ExpressionEnvironment())[0]
    assert isinstance(closure, XdmFunction)
    compiled = engine.compile("$items", XPathStaticContext(variable_types={"items": "item()*"}))
    assert compiled.continuation_plan is not None
    result = engine.evaluate(
        compiled,
        ExpressionEnvironment(variables={"items": (closure, XdmAtomic.integer(1))}),
    )
    assert result[0] is closure
    assert result[1] == XdmAtomic.integer(1)


# ---------------------------------------------------------------------------
# Shared XPath/XDM bridge primitives (Task 2.2)


def test_continuation_comparisons_read_empty_and_multi_item_variables() -> None:
    """An empty variable is an empty sequence, never one ``()`` operand.

    ``ValueToken.select`` expands a Python list item by item and treats a tuple
    as one item.  The continuation machine therefore has to hand over lists, or
    ``$empty = 1`` atomizes ``()`` as a value instead of comparing an empty
    sequence.  W3C ``boolean/boolean-101`` and ``boolean/boolean-070`` exercise
    the same rule through stylesheet variables.
    """
    engine = XPathEngine()
    empty = ExpressionEnvironment(variables={"a": (), "b": ()})
    assert engine.evaluate(engine.compile("$a = 1"), empty) == (XdmAtomic.boolean(False),)
    assert engine.evaluate(engine.compile("$a != 1"), empty) == (XdmAtomic.boolean(False),)
    assert engine.evaluate(engine.compile("$a eq 1"), empty) == ()
    assert engine.evaluate(engine.compile("$a = $b"), empty) == (XdmAtomic.boolean(False),)

    multi = ExpressionEnvironment(
        variables={
            "a": (XdmAtomic.double(2.0), XdmAtomic.double(3.0)),
            "b": (XdmAtomic.double(3.0),),
        }
    )
    assert engine.evaluate(engine.compile("$a = $b"), multi) == (XdmAtomic.boolean(True),)
    assert engine.evaluate(engine.compile("$a = 7"), multi) == (XdmAtomic.boolean(False),)

    with pytest.raises(Exception) as caught:
        engine.evaluate(engine.compile("(1, 2) eq 1"), ExpressionEnvironment())
    assert _error_code(caught.value) == "XPTY0004"


def test_constructor_lookup_argument_is_not_partial_application() -> None:
    """``xs:integer($map?value)`` casts; only a bare ``?`` is a placeholder.

    elementpath's ``XPathConstructor.nud`` checks the argument's root symbol,
    so a lookup expression such as ``$map?value`` was mistaken for a partial
    application and the constructor silently produced a function item.  W3C
    ``fn/transform/transform-002`` casts the native node value of
    ``transform(...)?output`` the same way.
    """
    engine = XPathEngine()
    mapping = XdmMap([(XdmAtomic.string("value"), XdmAtomic.integer(42))])
    environment = ExpressionEnvironment(variables={"m": mapping})

    assert engine.evaluate(engine.compile("xs:integer($m?value)"), environment) == (
        XdmAtomic.integer(42),
    )
    assert engine.evaluate(engine.compile("xs:integer($m?missing)"), environment) == ()

    partial = engine.evaluate(engine.compile("xs:integer(?)"), ExpressionEnvironment())
    assert len(partial) == 1 and isinstance(partial[0], XdmFunction)
    assert partial[0].invoke((XdmAtomic.string("7"),)) == (XdmAtomic.integer(7),)
    with pytest.raises(Exception) as partial_error:
        partial[0].invoke((XdmAtomic.string("?"),))
    assert _error_code(partial_error.value) == "XPTY0004"

    question = XdmMap([(XdmAtomic.string("value"), XdmAtomic.string("?"))])
    with pytest.raises(Exception) as lookup_error:
        engine.evaluate(
            engine.compile("xs:integer($m?value)"),
            ExpressionEnvironment(variables={"m": question}),
        )
    assert _error_code(lookup_error.value) == "FORG0001"

    with pytest.raises(Exception) as caught:
        engine.evaluate(engine.compile("xs:integer('x7')"), ExpressionEnvironment())
    assert _error_code(caught.value) == "FORG0001"

    with pytest.raises(Exception) as too_many:
        engine.compile("xs:integer(1, 2)")
    assert _error_code(too_many.value) == "XPST0017"
    with pytest.raises(Exception) as truncated:
        engine.compile("xs:integer(")
    assert _error_code(truncated.value) == "XPST0017"

    reference = engine.evaluate(engine.compile("xs:integer#1"), ExpressionEnvironment())[0]
    assert reference.invoke((XdmAtomic.string("7"),)) == (XdmAtomic.integer(7),)


def test_constructor_parse_arguments_flag_short_circuits_nud() -> None:
    """The corrected constructor pauses argument parsing when the parser says so.

    elementpath's static schema evaluation parses constructors with
    ``parse_arguments`` disabled; the replacement ``nud`` must return the token
    unchanged there rather than consume a following expression.
    """
    from pyxsd.xpath.engine import _build_parser

    parser = _build_parser("3.1", XPathStaticContext(), key=False)
    parser.parse_arguments = False
    token = parser.symbol_table["integer"](parser)
    assert token.nud() is token


def test_matches_q_flag_uses_the_profile_regex_vocabulary() -> None:
    """``q`` is an XPath 3.0 flag; a native 2.0 expression rejects it.

    elementpath's 2.0 parser compares ``parser.version > '2'`` textually, so it
    accepted the XPath 3.0 ``q`` flag and a native 2.0 profile reported a match
    instead of ``FORX0001``.
    """
    three = XPathEngine("3.0")
    assert three.evaluate(three.compile("matches('abc', 'a.c', 'q')"), ExpressionEnvironment()) == (
        XdmAtomic.boolean(False),
    )
    assert three.evaluate(three.compile("matches('a.c', 'a.c', 'q')"), ExpressionEnvironment()) == (
        XdmAtomic.boolean(True),
    )

    two = XPathEngine("2.0")
    with pytest.raises(Exception) as flag_error:
        two.evaluate(two.compile("matches('abc', 'a.c', 'q')"), ExpressionEnvironment())
    assert _error_code(flag_error.value) == "FORX0001"
    assert two.evaluate(two.compile("matches('abc', 'a.c', 'x')"), ExpressionEnvironment()) == (
        XdmAtomic.boolean(True),
    )


def test_three_argument_tokenize_uses_the_profile_regex_flags() -> None:
    """The third ``tokenize`` argument uses the profile's flag vocabulary.

    elementpath's 2.0 parser also accepted the XPath 3.0 ``q`` flag here, so a
    native 2.0 expression returned the whole input instead of raising
    ``FORX0001``; the W3C XSLT 2.0 catalog uses the third argument for the 2.0
    flags (``regex/regex-011``, ``regex/regex-014``).
    """
    three = XPathEngine("3.0")
    assert three.evaluate(
        three.compile("tokenize('a1b2c', '[0-9]', 'q')"), ExpressionEnvironment()
    ) == (XdmAtomic.string("a1b2c"),)
    assert three.evaluate(
        three.compile("tokenize('a b c', ' ')", XPathStaticContext()), ExpressionEnvironment()
    ) == (XdmAtomic.string("a"), XdmAtomic.string("b"), XdmAtomic.string("c"))

    two = XPathEngine("2.0")
    assert two.evaluate(two.compile("matches('abc', 'abc')"), ExpressionEnvironment()) == (
        XdmAtomic.boolean(True),
    )
    assert two.evaluate(
        two.compile("tokenize('a1b2c', '[0-9]', 'i')"), ExpressionEnvironment()
    ) == (XdmAtomic.string("a"), XdmAtomic.string("b"), XdmAtomic.string("c"))
    with pytest.raises(Exception) as tokenize_flag_error:
        two.evaluate(
            two.compile("tokenize('a1b2c', '[0-9]', 'q')"),
            ExpressionEnvironment(),
        )
    assert _error_code(tokenize_flag_error.value) == "FORX0001"


def test_absolute_paths_and_following_work_from_non_element_focus() -> None:
    document = _document(b"<root><!--one--><a x='1'>text</a><!--two--></root>")
    root = document.root_element
    element = next(child for child in root.children if child.kind is NodeKind.ELEMENT)
    text = next(child for child in element.children if child.kind is NodeKind.TEXT)
    attribute = element.attributes[0]

    engine = XPathEngine("3.1")
    absolute = engine.compile("count(//a)", NODE_CONTEXT)
    following = engine.compile("count(following::comment())", NODE_CONTEXT)

    for item in (text, attribute):
        assert engine.evaluate(absolute, ExpressionEnvironment(), focus=Focus.singleton(item)) == (
            XdmAtomic.integer(1),
        )
    assert engine.evaluate(
        following, ExpressionEnvironment(), focus=Focus.singleton(attribute)
    ) == (XdmAtomic.integer(1),)
    assert engine.evaluate(following, ExpressionEnvironment(), focus=Focus.singleton(element)) == (
        XdmAtomic.integer(1),
    )


def test_untyped_atomic_ordering_is_string_ordering() -> None:
    """Two untyped operands compare as strings, not as doubles.

    Functions and Operators 3.1 section 3.7.2 casts both untyped atomic operands
    of a general comparison to ``xs:string``.  W3C ``boolean/boolean-073``
    compares untyped node values where one value (``abc``) is not a number.
    """
    engine = XPathEngine()
    untyped = ExpressionEnvironment(
        variables={
            "a": XdmAtomic.untyped_atomic("3"),
            "b": XdmAtomic.untyped_atomic("22"),
        }
    )
    assert engine.evaluate(engine.compile("$a > $b"), untyped) == (XdmAtomic.boolean(True),)
    assert engine.evaluate(engine.compile("$a < $b"), untyped) == (XdmAtomic.boolean(False),)
    assert engine.evaluate(engine.compile("$a = $b"), untyped) == (XdmAtomic.boolean(False),)

    untyped_sequences = ExpressionEnvironment(
        variables={
            "a": (XdmAtomic.untyped_atomic("3"), XdmAtomic.untyped_atomic("4")),
            "b": (XdmAtomic.untyped_atomic("22"),),
        }
    )
    assert engine.evaluate(engine.compile("$a > $b"), untyped_sequences) == (
        XdmAtomic.boolean(True),
    )
    # Explicit numeric conversion still uses the untyped value.
    assert engine.evaluate(engine.compile("$a + 1"), untyped) == (XdmAtomic.double(4.0),)

    document = _document(
        b"<dummy><set1><value>2</value><value>3</value></set1>"
        b"<set2><value>3</value><value>22</value></set2></dummy>"
    )
    assert engine.evaluate(
        engine.compile("/dummy/set1/value > /dummy/set2/value"),
        ExpressionEnvironment(),
        focus=document,
    ) == (XdmAtomic.boolean(True),)


def test_lang_reads_the_language_in_scope_of_bridged_elements() -> None:
    """``fn:lang`` resolves ``xml:lang`` on the bridged adapter chain.

    elementpath implements ``lang`` only for its own etree elements and returned
    false for every grounded adapter; W3C ``boolean/boolean-009`` and
    ``boolean/boolean-099`` assert the inherited language.
    """
    document = _document(b'<doc a="1" xml:lang="en"><p/><p xml:lang="fr"><q/></p></doc>')
    engine = XPathEngine()
    english, french = document.root_element.children
    nested = french.children[0] if french.children else french

    def language(focus: Any) -> Any:
        return engine.evaluate(engine.compile("lang('en')"), ExpressionEnvironment(), focus=focus)

    assert language(document.root_element) == (XdmAtomic.boolean(True),)
    assert language(english) == (XdmAtomic.boolean(True),)
    assert language(french) == (XdmAtomic.boolean(False),)
    assert language(nested) == (XdmAtomic.boolean(False),)
    assert language(_document(b"<doc><p/></doc>").root_element) == (XdmAtomic.boolean(False),)

    with pytest.raises(Exception) as invalid:
        engine.evaluate(
            engine.compile("lang('en', 3)"),
            ExpressionEnvironment(),
            focus=document.root_element,
        )
    assert _error_code(invalid.value) == "XPTY0004"
    with pytest.raises(Exception) as absent:
        engine.evaluate(engine.compile("lang('en')"), ExpressionEnvironment())
    assert _error_code(absent.value) == "XPTY0004"


def test_declared_function_collision_keeps_parser_class_tables_immutable() -> None:
    """A colliding declared function must not mutate a parser class table.

    elementpath moves a colliding built-in to its expanded symbol before it
    registers a proxy.  The continuation compiler's bare parser had no table of
    its own, so that write changed ``XPath31Parser.symbol_table`` for every
    later parser in the process: later ``fn:doc`` overrides resolved to the
    moved built-in and silently stopped applying.
    """
    from elementpath.xpath31._xpath31_functions import XPath31Parser

    before = dict(XPath31Parser.symbol_table)
    continuation.compile_expression(
        "1",
        namespaces={"my": "urn:my"},
        functions={"{urn:my}doc": 0, "{urn:my}value": 0},
    )
    assert dict(XPath31Parser.symbol_table) == before

    parser = engine_module._build_parser("3.1", XPathStaticContext(), key=False)
    assert "{http://www.w3.org/2005/xpath-functions}doc" not in parser.symbol_table


def test_override_symbol_prefers_the_expanded_function_token() -> None:
    """An override resolves to the real token, never the collision proxy.

    elementpath's declared-function registration moves a colliding built-in to
    its expanded symbol and leaves a ``ProxyToken`` at the bare local name.
    ``_override_symbol`` has to pick the expanded token in that state; otherwise
    the override class reads its signature from a proxy class that has none.
    """
    from pyxsd.xpath.engine import _build_parser, _override_symbol

    static = XPathStaticContext(namespaces={"my": "urn:my"}, functions={"my:doc": 0})
    parser = _build_parser("3.1", static, key=False)
    assert _override_symbol(parser, "{http://www.w3.org/2005/xpath-functions}doc") == (
        "{http://www.w3.org/2005/xpath-functions}doc"
    )
    assert _override_symbol(parser, "my:doc") == "{urn:my}doc"
    assert _override_symbol(parser, "my:other") == "{urn:my}other"
    assert _override_symbol(parser, "plain") == "plain"


def test_constructor_error_codes_keep_upstream_precedence() -> None:
    """A constructor re-raises an elementpath cast error unchanged.

    ``xs:integer(xs:double('NaN'))`` and its infinity form are F&O
    ``FOCA0002`` failures raised by elementpath's own cast; the corrected
    constructor must not convert their ``ElementPathValueError``/
    ``ElementPathTypeError`` (subclasses of ``ValueError``/``TypeError``) into
    ``FORG0001``.
    """
    engine = XPathEngine()
    for source in ("xs:integer(xs:double('NaN'))", "xs:integer(xs:double('INF'))"):
        with pytest.raises(Exception) as caught:
            engine.evaluate(engine.compile(source), ExpressionEnvironment())
        assert _error_code(caught.value) == "FOCA0002", source


def test_a_lookup_expression_is_a_path_step() -> None:
    """XPath 3.1 section 3.3.1.2: ``E?*`` is a PostfixExpr, so ``?*/child`` is legal."""
    document = _document(
        b"<doc><item><price>1</price></item><item><price>2</price></item>"
        b"<extra><price>3</price></extra></doc>"
    )
    engine = XPathEngine("3.1")
    compiled = engine.compile(
        "string-join([/doc/item, /doc/extra]?* / price, ' ')"
        " || '|' || string-join([/doc/item[1]]?* / price, ' ')",
        XPathStaticContext(),
    )
    values = engine.evaluate(compiled, ExpressionEnvironment(), focus=document)
    assert values == (XdmAtomic.string("1 2 3|1"),)


def test_a_lookup_path_step_rejects_a_non_node_intermediate() -> None:
    """Only nodes can be path steps; an atomic lookup result is XPTY0019."""
    engine = XPathEngine("3.1")
    compiled = engine.compile("count([1, 2]?* / self::node())", XPathStaticContext())
    with pytest.raises(Exception) as caught:
        engine.evaluate(compiled, ExpressionEnvironment(), focus=XdmAtomic.integer(0))
    assert _error_code(caught.value) == "XPTY0019"


def test_a_union_of_temporary_trees_uses_creation_order_in_both_operand_orders() -> None:
    """Cross-tree document order is total and stable, not hash order.

    elementpath sorts a union by each adapter's ``position``; a document node is
    position 0 in every tree, so two temporary trees tie and the hash-ordered
    ``set`` decided the result.  Each tree's own range position keeps the
    result a document order: the earlier-created tree first, whatever the
    operand order.
    """
    body = _stylesheet(
        """
        <xsl:template name="main">
          <xsl:variable name="first"><settings><setting1/></settings></xsl:variable>
          <xsl:variable name="second"><settings><setting2/></settings></xsl:variable>
          <xsl:variable name="third"><settings><setting3/></settings></xsl:variable>
          <xsl:sequence select="(
            ($first | $second)/settings/*/name(),
            ($second | $first)/settings/*/name(),
            ($third | $second | $first)/settings/*/name()
          )"/>
        </xsl:template>
        """
    )
    compiled = XsltProcessor().compile(io.BytesIO(body))
    result = compiled.transform(initial_template="main", delivery="raw")

    assert tuple(item.string_value for item in result.principal) == (
        "setting1",
        "setting2",
        "setting1",
        "setting2",
        "setting1",
        "setting2",
        "setting3",
    )


def test_a_node_set_operator_requires_nodes_and_a_dynamic_context() -> None:
    """The ordered set operators keep the substrate's two guards."""
    engine = XPathEngine("3.1")
    compiled = engine.compile("(1 | 2)", XPathStaticContext())
    with pytest.raises(Exception) as caught:
        engine.evaluate(compiled, ExpressionEnvironment(), focus=_document())
    assert _error_code(caught.value) == "XPTY0004"

    parser = _build_parser("3.1", XPathStaticContext(), key=False)
    token = parser.parse("(1 | 2)")
    with pytest.raises(Exception) as absent:
        list(token.select(None))
    assert _error_code(absent.value) == "XPDY0002"
