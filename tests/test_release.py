"""The changelog and the checks before a release (scripts/release.py)."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from todoist_takeout import __version__
from todoist_takeout.sqlite_view import SOURCE_URL

ROOT = Path(__file__).parent.parent
REPOSITORY = "https://github.com/sercxanto/todoist-takeout"

RELEASED = f"""# Changelog

Preamble.

## [Unreleased]

### Fixed

- A fix that is not released yet.

## [0.2.0] - 2026-12-01

### Added

- A new option.

### Fixed

- A fix.

## [0.1.0] - 2026-11-01

### Added

- The first release.

[Unreleased]: {REPOSITORY}/compare/v0.2.0...HEAD
[0.2.0]: {REPOSITORY}/releases/tag/v0.2.0
[0.1.0]: {REPOSITORY}/releases/tag/v0.1.0
"""

UNRELEASED_ONLY = f"""# Changelog

## [Unreleased]

### Added

- Everything.

[Unreleased]: {REPOSITORY}/commits/main
"""


def write_release(root: Path, changelog: str, version: str, newline: str = "\n") -> Path:
    (root / "CHANGELOG.md").write_bytes(changelog.replace("\n", newline).encode())
    init = root / "src" / "todoist_takeout" / "__init__.py"
    init.parent.mkdir(parents=True)
    init.write_bytes(f'"""Docstring."""{newline}{newline}__version__ = "{version}"{newline}'.encode())
    return root


def test_the_changelog_has_the_structure_of_keep_a_changelog(release):
    assert release.problems((ROOT / "CHANGELOG.md").read_text(encoding="utf-8")) == []


def test_the_newest_release_of_the_changelog_is_the_package_version(release):
    newest = release.newest_release((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    # Before the first release, any version is accepted.
    if newest is not None:
        assert newest.name == __version__


@pytest.mark.parametrize("changelog", [RELEASED, UNRELEASED_ONLY], ids=["released", "unreleased only"])
def test_a_well_formed_changelog_has_no_problems(release, changelog):
    assert release.problems(changelog) == []


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (
            lambda text: text.replace("## [Unreleased]\n\n### Fixed\n\n- A fix that is not released yet.\n\n", ""),
            "first section",
        ),
        (
            lambda text: text.replace(
                "## [Unreleased]\n\n### Fixed\n\n- A fix that is not released yet.\n\n", ""
            ).replace("## [0.1.0]", "## [Unreleased]\n\n## [0.1.0]"),
            "first section",
        ),
        (lambda text: text.replace("## [0.2.0] - 2026-12-01", "## [0.2.0]"), "no valid date"),
        (lambda text: text.replace("2026-12-01", "2026-13-01"), "no valid date"),
        (lambda text: text.replace("2026-12-01", "1 December 2026"), "no valid date"),
        (lambda text: text.replace("## [0.2.0]", "## [0.2]"), "does not name a version"),
        (lambda text: text.replace("## [0.2.0]", "## 0.2.0"), "is neither"),
        (lambda text: text.replace("[0.2.0] - 2026-12-01", "[0.0.9] - 2026-12-01"), "newest first"),
        (lambda text: text.replace("### Fixed\n\n- A fix.", "### Improved\n\n- A fix."), "'### Improved' in [0.2.0]"),
        (lambda text: text.replace("### Added\n\n- A new", "#### Added\n\n- A new"), "'#### Added' in [0.2.0]"),
        (lambda text: text.replace(f"[0.1.0]: {REPOSITORY}/releases/tag/v0.1.0\n", ""), "link reference of [0.1.0]"),
        (lambda text: text.replace("compare/v0.2.0...HEAD", "commits/main"), "link reference of [Unreleased]"),
    ],
    ids=[
        "unreleased missing",
        "unreleased below a release",
        "date missing",
        "invalid date",
        "date in another format",
        "two-part version",
        "heading without brackets",
        "wrong order",
        "unknown category",
        "category on the wrong level",
        "link reference missing",
        "unreleased link not moved on",
    ],
)
def test_a_changelog_that_breaks_the_structure_is_reported(release, change, problem):
    changelog = change(RELEASED)
    assert changelog != RELEASED

    found = release.problems(changelog)

    assert any(problem in message for message in found), found


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["LF", "CRLF"])
def test_check_accepts_a_tag_that_agrees_with_the_version_and_the_changelog(release, tmp_path, newline):
    write_release(tmp_path, RELEASED, "0.2.0", newline)

    assert release.check("v0.2.0", tmp_path) == []


def test_check_names_both_versions_if_the_tag_does_not_match(release, tmp_path):
    write_release(tmp_path, RELEASED, "0.2.0")

    found = release.check("v0.2.1", tmp_path)

    assert any("v0.2.1" in message and "0.2.0" in message for message in found), found
    assert any("no section ## [0.2.1]" in message for message in found), found


def test_check_requires_a_changelog_section_for_the_version(release, tmp_path):
    write_release(tmp_path, UNRELEASED_ONLY, "0.1.0")

    assert release.check("v0.1.0", tmp_path) == [
        "CHANGELOG.md has no section ## [0.1.0] - YYYY-MM-DD as its newest release (newest: none)"
    ]


def test_check_requires_the_version_to_be_the_newest_release(release, tmp_path):
    write_release(tmp_path, RELEASED, "0.1.0")

    found = release.check("v0.1.0", tmp_path)

    assert found == ["CHANGELOG.md has no section ## [0.1.0] - YYYY-MM-DD as its newest release (newest: [0.2.0])"]


def test_check_requires_entries_in_the_section(release, tmp_path):
    write_release(tmp_path, RELEASED.replace("### Added\n\n- A new option.\n\n### Fixed\n\n- A fix.\n", ""), "0.2.0")

    assert release.check("v0.2.0", tmp_path) == ["the section [0.2.0] of CHANGELOG.md has no entries"]


def test_check_reports_the_problems_of_the_changelog(release, tmp_path):
    write_release(tmp_path, RELEASED.replace("## [0.1.0] - 2026-11-01", "## [0.1.0]"), "0.2.0")

    assert release.check("v0.2.0", tmp_path) == ["'## [0.1.0]' has no valid date YYYY-MM-DD"]


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["LF", "CRLF"])
def test_notes_are_the_section_without_its_heading_and_the_link_references(release, tmp_path, newline):
    write_release(tmp_path, RELEASED, "0.2.0", newline)

    assert release.notes("0.2.0", tmp_path) == "### Added\n\n- A new option.\n\n### Fixed\n\n- A fix."
    assert release.notes("0.1.0", tmp_path) == "### Added\n\n- The first release."


def test_notes_of_a_missing_or_empty_section_are_none(release, tmp_path):
    write_release(tmp_path, RELEASED.replace("- The first release.\n", ""), "0.2.0")

    assert release.notes("0.3.0", tmp_path) is None
    assert release.notes("0.1.0", tmp_path) is None


def test_the_command_line_exits_1_with_the_problems(release, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(release, "ROOT", write_release(tmp_path, RELEASED, "0.2.0"))

    assert release.main(["check", "v0.2.0"]) == 0
    assert release.main(["check", "v0.3.0"]) == 1
    assert "error: the tag v0.3.0 does not match the package version 0.2.0" in capsys.readouterr().err
    assert release.main(["notes", "0.1.0"]) == 0
    assert capsys.readouterr().out == "### Added\n\n- The first release.\n"
    assert release.main(["notes", "0.3.0"]) == 1


def test_the_pypi_description_is_the_readme_with_its_links_moved_to_the_tag(tmp_path):
    rendered = tmp_path / "description.md"
    # The plugin's command line sets the version to 42.0. It reads and writes in the locale's
    # encoding, which on Windows cannot hold the README's arrows, so UTF-8 mode is switched on.
    subprocess.run(
        [sys.executable, "-m", "hatch_fancy_pypi_readme", "pyproject.toml", "-o", str(rendered)],
        cwd=ROOT,
        env={**os.environ, "PYTHONUTF8": "1"},
        check=True,
    )

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    expected, moved = re.subn(rf"{re.escape(SOURCE_URL)}/(blob|raw)/main/", rf"{SOURCE_URL}/\1/v42.0/", readme)
    assert moved > 0, "README.md has no links into the repository; is SOURCE_URL still right?"
    # The command line ends its output with a newline of its own.
    assert rendered.read_text(encoding="utf-8").rstrip("\n") == expected.rstrip("\n")
