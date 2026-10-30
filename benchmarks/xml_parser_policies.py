"""Benchmark default stdlib parsing against the protected defusedxml profile.

Run with::

    uv run python benchmarks/xml_parser_policies.py

The report gives parser-only and end-to-end parse/bind medians for the same
ordinary XML fixture. There is deliberately no pass/fail timing threshold:
the default profile exists to keep stdlib speed, while protected callers opt
in to defusedxml's declaration checks and accept its overhead. The script
does verify that each profile actually selects the parser it claims.
"""

from __future__ import annotations

import argparse
import io
import platform
import statistics
import sys
import time
from collections.abc import Callable
from typing import Any
from xml.etree import ElementTree as ET

import pyxsd
from pyxsd.namespaces import NamespaceContext, parse_with_namespaces
from pyxsd.xml_policy import XmlParserPolicy

SCHEMA = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="catalog">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="item" type="xs:string" maxOccurs="unbounded"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""


def fixture(items: int) -> bytes:
    body = "".join(f"<item>value {index}</item>" for index in range(items))
    return f"<catalog>{body}</catalog>".encode()


def median_seconds(operation: Callable[[], Any], repeats: int) -> float:
    operation()
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        operation()
        samples.append(time.perf_counter() - started)
    return statistics.median(samples)


def verify_backends() -> tuple[bool, bool]:
    """Return whether default used stdlib and protected used defusedxml."""
    from defusedxml import ElementTree as DefusedET

    original_stdlib = ET.iterparse
    original_defused = DefusedET.iterparse
    stdlib_calls: list[int] = []
    defused_calls: list[int] = []

    def record_stdlib(*args: Any, **kwargs: Any) -> Any:
        stdlib_calls.append(1)
        return original_stdlib(*args, **kwargs)

    def record_defused(*args: Any, **kwargs: Any) -> Any:
        defused_calls.append(1)
        return original_defused(*args, **kwargs)

    data = fixture(10)
    try:
        ET.iterparse = record_stdlib
        DefusedET.iterparse = record_defused
        parse_with_namespaces(io.BytesIO(data), NamespaceContext())
        default_is_stdlib = bool(stdlib_calls) and not defused_calls
        stdlib_calls.clear()
        parse_with_namespaces(
            io.BytesIO(data),
            NamespaceContext(),
            policy=XmlParserPolicy.protected(),
        )
        protected_is_defused = bool(defused_calls) and not stdlib_calls
    finally:
        ET.iterparse = original_stdlib
        DefusedET.iterparse = original_defused
    return default_is_stdlib, protected_is_defused


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=int, default=1000, help="catalog items in the fixture")
    parser.add_argument("--repeats", type=int, default=7, help="timed repetitions per operation")
    args = parser.parse_args(argv)

    data = fixture(args.items)
    protected = XmlParserPolicy.protected()
    schema = pyxsd.Schema.compile(io.StringIO(SCHEMA))

    rows = [
        (
            "parser-only (stdlib default)",
            median_seconds(
                lambda: parse_with_namespaces(io.BytesIO(data), NamespaceContext()),
                args.repeats,
            ),
        ),
        (
            "parser-only (protected)",
            median_seconds(
                lambda: parse_with_namespaces(
                    io.BytesIO(data), NamespaceContext(), policy=protected
                ),
                args.repeats,
            ),
        ),
        (
            "end-to-end parse/bind (default)",
            median_seconds(lambda: schema.parse(io.BytesIO(data)), args.repeats),
        ),
        (
            "end-to-end parse/bind (protected)",
            median_seconds(
                lambda: schema.parse(io.BytesIO(data), xml_policy=protected),
                args.repeats,
            ),
        ),
    ]
    default_is_stdlib, protected_is_defused = verify_backends()

    print(f"python {platform.python_version()} on {platform.platform()}")
    print(f"fixture: {len(data)} bytes, {args.items} items; median of {args.repeats} runs")
    print(f"{'operation':42} {'median ms':>10}")
    for name, seconds in rows:
        print(f"{name:42} {seconds * 1000:10.3f}")
    print(f"default backend stdlib: {default_is_stdlib}")
    print(f"protected backend defusedxml: {protected_is_defused}")
    if not (default_is_stdlib and protected_is_defused):
        print("backend verification failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
