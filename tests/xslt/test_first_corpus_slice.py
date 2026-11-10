"""First corpus smoke slice for the public processor.

Only cases this vertical slice genuinely executes are selected.  Each case is
compiled and run through the Task 3 harness, which evaluates every declared
assertion independently; any adapter gap or skipped assertion is a failure
here, never silent coverage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import pytest
from tests.xslt.assertions import ResultSnapshot
from tests.xslt.catalog import load_catalog
from tests.xslt.model import Param, TestCase
from tests.xslt.outcomes import Outcome
from tests.xslt.runner import run_cases
from tests.xslt.selection import XSLT30_NON_STREAMING

from pyxsd.xdm import XdmArray, XdmAtomic, XdmFunction, XdmMap
from pyxsd.xdm.nodes import NodeKind, XdmNode
from pyxsd.xpath.engine import XPathEngine
from pyxsd.xpath.environment import ExpressionEnvironment, XPathStaticContext
from pyxsd.xslt import TransformationResult, XsltProcessor

CORPUS = Path(__file__).parent / "corpus"

#: Cases whose entry point and instruction subset this slice executes.
SELECTED = {
    "package-version/package-version-001": frozenset({"string"}),
    "initial-template/initial-template-002": frozenset({"xml"}),
    "initial-function/initial-function-002": frozenset({"count", "value"}),
    "initial-function/initial-function-100e": frozenset({"count", "type"}),
    "seqtor/seqtor-001": frozenset({"string"}),
    "seqtor/seqtor-002": frozenset({"string"}),
}


def _engine() -> XPathEngine:
    return XPathEngine("3.1")


def _evaluate_select(select: str) -> tuple[Any, ...]:
    engine = _engine()
    compiled = engine.compile(select, XPathStaticContext())
    return engine.evaluate(compiled, ExpressionEnvironment())


def _parameter_values(parameters: tuple[Param, ...]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for parameter in parameters:
        if parameter.name is None:
            continue
        if parameter.select is None:
            raise pytest.fail(f"smoke parameter {parameter.name!r} has no select expression")
        values[parameter.name] = _evaluate_select(parameter.select)
    return values


def _document_xml(node: XdmNode) -> str:
    def build(source: XdmNode) -> Any:
        element = ET.Element(source.name or source.local_name or "node")
        for attribute in source.attributes:
            element.set(attribute.name, attribute.value)
        for child in source.children:
            if child.kind is NodeKind.TEXT:
                if len(element):
                    element[-1].tail = (element[-1].tail or "") + child.string_value
                else:
                    element.text = (element.text or "") + child.string_value
            elif child.kind is NodeKind.ELEMENT:
                element.append(build(child))
            elif child.kind is NodeKind.COMMENT:
                element.append(ET.Comment(child.string_value))
            elif child.kind is NodeKind.PROCESSING_INSTRUCTION:
                element.append(ET.ProcessingInstruction(child.target or "", child.string_value))
        return element

    elements = [child for child in node.children if child.kind is NodeKind.ELEMENT]
    if len(elements) == 1 and len(node.children) == 1:
        return ET.tostring(build(elements[0]), encoding="unicode")
    return escape("".join(child.string_value for child in node.children))


def _value(item: Any) -> Any:
    if isinstance(item, XdmAtomic):
        return item.value
    if isinstance(item, XdmMap):
        return {key.value: _value_sequence(value) for key, value in item.entries}
    if isinstance(item, XdmArray):
        return [_value_sequence(member) for member in item.members]
    if isinstance(item, XdmFunction):
        return item
    if isinstance(item, XdmNode):
        return _document_xml(item)
    return item


def _value_sequence(sequence: Any) -> Any:
    values = tuple(_value(item) for item in sequence)
    return values[0] if len(values) == 1 else values


def _snapshot(result: TransformationResult) -> ResultSnapshot:
    entry = result.principal_entry
    if entry.tree is not None:
        return ResultSnapshot(
            xml=_document_xml(entry.tree),
            secondary={uri: _snapshot_entry(value) for uri, value in result.secondary.items()},
            messages=(),
            warnings=(),
        )
    return ResultSnapshot(
        raw=tuple(_value(item) for item in entry.raw),
        secondary={uri: _snapshot_entry(value) for uri, value in result.secondary.items()},
        messages=(),
        warnings=(),
    )


def _snapshot_entry(entry: Any) -> ResultSnapshot:
    if entry.tree is not None:
        return ResultSnapshot(xml=_document_xml(entry.tree), messages=(), warnings=())
    return ResultSnapshot(
        raw=tuple(_value(item) for item in entry.raw),
        messages=(),
        warnings=(),
    )


def _delivery(case: TestCase) -> str:
    output = case.invocation.output or case.environment.output
    if output is not None and output.tree is False:
        return "raw"
    if output is not None and output.tree is True:
        return "document"
    return "stylesheet"


def _execute(case: TestCase) -> ResultSnapshot:
    stylesheets = [sheet for sheet in case.stylesheets if sheet.role != "secondary"]
    if len(stylesheets) != 1:
        raise AssertionError(f"smoke case {case.name!r} must have one principal stylesheet")
    if case.document_root is None:
        raise AssertionError(f"smoke case {case.name!r} has no corpus root")
    stylesheet = case.document_root / Path(str(case.stylesheets[0].path))
    invocation = case.invocation
    static_parameters = _parameter_values(
        tuple(parameter for parameter in invocation.params if parameter.static)
    )
    parameters = _parameter_values(
        tuple(
            parameter
            for parameter in (*case.environment.params, *invocation.params)
            if not parameter.static
        )
    )
    template_parameters: dict[str, Any] = {}
    tunnel_parameters: dict[str, Any] = {}
    if invocation.initial_template is not None:
        for parameter in invocation.initial_template.params:
            if parameter.name is None or parameter.select is None:
                continue
            value = _evaluate_select(parameter.select)
            if parameter.tunnel:
                tunnel_parameters[parameter.name] = value
            else:
                template_parameters[parameter.name] = value
    arguments: tuple[Any, ...] = ()
    if invocation.initial_function is not None:
        arguments = tuple(
            value
            for parameter in invocation.initial_function.params
            if parameter.select is not None
            for value in (_evaluate_select(parameter.select),)
        )
    compiled = XsltProcessor().compile(
        stylesheet,
        static_parameters=static_parameters,
    )
    result = compiled.transform(
        parameters=parameters,
        template_parameters=template_parameters,
        tunnel_parameters=tunnel_parameters,
        initial_template=(
            invocation.initial_template.name if invocation.initial_template is not None else None
        ),
        initial_mode=(
            invocation.initial_mode.name if invocation.initial_mode is not None else None
        ),
        initial_function=(
            invocation.initial_function.name if invocation.initial_function is not None else None
        ),
        initial_function_arguments=arguments,
        delivery=_delivery(case),
    )
    return _snapshot(result)


def test_selected_cases_exist_and_are_applicable() -> None:
    catalog = load_catalog(CORPUS / "catalog.xml")

    for name in SELECTED:
        case = catalog.cases[name]
        assert case.stylesheet_role == "principal"
        assert XSLT30_NON_STREAMING.supports("schema_aware")
        assert case.result_xml.strip()


def test_first_corpus_slice_executes_every_selected_assertion() -> None:
    catalog = load_catalog(CORPUS / "catalog.xml")
    cases = tuple(catalog.cases[name] for name in SELECTED)

    results = run_cases(cases, _execute, XSLT30_NON_STREAMING)

    assert [result.name for result in results] == list(SELECTED)
    for result in results:
        assert result.outcome is Outcome.PASS, result.message
    assert {result.name: result.coverage for result in results} == dict(SELECTED)
    assert not any(result.outcome is Outcome.ADAPTER_GAP for result in results)
    assert not any(result.outcome is Outcome.CORPUS_DISPUTE for result in results)
