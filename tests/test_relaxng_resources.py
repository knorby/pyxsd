"""Authorized RELAX NG resource loading and canonical dependency closure.

The loader is the only path by which a RELAX NG schema reads another
document.  It resolves every reference against its containing resource (not
the process working directory), deduplicates canonical resources, and keeps
complete evidence for cache identity.  These tests pin policy forwarding,
resolution semantics, origin recording, and dependency evidence.
"""

from __future__ import annotations

import io
from dataclasses import FrozenInstanceError
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from pyxsd.locations import IssueLocation, PathStep
from pyxsd.namespaces import parse_with_namespaces
from pyxsd.relaxng.origins import SourceOrigin
from pyxsd.relaxng.resources import (
    LoadedResource,
    ResourceDependency,
    ResourceLoader,
    ResourceReference,
    resolve_xml_base,
)
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


class TestResolve:
    def test_resolve_uses_the_containing_resource_not_the_working_directory(self):
        loader = ResourceLoader()
        main = loader.load(FIXTURES / "external" / "main.rng")

        direct = loader.resolve("parts/child.rng", main, None)
        relative = loader.resolve("child.rng", main, "parts/")
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

        loaded = loader.resolve("child.rng", containing, "parts/")

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
        loader.resolve("child.rng", main, "parts/")

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
