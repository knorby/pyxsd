"""Corpus cases that declare collections execute against their declared members.

A collection is a named group of local documents the environment declares; the
executor builds a resolver from those declarations and attaches it to the
per-case processor, so ``collection()`` and ``uri-collection()`` see the
corpus instead of the engine's ``FODC0002``.  Building the mapping reads no
member: a declaration the case never requests cannot fail, and a collection
the case never declared stays unknown so the engine reports its own error.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from urllib.parse import urljoin

import pytest
from tests.xslt import suite
from tests.xslt.assertions import ResultSnapshot
from tests.xslt.model import (
    Collection,
    Environment,
    InitialTemplate,
    Invocation,
    Package,
    Source,
    Stylesheet,
)
from tests.xslt.model import TestCase as CatalogCase
from tests.xslt.runner import CaseExecutionError
from tests.xslt.support import stylesheet

PROFILE = suite.SUITE_PROFILES["xslt30"]

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"

LIBRARY = "urn:example:lib"
PRINCIPAL = "urn:example:principal"


@pytest.fixture(autouse=True)
def _empty_cache():
    """Keep the bounded artifact cache from leaking between tests."""
    suite._COMPILED.clear()
    yield
    suite._COMPILED.clear()


def _write(root: Path, name: str, text: str) -> Path:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _sheet(root: Path, name: str, body: str) -> Path:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(stylesheet(body, declare_xs=False))
    return target


def _output(select: str) -> str:
    """One initial template writing *select*'s value."""
    return f'<xsl:template name="main"><out><xsl:value-of select="{select}"/></out></xsl:template>'


def _member(name: str, uri: str | None = None) -> Source:
    """A collection member naming one corpus file, with its declared URI."""
    return Source(role="source", file=name, path=PurePosixPath(name), uri=uri)


def _case(
    root: Path,
    sheet: str,
    *,
    collections: tuple[Collection, ...] = (),
    packages: tuple[Package, ...] = (),
    environment_packages: tuple[Package, ...] = (),
) -> CatalogCase:
    return CatalogCase(
        name="collection/example",
        local_name="example",
        test_set="collection",
        description="",
        source=PurePosixPath("test-set.xml"),
        environment=Environment(
            collections=collections,
            packages=environment_packages,
        ),
        invocation=Invocation(
            stylesheets=(Stylesheet(role="principal", path=PurePosixPath(sheet)),),
            packages=packages,
            initial_template=InitialTemplate(name="main"),
        ),
        document_root=root,
    )


def _served_value(snapshot: ResultSnapshot) -> str:
    """The value the executor's single ``<out>`` element wraps."""
    return snapshot.xml.removeprefix("<out>").removesuffix("</out>")


def test_a_named_collection_serves_its_declared_members(tmp_path: Path) -> None:
    _write(tmp_path, "a.xml", "<a>1</a>")
    _write(tmp_path, "b.xml", "<b>2</b>")
    _sheet(
        tmp_path,
        "main.xsl",
        _output(
            "concat(count(collection('catalog')), ':', "
            "string-join(collection('catalog')/*/string(), ''))"
        ),
    )
    case = _case(
        tmp_path,
        "main.xsl",
        collections=(
            Collection(
                uri="catalog", sources=(_member("a.xml", "a.xml"), _member("b.xml", "b.xml"))
            ),
        ),
    )

    assert suite.execute_case(case, PROFILE).xml == "<out>2:12</out>"


def test_the_default_collection_serves_an_empty_declaration(tmp_path: Path) -> None:
    _sheet(
        tmp_path, "main.xsl", _output("concat(count(collection()), ':', count(uri-collection()))")
    )
    case = _case(tmp_path, "main.xsl", collections=(Collection(uri=None),))

    assert suite.execute_case(case, PROFILE).xml == "<out>0:0</out>"


def test_an_absent_default_collection_reads_empty_when_a_resolver_is_attached(
    tmp_path: Path,
) -> None:
    """Pin a harness limitation: a resolver cannot report a default as absent.

    The engine distinguishes a missing resolver (``FODC0002``) from a resolver
    that supplies no default items (a legal empty sequence, F&O 3.1 §14.6.3).
    The harness's resolver contract only returns a sequence, so any case that
    declares a collection attaches one, and an undeclared default then reads as
    empty.  This asserts today's behavior; the resolver contract cannot signal
    absence, and the gap is recorded as a follow-up rather than repaired here.
    """
    _write(tmp_path, "a.xml", "<a/>")
    _sheet(tmp_path, "main.xsl", _output("count(collection())"))
    declared = _case(
        tmp_path,
        "main.xsl",
        collections=(Collection(uri="catalog", sources=(_member("a.xml", "a.xml"),)),),
    )

    assert suite.execute_case(declared, PROFILE).xml == "<out>0</out>"

    undeclared = _case(tmp_path, "main.xsl")
    with pytest.raises(CaseExecutionError) as caught:
        suite.execute_case(undeclared, PROFILE)
    assert caught.value.code == "FODC0002"
    assert "no default collection is available" in caught.value.message


def test_a_relative_collection_uri_resolves_against_the_compiled_module(
    tmp_path: Path,
) -> None:
    """The engine resolves ``data`` against ``sub/main.xsl``, not the test-set."""
    _write(tmp_path, "sub/data.xml", "<a/>")
    _sheet(tmp_path, "sub/main.xsl", _output("count(collection('data'))"))
    case = _case(
        tmp_path,
        "sub/main.xsl",
        collections=(Collection(uri="data", sources=(_member("sub/data.xml", "sub/data.xml"),)),),
    )

    assert suite.execute_case(case, PROFILE).xml == "<out>1</out>"


def test_a_fragment_member_is_the_element_its_xml_id_names(tmp_path: Path) -> None:
    _write(tmp_path, "docs.xml", '<doc><a xml:id="frag"/><a xml:id="other"/></doc>')
    _sheet(
        tmp_path,
        "main.xsl",
        _output(
            "concat(count(collection('c')), ':', name(collection('c')[1]), ':', "
            "count(collection('c')[1]/a))"
        ),
    )
    declaration = Collection(uri="c", sources=(_member("docs.xml", "docs.xml#frag"),))
    case = _case(tmp_path, "main.xsl", collections=(declaration,))

    assert suite.execute_case(case, PROFILE).xml == "<out>1:a:0</out>"

    missing = _case(
        tmp_path,
        "main.xsl",
        collections=(Collection(uri="c", sources=(_member("docs.xml", "docs.xml#missing"),)),),
    )

    with pytest.raises(CaseExecutionError) as caught:
        suite.execute_case(missing, PROFILE)
    assert caught.value.code == "FODC0002"


def test_a_collection_member_carries_its_declared_document_uri(tmp_path: Path) -> None:
    _write(tmp_path, "a.xml", "<a/>")
    _sheet(tmp_path, "main.xsl", _output("string-join(uri-collection('c'), '|')"))
    case = _case(
        tmp_path,
        "main.xsl",
        collections=(Collection(uri="c", sources=(_member("a.xml", "renamed/a.xml"),)),),
    )

    value = _served_value(suite.execute_case(case, PROFILE))

    assert value == urljoin(tmp_path.resolve().as_uri() + "/", "renamed/a.xml")


def test_a_member_without_a_declared_uri_is_identified_by_its_file(tmp_path: Path) -> None:
    _write(tmp_path, "a.xml", "<a/>")
    _sheet(tmp_path, "main.xsl", _output("string-join(uri-collection('c'), '|')"))
    case = _case(
        tmp_path,
        "main.xsl",
        collections=(Collection(uri="c", sources=(_member("a.xml"),)),),
    )

    assert (
        _served_value(suite.execute_case(case, PROFILE)) == (tmp_path.resolve() / "a.xml").as_uri()
    )


def test_an_inline_member_without_a_uri_is_served(tmp_path: Path) -> None:
    _sheet(tmp_path, "main.xsl", _output("collection('c')/a"))
    case = _case(
        tmp_path,
        "main.xsl",
        collections=(Collection(uri="c", sources=(Source(role="source", content="<a>7</a>"),)),),
    )

    assert suite.execute_case(case, PROFILE).xml == "<out>7</out>"


def test_an_absolute_collection_uri_is_keyed_verbatim(tmp_path: Path) -> None:
    _write(tmp_path, "a.xml", "<a/>")
    _sheet(tmp_path, "main.xsl", _output("count(collection('urn:example:catalog'))"))
    case = _case(
        tmp_path,
        "main.xsl",
        collections=(Collection(uri="urn:example:catalog", sources=(_member("a.xml", "a.xml"),)),),
    )

    assert suite.execute_case(case, PROFILE).xml == "<out>1</out>"


def test_identical_repeated_declarations_collapse(tmp_path: Path) -> None:
    _write(tmp_path, "a.xml", "<a/>")
    _sheet(tmp_path, "main.xsl", _output("count(collection('c'))"))
    declaration = Collection(uri="c", sources=(_member("a.xml", "a.xml"),))
    case = _case(tmp_path, "main.xsl", collections=(declaration, declaration))

    assert suite.execute_case(case, PROFILE).xml == "<out>1</out>"


def test_a_library_module_resolves_its_collection_against_its_own_directory(
    tmp_path: Path,
) -> None:
    """A library calling ``collection()`` resolves ``data`` against its file.

    The library and its member live in ``lib/``, so the principal directory and
    the test-set directory key ``data`` at the top level only.  Removing the
    library directory from the bases leaves no key for the URI the library's
    module asks for, and the direct assertions below fail before execution.
    """
    _write(
        tmp_path,
        "lib/library.xsl",
        f"""<xsl:package xmlns:xsl="{XSLT_NS}" xmlns:lib="{LIBRARY}" name="{LIBRARY}"
    version="3.0">
  <xsl:function name="lib:members" visibility="public">
    <xsl:sequence select="count(collection('data'))"/>
  </xsl:function>
</xsl:package>""",
    )
    _write(
        tmp_path,
        "principal.xsl",
        f"""<xsl:package xmlns:xsl="{XSLT_NS}" xmlns:lib="{LIBRARY}" name="{PRINCIPAL}"
    exclude-result-prefixes="lib" version="3.0">
  <xsl:use-package name="{LIBRARY}"/>
  <xsl:template name="main" visibility="public">
    <out><xsl:value-of select="lib:members()"/></out>
  </xsl:template>
</xsl:package>""",
    )
    _write(tmp_path, "lib/data.xml", "<a/>")
    case = _case(
        tmp_path,
        "lib/library.xsl",
        collections=(Collection(uri="data", sources=(_member("lib/data.xml", "lib/data.xml"),)),),
        packages=(Package(role="principal", path=PurePosixPath("principal.xsl")),),
        environment_packages=(Package(role="secondary", path=PurePosixPath("lib/library.xsl")),),
    )
    units = suite._compilation_units(case)

    assert suite._collection_bases(case, units) == (
        tmp_path.resolve(),
        (tmp_path / "lib").resolve(),
    )
    resolve = suite._collection_resolver(case, units)
    assert resolve is not None
    assert len(resolve((tmp_path / "lib" / "data").resolve().as_uri())) == 1
    assert suite.execute_case(case, PROFILE).xml == "<out>1</out>"


def test_an_undeclared_collection_is_the_engine_fodc0002(tmp_path: Path) -> None:
    _write(tmp_path, "a.xml", "<a/>")
    _sheet(tmp_path, "main.xsl", _output("count(collection('other'))"))
    case = _case(
        tmp_path,
        "main.xsl",
        collections=(Collection(uri="catalog", sources=(_member("a.xml", "a.xml"),)),),
    )

    with pytest.raises(CaseExecutionError) as caught:
        suite.execute_case(case, PROFILE)

    assert caught.value.code == "FODC0002"
    assert "other" in caught.value.message
    assert "not available to this transformation" in caught.value.message


def _unsupported_collections(shape: str, root: Path) -> tuple[Collection, ...]:
    """One declaration shape the harness must refuse, built over *root*."""
    if shape == "named-empty":
        return (Collection(uri="catalog"),)
    if shape == "remote":
        return (
            Collection(
                uri="catalog",
                sources=(Source(role="source", uri="https://example.test/a.xml"),),
            ),
        )
    if shape == "parent":
        return (Collection(uri="catalog", sources=(_member("../escape.xml", "../escape.xml"),)),)
    if shape == "absolute":
        return (Collection(uri="catalog", sources=(_member("/outside/escape.xml"),)),)
    if shape == "symlink":
        outside = root.parent / f"{root.name}-outside.xml"
        outside.write_text("<a/>", encoding="utf-8")
        (root / "escape.xml").symlink_to(outside)
        return (Collection(uri="catalog", sources=(_member("escape.xml", "escape.xml"),)),)
    if shape == "ambiguous":
        return (
            Collection(uri="catalog", sources=(_member("a.xml", "a.xml"),)),
            Collection(uri="catalog", sources=(_member("b.xml", "b.xml"),)),
        )
    raise AssertionError(f"unknown shape {shape!r}")


@pytest.mark.parametrize(
    ("shape", "reason"),
    [
        ("named-empty", "named collection without sources"),
        ("remote", "collection source without a file or inline content"),
        ("parent", "collection source outside the corpus root"),
        ("absolute", "collection source outside the corpus root"),
        ("symlink", "collection source outside the corpus root"),
        ("ambiguous", "ambiguous collection"),
    ],
)
def test_collections_the_harness_cannot_serve_are_unsupported(
    tmp_path: Path,
    shape: str,
    reason: str,
) -> None:
    _write(tmp_path, "a.xml", "<a/>")
    _write(tmp_path, "b.xml", "<b/>")
    _sheet(tmp_path, "main.xsl", _output("count(collection('catalog'))"))
    case = _case(tmp_path, "main.xsl", collections=_unsupported_collections(shape, tmp_path))

    with pytest.raises(suite.UnsupportedCase, match=reason):
        suite.execute_case(case, PROFILE)


def test_a_rewritten_collection_member_is_not_shared_with_another_case(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "a.xml", "<a>1</a>")
    _sheet(tmp_path, "main.xsl", _output("collection('c')/a"))
    case = _case(
        tmp_path,
        "main.xsl",
        collections=(Collection(uri="c", sources=(_member("a.xml", "a.xml"),)),),
    )

    assert suite.execute_case(case, PROFILE).xml == "<out>1</out>"
    _write(tmp_path, "a.xml", "<a>2</a>")
    assert suite.execute_case(case, PROFILE).xml == "<out>2</out>"
    assert suite._COMPILED == {}


def test_a_collection_member_obeys_the_stylesheet_space_rules(
    tmp_path: Path,
) -> None:
    """A resolver member is stripped or preserved by the calling package.

    The same member text is selected for stripping in one stylesheet and left
    alone in the other, so neither answer can come from the parser's default
    whitespace handling.  This is the discriminating check that
    ``collection-005`` only approximated before the member path applied the
    package's declarations.
    """
    _write(tmp_path, "docs.xml", "<doc><a> </a><b> </b></doc>")
    _sheet(
        tmp_path,
        "stripped.xsl",
        '<xsl:strip-space elements="a"/><xsl:preserve-space elements="b"/>'
        '<xsl:template name="main"><out>'
        "<xsl:value-of select=\"string-length(collection('docs')//a)\"/>"
        "<xsl:value-of select=\"string-length(collection('docs')//b)\"/>"
        "</out></xsl:template>",
    )
    _sheet(
        tmp_path,
        "preserved.xsl",
        '<xsl:template name="main"><out>'
        "<xsl:value-of select=\"string-length(collection('docs')//a)\"/>"
        "<xsl:value-of select=\"string-length(collection('docs')//b)\"/>"
        "</out></xsl:template>",
    )
    collections = (Collection(uri="docs", sources=(_member("docs.xml", "docs.xml"),)),)

    stripped = _case(tmp_path, "stripped.xsl", collections=collections)
    preserved = _case(tmp_path, "preserved.xsl", collections=collections)

    assert suite.execute_case(stripped, PROFILE).xml == "<out>01</out>"
    assert suite.execute_case(preserved, PROFILE).xml == "<out>11</out>"


def test_an_unused_collection_declaration_does_not_stop_the_case(tmp_path: Path) -> None:
    """A declaration is built without I/O, so a missing member is never read."""
    _sheet(tmp_path, "main.xsl", '<xsl:template name="main"><out>ok</out></xsl:template>')
    case = _case(
        tmp_path,
        "main.xsl",
        collections=(Collection(uri="catalog", sources=(_member("missing.xml", "missing.xml"),)),),
    )

    assert suite.execute_case(case, PROFILE).xml == "<out>ok</out>"


def test_the_resolver_parses_a_member_only_when_it_is_requested(tmp_path: Path) -> None:
    _sheet(tmp_path, "main.xsl", _output("count(collection('urn:example:catalog'))"))
    case = _case(
        tmp_path,
        "main.xsl",
        collections=(
            Collection(
                uri="urn:example:catalog",
                sources=(_member("missing.xml", "missing.xml"),),
            ),
        ),
    )
    resolve = suite._collection_resolver(case, suite._compilation_units(case))

    assert resolve is not None
    assert resolve("urn:example:other") == ()
    with pytest.raises(FileNotFoundError):
        resolve("urn:example:catalog")


def test_the_resolver_parses_a_member_once(tmp_path: Path) -> None:
    _write(tmp_path, "a.xml", "<a/>")
    _sheet(tmp_path, "main.xsl", _output("count(collection('urn:example:catalog'))"))
    declaration = Collection(
        uri="urn:example:catalog",
        sources=(_member("a.xml", "a.xml"),),
    )
    case = _case(tmp_path, "main.xsl", collections=(declaration,))
    resolve = suite._collection_resolver(case, suite._compilation_units(case))

    assert resolve is not None
    first = resolve("urn:example:catalog")

    assert first is resolve("urn:example:catalog")
    assert first[0].owner_document_uri == urljoin(tmp_path.resolve().as_uri() + "/", "a.xml")
