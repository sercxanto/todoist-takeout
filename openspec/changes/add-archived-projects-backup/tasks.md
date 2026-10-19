# Tasks

## Session plan

Not tracked. This section is guidance for whoever starts the implementation sessions.

- Start each session fresh with `/opsx:apply add-archived-projects-backup`, name the groups it covers, and stop at the end of the last one.
- Commit after each completed group, once its tests pass.
- Session 1: groups 1 and 2, medium effort. Group 1 needs the user with a real token for task 1.2: ask before starting it, never ask for a token in chat, and use only the Free test account (`TODOIST_CONTRACT_WRITE_TOKEN`), because the probe writes.
- Session 2: groups 3 and 4, high effort. Detection, write order and the fill-in rule decide what is never fetched again.
- Session 3: groups 5 and 6, high effort. Crash and resume tests, and the expected tree.
- Session 4: group 7, medium effort. Tasks 7.2 and 7.3 need the user with a real token: use the Pro account (`TODOIST_CONTRACT_TOKEN`) for read-only steps only, and the Free test account for writes.

## 1. Last probe and the format rule for copies

- [x] 1.1 Add a write probe to the probe spike (`tests/contract/probes.py`, `tests/contract/test_probe_write.py`) on the Free account that records booleans only, never values: whether a task's `updated_at` changes when it is completed, when a comment is added, and when its project is archived and unarchived; whether the list of archived projects contains an archived sub-project of an archived parent; and whether `subprojects` of `/full` holds a grandchild. Verify offline that `tests/test_contract_probes.py` passes and that the probe's output holds no ID, timestamp or content
- [x] 1.2 With the user, run the probe of 1.1 against the Free account, and record the results under "API findings" in `proposal.md`. Verify that the findings file stays untracked (`git status` shows nothing under `tests/contract/findings/`) and that `proposal.md` holds no live ID or content
- [x] 1.3 Settle the rule for picking the current copy of a task (design D11) from the result of 1.2, and update D11 and the design's Open Questions to match. Verify that design.md no longer calls the rule proposed

## 2. Missing-object count of a full sync

- [x] 2.1 Extend `_stored_live_keys` in `sync.py` with the exclusions of design D9 (archived projects, objects whose `project_id` names one, checked items, archived sections, notes of excluded items), keep the warning for the rest, and log the excluded count at INFO. Verify with apply tests for every scenario of the modified requirement "Server-initiated full sync": "Token no longer accepted", "Completed tasks", "Archived project" (six objects), "Archived section" and "Mixed"
- [x] 2.2 Document in `storage_format.md` that objects which left the sync because they were completed or archived do not count as missing after a full sync, and that archived sections stay as the sync reported them, while sections archived before the first backup are lost apart from the `section_id` of their tasks. Verify that `tests/test_storage_format.py` passes

## 3. Fixtures and snapshot fetch

- [x] 3.1 Add hand-written fixtures under `tests/fixtures/archived/` with invented IDs: archived-list pages with and without `next_cursor`, `/full` responses with open tasks, sections, an archived sub-project and an empty `subprojects`, `by_completion_date` pages filtered by project, comment pages for a task (with a `file_attachment`) and a project, and error bodies for 404 and a plan restriction. Verify with a fixture-loading test that every file parses
- [x] 3.2 Make `_CommentStore` in `completed.py` take its directory as a parameter, without changing its behaviour for `completed/comments/`. Verify that the existing completed-tasks tests pass unchanged
- [x] 3.3 Create `archived.py` with `run(target, client, state, now)` and call it from `run_backup` after `completed.run` (design D1). Implement the list walk and the snapshot fetch of D5 with the write order of D3, the windows of D7 and the case-collision check of D12, and add "including those of archived projects" to the `PLAN_FEATURES` texts. Verify with run tests for the spec requirements "Archived projects are listed", "Snapshot of an archived project", "Completed tasks over the project's lifetime" (13 windows; unknown `created_at`), "Attachments are not downloaded" and "Project gone during the run", and that every produced file matches the spec requirement "Archived-projects layout"

## 4. Detection, refetch, fill-in and sub-projects

- [x] 4.1 Implement the detection table of design D4 and the refetch of D5, in which `full.json` and `completed.json` are replaced and comment files are merged. Verify with run tests for "Repeat run" (only the list requested, no file rewritten), "Unarchived and archived again", and "Comment deleted before the project was archived again"
- [x] 4.2 Implement the fill-in of design D6 for `completed.json` and `project_comments.json`, and the plan handling of D5 and D10. Verify with run tests for every scenario of the spec requirement "Plan limits for archived projects", for a plan without comments followed by one with comments, and for a 404 on a task's comments that is not retried in the next run
- [x] 4.3 Implement the sub-project queue of design D8. Verify with run tests for "Sub-project only in its parent" and "Sub-project listed too", and a unit test in which two projects name each other as sub-projects and each is fetched once
- [x] 4.4 Add the INFO line of design D13. Verify with a run test that reads the log of a run that fetches two new snapshots, refetches one and fills in one

## 5. Format description

- [x] 5.1 Document `archived/` in `storage_format.md`: the layout, the response-shaped contents, `project.json` as the checkpoint, the refetch rule, the fill-in rule, snapshots kept after unarchiving or deletion, and comments merged and never removed. Verify that the path-pattern tests of `tests/test_storage_format.py`, extended to `archived/`, find every produced path documented and every documented pattern produced
- [x] 5.2 Add the section "Copies of the same task" to `storage_format.md` (design D11) with the table of places and the rule settled in 1.3, including the comments in `completed/comments/` and `archived/<project id>/comments/`. Verify with a test that the description names all four task locations and both comment locations

## 6. Whole runs

- [ ] 6.1 Add crash and resume tests for this part: with the fault-injection hook at several counts during a first run with three archived projects, one with a sub-project, a rerun produces the tree of an uninterrupted run byte for byte and makes no `/full` request for a finished snapshot
- [ ] 6.2 Add an idempotence test: two runs with unchanged fake responses leave every file under `archived/` with the same bytes and modification time
- [ ] 6.3 Extend the end-to-end CLI test of `tests/test_cross_platform.py` and `tests/fixtures/expected_tree/` with an `archived/` snapshot. Verify that the produced tree matches byte for byte on Linux, and that the test runs in the CI matrix
- [ ] 6.4 Update the project `README.md`: the backup scope with archived projects, the roadmap, and advice for the first run on an account with many archived projects. Verify that every link in the changed sections resolves

## 7. Fixture recording, contract tests and probe removal

- [ ] 7.1 Extend `scripts/record_fixtures.py` to record the archived list and one `/full`, read-only, scrubbed by the existing rules, and add both endpoints to `shapes.json`. Verify with an offline scrubber test that IDs inside `subprojects` and `collaborators` are pseudonymized consistently
- [ ] 7.2 Add a read-only contract test for the archived list and one `/full`, compared with the recorded shapes, and run it with the user against both accounts. Verify that it passes on both and fails offline when a field is added to a recorded shape
- [ ] 7.3 Add a write contract test, Free account only and behind the existing guard: create a project with a sub-project, a section, an open task, a completed task and comments, archive the parent, run the tool into a temporary backup, force a full sync, and assert both snapshots, the comment files and no missing-object warning. Run it with the user. Verify that it passes and cleans up the projects it created
- [ ] 7.4 Remove the probe spike (`tests/contract/probes.py`, `tests/contract/test_probe_*.py`, `tests/test_contract_probes.py`) now that its findings are in the proposals. Verify that the offline suite and `pytest --collect-only tests/contract` pass, and that no remaining file imports `probes`

## Workflow follow-up

- Archive the change once the review is done, and check that the main specs gain `archived-projects-backup` and the modified requirements of `backup-storage` and `active-data-sync`.
