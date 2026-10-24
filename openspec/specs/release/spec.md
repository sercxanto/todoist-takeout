# release Specification

## Purpose

Defines how a version of todoist-takeout is cut and published: version and tag rules, the changelog, the checks before publication, the approval gate, and what users find on PyPI and GitHub for each version.

## Requirements

### Requirement: Version numbers
Versions SHALL follow Semantic Versioning in the form `MAJOR.MINOR.PATCH`. The package version, the version printed by `todoist-takeout --version` and the version of the newest released changelog section SHALL be the same, once a release exists.

#### Scenario: Version agrees with the changelog
- **WHEN** the newest released section of `CHANGELOG.md` is `0.1.0`
- **THEN** the package version is `0.1.0` and `todoist-takeout --version` prints `0.1.0`

#### Scenario: Before the first release
- **WHEN** `CHANGELOG.md` has no released section yet
- **THEN** the offline tests accept any valid version number

### Requirement: Changelog
The repository SHALL hold a `CHANGELOG.md` in the Keep a Changelog format: an `Unreleased` section first, then one section per release, newest first, each headed `[<version>] - <YYYY-MM-DD>`, with entries grouped under Keep a Changelog's categories. Each section SHALL have a link reference to its tag or comparison on GitHub. An offline test SHALL check this structure.

#### Scenario: Well-formed changelog
- **WHEN** the offline tests run
- **THEN** they fail if `Unreleased` is missing or not first, a release heading has no valid date, releases are not in descending order, a category is unknown, or a section has no link reference

### Requirement: Release trigger
A release SHALL be started only by pushing a tag `v<version>` to the GitHub repository. Before anything is published, the release SHALL stop if the tag does not equal `v` followed by the package version, if the tagged commit is not on `main`, or if `CHANGELOG.md` has no non-empty section for that version.

#### Scenario: Tag and version agree
- **WHEN** the maintainer pushes `v0.1.0` on a commit of `main` whose package version is `0.1.0` and whose changelog has a dated, non-empty `0.1.0` section
- **THEN** the release continues to the build

#### Scenario: Tag does not match the version
- **WHEN** the maintainer pushes `v0.1.1` on a commit whose package version is `0.1.0`
- **THEN** the release stops with an error that names both versions, and nothing is published

#### Scenario: Changelog section missing
- **WHEN** the tag matches the version but `CHANGELOG.md` has no section for it
- **THEN** the release stops with an error, and nothing is published

### Requirement: Tested build
The sdist and the wheel SHALL be built once per release, from the tagged commit, and the same files SHALL be published to TestPyPI, PyPI and the GitHub Release. Before publication, the full offline test suite SHALL pass on Linux, macOS and Windows, and the built wheel SHALL install and print its version on each of the three.

#### Scenario: A test fails
- **WHEN** a test or the wheel's smoke test fails on any of the three operating systems
- **THEN** nothing is published

#### Scenario: Same files everywhere
- **WHEN** a release is published
- **THEN** the files on PyPI and those attached to the GitHub Release have identical hashes

### Requirement: Publication with approval
Each release SHALL be published to TestPyPI first and to PyPI only after the maintainer approves it. Publication SHALL authenticate through PyPI's Trusted Publishing; no long-lived PyPI token SHALL be stored in the repository or its settings.

#### Scenario: Waiting for approval
- **WHEN** the build and the tests have passed and the files are on TestPyPI
- **THEN** the release waits until the maintainer approves the PyPI deployment, and publishes nothing to PyPI before

#### Scenario: Provenance
- **WHEN** a release is on PyPI
- **THEN** PyPI shows an attestation that the files were built by the release workflow of this repository

### Requirement: GitHub Release
After the publication to PyPI, a GitHub Release SHALL be created for the tag, titled with the version, with the changelog section of that version as its notes and the sdist and the wheel attached.

#### Scenario: Release notes
- **WHEN** `v0.1.0` has been published to PyPI
- **THEN** the GitHub Release `v0.1.0` shows the entries of the `0.1.0` changelog section and offers both files for download

### Requirement: Version-specific PyPI description
The project description on PyPI SHALL be the README, with every link to a file of the repository on `main`, including images, pointing to the release tag of that version instead. The README in the repository SHALL keep linking to `main`.

#### Scenario: Links on PyPI
- **WHEN** version `0.1.0` is built
- **THEN** its description links to `https://github.com/sercxanto/todoist-takeout/blob/v0.1.0/...` and `.../raw/v0.1.0/...`, and contains no link to `/blob/main/` or `/raw/main/`

#### Scenario: README on GitHub
- **WHEN** a user reads the README in the repository
- **THEN** its links point to `main`, as checked by the documentation test

### Requirement: Changelog entries with changes
Every change that users can notice SHALL add an entry under `Unreleased` in the same change. Changes that users cannot notice, such as updates of development dependencies, CI actions or tests, SHALL NOT need an entry.

#### Scenario: User-visible change
- **WHEN** an OpenSpec change adds an option to the CLI
- **THEN** its tasks include adding an entry under `Unreleased`

#### Scenario: Dependabot update
- **WHEN** Dependabot updates a development dependency
- **THEN** no changelog entry is needed
