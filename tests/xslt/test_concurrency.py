"""Overlapping invocations of shared compiled XSLT and XPath artifacts."""

from __future__ import annotations

import io
from collections.abc import Callable
from concurrent.futures import Future, wait
from threading import Barrier, Event, Thread
from typing import TypeVar

import pytest
from tests.xslt.support import stylesheet

from pyxsd.schema import Schema
from pyxsd.xdm import XdmAtomic, XdmFunction
from pyxsd.xdm.errors import XdmInputError
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xpath.engine import XPathEngine, XPathParserCache
from pyxsd.xpath.environment import ExpressionEnvironment, XPathStaticContext
from pyxsd.xslt import ExtensionSignature, XsltExtensions, XsltProcessor
from pyxsd.xslt.runtime.evaluate import DynamicExpressionCache

_APP = "urn:concurrency"
_TIMEOUT = 60
_T = TypeVar("_T")


def _run_workers(
    barrier: Barrier, work: Callable[[int], _T], *, timeout: float = 2 * _TIMEOUT
) -> list[_T]:
    errors: list[BaseException] = []
    futures: list[Future[_T]] = [Future() for _ in range(barrier.parties)]

    def guarded(index: int) -> None:
        try:
            result = work(index)
        except BaseException as error:
            errors.append(error)
            barrier.abort()
            futures[index].set_exception(error)
        else:
            futures[index].set_result(result)

    # ThreadPoolExecutor joins its workers at interpreter shutdown, even after
    # shutdown(wait=False). Raw daemon threads let pytest exit after a deadlock.
    for index in range(barrier.parties):
        Thread(target=guarded, args=(index,), daemon=True).start()
    _, pending = wait(futures, timeout=timeout)
    if pending:
        barrier.abort()
        stuck = ", ".join(
            f"worker {index}" for index, future in enumerate(futures) if future in pending
        )
        raise AssertionError(f"timed out waiting for {stuck}")
    if errors:
        # Report the original worker failure, rather than a peer's broken barrier.
        raise errors[0]
    return [future.result(timeout=0) for future in futures]


def _sheet(body: str) -> io.BytesIO:
    return io.BytesIO(stylesheet(body, namespaces={"app": _APP}))


def test_worker_timeout_names_stuck_worker():
    release = Event()
    finished = Event()
    barrier = Barrier(2, timeout=_TIMEOUT)

    def worker(index: int) -> None:
        if index == 1:
            release.wait()
            finished.set()

    try:
        with pytest.raises(AssertionError, match=r"timed out waiting for worker 1$"):
            _run_workers(barrier, worker, timeout=0.25)
    finally:
        release.set()
    assert finished.wait(_TIMEOUT)


def test_worker_exception_is_not_masked_by_peer_barrier_failure():
    barrier = Barrier(2, timeout=_TIMEOUT)
    peer_ready = Event()

    def worker(index: int) -> None:
        if index == 1:
            assert peer_ready.wait(_TIMEOUT)
            raise ValueError("original worker error")
        peer_ready.set()
        barrier.wait()

    with pytest.raises(ValueError, match="original worker error"):
        _run_workers(barrier, worker)


@pytest.mark.parametrize("workers", [1, 3])
def test_callback_reenters_same_stylesheet_with_independent_dynamic_state(workers):
    barrier = Barrier(workers, timeout=_TIMEOUT)

    def reenter(label: str) -> str:
        barrier.wait()
        inner_label = f"inner-{label}"
        inner = run(inner_label, 10, inner=True)
        assert inner.principal == expected(inner_label, 10, "leaf")
        assert inner.secondary[uri].tree.string_value == f"{inner_label}:10"
        assert list(inner.secondary) == [uri]
        barrier.wait()
        return inner.principal[-2].value

    extensions = XsltExtensions().register_function(
        f"{{{_APP}}}reenter", 1, reenter, ExtensionSignature(("xs:string",), "xs:string")
    )
    compiled = XsltProcessor().compile(
        _sheet("""
        <xsl:param name="inner" as="xs:boolean"/>
        <xsl:param name="label" as="xs:string"/>
        <xsl:variable name="global" select="concat($label, ':', doc/n[1]/@v)"/>
        <xsl:mode use-accumulators="#all"/>
        <xsl:key name="by-group" match="n" use="@group"/>
        <xsl:accumulator name="sum" as="xs:integer" initial-value="0">
          <xsl:accumulator-rule match="n" select="$value + xs:integer(@v)"/>
        </xsl:accumulator>
        <xsl:template match="/">
          <xsl:for-each-group select="doc/n" group-by="@group">
            <xsl:variable name="local" select="concat($global, ':', @v)"/>
            <xsl:variable name="before" select="accumulator-before('sum')"/>
            <xsl:variable name="key" select="string-join(key('by-group', @group)/@v, ',')"/>
            <xsl:result-document href="urn:shared-result">
              <out><xsl:value-of select="$global"/></out>
            </xsl:result-document>
            <xsl:sequence select="
              $local, $before, $key,
              (if ($inner) then 'leaf' else app:reenter($label)),
              string(@v), string(current-grouping-key()),
              string-join(current-group()/@v, ','),
              accumulator-after('sum'), string-join(key('by-group', @group)/@v, ','),
              $global, concat(position(), '/', last())"/>
          </xsl:for-each-group>
        </xsl:template>
        """),
        extensions=extensions,
    )
    uri = "urn:shared-result"

    def run(label: str, first: int, *, inner: bool):
        source = f'<doc><n group="{label}" v="{first}"/><n group="{label}" v="{first + 1}"/></doc>'
        return compiled.transform(
            source, delivery="raw", parameters={"label": label, "inner": inner}
        )

    def expected(label: str, first: int, callback: str):
        values = f"{first},{first + 1}"
        return (
            XdmAtomic.string(f"{label}:{first}:{first}"),
            XdmAtomic.integer(first),
            XdmAtomic.string(values),
            XdmAtomic.string(callback),
            XdmAtomic.string(str(first)),
            XdmAtomic.string(label),
            XdmAtomic.string(values),
            XdmAtomic.integer(first),
            XdmAtomic.string(values),
            XdmAtomic.string(f"{label}:{first}"),
            XdmAtomic.string("1/1"),
        )

    def worker(index: int) -> None:
        label = f"worker-{index}"
        first = index + 1
        outer = run(label, first, inner=False)
        assert outer.principal == expected(label, first, f"inner-{label}:10")
        assert list(outer.secondary) == [uri]
        assert outer.secondary[uri].tree.string_value == f"{label}:{first}"

    _run_workers(barrier, worker)


def test_one_compiled_xpath_uses_each_threads_focus_variables_and_functions():
    barrier = Barrier(3, timeout=_TIMEOUT)
    engine = XPathEngine()
    compiled = engine.compile(
        "(., $n, app:pause(), ., $n, app:value())",
        XPathStaticContext(
            namespaces={"app": _APP},
            variable_types={"n": "xs:integer"},
            functions={"app:pause": 0, "app:value": 0},
        ),
    )

    def worker(index: int) -> None:
        focus = XdmAtomic.integer(index + 20)
        variable = XdmAtomic.integer(index + 10)

        def pause():
            barrier.wait()
            return ()

        environment = ExpressionEnvironment(
            variables={"n": variable},
            functions={"app:pause": pause, "app:value": lambda: index},
        )
        assert engine.evaluate(compiled, environment, focus=focus) == (
            focus,
            variable,
            focus,
            variable,
            XdmAtomic.integer(index),
        )

    _run_workers(barrier, worker)


def test_closures_partial_applications_and_references_keep_their_invocation():
    barrier = Barrier(3, timeout=_TIMEOUT)

    def pause(focus: XdmAtomic) -> int:
        barrier.wait()
        return focus.value

    extensions = XsltExtensions().register_function(
        f"{{{_APP}}}pause", 0, pause, ExtensionSignature((), "xs:integer"), contextual=True
    )
    compiled = XsltProcessor().compile(
        _sheet("""
        <xsl:param name="n" as="xs:integer"/>
        <xsl:function name="app:add" as="xs:string">
          <xsl:param name="a" as="xs:integer"/>
          <xsl:param name="b" as="xs:integer"/>
          <xsl:sequence select="($n + $a + $b) ! concat(., ':', app:pause(), ':', .)"/>
        </xsl:function>
        <xsl:template name="xsl:initial-template">
          <xsl:variable name="local" select="$n * 10"/>
          <xsl:variable name="f" select="function($a, $b){
            ($local + $a + $b) ! concat(., ':', app:pause(), ':', .)
          }"/>
          <xsl:variable name="partial" select="app:add(?, 1)"/>
          <xsl:variable name="ref" select="app:add#2"/>
          <xsl:sequence select="$f, $partial, $ref"/>
        </xsl:template>
        """),
        extensions=extensions,
    )

    # All workers also call the same exported closure with different arguments.
    shared_closure, _, _ = compiled.transform(delivery="raw", parameters={"n": 100}).principal

    def worker(index: int) -> None:
        n = index + 1
        result = compiled.transform(delivery="raw", parameters={"n": n})
        closure, partial, reference = result.principal
        # Each call reaches one barrier generation inside the function body.
        for function, args, value in (
            (closure, (2, 3), n * 10 + 5),
            (partial, (4,), n + 5),
            (reference, (6, 7), n + 13),
            (shared_closure, (index, 3), 1003 + index),
        ):
            assert isinstance(function, XdmFunction)
            assert function.invoke(args) == (XdmAtomic.string(f"{value}:{value}:{value}"),)

    _run_workers(barrier, worker)


def test_deferred_functions_from_one_parser_keep_separate_callback_targets():
    barrier = Barrier(2, timeout=_TIMEOUT)
    engine = XPathEngine()
    compiled = engine.compile(
        "function(){(app:pause(), app:value())}",
        XPathStaticContext(
            namespaces={"app": _APP},
            functions={"app:pause": 0, "app:value": 0},
        ),
    )
    cache = XPathParserCache()

    def pause_first() -> tuple[()]:
        barrier.wait()  # The second worker can now enter its function.
        barrier.wait()  # Its callback targets are now installed.
        return ()

    def pause_second() -> tuple[()]:
        barrier.wait()
        barrier.wait()  # Stay suspended until the first worker reads its target.
        return ()

    def capture(pause: Callable[[], tuple[()]], value: int) -> XdmFunction:
        environment = ExpressionEnvironment(
            parser_cache=cache, functions={"app:pause": pause, "app:value": lambda: value}
        )
        (function,) = engine.evaluate(compiled, environment)
        assert isinstance(function, XdmFunction)
        return function

    functions = (capture(pause_first, 11), capture(pause_second, 22))
    assert cache.bucket_count == 1
    sheet = XsltProcessor().compile(
        _sheet("""
        <xsl:param name="deferred" as="function(*)"/>
        <xsl:template name="xsl:initial-template">
          <xsl:sequence select="$deferred()"/>
        </xsl:template>
        """)
    )

    def worker(index: int) -> None:
        if index == 1:
            barrier.wait()
        result = sheet.transform(delivery="raw", parameters={"deferred": functions[index]})
        assert result.principal == (XdmAtomic.integer((11, 22)[index]),)
        if index == 0:
            barrier.wait()

    _run_workers(barrier, worker)


def test_dynamic_expression_cache_reuses_plans_only_within_each_invocation(monkeypatch):
    barrier = Barrier(3, timeout=_TIMEOUT)
    original = DynamicExpressionCache.compile
    plans = {}

    def compile_after_overlap(cache, expression, identity):
        barrier.wait()
        plan = original(cache, expression, identity)
        plans.setdefault(cache, []).append(plan)
        return plan

    monkeypatch.setattr(DynamicExpressionCache, "compile", compile_after_overlap)
    compiled = XsltProcessor().compile(
        _sheet("""
        <xsl:param name="n" as="xs:integer"/>
        <xsl:template match="/">
          <xsl:for-each select="1 to 2">
            <xsl:evaluate xpath="'. + $n'" context-item="." with-params="map{QName('', 'n'): $n}"/>
          </xsl:for-each>
        </xsl:template>
        """)
    )
    source = XdmDocument.parse(b"<shared/>")

    def worker(index: int) -> None:
        result = compiled.transform(source, parameters={"n": index}, delivery="raw")
        assert result.principal == (XdmAtomic.integer(index + 1), XdmAtomic.integer(index + 2))

    _run_workers(barrier, worker)
    assert len(plans) == 3
    for first, second in plans.values():
        assert first is second
    assert len({id(pair[0]) for pair in plans.values()}) == 3


def test_stale_bound_source_is_rejected_but_captured_and_returned_xdm_stay_usable():
    schema = Schema.compile(
        io.StringIO("""
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="value" type="xs:string"/>
        </xs:schema>
        """)
    )
    bound = schema.parse(io.StringIO("<value>original</value>"), capture_xdm=True)
    captured = bound.to_xdm()
    compiled = XsltProcessor().compile(
        _sheet("""
        <xsl:template match="/"><xsl:sequence select="/*"/></xsl:template>
        <xsl:template match="value"><xsl:sequence select="string(.)"/></xsl:template>
        """)
    )
    (returned,) = compiled.transform(bound, delivery="raw").principal
    bound.root._value_ = ["edited"]
    barrier = Barrier(3, timeout=_TIMEOUT)

    def reject_stale() -> None:
        with pytest.raises(XdmInputError, match="changed since XDM capture"):
            compiled.transform(bound, delivery="raw")

    def transform_captured() -> None:
        (result,) = compiled.transform(captured, delivery="raw").principal
        assert result.string_value == "original"

    def transform_returned() -> None:
        assert compiled.transform(returned, delivery="raw").principal == (
            XdmAtomic.string("original"),
        )

    checks = (reject_stale, transform_captured, transform_returned)

    def worker(index: int) -> None:
        barrier.wait()
        checks[index]()

    _run_workers(barrier, worker)
