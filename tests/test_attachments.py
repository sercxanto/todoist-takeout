"""Tests of the download step for attachment files (add-attachment-downloads, groups 4.2 to 4.4)."""

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fake_api import (
    FILE_TOKEN,
    FIXTURE_FILES,
    FakeApi,
    archived_answers,
    attachment_fixture,
    completed_answers,
    completed_fixture,
    file_body,
)

from todoist_takeout import attachments, storage, sync
from todoist_takeout.api import Client
from todoist_takeout.cli import main
from todoist_takeout.errors import NetworkError, RateLimitExhausted, StorageError

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
SYNC_FIXTURES = Path(__file__).parent / "fixtures" / "sync"
TOKEN = "0123456789abcdef0123456789abcdef01234567"
FILES = "https://files.todoist.com/fake/test"


def url(comment_id: str, name: str = "preise.pdf") -> str:
    return f"{FILES}/{comment_id}/{name}"


def note(comment_id: str, name="preise.pdf", *, size=20, file_url=None, item_id="6X3Tk9Pq2Vm5Rs8W", **fields):
    """A comment with an uploaded file. `fields` go into the attachment; None removes one."""
    attachment = {
        "resource_type": "file",
        "file_name": name,
        "file_size": size,
        "file_type": "application/pdf",
        "file_url": file_url or url(comment_id, name if isinstance(name, str) else "file"),
        "upload_state": "completed",
        **fields,
    }
    attachment = {k: v for k, v in attachment.items() if v is not None}
    return {
        "id": comment_id,
        "item_id": item_id,
        "content": "see the file",
        "file_attachment": attachment,
        "is_deleted": False,
        "posted_at": "2026-09-12T17:00:00.000000Z",
    }


def store_note(target: Path, comment: dict, resource: str = "notes") -> None:
    path = target / "sync" / resource / f"{storage.encode_key(comment['id'])}.json"
    storage.write_bytes_atomic(path, storage.encode_json(comment))


def store_results(path: Path, comments: list) -> None:
    storage.write_bytes_atomic(path, storage.encode_json({"results": comments, "next_cursor": None}))


def serve(api: FakeApi, *comments) -> FakeApi:
    """Serve the file of each comment with as many bytes as its `file_size`."""
    for comment in comments:
        attachment = comment["file_attachment"]
        api.serve_file(attachment["file_url"], file_body(str(attachment["file_name"]), attachment["file_size"]))
    return api


class Waits(list):
    def __call__(self, delay):
        self.append(delay)


def download(target: Path, api: FakeApi, **client_options) -> Client:
    client = Client(api, sleep=Waits(), jitter=lambda: 0.0, **client_options)
    attachments.run(target, client, lambda: NOW)
    return client


def files_under(target: Path) -> dict[str, bytes]:
    root = target / "attachments"
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# --- The fake file transport (4.1) -------------------------------------------------------------


def test_fake_file_transport_sends_the_token_only_to_the_first_hop():
    signed = "https://d1fake.cloudfront.net/fake/preise.pdf?Signature=fake"
    api = FakeApi(files={}).redirect_file(url("N1"), signed).serve_file(signed, b"%PDF")
    assert Client(api).download(url("N1"), lambda chunks: b"".join(chunks)) == b"%PDF"
    assert api.file_requests == [url("N1")]
    assert api.file_hops == [(url("N1"), f"Bearer {FILE_TOKEN}"), (signed, None)]


def test_fake_file_transport_answers_limited_routes_first():
    api = FakeApi(files={}).serve_file(url("N1"), b"x" * 10).fail_file(url("N1"), times=1)
    api.serve_file(url("N1"), b"abc", gzipped=True)
    assert Client(api, sleep=Waits()).download(url("N1"), lambda chunks: b"".join(chunks)) == b"abc"
    assert len(api.file_requests) == 2


def test_fake_file_transport_fails_on_a_url_without_a_route():
    with pytest.raises(AssertionError, match="unexpected file request"):
        Client(FakeApi(files={})).download(url("N1"), lambda chunks: b"".join(chunks))


# --- 4.2 Uploaded files of stored comments are downloaded ------------------------------------


def test_file_of_a_comment_of_an_active_task(tmp_path):
    comment = note("N1", "preise.pdf")
    store_note(tmp_path, comment)
    api = serve(FakeApi(files={}), comment)
    download(tmp_path, api)
    assert files_under(tmp_path) == {"N1/preise.pdf": file_body("preise.pdf", 20)}
    assert api.file_hops == [(url("N1"), f"Bearer {FILE_TOKEN}")]


def test_file_of_a_project_comment_in_the_sync(tmp_path):
    comment = note("N1", "plan.pdf", item_id=None, project_id="6Jf8VQXxpwv5XvQ2")
    store_note(tmp_path, comment, "project_notes")
    download(tmp_path, serve(FakeApi(files={}), comment))
    assert list(files_under(tmp_path)) == ["N1/plan.pdf"]


def test_files_of_comments_of_archived_projects(tmp_path):
    project_comment, task_comment = note("N2", "projekt.pdf"), note("N4", "aufgabe.pdf")
    store_results(tmp_path / "archived" / "P1" / "project_comments.json", [project_comment])
    store_results(tmp_path / "archived" / "P1" / "comments" / "T2.json", [task_comment])
    download(tmp_path, serve(FakeApi(files={}), project_comment, task_comment))
    assert list(files_under(tmp_path)) == ["N2/projekt.pdf", "N4/aufgabe.pdf"]


def test_file_of_a_comment_of_a_completed_task(tmp_path):
    comment = note("N3", "rechnung.pdf")
    store_results(
        tmp_path / "completed" / "comments" / "T1.json",
        [note("N0", file_url=None) | {"file_attachment": None}, comment],
    )
    download(tmp_path, serve(FakeApi(files={}), comment))
    assert list(files_under(tmp_path)) == ["N3/rechnung.pdf"]


def test_file_of_a_deleted_comment_is_requested_once(tmp_path):
    comment = note("N1") | {"is_deleted": True}
    store_results(tmp_path / "completed" / "comments" / "T1.json", [comment])
    api = serve(FakeApi(files={}), comment)
    download(tmp_path, api)
    assert api.file_requests == [url("N1")]
    assert list(files_under(tmp_path)) == ["N1/preise.pdf"]


def test_live_copy_of_a_comment_in_the_sync_comes_before_its_tombstone(tmp_path):
    live = note("N1")
    store_note(tmp_path, live)
    tombstone = tmp_path / "sync" / "notes" / f"N1{sync.DELETED_SUFFIX}"
    storage.write_bytes_atomic(tombstone, storage.encode_json(note("N1", file_url=url("N1", "other.pdf"))))
    api = serve(FakeApi(files={}), live)
    download(tmp_path, api)
    assert api.file_requests == [url("N1")]


def test_backup_made_by_an_earlier_version_gets_all_its_files(tmp_path):
    first = run_backup(tmp_path, FakeApi(ACCOUNT))
    stored = files_under(tmp_path)
    assert stored == {"6X7rfFVQXwRmcvFp/list.pdf": FIXTURE_FILES[LIST_URL]}
    # A version without this step left no attachments/ directory.
    os.remove(tmp_path / "attachments" / "6X7rfFVQXwRmcvFp" / "list.pdf")
    os.rmdir(tmp_path / "attachments" / "6X7rfFVQXwRmcvFp")
    os.rmdir(tmp_path / "attachments")

    again = run_backup(tmp_path, FakeApi(ACCOUNT))
    assert first.file_requests == again.file_requests == [LIST_URL]
    assert files_under(tmp_path) == stored


# --- 4.2 Only uploaded files ---------------------------------------------------------------


def test_link_attachment_is_not_requested(tmp_path):
    [link, _pending, _foreign] = attachment_fixture("comments")["results"]
    store_results(tmp_path / "completed" / "comments" / "T1.json", [link])
    api = FakeApi(files={})
    download(tmp_path, api)
    assert api.file_hops == []
    assert not (tmp_path / "attachments").exists()


@pytest.mark.parametrize("resource_type", ["url"])
def test_link_type_with_a_file_url_is_not_requested(tmp_path, resource_type):
    store_note(tmp_path, note("N1", resource_type=resource_type))
    api = FakeApi(files={})
    download(tmp_path, api)
    assert api.file_hops == []


@pytest.mark.parametrize("resource_type", ["file", "image", "video", None])
def test_other_resource_types_are_uploaded_files(tmp_path, resource_type):
    comment = note("N1", resource_type=resource_type)
    store_note(tmp_path, comment)
    api = serve(FakeApi(files={}), comment)
    download(tmp_path, api)
    assert api.file_requests == [url("N1")]


def test_upload_that_is_not_complete_is_downloaded_once_a_copy_reports_it_complete(tmp_path):
    [_link, pending, _foreign] = attachment_fixture("comments")["results"]
    store_results(tmp_path / "completed" / "comments" / "T1.json", [pending])
    api = FakeApi(files={})
    download(tmp_path, api)
    assert api.file_hops == []

    done = {**pending, "file_attachment": {**pending["file_attachment"], "upload_state": "completed"}}
    store_results(tmp_path / "archived" / "P1" / "comments" / "T1.json", [done])
    api = serve(FakeApi(files={}), done)
    download(tmp_path, api)
    assert list(files_under(tmp_path)) == [f"{pending['id']}/scan.pdf"]


@pytest.mark.parametrize("attachment", [None, "absent", {}, {"file_url": ""}, "not an object"])
def test_comment_without_an_uploaded_file_makes_no_request(tmp_path, attachment):
    comment = note("N1")
    if attachment == "absent":
        del comment["file_attachment"]
    else:
        comment["file_attachment"] = attachment
    store_note(tmp_path, comment)
    api = FakeApi(files={})
    download(tmp_path, api)
    assert api.file_hops == []
    assert not (tmp_path / "attachments").exists()


# --- 4.2 One file per comment --------------------------------------------------------------


def test_comment_stored_twice_is_downloaded_once(tmp_path):
    comment = note("N1")
    store_results(tmp_path / "completed" / "comments" / "T1.json", [comment])
    store_results(tmp_path / "archived" / "P1" / "comments" / "T1.json", [comment])
    api = serve(FakeApi(files={}), comment)
    download(tmp_path, api)
    assert api.file_requests == [url("N1")]
    assert list(files_under(tmp_path)) == ["N1/preise.pdf"]


def test_first_copy_in_the_order_sync_completed_archived_supplies_the_url(tmp_path):
    in_sync = note("N1", file_url=url("N1", "sync.pdf"))
    store_results(
        tmp_path / "archived" / "P1" / "comments" / "T1.json", [note("N1", file_url=url("N1", "archived.pdf"))]
    )
    store_results(tmp_path / "completed" / "comments" / "T1.json", [note("N1", file_url=url("N1", "completed.pdf"))])
    store_note(tmp_path, in_sync)
    api = serve(FakeApi(files={}), in_sync)
    download(tmp_path, api)
    assert api.file_requests == [url("N1", "sync.pdf")]

    os.remove(tmp_path / "sync" / "notes" / "N1.json")
    os.remove(tmp_path / "attachments" / "N1" / "preise.pdf")
    api = FakeApi(files={}).serve_file(url("N1", "completed.pdf"), file_body("preise.pdf", 20))
    download(tmp_path, api)
    assert api.file_requests == [url("N1", "completed.pdf")]


def test_copy_without_a_file_does_not_hide_one_with_a_file(tmp_path):
    # The sync's tombstone of a deleted comment has `file_attachment: null`; a frozen copy keeps it.
    store_note(tmp_path, note("N1") | {"file_attachment": None, "is_deleted": True})
    comment = note("N1")
    store_results(tmp_path / "archived" / "P1" / "comments" / "T1.json", [comment])
    api = serve(FakeApi(files={}), comment)
    download(tmp_path, api)
    assert api.file_requests == [url("N1")]


# --- 4.2 Stored files are not downloaded again --------------------------------------------


def test_repeat_run_makes_no_request_and_rewrites_nothing(tmp_path):
    comments = [note("N1"), note("N2", "zwei.pdf")]
    store_results(tmp_path / "completed" / "comments" / "T1.json", comments)
    download(tmp_path, serve(FakeApi(files={}), *comments))
    before = {p: p.stat().st_mtime_ns for p in (tmp_path / "attachments").rglob("*")}

    api = FakeApi(files={})
    download(tmp_path, api)
    assert api.file_hops == []
    assert {p: p.stat().st_mtime_ns for p in (tmp_path / "attachments").rglob("*")} == before


def test_stored_file_under_another_name_is_kept(tmp_path):
    store_note(tmp_path, note("N1", "Rechnung: März.pdf"))
    old = tmp_path / "attachments" / "N1" / "Rechnung März.pdf"
    storage.write_bytes_atomic(old, b"stored earlier")
    api = FakeApi(files={})
    download(tmp_path, api)
    assert api.file_hops == []
    assert files_under(tmp_path) == {"N1/Rechnung März.pdf": b"stored earlier"}


@pytest.mark.parametrize("left", ["temp file", "empty directory"])
def test_directory_without_a_stored_file_counts_as_not_stored(tmp_path, left):
    comment = note("N1")
    store_note(tmp_path, comment)
    directory = tmp_path / "attachments" / "N1"
    directory.mkdir(parents=True)
    if left == "temp file":
        (directory / ".preise.pdf.0123abcd.tmp").write_bytes(b"part")
    api = serve(FakeApi(files={}), comment)
    download(tmp_path, api)
    assert api.file_requests == [url("N1")]
    assert (directory / "preise.pdf").read_bytes() == file_body("preise.pdf", 20)


# --- 4.2 Attachments layout ------------------------------------------------------------------


@pytest.mark.parametrize(
    "comment_id, file_name, path",
    [
        ("6X7rfFVQXwRmcvFp", "preise.pdf", "6X7rfFVQXwRmcvFp/preise.pdf"),
        ("CON", "con.txt", "%43ON/_con.txt"),
        ("N1", "Rechnung: März/April.pdf", "N1/Rechnung_ März_April.pdf"),
        ("N2", ".env", "N2/_env"),
        ("N3", "", "N3/attachment"),
        ("N4", None, "N4/attachment"),
        (2671355, "zahl.pdf", "2671355/zahl.pdf"),
    ],
)
def test_layout_is_comment_id_and_cleaned_file_name(tmp_path, comment_id, file_name, path):
    comment = note(comment_id, file_name, file_url=url(str(comment_id)))
    if file_name is None:
        comment["file_attachment"]["file_name"] = None
    store_results(tmp_path / "completed" / "comments" / "T1.json", [comment])
    api = FakeApi(files={}).serve_file(url(str(comment_id)), file_body("x", 20))
    download(tmp_path, api)
    assert list(files_under(tmp_path)) == [path]
    directory = path.split("/")[0]
    assert directory == storage.encode_key(str(comment_id))
    assert path.split("/")[1] == storage.attachment_file_name(file_name)


def test_every_produced_path_matches_the_layout(tmp_path):
    run_backup(tmp_path, FakeApi(ACCOUNT))
    stored = collect_ids(tmp_path)
    assert stored
    for name in files_under(tmp_path):
        directory, file_name = name.split("/")
        assert directory in stored
        assert file_name == storage.attachment_file_name(stored[directory])
        assert len(os.listdir(tmp_path / "attachments" / directory)) == 1


def collect_ids(target: Path) -> dict[str, object]:
    return {storage.encode_key(a.comment_id): a.file_name for a in attachments.collect(target)}


def case_insensitive(directory: Path) -> bool:
    probe = directory / "case-probe"
    probe.write_bytes(b"")
    try:
        return (directory / "CASE-PROBE").exists()
    finally:
        probe.unlink()


def test_comment_ids_that_differ_only_in_case_do_not_share_a_directory(tmp_path):
    if not case_insensitive(tmp_path):
        pytest.skip("the file system of the temporary directory is case-sensitive")
    store_results(tmp_path / "completed" / "comments" / "T1.json", [note("abc"), note("ABC", "other.pdf")])
    api = serve(FakeApi(files={}), note("abc"), note("ABC", "other.pdf"))
    with pytest.raises(StorageError, match="letter case"):
        download(tmp_path, api)
    assert files_under(tmp_path) == {"abc/preise.pdf": file_body("preise.pdf", 20)}
    assert api.file_requests == [url("abc")]


# --- Whole runs ----------------------------------------------------------------------------

LIST_URL = "https://files.todoist.com/fake/sync/6X7rfFVQXwRmcvFp/list.pdf"


def fixture_bytes(name: str) -> bytes:
    return (SYNC_FIXTURES / f"{name}.json").read_bytes()


def unchanged(token: str) -> bytes:
    return json.dumps({"sync_token": token, "full_sync": False, "items": [], "projects": []}).encode()


# The account of the sync fixtures, whose one task comment has the file `list.pdf`.
ACCOUNT = {"*": fixture_bytes("full_sync"), "FULL-1": fixture_bytes("incremental_edit"), "INC-1": unchanged("INC-1")}


def with_notes(*comments) -> dict:
    """ACCOUNT with more task comments in its full sync."""
    full = json.loads(fixture_bytes("full_sync"))
    full["notes"] = [*full["notes"], *comments]
    return {**ACCOUNT, "*": json.dumps(full).encode()}


def run_backup(target: Path, api: FakeApi, **client_options) -> FakeApi:
    sync.run_backup(target, Client(api, sleep=Waits(), jitter=lambda: 0.0, **client_options), now=lambda: NOW)
    return api


def cli(target: Path, api: FakeApi, monkeypatch, *options) -> int:
    monkeypatch.setenv("TODOIST_API_TOKEN", TOKEN)
    return main(
        ["backup", *options, str(target)],
        client_factory=lambda token, wait: Client(api, max_rate_limit_wait=wait, sleep=Waits()),
    )


# --- 4.3 Complete files only -----------------------------------------------------------------


def test_file_of_the_stated_size_is_stored(tmp_path):
    comment = note("N1", size=20480)
    store_note(tmp_path, comment)
    download(tmp_path, serve(FakeApi(files={}), comment))
    assert len(files_under(tmp_path)["N1/preise.pdf"]) == 20480


def test_file_without_a_stated_size_is_stored_as_received(tmp_path):
    comment = note("N1", size=None)
    store_note(tmp_path, comment)
    download(tmp_path, FakeApi(files={}).serve_file(url("N1"), b"any length"))
    assert files_under(tmp_path) == {"N1/preise.pdf": b"any length"}


def test_file_of_another_size_is_not_stored_and_requested_again(tmp_path, monkeypatch, caplog):
    account = with_notes(note("N1", size=20480))
    api = FakeApi(account).serve_file(url("N1"), b"x" * 10240)
    with caplog.at_level(logging.WARNING):
        assert cli(tmp_path, api, monkeypatch, "--quiet") == 0
    assert not (tmp_path / "attachments" / "N1").exists()
    assert warnings(caplog) == [
        "the attachment file of comment N1 was not downloaded: received 10240 bytes, but the file has 20480 bytes"
    ]
    assert api.file_requests == [LIST_URL, url("N1")]

    api = FakeApi(account).serve_file(url("N1"), b"x" * 20480)
    assert cli(tmp_path, api, monkeypatch, "--quiet") == 0
    assert api.file_requests == [url("N1")]
    assert len(files_under(tmp_path)["N1/preise.pdf"]) == 20480


def test_gzip_sent_anyway_is_stored_decoded(tmp_path):
    comment = note("N1", size=20480)
    store_note(tmp_path, comment)
    body = file_body("preise.pdf", 20480)
    download(tmp_path, FakeApi(files={}).serve_file(url("N1"), body, gzipped=True))
    assert files_under(tmp_path) == {"N1/preise.pdf": body}


def test_body_cut_off_by_the_network_is_downloaded_again_from_the_start(tmp_path):
    comment = note("N1", size=200000)
    store_note(tmp_path, comment)
    body = file_body("preise.pdf", 200000)
    api = FakeApi(files={}).serve_file(url("N1"), body).serve_file(url("N1"), body, fail_after=70000, times=1)
    client = download(tmp_path, api)
    assert api.file_requests == [url("N1"), url("N1")]
    assert client._sleep == [1.0]
    assert files_under(tmp_path) == {"N1/preise.pdf": body}


def test_killed_during_a_download_leaves_only_a_temp_file_and_the_next_run_completes_it(tmp_path):
    run_backup(tmp_path / "reference", FakeApi(ACCOUNT))
    # The last write but one of a first run is the attachment file; the last is state.json.
    writes = count_writes(tmp_path / "count")
    storage.set_fault_after(writes - 2)
    try:
        with pytest.raises(storage.InjectedFault):
            run_backup(tmp_path / "killed", FakeApi(ACCOUNT))
    finally:
        storage.set_fault_after(None)
    directory = tmp_path / "killed" / "attachments" / "6X7rfFVQXwRmcvFp"
    [left] = os.listdir(directory)
    assert left.startswith(".list.pdf.") and left.endswith(".tmp")

    api = run_backup(tmp_path / "killed", FakeApi(ACCOUNT))
    assert api.file_requests == [LIST_URL]
    assert files_under(tmp_path / "killed") == files_under(tmp_path / "reference")


def count_writes(target: Path) -> int:
    """The number of writes of a first run of ACCOUNT."""
    storage.set_fault_after(10_000)
    try:
        run_backup(target, FakeApi(ACCOUNT))
        return 10_000 - storage._fault_after
    finally:
        storage.set_fault_after(None)


# --- 4.3 Refused files produce a warning ----------------------------------------------------


@pytest.mark.parametrize("status", [401, 403, 404, 410, 400])
def test_refused_file_warns_and_the_run_succeeds(tmp_path, monkeypatch, caplog, status):
    refused, other = note("N1"), note("N2", "zwei.pdf")
    api = serve(FakeApi(with_notes(refused, other)), other).answer_file(url("N1"), status, b'{"error": "nope"}')
    with caplog.at_level(logging.WARNING):
        assert cli(tmp_path, api, monkeypatch, "--quiet") == 0
    assert warnings(caplog) == [
        f"the attachment file of comment N1 was not downloaded: GET files.todoist.com/fake/test/N1/preise.pdf "
        f"was refused (HTTP {status})"
    ]
    assert "TODOIST_API_TOKEN" not in caplog.text
    assert set(files_under(tmp_path)) == {"6X7rfFVQXwRmcvFp/list.pdf", "N2/zwei.pdf"}
    assert not (tmp_path / "attachments" / "N1").exists()
    assert json.loads((tmp_path / "state.json").read_bytes())["last_successful_run_utc"] is not None


def test_file_in_an_encoding_the_tool_cannot_read_warns_and_the_run_succeeds(tmp_path, monkeypatch, caplog):
    unreadable, other = note("N1"), note("N2", "zwei.pdf")
    api = serve(FakeApi(with_notes(unreadable, other)), other)
    api.answer_file(url("N1"), 200, b"\x8b\x00brotli", {"Content-Encoding": "br"})
    with caplog.at_level(logging.WARNING):
        assert cli(tmp_path, api, monkeypatch, "--quiet") == 0
    assert warnings(caplog) == [
        "the attachment file of comment N1 was not downloaded: GET files.todoist.com/fake/test/N1/preise.pdf "
        "cannot be used: the file host sent an unsupported Content-Encoding 'br'"
    ]
    assert api.file_requests.count(url("N1")) == 1  # not retried
    assert set(files_under(tmp_path)) == {"6X7rfFVQXwRmcvFp/list.pdf", "N2/zwei.pdf"}
    assert not (tmp_path / "attachments" / "N1").exists()


def test_file_url_with_spaces_and_umlauts_is_requested_percent_encoded(tmp_path):
    comment = note("N1", "Rechnung März.pdf", file_url=f"{FILES}/N1/Rechnung März.pdf")
    store_note(tmp_path, comment)
    api = FakeApi(files={}).serve_file(f"{FILES}/N1/Rechnung%20M%C3%A4rz.pdf", file_body("x", 20))
    download(tmp_path, api)
    assert files_under(tmp_path) == {"N1/Rechnung März.pdf": file_body("x", 20)}


def test_refused_file_is_requested_again_in_the_next_run(tmp_path):
    comment = note("N1")
    store_note(tmp_path, comment)
    download(tmp_path, FakeApi(files={}).answer_file(url("N1"), 404))
    assert not (tmp_path / "attachments").exists() or files_under(tmp_path) == {}

    api = serve(FakeApi(files={}), comment)
    download(tmp_path, api)
    assert api.file_requests == [url("N1")]
    assert list(files_under(tmp_path)) == ["N1/preise.pdf"]


@pytest.mark.parametrize(
    "location, reason",
    [("http://storage.example.net/preise.pdf", "not HTTPS"), ("https://hop.example.net/1", "after 5 redirects")],
)
def test_redirect_that_is_not_followed_warns(tmp_path, caplog, location, reason):
    store_note(tmp_path, note("N1"))
    api = FakeApi(files={}).redirect_file(url("N1"), location)
    for hop in range(1, 6):
        api.redirect_file(f"https://hop.example.net/{hop}", f"https://hop.example.net/{hop + 1}")
    with caplog.at_level(logging.WARNING):
        download(tmp_path, api)
    [warning] = warnings(caplog)
    assert warning.startswith("the attachment file of comment N1 was not downloaded: ") and reason in warning
    assert not (tmp_path / "attachments").exists() or files_under(tmp_path) == {}
    assert [auth for _url, auth in api.file_hops][1:] == [None] * (len(api.file_hops) - 1)


# --- 4.3 Persistent failures end the run ---------------------------------------------------


def test_network_down_during_the_downloads_ends_the_run(tmp_path, monkeypatch, capsys):
    first, second = note("N1"), note("N2", "zwei.pdf")
    api = serve(FakeApi(with_notes(first, second)), first).fail_file(url("N2", "zwei.pdf"))
    assert cli(tmp_path, api, monkeypatch, "--quiet") == 1
    assert set(files_under(tmp_path)) == {"6X7rfFVQXwRmcvFp/list.pdf", "N1/preise.pdf"}
    assert api.file_requests.count(url("N2", "zwei.pdf")) == 5
    assert capsys.readouterr().err.splitlines()[-1].startswith("ERROR cannot reach files.todoist.com")
    assert json.loads((tmp_path / "state.json").read_bytes())["last_successful_run_utc"] is None


def test_persistent_server_errors_end_the_run(tmp_path):
    store_note(tmp_path, note("N1"))
    api = FakeApi(files={}).answer_file(url("N1"), 503)
    with pytest.raises(Exception) as info:
        download(tmp_path, api)
    assert type(info.value).__name__ == "ApiError" and info.value.http_status == 503
    assert len(api.file_requests) == 5


def test_rate_limit_on_the_file_host_waits_and_counts_toward_the_cap(tmp_path):
    comment = note("N1")
    store_note(tmp_path, comment)
    api = serve(FakeApi(files={}), comment).answer_file(url("N1"), 429, headers={"Retry-After": "12"}, times=1)
    client = download(tmp_path, api)
    assert client._sleep == [12.0]
    assert client.rate_limit_waited == 12.0
    assert api.file_requests == [url("N1"), url("N1")]
    assert list(files_under(tmp_path)) == ["N1/preise.pdf"]


def test_rate_limit_cap_on_the_file_host_ends_the_run(tmp_path, monkeypatch):
    first, second = note("N1"), note("N2", "zwei.pdf")
    api = serve(FakeApi(with_notes(first, second)), first)
    api.answer_file(url("N2", "zwei.pdf"), 429, headers={"Retry-After": "600"})
    assert cli(tmp_path, api, monkeypatch, "--quiet", "--max-rate-limit-wait", "900") == 1
    assert "N1/preise.pdf" in files_under(tmp_path)


@pytest.mark.parametrize("error", [NetworkError, RateLimitExhausted])
def test_run_failure_keeps_the_files_stored_before(tmp_path, error):
    comments = [note("N1"), note("N2", "zwei.pdf")]
    store_results(tmp_path / "completed" / "comments" / "T1.json", comments)
    api = serve(FakeApi(files={}), comments[0])
    if error is NetworkError:
        api.fail_file(url("N2", "zwei.pdf"))
    else:
        api.answer_file(url("N2", "zwei.pdf"), 429, headers={"Retry-After": "60"})
    with pytest.raises(error):
        download(tmp_path, api, max_rate_limit_wait=30)
    assert list(files_under(tmp_path)) == ["N1/preise.pdf"]
    assert not (tmp_path / "attachments" / "N2").exists()


# --- 4.3 Attachment on an unexpected host ---------------------------------------------------


def test_file_on_a_foreign_host_is_not_requested(tmp_path, monkeypatch, caplog):
    [_link, _pending, foreign] = attachment_fixture("comments")["results"]
    api = FakeApi(with_notes(foreign))
    with caplog.at_level(logging.WARNING):
        assert cli(tmp_path, api, monkeypatch, "--quiet") == 0
    assert all("example.com" not in hop for hop, _auth in api.file_hops)
    assert warnings(caplog) == [
        f"the attachment file of comment {foreign['id']} was not downloaded: example.com is not a Todoist host"
    ]
    assert list(files_under(tmp_path)) == ["6X7rfFVQXwRmcvFp/list.pdf"]


def test_file_over_plain_http_is_not_requested(tmp_path, caplog):
    store_note(tmp_path, note("N1", file_url="http://files.todoist.com/fake/test/N1/preise.pdf"))
    api = FakeApi(files={})
    with caplog.at_level(logging.WARNING):
        download(tmp_path, api)
    assert api.file_hops == []
    assert warnings(caplog) == ["the attachment file of comment N1 was not downloaded: its URL does not use HTTPS"]


# --- 4.4 Messages ----------------------------------------------------------------------------


def test_log_of_a_run_with_downloads_a_stored_file_and_a_refusal(tmp_path, caplog):
    comments = [
        note("N1", size=300_000),
        note("N2", "zwei.pdf", size=200_000),
        note("N3", "drei.pdf"),
        note("N4", "vier.pdf"),
    ]
    store_results(tmp_path / "completed" / "comments" / "T1.json", comments)
    storage.write_bytes_atomic(tmp_path / "attachments" / "N3" / "drei.pdf", b"stored earlier")
    api = serve(FakeApi(files={}), *comments[:2]).answer_file(url("N4", "vier.pdf"), 404)
    with caplog.at_level(logging.DEBUG, logger="todoist_takeout.attachments"):
        download(tmp_path, api)
    assert [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "todoist_takeout.attachments"] == [
        ("INFO", "downloading 3 attachment files (1 already stored)"),
        ("DEBUG", "downloading the attachment file of comment N1 from files.todoist.com"),
        ("DEBUG", "downloading the attachment file of comment N2 from files.todoist.com"),
        ("DEBUG", "downloading the attachment file of comment N4 from files.todoist.com"),
        (
            "WARNING",
            "the attachment file of comment N4 was not downloaded: GET files.todoist.com/fake/test/N4/vier.pdf "
            "was refused (HTTP 404)",
        ),
        ("INFO", "downloaded 2 attachment files (0.5 MB); 1 could not be downloaded"),
    ]


def test_nothing_is_logged_when_every_file_is_stored(tmp_path, caplog):
    store_note(tmp_path, note("N1"))
    storage.write_bytes_atomic(tmp_path / "attachments" / "N1" / "preise.pdf", b"stored")
    with caplog.at_level(logging.DEBUG, logger="todoist_takeout.attachments"):
        download(tmp_path, FakeApi(files={}))
    assert [r for r in caplog.records if r.name == "todoist_takeout.attachments"] == []


def test_summary_is_logged_when_the_run_fails(tmp_path, caplog):
    comments = [note("N1"), note("N2", "zwei.pdf")]
    store_results(tmp_path / "completed" / "comments" / "T1.json", comments)
    api = serve(FakeApi(files={}), comments[0]).fail_file(url("N2", "zwei.pdf"))
    with caplog.at_level(logging.INFO, logger="todoist_takeout.attachments"), pytest.raises(NetworkError):
        download(tmp_path, api)
    assert caplog.records[-1].getMessage() == "downloaded 1 attachment files (0.0 MB); 0 could not be downloaded"


# --- 6.1 Crash and resume ----------------------------------------------------------------------


def whole_account() -> FakeApi:
    """The account of the sync, completed and archived fixtures, with four uploaded files. The comment
    with a file of the completed task is stored twice: in `completed/comments/`, and in the sync, which
    holds it from before its task was completed."""
    [priced] = [c for c in completed_fixture("comments")["results"] if c["file_attachment"]]
    return archived_answers(completed_answers(FakeApi(with_notes(priced)), NOW))


def stored_urls(target: Path) -> list[str]:
    """The URLs of the files that are stored, in the order of `attachments.collect`."""
    root = target / "attachments"
    names = set(os.listdir(root)) if root.is_dir() else set()
    return [
        a.url
        for a in attachments.collect(target)
        if attachments._holds_file(root, names, storage.encode_key(a.comment_id))
    ]


@pytest.fixture(scope="module")
def whole_run(tmp_path_factory):
    """The tree of an uninterrupted first run of `whole_account`, and the number of its writes."""
    target = tmp_path_factory.mktemp("reference")
    storage.set_fault_after(10_000)
    try:
        api = run_backup(target, whole_account())
        writes = 10_000 - storage._fault_after
    finally:
        storage.set_fault_after(None)
    # Each file is requested once, although one comment is stored twice.
    copies = [c["id"] for c in attachments._stored_comments(target) if isinstance(c, dict) and c.get("file_attachment")]
    assert len(copies) == 5 and len(set(copies)) == 4
    assert api.file_requests == stored_urls(target) == [a.url for a in attachments.collect(target)]
    assert len(api.file_requests) == 4
    assert len({path.split("/")[0] for path in files_under(target)}) == 4
    return snapshot(target), writes


def snapshot(target: Path) -> dict[str, bytes]:
    return {p.relative_to(target).as_posix(): p.read_bytes() for p in sorted(target.rglob("*")) if p.is_file()}


# The writes of a first run end with the four attachment files and state.json. A crash after
# `writes - n` writes hits, for n = 6 to 1: the last write before the downloads, each of the four
# files, and state.json.
@pytest.mark.parametrize("before_end", range(6, 0, -1))
def test_resume_after_a_crash_during_the_downloads_gives_the_uninterrupted_result(tmp_path, whole_run, before_end):
    reference, writes = whole_run
    storage.set_fault_after(writes - before_end)
    try:
        with pytest.raises(storage.InjectedFault):
            run_backup(tmp_path, whole_account())
    finally:
        storage.set_fault_after(None)
    stored = stored_urls(tmp_path)
    assert len(stored) == min(4, max(0, 5 - before_end))

    api = run_backup(tmp_path, whole_account())

    assert snapshot(tmp_path) == reference
    assert set(api.file_requests).isdisjoint(stored)
    assert api.file_requests == [u for u in stored_urls(tmp_path) if u not in stored]


# --- 6.2 Idempotence ----------------------------------------------------------------------------

OLD = 1_000_000_000


def test_repeat_run_requests_no_file_and_leaves_every_attachment_unchanged(tmp_path):
    run_backup(tmp_path, whole_account())
    paths = [p for p in (tmp_path / "attachments").rglob("*") if p.is_file()]
    for path in paths:
        os.utime(path, ns=(OLD, OLD))
    before = files_under(tmp_path)

    api = run_backup(tmp_path, whole_account())

    assert api.file_requests == [] and api.file_hops == []
    assert files_under(tmp_path) == before
    assert len(before) == 4
    assert [p for p in (tmp_path / "attachments").rglob("*") if p.is_file() and p.stat().st_mtime_ns != OLD] == []


def test_log_lines_hold_neither_the_token_nor_a_query_string(tmp_path, caplog):
    signed = "https://d1fake.cloudfront.net/fake/N1/preise.pdf?Expires=1&Signature=s3cr3t"
    store_note(tmp_path, note("N1"))
    api = FakeApi(files={}).redirect_file(url("N1"), signed).serve_file(signed, file_body("preise.pdf", 20))
    with caplog.at_level(logging.DEBUG):
        download(tmp_path, api)
    assert api.file_hops[0][1] == f"Bearer {FILE_TOKEN}"
    for record in caplog.records:
        assert FILE_TOKEN not in record.getMessage() and "s3cr3t" not in record.getMessage()
