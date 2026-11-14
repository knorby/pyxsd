"""Per-profile execution of the pinned W3C XSLT test catalog.

Each profile is a distinct processor identity plus the declared catalog
capabilities it implements.  The 3.0 profile runs the default processor; the
native 2.0 and 1.0 profiles construct ``XsltProcessor(language=..., xpath=...)``
themselves, so their verdicts never describe a ``version`` attribute on a 3.0
processor.

The executor covers the invocation shapes the catalog declares in
``<environment>`` and ``<test>`` (context items, sources, parameters, entry
points, and delivery).  A shape the harness cannot drive is reported as
``adapter-gap``; an engine exception is a ``fail`` with its message, never a
silent pass.  Every executed case is bounded by a per-case budget, so one
pathological stylesheet cannot stall a profile run.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from tests.xslt.assertions import ResultSnapshot
from tests.xslt.model import Param, Source, TestCase
from tests.xslt.outcomes import CaseResult
from tests.xslt.runner import CaseExecutionError, run_cases
from tests.xslt.selection import XSLT30_NON_STREAMING, FeatureProfile

from pyxsd.xdm import XdmArray, XdmAtomic, XdmFunction, XdmMap
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xpath.engine import XPathEngine
from pyxsd.xpath.environment import ExpressionEnvironment, Focus, XPathStaticContext
from pyxsd.xslt import TransformationResult, XsltProcessor
from pyxsd.xslt.errors import XsltError

#: Capabilities a native XSLT 2.0 processor implements in addition to its
#: language versions.  Streaming, higher-order functions, dynamic evaluation,
#: packages, maps, arrays, XML 1.1, and embedded stylesheet PIs are XSLT 3.0
#: features and stay outside this profile.
_XSLT20_FEATURES = frozenset(
    {
        "schema_aware",
        "serialization",
        "backwards_compatibility",
        "namespace_axis",
        "dtd",
        "disabling_output_escaping",
        "built_in_derived_types",
        "HTML4",
    }
)

#: Capabilities a native XSLT 1.0 processor implements.  Everything that
#: arrived with 2.0 or 3.0 is deliberately outside the profile.
_XSLT10_FEATURES = frozenset(
    {
        "namespace_axis",
        "dtd",
        "disabling_output_escaping",
        "HTML4",
    }
)

#: 3.0-only capabilities that both native profiles deliberately exclude.
_PRE_30_DECLINED = frozenset(
    {
        "streaming",
        "streaming-fallback",
        "higher_order_functions",
        "dynamic_evaluation",
        "packages",
        "maps",
        "arrays",
        "serialization",
        "schema_aware",
        "backwards_compatibility",
        "built_in_derived_types",
        "HTML5",
        "XML_1.1",
        "xsl-stylesheet-processing-instruction",
    }
)

#: A code-less engine exception is reported under this sentinel so an
#: expected error never masquerades as a matching code.
UNCODED_ERROR = "pyxsd-error"

#: Headroom over a case budget before the parent treats a worker as wedged.
#: The in-worker alarm normally fires first; the parent deadline catches a
#: C-level hang that Python signals cannot interrupt.
_PARENT_GRACE = 5.0


class UnsupportedCase(Exception):
    """The harness cannot drive one catalog case's declared shape."""


@dataclass(frozen=True)
class SuiteProfile:
    """One conformance profile: catalog capabilities and processor identity."""

    name: str
    feature_profile: FeatureProfile
    processor: XsltProcessor

    def __post_init__(self) -> None:
        if self.feature_profile.name != self.name:
            raise ValueError("the feature profile name must match the suite profile")
        if self.processor.language != self.feature_profile.language:
            raise ValueError("the processor language must match the feature profile")
        if self.processor.xpath != self.feature_profile.xpath_version:
            raise ValueError("the processor XPath version must match the feature profile")


def _profile(
    name: str,
    language: str,
    xpath: str,
    xsd_version: str,
    features: frozenset[str],
    declined: frozenset[str],
    description: str,
) -> SuiteProfile:
    return SuiteProfile(
        name=name,
        feature_profile=FeatureProfile(
            name=name,
            language=language,
            xpath_version=xpath,
            xsd_version=xsd_version,
            features=features,
            declined_features=declined - features,
            description=description,
        ),
        processor=XsltProcessor(
            language=language,
            xpath=xpath,
            xsd_version=xsd_version,
        ),
    )


#: The three conformance profiles, keyed by their CI job and baseline name.
SUITE_PROFILES: Mapping[str, SuiteProfile] = {
    "xslt30": SuiteProfile(
        name="xslt30",
        feature_profile=XSLT30_NON_STREAMING,
        processor=XsltProcessor(language="3.0", xpath="3.1"),
    ),
    "xslt20": _profile(
        name="xslt20",
        language="2.0",
        xpath="2.0",
        xsd_version="1.0",
        features=_XSLT20_FEATURES,
        declined=_PRE_30_DECLINED,
        description="native schema-aware XSLT 2.0 with XPath 2.0 and XSD 1.0",
    ),
    "xslt10": _profile(
        name="xslt10",
        language="1.0",
        xpath="1.0",
        xsd_version="1.0",
        features=_XSLT10_FEATURES,
        declined=_PRE_30_DECLINED | _XSLT20_FEATURES,
        description="native XSLT 1.0 with XPath 1.0 result-tree fragments",
    ),
}


# ---------------------------------------------------------------------------
# Result conversion


def _document_xml(node: XdmNode) -> str:
    """One XDM tree as a deterministic XML string for the assertion layer."""

    def build(source: XdmNode) -> Any:
        element = ET.Element(source.name or source.local_name or "node")
        for attribute in source.attributes:
            element.set(attribute.name, attribute.string_value)
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


def _snapshot_entry(entry: Any) -> ResultSnapshot:
    method = entry.serialization.get("method")
    encoding = entry.serialization.get("encoding")
    if entry.tree is not None:
        return ResultSnapshot(
            xml=_document_xml(entry.tree),
            serialized=entry.serialized,
            serialization_method=method,
            serialization_encoding=encoding,
            messages=(),
            warnings=(),
        )
    return ResultSnapshot(
        raw=tuple(_value(item) for item in entry.raw),
        serialized=entry.serialized,
        serialization_method=method,
        serialization_encoding=encoding,
        messages=(),
        warnings=(),
    )


def _snapshot(result: TransformationResult) -> ResultSnapshot:
    entry = _snapshot_entry(result.principal_entry)
    messages = tuple(
        ResultSnapshot(raw=(message.text,), xml=message.text) for message in result.messages
    )
    return ResultSnapshot(
        raw=entry.raw,
        xml=entry.xml,
        serialized=entry.serialized,
        messages=messages,
        secondary={uri: _snapshot_entry(value) for uri, value in result.secondary.items()},
        warnings=(),
        serialization_method=entry.serialization_method,
        serialization_encoding=entry.serialization_encoding,
    )


# ---------------------------------------------------------------------------
# Invocation inputs


def _evaluate(
    select: str,
    xpath_version: str,
    *,
    focus: XdmDocument | XdmNode | None = None,
) -> tuple[Any, ...]:
    engine = XPathEngine(xpath_version)
    compiled = engine.compile(select, XPathStaticContext())
    resolved = None if focus is None else Focus(item=focus)
    return tuple(engine.evaluate(compiled, ExpressionEnvironment(), focus=resolved))


def _parameter_values(
    parameters: Iterable[Param],
    xpath_version: str,
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for parameter in parameters:
        if parameter.name is None or parameter.select is None:
            continue
        values[parameter.name] = _evaluate(parameter.select, xpath_version)
    return values


def _context_source(case: TestCase) -> Source | None:
    sources = [source for source in case.environment.sources if source.role == "."]
    if not sources:
        return None
    if len(sources) > 1:
        raise UnsupportedCase("multiple context-item sources")
    if any(source.validation == "strict" for source in case.environment.sources):
        raise UnsupportedCase("schema-validated source")
    for schema in case.environment.schemas:
        if schema.role in ("schema", "source-reference"):
            raise UnsupportedCase("source schema")
    return sources[0]


def _parse_source(case: TestCase, source: Source) -> XdmDocument:
    if case.document_root is None:
        raise UnsupportedCase("case without a corpus root")
    if source.content is not None:
        return XdmDocument.parse(source.content.encode("utf-8"))
    if source.path is not None:
        return XdmDocument.parse(case.document_root / Path(str(source.path)))
    raise UnsupportedCase("context source without a file or inline content")


def _source_input(
    case: TestCase,
    profile: SuiteProfile,
) -> tuple[Any, ...] | XdmDocument | XdmNode | None:
    """The initial context item or match selection the catalog declares."""
    xpath_version = profile.processor.xpath
    if case.environment.context_item is not None:
        select = case.environment.context_item.select
        return () if select is None else _evaluate(select, xpath_version)
    source = _context_source(case)
    mode = case.invocation.initial_mode
    if source is None:
        if mode is not None and mode.select:
            return _evaluate(mode.select, xpath_version)
        return None
    document = _parse_source(case, source)
    if source.select:
        return _evaluate(source.select, xpath_version, focus=document)
    if mode is not None and mode.select:
        return _evaluate(mode.select, xpath_version, focus=document)
    return document


def _delivery(case: TestCase) -> str:
    output = case.invocation.output or case.environment.output
    if output is not None and output.tree is False:
        return "raw"
    if output is not None and output.tree is True:
        return "document"
    return "stylesheet"


def _stylesheet(case: TestCase) -> Path:
    if case.packages:
        raise UnsupportedCase("packages")
    stylesheets = [sheet for sheet in case.stylesheets if sheet.role != "secondary"]
    if len(stylesheets) != 1:
        raise UnsupportedCase(f"{len(stylesheets)} principal stylesheets")
    if stylesheets[0].path is None:
        raise UnsupportedCase("principal stylesheet without a file")
    if case.document_root is None:
        raise UnsupportedCase("case without a corpus root")
    return case.document_root / Path(str(stylesheets[0].path))


def execute_case(case: TestCase, profile: SuiteProfile) -> ResultSnapshot:
    """Compile and run one case, returning its result for independent assertion."""
    if case.environment.resources:
        raise UnsupportedCase("resources")
    if case.environment.collections:
        raise UnsupportedCase("collections")
    output = case.invocation.output or case.environment.output
    if output is not None and output.result_var is not None:
        raise UnsupportedCase("result-variable output")
    stylesheet = _stylesheet(case)
    invocation = case.invocation
    xpath_version = profile.processor.xpath
    declared_parameters = (*case.environment.params, *invocation.params)
    static_parameters = _parameter_values(
        (parameter for parameter in declared_parameters if parameter.static),
        xpath_version,
    )
    parameters = _parameter_values(
        (parameter for parameter in declared_parameters if not parameter.static),
        xpath_version,
    )
    template_parameters: dict[str, Any] = {}
    tunnel_parameters: dict[str, Any] = {}
    if invocation.initial_template is not None:
        for parameter in invocation.initial_template.params:
            if parameter.name is None or parameter.select is None:
                continue
            value = _evaluate(parameter.select, xpath_version)
            target = tunnel_parameters if parameter.tunnel else template_parameters
            target[parameter.name] = value
    elif invocation.initial_mode is not None:
        for parameter in invocation.initial_mode.params:
            if parameter.name is None or parameter.select is None:
                continue
            value = _evaluate(parameter.select, xpath_version)
            target = tunnel_parameters if parameter.tunnel else template_parameters
            target[parameter.name] = value
    arguments: tuple[Any, ...] = ()
    if invocation.initial_function is not None:
        arguments = tuple(
            value
            for parameter in invocation.initial_function.params
            if parameter.select is not None
            for value in (_evaluate(parameter.select, xpath_version),)
        )
    options: dict[str, Any] = {
        "parameters": parameters,
        "template_parameters": template_parameters,
        "tunnel_parameters": tunnel_parameters,
        "initial_template": (
            invocation.initial_template.name if invocation.initial_template is not None else None
        ),
        "initial_mode": invocation.initial_mode.name
        if invocation.initial_mode is not None
        else None,
        "initial_function": (
            invocation.initial_function.name if invocation.initial_function is not None else None
        ),
        "initial_function_arguments": arguments,
        "delivery": _delivery(case),
    }
    try:
        source = _source_input(case, profile)
        if source is not None:
            options["source"] = source
        compiled = profile.processor.compile(stylesheet, static_parameters=static_parameters)
        return _snapshot(compiled.transform(**options))
    except XsltError as exc:
        raise CaseExecutionError(exc.code or UNCODED_ERROR, str(exc)) from exc


# ---------------------------------------------------------------------------
# Bounded execution


def _alarm_supported() -> bool:
    return hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer")


@contextmanager
def case_budget(seconds: float | None):
    """Bound one case, or run unbounded when no budget is configured."""
    if seconds is None or not _alarm_supported():
        yield
        return

    def expire(signum: int, frame: Any) -> None:
        raise TimeoutError(f"the case exceeded its {seconds:g} s budget")

    try:
        previous = signal.signal(signal.SIGALRM, expire)
    except ValueError:  # not the main thread: no process-wide timer is available
        yield
        return
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def run_case(
    case: TestCase,
    profile: SuiteProfile,
    *,
    timeout: float | None = None,
) -> CaseResult:
    """Run one case, classifying harness gaps and engine crashes honestly."""
    try:
        with case_budget(timeout):
            return run_cases(
                [case],
                lambda item: execute_case(item, profile),
                profile.feature_profile,
            )[0]
    except UnsupportedCase as exc:
        return CaseResult.adapter_gap(case.name, detail=str(exc))
    except TimeoutError as exc:
        return CaseResult.timeout(case.name, detail=str(exc))
    except Exception as exc:  # the engine raising is a verdict, not a harness abort
        return CaseResult.failed(case.name, message=f"{type(exc).__name__}: {exc}")


def run_suite(
    cases: Iterable[TestCase],
    profile: SuiteProfile,
    *,
    timeout: float | None = None,
) -> tuple[CaseResult, ...]:
    """Run every case once, in catalog order."""
    return tuple(run_case(case, profile, timeout=timeout) for case in cases)


def _run_payload(payload: tuple[str, TestCase, float | None]) -> tuple[str, CaseResult]:
    """Worker entry point: a profile name resolves to an immutable identity here."""
    profile_name, case, timeout = payload
    return case.name, run_case(case, SUITE_PROFILES[profile_name], timeout=timeout)


def default_jobs() -> int:
    """The worker count for a full corpus run, overridable for CI accounting."""
    configured = os.environ.get("PYXSD_XSLT_JOBS")
    if configured is None:
        return os.cpu_count() or 1
    return max(1, int(configured))


def run_parallel(
    cases: Iterable[TestCase],
    profile: SuiteProfile,
    *,
    timeout: float | None = None,
    jobs: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[CaseResult, ...]:
    """Run every case across worker processes, preserving catalog order.

    Workers receive the profile name rather than the profile object: the
    immutable processor configuration is not picklable, and resolving the
    checked-in profile by name is the same identity every worker executes.
    """
    cases = tuple(cases)
    workers = default_jobs() if jobs is None else max(1, jobs)
    if workers == 1 or len(cases) <= 1:
        return run_suite(cases, profile, timeout=timeout)
    method = "fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn"
    context = multiprocessing.get_context(method)
    budget = None if timeout is None else timeout + _PARENT_GRACE
    results: dict[str, CaseResult] = {}
    pending = list(cases)
    pool = context.Pool(processes=workers)
    try:
        while pending:
            batch = pending[:workers]
            pending = pending[workers:]
            submitted = [
                (
                    case,
                    pool.apply_async(_run_payload, ((profile.name, case, timeout),)),
                )
                for case in batch
            ]
            for case, handle in submitted:
                try:
                    name, result = handle.get(timeout=budget)
                except multiprocessing.TimeoutError:
                    results[case.name] = CaseResult.timeout(
                        case.name,
                        detail=f"the case exceeded its {timeout:g} s budget",
                    )
                    # A C-level hang ignores the in-worker alarm.  The other
                    # workers in this batch were terminated with the pool, so
                    # every uncollected case runs again in a fresh pool.
                    pending[0:0] = [
                        other
                        for other, other_result in submitted
                        if other.name != case.name and other.name not in results
                    ]
                    pool.terminate()
                    pool.join()
                    pool = context.Pool(processes=workers)
                    break
                else:
                    results[name] = result
                    if on_progress is not None:
                        on_progress(len(results), len(cases))
    finally:
        pool.close()
        pool.join()
    return tuple(results[case.name] for case in cases)
