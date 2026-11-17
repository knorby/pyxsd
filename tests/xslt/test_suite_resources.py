"""Corpus cases that declare resources execute against their declared files.

A ``<resource>`` declaration names a file the ``unparsed-text`` family may read
under a URI the stylesheet chooses; it is never a refusal in itself.  The
executor builds a read-only policy from the declarations and attaches it to
both resource-policy fields of the per-case processor, so a declared ``http:``
URI is served from its declared local fixture and everything undeclared keeps
the engine's local-file behaviour.  Nothing is fetched: a text read of a
remote URI the corpus ships no fixture for is a harness gap, and a declaration
the stylesheet never reads cannot fail.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest
from tests.xslt import suite
from tests.xslt.model import Environment, InitialTemplate, Invocation, Resource, Stylesheet
from tests.xslt.model import TestCase as CatalogCase
from tests.xslt.outcomes import Outcome
from tests.xslt.runner import CaseExecutionError
from tests.xslt.support import stylesheet

PROFILE = suite.SUITE_PROFILES["xslt30"]

MISSION_URI = "https://example.test/mission.html"


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


def _write_bytes(root: Path, name: str, raw: bytes) -> Path:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return target


def _sheet(root: Path, name: str, body: str) -> Path:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(stylesheet(body, declare_xs=False))
    return target


def _output(select: str) -> str:
    """One initial template writing *select*'s value."""
    return f'<xsl:template name="main"><out><xsl:value-of select="{select}"/></out></xsl:template>'


def _resource(**overrides: object) -> Resource:
    """One resource declaration naming a local corpus file."""
    fields: dict[str, object] = {
        "role": "resource",
        "file": "r.txt",
        "path": PurePosixPath("r.txt"),
        "uri": "r.txt",
    }
    fields.update(overrides)
    return Resource(**fields)  # type: ignore[arg-type]


def _case(
    root: Path,
    sheet: str,
    *,
    resources: tuple[Resource, ...] = (),
) -> CatalogCase:
    return CatalogCase(
        name="resource/example",
        local_name="example",
        test_set="resource",
        description="",
        source=PurePosixPath("test-set.xml"),
        environment=Environment(resources=resources),
        invocation=Invocation(
            stylesheets=(Stylesheet(role="principal", path=PurePosixPath(sheet)),),
            initial_template=InitialTemplate(name="main"),
        ),
        document_root=root,
    )


def test_a_declared_local_resource_is_available_to_unparsed_text(tmp_path: Path) -> None:
    # The URI the stylesheet asks for is not the file's location, and the
    # stylesheet sits in a subdirectory: only a mapping registered under every
    # module directory, keyed by URI rather than by path, can serve the read.
    _write(tmp_path, "data/hello.txt", "hello")
    _sheet(tmp_path, "sub/main.xsl", _output("unparsed-text('renamed/hello.txt')"))
    case = _case(
        tmp_path,
        "sub/main.xsl",
        resources=(
            _resource(
                file="data/hello.txt",
                path=PurePosixPath("data/hello.txt"),
                uri="renamed/hello.txt",
                media_type="text/plain",
            ),
        ),
    )

    assert suite.execute_case(case, PROFILE).xml == "<out>hello</out>"


def test_a_declared_encoding_is_honoured(tmp_path: Path) -> None:
    # The policy API carries no encoding channel, so the declaration is the
    # corpus's statement of the bytes' encoding: the policy decodes with it and
    # serves UTF-8, which is what the engine's one-argument read decodes with.
    _write_bytes(tmp_path, "data/wide.txt", "Kinder, Küche".encode("utf-16"))
    _sheet(tmp_path, "main.xsl", _output("unparsed-text('wide.txt')"))
    case = _case(
        tmp_path,
        "main.xsl",
        resources=(
            _resource(
                file="data/wide.txt",
                path=PurePosixPath("data/wide.txt"),
                uri="wide.txt",
                encoding="utf-16",
            ),
        ),
    )

    assert suite.execute_case(case, PROFILE).xml == "<out>Kinder, Küche</out>"

    bogus = replace(
        case,
        environment=replace(
            case.environment,
            resources=(
                _resource(
                    file="data/wide.txt",
                    path=PurePosixPath("data/wide.txt"),
                    uri="wide.txt",
                    encoding="no-such-codec",
                ),
            ),
        ),
    )
    with pytest.raises(suite.UnsupportedCase, match="is not decodable as no-such-codec"):
        suite.execute_case(bogus, PROFILE)


def test_a_declared_remote_resource_is_served_from_its_local_fixture(
    tmp_path: Path,
) -> None:
    # A declared http: URI is never offered to the application policy alone:
    # the engine refuses a remote scheme before consulting it, so the same
    # object must fill the remote field too.  No DNS or HTTP is attempted; a
    # regression that fetches would fail or hang this test.
    _write(tmp_path, "fixtures/mission.html", "mission text")
    _sheet(tmp_path, "main.xsl", _output(f"unparsed-text('{MISSION_URI}')"))
    case = _case(
        tmp_path,
        "main.xsl",
        resources=(
            _resource(
                file="fixtures/mission.html",
                path=PurePosixPath("fixtures/mission.html"),
                uri=MISSION_URI,
                media_type="text/html",
            ),
        ),
    )

    assert suite.execute_case(case, PROFILE).xml == "<out>mission text</out>"


@pytest.mark.parametrize(
    ("resources", "reason"),
    [
        pytest.param(
            (
                _resource(
                    file=MISSION_URI,
                    path=None,
                    uri=MISSION_URI,
                    media_type="text/html",
                ),
            ),
            "without a local fixture",
            id="remote-without-fixture",
        ),
        pytest.param(
            (Resource(role="resource"),),
            "resource without a file or URI",
            id="neither-file-nor-uri",
        ),
    ],
)
def test_a_text_resource_without_a_local_fixture_is_a_harness_gap(
    tmp_path: Path, resources: tuple[Resource, ...], reason: str
) -> None:
    _sheet(tmp_path, "main.xsl", _output(f"unparsed-text('{MISSION_URI}')"))
    case = _case(tmp_path, "main.xsl", resources=resources)

    with pytest.raises(suite.UnsupportedCase, match=re.escape(reason)):
        suite.execute_case(case, PROFILE)
    result = suite.run_case(case, PROFILE)
    assert result.outcome is Outcome.ADAPTER_GAP
    assert reason in result.message


def test_resource_declarations_outside_the_corpus_are_refused(tmp_path: Path) -> None:
    # A declared path is clamped inside the corpus root before it is read, and
    # two declarations for one URI with different bytes cannot both be true.
    (tmp_path.parent / "escape.txt").write_text("outside", encoding="utf-8")
    _write(tmp_path, "one.txt", "one")
    _write(tmp_path, "two.txt", "two")
    _sheet(tmp_path, "escape.xsl", _output("unparsed-text('escape.txt')"))
    _sheet(tmp_path, "dup.xsl", _output("unparsed-text('dup.txt')"))
    escape_case = _case(
        tmp_path,
        "escape.xsl",
        resources=(
            _resource(
                file="../escape.txt",
                path=PurePosixPath("../escape.txt"),
                uri="escape.txt",
            ),
        ),
    )
    dup_case = _case(
        tmp_path,
        "dup.xsl",
        resources=(
            _resource(file="one.txt", path=PurePosixPath("one.txt"), uri="dup.txt"),
            _resource(file="two.txt", path=PurePosixPath("two.txt"), uri="dup.txt"),
        ),
    )

    with pytest.raises(suite.UnsupportedCase, match="resource path outside the corpus root"):
        suite.execute_case(escape_case, PROFILE)
    with pytest.raises(suite.UnsupportedCase, match="ambiguous resource") as excinfo:
        suite.execute_case(dup_case, PROFILE)
    assert "dup.txt" in str(excinfo.value)


def test_a_declaration_the_stylesheet_never_reads_does_not_block_it(
    tmp_path: Path,
) -> None:
    # A declaration is only a key until a stylesheet asks for it, whatever its
    # media type or whether it names a fixture at all; two declarations that
    # agree collapse, and one keyed by its path alone is as inert as the rest.
    _write(tmp_path, "external.dtd", "<!ELEMENT doc ANY>")
    _write(tmp_path, "module.xqm", "module namespace m = 'urn:m'")
    _write(tmp_path, "same.txt", "same")
    _sheet(tmp_path, "main.xsl", _output("'ok'"))
    case = _case(
        tmp_path,
        "main.xsl",
        resources=(
            _resource(
                file=MISSION_URI,
                path=None,
                uri=MISSION_URI,
                media_type="text/html",
            ),
            _resource(
                file="external.dtd",
                path=PurePosixPath("external.dtd"),
                uri="external.dtd",
                media_type="application/xml-dtd",
            ),
            _resource(
                file="module.xqm",
                path=PurePosixPath("module.xqm"),
                uri="module.xqm",
                media_type="application/xquery",
            ),
            _resource(file="silent.txt", path=PurePosixPath("silent.txt"), uri=None),
            _resource(file="same.txt", path=PurePosixPath("same.txt"), uri="same.txt"),
            _resource(file="same.txt", path=PurePosixPath("same.txt"), uri="same.txt"),
        ),
    )

    assert suite.execute_case(case, PROFILE).xml == "<out>ok</out>"


def test_a_resource_environment_is_never_shared_through_the_compile_cache(
    tmp_path: Path,
) -> None:
    # One stylesheet, two cases, two fixtures: a cached artifact would bind
    # the second case to the first case's policy and answer `one` twice.
    _write(tmp_path, "a.txt", "one")
    _write(tmp_path, "b.txt", "two")
    _sheet(tmp_path, "main.xsl", _output("unparsed-text('r.txt')"))
    first = _case(
        tmp_path,
        "main.xsl",
        resources=(_resource(file="a.txt", path=PurePosixPath("a.txt"), uri="r.txt"),),
    )
    second = _case(
        tmp_path,
        "main.xsl",
        resources=(_resource(file="b.txt", path=PurePosixPath("b.txt"), uri="r.txt"),),
    )

    assert suite.execute_case(first, PROFILE).xml == "<out>one</out>"
    assert suite.execute_case(second, PROFILE).xml == "<out>two</out>"
    assert suite._COMPILED == {}


def test_undeclared_reads_keep_the_engine_local_behaviour(tmp_path: Path) -> None:
    # With a policy attached, a read the declarations do not name still falls
    # through to the local file default: an existing local file is served, a
    # missing one is the engine's own FOUT1170, and an undeclared remote
    # target is a policy denial, not a declared non-goal.
    _write(tmp_path, "r.txt", "declared")
    _write(tmp_path, "other.txt", "local fallback")
    _sheet(tmp_path, "local.xsl", _output("unparsed-text('other.txt')"))
    _sheet(tmp_path, "missing.xsl", _output("unparsed-text('missing.txt')"))
    _sheet(tmp_path, "remote.xsl", _output("unparsed-text('https://example.test/absent.txt')"))
    environment = (_resource(),)
    local = _case(tmp_path, "local.xsl", resources=environment)
    missing = _case(tmp_path, "missing.xsl", resources=environment)
    remote = _case(tmp_path, "remote.xsl", resources=environment)

    assert suite.execute_case(local, PROFILE).xml == "<out>local fallback</out>"
    with pytest.raises(CaseExecutionError) as excinfo:
        suite.execute_case(missing, PROFILE)
    assert excinfo.value.code == "FOUT1170"
    result = suite.run_case(remote, PROFILE)
    assert result.outcome is Outcome.FAIL
    assert "ResourcePolicyError" in result.message


def test_the_local_path_helper_accepts_only_absolute_local_names() -> None:
    assert suite._local_path("relative.txt") is None
    assert suite._local_path("/tmp/absolute.txt") == Path("/tmp/absolute.txt")


def test_a_non_canonical_file_uri_is_not_a_local_path(tmp_path: Path) -> None:
    # A canonical file URI has no authority, no query, and an absolute path;
    # anything else stays a refusal, and nothing relative is ever dereferenced.
    _write(tmp_path, "r.txt", "declared")
    environment = (_resource(),)
    for uri in (
        "file://example.test/authority.txt",
        "file:///query.txt?allowed=no",
        "file:relative.txt",
    ):
        _sheet(tmp_path, "non-canonical.xsl", _output(f"unparsed-text('{uri}')"))
        case = _case(tmp_path, "non-canonical.xsl", resources=environment)

        result = suite.run_case(case, PROFILE)
        assert result.outcome is Outcome.FAIL
        assert "ResourcePolicyError" in result.message, uri
