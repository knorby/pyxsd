"""Bounded positional XML comparison, called only inside clean workers.

Occurrence paths are addresses, not business identities. A moved/reordered node
can appear as changed, added or removed; this comparison never invents a match.
"""

import hashlib
import json
from xml.etree.ElementTree import Element

from pydantic import JsonValue
from pyxsd.namespaces import NamespaceContext

from .captures import InputResourcePolicy
from .config import Limits
from .worker_protocol import StructuralDiffOperation
from .workers import capture_from_wire


def _index(root: Element) -> dict[tuple[tuple[str, int], ...], str]:
    result: dict[tuple[tuple[str, int], ...], str] = {}
    stack: list[tuple[Element, tuple[tuple[str, int], ...]]] = [(root, ((root.tag, 1),))]
    while stack:
        node, path = stack.pop()
        signature = json.dumps(
            [node.tag, sorted(node.attrib.items()), node.text, node.tail],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        result[path] = hashlib.sha256(signature).hexdigest()
        counts: dict[str, int] = {}
        for child in node:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            stack.append((child, (*path, (child.tag, counts[child.tag]))))
    return result


def compare(operation: StructuralDiffOperation, limits: Limits) -> dict[str, JsonValue]:
    policy = InputResourcePolicy(capture_from_wire(operation.capture), limits)
    indexes = []
    for reference in (operation.original, operation.candidate):
        data = policy.read(reference.path, base=None, purpose="instance")
        indexes.append(_index(policy.parse(data, NamespaceContext())))
    before, after = indexes
    changes: list[JsonValue] = []
    total = 0
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        total += 1
        if len(changes) < operation.limit:
            changes.append(
                {
                    "path": [{"name": name, "index": index} for name, index in path],
                    "kind": "added" if old is None else "removed" if new is None else "changed",
                    "before_digest": old,
                    "after_digest": new,
                }
            )
    return {
        "available": True,
        "identity": "positional-expanded-name",
        "changes": changes,
        "total": total,
        "truncated": total > len(changes),
    }
