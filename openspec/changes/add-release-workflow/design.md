# Design

## Context

See proposal.md for the motivation and specs/release/spec.md for the requirements. The current state:

- `src/todoist_takeout/__init__.py` holds `__version__ = "0.1.0"`; Hatchling reads it (`[tool.hatch.version]`), and `todoist-takeout --version` prints it. Nothing has been released and no tag exists. The package name is free on PyPI and TestPyPI (checked 2026-10-24).
- `pyproject.toml` sets `readme = "README.md"`. The README links only to absolute URLs, files of the repository as `https://github.com/sercxanto/todoist-takeout/blob/main/<path>` and the screenshot as `.../raw/main/<path>`; `tests/test_docs.py` enforces this. The archived change `simplify-readme` (D3) rejected `hatch-fancy-pypi-readme` while links pointed to `main` and left pinned links to this change.
- `.github/workflows/tests.yml` runs on pushes to `main` and on pull requests: `ruff and ty`, and pytest on Linux, macOS and Windows with Python 3.11 and 3.13. Actions are pinned to commit hashes; Dependabot updates them and the development dependencies once a month.
- GitHub rulesets exist: `main` cannot be deleted or force-pushed; the seven CI checks are required on `main`, with a bypass for repository admins, so the maintainer still pushes directly; tags `v*` can be created, moved or deleted by repository admins only.
- `scripts/` holds standalone scripts that tests load through `conftest.load_module`.
- The classifiers name only `Programming Language :: Python :: 3`; the sdist includes `src/`, `tests/`, `scripts/`, `docs/`, `README.md`, `CONTRIBUTING.md` and `LICENSE`.

## Goals / Non-Goals

**Goals:**

- Cutting a release is a short, documented sequence of local steps followed by one approval click; everything after the tag push is automated.
- Nothing reaches PyPI that has not passed the full test matrix, and the files on PyPI are exactly the ones that were tested.
- No long-lived secret exists for publishing.

**Non-Goals:**

- Automating the changelog or the version bump.
- Signing tags or commits.
- Building anything other than the pure-Python sdist and wheel.

## Decisions

### D1: A hand-written changelog in the Keep a Changelog format

`CHANGELOG.md` at the repository root follows Keep a Changelog 1.1.0: a short preamble, `## [Unreleased]`, then `## [X.Y.Z] - YYYY-MM-DD` per release, newest first, with entries under `### Added`, `### Changed`, `### Deprecated`, `### Removed`, `### Fixed` and `### Security`, and link references at the end: `[Unreleased]` to `compare/vX.Y.Z...HEAD` (before the first release, to `commits/main`), each version to `releases/tag/vX.Y.Z`. Entries are written for users: what they notice, not how it was built. The first `Unreleased` content summarises what 0.1.0 offers in a few lines under `### Added`, not every change since the start.

`scripts/release.py` parses the file (D3), and `tests/test_release.py` checks the repository's `CHANGELOG.md` with it: the structure of the spec's Changelog requirement, and that the newest released version, if any, equals `__version__`.

Entries are kept with the work: a tasks rule in `openspec/config.yaml` makes every user-visible OpenSpec change add one, and `AGENTS.md` says the same for changes made without OpenSpec. That replaces the CI check that fragment tools offer.

*Alternatives:* news fragments with towncrier or scriv, rejected for now: their main gain is avoiding merge conflicts between parallel branches, and in this repository a conflict in `Unreleased` is two adjacent lines that a rebase resolves; Dependabot never touches the changelog. Switching later keeps the existing text. A changelog generated from commits (git-cliff, Conventional Commits), rejected: the commit messages are prose for developers, and a generated changelog reads like a commit list.

### D2: The version lives in `__version__` and changes only in the release commit

The release commit sets `__version__`, moves the `Unreleased` entries into the new dated section, updates the link references, and is tagged `vX.Y.Z` (annotated). Between releases, `main` carries the version of the last release. While the version is 0.x, a change that breaks the CLI or what users rely on in the backup raises the minor version; everything else raises the patch version. The backup's own format version in `state.json` is independent of the package version.

*Alternatives:* `hatch-vcs`, deriving the version from the tag, rejected: one more build plugin, versions such as `0.1.1.dev3+g1a2b3c` for every build from `main`, and an editable install whose `--version` depends on Git; the tag check would become trivial, but the changelog check remains anyway. A `.dev0` bump right after each release, rejected: an extra commit per release for a version that is only seen by people who install from Git.

### D3: `scripts/release.py`, one place for the release checks

A standalone script, standard library only, runnable as `python scripts/release.py <command>` with any Python 3.11 or newer:

- `check <tag>`: fails with exit status 1 and a message naming the values if the tag is not `v` plus `__version__` (read from `__init__.py` with the same pattern Hatchling uses, without importing the package), or if `CHANGELOG.md` has no dated, non-empty section for that version.
- `notes <version>`: prints the body of that version's section, without its heading, for the GitHub Release.

The parser is a function the tests call directly (D1), on the real file and on small invalid examples. It reads with `encoding="utf-8"` in text mode, so a Windows checkout with CRLF line endings parses the same; it uses `pathlib` only and writes nothing.

The check whether the tagged commit is on `main` needs Git and stays in the workflow (D4).

*Alternative:* inline shell in the workflow, rejected: the same checks would be needed locally before tagging, and shell is not tested offline.

### D4: `release.yml`, started by a tag, gated by an environment

Triggered by `push: tags: ["v*"]`. Top-level `permissions: contents: read`; jobs that need more ask for it. Actions are pinned to commit hashes with version comments, as in `tests.yml`, and checkouts use `persist-credentials: false` except where a job needs Git history. `concurrency` groups by tag without cancelling, so a second push of the same tag cannot race the first.

| Job | Needs | What it does |
|---|---|---|
| `check` | | Full-history checkout; fails if the tagged commit is not an ancestor of `origin/main`; runs `release.py check "$GITHUB_REF_NAME"` |
| `tests` | `check` | Calls `tests.yml` (D5): lint, type check and the offline tests on all three systems |
| `build` | `check` | `uv build`; `twine check --strict dist/*`; checks that the wheel's `METADATA` links to `/vX.Y.Z/` and not to `/main/` (D6); uploads `dist/` as a workflow artifact |
| `smoke` | `build` | On Linux, macOS and Windows with Python 3.11 and 3.13: runs `todoist-takeout --version` from the built wheel (`uvx --from <wheel>`), expects `X.Y.Z` |
| `testpypi` | `tests`, `smoke` | Environment `testpypi`; `id-token: write`; publishes `dist/` to TestPyPI with `skip-existing`, so re-running a later failed job of the same run does not fail on files already there (a re-tag is not covered, see D8) |
| `pypi` | `testpypi` | Environment `pypi`, which has a required reviewer; `id-token: write`; publishes `dist/` to PyPI |
| `github-release` | `pypi` | `contents: write`; `gh release create "$GITHUB_REF_NAME" dist/* --verify-tag --title X.Y.Z --notes-file <notes>` with the output of `release.py notes` |

Every publishing job downloads the one `dist/` artifact of `build`, so all three destinations get identical files (spec: Tested build). The approval of the `pypi` environment is the pause in which the maintainer looks at the TestPyPI page, the description in particular, before anything permanent happens; a PyPI version can never be uploaded again, while TestPyPI is a sandbox.

Publishing uses `pypa/gh-action-pypi-publish`, which authenticates through Trusted Publishing (OIDC) and generates PEP 740 attestations, so PyPI shows where the files were built. It is a CI action, not a dependency of the package; the reason for it over `uv publish` is the attestations, which `uv publish` uploads but does not create.

The workflow uses no repository secrets. `gh` is preinstalled on the runners and uses the job's `GITHUB_TOKEN`.

*Alternatives:* starting the release from a GitHub Release created in the web UI, rejected: the tag is then created by the UI, and the notes would be typed twice; a `workflow_dispatch` dry run that uploads to TestPyPI, rejected: it would use up the version number on TestPyPI before the real tag, and the local dry run (D8) covers the build. Publishing straight to PyPI without TestPyPI, rejected: the description's rendering and the links can only be seen on a real index.

### D5: `tests.yml` becomes reusable

`tests.yml` gets a `workflow_call` trigger next to its current ones and is called by the `tests` job of `release.yml`. The tests therefore run against the tagged commit itself, even when it was pushed directly to `main` with the admin bypass and before its push-triggered run finished. The check names of the called jobs carry a `tests / ` prefix in the release run, which does not touch the ruleset for `main`, since that only sees the runs started by pushes and pull requests.

*Alternative:* have `check` look up the push-triggered run of the tagged commit and require it to be green, rejected: it depends on timing and on the GitHub API, and repeats less than a direct call.

### D6: `hatch-fancy-pypi-readme` pins the PyPI description to the tag

`pyproject.toml` declares `readme` dynamic and configures the plugin with one fragment, the whole `README.md`, and one substitution:

```toml
[[tool.hatch.metadata.hooks.fancy-pypi-readme.substitutions]]
pattern = 'https://github\.com/sercxanto/todoist-takeout/(blob|raw)/main/'
replacement = 'https://github.com/sercxanto/todoist-takeout/\1/v$HFPR_VERSION/'
```

The plugin replaces `$HFPR_VERSION` in the final description with the package version (its README, section "Referencing Packaging Metadata", 25.1.0). Links that are not to repository files, such as the badges and `todoist.com`, are left alone; the README in the repository keeps `main`, so `test_docs.py` is unchanged.

The plugin is listed in `build-system.requires` with a lower bound, which build front ends install into the isolated build environment only; it is never installed with todoist-takeout, so the package keeps no runtime dependency. It is also a development dependency, so that `tests/test_release.py` can render the description offline with the plugin's CLI, which sets the version to `42.0` in that mode: the test checks that the result is the README with every repository link moved from `main` to `v42.0` and nothing else changed. The `build` job (D4) checks the real version in the built metadata.

*Alternatives:* leave the links on `main`, rejected: PyPI keeps the description of each version forever, and links to `main` drift away from it (options that the installed version lacks, moved pages). A small custom Hatch build hook, rejected: own code for what a widely used plugin (attrs, Black, Pydantic) does. Relative links rewritten to absolute ones, rejected in `simplify-readme` already.

### D7: README for an installable package

- Three badges under the title, each an image linking to its page: CI (`actions/workflows/tests.yml/badge.svg?branch=main`), PyPI version and supported Python versions (shields.io, which reads the classifiers of D9). They link to absolute URLs, so `test_docs.py` accepts them; whether its link pattern also sees the outer link of a linked image is checked during implementation.
- Quick start: "todoist-takeout needs Python 3.11 or newer; uv installs it if needed", then `uv tool install todoist-takeout`, then one sentence for pipx users: `pipx install todoist-takeout`, and `pipx run datasette ~/Backups/todoist` instead of `uvx datasette`. The installation from a Git URL and from a checkout moves to `CONTRIBUTING.md`, which is where people who want unreleased code look.
- The `Status` section and the "not on PyPI yet" sentence go.

### D8: Contributor guidance: releasing, dry run, emergency path

`CONTRIBUTING.md` gets a section "Releasing":

1. Move the `Unreleased` entries into `## [X.Y.Z] - <today>`, update the link references, set `__version__`.
2. Dry run: `python scripts/release.py check vX.Y.Z`, `uv build`, `uvx twine check --strict dist/*`; optionally render the description with `uvx hatch-fancy-pypi-readme`.
3. Commit as "Release X.Y.Z", push to `main`, wait for CI.
4. `git tag -a vX.Y.Z -m "X.Y.Z"` and `git push origin vX.Y.Z`.
5. In the release run, check the TestPyPI page, then approve the `pypi` deployment.
6. Check the PyPI page and the GitHub Release.

A bad release is yanked on PyPI and followed by a patch release; a version number is never reused, and a tag that was published is never moved. What happens after a failed run depends on how far it got:

- Failed before `testpypi` (check, tests, build, smoke): fix the cause in a new commit, delete the tag and create it again on that commit (admins only); the version is unchanged.
- Failed or rejected from `testpypi` on, including a rejected approval: the version is used up, because TestPyPI, like PyPI, accepts a file name only once, and with `skip-existing` a re-tag would leave the old files on TestPyPI, so the review would no longer show what PyPI would get. The fix goes out as the next patch version; the changelog section is renamed to it, and the skipped version never appears on PyPI, a gap that SemVer allows.
- Failed after `pypi` (`github-release`): re-run the failed job; it uses the same artifact.

The emergency path, when GitHub Actions cannot publish: create an API token on PyPI scoped to the project, build from a clean checkout of the tag, `uv publish --token …`, delete the token. Such a release has no attestation. No token is kept between releases.

`AGENTS.md` says that a change users notice adds an entry under `Unreleased`, and `openspec/config.yaml` gets the matching tasks rule (D1).

### D9: Package metadata

- Classifiers for `Programming Language :: Python :: 3.11`, `3.12` and `3.13`, the versions the CI matrix (3.11, 3.13) and the dev container (3.12) run, plus `Programming Language :: Python :: 3 :: Only` and `Topic :: System :: Archiving :: Backup`.
- `[project.urls]` gets `Changelog = ".../blob/main/CHANGELOG.md"`.
- `CHANGELOG.md` joins the sdist's `include` list, so the sdist carries its own history.

### D10: Manual setup outside the repository

Done by the maintainer, with steps listed in tasks.md:

- PyPI and TestPyPI (separate accounts): a pending trusted publisher for the project `todoist-takeout`, owner `sercxanto`, repository `todoist-takeout`, workflow `release.yml`, environment `pypi` or `testpypi`.
- GitHub environments: `testpypi` and `pypi`, both with deployments limited to tags `v*`; `pypi` with the maintainer as required reviewer and "Prevent self-review" off, since the maintainer approves their own release.

## Risks / Trade-offs

- [The tagged commit passed no CI because it was pushed with the admin bypass] → The release runs the full matrix itself (D5).
- [A tag on a commit outside `main`] → `check` stops the release; only admins can create `v*` tags.
- [Someone registers `todoist-takeout` on PyPI before the first upload; a pending publisher does not reserve the name] → Set up the publishers and release soon after merging.
- [`$HFPR_VERSION` is expanded before the substitutions run, so the inserted placeholder stays literal] → Verified with `uv build` during implementation (task 2.2), and on every release by the `build` job, which fails before anything is published.
- [A new release of the build plugin breaks the build] → The `build` job fails before publication; the lower bound can become a pin.
- [PyPI succeeds and the GitHub Release fails] → Re-run the failed job; it uses the same artifact. Artifacts are kept for the default 90 days.
- [TestPyPI is down] → Re-run once it is back; the approval gate waits up to 30 days.
- [Between merging this change and the first release, the README tells users to install from PyPI] → The first release follows right after the merge.
- [`--version` from a Git install of `main` shows the last release, not "development"] → Accepted (D2).

## Migration Plan

1. Implement and merge this change; CI on `main` stays green.
2. The maintainer sets up the trusted publishers and the environments (D10).
3. The first release, 0.1.0, follows the steps of D8. It is not part of this change.

Rollback: removing `release.yml` stops releases; a published version can only be yanked.

Verification on Windows and macOS: the release workflow's own jobs run there (`smoke`, and `tests` through `tests.yml`), and `tests/test_release.py` runs in the regular CI matrix on all three systems after the push of this change.

## Open Questions

- Whether to add Python 3.14 and 3.15 to the CI matrix and the classifiers; independent of this change.
- Whether the PyPI description should also show the newest changelog section, which the plugin can append; can be added to D6's configuration later.
