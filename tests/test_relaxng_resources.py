"""Authorized RELAX NG resource loading and canonical dependency closure.

The loader is the only path by which a RELAX NG schema reads another
document.  It resolves every reference against its containing resource (not
the process working directory), deduplicates canonical resources, and keeps
complete evidence for cache identity.  These tests pin policy forwarding,
resolution semantics, origin recording, and dependency evidence.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import FrozenInstanceError
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from pyxsd.exceptions import ValidationError
from pyxsd.locations import IssueLocation, PathStep
from pyxsd.namespaces import parse_with_namespaces
from pyxsd.relaxng.origins import SourceOrigin
from pyxsd.relaxng.resources import (
    LoadedCompactResource,
    LoadedResource,
    ResourceDependency,
    ResourceLoader,
    ResourceReference,
    resolve_xml_base,
)
from pyxsd.relaxng.schema import RelaxNGSchema
from pyxsd.resource_policy import ResourceData, ResourcePolicyError
from pyxsd.xml_policy import XmlLimits, XmlParserPolicy

FIXTURES = Path(__file__).parent / "relaxng" / "fixtures"
RNG_NS = "http://relaxng.org/ns/structure/1.0"
RNG_START = f'<grammar xmlns="{RNG_NS}"/>'


class RecordingPolicy:
    """A filesystem policy that records every read and parse."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.reads: list[tuple[str, str | None, str]] = []
        self.parsed: list[str] = []
        self.denied: set[str] = set()
        self.aliases: dict[str, str] = {}

    def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
        self.reads.append((source, base, purpose))
        path = Path(source)
        if not path.is_absolute():
            path = (Path(base).parent if base is not None else self.root) / path
        path = path.resolve()
        if path.name in self.denied or not path.is_relative_to(self.root):
            raise ResourcePolicyError("resource denied")
        return ResourceData(self.aliases.get(str(path), str(path)), path.read_bytes())

    def parse(self, data: ResourceData, context: object) -> ET.Element:
        self.parsed.append(data.source_id)
        return parse_with_namespaces(io.BytesIO(data.content), context)  # type: ignore[arg-type]


class MemoryPolicy:
    """An in-memory policy whose source names are canonical URIs."""

    def __init__(
        self,
        contents: dict[str, bytes],
        *,
        aliases: dict[str, str] | None = None,
    ) -> None:
        self.contents = dict(contents)
        self.aliases = dict(aliases or {})
        self.reads: list[tuple[str, str | None, str]] = []

    def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
        self.reads.append((source, base, purpose))
        try:
            content = self.contents[source]
        except KeyError:
            raise ResourcePolicyError(f"resource denied: {source}") from None
        return ResourceData(self.aliases.get(source, source), content)

    def parse(self, data: ResourceData, context: object) -> ET.Element:
        return parse_with_namespaces(io.BytesIO(data.content), context)  # type: ignore[arg-type]


class TestLoad:
    def test_load_path_records_canonical_id_namespaces_and_origins(self):
        path = (FIXTURES / "grammar.rng").resolve()
        loaded = ResourceLoader().load(path)

        assert isinstance(loaded, LoadedResource)
        assert loaded.syntax == "xml"
        assert repr(loaded) == f"LoadedResource(canonical_id={str(path)!r})"
        assert loaded.canonical_id == str(path)
        assert loaded.root.tag == f"{{{RNG_NS}}}grammar"
        assert loaded.namespaces.bindings_for(loaded.root)[""] == RNG_NS
        root_origin = SourceOrigin(
            str(path), IssueLocation(str(path), (PathStep(loaded.root.tag, 1),))
        )
        assert root_origin in loaded.origins
        assert len(loaded.origins) == len(list(loaded.root.iter()))

    def test_load_named_and_unnamed_streams_have_distinct_identities(self):
        loader = ResourceLoader()
        first = loader.load(io.StringIO(RNG_START))
        second = loader.load(io.StringIO(RNG_START))
        named = io.StringIO(RNG_START)
        named.name = "named.rng"
        empty_named = io.StringIO(RNG_START)
        empty_named.name = ""

        assert first.canonical_id == "<memory>"
        assert second.canonical_id != first.canonical_id
        assert loader.load(named).canonical_id == "named.rng"
        assert loader.load(empty_named).canonical_id.startswith("<memory")

    def test_remote_reference_requires_an_authorized_policy(self):
        with pytest.raises(ResourcePolicyError, match="remote"):
            ResourceLoader().load("https://example.com/schema.rng")

    def test_remote_policy_reads_and_parses_with_schema_purpose(self):
        url = "https://example.com/schema.rng"
        policy = MemoryPolicy({url: RNG_START.encode()})
        loaded = ResourceLoader(policy=policy).load(url)

        assert loaded.canonical_id == url
        assert policy.reads == [(url, None, "schema")]

    def test_purpose_is_validated_and_forwarded_to_the_policy(self):
        url = "https://example.com/schema.rng"
        policy = MemoryPolicy({url: RNG_START.encode()})
        loader = ResourceLoader(policy=policy)

        loader.load(url, purpose="schema-hint")

        assert policy.reads[-1] == (url, None, "schema-hint")
        with pytest.raises(ValueError, match="unknown resource purpose"):
            loader.load(url, purpose="bogus")  # type: ignore[arg-type]

    def test_policy_denial_is_not_treated_as_a_missing_resource(self, tmp_path):
        (tmp_path / "schema.rng").write_text(RNG_START, encoding="utf-8")
        policy = RecordingPolicy(tmp_path)
        policy.denied.add("schema.rng")

        with pytest.raises(ResourcePolicyError, match="denied"):
            ResourceLoader(policy=policy).load("schema.rng")

    def test_missing_file_is_an_os_error(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ResourceLoader().load(tmp_path / "missing.rng")

    def test_malformed_xml_propagates_the_parse_error(self):
        with pytest.raises(ET.ParseError):
            ResourceLoader().load(io.StringIO("<grammar"))

    def test_binary_stream_is_read_without_reencoding(self):
        loaded = ResourceLoader().load(io.BytesIO(RNG_START.encode("utf-8")))
        assert loaded.root.tag == f"{{{RNG_NS}}}grammar"

    def test_unnamed_streams_with_a_policy_are_still_distinct(self):
        loader = ResourceLoader(policy=MemoryPolicy({}))

        first = loader.load(io.StringIO(RNG_START))
        second = loader.load(io.StringIO(RNG_START))

        assert first.canonical_id == "<memory>"
        assert second.canonical_id == "<memory#2>"
        assert len(loader.dependencies()) == 2

    def test_verified_element_cannot_bypass_policy_parsing(self):
        with pytest.raises(ResourcePolicyError, match="Element"):
            ResourceLoader().load(ET.Element(f"{{{RNG_NS}}}grammar"))

    def test_parser_limits_apply_without_a_resource_policy(self):
        loader = ResourceLoader(xml_limits=XmlLimits(max_elements=1))
        with pytest.raises(ResourcePolicyError, match="max_elements"):
            loader.load(FIXTURES / "grammar.rng")

    def test_parser_policy_applies_without_a_resource_policy(self):
        source = io.StringIO(f'<!DOCTYPE grammar [<!ENTITY x "x">]>{RNG_START}')
        with pytest.raises(ResourcePolicyError, match="DOCTYPE"):
            ResourceLoader(xml_policy=XmlParserPolicy.strict()).load(source)

    def test_canonical_dedup_returns_the_same_loaded_resource(self, tmp_path):
        (tmp_path / "schema.rng").write_text(RNG_START, encoding="utf-8")
        loader = ResourceLoader()

        first = loader.load(tmp_path / "schema.rng")
        second = loader.load(tmp_path / "." / "schema.rng")

        assert second is first
        assert len(loader.dependencies()) == 1

    def test_two_aliases_dedupe_to_one_canonical_resource(self):
        first_alias = "first.rng"
        second_alias = "second.rng"
        final = "https://example.com/final.rng"
        policy = MemoryPolicy(
            {
                first_alias: RNG_START.encode(),
                second_alias: RNG_START.encode(),
                final: RNG_START.encode(),
            },
            aliases={first_alias: final, second_alias: final},
        )
        loader = ResourceLoader(policy=policy)

        first = loader.load(first_alias)
        second = loader.load(second_alias)

        assert second is first
        assert loader.dependencies()[0].canonical_id == final
        assert policy.reads == [
            (first_alias, None, "schema"),
            (second_alias, None, "schema"),
        ]

    def test_policy_alias_dedupes_under_its_canonical_name(self):
        alias = "alias.rng"
        final = "https://example.com/final.rng"
        policy = MemoryPolicy(
            {alias: RNG_START.encode(), final: RNG_START.encode()},
            aliases={alias: final},
        )
        loader = ResourceLoader(policy=policy)

        first = loader.load(alias)
        again = loader.load(alias)
        by_final = loader.load(final)

        assert first.canonical_id == final
        assert again is first
        assert by_final is first
        assert policy.reads == [(alias, None, "schema")]

    def test_loaded_namespace_context_preserves_undeclaration(self):
        loaded = ResourceLoader().load(FIXTURES / "namespaces.rng")
        elements = list(loaded.root.iter())
        value_element = next(element for element in elements if element.tag == f"{{{RNG_NS}}}value")
        defaulted = next(element for element in elements if element.get("name") == "child")

        assert loaded.namespaces.bindings_for(defaulted)[""] == "urn:example:default"
        assert loaded.namespaces.bindings_for(value_element)[""] == ""


class TestCompactLoad:
    def test_compact_load_retains_admitted_bytes_and_dependency_hash(self, tmp_path):
        content = b"element root { text }\n"
        path = tmp_path / "schema.rnc"
        path.write_bytes(content)
        resolved = str(path.resolve())
        policy = RecordingPolicy(tmp_path)
        loader = ResourceLoader(policy=policy)

        loaded = loader.load(path, syntax="compact")

        assert isinstance(loaded, LoadedCompactResource)
        assert loaded.syntax == "compact"
        assert repr(loaded) == f"LoadedCompactResource(canonical_id={resolved!r})"
        assert loaded.canonical_id == resolved
        assert loaded.content == content
        assert loaded.inherited_namespace == ""
        assert policy.reads == [(str(path), None, "schema")]
        assert policy.parsed == []
        dependency = loader.dependencies()[0]
        assert dependency.canonical_id == resolved
        assert dependency.content_sha256 == hashlib.sha256(content).hexdigest()

    def test_compact_load_dedupes_by_canonical_id(self, tmp_path):
        path = tmp_path / "schema.rnc"
        path.write_bytes(b"element root { text }\n")
        policy = RecordingPolicy(tmp_path)
        loader = ResourceLoader(policy=policy)

        first = loader.load(path, syntax="compact")
        second = loader.load(tmp_path / "." / path.name, syntax="compact")

        assert second is first
        assert len(loader.dependencies()) == 1
        assert policy.parsed == []

    def test_compact_view_inherits_a_namespace_without_mutating_the_resource(self, tmp_path):
        path = tmp_path / "schema.rnc"
        path.write_bytes(b"element root { text }\n")
        resource = ResourceLoader().load(path, syntax="compact")

        view = resource.with_namespace("urn:one")

        assert view is not resource
        assert view.inherited_namespace == "urn:one"
        assert view.namespace_at(resource.origins) == "urn:one"
        assert view.content == resource.content
        assert view.canonical_id == resource.canonical_id
        assert resource.inherited_namespace == ""
        assert resource.namespace_at(resource.origins) == ""
        assert resource.with_namespace("") is resource
        assert view.with_namespace("urn:one") is view

    def test_unknown_resource_syntax_is_rejected_before_reading(self, tmp_path):
        path = tmp_path / "schema.rnc"
        path.write_bytes(b"element root { text }\n")
        policy = RecordingPolicy(tmp_path)

        with pytest.raises(ValueError, match="syntax"):
            ResourceLoader(policy=policy).load(path, syntax="auto")  # type: ignore[arg-type]

        assert policy.reads == []

    def test_a_canonical_resource_keeps_one_syntax(self, tmp_path):
        path = tmp_path / "schema.rnc"
        path.write_bytes(b"element root { text }\n")
        loader = ResourceLoader()
        loader.load(path, syntax="compact")

        with pytest.raises(ValueError, match="compact"):
            loader.load(path, syntax="xml")


class TestCompactResolve:
    def test_compact_resolve_reads_a_dependency_as_compact_regardless_of_suffix(self, tmp_path):
        child_content = b"element child { empty }\n"
        (tmp_path / "main.rnc").write_bytes(b'include "child.rng"\n')
        (tmp_path / "child.rng").write_bytes(child_content)
        policy = RecordingPolicy(tmp_path)
        loader = ResourceLoader(policy=policy)
        main = loader.load(tmp_path / "main.rnc", syntax="compact")

        child = loader.resolve("child.rng", main, None)

        assert isinstance(main, LoadedCompactResource)
        assert isinstance(child, LoadedCompactResource)
        assert child.syntax == "compact"
        assert child.content == child_content
        assert child.canonical_id == str((tmp_path / "child.rng").resolve())
        assert policy.parsed == []

    def test_an_xml_closure_cannot_resolve_a_compact_resource(self, tmp_path):
        child = tmp_path / "child.rng"
        child.write_bytes(b"element child { empty }\n")
        (tmp_path / "main.rng").write_text(RNG_START, encoding="utf-8")
        loader = ResourceLoader()
        loader.load(child, syntax="compact")
        main = loader.load(tmp_path / "main.rng")

        with pytest.raises(ValueError, match="compact"):
            loader.resolve("child.rng", main, None)


class TestResolve:
    def test_resolve_uses_the_containing_resource_not_the_working_directory(self):
        loader = ResourceLoader()
        main = loader.load(FIXTURES / "external" / "main.rng")

        direct = loader.resolve("parts/child.rng", main, None)
        relative = loader.resolve("child.rng", main, resolve_xml_base(main.canonical_id, "parts/"))
        normalized = loader.resolve("parts/../parts/child.rng", main, None)

        expected = str((FIXTURES / "external" / "parts" / "child.rng").resolve())
        assert direct.canonical_id == expected
        assert relative is direct
        assert normalized is direct

    def test_relative_resolution_from_an_anonymous_stream_is_denied(self):
        loader = ResourceLoader()
        resource = loader.load(io.StringIO(RNG_START))

        with pytest.raises(ResourcePolicyError, match="named source base"):
            loader.resolve("child.rng", resource, None)

    def test_absolute_remote_xml_base_pivots_through_the_policy(self):
        url = "https://example.com/schemas/child.rng"
        policy = MemoryPolicy(
            {url: f'<element xmlns="{RNG_NS}" name="child"><empty/></element>'.encode()}
        )
        loader = ResourceLoader(policy=policy)
        main = ResourceLoader().load(FIXTURES / "external" / "main.rng")

        loaded = loader.resolve("child.rng", main, "https://example.com/schemas/")

        assert loaded.canonical_id == url
        assert policy.reads == [(url, None, "schema")]

    def test_absolute_remote_reference_is_not_joined_to_a_local_base(self):
        url = "https://example.com/absolute.rng"
        policy = MemoryPolicy({url: RNG_START.encode()})
        loader = ResourceLoader(policy=policy)
        main = ResourceLoader().load(FIXTURES / "external" / "main.rng")

        loaded = loader.resolve(url, main, None)

        assert loaded.canonical_id == url
        assert policy.reads == [(url, None, "schema")]

    def test_absolute_local_reference_is_independent_of_the_containing_base(self, tmp_path):
        target = (tmp_path / "absolute.rng").resolve()
        target.write_text(RNG_START, encoding="utf-8")
        loader = ResourceLoader()
        main = loader.load(FIXTURES / "external" / "main.rng")

        loaded = loader.resolve(str(target), main, None)

        assert loaded.canonical_id == str(target)

    def test_remote_containing_resource_relative_xml_base_through_resolve(self):
        main_url = "https://example.com/schemas/main.rng"
        child_url = "https://example.com/schemas/parts/child.rng"
        policy = MemoryPolicy(
            {
                main_url: RNG_START.encode(),
                child_url: f'<element xmlns="{RNG_NS}" name="child"><empty/></element>'.encode(),
            }
        )
        loader = ResourceLoader(policy=policy)
        containing = loader.load(main_url)

        loaded = loader.resolve("child.rng", containing, resolve_xml_base(main_url, "parts/"))

        assert loaded.canonical_id == child_url
        assert loaded.root.get("name") == "child"
        assert policy.reads[-1] == (child_url, None, "schema")

    def test_empty_reference_resolves_to_the_containing_resource(self):
        loader = ResourceLoader()
        main = loader.load(FIXTURES / "external" / "main.rng")

        assert loader.resolve("", main, None) is main

        dependency = next(
            item for item in loader.dependencies() if item.canonical_id == main.canonical_id
        )
        assert len(dependency.references) == 1
        assert dependency.references[0].reference == ""
        assert dependency.references[0].containing_id == main.canonical_id

    def test_memory_resource_can_resolve_an_absolute_local_reference(self, tmp_path):
        target = (tmp_path / "absolute.rng").resolve()
        target.write_text(RNG_START, encoding="utf-8")
        loader = ResourceLoader()
        memory = loader.load(io.StringIO(RNG_START))

        loaded = loader.resolve(str(target), memory, "<memory>")

        assert loaded.canonical_id == str(target)

    def test_absolute_reference_from_an_anonymous_stream_needs_no_named_base(self):
        url = "https://example.com/absolute.rng"
        policy = MemoryPolicy({url: RNG_START.encode()})
        loader = ResourceLoader(policy=policy)
        memory = loader.load(io.StringIO(RNG_START))

        loaded = loader.resolve(url, memory, "<memory>")

        assert loaded.canonical_id == url
        assert policy.reads == [(url, None, "schema")]

    def test_missing_dependency_is_not_cached_as_a_dependency(self):
        loader = ResourceLoader()
        main = loader.load(FIXTURES / "external" / "main.rng")
        missing = str((FIXTURES / "external" / "missing.rng").resolve())

        with pytest.raises(FileNotFoundError):
            loader.resolve("missing.rng", main, None)

        assert [dependency.canonical_id for dependency in loader.dependencies()] == [
            main.canonical_id
        ]
        assert missing not in {dependency.canonical_id for dependency in loader.dependencies()}

    def test_malformed_dependency_raises_a_parse_error(self, tmp_path):
        (tmp_path / "main.rng").write_text(RNG_START, encoding="utf-8")
        (tmp_path / "child.rng").write_text("<child", encoding="utf-8")
        loader = ResourceLoader()
        main = loader.load(tmp_path / "main.rng")

        with pytest.raises(ET.ParseError):
            loader.resolve("child.rng", main, None)

        assert [dependency.canonical_id for dependency in loader.dependencies()] == [
            main.canonical_id
        ]


class TestDependencies:
    def test_dependencies_record_the_closure_and_reference_evidence(self):
        loader = ResourceLoader()
        main = loader.load(FIXTURES / "external" / "main.rng")
        child = loader.resolve("parts/child.rng", main, None)
        loader.resolve("child.rng", main, resolve_xml_base(main.canonical_id, "parts/"))

        dependencies = loader.dependencies()

        assert [dependency.canonical_id for dependency in dependencies] == sorted(
            [main.canonical_id, child.canonical_id]
        )
        by_id = {dependency.canonical_id: dependency for dependency in dependencies}
        root = by_id[main.canonical_id]
        nested = by_id[child.canonical_id]
        assert root.references == ()
        assert root.purpose == "schema"
        assert len(root.content_sha256) == 64
        assert nested.source_id == child.canonical_id
        assert len(nested.references) == 2
        assert {reference.reference for reference in nested.references} == {
            "parts/child.rng",
            "child.rng",
        }
        assert all(
            reference.containing_id == main.canonical_id
            and reference.canonical_id == child.canonical_id
            for reference in nested.references
        )

    def test_repeated_resolve_records_each_distinct_reference_once(self):
        loader = ResourceLoader()
        main = loader.load(FIXTURES / "external" / "main.rng")
        loader.resolve("parts/child.rng", main, None)
        loader.resolve("parts/child.rng", main, None)

        child_id = str((FIXTURES / "external" / "parts" / "child.rng").resolve())
        dependency = next(item for item in loader.dependencies() if item.canonical_id == child_id)
        assert len(dependency.references) == 1

    def test_dependencies_are_sorted_and_immutable_values(self):
        loader = ResourceLoader()
        main = loader.load(FIXTURES / "external" / "main.rng")
        loader.resolve("parts/child.rng", main, None)

        dependencies = loader.dependencies()
        assert dependencies == tuple(
            sorted(dependencies, key=lambda dependency: dependency.canonical_id)
        )
        assert all(isinstance(dependency.references, tuple) for dependency in dependencies)

        reference = dependencies[-1].references[0]
        with pytest.raises(FrozenInstanceError):
            reference.reference = "other"  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            dependencies[-1].canonical_id = "other"  # type: ignore[misc]

    def test_dependency_types_are_hashable(self):
        reference = ResourceReference(
            canonical_id="b.rng",
            containing_id="a.rng",
            reference="b.rng",
            base="a.rng",
            xml_base=None,
        )
        dependency = ResourceDependency(
            canonical_id="b.rng",
            source_id="b.rng",
            purpose="schema",
            content_sha256="0" * 64,
            references=(reference,),
        )

        assert len({reference, reference}) == 1
        assert len({dependency, dependency}) == 1


class TestXmlBase:
    @pytest.mark.parametrize("base", ["local.rng", "https://example.com/main.rng"])
    @pytest.mark.parametrize("xml_base", ["%zz/", "https://example.com/%zz/"])
    def test_malformed_uri_reference_is_rejected_before_resolution(self, base, xml_base):
        with pytest.raises(ResourcePolicyError, match="URI reference"):
            resolve_xml_base(base, xml_base)

    def test_trailing_slash_directory_semantics_are_preserved(self):
        main = str((FIXTURES / "external" / "main.rng").resolve())

        parts = resolve_xml_base(main, "parts/")
        nested = resolve_xml_base(parts, "sub/")

        assert parts.endswith("parts/")
        assert nested.endswith("parts/sub/")

    def test_dot_segments_keep_directory_semantics(self, tmp_path):
        main = str(tmp_path / "a" / "b" / "main.rng")
        a_dir = str(tmp_path / "a")
        b_dir = str(tmp_path / "a" / "b")

        assert resolve_xml_base(main, ".") == b_dir + "/"
        assert resolve_xml_base(main, "..") == a_dir + "/"
        assert resolve_xml_base(main, "parts/.") == b_dir + "/parts/"
        assert resolve_xml_base(main, "parts/..") == b_dir + "/"

        assert resolve_xml_base(resolve_xml_base(main, "."), "child.rng") == (b_dir + "/child.rng")
        assert resolve_xml_base(resolve_xml_base(main, ".."), "child.rng") == (a_dir + "/child.rng")

    def test_relative_xml_base_resolves_against_the_current_base(self):
        main = (FIXTURES / "external" / "main.rng").resolve()

        assert resolve_xml_base(str(main), "parts") == str(main.parent / "parts")
        assert resolve_xml_base(str(main), "../other.rng") == str(main.parent.parent / "other.rng")

    def test_empty_xml_base_keeps_the_current_base(self):
        main = (FIXTURES / "external" / "main.rng").resolve()
        assert resolve_xml_base(str(main), "") == str(main)

    def test_xml_base_fragment_is_rejected_before_filesystem_normalization(self, tmp_path):
        main = (tmp_path / "main.rng").resolve()

        with pytest.raises(ResourcePolicyError, match="fragment"):
            resolve_xml_base(str(main), "parts/#fragment")

        assert (
            resolve_xml_base(str(main), "parts%23fragment/")
            == str(tmp_path / "parts#fragment") + "/"
        )

    def test_absolute_remote_xml_base_is_used_verbatim(self):
        url = "https://example.com/schema.rng"
        assert resolve_xml_base("local.rng", url) == url

    def test_remote_base_uses_rfc_3986_joining(self):
        base = "https://example.com/schemas/main.rng"
        assert resolve_xml_base(base, "../other/") == "https://example.com/other/"

    def test_relative_xml_base_from_memory_is_denied(self):
        with pytest.raises(ResourcePolicyError, match="named source base"):
            resolve_xml_base("<memory>", "parts/")

    def test_absolute_local_xml_base_from_memory_is_allowed(self, tmp_path):
        target = (tmp_path / "parts").resolve()
        assert resolve_xml_base("<memory>", str(target)) == str(target)


def test_unicode_href_is_xlink_escaped_before_policy_lookup():
    url = "https://example.com/caf%C3%A9.rng"
    policy = MemoryPolicy(
        {url: f'<element xmlns="{RNG_NS}" name="child"><empty/></element>'.encode()}
    )
    loader = ResourceLoader(policy=policy)
    main = ResourceLoader().load(FIXTURES / "external" / "main.rng")

    loaded = loader.resolve("caf\u00e9.rng", main, "https://example.com/")

    assert loaded.canonical_id == url
    assert policy.reads == [(url, None, "schema")]


def test_local_space_bearing_href_maps_through_the_policy(tmp_path):
    child = tmp_path / "child with space.rng"
    child.write_text(RNG_START, encoding="utf-8")
    main_path = tmp_path / "main.rng"
    main_path.write_text(RNG_START, encoding="utf-8")
    policy = MemoryPolicy(
        {str(main_path): RNG_START.encode(), str(child): RNG_START.encode()},
        aliases={str(child): "urn:canonical:child"},
    )
    loader = ResourceLoader(policy=policy)
    containing = loader.load(main_path)

    loaded = loader.resolve("child with space.rng", containing, None)

    assert loaded.canonical_id == "urn:canonical:child"
    assert policy.reads[-1][0] == str(child)


def test_fragment_href_never_opens_a_decoy_file(tmp_path):
    decoy = tmp_path / "child.rng#fragment"
    decoy.write_text(RNG_START, encoding="utf-8")
    main_path = tmp_path / "main.rng"
    main_path.write_text(RNG_START, encoding="utf-8")
    policy = RecordingPolicy(tmp_path)
    loader = ResourceLoader(policy=policy)
    main = loader.load(main_path)
    reads = list(policy.reads)

    with pytest.raises(ResourcePolicyError, match="fragment"):
        loader.resolve("child.rng#fragment", main, None)

    assert policy.reads == reads
    assert str(decoy) not in {dependency.canonical_id for dependency in loader.dependencies()}


def test_fragment_href_is_rejected_without_a_policy(tmp_path):
    decoy = tmp_path / "child.rng#fragment"
    decoy.write_text(RNG_START, encoding="utf-8")
    main_path = tmp_path / "main.rng"
    main_path.write_text(RNG_START, encoding="utf-8")
    loader = ResourceLoader()
    main = loader.load(main_path)

    with pytest.raises(ResourcePolicyError, match="fragment"):
        loader.resolve("child.rng#fragment", main, None)


def test_fragment_with_dot_segments_is_rejected_before_path_normalization(tmp_path):
    target = tmp_path / "target.rng"
    target.write_text(RNG_START, encoding="utf-8")
    main_path = tmp_path / "main.rng"
    main_path.write_text(RNG_START, encoding="utf-8")
    policy = RecordingPolicy(tmp_path)
    loader = ResourceLoader(policy=policy)
    main = loader.load(main_path)
    reads = list(policy.reads)

    with pytest.raises(ResourcePolicyError, match="fragment"):
        loader.resolve("child.rng#fragment/../target.rng", main, None)

    assert policy.reads == reads
    assert str(target.resolve()) not in {item.canonical_id for item in loader.dependencies()}


def test_percent_encoded_hash_is_a_filename_not_a_fragment(tmp_path):
    child = tmp_path / "child#name.rng"
    child.write_text(RNG_START, encoding="utf-8")
    main_path = tmp_path / "main.rng"
    main_path.write_text(RNG_START, encoding="utf-8")
    policy = RecordingPolicy(tmp_path)
    loader = ResourceLoader(policy=policy)
    main = loader.load(main_path)
    reads = list(policy.reads)

    loaded = loader.resolve("child%23name.rng", main, None)

    assert loaded.canonical_id == str(child.resolve())
    assert policy.reads == [*reads, (str(child.resolve()), None, "schema")]
    assert str(child.resolve()) in {item.canonical_id for item in loader.dependencies()}


def test_nested_effective_bases_are_not_percent_decoded_again(tmp_path):
    correct_directory = tmp_path / "parts%20" / "sub "
    correct_directory.mkdir(parents=True)
    correct = correct_directory / "child.rng"
    correct.write_text(RNG_START, encoding="utf-8")
    decoy_directory = tmp_path / "parts " / "sub "
    decoy_directory.mkdir(parents=True)
    decoy = decoy_directory / "child.rng"
    decoy.write_text(RNG_START, encoding="utf-8")
    main_path = tmp_path / "main.rng"
    main_path.write_text(RNG_START, encoding="utf-8")
    loader = ResourceLoader()
    main = loader.load(main_path)

    base = resolve_xml_base(resolve_xml_base(main.canonical_id, "parts%2520/"), "sub%20/")
    assert base == str(correct_directory.resolve()) + "/"
    loaded = loader.resolve("child.rng", main, base)

    assert loaded.canonical_id == str(correct.resolve())
    assert loaded.canonical_id != str(decoy.resolve())


def test_xml_base_values_are_xlink_escaped():
    local = str((FIXTURES / "external" / "main.rng").resolve())

    assert (
        resolve_xml_base("https://example.com/schemas/main.rng", "caf\u00e9/")
        == "https://example.com/schemas/caf%C3%A9/"
    )
    assert (
        resolve_xml_base(local, "parts with space/")
        == str((FIXTURES / "external" / "parts with space").resolve()) + "/"
    )


def test_a_malformed_utf8_local_reference_is_rejected():
    local = str((FIXTURES / "external" / "main.rng").resolve())

    with pytest.raises(ResourcePolicyError, match="UTF-8"):
        resolve_xml_base(local, "%FF")


def test_malformed_xml_base_never_opens_a_local_decoy(tmp_path):
    decoy = tmp_path / "%zz" / "child.rng"
    decoy.parent.mkdir()
    decoy.write_text(f'<element xmlns="{RNG_NS}" name="child"><empty/></element>', encoding="utf-8")
    main = tmp_path / "main.rng"
    main.write_text(
        f'<externalRef xmlns="{RNG_NS}" xml:base="%zz/" href="child.rng"/>',
        encoding="utf-8",
    )
    policy = RecordingPolicy(tmp_path)

    schema = RelaxNGSchema.compile(main, resource_policy=policy)

    with pytest.raises(ValidationError):
        schema.require_valid()
    finding = next(issue for issue in schema.report.issues if issue.code == "relaxng-invalid-base")
    assert finding.location == IssueLocation(
        str(main),
        (PathStep(f"{{{RNG_NS}}}externalRef", 1),),
        attribute="{http://www.w3.org/XML/1998/namespace}base",
    )
    assert len(finding.message) < 200
    assert str(decoy) not in {source for source, _, _ in policy.reads}
    assert [dependency.canonical_id for dependency in schema.dependencies] == [str(main)]


@pytest.mark.parametrize("xml_base", ["%zz/", "https://example.com/%zz/"])
def test_malformed_xml_base_never_opens_a_remote_decoy(xml_base):
    main = "https://example.com/main.rng"
    decoy = "https://example.com/%zz/child.rng"
    policy = MemoryPolicy(
        {
            main: f'<externalRef xmlns="{RNG_NS}" xml:base="{xml_base}" href="child.rng"/>'.encode(),
            decoy: f'<element xmlns="{RNG_NS}" name="child"><empty/></element>'.encode(),
        }
    )

    schema = RelaxNGSchema.compile(main, resource_policy=policy)

    with pytest.raises(ValidationError):
        schema.require_valid()
    assert "relaxng-invalid-base" in {issue.code for issue in schema.report.issues}
    assert decoy not in {source for source, _, _ in policy.reads}
    assert [dependency.canonical_id for dependency in schema.dependencies] == [main]


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("xml_base", ["caf\u00e9 %23/", "caf%C3%A9%20%23/"])
def test_valid_xml_base_keeps_unicode_space_and_encoded_hash(tmp_path, remote, xml_base):
    base = "https://example.com" if remote else str(tmp_path)
    main = f"{base}/main.rng"
    directory = "caf%C3%A9%20%23" if remote else "caf\u00e9 #"
    child = f"{base}/{directory}/child.rng"
    policy = MemoryPolicy(
        {
            main: f'<externalRef xmlns="{RNG_NS}" xml:base="{xml_base}" href="child.rng"/>'.encode(),
            child: f'<element xmlns="{RNG_NS}" name="child"><empty/></element>'.encode(),
        }
    )

    schema = RelaxNGSchema.compile(main, resource_policy=policy)

    schema.require_valid()
    assert policy.reads == [(main, None, "schema"), (child, None, "schema")]
    assert {dependency.canonical_id for dependency in schema.dependencies} == {main, child}
