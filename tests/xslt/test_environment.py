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
    params: tuple[Param, ...] = (),
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
        ),
        invocation=Invocation(
            stylesheets=(Stylesheet(role="principal", path=PurePosixPath("sheet.xsl")),),
            params=params,
        ),
        document_root=root,
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
            [["money.xsd", "1.0"]],
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
