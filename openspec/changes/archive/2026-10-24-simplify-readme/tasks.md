# Tasks

## 1. Development notes

- [x] 1.1 Move `docs/ideas.md` to `dev/ideas.md` with `git mv` and translate it into English, including the status names; mark the README idea "change created" with a link to this change, and add badges, the PyPI install line, links pinned to release tags and the PyPI rendering check to the release-workflow idea (design D7). Verify with `git status` that the file shows as renamed and that `docs/` no longer holds it

## 2. Detailed pages under `docs/`

- [x] 2.1 Write `docs/backup.md` from the sections listed for it in design D2, with an opening sentence and a link back to the README. Verify that every fact of those README sections appears on the page
- [x] 2.2 Write `docs/viewing.md` from its sections in design D2; replace the in-page anchor links of the current text with links within the page or to `backup.md`. Verify that every table, view and command of those sections appears on the page
- [x] 2.3 Write `docs/scheduling.md` and `docs/platform-notes.md` from their sections in design D2. Verify that the cron line, the launchd plist and both PowerShell snippets are unchanged
- [x] 2.4 Link the pages to each other and to `../src/todoist_takeout/storage_format.md` with relative links (design D3). Verify that every relative link and anchor resolves in a GitHub preview or by inspection
- [x] 2.5 Compare the current `README.md` with the four pages section by section and list any sentence that is missing; restore it or record why it was cut. Verify that the list is empty or every item is accounted for

## 3. Demo backup and screenshot

- [x] 3.1 Write invented demo responses under `scripts/demo/`: a full sync with a few projects, sections, labels, open, overdue and recurring tasks and a comment, plus completed tasks and completion events, with dates relative to the day of the run (design D5). Verify that no name, ID or text comes from a real account or from the recordings
- [x] 3.2 Write the data step of `scripts/demo_screenshot.py`: run `backup` against `FakeApi` with the demo responses into a temporary directory, then `sqlite`. Add a test that runs this step and checks that `open_tasks` and `done_log` have rows. Verify with `uv run pytest tests/test_demo_screenshot.py`
- [x] 3.3 Add the screenshot step: start Datasette on a free port, take the screenshot of `open_tasks` with `uvx shot-scraper` at about 1200 pixels width, write `docs/images/datasette.png`, and stop Datasette. Verify by running the script and looking at the image
- [x] 3.4 Document in `CONTRIBUTING.md` how to recreate the screenshot (`uvx shot-scraper install` once, then the script) and when to do it. Verify that the documented commands run as written

## 4. README and links

- [x] 4.1 Add `[project.urls]` to `pyproject.toml` as in design D4. Verify with `uv build` that the built metadata lists the three URLs
- [x] 4.2 Rewrite `README.md` with the outline of design D1, absolute links and image URL (design D3), and no in-page anchors. Verify that it is at most about 80 lines and that the quick-start commands run as written against a test account or the demo backup
- [x] 4.3 Write `tests/test_docs.py` as in design D6. Verify that it passes, and that it fails when a relative link is added to the README or a relative link in `docs/` points to a missing file or heading
- [x] 4.4 Check the PyPI rendering as in design D8 (`readme_renderer` and `twine check`). Verify that both report no error and that the rendered HTML shows the image and links as absolute URLs

## 5. Contributor guidance

- [x] 5.1 Update `AGENTS.md` and `CONTRIBUTING.md`: user documentation goes into the README (short) or `docs/` (details), development notes into `dev/`, and the README uses absolute links only, which `tests/test_docs.py` checks (design D7). Verify that both files link to the right places and that `CONTRIBUTING.md` no longer refers to README sections that moved

## 6. Checks

- [x] 6.1 Run `uv run ruff format .`, `uv run ruff check .` and `uv run pytest`, and fix what they report. Verify that all three pass

## Workflow follow-up

- Archive the change after review.
