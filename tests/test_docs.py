"""The links of the user documentation.

The README is also the project description on PyPI, where relative links and in-page anchors lead
nowhere, so it links only to absolute URLs. Its links into this repository, and the relative links
of the pages under docs/, must name files and headings that exist.
"""

import re
import tomllib
from pathlib import Path

import pytest

from todoist_takeout.sqlite_view import SOURCE_URL

ROOT = Path(__file__).parent.parent
README = ROOT / "README.md"
DOCS = sorted((ROOT / "docs").glob("*.md"))
# The prefixes of links to a file of this repository, on the main branch.
REPOSITORY_FILES = (f"{SOURCE_URL}/blob/main/", f"{SOURCE_URL}/raw/main/")

# Inline links and images: [text](target) and ![alt](target). Code is left out first, since a
# shell snippet or a table cell can hold brackets.
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]*)\)")
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
OTHER_LINK_FORMS = re.compile(r"<a\s|<img\s|^\[[^\]]+\]:\s", re.IGNORECASE | re.MULTILINE)


def prose(path: Path) -> str:
    return CODE.sub("", path.read_text(encoding="utf-8"))


def links(path: Path) -> list[str]:
    return LINK.findall(prose(path))


def anchors(path: Path) -> set[str]:
    """The IDs GitHub gives the headings of a Markdown file."""
    headings = re.findall(r"^#+\s+(.+?)\s*$", prose(path), re.MULTILINE)
    return {re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-") for heading in headings}


def check_target(path: Path, target: Path, anchor: str) -> None:
    assert target.is_file(), f"{path.name} links to {target.relative_to(ROOT)}, which does not exist"
    if anchor:
        assert anchor in anchors(target), f"{path.name} links to #{anchor}, which is not a heading of {target.name}"


@pytest.mark.parametrize("path", [README, *DOCS], ids=lambda p: p.name)
def test_links_use_only_inline_markdown(path):
    assert links(path), f"{path.name} has no links; is the pattern still right?"
    assert not OTHER_LINK_FORMS.search(prose(path)), f"{path.name} uses a link form the tests do not check"


def test_readme_links_are_absolute():
    for link in links(README):
        assert link.startswith(("https://", "mailto:")), f"README.md links to {link!r}, which PyPI cannot resolve"


def test_readme_links_into_the_repository_name_existing_files():
    checked = 0
    for link in links(README):
        prefix = next((p for p in REPOSITORY_FILES if link.startswith(p)), None)
        if prefix is None:
            continue
        file, _, anchor = link.removeprefix(prefix).partition("#")
        check_target(README, ROOT / file, anchor)
        checked += 1
    assert checked, "README.md has no links into the repository; is SOURCE_URL still right?"


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_relative_links_of_the_docs_resolve(path):
    for link in links(path):
        if re.match(r"[a-z]+:", link):
            continue
        file, _, anchor = link.partition("#")
        check_target(path, (path.parent / file).resolve() if file else path, anchor)


def test_project_urls_point_to_the_repository():
    urls = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["urls"]
    assert urls
    for name, url in urls.items():
        assert url.startswith(SOURCE_URL), f"[project.urls] {name} is {url}"
    documentation = urls["Documentation"].partition("#")[2]
    assert documentation in anchors(README)
