"""Environment assembly: the compile-time schema pool and its fingerprint.

A catalog case declares the schemas its stylesheet imports or its sources are
validated against.  ``build_schema_pool`` compiles them under their own declared
XSD versions and composes one pool; ``environment_fingerprint`` names every
declaration that can change the compiled artifact, so two cases that share a
stylesheet never share a compiled stylesheet.  These tests are
corpus-independent and use small synthetic schemas.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from tests.xslt import suite
from tests.xslt.catalog import CatalogError
from tests.xslt.environment import (
    _COMPILED_SCHEMA_CACHE_SIZE,
    _COMPILED_SCHEMAS,
    build_schema_pool,
    environment_fingerprint,
)
from tests.xslt.model import (
    CATALOG_NS,
    Collection,
    Environment,
    Invocation,
    Package,
    Param,
    Schema,
    Source,
    Stylesheet,
)
from tests.xslt.model import TestCase as CatalogCase
from tests.xslt.outcomes import Outcome
from tests.xslt.runner import CaseExecutionError
from tests.xslt.support import stylesheet

from pyxsd.schema import Schema as CompiledSchema
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltStaticError

XSD_NS = "http://www.w3.org/2001/XMLSchema"
MONEY_NS = "urn:money"
SHOP_NS = "urn:shop"
XHTML_NS = "http://www.w3.org/1999/xhtml"
HAT_NS = "urn:hats"
REDEFINE_NS = "urn:redefine"

XSLT30 = suite.SUITE_PROFILES["xslt30"]
XSLT10 = suite.SUITE_PROFILES["xslt10"]


@pytest.fixture(autouse=True)
def _empty_caches():
    _COMPILED_SCHEMAS.clear()
    suite._COMPILED.clear()
    yield
    _COMPILED_SCHEMAS.clear()
    suite._COMPILED.clear()


def _write(root: Path, name: str, text: str) -> Path:
    target = root / name
    target.write_text(text, encoding="utf-8")
    return target


def _simple_schema(namespace: str, name: str) -> str:
    return f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{namespace}"
        xmlns:t="{namespace}" elementFormDefault="qualified">
      <xs:simpleType name="{name}">
        <xs:restriction base="xs:string"><xs:maxLength value="8"/></xs:restriction>
      </xs:simpleType>
      <xs:element name="{name}" type="t:{name}"/>
    </xs:schema>"""


def _case(
    root: Path | None,
    *,
    schemas: tuple[Schema, ...] = (),
    packages: tuple[Package, ...] = (),
    collections: tuple[Collection, ...] = (),
    sources: tuple[Source, ...] = (),
    params: tuple[Param, ...] = (),
    result: str = "",
) -> CatalogCase:
    return CatalogCase(
        name="t/case",
        local_name="case",
        test_set="t",
        description="",
        source=PurePosixPath("t"),
        environment=Environment(
            schemas=schemas,
            packages=packages,
            collections=collections,
            sources=sources,
        ),
        invocation=Invocation(
            stylesheets=(Stylesheet(role="principal", path=PurePosixPath("sheet.xsl")),),
            params=params,
        ),
        document_root=root,
        result_xml=result,
    )


def _schema(role: str, name: str, *, xsd_version: str = "1.0", uri: str | None = None) -> Schema:
    return Schema(
        role=role,
        file=name,
        path=PurePosixPath(name),
        uri=uri,
        xsd_version=xsd_version,
    )


def _counting_compiles(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, dict[str, Any]]]:
    """Record every schema compile the environment module performs."""
    calls: list[tuple[Path, dict[str, Any]]] = []
    original = CompiledSchema.compile

    def compile(cls, source, **kwargs):
        calls.append((Path(str(source)), kwargs))
        # A policy in these tests is a sentinel: the compile itself stays real.
        forwarded = {key: value for key, value in kwargs.items() if key != "resource_policy"}
        return original(source, **forwarded)

    monkeypatch.setattr(CompiledSchema, "compile", classmethod(compile))
    return calls


# ---------------------------------------------------------------------------
# Pool assembly


def test_schema_pool_uses_each_declared_xsd_version(tmp_path: Path) -> None:
    _write(tmp_path, "money.xsd", _simple_schema(MONEY_NS, "amount"))
    _write(tmp_path, "shop.xsd", _simple_schema(SHOP_NS, "sku"))
    case = _case(
        tmp_path,
        schemas=(
            _schema("stylesheet-import", "money.xsd", xsd_version="1.0"),
            _schema("stylesheet-import", "shop.xsd", xsd_version="1.1"),
        ),
    )

    pool = build_schema_pool(case, xsd_version="1.1")

    assert pool is not None
    assert [schema.xsd_version for schema in pool.raw_schemas] == ["1.0", "1.1"]
    assert pool.xsd_version == "1.1"


def test_duplicate_target_namespaces_merge(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "xhtml-a.xsd",
        f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{XHTML_NS}"
            xmlns:h="{XHTML_NS}" elementFormDefault="qualified">
          <xs:element name="html" type="xs:string"/>
        </xs:schema>""",
    )
    _write(
        tmp_path,
        "xhtml-b.xsd",
        f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{XHTML_NS}"
            xmlns:h="{XHTML_NS}" elementFormDefault="qualified">
          <xs:element name="body" type="xs:string"/>
        </xs:schema>""",
    )
    case = _case(
        tmp_path,
        schemas=(
            _schema("stylesheet-import", "xhtml-a.xsd"),
            _schema("stylesheet-import", "xhtml-b.xsd"),
        ),
    )

    pool = build_schema_pool(case, xsd_version="1.1")

    assert pool is not None
    assert pool.get_element(f"{{{XHTML_NS}}}html") is not None
    assert pool.get_element(f"{{{XHTML_NS}}}body") is not None


def test_a_secondary_document_the_principal_composes_is_not_a_root(tmp_path: Path) -> None:
    """A redefined document is a dependency, not a second composition unit."""
    sub = _write(
        tmp_path,
        "sub.xsd",
        f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:t="{REDEFINE_NS}"
            targetNamespace="{REDEFINE_NS}">
          <xs:simpleType name="subject">
            <xs:restriction base="xs:string">
              <xs:enumeration value="1"/><xs:enumeration value="0"/>
            </xs:restriction>
          </xs:simpleType>
        </xs:schema>""",
    )
    principal = _write(
        tmp_path,
        "principal.xsd",
        f"""<xs:schema xmlns:xs="{XSD_NS}" xmlns:t="{REDEFINE_NS}"
            targetNamespace="{REDEFINE_NS}">
          <xs:redefine schemaLocation="sub.xsd">
            <xs:simpleType name="subject">
              <xs:restriction base="t:subject"><xs:enumeration value="1"/></xs:restriction>
            </xs:simpleType>
          </xs:redefine>
          <xs:element name="root" type="t:subject"/>
        </xs:schema>""",
    )
    case = _case(
        tmp_path,
        schemas=(
            _schema("stylesheet-import", "principal.xsd"),
            _schema("secondary", "sub.xsd"),
        ),
    )

    pool = build_schema_pool(case, xsd_version="1.1")

    assert pool is not None
    assert len(pool.raw_schemas) == 1
    subject = pool.get_type(f"{{{REDEFINE_NS}}}subject")
    assert subject is not None
    assert subject.is_valid("1")
    assert not subject.is_valid("0")

    # Composing both documents would repeat the pre-redefinition type; the
    # harness must not ask the engine to accept that contradiction.
    both = [CompiledSchema.compile(principal), CompiledSchema.compile(sub)]
    with pytest.raises(ValueError, match="conflicting type definition"):
        SchemaPool(both, xsd_version="1.1", merge_namespaces=True)


def test_a_secondary_document_keeps_its_own_root(tmp_path: Path) -> None:
    """With no declaration covering its namespace, a secondary document stands alone."""
    _write(tmp_path, "secondary.xsd", _simple_schema(SHOP_NS, "sku"))
    case = _case(tmp_path, schemas=(_schema("secondary", "secondary.xsd"),))

    pool = build_schema_pool(case, xsd_version="1.1")

    assert pool is not None
    assert [schema.target_namespace for schema in pool.raw_schemas] == [SHOP_NS]
    assert pool.get_element(f"{{{SHOP_NS}}}sku") is not None


def test_no_declared_schemas_build_no_pool() -> None:
    assert build_schema_pool(_case(None), xsd_version="1.1") is None


def test_a_missing_schema_file_is_a_catalog_error(tmp_path: Path) -> None:
    case = _case(tmp_path, schemas=(_schema("stylesheet-import", "missing.xsd"),))

    with pytest.raises(CatalogError, match=r"missing\.xsd"):
        build_schema_pool(case, xsd_version="1.1")


def test_a_schema_without_a_file_is_a_catalog_error(tmp_path: Path) -> None:
    declaration = Schema(role="secondary", uri=SHOP_NS, xsd_version="1.0")
    case = _case(tmp_path, schemas=(declaration,))

    with pytest.raises(CatalogError, match="without a file"):
        build_schema_pool(case, xsd_version="1.1")


def test_a_case_without_a_corpus_root_is_a_catalog_error() -> None:
    case = _case(None, schemas=(_schema("stylesheet-import", "money.xsd"),))

    with pytest.raises(CatalogError, match="corpus root"):
        build_schema_pool(case, xsd_version="1.1")


# ---------------------------------------------------------------------------
# Compiled-schema cache


def test_a_schema_compiles_once_per_path_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "money.xsd", _simple_schema(MONEY_NS, "amount"))
    case = _case(tmp_path, schemas=(_schema("stylesheet-import", "money.xsd"),))
    calls = _counting_compiles(monkeypatch)

    first = build_schema_pool(case, xsd_version="1.1")
    second = build_schema_pool(case, xsd_version="1.1")
    other_version = build_schema_pool(
        _case(
            tmp_path,
            schemas=(_schema("stylesheet-import", "money.xsd", xsd_version="1.1"),),
        ),
        xsd_version="1.1",
    )

    assert first is not None and second is not None and other_version is not None
    assert first.raw_schemas[0] is second.raw_schemas[0]
    assert first.raw_schemas[0] is not other_version.raw_schemas[0]
    assert [path for path, _ in calls] == [tmp_path / "money.xsd", tmp_path / "money.xsd"]
    assert [kwargs["xsd_version"] for _, kwargs in calls] == ["1.0", "1.1"]


def test_the_schema_cache_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _counting_compiles(monkeypatch)
    names = []
    for index in range(_COMPILED_SCHEMA_CACHE_SIZE + 1):
        name = f"s{index}.xsd"
        names.append(name)
        _write(tmp_path, name, _simple_schema(f"urn:s{index}", f"t{index}"))
        build_schema_pool(
            _case(tmp_path, schemas=(_schema("secondary", name),)),
            xsd_version="1.1",
        )

    assert len(calls) == _COMPILED_SCHEMA_CACHE_SIZE + 1
    assert len(_COMPILED_SCHEMAS) == _COMPILED_SCHEMA_CACHE_SIZE
    # The oldest entry was evicted, so asking for it compiles again.
    build_schema_pool(_case(tmp_path, schemas=(_schema("secondary", names[0]),)), xsd_version="1.1")
    assert len(calls) == _COMPILED_SCHEMA_CACHE_SIZE + 2


def test_a_resource_policy_reaches_the_schema_compiler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "money.xsd", _simple_schema(MONEY_NS, "amount"))
    calls = _counting_compiles(monkeypatch)
    policy = object()

    build_schema_pool(
        _case(tmp_path, schemas=(_schema("secondary", "money.xsd"),)),
        xsd_version="1.1",
        resource_policy=policy,
    )

    assert [(kwargs["xsd_version"], kwargs["resource_policy"]) for _, kwargs in calls] == [
        ("1.0", policy)
    ]


def test_the_schema_cache_is_keyed_by_the_resource_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A policy can serve different bytes for one path, so it is cache identity."""
    _write(tmp_path, "money.xsd", _simple_schema(MONEY_NS, "amount"))
    case = _case(tmp_path, schemas=(_schema("secondary", "money.xsd"),))
    calls = _counting_compiles(monkeypatch)
    first = object()
    second = object()

    build_schema_pool(case, xsd_version="1.1", resource_policy=first)
    build_schema_pool(case, xsd_version="1.1", resource_policy=first)
    build_schema_pool(case, xsd_version="1.1", resource_policy=second)
    build_schema_pool(case, xsd_version="1.1")

    assert len(calls) == 3
    assert len(_COMPILED_SCHEMAS) == 3


# ---------------------------------------------------------------------------
# Environment fingerprint


def test_the_fingerprint_is_empty_without_declarations() -> None:
    assert environment_fingerprint(_case(None)) == ""


def test_the_fingerprint_distinguishes_schema_sets(tmp_path: Path) -> None:
    money = _case(tmp_path, schemas=(_schema("stylesheet-import", "money.xsd"),))
    shop = _case(tmp_path, schemas=(_schema("stylesheet-import", "shop.xsd"),))
    both = _case(
        tmp_path,
        schemas=(
            _schema("stylesheet-import", "money.xsd"),
            _schema("stylesheet-import", "shop.xsd"),
        ),
    )

    assert environment_fingerprint(money) != environment_fingerprint(shop)
    assert environment_fingerprint(money) != environment_fingerprint(both)
    assert environment_fingerprint(both) != environment_fingerprint(shop)


def test_the_fingerprint_ignores_declaration_order(tmp_path: Path) -> None:
    first = _schema("stylesheet-import", "money.xsd")
    second = _schema("secondary", "shop.xsd", xsd_version="1.1")

    assert environment_fingerprint(_case(tmp_path, schemas=(first, second))) == (
        environment_fingerprint(_case(tmp_path, schemas=(second, first)))
    )


def test_the_fingerprint_tracks_every_schema_version(tmp_path: Path) -> None:
    ten = _case(tmp_path, schemas=(_schema("stylesheet-import", "money.xsd"),))
    eleven = _case(
        tmp_path,
        schemas=(_schema("stylesheet-import", "money.xsd", xsd_version="1.1"),),
    )

    assert environment_fingerprint(ten) != environment_fingerprint(eleven)


def test_the_fingerprint_tracks_schema_roles(tmp_path: Path) -> None:
    """Role decides pool composition, so it is part of the identity."""
    imported = _case(tmp_path, schemas=(_schema("stylesheet-import", "money.xsd"),))
    supporting = _case(tmp_path, schemas=(_schema("secondary", "money.xsd"),))

    assert environment_fingerprint(imported) != environment_fingerprint(supporting)


def test_the_fingerprint_tracks_schema_uris(tmp_path: Path) -> None:
    """A declared namespace URI can select a hinted schema, so it is identity."""
    plain = _case(tmp_path, schemas=(_schema("source-reference", "money.xsd"),))
    hinted = _case(
        tmp_path,
        schemas=(_schema("source-reference", "money.xsd", uri=MONEY_NS),),
    )

    assert environment_fingerprint(plain) != environment_fingerprint(hinted)


def test_the_fingerprint_tracks_packages() -> None:
    library = Package(role="secondary", path=PurePosixPath("lib.xsl"))

    def declared(package: Package) -> str:
        return environment_fingerprint(_case(None, packages=(package,)))

    assert declared(library) != declared(replace(library, package_version="2"))
    assert declared(library) != declared(replace(library, role="principal"))
    assert declared(library) != declared(replace(library, uri="urn:lib"))
    assert declared(library) != declared(replace(library, path=PurePosixPath("other.xsl")))
    assert declared(library) != environment_fingerprint(_case(None))


def test_the_fingerprint_tracks_collections() -> None:
    def declared(sources: tuple[Source, ...], uri: str | None = None) -> str:
        return environment_fingerprint(
            _case(None, collections=(Collection(uri=uri, sources=sources),))
        )

    first = (Source(role="source", path=PurePosixPath("a.xml")),)
    second = (Source(role="source", path=PurePosixPath("b.xml")),)
    inline = (Source(role="source", content="<a/>"),)

    assert declared(first) != declared(second)
    assert declared(first) != declared(first, uri="urn:collection")
    assert declared(inline) != declared((Source(role="source", content="<b/>"),))
    assert declared(first + second) == declared(second + first)


def test_the_fingerprint_is_a_stable_digest(tmp_path: Path) -> None:
    """The fingerprint is a deterministic function of the declarations alone."""
    case = _case(
        tmp_path,
        schemas=(_schema("stylesheet-import", "money.xsd"),),
        packages=(Package(role="secondary", path=PurePosixPath("lib.xsl")),),
        collections=(Collection(uri="urn:c", sources=()),),
    )
    payload = json.dumps(
        [
            [["money.xsd", "stylesheet-import", "", "1.0"]],
            [["lib.xsl", "secondary", "", ""]],
            [["urn:c", []]],
        ],
        separators=(",", ":"),
    )

    assert environment_fingerprint(case) == hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Execution wiring


def _hat_schema(*, maximum: int = 10) -> str:
    return f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{HAT_NS}"
    xmlns:t="{HAT_NS}" elementFormDefault="qualified">
  <xs:simpleType name="hatsize">
    <xs:restriction base="xs:integer">
      <xs:minInclusive value="4"/><xs:maxInclusive value="{maximum}"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:element name="head" type="t:hatsize"/>
</xs:schema>"""


HAT_SCHEMA = _hat_schema()


def _xhtml_schema(element: str, type_name: str) -> str:
    return f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{XHTML_NS}"
    xmlns:h="{XHTML_NS}" elementFormDefault="qualified">
  <xs:complexType name="{type_name}"/>
  <xs:element name="{element}" type="h:{type_name}"/>
</xs:schema>"""


def _hat_sheet(root: Path) -> Path:
    return _write(
        root,
        "sheet.xsl",
        stylesheet(
            '<xsl:template name="xsl:initial-template"><out>'
            '<xsl:value-of select="hat:hatsize(4)"/></out></xsl:template>',
            namespaces={"hat": HAT_NS},
        ).decode("utf-8"),
    )


def test_execute_case_supplies_the_declared_schemas(tmp_path: Path) -> None:
    _write(tmp_path, "hat.xsd", HAT_SCHEMA)
    sheet = _hat_sheet(tmp_path)
    case = _case(tmp_path, schemas=(_schema("stylesheet-import", "hat.xsd"),))

    snapshot = suite.execute_case(case, XSLT30)

    assert snapshot.xml == "<out>4</out>"
    assert len(suite._COMPILED) == 1
    (key,) = suite._COMPILED
    assert key[0] == "xslt30"
    assert key[1] == str(sheet)
    assert key[2] == hashlib.sha256(sheet.read_bytes()).digest()
    assert key[3] == environment_fingerprint(case)


def test_a_stylesheet_without_the_pool_cannot_resolve_its_type(tmp_path: Path) -> None:
    """Without the environment, the constructor is not in scope."""
    _hat_sheet(tmp_path)

    with pytest.raises(XsltStaticError, match="hat:hatsize"):
        XsltProcessor().compile(tmp_path / "sheet.xsl")


def test_a_non_schema_aware_profile_compiles_without_a_pool(tmp_path: Path) -> None:
    _write(tmp_path, "money.xsd", _simple_schema(MONEY_NS, "amount"))
    _write(
        tmp_path,
        "sheet.xsl",
        stylesheet(
            '<xsl:template name="xsl:initial-template"><out>'
            '<xsl:value-of select="1 + 1"/></out></xsl:template>',
            version="1.0",
        ).decode("utf-8"),
    )
    case = _case(tmp_path, schemas=(_schema("stylesheet-import", "money.xsd"),))

    snapshot = suite.execute_case(case, XSLT10)

    assert snapshot.xml == "<out>2</out>"
    (key,) = suite._COMPILED
    assert key[3] == environment_fingerprint(case)


def test_static_parameters_still_receive_the_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, "hat.xsd", HAT_SCHEMA)
    _hat_sheet(tmp_path)
    case = _case(
        tmp_path,
        schemas=(_schema("stylesheet-import", "hat.xsd"),),
        params=(Param(name="flag", select="'yes'", static=True),),
    )
    seen: list[Any] = []
    original = XsltProcessor.compile

    def compile(processor, source, **kwargs):
        seen.append(kwargs.get("schema"))
        return original(processor, source, **kwargs)

    monkeypatch.setattr(XsltProcessor, "compile", compile)

    snapshot = suite.execute_case(case, XSLT30)

    assert snapshot.xml == "<out>4</out>"
    assert isinstance(seen[0], SchemaPool)
    assert suite._COMPILED == {}


def test_swapped_schema_roles_never_share_a_compiled_artifact(tmp_path: Path) -> None:
    """One path set, two roles: the pool differs, so the artifact must too.

    Both schemas target the XHTML namespace, so the secondary filter keeps
    exactly one document: whichever declaration is not ``secondary``.  The
    stylesheet needs an element that only the ``a`` document declares, so
    sharing the artifact would decide the second case with the first case's
    pool instead of failing its compile.
    """
    _write(tmp_path, "xhtml-a.xsd", _xhtml_schema("html", "htmlType"))
    _write(tmp_path, "xhtml-b.xsd", _xhtml_schema("body", "bodyType"))
    _write(
        tmp_path,
        "sheet.xsl",
        stylesheet(
            '<xsl:template match="schema-element(h:html)"><out>typed</out></xsl:template>'
            '<xsl:template match="/"><out>other</out></xsl:template>',
            namespaces={"h": XHTML_NS},
        ).decode("utf-8"),
    )
    source = (Source(role=".", content=f'<html xmlns="{XHTML_NS}"/>'),)
    a_is_imported = _case(
        tmp_path,
        schemas=(
            _schema("stylesheet-import", "xhtml-a.xsd"),
            _schema("secondary", "xhtml-b.xsd"),
        ),
        sources=source,
    )
    b_is_imported = _case(
        tmp_path,
        schemas=(
            _schema("secondary", "xhtml-a.xsd"),
            _schema("stylesheet-import", "xhtml-b.xsd"),
        ),
        sources=source,
    )

    assert environment_fingerprint(a_is_imported) != environment_fingerprint(b_is_imported)
    assert suite.execute_case(a_is_imported, XSLT30).xml == "<out>other</out>"
    with pytest.raises(CaseExecutionError, match="schema-element"):
        suite.execute_case(b_is_imported, XSLT30)
    # The failing case caches nothing, so one artifact exists: the one whose
    # pool names ``h:html``.
    assert len(suite._COMPILED) == 1


# ---------------------------------------------------------------------------
# Source validation


#: A validated element and the same element reached through its name alone are
#: both ``true`` for an element test, because the processor resolves the name
#: through the in-scope schema.  The atomic type annotation is what separates a
#: tree the source declaration validated from the untyped ``strip`` default.
_TYPED_INTEGER = "data(/*) instance of xs:integer"

#: The same probe with the root's name, so a lax accept still shows a document.
_LAX_PROBE = f"concat(local-name(/*), ':', {_TYPED_INTEGER})"


def _result(assertion: str) -> str:
    return f'<result xmlns="{CATALOG_NS}">{assertion}</result>'


def _assertion(xml: str) -> str:
    return _result(f"<assert-xml><![CDATA[{xml}]]></assert-xml>")


def _expected_error(code: str) -> str:
    return _result(f'<error code="{code}"/>')


def _source_case(
    root: Path,
    *,
    sources: tuple[Source, ...],
    schemas: tuple[Schema, ...],
    body: str,
    result: str,
    namespaces: Mapping[str, str] | None = None,
) -> CatalogCase:
    """One catalog-shaped case whose assertion names its expected verdict."""
    _write(root, "sheet.xsl", stylesheet(body, namespaces=namespaces or {}).decode("utf-8"))
    return _case(root, sources=sources, schemas=schemas, result=result)


def _typed_probe() -> str:
    return (
        '<xsl:template match="/"><out>'
        f'<xsl:value-of select="{_TYPED_INTEGER}"/>'
        "</out></xsl:template>"
    )


def _file_source(name: str, validation: str | None = None) -> Source:
    return Source(
        role=".",
        file=name,
        path=PurePosixPath(name),
        validation=validation,
    )


@pytest.mark.parametrize(
    ("declared", "expected"),
    [(None, None), ("skip", None), ("strict", "strict"), ("lax", "lax")],
)
def test_source_validation_names_only_declared_processor_modes(
    declared: str | None,
    expected: str | None,
) -> None:
    """``skip`` asks for no validation, so it names no processor mode."""
    case = _case(None, sources=(Source(role=".", content="<a/>", validation=declared),))

    assert suite.source_validation(case) == expected


def test_source_validation_ignores_a_non_context_source() -> None:
    """Only the ``.`` role reaches the processor as its context source."""
    case = _case(None, sources=(Source(role="source", content="<a/>", validation="strict"),))

    assert suite.source_validation(case) is None


def test_multiple_context_item_sources_remain_an_unsupported_shape() -> None:
    """Two ``.`` sources cannot both be the invocation's context item."""
    case = _case(
        None,
        sources=(
            Source(role=".", content="<a/>"),
            Source(role=".", content="<b/>"),
        ),
    )

    with pytest.raises(suite.UnsupportedCase, match="multiple context-item sources"):
        suite.source_validation(case)


def test_a_context_source_without_a_corpus_root_is_an_adapter_gap() -> None:
    case = _case(None, sources=(Source(role=".", content="<a/>"),))

    result = suite.run_case(case, XSLT30)

    assert result.outcome is Outcome.ADAPTER_GAP
    assert result.message == "case without a corpus root"


def test_raw_source_has_its_own_corpus_root_guard() -> None:
    """``_raw_source`` resolves a file against the root, so it checks it itself.

    ``execute_case`` reaches ``_stylesheet``'s guard first, so this one is only
    reachable through the source helper; it must not rely on the caller.
    """
    source = Source(role=".", content="<a/>")

    with pytest.raises(suite.UnsupportedCase, match="case without a corpus root"):
        suite._raw_source(_case(None, sources=(source,)), source)


def test_a_context_source_without_a_file_or_content_is_an_adapter_gap(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path, sources=(Source(role="."),))

    result = suite.run_case(case, XSLT30)

    assert result.outcome is Outcome.ADAPTER_GAP
    assert result.message == "context source without a file or inline content"


def test_a_strict_source_is_validated_with_the_environment_pool(tmp_path: Path) -> None:
    _write(tmp_path, "hat.xsd", HAT_SCHEMA)
    case = _source_case(
        tmp_path,
        sources=(
            Source(
                role=".",
                content=f'<t:head xmlns:t="{HAT_NS}">4</t:head>',
                validation="strict",
            ),
        ),
        schemas=(_schema("schema", "hat.xsd"),),
        body=_typed_probe(),
        result=_assertion("<out>true</out>"),
    )

    result = suite.run_case(case, XSLT30)

    assert result.outcome is Outcome.PASS


def test_an_invalid_strict_source_reports_the_engine_validation_code(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "hat.xsd", HAT_SCHEMA)
    _write(tmp_path, "head.xml", f'<t:head xmlns:t="{HAT_NS}">12</t:head>')
    case = _source_case(
        tmp_path,
        sources=(_file_source("head.xml", "strict"),),
        schemas=(_schema("schema", "hat.xsd"),),
        body=_typed_probe(),
        result=_expected_error("XTTE1510"),
    )

    result = suite.run_case(case, XSLT30)

    assert result.outcome is Outcome.PASS
    assert result.expected_error_code == result.actual_error_code == "XTTE1510"


def test_a_lax_source_without_a_declaration_is_accepted_untyped(tmp_path: Path) -> None:
    """Lax validation uses the untyped type for an undeclared element."""
    _write(tmp_path, "hat.xsd", HAT_SCHEMA)
    case = _source_case(
        tmp_path,
        sources=(
            Source(
                role=".",
                content=f'<t:other xmlns:t="{HAT_NS}"/>',
                validation="lax",
            ),
        ),
        schemas=(_schema("schema", "hat.xsd"),),
        body=(
            '<xsl:template match="/"><out>'
            f'<xsl:value-of select="{_LAX_PROBE}"/>'
            "</out></xsl:template>"
        ),
        result=_assertion("<out>other:false</out>"),
    )

    result = suite.run_case(case, XSLT30)

    assert result.outcome is Outcome.PASS


def test_a_lax_source_that_violates_a_declaration_is_rejected(tmp_path: Path) -> None:
    """Lax tolerates no declaration, not invalid content under one."""
    _write(tmp_path, "hat.xsd", HAT_SCHEMA)
    case = _source_case(
        tmp_path,
        sources=(
            Source(
                role=".",
                content=f'<t:head xmlns:t="{HAT_NS}">12</t:head>',
                validation="lax",
            ),
        ),
        schemas=(_schema("schema", "hat.xsd"),),
        body=_typed_probe(),
        result=_expected_error("XTTE1515"),
    )

    result = suite.run_case(case, XSLT30)

    assert result.outcome is Outcome.PASS
    assert result.actual_error_code == "XTTE1515"


def _no_namespace_head(root: Path) -> None:
    """Write a no-namespace schema whose ``head`` element carries two facets."""
    _write(
        root,
        "head.xsd",
        f"""<xs:schema xmlns:xs="{XSD_NS}" elementFormDefault="qualified">
  <xs:simpleType name="headSize">
    <xs:restriction base="xs:integer">
      <xs:minInclusive value="4"/><xs:maxInclusive value="10"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:element name="head" type="headSize"/>
</xs:schema>""",
    )


def test_a_no_namespace_instance_validates_by_the_pool_root_name(
    tmp_path: Path,
) -> None:
    """The declared no-namespace root validates by name in the merged pool.

    The engine looks the root's expanded name up in the case's pool; it never
    reads ``xsi:noNamespaceSchemaLocation``.  The instance names the same
    schema the pool holds, so this case proves the pool path works, not that
    an instance hint was fetched.
    """
    _no_namespace_head(tmp_path)
    _write(
        tmp_path,
        "head.xml",
        '<head xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:noNamespaceSchemaLocation="head.xsd">4</head>',
    )
    case = _source_case(
        tmp_path,
        sources=(_file_source("head.xml", "strict"),),
        schemas=(_schema("source-reference", "head.xsd"),),
        body=_typed_probe(),
        result=_assertion("<out>true</out>"),
    )

    result = suite.run_case(case, XSLT30)

    assert result.outcome is Outcome.PASS


def test_an_instance_hint_outside_the_pool_is_not_fetched(tmp_path: Path) -> None:
    """A hinted schema the case did not declare stays unused.

    ``head.xsd`` declares the instance's no-namespace root, but only
    ``hat.xsd`` is in the case's pool.  If the engine fetched the hint the
    strict assessment would succeed; because it validates by pool lookup the
    root has no declaration and the strict code is ``XTTE1512``.
    """
    _no_namespace_head(tmp_path)
    _write(tmp_path, "hat.xsd", HAT_SCHEMA)
    _write(
        tmp_path,
        "head.xml",
        '<head xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:noNamespaceSchemaLocation="head.xsd">4</head>',
    )
    case = _source_case(
        tmp_path,
        sources=(_file_source("head.xml", "strict"),),
        schemas=(_schema("schema", "hat.xsd"),),
        body=_typed_probe(),
        result=_expected_error("XTTE1512"),
    )

    result = suite.run_case(case, XSLT30)

    assert result.outcome is Outcome.PASS
    assert result.actual_error_code == "XTTE1512"


def test_a_source_without_a_validation_attribute_keeps_the_strip_default(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "hat.xsd", HAT_SCHEMA)
    case = _source_case(
        tmp_path,
        sources=(Source(role=".", content=f'<t:head xmlns:t="{HAT_NS}">4</t:head>'),),
        schemas=(_schema("schema", "hat.xsd"),),
        body=_typed_probe(),
        result=_assertion("<out>false</out>"),
    )

    result = suite.run_case(case, XSLT30)

    assert result.outcome is Outcome.PASS


def test_a_selection_from_an_unvalidated_source_stays_untyped(tmp_path: Path) -> None:
    """A selection is evaluated over the parsed source, not re-validated."""
    case = _source_case(
        tmp_path,
        sources=(Source(role=".", content="<doc><head>4</head></doc>", select="doc/head"),),
        schemas=(),
        body=(
            '<xsl:template match="head"><out>'
            '<xsl:value-of select="data(.) instance of xs:integer"/>'
            "</out></xsl:template>"
        ),
        result=_assertion("<out>false</out>"),
    )

    result = suite.run_case(case, XSLT30)

    assert result.outcome is Outcome.PASS


def test_a_validated_source_selection_sees_the_validated_tree(tmp_path: Path) -> None:
    """A declared validation is assessed before the selection is made."""
    _no_namespace_head(tmp_path)
    case = _source_case(
        tmp_path,
        sources=(
            Source(
                role=".",
                content="<head>4</head>",
                validation="strict",
                select="/*",
            ),
        ),
        schemas=(_schema("schema", "head.xsd"),),
        body=(
            '<xsl:template match="head"><out>'
            '<xsl:value-of select="data(.) instance of xs:integer"/>'
            "</out></xsl:template>"
        ),
        result=_assertion("<out>true</out>"),
    )

    result = suite.run_case(case, XSLT30)

    assert result.outcome is Outcome.PASS


@pytest.mark.parametrize(
    ("validation", "instance", "code"),
    [
        ("strict", "<other/>", "XTTE1512"),
        ("strict", "<head>12</head>", "XTTE1510"),
        ("lax", "<head>12</head>", "XTTE1515"),
    ],
)
def test_a_validated_selection_reports_each_assessment_code(
    tmp_path: Path,
    validation: str,
    instance: str,
    code: str,
) -> None:
    """The harness reports the code the same assessment raises inside an invocation."""
    _no_namespace_head(tmp_path)
    case = _source_case(
        tmp_path,
        sources=(
            Source(
                role=".",
                content=instance,
                validation=validation,
                select="/*",
            ),
        ),
        schemas=(_schema("schema", "head.xsd"),),
        body=(
            '<xsl:template match="head"><out>typed</out></xsl:template>'
            '<xsl:template match="*"><out>untyped</out></xsl:template>'
        ),
        result=_expected_error(code),
    )

    result = suite.run_case(case, XSLT30)

    assert result.outcome is Outcome.PASS
    assert result.actual_error_code == code
