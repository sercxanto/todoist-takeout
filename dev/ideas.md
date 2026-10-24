# Ideas

A place for ideas that are not thought through yet. A concrete plan becomes an OpenSpec change
(`/opsx:explore`, then `/opsx:propose`) and is struck out or linked here.

Status: `raw` · `clarified` · `change created` · `rejected`

## Read the token from a file

- Status: raw
- Today the token comes only from `TODOIST_API_TOKEN`. Every scheduling example in
  `docs/scheduling.md` needs a shell wrapper (cron, launchd, PowerShell) to set it from a file,
  and the `export` line of the README's quick start ends up in the shell history.
- Open: a variable such as `TODOIST_API_TOKEN_FILE`, an option such as `--token-file`, or both;
  which one wins when several are set; whether to warn about a token file that others can read.
