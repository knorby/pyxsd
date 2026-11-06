"""Check built documentation links and metadata without network access.

Run ``python -m tests.docs_site BUILD_DIR --base-url https://host/docs/``.
Only links within that site's base path are checked; external links belong to
Sphinx's separate linkcheck build. HTML fragments are checked, asset fragments
are not. Symlinks cannot take a checked link outside the build directory.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ENTRY_PAGES = (
    "index.html",
    "quickstart.html",
    "api.html",
    "validation.html",
    "integrations.html",
    "mcp.html",
    "schema-resolution.html",
)


@dataclass(frozen=True)
class LinkProblem:
    page: str
    target: str
    reason: str


@dataclass
class Page:
    ids: set[str] = field(default_factory=set)
    links: list[str] = field(default_factory=list)
    descriptions: list[str | None] = field(default_factory=list)
    canonicals: list[str | None] = field(default_factory=list)


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.page = Page()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.page.ids.add(attrs["id"])
        if tag == "a":
            if "name" in attrs:
                self.page.ids.add(attrs["name"])
            if "href" in attrs:
                self.page.links.append(attrs["href"] or "")
        if tag == "meta" and attrs.get("name") == "description":
            self.page.descriptions.append(attrs.get("content"))
        if tag == "link" and "canonical" in attrs.get("rel", "").split():
            self.page.canonicals.append(attrs.get("href"))


def parse_page(source: str) -> Page:
    parser = _Parser()
    parser.feed(source)
    return parser.page


def _load(build_dir: Path) -> tuple[Path, dict[Path, Page]]:
    root = build_dir.resolve()
    if not root.is_dir():
        raise ValueError(f"not a directory: {build_dir}")
    pages = {
        path.resolve(): parse_page(path.read_text(encoding="utf-8"))
        for path in sorted(root.rglob("*.html"))
        if path.resolve().is_relative_to(root)
    }
    if not pages:
        raise ValueError(f"no HTML files: {build_dir}")
    return root, pages


def _links(root: Path, pages: dict[Path, Page], base_url: str) -> list[LinkProblem]:
    base = urlsplit(base_url)
    prefix = unquote(base.path).rstrip("/") + "/"
    problems = []
    for path, page in pages.items():
        for href in page.links:
            url = urlsplit(href)
            if url.scheme and url.scheme not in {"http", "https"}:
                continue
            if url.netloc and url.netloc.lower() != base.netloc.lower():
                continue
            name = unquote(url.path)
            if name.startswith("/") or url.netloc:
                if not name.startswith(prefix):
                    continue  # Same host, but outside this documentation site.
                target = root / name[len(prefix) :]
            elif name:
                target = path.parent / name
            else:
                target = path
            target = target.resolve()
            if target.is_dir():
                target = (target / "index.html").resolve()
            reason = None
            if not target.is_relative_to(root):
                reason = "escapes build root"
            elif not target.is_file():
                reason = "missing file"
            elif (
                url.fragment and target in pages and unquote(url.fragment) not in pages[target].ids
            ):
                reason = "missing anchor"
            if reason:
                problems.append(LinkProblem(path.relative_to(root).as_posix(), href, reason))
    return problems


def check_links(build_dir: Path, *, base_url: str) -> list[LinkProblem]:
    root, pages = _load(build_dir)
    return sorted(_links(root, pages, base_url), key=lambda p: (p.page, p.target, p.reason))


def _metadata(root, pages, base_url, entry_pages):
    problems = []
    for path, page in pages.items():
        name = path.relative_to(root).as_posix()
        canonical = base_url.rstrip("/") + "/" + name
        reason = None
        if not page.canonicals:
            reason = "missing canonical"
        elif len(page.canonicals) != 1:
            reason = "duplicate canonical"
        elif page.canonicals[0] != canonical:
            reason = "incorrect canonical"
        if reason:
            problems.append(LinkProblem(name, canonical, reason))
    for name in entry_pages:
        page = pages.get(root / name)
        if page is None:
            problems.append(LinkProblem(name, "description", "missing entry page"))
        elif len(page.descriptions) != 1 or not (page.descriptions[0] or "").strip():
            problems.append(LinkProblem(name, "description", "expected one non-empty description"))
    return problems


def check_metadata(
    build_dir: Path, *, base_url: str, entry_pages: Sequence[str]
) -> list[LinkProblem]:
    root, pages = _load(build_dir)
    return sorted(
        _metadata(root, pages, base_url, entry_pages), key=lambda p: (p.page, p.target, p.reason)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build_dir", type=Path)
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args(argv)
    try:
        root, pages = _load(args.build_dir)
        problems = _links(root, pages, args.base_url)
        problems += _metadata(root, pages, args.base_url, ENTRY_PAGES)
    except (ValueError, OSError) as exc:
        print(f"docs-site: {exc}", file=sys.stderr)
        return 2
    for problem in sorted(problems, key=lambda p: (p.page, p.target, p.reason)):
        print(f"{problem.page}: {problem.target} ({problem.reason})")
    print(f"{len(pages)} pages, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
