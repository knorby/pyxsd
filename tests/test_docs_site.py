"""Deterministic HTML link checks, including offline and path-boundary cases."""

import pytest
from tests.docs_site import LinkProblem, check_links, check_metadata, main, parse_page

BASE = "https://pyxsd.knorby.com/"


def write_page(root, name, body="", *, canonical=None, description="example"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    href = BASE + name if canonical is None else canonical
    path.write_text(
        f'<meta name="description" content="{description}">'
        f'<link rel="canonical" href="{href}">{body}',
        encoding="utf-8",
    )
    return path


def test_parse_page():
    page = parse_page(
        '<a id="first" href="a.html#x">A</a><a name="legacy"></a>'
        '<meta name="description"><link rel="canonical">'
    )
    assert page.ids == {"first", "legacy"}
    assert page.links == ["a.html#x"]
    assert page.descriptions == [None]
    assert page.canonicals == [None]


@pytest.mark.parametrize(
    ("href", "reason"),
    [
        ("api.html#target", None),
        ("gone.html", "missing file"),
        ("#home", None),
        ("#missing", "missing anchor"),
        ("api.html#missing", "missing anchor"),
        ("sub/", None),
        ("special%20name.html#c%2B%2B-mode", None),
        ("api.html?q=1#target", None),
        ("/api.html#target", None),
        (BASE + "api.html#target", None),
        ("//pyxsd.knorby.com/api.html#target", None),
        ("image.svg#whatever", None),
        ("https://elsewhere.invalid/gone", None),
        ("mailto:a@example.invalid", None),
        ("tel:123", None),
        ("javascript:void(0)", None),
        ("", None),
        ("../escape.html", "escapes build root"),
        ("%2e%2e/escape.html", "escapes build root"),
    ],
)
def test_link_resolution(tmp_path, href, reason):
    write_page(tmp_path, "index.html", f'<i id="home"></i><a href="{href}">link</a>')
    write_page(tmp_path, "api.html", '<i id="target"></i>')
    write_page(tmp_path, "special name.html", '<i id="c++-mode"></i>')
    write_page(tmp_path, "sub/index.html")
    (tmp_path / "image.svg").write_text("<svg/>", encoding="utf-8")
    want = [] if reason is None else [LinkProblem("index.html", href, reason)]
    assert check_links(tmp_path, base_url=BASE) == want


def test_nested_and_symlink_paths(tmp_path):
    root = tmp_path / "site"
    write_page(root, "index.html")
    write_page(
        root,
        "sub/page.html",
        '<a href="../index.html">ok</a>'
        '<a href="../../escape.html">bad</a><a href="../outside.html">bad</a>',
    )
    outside = write_page(tmp_path, "outside.html")
    (root / "outside.html").symlink_to(outside)
    assert check_links(root, base_url=BASE) == [
        LinkProblem("sub/page.html", "../../escape.html", "escapes build root"),
        LinkProblem("sub/page.html", "../outside.html", "escapes build root"),
    ]


def test_base_url_subpath(tmp_path):
    write_page(
        tmp_path,
        "index.html",
        '<a href="/docs/api.html#target">root</a>'
        '<a href="https://example.invalid/docs/api.html?x#target">full</a>'
        '<a href="https://example.invalid/elsewhere.html">outside docs</a>',
    )
    write_page(tmp_path, "api.html", '<i id="target"></i>')
    assert check_links(tmp_path, base_url="https://example.invalid/docs/") == []


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        ("", "missing canonical"),
        ('<link rel="canonical" href="wrong">', "incorrect canonical"),
        ('<link rel="canonical" href=""><link rel="canonical" href="">', "duplicate canonical"),
        ('<link rel="canonical">', "incorrect canonical"),
    ],
)
def test_canonical_problems(tmp_path, metadata, reason):
    (tmp_path / "index.html").write_text(metadata, encoding="utf-8")
    assert check_metadata(tmp_path, base_url=BASE, entry_pages=()) == [
        LinkProblem("index.html", BASE + "index.html", reason)
    ]


@pytest.mark.parametrize(
    "metadata",
    [
        "",
        '<meta name="description">',
        '<meta name="description" content=" ">',
        '<meta name="description" content="a"><meta name="description" content="b">',
    ],
)
def test_description_problems(tmp_path, metadata):
    (tmp_path / "index.html").write_text(
        f'<link rel="canonical" href="{BASE}index.html">{metadata}', encoding="utf-8"
    )
    assert check_metadata(tmp_path, base_url=BASE, entry_pages=("index.html",)) == [
        LinkProblem("index.html", "description", "expected one non-empty description")
    ]


def test_valid_metadata_and_missing_entry(tmp_path):
    write_page(tmp_path, "index.html")
    write_page(tmp_path, "sub/page.html")
    assert check_metadata(tmp_path, base_url=BASE, entry_pages=("index.html",)) == []
    assert check_metadata(tmp_path, base_url=BASE, entry_pages=("missing.html",)) == [
        LinkProblem("missing.html", "description", "missing entry page")
    ]


def test_cli_exit_codes(tmp_path, capsys):
    assert main([str(tmp_path / "missing"), "--base-url", BASE]) == 2
    assert "not a directory" in capsys.readouterr().err
    assert main([str(tmp_path), "--base-url", BASE]) == 2
    assert "no HTML" in capsys.readouterr().err
    for name in (
        "index",
        "quickstart",
        "api",
        "validation",
        "integrations",
        "mcp",
        "schema-resolution",
    ):
        write_page(tmp_path, name + ".html")
    assert main([str(tmp_path), "--base-url", BASE]) == 0
    assert "7 pages, 0 problems" in capsys.readouterr().out
    write_page(tmp_path, "index.html", '<a href="gone.html">bad</a>')
    assert main([str(tmp_path), "--base-url", BASE]) == 1
    assert "index.html: gone.html (missing file)" in capsys.readouterr().out
