"""Rendered documentation contracts; install the docs group to run these tests."""

import os
import re
import runpy
import subprocess
import sys
from html import unescape
from pathlib import Path

import pytest

pytest.importorskip("sphinx")

ROOT = Path(__file__).resolve().parents[1]


def build_fixture_site(tmp_path, *, extra_conf="", body=""):
    """Build an offline fixture with this project's copy/reference settings."""
    conf = runpy.run_path(str(ROOT / "docs/conf.py"))
    names = [name for name in conf if name.startswith("copybutton_") or name == "nitpicky"]
    extensions = ["myst_parser"]
    if "sphinx_copybutton" in conf["extensions"]:
        extensions.append("sphinx_copybutton")
    (tmp_path / "conf.py").write_text(
        f"extensions = {extensions!r}\nmaster_doc = 'index'\n"
        + "".join(f"{name} = {conf[name]!r}\n" for name in names)
        + extra_conf,
        encoding="utf-8",
    )
    (tmp_path / "index.md").write_text(body, encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "sphinx",
            "-W",
            "--keep-going",
            "-b",
            "html",
            str(tmp_path),
            str(tmp_path / "_build"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


@pytest.fixture(scope="session")
def build(tmp_path_factory):
    path = tmp_path_factory.mktemp("sphinx-copy")
    result = build_fixture_site(
        path,
        body="""# Copy examples

```python
if True:
    print("hello")
```

```console
$ uv run pytest \\
  tests/test_docs_examples.py
```

```xml
<note>hello</note>
```

```{code-block} text
:class: no-copy

expected output only
```
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return path / "_build"


@pytest.fixture(scope="session")
def docs_build():
    path = os.environ.get("PYXSD_DOCS_BUILD")
    if not path:
        pytest.skip("set PYXSD_DOCS_BUILD to a freshly built HTML directory")
    result = Path(path).resolve()
    assert (result / "index.html").is_file()
    return result


def test_copy_buttons_attached_to_runnable_blocks_only(build):
    html = (build / "index.html").read_text(encoding="utf-8")
    assert "_static/copybutton.js" in html
    script = (build / "_static/copybutton.js").read_text(encoding="utf-8")
    assert "div:not(.no-copy) > div.highlight > pre" in script
    assert html.count('class="highlight"') == 4


def test_console_prompts_marked_for_stripping(build):
    html = (build / "index.html").read_text(encoding="utf-8")
    assert '<span class="gp">$ </span>' in html
    script = (build / "_static/copybutton.js").read_text(encoding="utf-8")
    assert "true, false, true, true" in script  # regexp, all lines, strip, keep blanks


def test_no_copy_block_has_no_button(build):
    html = (build / "index.html").read_text(encoding="utf-8")
    container = html.split("no-copy", 1)[1].split("</div>", 1)[0]
    assert "expected output only" in container
    assert "copybtn" not in container


def test_real_site_copy_buttons(docs_build):
    html = (docs_build / "demo.html").read_text(encoding="utf-8")
    assert "_static/copybutton.js" in html
    container = html.split("no-copy", 1)[1].split("</div>", 1)[0]
    assert "color: 2" in container
    assert "copybtn" not in container


def test_nitpicky_build_fails_on_unknown_target(tmp_path):
    result = build_fixture_site(tmp_path, body="See {py:func}`no_such_function`.\n")
    assert result.returncode != 0
    assert "no_such_function" in result.stdout + result.stderr


def test_known_target_resolves(tmp_path):
    result = build_fixture_site(
        tmp_path,
        body="""# API

```{py:function} documented_function()
A documented function.
```

See {py:func}`documented_function`.
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_conf_has_no_regex_suppression():
    conf = runpy.run_path(str(ROOT / "docs/conf.py"))
    assert conf.get("nitpicky") is True
    assert not conf.get("nitpick_ignore_regex")


def test_api_summary_links_resolve_to_unique_anchors(docs_build):
    html = (docs_build / "api.html").read_text(encoding="utf-8")
    tables = re.findall(r'<table[^>]*class="[^"]*autosummary[^"]*".*?</table>', html, re.S)
    assert len(tables) == 2
    for cls, methods in (
        ("pyxsd.schema.Schema", ["compile", "parse", "iter_parse", "stub", "require_valid"]),
        (
            "pyxsd.document.Document",
            [
                "find",
                "findall",
                "xpath",
                "to_dict",
                "to_json",
                "to_string",
                "transform",
                "revalidate",
                "write",
                "require_valid",
            ],
        ),
    ):
        for name in methods:
            anchor = f"{cls}.{name}"
            assert html.count(f'id="{anchor}"') == 1, anchor
            assert f'href="#{anchor}"' in "".join(tables), anchor


def test_real_site_channel_banner(docs_build):
    from pyxsd import __version__

    for path in docs_build.rglob("*.html"):
        html = path.read_text(encoding="utf-8")
        banners = re.findall(r'<p class="docs-channel">(.*?)</p>', html, re.S)
        assert len(banners) == 1, path
        text = " ".join(unescape(re.sub(r"<[^>]*>", "", banners[0])).split())
        assert f"Development documentation (develop) · package {__version__}" in text, path
        target = (
            "#release-specific-sources"
            if path == docs_build / "index.html"
            else "index.html#release-specific-sources"
        )
        assert f'{target}"' in banners[0], path
        assert "_static/docs.css" in html, path


def test_channel_configuration_is_explicit():
    conf = runpy.run_path(str(ROOT / "docs/conf.py"))
    assert conf.get("html_context", {}).get("docs_channel") == "develop"
