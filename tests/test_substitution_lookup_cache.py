"""Unit tests for the per-compile substitution-group lookup cache.

The head/member factories in ``pyxsd.schema_checks`` are consulted from
every UPA and particle-derivation check; on large schemas the per-call
candidate scan they replace dominates compile time. These tests pin the
cache's lifetime (owned by the schema declaration, shared by every
factory call on that schema, isolated between compiles) and the
elemZ020 local-inherits-global lookup the cache must not change.
"""

import io

from pyxsd import Schema
from pyxsd.schema_checks import (
    _substitution_cache,
    substitution_head_lookup,
    substitution_member_lookup,
)

SUB_SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="head" type="xs:string"/>
  <xs:element name="member" type="xs:string" substitutionGroup="head"/>
  <xs:element name="blocked" type="xs:string" substitutionGroup="head"
              block="substitution"/>
</xs:schema>"""


def _compile(xsd: str):
    schema = Schema.compile(io.StringIO(xsd))
    host = schema._host
    return schema, host


def _element(host, name):
    return host.components.getFromName(name, kind="element")


def test_head_lookup_results_are_consistent_across_factory_calls():
    _, host = _compile(SUB_SCHEMA)
    member = _element(host, "member")
    head = _element(host, "head")
    blocked = _element(host, "blocked")
    from_member = substitution_head_lookup(member, host)
    from_head = substitution_head_lookup(head, host)
    assert from_member is not None and from_head is not None
    # Both factories resolve member and blocked to their head; the
    # block token is _head_chain's business (upa.py), not lookup's.
    assert from_member(member) is head
    assert from_head(member) is head
    assert from_member(blocked) is head


def test_lookup_cache_is_shared_per_schema():
    _, host = _compile(SUB_SCHEMA)
    member = _element(host, "member")
    head = _element(host, "head")
    cache = _substitution_cache(member.getSchema())
    # The same cache object serves every factory call on the schema,
    # and repeated resolution does not rebuild the candidate list.
    assert _substitution_cache(head.getSchema()) is cache
    before = cache["candidates"]
    substitution_head_lookup(head, host)
    assert cache["candidates"] is before


def test_local_declaration_inherits_global_membership():
    # elemZ020 shape: a local element whose expanded name matches a
    # global with a substitutionGroup resolves to that global's head.
    xsd = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="head" type="xs:string"/>
  <xs:element name="member" type="xs:string" substitutionGroup="head"/>
  <xs:complexType name="holder">
    <xs:sequence>
      <xs:element name="member" type="xs:string"/>
    </xs:sequence>
  </xs:complexType>
</xs:schema>"""
    _, host = _compile(xsd)
    holder = host.components.getFromName("holder", kind="type")

    def _descendants(node):
        for child in node.processedChildren:
            if child is None:
                continue
            yield child
            yield from _descendants(child)

    local_member = next(
        child
        for child in _descendants(holder)
        if type(child).__name__ == "Element" and getattr(child, "name", None) == "member"
    )
    head = _element(host, "head")
    lookup = substitution_head_lookup(local_member, host)
    assert lookup is not None
    assert lookup(local_member) is head


def test_caches_are_isolated_between_compiles():
    other = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="headB" type="xs:string"/>
  <xs:element name="memberB" type="xs:string" substitutionGroup="headB"/>
</xs:schema>"""
    _, host_a = _compile(SUB_SCHEMA)
    _, host_b = _compile(other)
    member_a = _element(host_a, "member")
    member_b = _element(host_b, "memberB")
    head_b = _element(host_b, "headB")
    cache_a = _substitution_cache(member_a.getSchema())
    cache_b = _substitution_cache(member_b.getSchema())
    assert cache_a is not cache_b
    lookup_b = substitution_head_lookup(member_b, host_b)
    assert lookup_b is not None
    assert lookup_b(member_b) is head_b
    assert all(candidate is not member_a for candidate in cache_b["candidates"])


def test_member_lookup_memoizes_member_lists():
    _, host = _compile(SUB_SCHEMA)
    head = _element(host, "head")
    member = _element(host, "member")
    members = substitution_member_lookup(member, host)
    assert members is not None
    first = members(head)
    assert member in first
    cache = _substitution_cache(head.getSchema())
    assert cache["members"][id(head)] is first
    assert members(head) is first


def test_lookup_does_not_cache_wrapper_objects():
    # The group-redefine check feeds fresh _GroupRedefineDeclaration
    # wrappers through head_lookup; a wrapper is short-lived, so its id
    # can be recycled by the next wrapper (the demonstrated wrong-head
    # bug). Resolution must still work through the wrapper's delegation,
    # but the result must never be memoized under the wrapper's id.
    from pyxsd.schema_checks import _GroupRedefineDeclaration

    _, host = _compile(SUB_SCHEMA)
    member = _element(host, "member")
    head = _element(host, "head")
    lookup = substitution_head_lookup(member, host)
    assert lookup is not None
    wrapped = _GroupRedefineDeclaration(member)
    assert lookup(wrapped) is head
    cache = _substitution_cache(member.getSchema())
    assert id(wrapped) not in cache["head"]


def test_member_lookup_does_not_cache_wrapper_objects():
    from pyxsd.schema_checks import _GroupRedefineDeclaration

    _, host = _compile(SUB_SCHEMA)
    head = _element(host, "head")
    member = _element(host, "member")
    members = substitution_member_lookup(member, host)
    assert members is not None
    wrapped = _GroupRedefineDeclaration(head)
    # Identity comparisons make a wrapped head match no candidates (a
    # pre-existing quirk the original per-call scan shared); the pinned
    # behavior is that the wrapper resolves without crashing and its
    # result is never memoized under a recyclable id().
    assert members(wrapped) == []
    cache = _substitution_cache(head.getSchema())
    assert id(wrapped) not in cache["members"]
