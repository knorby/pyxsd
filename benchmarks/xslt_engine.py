"""Report deterministic XSLT operation and allocation counters.

Run with either selector, or both together::

    uv run python benchmarks/xslt_engine.py --operation-counts
    uv run python benchmarks/xslt_engine.py --allocations
    uv run python benchmarks/xslt_engine.py --operation-counts --allocations

With no selector, both sections are reported.  The script prints one sorted
JSON document and compares no elapsed time against a threshold.

Operation counts come from the invocation ledger pyxsd retains on each
:class:`~pyxsd.xslt.TransformationResult`; every counter the
:class:`~pyxsd.xslt.runtime.frames.ExecutionBudget` exposes is reported under
``counters``, and the compiled stylesheet's mode and template-rule counts are
reported under ``compile``.  Explicit output and resource limits make the
otherwise lazy accounting counters observable.

Allocation figures come from :mod:`tracemalloc` and measure one warm
invocation per stylesheet, excluding compilation.  They are informational:
CPython's allocator and pyxsd's invocation-local caches can leave different
live-block totals between two runs in one interpreter, so the benchmark does
not claim a stable allocation count.

The output keys are:

``python``
    Interpreter version.
``pyxsd``
    Installed pyxsd version.
``operation_counts``
    Present for ``--operation-counts``.  ``limits`` names the accounting
    limits, and ``operations`` maps each stylesheet name to its ``counters``,
    ``compile`` counts, and serialized byte length.
``allocations``
    Present for ``--allocations``.  ``operations`` maps each stylesheet name
    to ``blocks``, ``bytes``, and ``peak_bytes`` from ``tracemalloc``.
"""

from __future__ import annotations

import argparse
import io
import json
import platform
import tracemalloc
from collections.abc import Mapping
from typing import Any

from pyxsd import __version__
from pyxsd.xslt import ExecutionLimits, XsltProcessor
from pyxsd.xslt.processor import CompiledStylesheet
from pyxsd.xslt.results import TransformationResult

#: Every counter :class:`~pyxsd.xslt.runtime.frames.ExecutionBudget` exposes.
COUNTER_FIELDS = (
    "annotations",
    "documents",
    "frames",
    "max_frame_depth",
    "output_bytes",
    "output_nodes",
    "resource_bytes",
    "steps",
)

#: Accounting limits large enough to observe lazy counters on small inputs.
LIMITS = ExecutionLimits(
    max_annotations=100_000,
    max_output_bytes=100_000,
    max_output_nodes=100_000,
    max_resource_bytes=100_000,
)

ATTRIBUTES_STYLESHEET = """\
<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0">
  <xsl:output method="xml" omit-xml-declaration="yes"/>
  <xsl:template match="/catalog">
    <result count="{count(item)}">
      <xsl:for-each select="item[@keep = 'yes']">
        <entry><xsl:value-of select="@id"/></entry>
      </xsl:for-each>
    </result>
  </xsl:template>
</xsl:stylesheet>
"""

GROUPING_STYLESHEET = """\
<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0">
  <xsl:output method="xml" omit-xml-declaration="yes"/>
  <xsl:template match="/catalog">
    <grouped>
      <xsl:for-each-group select="item" group-by="@keep">
        <group key="{current-grouping-key()}" count="{count(current-group())}">
          <xsl:apply-templates select="current-group()" mode="label"/>
        </group>
      </xsl:for-each-group>
    </grouped>
  </xsl:template>
  <xsl:template match="item" mode="label">
    <label><xsl:value-of select="@id"/></label>
  </xsl:template>
</xsl:stylesheet>
"""

CATALOG = (
    '<catalog><item id="a" keep="yes"/><item id="b" keep="no"/><item id="c" keep="yes"/></catalog>'
)

#: Embedded stylesheets and the exact serialized result for the shared catalog.
OPERATIONS: Mapping[str, tuple[str, str]] = {
    "attributes": (
        ATTRIBUTES_STYLESHEET,
        '<result count="3"><entry>a</entry><entry>c</entry></result>',
    ),
    "grouping": (
        GROUPING_STYLESHEET,
        '<grouped><group key="yes" count="2"><label>a</label><label>c</label></group>'
        '<group key="no" count="1"><label>b</label></group></grouped>',
    ),
}


def _compile(name: str) -> CompiledStylesheet:
    """Compile one embedded benchmark stylesheet."""
    return XsltProcessor().compile(io.StringIO(OPERATIONS[name][0]))


def _invoke(compiled: CompiledStylesheet, name: str) -> tuple[TransformationResult, str]:
    """Run one invocation and return its result with the checked serialization."""
    result = compiled.transform(source=io.StringIO(CATALOG), delivery="stylesheet", limits=LIMITS)
    serialized = result.serialize()
    expected = OPERATIONS[name][1]
    if serialized != expected:
        raise RuntimeError(
            f"benchmark operation {name!r} produced {serialized!r}, not {expected!r}"
        )
    return result, serialized


def _compile_counts(compiled: CompiledStylesheet) -> dict[str, int]:
    """Report the compiled artifact's mode and template-rule counts."""
    return {
        "modes": len(compiled.mode_names),
        "template_rules": sum(compiled.mode_rule_count(mode) for mode in compiled.mode_names),
    }


def _budget_counters(result: TransformationResult) -> dict[str, int]:
    """Read every invocation-ledger counter the result retains."""
    budget = result._budget
    assert budget is not None, "a transformation result retains its invocation ledger"
    return {field: int(getattr(budget, field)) for field in COUNTER_FIELDS}


def _limit_values() -> dict[str, Any]:
    """Report the accounting limits used by every operation."""
    return {
        "max_annotations": LIMITS.max_annotations,
        "max_documents": LIMITS.max_documents,
        "max_frames": LIMITS.max_frames,
        "max_output_bytes": LIMITS.max_output_bytes,
        "max_output_nodes": LIMITS.max_output_nodes,
        "max_resource_bytes": LIMITS.max_resource_bytes,
        "max_steps": LIMITS.max_steps,
    }


def operation_counts() -> dict[str, Any]:
    """Measure every operation and report its deterministic counters."""
    operations = {}
    for name in OPERATIONS:
        compiled = _compile(name)
        result, serialized = _invoke(compiled, name)
        operations[name] = {
            "compile": _compile_counts(compiled),
            "counters": _budget_counters(result),
            "serialized_bytes": len(serialized.encode("utf-8")),
        }
    return {"limits": _limit_values(), "operations": operations}


def allocation_counts() -> dict[str, Any]:
    """Report tracemalloc figures for one warm invocation per stylesheet."""
    operations = {}
    for name in OPERATIONS:
        compiled = _compile(name)
        _invoke(compiled, name)
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            result, _ = _invoke(compiled, name)
            snapshot = tracemalloc.take_snapshot()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        statistics = snapshot.statistics("filename")
        operations[name] = {
            "blocks": sum(statistic.count for statistic in statistics),
            "bytes": sum(statistic.size for statistic in statistics),
            "peak_bytes": int(peak),
        }
        del result
    return {"operations": operations}


def main(argv: list[str] | None = None) -> int:
    """Print the selected benchmark sections as sorted JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--operation-counts",
        action="store_true",
        help="report ExecutionBudget and compile counts",
    )
    parser.add_argument(
        "--allocations",
        action="store_true",
        help="report tracemalloc allocation figures",
    )
    arguments = parser.parse_args(argv)
    report_counts = arguments.operation_counts
    report_allocations = arguments.allocations
    if not report_counts and not report_allocations:
        report_counts = report_allocations = True
    report = {"python": platform.python_version(), "pyxsd": __version__}
    if report_counts:
        report["operation_counts"] = operation_counts()
    if report_allocations:
        report["allocations"] = allocation_counts()
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
