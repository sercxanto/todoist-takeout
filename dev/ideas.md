# Ideas

A place for ideas that are not thought through yet. A concrete plan becomes an OpenSpec change
(`/opsx:explore`, then `/opsx:propose`) and is struck out or linked here.

Status: `raw` · `clarified` · `change created` · `rejected`

## Add a changelog

- Status: raw
- Open: format (for example Keep a Changelog), written by hand or generated, relation to the
  OpenSpec changes.

## Conduct an independent review session

- Status: raw
- Review the codebase and the repo regarding code quality and usability in a fresh session

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

## Read the token from a file

- Status: raw
- Today the token comes only from `TODOIST_API_TOKEN`. Every scheduling example in
  `docs/scheduling.md` needs a shell wrapper (cron, launchd, PowerShell) to set it from a file,
  and the `export` line of the README's quick start ends up in the shell history.
- Open: a variable such as `TODOIST_API_TOKEN_FILE`, an option such as `--token-file`, or both;
  which one wins when several are set; whether to warn about a token file that others can read.
