"""Typed host conversion for Python extension functions.

Only the explicit registry seam adapts host values.  Arguments arrive as XDM
nodes, maps, arrays, functions, or typed atomic payloads; results are validated
against the declared type and cardinality.  ``None`` is never an empty
sequence, and a wrong type or count is ``XPTY0004``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from pyxsd import xsd_data_types as xsd
from pyxsd.xdm import XdmArray, XdmAtomic, XdmFunction, XdmMap
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xslt import ExtensionSignature, XsltDynamicError, XsltExtensions
from pyxsd.xslt.extensions import EXTENSION_ERROR_CODE, ExtensionTarget

APP = "urn:app"


def app(local: str) -> str:
    return f"{{{APP}}}{local}"


def integer_argument(value: int) -> int:
    return value + 1


def decimal_argument(value: Decimal) -> Decimal:
    return value + Decimal("0.5")


def double_argument(value: float) -> float:
    return value * 0.5


def boolean_argument(value: bool) -> bool:
    return not value


def string_argument(value: str) -> str:
    return value.upper()


def qname_argument(value: Any) -> Any:
    assert isinstance(value, XdmAtomic)
    return value


def target(callback: Any, parameters: tuple[str, ...], result: str) -> Any:
    extensions = XsltExtensions().register_function(
        app("f"),
        len(parameters),
        callback,
        signature=ExtensionSignature(parameters, result),
    )
    return extensions.runtime_targets()[app("f")]


def invocation_error(call: Any) -> XsltDynamicError:
    with pytest.raises(XsltDynamicError) as caught:
        call()
    assert caught.value.code == EXTENSION_ERROR_CODE
    return caught.value


@pytest.mark.parametrize(
    ("callback", "parameters", "result", "item", "expected"),
    [
        (integer_argument, ("xs:integer",), "xs:integer", 4, XdmAtomic.integer(5)),
        (decimal_argument, ("xs:decimal",), "xs:decimal", "1.5", XdmAtomic.decimal("2.0")),
        (double_argument, ("xs:double",), "xs:double", 3.0, XdmAtomic.double(1.5)),
        (double_argument, ("xs:float",), "xs:double", 3.0, XdmAtomic.double(1.5)),
        (boolean_argument, ("xs:boolean",), "xs:boolean", True, XdmAtomic.boolean(False)),
        (string_argument, ("xs:string",), "xs:string", "ab", XdmAtomic.string("AB")),
        (
            string_argument,
            ("xs:untypedAtomic",),
            "xs:string",
            "ab",
            XdmAtomic.string("AB"),
        ),
        (
            string_argument,
            ("xs:anyURI",),
            "xs:string",
            "urn:one",
            XdmAtomic.string("URN:ONE"),
        ),
    ],
)
def test_atomic_payloads_convert_to_python_values(
    callback: Any,
    parameters: tuple[str, ...],
    result: str,
    item: Any,
    expected: XdmAtomic,
) -> None:
    argument = item if isinstance(item, XdmAtomic) else _atomic(parameters[0], item)

    assert target(callback, parameters, result).invoke_xdm(((argument,),)) == (expected,)


def _atomic(type_name: str, value: Any) -> XdmAtomic:
    local = type_name.partition(":")[2]
    if local == "integer":
        return XdmAtomic.integer(value)
    if local == "decimal":
        return XdmAtomic.decimal(value)
    if local == "double":
        return XdmAtomic.double(value)
    if local == "float":
        return XdmAtomic.float(value)
    if local == "boolean":
        return XdmAtomic.boolean(value)
    if local == "untypedAtomic":
        return XdmAtomic.untyped_atomic(value)
    if local == "anyURI":
        return XdmAtomic.any_uri(value)
    return XdmAtomic.string(value)


def test_qname_keeps_its_xdm_identity() -> None:
    atomic = XdmAtomic.typed(xsd.QName("p:local"), xsd.QName)

    assert target(qname_argument, ("xs:QName",), "xs:QName").invoke_xdm(((atomic,),)) == (atomic,)


def test_node_map_array_and_function_values_pass_through() -> None:
    node = XdmDocument.parse(b"<root/>")
    mapping = XdmMap.from_mapping({"a": 1})
    array = XdmArray((1, 2))
    function = XdmFunction(lambda arguments, environment, closure: (), arity=0)

    def collect(*values: Any) -> tuple[Any, ...]:
        return values

    converted = target(
        collect,
        ("node()", "map(*)", "array(*)", "function(*)"),
        "item()*",
    ).invoke_xdm(((node,), (mapping,), (array,), (function,)))

    assert converted == (node, mapping, array, function)


def test_element_and_document_node_are_checked() -> None:
    document = XdmDocument.parse(b"<root><child/></root>")
    element = document.root_element

    def identity(value: Any) -> Any:
        return value

    assert target(identity, ("document-node()",), "document-node()").invoke_xdm(((document,),)) == (
        document,
    )
    assert target(identity, ("element()",), "element()").invoke_xdm(((element,),)) == (element,)

    invocation_error(
        lambda: target(identity, ("element()",), "element()").invoke_xdm(((document,),))
    )


def test_sequence_cardinality_returns_none_tuples_and_singletons() -> None:
    received: list[Any] = []

    def optional(value: Any) -> Any:
        received.append(value)
        return () if value is None else value

    maybe = target(optional, ("xs:integer?",), "xs:integer?")
    assert maybe.invoke_xdm(((),)) == ()
    assert received[-1] is None
    assert maybe.invoke_xdm(((XdmAtomic.integer(1),),)) == (XdmAtomic.integer(1),)
    assert received[-1] == 1
    invocation_error(lambda: maybe.invoke_xdm(((XdmAtomic.integer(1), XdmAtomic.integer(2)),)))

    def many(values: Any) -> Any:
        received.append(values)
        return values

    sequence = target(many, ("xs:integer*",), "xs:integer*")
    assert sequence.invoke_xdm(((),)) == ()
    assert received[-1] == ()
    assert sequence.invoke_xdm(((XdmAtomic.integer(1), XdmAtomic.integer(2)),)) == (
        XdmAtomic.integer(1),
        XdmAtomic.integer(2),
    )
    assert received[-1] == (1, 2)

    required = target(many, ("xs:integer+",), "xs:integer+")
    invocation_error(lambda: required.invoke_xdm(((),)))

    def identity(value: Any) -> Any:
        return value

    singleton = target(identity, ("xs:integer",), "xs:integer")
    invocation_error(lambda: singleton.invoke_xdm(((),)))
    invocation_error(lambda: singleton.invoke_xdm(((XdmAtomic.integer(1), XdmAtomic.integer(2)),)))
    invocation_error(lambda: singleton.invoke_xdm(()))
    invocation_error(
        lambda: singleton.invoke_xdm(((XdmAtomic.integer(1),), (XdmAtomic.integer(2),)))
    )


def test_numeric_promotion_is_accepted_for_arguments() -> None:
    def as_float(value: float) -> float:
        return float(value)

    assert target(as_float, ("xs:double",), "xs:double").invoke_xdm(((XdmAtomic.integer(2),),)) == (
        XdmAtomic.double(2.0),
    )
    invocation_error(
        lambda: target(lambda value: value, ("xs:integer",), "xs:integer").invoke_xdm(
            ((XdmAtomic.double(2.0),),)
        )
    )


def test_wrong_argument_item_type_is_xty0004() -> None:
    invocation_error(
        lambda: target(lambda value: value, ("xs:integer",), "xs:integer").invoke_xdm(
            ((XdmAtomic.string("two"),),)
        )
    )
    invocation_error(
        lambda: target(lambda value: value, ("node()",), "node()").invoke_xdm(
            ((XdmAtomic.integer(1),),)
        )
    )


def test_none_result_is_an_error_even_for_optional_results() -> None:
    def none_result(value: Any) -> None:
        del value
        return None

    for result in ("item()*", "xs:integer?", "node()*"):
        invocation_error(
            lambda result=result: target(none_result, ("xs:integer",), result).invoke_xdm(
                ((XdmAtomic.integer(1),),)
            )
        )


def test_result_type_and_cardinality_are_validated() -> None:
    def string_result(value: Any) -> str:
        del value
        return "text"

    invocation_error(
        lambda: target(string_result, ("xs:integer",), "xs:integer").invoke_xdm(
            ((XdmAtomic.integer(1),),)
        )
    )

    def two_results(value: Any) -> tuple[Any, ...]:
        del value
        return (1, 2)

    invocation_error(
        lambda: target(two_results, ("xs:integer",), "xs:integer").invoke_xdm(
            ((XdmAtomic.integer(1),),)
        )
    )
    invocation_error(
        lambda: target(two_results, ("xs:integer",), "xs:integer?").invoke_xdm(
            ((XdmAtomic.integer(1),),)
        )
    )
    invocation_error(
        lambda: target(lambda value: (), ("xs:integer",), "xs:integer+").invoke_xdm(
            ((XdmAtomic.integer(1),),)
        )
    )


def test_exact_declared_atomic_results_are_returned_unchanged() -> None:
    atomic = XdmAtomic.string("text")
    (converted,) = target(lambda: atomic, (), "xs:string").invoke_xdm(())
    assert converted is atomic

    token = XdmAtomic.typed(xsd.Token("name"), xsd.Token)
    (subtype,) = target(lambda: token, (), "xs:string").invoke_xdm(())
    assert subtype is token


def test_declared_results_are_converted_to_the_declared_atomic_type() -> None:
    (uri,) = target(lambda: "urn:example", (), "xs:anyURI").invoke_xdm(())
    assert uri.type_name == "{http://www.w3.org/2001/XMLSchema}anyURI"
    assert uri.lexical == "urn:example"

    (single,) = target(lambda: 1.5, (), "xs:float").invoke_xdm(())
    assert single.type_name == "{http://www.w3.org/2001/XMLSchema}float"
    assert single == XdmAtomic.float(1.5)

    (promoted,) = target(lambda: 5, (), "xs:double").invoke_xdm(())
    assert promoted.type_name == "{http://www.w3.org/2001/XMLSchema}double"
    assert promoted == XdmAtomic.double(5)

    (raw,) = target(lambda: XdmAtomic.untyped_atomic("text"), (), "xs:string").invoke_xdm(())
    assert raw.type_name == "{http://www.w3.org/2001/XMLSchema}string"
    assert raw == XdmAtomic.string("text")


def test_uncastable_declared_results_are_xpty0004() -> None:
    error = invocation_error(lambda: target(lambda: "not-a-date", (), "xs:date").invoke_xdm(()))
    assert isinstance(error.cause, TypeError)
    assert error.__cause__ is error.cause
    invocation_error(lambda: target(lambda: "text", (), "xs:integer").invoke_xdm(()))


def test_qname_and_notation_results_keep_their_xdm_identity() -> None:
    qname = XdmAtomic.typed(xsd.QName("p:local"), xsd.QName)
    (converted,) = target(lambda: qname, (), "xs:QName").invoke_xdm(())
    assert converted is qname

    notation = XdmAtomic.typed(xsd.NOTATION("p:local"), xsd.NOTATION)
    (kept,) = target(lambda: notation, (), "xs:NOTATION").invoke_xdm(())
    assert kept is notation

    invocation_error(lambda: target(lambda: "p:local", (), "xs:QName").invoke_xdm(()))
    invocation_error(lambda: target(lambda: "p:local", (), "xs:NOTATION").invoke_xdm(()))


def test_wrong_declared_result_cardinality_is_xpty0004() -> None:
    def two_floats() -> tuple[float, float]:
        return (1.5, 2.5)

    invocation_error(lambda: target(two_floats, (), "xs:float").invoke_xdm(()))


def test_result_host_values_and_xdm_values_both_convert() -> None:
    def mixed() -> tuple[Any, ...]:
        return (1, XdmAtomic.string("two"), Decimal("3.5"))

    converted = target(mixed, (), "item()*").invoke_xdm(())
    assert converted == (
        XdmAtomic.integer(1),
        XdmAtomic.string("two"),
        XdmAtomic.decimal("3.5"),
    )


def test_callback_exception_keeps_identity_and_cause() -> None:
    def explode() -> None:
        raise ValueError("host failure")

    with pytest.raises(XsltDynamicError) as caught:
        target(explode, (), "item()*").invoke_xdm(())
    assert caught.value.code is None
    assert caught.value.allow_catch is False
    assert isinstance(caught.value.cause, ValueError)
    assert caught.value.__cause__ is caught.value.cause


def test_public_signature_constants() -> None:
    from pyxsd.xslt import no_args as exported_no_args
    from pyxsd.xslt import xs_integer_to_integer as exported_integer

    assert exported_no_args == ExtensionSignature((), "item()*")
    assert exported_integer == ExtensionSignature(("xs:integer",), "xs:integer")


def test_signature_validation_edges() -> None:
    with pytest.raises(TypeError, match="tuple"):
        ExtensionSignature(["xs:integer"], "item()*")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unsupported"):
        ExtensionSignature((" xs:integer",), "item()*")
    with pytest.raises(ValueError, match="unsupported"):
        ExtensionSignature(("xs:integer",), "")
    with pytest.raises(ValueError, match="unsupported"):
        ExtensionSignature(("unknown:integer",), "item()*")
    with pytest.raises(ValueError, match="unsupported"):
        ExtensionSignature(("xs:missing",), "item()*")

    parsed = ExtensionSignature(("item()?", "node()*", "xs:integer+"), "map(*)")
    assert [parameter.occurrence for parameter in parsed.parsed_parameters] == [
        "zero-or-one",
        "zero-or-more",
        "one-or-more",
    ]
    assert parsed.parsed_result.occurrence == "one"


@pytest.mark.parametrize(
    ("type_name", "value"),
    [
        ("node()", XdmAtomic.integer(1)),
        ("element()", XdmDocument.parse(b"<root/>")),
        (
            "document-node()",
            XdmDocument.parse(b"<root/>").root_element,
        ),
        ("map(*)", XdmArray((1,))),
        ("array(*)", XdmMap.from_mapping({"a": 1})),
        ("function(*)", XdmMap.from_mapping({"a": 1})),
        ("xs:integer", XdmMap.from_mapping({"a": 1})),
    ],
)
def test_argument_structural_mismatches(type_name: str, value: Any) -> None:
    invocation_error(
        lambda: target(lambda item: item, (type_name,), "item()*").invoke_xdm(((value,),))
    )


@pytest.mark.parametrize(
    ("type_name", "value"),
    [
        ("node()", XdmDocument.parse(b"<root/>")),
        ("element()", XdmDocument.parse(b"<root/>").root_element),
        ("document-node()", XdmDocument.parse(b"<root/>")),
        ("map(*)", XdmMap.from_mapping({"a": 1})),
        ("array(*)", XdmArray((1,))),
        (
            "function(*)",
            XdmFunction(lambda arguments, environment, closure: (), arity=0),
        ),
    ],
)
def test_result_structural_types(type_name: str, value: Any) -> None:
    def identity() -> Any:
        return value

    assert target(identity, (), type_name).invoke_xdm(()) == (value,)


def test_result_structural_mismatch_is_xty0004() -> None:
    def atomic_result() -> Any:
        return XdmAtomic.integer(1)

    invocation_error(lambda: target(atomic_result, (), "node()").invoke_xdm(()))


def test_integer_promotes_to_decimal_payload() -> None:
    def as_decimal(value: Decimal) -> Decimal:
        assert isinstance(value, Decimal)
        return value

    assert target(as_decimal, ("xs:decimal",), "xs:decimal").invoke_xdm(
        ((XdmAtomic.integer(2),),)
    ) == (XdmAtomic.decimal("2"),)


def test_item_argument_accepts_every_xdm_item() -> None:
    def identity(value: Any) -> Any:
        return value if isinstance(value, tuple) else (value,)

    node = XdmDocument.parse(b"<root/>")
    mapping = XdmMap.from_mapping({"a": 1})
    assert target(identity, ("item()",), "item()*").invoke_xdm(((node,),)) == (node,)
    assert target(identity, ("item()",), "item()*").invoke_xdm(((mapping,),)) == (mapping,)


def test_string_family_accepts_untyped_and_uri_arguments() -> None:
    def identity(value: str) -> str:
        return value

    converted = target(identity, ("xs:string",), "xs:string")
    assert converted.invoke_xdm(((XdmAtomic.untyped_atomic("value"),),)) == (
        XdmAtomic.string("value"),
    )
    assert converted.invoke_xdm(((XdmAtomic.any_uri("urn:value"),),)) == (
        XdmAtomic.string("urn:value"),
    )


def test_unusable_result_is_xty0004() -> None:
    def unusable() -> Any:
        return object()

    invocation_error(lambda: target(unusable, (), "item()*").invoke_xdm(()))


def test_extension_target_and_registry_validation() -> None:
    with pytest.raises(TypeError, match="signature"):
        XsltExtensions().register_function(app("f"), 1, integer_argument, "xs:integer")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="contextual"):
        XsltExtensions().register_function(
            app("f"),
            0,
            lambda: (),
            contextual="yes",  # type: ignore[arg-type]
        )

    extensions = XsltExtensions().register_function(app("f"), 0, lambda: ())
    definition = next(iter(extensions.functions.values()))

    with pytest.raises(ValueError, match="at least one"):
        ExtensionTarget(())
    with pytest.raises(TypeError, match="ExtensionFunction"):
        ExtensionTarget((object(),))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="context item"):
        ExtensionTarget((definition,), context_item="text")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="fallback"):
        ExtensionTarget((definition,), fallback=object())

    with pytest.raises(TypeError, match="ExtensionFunction"):
        XsltExtensions({(app("f"), 0): object()})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="key"):
        XsltExtensions({(app("other"), 0): definition})
    with pytest.raises(TypeError, match="Collation"):
        XsltExtensions(_collations={app("collation"): object()})
    with pytest.raises(TypeError, match="Collation"):
        XsltExtensions(_collations={1: definition})  # type: ignore[dict-item]
