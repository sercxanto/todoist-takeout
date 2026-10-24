# Proposal

> Status: the scope was settled with the user in conversation (2026-10-24).

## Why

The repository is public, CI runs on its own, and every part of the backup works, but there is no release: users install from a Git URL, PyPI has no package, and nothing records what changed between versions. A release workflow and a changelog are the two missing pieces before a first version, and they depend on each other, since every release needs its changelog section.

## What Changes

- **Changelog:** a hand-written `CHANGELOG.md` at the repository root in the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format, with an `Unreleased` section at the top and one section per release. It is written for users; the archived OpenSpec changes remain the developer record. An offline test checks its structure.
- **Versioning:** [Semantic Versioning](https://semver.org), starting at 0.1.0. The version stays in `src/todoist_takeout/__init__.py`; the release commit sets it and dates the changelog section.
- **Release workflow:** a new `.github/workflows/release.yml`, started by pushing a tag `v<version>`. It checks that the tag, `__version__` and the changelog agree, builds the sdist and the wheel once, runs the full test matrix and a smoke test of the built wheel on Linux, macOS and Windows, publishes to TestPyPI, waits for the maintainer's approval, publishes to PyPI through Trusted Publishing (no API token is stored anywhere), and creates a GitHub Release with the changelog section as its notes and the built files attached.
- **A release helper:** `scripts/release.py`, standard library only, checks the tag against the version and the changelog, and extracts a version's section as release notes. CI and the maintainer use the same checks.
- **PyPI description:** the README's links to files of the repository point to `main` on GitHub, but on PyPI they point to the release tag, so the description of each version shows the documentation of that version. `hatch-fancy-pypi-readme`, a build-time plugin, rewrites them; the README in the repository is unchanged.
- **README:** badges for CI, the PyPI version and the supported Python versions; installation from PyPI with `uv tool install todoist-takeout`, and `pipx install todoist-takeout` alongside with `pipx run datasette` as the counterpart of `uvx datasette`; the "not on PyPI yet" note and the `Status` section go.
- **Package metadata:** classifiers for the tested Python versions, which the Python-versions badge reads, and a `Changelog` URL in `[project.urls]`. `CHANGELOG.md` goes into the sdist.
- **Contributor guidance:** `CONTRIBUTING.md` describes how to cut a release, the local dry run (`uv build`, `twine check`) and the emergency path of publishing from a workstation with a short-lived token. `AGENTS.md` and `openspec/config.yaml` ask every change that users can notice to add an entry under `Unreleased`.
- **Manual setup by the maintainer:** pending trusted publishers on PyPI and TestPyPI, and the GitHub environments `testpypi` and `pypi`, the latter with a required reviewer; both accept deployments from tags `v*` only. The rulesets for `main` and for tags `v*` already exist.
- `dev/ideas.md`: the changelog and release-workflow ideas point to this change.

Out of scope: publishing the first release itself (it follows once this change is merged), pre-releases such as `0.2.0rc1`, generating the changelog from commits or fragments, and extending the CI matrix to Python versions after 3.13.

## Capabilities

### New Capabilities

- `release`: how a version of todoist-takeout is cut and published: the version and tag rules, the changelog, the checks before publishing, the approval gate, the publication to PyPI and the GitHub Release, and what the PyPI description links to.

### Modified Capabilities

None. The tool's behaviour is unchanged; `--version` (`backup-cli`) keeps printing `__version__`.

## Impact

- New files: `CHANGELOG.md`, `.github/workflows/release.yml`, `scripts/release.py`, `tests/test_release.py`.
- `pyproject.toml`: `readme` becomes dynamic through `hatch-fancy-pypi-readme` in `build-system.requires` (build time only, not installed with the package); the plugin also becomes a development dependency for the test of the PyPI description; Python classifiers; `Changelog` URL; `CHANGELOG.md` in the sdist. No runtime dependency.
- `.github/workflows/tests.yml` becomes callable from the release workflow (`workflow_call`).
- Documentation: `README.md`, `CONTRIBUTING.md`, `AGENTS.md`, `dev/ideas.md`; `openspec/config.yaml` gets a tasks rule.
- External: the package name `todoist-takeout` on PyPI and TestPyPI (free on 2026-10-24), two GitHub environments, two trusted publishers.
- No production code changes.
