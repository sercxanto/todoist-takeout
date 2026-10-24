# Tasks

## 1. Changelog and release helper

- [x] 1.1 Write `CHANGELOG.md` as in design D1: preamble, `## [Unreleased]` with a short `### Added` summary of what 0.1.0 offers, and the link reference `[Unreleased]` to `commits/main`. Verify that it renders as expected in a GitHub preview or by inspection, and that every entry is written from the user's point of view
- [x] 1.2 Write `scripts/release.py` with the commands `check <tag>` and `notes <version>` and the changelog parser (design D3), standard library only. Verify that `python scripts/release.py check v0.1.0` exits 1 naming the missing changelog section, and `check v9.9.9` exits 1 naming both versions
- [x] 1.3 Write `tests/test_release.py`: the repository's `CHANGELOG.md` has the structure of the spec's Changelog requirement; the newest released version, if any, equals `__version__`; `check` and `notes` on small valid and invalid changelogs in `tmp_path`, including CRLF line endings. Verify that `uv run pytest tests/test_release.py` passes, and that it fails when `Unreleased` is moved below a release or a release heading loses its date
- [x] 1.4 Add the changelog rule to `AGENTS.md` and the tasks rule to `openspec/config.yaml` (design D1, D8). Verify that `openspec instructions tasks --change add-release-workflow --json` lists the new rule

## 2. Package metadata and PyPI description

- [x] 2.1 Update `pyproject.toml` as in design D9: Python classifiers, `Programming Language :: Python :: 3 :: Only`, `Topic :: System :: Archiving :: Backup`, the `Changelog` URL, and `CHANGELOG.md` in the sdist. Verify with `uv build` that the metadata lists them and the sdist holds `CHANGELOG.md`
- [x] 2.2 Configure `hatch-fancy-pypi-readme` as in design D6: `readme` dynamic, the plugin in `build-system.requires` with a lower bound and in the `dev` group (`uv lock`), one fragment and the substitution. Verify with `uv build` that the wheel's `METADATA` links to `/blob/v0.1.0/` and `/raw/v0.1.0/` and to neither `/blob/main/` nor `/raw/main/`; if `$HFPR_VERSION` stays literal, stop and revise design D6 with the user
- [x] 2.3 Add the test of the PyPI description to `tests/test_release.py` (design D6): render it with the plugin's CLI and compare it with `README.md` after moving each repository link from `main` to `v42.0`. Verify that it passes, and that it fails when the substitution is removed from `pyproject.toml`
- [x] 2.4 Check the description with `uvx twine check --strict dist/*`. Verify that it reports no warning

## 3. Workflows

- [x] 3.1 Add `workflow_call` to the triggers of `.github/workflows/tests.yml` (design D5). Verify that the file parses and that the comment at its top still describes its triggers
- [x] 3.2 Write `.github/workflows/release.yml` with the jobs, permissions, environments and concurrency of design D4, every action pinned to a commit hash with its version as a comment (look up the current release of `actions/upload-artifact`, `actions/download-artifact` and `pypa/gh-action-pypi-publish`). Verify that the file parses, that `uvx zizmor .github/workflows/` reports no finding above "low" or each is explained in a comment, and that no job has `id-token: write` or `contents: write` except `testpypi`, `pypi` and `github-release`
- [x] 3.3 Run the commands of the `build` and `smoke` jobs locally on Linux (`uv build`, `twine check`, the `METADATA` check, `uvx --from dist/<wheel> todoist-takeout --version`). Verify that each passes and the version printed is `0.1.0`

## 4. Documentation

- [x] 4.1 Update `README.md` as in design D7: the three badges, installation from PyPI with uv and pipx, `pipx run datasette`, and without the `Status` section and the "not on PyPI yet" sentence. Verify that `uv run pytest tests/test_docs.py` passes and checks the badge targets, extending its link pattern if it misses the outer link of a linked image
- [x] 4.2 Add the section "Releasing" to `CONTRIBUTING.md` with the steps, the handling of a failed or bad release and the emergency path of design D8, and the installation from a Git URL and from a checkout (design D7). Verify that the dry-run commands run as written, apart from `release.py check`, which passes only on a release commit
- [x] 4.3 Mark the changelog and release-workflow ideas in `dev/ideas.md` "change created", with a link to this change. Verify that `tests/test_docs.py` still passes

## 5. Setup by the maintainer

These steps are done by the user in the web interfaces; ask them to do each and to confirm it.

- [x] 5.1 On PyPI: add a pending trusted publisher for the project `todoist-takeout`, owner `sercxanto`, repository `todoist-takeout`, workflow `release.yml`, environment `pypi` (design D10). Verify that the user sees it under "Your projects → Publishing" as pending
- [x] 5.2 On TestPyPI, with its own account: the same pending publisher with environment `testpypi`. Verify as in 5.1
- [x] 5.3 On GitHub (Settings → Environments): create `testpypi` and `pypi`, both limited to tags `v*`, and `pypi` with the user as required reviewer and "Prevent self-review" off. Verify that the user sees both environments with these rules

## 6. Checks

- [x] 6.1 Run `uv run ruff format .`, `uv run ruff check .`, `uv run ty check` and `uv run pytest`, and fix what they report. Verify that all four pass
- [ ] 6.2 After the user agrees, push the change to `main` and wait for CI. Verify that all seven checks are green, so `tests/test_release.py` passes on Windows and macOS too

## Workflow follow-up

- The release workflow first runs with the first real tag; cutting release 0.1.0 by the steps of design D8 is a separate request after this change.
- Archive the change after review, once release 0.1.0 has shown that the workflow works.
