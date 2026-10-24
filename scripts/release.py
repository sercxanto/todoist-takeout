"""Checks a release tag against the package version and the changelog, and prints a version's notes.

    python scripts/release.py check v0.1.0    # before tagging; the release workflow runs it too
    python scripts/release.py notes 0.1.0     # the changelog section of 0.1.0, for the GitHub Release

`check` fails if the tag is not `v` followed by `__version__`, if CHANGELOG.md does not have the
structure described in CONTRIBUTING.md (section "Releasing"), or if its newest release is not a
dated, non-empty section for that version. Whether the tagged commit is on main needs Git and is
checked in the release workflow.

Standard library only, so that it runs with any Python 3.11 or newer without installing anything.
The tests import this module for `problems`, `newest_release`, `check` and `notes`.
"""

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = "CHANGELOG.md"
INIT = "src/todoist_takeout/__init__.py"
REPOSITORY = "https://github.com/sercxanto/todoist-takeout"
UNRELEASED = "Unreleased"
# The categories of Keep a Changelog 1.1.0, in its order.
CATEGORIES = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
# The default pattern of Hatchling's version source, which reads the version for the build.
VERSION_ASSIGNMENT = re.compile(r"""^(__version__|VERSION) *= *(['"])v?(?P<version>.+?)\2""", re.MULTILINE | re.I)
# MAJOR.MINOR.PATCH without leading zeros; pre-releases are not used.
VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
HEADING = re.compile(r"## \[(?P<name>[^\]]+)\](?: - (?P<date>.+))?")
LINK_REFERENCE = re.compile(r"\[(?P<label>[^\]]+)\]: (?P<url>\S+)")


@dataclass
class Section:
    """A `## ` section of the changelog; `name` is None if its heading has neither form."""

    heading: str
    name: str | None
    date: str | None
    lines: list[str] = field(default_factory=list)

    @property
    def body(self) -> str:
        return "\n".join(self.lines).strip()

    @property
    def has_entries(self) -> bool:
        return any(line.startswith("- ") for line in self.lines)

    @property
    def is_release(self) -> bool:
        return self.name is not None and self.name != UNRELEASED


def parse(text: str) -> tuple[list[Section], dict[str, str]]:
    """The sections of a changelog in file order, and its link references by label."""
    sections: list[Section] = []
    links: dict[str, str] = {}
    for line in text.splitlines():
        line = line.rstrip()
        if reference := LINK_REFERENCE.fullmatch(line):
            links[reference["label"]] = reference["url"]
        elif line.startswith("## "):
            heading = HEADING.fullmatch(line)
            sections.append(Section(line, heading["name"], heading["date"]) if heading else Section(line, None, None))
        elif sections:
            sections[-1].lines.append(line)
    return sections, links


def is_date(text: str | None) -> bool:
    if text is None or not DATE.fullmatch(text):
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def problems(text: str) -> list[str]:
    """Where a changelog departs from Keep a Changelog with the link references of this repository."""
    sections, links = parse(text)
    found = []
    if not sections or sections[0].name != UNRELEASED:
        found.append(f"the first section must be ## [{UNRELEASED}]")
    releases = []
    for section in sections:
        if section.name is None:
            found.append(f"{section.heading!r} is neither ## [{UNRELEASED}] nor ## [X.Y.Z] - YYYY-MM-DD")
            continue
        if section.name == UNRELEASED:
            if section is not sections[0]:
                found.append(f"## [{UNRELEASED}] must be the first section, and the only one of that name")
        elif not VERSION.fullmatch(section.name):
            found.append(f"{section.heading!r} does not name a version X.Y.Z")
            continue
        elif not is_date(section.date):
            found.append(f"{section.heading!r} has no valid date YYYY-MM-DD")
        else:
            releases.append(section.name)
        for line in section.lines:
            if line.startswith("#") and line.removeprefix("### ") not in CATEGORIES:
                found.append(f"{line!r} in [{section.name}] is not one of ### {', ### '.join(CATEGORIES)}")
    for newer, older in zip(releases, releases[1:], strict=False):
        if version_key(newer) <= version_key(older):
            found.append(f"[{newer}] comes before [{older}], but releases must be newest first")
    expected = {UNRELEASED: f"{REPOSITORY}/compare/v{releases[0]}...HEAD" if releases else f"{REPOSITORY}/commits/main"}
    expected |= {release: f"{REPOSITORY}/releases/tag/v{release}" for release in releases}
    for label, url in expected.items():
        if links.get(label) != url:
            found.append(f"the link reference of [{label}] must be: [{label}]: {url}")
    return found


def newest_release(text: str) -> Section | None:
    return next((section for section in parse(text)[0] if section.is_release), None)


def package_version(root: Path) -> str | None:
    match = VERSION_ASSIGNMENT.search((root / INIT).read_text(encoding="utf-8"))
    return match["version"] if match else None


def check(tag: str, root: Path = ROOT) -> list[str]:
    """What stops `tag` from being released from the files under `root`; empty if nothing does."""
    text = (root / CHANGELOG).read_text(encoding="utf-8")
    found = problems(text)
    version = package_version(root)
    if version is None:
        found.append(f"{INIT} sets no __version__")
    elif tag != f"v{version}":
        found.append(f"the tag {tag} does not match the package version {version}; expected the tag v{version}")
    tagged = tag.removeprefix("v")
    newest = newest_release(text)
    if newest is None or newest.name != tagged:
        newest_name = f"[{newest.name}]" if newest else "none"
        found.append(
            f"{CHANGELOG} has no section ## [{tagged}] - YYYY-MM-DD as its newest release (newest: {newest_name})"
        )
    elif not newest.has_entries:
        found.append(f"the section [{tagged}] of {CHANGELOG} has no entries")
    return found


def notes(version: str, root: Path = ROOT) -> str | None:
    """The body of the changelog section of `version`, without its heading; None if it has no entries."""
    sections, _ = parse((root / CHANGELOG).read_text(encoding="utf-8"))
    section = next((section for section in sections if section.name == version), None)
    return section.body if section and section.has_entries else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Checks a release and prints its notes.")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    check_command = commands.add_parser("check", help="check a tag against the version and the changelog")
    check_command.add_argument("tag", help="the tag to release, such as v0.1.0")
    notes_command = commands.add_parser("notes", help="print the changelog section of a version")
    notes_command.add_argument("version", help="the version, such as 0.1.0")
    args = parser.parse_args(argv)

    if args.command == "check":
        found = check(args.tag, ROOT)
        for problem in found:
            print(f"error: {problem}", file=sys.stderr)
        if not found:
            print(f"{args.tag}: the version and {CHANGELOG} agree")
        return 1 if found else 0

    body = notes(args.version, ROOT)
    if body is None:
        print(f"error: {CHANGELOG} has no section [{args.version}] with entries", file=sys.stderr)
        return 1
    # The changelog is UTF-8, whatever the console's encoding.
    sys.stdout.reconfigure(encoding="utf-8")
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
