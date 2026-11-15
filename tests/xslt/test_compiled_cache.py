"""Compiled stylesheets are reused across cases that share one artifact.

The bulk conformance sets compile a single stylesheet for hundreds of
consecutive cases.  Compilation depends only on the stylesheet closure and the
profile when a case declares no static parameters, so the executor keeps a
small per-worker cache instead of rebuilding the same artifact per case.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

import pytest
from tests.xslt import suite
from tests.xslt.model import Environment, Invocation, Param, Stylesheet
from tests.xslt.model import TestCase as CatalogCase
from tests.xslt.support import stylesheet

from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltStaticError

PROFILE = suite.SUITE_PROFILES["xslt30"]


@dataclass
class _OtherProfile:
    """A profile identity that is not the checked-in one, for the cache key."""

    name: str
    processor: XsltProcessor


class _Recorder:
    """Count compilation calls while delegating to the real processor."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[Path, object]] = []
        original = XsltProcessor.compile

        def compile(processor, source, **kwargs):
            self.calls.append((Path(str(source)), kwargs.get("static_parameters")))
            return original(processor, source, **kwargs)

        monkeypatch.setattr(XsltProcessor, "compile", compile)

    def __len__(self) -> int:
        return len(self.calls)


@pytest.fixture(autouse=True)
def _empty_cache():
    suite._COMPILED.clear()
    yield
    suite._COMPILED.clear()


@pytest.fixture
def sheet(tmp_path: Path) -> Path:
    target = tmp_path / "sheet.xsl"
    target.write_bytes(
        stylesheet(
            '<xsl:template name="xsl:initial-template"><out>'
            '<xsl:value-of select="1 + 1"/></out></xsl:template>'
        )
    )
    return target


def _case(root: Path, params: tuple[Param, ...] = ()) -> CatalogCase:
    return CatalogCase(
        name="t/t",
        local_name="t",
        test_set="t",
        description="",
        source=PurePosixPath("t"),
        environment=Environment(),
        invocation=Invocation(
            stylesheets=(Stylesheet(role="principal", path=PurePosixPath("sheet.xsl")),),
            params=params,
        ),
        document_root=root,
    )


def test_one_stylesheet_compiles_once_per_profile(
    monkeypatch: pytest.MonkeyPatch, sheet: Path
) -> None:
    recorder = _Recorder(monkeypatch)
    first = suite._compile_stylesheet(PROFILE, sheet, {})
    second = suite._compile_stylesheet(PROFILE, sheet, {})
    assert first is second
    assert len(recorder) == 1
    assert recorder.calls == [(sheet, None)]


def test_profile_identity_is_part_of_the_key(monkeypatch: pytest.MonkeyPatch, sheet: Path) -> None:
    recorder = _Recorder(monkeypatch)
    other = cast(suite.SuiteProfile, _OtherProfile(name="xslt20", processor=PROFILE.processor))
    suite._compile_stylesheet(PROFILE, sheet, {})
    suite._compile_stylesheet(other, sheet, {})
    assert len(recorder) == 2
    assert [path for path, _ in recorder.calls] == [sheet, sheet]


def test_distinct_stylesheets_are_distinct_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = _Recorder(monkeypatch)
    first = tmp_path / "a.xsl"
    second = tmp_path / "b.xsl"
    for target in (first, second):
        target.write_bytes(
            stylesheet('<xsl:template name="xsl:initial-template"><out/></xsl:template>')
        )
    suite._compile_stylesheet(PROFILE, first, {})
    suite._compile_stylesheet(PROFILE, second, {})
    suite._compile_stylesheet(PROFILE, first, {})
    assert len(recorder) == 2


def test_the_cache_is_bounded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    recorder = _Recorder(monkeypatch)
    source = stylesheet('<xsl:template name="xsl:initial-template"><out/></xsl:template>')
    paths = []
    for index in range(suite._COMPILED_CACHE_SIZE + 1):
        target = tmp_path / f"{index}.xsl"
        target.write_bytes(source)
        paths.append(target)
        suite._compile_stylesheet(PROFILE, target, {})
    assert len(recorder) == suite._COMPILED_CACHE_SIZE + 1
    assert len(suite._COMPILED) == suite._COMPILED_CACHE_SIZE
    # The oldest entry was evicted, so asking for it compiles again.
    suite._compile_stylesheet(PROFILE, paths[0], {})
    assert len(recorder) == suite._COMPILED_CACHE_SIZE + 2


def test_static_parameters_bypass_the_cache(monkeypatch: pytest.MonkeyPatch, sheet: Path) -> None:
    recorder = _Recorder(monkeypatch)
    suite._compile_stylesheet(PROFILE, sheet, {"flag": ("yes",)})
    suite._compile_stylesheet(PROFILE, sheet, {"flag": ("yes",)})
    assert len(recorder) == 2
    assert recorder.calls == [(sheet, {"flag": ("yes",)}), (sheet, {"flag": ("yes",)})]
    assert suite._COMPILED == {}


def test_a_reused_artifact_transforms_each_case(
    monkeypatch: pytest.MonkeyPatch, sheet: Path
) -> None:
    recorder = _Recorder(monkeypatch)
    snapshot = suite.execute_case(_case(sheet.parent), PROFILE)
    assert snapshot.xml == "<out>2</out>"
    again = suite.execute_case(_case(sheet.parent), PROFILE)
    assert again == snapshot
    assert len(recorder) == 1
    assert len(suite._COMPILED) == 1


def test_static_parameter_cases_still_compile(sheet: Path) -> None:
    case = _case(sheet.parent, params=(Param(name="flag", select="'yes'", static=True),))
    snapshot = suite.execute_case(case, PROFILE)
    assert snapshot.xml == "<out>2</out>"
    assert suite._COMPILED == {}


def test_rewritten_stylesheet_is_compiled_again(
    monkeypatch: pytest.MonkeyPatch, sheet: Path
) -> None:
    recorder = _Recorder(monkeypatch)
    suite._compile_stylesheet(PROFILE, sheet, {})
    sheet.write_bytes(
        stylesheet(
            '<xsl:template name="xsl:initial-template"><out>'
            '<xsl:value-of select="2 + 2"/></out></xsl:template>'
        )
    )
    compiled = suite._compile_stylesheet(PROFILE, sheet, {})
    assert len(recorder) == 2
    assert compiled.transform().principal_tree.string_value == "4"


def test_an_unreadable_stylesheet_fails_without_caching(sheet: Path) -> None:
    missing = sheet.parent / "missing.xsl"
    with pytest.raises(XsltStaticError):
        suite._compile_stylesheet(PROFILE, missing, {})
    assert suite._COMPILED == {}
