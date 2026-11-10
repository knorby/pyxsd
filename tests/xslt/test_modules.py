"""Source-faithful stylesheet module loading.

The loader is the normative front end's resource seam: it reads every module
once per canonical identity, preserves comments/PIs, namespace bindings, base
URIs, and source locations, and keeps the include/import graph distinct from
later static-resource graphs.  These tests are corpus-independent.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from pyxsd.resource_policy import ResourceData, ResourcePolicyError
from pyxsd.xdm.nodes import NodeKind
from pyxsd.xslt.compiler.modules import ModuleLoader
from pyxsd.xslt.errors import XsltError, XsltStaticError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
DOC_NS = "urn:example:doc"


def _stylesheet(body: str = "", *, attributes: str = "") -> str:
    return (
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" '
        f'xmlns:doc="{DOC_NS}"{attributes}>{body}</xsl:stylesheet>'
    )


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class _MappingPolicy:
    """A minimal policy that admits named in-memory modules."""

    def __init__(self, modules: dict[str, str], *, denied: set[str] | None = None) -> None:
        self.modules = modules
        self.denied = denied or set()
        self.reads: list[tuple[str, str | None, str]] = []

    def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
        self.reads.append((source, base, purpose))
        if source in self.denied:
            raise ResourcePolicyError(f"denied {source}")
        try:
            text = self.modules[source]
        except KeyError:
            raise FileNotFoundError(source) from None
        return ResourceData(source, text.encode("utf-8"))

    def parse(self, data: ResourceData, context: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("the source-faithful loader must parse XDM itself")


def test_loader_reuses_canonical_identity(tmp_path: Path) -> None:
    main = _write(tmp_path, "main.xsl", _stylesheet('<xsl:include href="lib.xsl"/>'))
    library = _write(tmp_path, "lib.xsl", _stylesheet('<xsl:template name="lib"/>'))

    loader = ModuleLoader()
    first = loader.load(main)
    second = loader.load(Path(str(main)))

    assert first is second
    assert first.identity == main.resolve().as_uri()
    assert first.includes == (loader.load(library),)
    assert first.includes[0] is loader.load(library)
    assert first.includes[0].identity == library.resolve().as_uri()
    assert first.imports == ()


def test_include_and_import_graphs_stay_distinct(tmp_path: Path) -> None:
    main = _write(
        tmp_path,
        "main.xsl",
        _stylesheet('<xsl:include href="shared.xsl"/><xsl:import href="base.xsl"/>'),
    )
    shared = _write(tmp_path, "shared.xsl", _stylesheet('<xsl:template name="shared"/>'))
    base = _write(tmp_path, "base.xsl", _stylesheet('<xsl:template name="base"/>'))

    module = ModuleLoader().load(main)

    assert [child.identity for child in module.includes] == [shared.resolve().as_uri()]
    assert [child.identity for child in module.imports] == [base.resolve().as_uri()]
    assert module.includes[0].imports == ()
    assert module.imports[0].includes == ()


def test_include_cycle_reports_xtse0180(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.xsl", _stylesheet('<xsl:include href="b.xsl"/>'))
    _write(tmp_path, "b.xsl", _stylesheet('<xsl:include href="a.xsl"/>'))

    with pytest.raises(XsltStaticError) as caught:
        ModuleLoader().load(a)

    error = caught.value
    assert error.code == "XTSE0180"
    assert error.phase == "static"
    assert error.uri == a.resolve().as_uri()
    assert "includes itself" in str(error)


def test_import_cycle_reports_xtse0210(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.xsl", _stylesheet('<xsl:import href="b.xsl"/>'))
    _write(tmp_path, "b.xsl", _stylesheet('<xsl:import href="a.xsl"/>'))

    with pytest.raises(XsltStaticError) as caught:
        ModuleLoader().load(a)

    assert caught.value.code == "XTSE0210"
    assert "imports itself" in str(caught.value)


def test_simplified_stylesheet_detection_and_version_requirement(tmp_path: Path) -> None:
    plain = _write(
        tmp_path,
        "plain.xsl",
        f'<out xmlns:xsl="{XSLT_NS}" xmlns:doc="{DOC_NS}"/>',
    )

    with pytest.raises(XsltStaticError) as caught:
        ModuleLoader().load(plain)
    assert caught.value.code == "XTSE0150"

    simplified = _write(
        tmp_path,
        "simplified.xsl",
        f'<out xsl:version="3.0" xmlns:xsl="{XSLT_NS}" xmlns:doc="{DOC_NS}"><doc:item/></out>',
    )
    module = ModuleLoader().load(simplified)
    assert module.simplified is True
    assert module.version == "3.0"
    assert module.root.name == "out"
    assert module.root.in_scope_namespaces["doc"] == DOC_NS


def test_xml_base_resolves_include_and_retains_base_context(tmp_path: Path) -> None:
    main = _write(
        tmp_path,
        "main.xsl",
        _stylesheet(
            '<xsl:include href="lib.xsl"/>',
            attributes=' xml:base="modules/"',
        ),
    )
    library = _write(
        tmp_path,
        "modules/lib.xsl",
        _stylesheet('<xsl:template name="lib"/>'),
    )

    module = ModuleLoader().load(main)

    assert module.includes[0].identity == library.resolve().as_uri()
    assert module.root.base_uri == (tmp_path / "modules").resolve().as_uri() + "/"
    assert module.includes[0].root.base_uri == library.resolve().as_uri()


def test_raw_tree_retains_comments_pis_namespaces_and_provenance(tmp_path: Path) -> None:
    main = _write(
        tmp_path,
        "main.xsl",
        '<?xml version="1.0"?><!--before--><?build keep?>'
        + _stylesheet(
            "<!--inside--><?inner data?><xsl:template name='t' doc:kind='x'/>",
            attributes=' xml:base="sub/"',
        )
        + "<!--after-->",
    )

    module = ModuleLoader().load(main)
    document = module.document

    assert [child.kind for child in document.children] == [
        NodeKind.COMMENT,
        NodeKind.PROCESSING_INSTRUCTION,
        NodeKind.ELEMENT,
        NodeKind.COMMENT,
    ]
    root = module.root
    assert root.name == f"{{{XSLT_NS}}}stylesheet"
    assert root.prefix == "xsl"
    assert root.namespace_uri == XSLT_NS
    assert root.in_scope_namespaces["doc"] == DOC_NS
    assert root.base_uri == (tmp_path / "sub").resolve().as_uri() + "/"
    template = root.children[2]
    assert template.source_location() is not None
    assert template.attribute(f"{{{DOC_NS}}}kind") is not None
    assert template.attribute(f"{{{DOC_NS}}}kind").string_value == "x"


def test_missing_include_preserves_resource_cause(tmp_path: Path) -> None:
    main = _write(tmp_path, "main.xsl", _stylesheet('<xsl:include href="missing.xsl"/>'))

    with pytest.raises(XsltStaticError) as caught:
        ModuleLoader().load(main)

    error = caught.value
    assert error.code == "XTSE0165"
    assert error.uri == main.resolve().as_uri()
    assert error.location is not None
    assert error.location.path[-1].name == f"{{{XSLT_NS}}}include"
    assert isinstance(error.cause, FileNotFoundError)
    assert error.__cause__ is error.cause


def test_policy_denial_preserves_resource_error_cause(tmp_path: Path) -> None:
    main = _write(tmp_path, "main.xsl", _stylesheet('<xsl:include href="secret.xsl"/>'))
    policy = _MappingPolicy({str(main): main.read_text()}, denied={str(tmp_path / "secret.xsl")})

    with pytest.raises(XsltStaticError) as caught:
        ModuleLoader(policy).load(main)

    assert caught.value.code == "XTSE0165"
    assert isinstance(caught.value.cause, ResourcePolicyError)
    assert policy.reads[0] == (str(main), None, "schema")
    assert policy.reads[-1][0] == str(tmp_path / "secret.xsl")


def test_malformed_module_preserves_parser_cause(tmp_path: Path) -> None:
    broken = _write(tmp_path, "broken.xsl", "<xsl:stylesheet")

    with pytest.raises(XsltStaticError) as caught:
        ModuleLoader().load(broken)

    assert caught.value.code == "XTSE0165"
    assert caught.value.uri == broken.resolve().as_uri()
    assert type(caught.value.cause).__name__ == "PyXSDError"
    assert caught.value.__cause__ is caught.value.cause


def test_conditional_include_is_deferred_not_eagerly_loaded(tmp_path: Path) -> None:
    main = _write(
        tmp_path,
        "main.xsl",
        _stylesheet('<xsl:include href="missing.xsl" use-when="false()"/>'),
    )

    module = ModuleLoader().load(main)

    assert module.includes == ()


def test_parser_policy_override_is_rejected_with_a_policy(tmp_path: Path) -> None:
    from pyxsd.xml_policy import XmlParserPolicy

    main = _write(tmp_path, "main.xsl", _stylesheet())
    policy = _MappingPolicy({str(main): main.read_text()})

    with pytest.raises(ResourcePolicyError, match="parser"):
        ModuleLoader(policy, xml_policy=XmlParserPolicy.default())


def test_loader_accepts_named_in_memory_streams() -> None:
    stream = io.StringIO(_stylesheet('<xsl:template name="t"/>'))
    stream.name = "memory.xsl"  # type: ignore[attr-defined]

    module = ModuleLoader().load(stream)

    assert module.identity == Path("memory.xsl").resolve().as_uri()
    assert module.root.name == f"{{{XSLT_NS}}}stylesheet"


def test_error_fields_are_preserved() -> None:
    cause = ValueError("boom")
    error = XsltError(
        "failed",
        code="XTSE9999",
        phase="static",
        uri="file:///x.xsl",
        location=None,
        cause=cause,
    )

    assert error.code == "XTSE9999"
    assert error.phase == "static"
    assert error.uri == "file:///x.xsl"
    assert error.cause is cause
    assert "XTSE9999" in str(error)
    assert "file:///x.xsl" in str(error)


def test_source_module_helpers_and_provenance(tmp_path: Path) -> None:
    main = _write(
        tmp_path,
        "main.xsl",
        "<!--before-->" + _stylesheet('<xsl:template name="named"/><xsl:call-template/>'),
    )

    module = ModuleLoader().load(main)

    assert module.uri == module.identity
    assert module.base_uri == main.resolve().as_uri()
    assert module.attribute("version") == "3.0"
    assert module.source_location("named") is not None
    assert module.source_location("missing") is None
    assert module.find(f"{{{XSLT_NS}}}template") is not None
    assert module.find("named") is not None
    assert module.find("template") is not None
    assert module.find("missing") is None
    assert module.find("comment") is None


def test_anonymous_and_byte_streams_keep_source_fidelity() -> None:
    named = io.BytesIO(_stylesheet('<xsl:template name="named"/>').encode())
    named.name = "bytes.xsl"  # type: ignore[attr-defined]
    module = ModuleLoader().load(named)
    assert module.identity == Path("bytes.xsl").resolve().as_uri()
    assert module.find("named") is not None

    anonymous = io.StringIO(_stylesheet())
    anonymous_module = ModuleLoader().load(anonymous)
    assert anonymous_module.identity is None
    assert anonymous_module.base_uri is None
    assert anonymous_module.find("template") is None


def test_remote_and_file_uri_source_identities(tmp_path: Path) -> None:
    main = _write(tmp_path, "main.xsl", _stylesheet())
    policy = _MappingPolicy({str(main): main.read_text()})

    class UriPolicy(_MappingPolicy):
        def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
            data = super().read(source, base=base, purpose=purpose)
            return ResourceData("https://example.invalid/remote.xsl", data.content)

    remote = ModuleLoader(UriPolicy({str(main): main.read_text()})).load(main)
    assert remote.identity == "https://example.invalid/remote.xsl"
    assert remote.base_uri == "https://example.invalid/remote.xsl"

    class FilePolicy(_MappingPolicy):
        def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
            data = super().read(source, base=base, purpose=purpose)
            return ResourceData(main.resolve().as_uri(), data.content)

    file_module = ModuleLoader(FilePolicy({str(main): main.read_text()})).load(main)
    assert file_module.identity == main.resolve().as_uri()
    assert file_module.source_id == main.resolve().as_uri()
    assert policy.reads == []


def test_empty_and_remote_hrefs_report_resource_errors(tmp_path: Path) -> None:
    empty = _write(tmp_path, "empty.xsl", _stylesheet('<xsl:include href=""/>'))
    with pytest.raises(XsltStaticError) as caught:
        ModuleLoader().load(empty)
    assert caught.value.code == "XTSE0165"
    assert isinstance(caught.value.cause, ResourcePolicyError)

    remote = _write(
        tmp_path,
        "remote.xsl",
        _stylesheet('<xsl:include href="https://example.invalid/lib.xsl"/>'),
    )
    with pytest.raises(XsltStaticError) as caught:
        ModuleLoader().load(remote)
    assert caught.value.code == "XTSE0165"
    assert isinstance(caught.value.cause, ResourcePolicyError)


def test_relative_include_without_a_base_is_a_resource_error() -> None:
    stream = io.StringIO(_stylesheet('<xsl:include href="lib.xsl"/>'))

    with pytest.raises(XsltStaticError) as caught:
        ModuleLoader().load(stream)

    assert caught.value.code == "XTSE0165"
    assert isinstance(caught.value.cause, ResourcePolicyError)


def test_unsupported_xslt_root_reports_xtse0010(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "template.xsl",
        f'<xsl:template name="x" xmlns:xsl="{XSLT_NS}"/>',
    )

    with pytest.raises(XsltStaticError) as caught:
        ModuleLoader().load(path)

    assert caught.value.code == "XTSE0010"
    assert caught.value.location is not None


def test_conditional_forms_defer_resolution(tmp_path: Path) -> None:
    body = "".join(
        f'<xsl:include href="missing-{index}.xsl" {attribute}="false()"/>'
        for index, attribute in enumerate(("use-when", "xsl:use-when", "_use-when"))
    )
    main = _write(tmp_path, "main.xsl", _stylesheet(body))

    module = ModuleLoader().load(main)
    assert module.includes == ()

    root_conditional = _write(
        tmp_path,
        "root.xsl",
        _stylesheet('<xsl:include href="missing.xsl"/>', attributes=' use-when="false()"'),
    )
    assert ModuleLoader().load(root_conditional).includes == ()


def test_conditional_shadow_href_is_deferred(tmp_path: Path) -> None:
    main = _write(
        tmp_path,
        "main.xsl",
        _stylesheet('<xsl:include _href="missing.xsl"/>'),
    )

    assert ModuleLoader().load(main).includes == ()


def test_include_without_href_is_left_for_static_processing(tmp_path: Path) -> None:
    main = _write(tmp_path, "main.xsl", _stylesheet("<xsl:include/>"))

    module = ModuleLoader().load(main)

    assert module.includes == ()
    assert any(child.name == f"{{{XSLT_NS}}}include" for child in module.root.children)


def test_loader_parser_limits_are_applied(tmp_path: Path) -> None:
    from pyxsd.xml_policy import XmlLimits

    main = _write(tmp_path, "main.xsl", _stylesheet('<xsl:template name="t"/>'))

    with pytest.raises(XsltStaticError) as caught:
        ModuleLoader(xml_limits=XmlLimits(max_elements=1)).load(main)

    assert caught.value.code == "XTSE0165"
    assert isinstance(caught.value.cause, ResourcePolicyError)


def test_resolve_reference_cache_hit_returns_the_same_module(tmp_path: Path) -> None:
    library = _write(tmp_path, "lib.xsl", _stylesheet())
    loader = ModuleLoader()
    first = loader.load(library)

    second = loader.resolve_reference(
        "include",
        "lib.xsl",
        base_uri=library.resolve().as_uri(),
        owner_uri=library.resolve().as_uri(),
        location=None,
    )

    assert second is first


def test_absolute_reference_without_a_base_resolves_locally(tmp_path: Path) -> None:
    library = _write(tmp_path, "lib.xsl", _stylesheet())
    module = ModuleLoader().load(library)

    resolved = module.loader.resolve_reference(
        "include",
        str(library),
        base_uri=None,
        owner_uri=None,
        location=None,
    )

    assert resolved is module


def test_source_module_find_skips_comment_children(tmp_path: Path) -> None:
    main = _write(
        tmp_path,
        "main.xsl",
        _stylesheet("<!--inside--><xsl:template name='t'/>"),
    )

    module = ModuleLoader().load(main)

    assert module.find("missing") is None
    assert module.find("t") is not None


def test_malformed_included_module_reports_the_target_uri(tmp_path: Path) -> None:
    main = _write(tmp_path, "main.xsl", _stylesheet('<xsl:include href="broken.xsl"/>'))
    broken = _write(tmp_path, "broken.xsl", "<xsl:stylesheet")

    with pytest.raises(XsltStaticError) as caught:
        ModuleLoader().load(main)

    assert caught.value.code == "XTSE0165"
    assert caught.value.uri == broken.resolve().as_uri()
    assert caught.value.location is not None


def test_explicit_file_uri_include_is_denied_without_a_policy(tmp_path: Path) -> None:
    library = _write(tmp_path, "lib.xsl", _stylesheet('<xsl:template name="lib"/>'))
    main = _write(
        tmp_path,
        "main.xsl",
        _stylesheet(f'<xsl:include href="{library.resolve().as_uri()}"/>'),
    )

    with pytest.raises(XsltStaticError) as caught:
        ModuleLoader().load(main)

    assert caught.value.code == "XTSE0165"
    assert isinstance(caught.value.cause, ResourcePolicyError)

    with pytest.raises(XsltStaticError) as caught:
        ModuleLoader().load(library.resolve().as_uri())
    assert caught.value.code == "XTSE0165"
    assert isinstance(caught.value.cause, ResourcePolicyError)


def test_explicit_file_uri_include_can_be_admitted_by_policy(tmp_path: Path) -> None:
    library = _write(tmp_path, "lib.xsl", _stylesheet('<xsl:template name="lib"/>'))
    main = _write(
        tmp_path,
        "main.xsl",
        _stylesheet(f'<xsl:include href="{library.resolve().as_uri()}"/>'),
    )
    uri = library.resolve().as_uri()
    policy = _MappingPolicy({str(main): main.read_text(), uri: library.read_text()})

    module = ModuleLoader(policy).load(main)

    assert module.includes[0].identity == uri
    assert module.includes[0].find("lib") is not None


class _ChunkedReader:
    def __init__(self, text: str, chunk_size: int = 5) -> None:
        self._text = text
        self._index = 0
        self._chunk_size = chunk_size
        self.requests: list[int] = []
        self.name = "chunked.xsl"

    def read(self, size: int = -1) -> str:
        self.requests.append(size)
        if self._index >= len(self._text):
            return ""
        limit = self._chunk_size if size < 0 else min(size, self._chunk_size)
        chunk = self._text[self._index : self._index + limit]
        self._index += len(chunk)
        return chunk


def test_chunked_stream_is_read_to_eof() -> None:
    stream = _ChunkedReader(_stylesheet('<xsl:template name="last-template"/>'))

    module = ModuleLoader().load(stream)

    assert module.find("last-template") is not None
    assert len(stream.requests) > 1


def test_text_stream_encoding_declaration_is_normalized() -> None:
    text = '<?xml version="1.0" encoding="iso-8859-1"?>' + _stylesheet('<xsl:template name="t"/>')
    stream = io.StringIO(text)
    stream.name = "text.xsl"  # type: ignore[attr-defined]

    module = ModuleLoader().load(stream)

    assert module.find("t") is not None


def test_stream_byte_budget_bounds_reads() -> None:
    from pyxsd.xml_policy import XmlLimits

    stream = _ChunkedReader(_stylesheet(), chunk_size=64)

    with pytest.raises(XsltStaticError) as caught:
        ModuleLoader(xml_limits=XmlLimits(max_bytes=20)).load(stream)

    assert caught.value.code == "XTSE0165"
    assert isinstance(caught.value.cause, ResourcePolicyError)
    assert max(stream.requests) <= 21


def test_relative_include_against_remote_base_stays_remote(tmp_path: Path) -> None:
    main = _write(
        tmp_path,
        "main.xsl",
        _stylesheet('<xsl:include href="lib.xsl"/>'),
    )
    remote_lib = _stylesheet('<xsl:template name="lib"/>')

    class RemotePolicy(_MappingPolicy):
        def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
            if source.endswith("main.xsl"):
                return ResourceData(
                    "https://example.invalid/main.xsl",
                    main.read_text().encode("utf-8"),
                )
            if source == "https://example.invalid/lib.xsl":
                return ResourceData(source, remote_lib.encode("utf-8"))
            raise FileNotFoundError(source)

    module = ModuleLoader(RemotePolicy({})).load(main)

    assert module.includes[0].identity == "https://example.invalid/lib.xsl"
