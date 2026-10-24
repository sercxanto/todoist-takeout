# Ideas

A place for ideas that are not thought through yet. A concrete plan becomes an OpenSpec change
(`/opsx:explore`, then `/opsx:propose`) and is struck out or linked here.

Status: `raw` · `clarified` · `change created` · `rejected`

## Add a changelog

- Status: raw
- Open: format (for example Keep a Changelog), written by hand or generated, relation to the
  OpenSpec changes.

## JSON schema

- Status: raw
- Open: Does it make sense to add a JSON schema file in addition to the format description?

## Conduct a independend review session

- Status: raw
- Review the codebase and the repo regarding code quality and useability in a fresh session

## Define the release workflow

- Status: raw
- Open: versioning, tagging, build and publication (PyPI?), how it fits with the changelog and
  the license.
- Badges in the README, for example CI status, PyPI version and supported Python versions. They
  make sense only once CI runs on its own and the repository is public.
- The install line in the README becomes `uv tool install todoist-takeout`.
- Links in the README point to `main`; pin them to the release tag instead, for example with
  `hatch-fancy-pypi-readme`, so that PyPI shows the documentation of the released version.
- Check that the README and its screenshot render on PyPI once the repository is public.
