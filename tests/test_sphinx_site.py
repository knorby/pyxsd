"""Rendered documentation contracts; install the docs group to run these tests."""

import os
import runpy
import subprocess
import sys
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
