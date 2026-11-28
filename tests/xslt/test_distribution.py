"""Opt-in offline installation checks for the built XSLT distributions.

Run with ``PYXSD_DIST_DIR`` pointing at a fresh ``uv build`` output directory;
without it the archive checks skip because they install the declared archives
rather than the checkout.  Each archive is installed into a fresh virtual
environment from the local uv cache with network access disabled, and a smoke
script copied outside the checkout verifies imports, packaged schema
resources, transformation, and serialization.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.xslt import distribution_smoke
from tests.xslt.support import run_probe

from pyxsd import __version__

REPO_ROOT = Path(__file__).resolve().parents[2]
DIST_ENVIRONMENT = "PYXSD_DIST_DIR"
UNROUTABLE = "http://127.0.0.1:9"
ARCHIVES = {
    "sdist": f"pyxsd-{__version__}.tar.gz",
    "wheel": f"pyxsd-{__version__}-py3-none-any.whl",
}


def offline_environment() -> dict[str, str]:
    """The process environment with every route to a package index removed."""
    environment = dict(os.environ)
    for name in ("PYTHONHOME", "VIRTUAL_ENV"):
        environment.pop(name, None)
    environment.update(
        {
            "ALL_PROXY": UNROUTABLE,
            "HTTP_PROXY": UNROUTABLE,
            "HTTPS_PROXY": UNROUTABLE,
            "UV_LINK_MODE": "copy",
            "UV_OFFLINE": "1",
            "UV_PYTHON_DOWNLOADS": "never",
            "all_proxy": UNROUTABLE,
            "http_proxy": UNROUTABLE,
            "https_proxy": UNROUTABLE,
            "no_proxy": "",
            "PYTHONPATH": "",
        }
    )
    return environment


def _venv_python(root: Path) -> Path:
    """The interpreter path inside a virtual environment rooted at *root*."""
    scripts = sysconfig.get_path("scripts", vars={"base": str(root), "platbase": str(root)})
    return Path(scripts) / f"python{sysconfig.get_config_var('EXE') or ''}"


def _probe(
    command: list[str], *, cwd: Path, environment: dict[str, str], timeout: int
) -> subprocess.CompletedProcess[str]:
    """Run a deadline-bounded command and report its captured output on failure."""
    try:
        return run_probe(command, timeout=timeout, cwd=cwd, env=environment)
    except subprocess.CalledProcessError as error:
        pytest.fail(
            f"{' '.join(command)} failed with exit {error.returncode}:\n"
            f"{error.stdout}\n{error.stderr}"
        )


@pytest.mark.skipif(
    DIST_ENVIRONMENT not in os.environ,
    reason=f"set {DIST_ENVIRONMENT} to freshly built distributions",
)
@pytest.mark.parametrize("archive", sorted(ARCHIVES))
def test_archive_installs_offline_and_runs_the_xslt_smoke(tmp_path: Path, archive: str) -> None:
    artifact = Path(os.environ[DIST_ENVIRONMENT]).resolve() / ARCHIVES[archive]
    assert artifact.is_file(), f"missing built archive {artifact}"
    uv = shutil.which("uv")
    assert uv is not None, "installing a built archive offline requires uv"
    environment = offline_environment()
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / archive
    _probe(
        [uv, "venv", "--python", sys.executable, str(root)],
        timeout=180,
        cwd=outside,
        environment=environment,
    )
    _probe(
        [uv, "pip", "install", "--python", str(_venv_python(root)), "--offline", str(artifact)],
        timeout=300,
        cwd=outside,
        environment=environment,
    )
    smoke = outside / "distribution_smoke.py"
    shutil.copyfile(distribution_smoke.__file__, smoke)
    completed = _probe(
        [str(_venv_python(root)), "-I", str(smoke), str(root), str(REPO_ROOT)],
        timeout=180,
        cwd=outside,
        environment=environment,
    )
    assert f"pyxsd {__version__} imported from {root}" in completed.stdout


def test_offline_probe_surfaces_captured_failure_output(tmp_path: Path) -> None:
    with pytest.raises(pytest.fail.Exception, match="failed with exit 3"):
        _probe(
            [sys.executable, "-c", "raise SystemExit(3)"],
            cwd=tmp_path,
            environment=dict(os.environ),
            timeout=30,
        )


def test_smoke_fixture_requires_two_paths(capsys: pytest.CaptureFixture[str]) -> None:
    assert distribution_smoke.main(["distribution_smoke.py"]) == 2
    assert "usage:" in capsys.readouterr().err


def test_smoke_fixture_rejects_a_package_outside_the_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = tmp_path / "environment"
    assert distribution_smoke.main(["smoke", str(environment), str(tmp_path)]) == 1
    assert "outside" in capsys.readouterr().err


def test_smoke_fixture_detects_a_module_from_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leaked = tmp_path / "leaked_module.py"
    leaked.write_text("", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "leaked_module", SimpleNamespace(__file__=str(leaked)))
    assert distribution_smoke.leaked_modules(tmp_path) == ["leaked_module"]
    assert distribution_smoke.leaked_modules(tmp_path / "elsewhere") == []


def test_smoke_fixture_reports_modules_loaded_from_the_checkout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(distribution_smoke, "leaked_modules", lambda checkout: ["leaked"])
    assert distribution_smoke.main(["smoke", str(REPO_ROOT), str(tmp_path)]) == 1
    assert "modules imported from the checkout: leaked" in capsys.readouterr().err


def test_smoke_fixture_rejects_the_wrong_inspection_profile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "pyxsd.xslt.inspect_stylesheet",
        lambda source, *, language: SimpleNamespace(language="1.0"),
    )
    assert distribution_smoke.main(["smoke", str(REPO_ROOT), str(tmp_path)]) == 1
    assert "inspection selected" in capsys.readouterr().err


def test_smoke_fixture_rejects_a_different_serialized_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(distribution_smoke, "EXPECTED", "<different/>")
    assert distribution_smoke.main(["smoke", str(REPO_ROOT), str(tmp_path)]) == 1
    assert "serialized" in capsys.readouterr().err


def test_smoke_fixture_accepts_a_checkout_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert distribution_smoke.main(["smoke", str(REPO_ROOT), str(tmp_path)]) == 0
    assert f"pyxsd {__version__} imported from" in capsys.readouterr().out
