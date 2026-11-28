"""Canonical graph identity for a deeply authored ``choice`` pattern.

The compilation contract, its other authored depths, and the shared fixtures
live in ``test_relaxng_schema``; this module keeps the heaviest authored shape
in its own scheduling unit.
"""

from __future__ import annotations

import io

import pytest
from tests.relaxng_schema_support import NS, compile_schema, deref

from pyxsd.relaxng.patterns import (
    Choice,
    Element,
    Empty,
    Group,
    Interleave,
    Ref,
    Text,
    nullable,
)


@pytest.mark.parametrize("construct", ["choice"])
def test_deep_authored_patterns_compile_to_deterministic_canonical_graphs(construct):
    depth = 1100
    if construct == "element":
        body = '<element name="node">' * depth + "<empty/>" + "</element>" * depth
    elif construct == "mixed":
        body = (
            '<element name="node"><group><text/><choice><empty/>' * depth
            + "<text/>"
            + "</choice></group></element>" * depth
        )
    elif construct == "group":
        body = f"<{construct}><text/>" * depth + "<text/>" + f"</{construct}>" * depth
        body = f'<element name="root">{body}</element>'
    else:
        body = (
            "".join(
                f'<choice><element name="child{index}"><empty/></element>' for index in range(depth)
            )
            + "<text/>"
            + "</choice>" * depth
        )
        body = f'<element name="root">{body}</element>'
    xml = f'<grammar xmlns="{NS}"><start>{body}</start></grammar>'
    signatures = []
    for _ in range(2):
        schema = compile_schema(io.StringIO(xml))
        assert not schema.report.has_errors
        compiled = schema.grammar
        pending = [compiled.start, *(item.pattern for item in compiled.definitions.values())]
        seen = set()
        signature = []
        while pending:
            node = pending.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            assert compiled.graph.intern(node) is node
            signature.append((type(node).__name__, nullable(node)))
            if construct == "choice" and isinstance(node, Choice):
                assert nullable(node) is isinstance(node.second, Text)
            else:
                assert nullable(node) is isinstance(node, (Empty, Text, Group, Choice))
            if isinstance(node, Ref):
                assert compiled.graph.intern(node.target) is node.target
                pending.append(node.target)
            elif isinstance(node, (Choice, Group, Interleave)):
                pending.extend((node.second, node.first))
            elif isinstance(node, Element):
                pending.append(node.pattern)
        signatures.append(signature)
        assert not nullable(compiled.start)
        expected_definitions = {"element": depth, "mixed": depth, "group": 1, "choice": depth + 1}
        assert len(compiled.definitions) == expected_definitions[construct]
        if construct == "group":
            assert sum(kind == "Group" for kind, _ in signature) == depth
        elif construct == "choice":
            assert sum(kind == "Choice" for kind, _ in signature) == depth
            assert nullable(deref(compiled.start).pattern)
        elif construct == "element":
            assert len(compiled.graph) == depth * 2 + 1
        else:
            assert sum(kind == "Choice" for kind, _ in signature) == depth
    assert signatures[0] == signatures[1]
