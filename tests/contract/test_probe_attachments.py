"""Attachment probes R8 and W9 of add-attachment-downloads (design D11).

A spike, not a regression test; it is removed by task 7.4 of that change once its findings are in
the proposal. Run each probe alone:

    pytest -m contract tests/contract/test_probe_attachments.py -k r8   # TODOIST_CONTRACT_TOKEN
    pytest -m contract tests/contract/test_probe_attachments.py -k w9   # the write account, behind the guard

R8 reads the comments of the sync, of recent completed tasks and of archived projects, describes
every `file_attachment`, and requests up to five uploaded files into a temporary directory, which
pytest removes. It sends no command.

W9 uploads a small text file and a small PNG through `POST /api/v1/uploads`, adds them and a link
as comments, and describes them as R8 does. It then compares `file_url` after a full sync and after
archiving the project, deletes one comment and requests its file again, and removes everything it
created.

The findings, in `findings/probe-attachments-read.json` and `findings/probe-attachments-write.json`,
hold booleans, counts, types, statuses and host names only; `probes.check_findings` refuses any
other document. Each test fails only when a probe crashes, a findings document is unsafe, or the
cleanup fails.
"""

import json
import struct
import time
import urllib.parse
import zlib
from datetime import datetime, timezone

import pytest

from todoist_takeout.api import BASE_URL
from todoist_takeout.errors import ApiError

PREFIX = "todoist-takeout probe"
TEXT_NAME = "probe-note.txt"
PNG_NAME = "probe-pixel.png"
TEXT = b"todoist-takeout attachment probe\n" * 64
LINK_URL = "https://example.com/todoist-takeout-probe"
# Seconds between steps, so that a URL with a time in it would change.
PAUSE = 2.0


def png_pixel() -> bytes:
    """A valid 1x1 PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
            + chunk(b"IEND", b""))


def find(objects, object_id):
    return next((o for o in objects or [] if isinstance(o, dict) and str(o.get("id")) == str(object_id)), None)


def write_safely(probes, write_findings, name, findings, secrets, token):
    problems = probes.check_findings(findings, secrets)
    if problems:
        return problems
    write_findings(name, findings, token)
    return []


def test_r8_read(contract_support, probes, client_for, tmp_path, write_findings):
    token = contract_support.env_token(contract_support.READ_TOKEN)
    if not token:
        pytest.skip(f"{contract_support.READ_TOKEN} is not set")
    secrets = probes.Secrets()
    findings, crashed = probes.probe_read(client_for(token), probes.RawHttp(tmp_path), token,
                                          datetime.now(timezone.utc), secrets)
    problems = write_safely(probes, write_findings, "probe-attachments-read", findings, secrets, token)
    assert not problems, f"findings not written, they may hold account data: {problems}"
    assert not crashed, f"probes crashed: {crashed}"


def test_w9_write(write_client, contract_support, probes, tmp_path, write_findings):
    token, client = write_client
    commands = contract_support.Commands(client)
    http = probes.RawHttp(tmp_path)
    secrets = probes.Secrets()
    sections = probes.Sections()
    findings: dict = {}
    projects: list[str] = []
    file_urls: list[str] = []
    notes: dict[str, str | None] = {}
    first: dict[str, dict] = {}
    for value in (PREFIX, TEXT_NAME, PNG_NAME, TEXT, LINK_URL):
        secrets.add(value)

    def add(command, args, what):
        new = commands.add(command, args)
        probes.require(new, f"{command} for {what} was rejected")
        secrets.add(str(new))
        return str(new)

    def upload(name, content, content_type, project):
        body, header = probes.multipart({"file_name": name, "project_id": project}, "file", name, content,
                                        content_type)
        answer = http.request("POST", urllib.parse.urljoin(BASE_URL, probes.UPLOADS), token=token,
                              headers={"Content-Type": header}, data=body, keep_body=True)
        result = json.loads(answer.body) if 200 <= answer.status < 300 else None
        secrets.add(result)
        if isinstance(result, dict) and isinstance(result.get("file_url"), str):
            file_urls.append(result["file_url"])
        return {
            "status": answer.status,
            "attachment": probes.attachment_facts(result),
            "file_size_equals_uploaded_length": isinstance(result, dict) and result.get("file_size") == len(content),
        }, result

    def described(objects):
        return {name: probes.attachment_facts((find(objects, note) or {}).get("file_attachment"))
                for name, note in notes.items() if note}

    def urls_equal(objects):
        return {name: ((find(objects, notes[name]) or {}).get("file_attachment") or {}).get("file_url")
                == att["file_url"] for name, att in first.items()}

    try:
        with sections.run(findings, "setup") as entry:
            project = add("project_add", {"name": f"{PREFIX}: attachments"}, "a project")
            projects.append(project)
            task = add("item_add", {"content": f"{PREFIX}: task", "project_id": project}, "a task")
            observer = contract_support.Observer(client)
            entry["ok"] = True

        with sections.run(findings, "W9_upload") as entry:
            entry["text"], text = upload(TEXT_NAME, TEXT, "text/plain", project)
            entry["png"], png = upload(PNG_NAME, png_pixel(), "image/png", project)
            for name, attachment in (("text", text), ("png", png)):
                if isinstance(attachment, dict):
                    notes[name] = commands.add("note_add", {"item_id": task, "content": f"{PREFIX}: {name}",
                                                            "file_attachment": attachment})
                    secrets.add(notes[name])
            link = {"resource_type": "url", "url": LINK_URL, "title": f"{PREFIX}: link"}
            notes["link"] = commands.add("note_add", {"item_id": task, "content": f"{PREFIX}: link",
                                                      "file_attachment": link})
            secrets.add(notes["link"])
            entry["comments_created"] = {name: note is not None for name, note in notes.items()}
            synced = observer.changes().get("notes")
            secrets.add(synced)
            entry["in_sync"] = described(synced)
            listed = probes.comments_of(client, task_id=task)
            secrets.add(listed)
            entry["in_comments_endpoint"] = described(listed)
            for name in ("text", "png"):
                stored = (find(synced, notes.get(name)) or {}).get("file_attachment")
                if probes.is_upload(stored):
                    first[name] = stored
            entry["sync_file_url_equals_upload"] = {
                name: att["file_url"] == (text if name == "text" else png).get("file_url") for name, att in first.items()
            }
            entry["comments_endpoint_file_url_equals_sync"] = urls_equal(listed)

        with sections.run(findings, "W9_download") as entry:
            for name, attachment in first.items():
                entry[name] = probes.probe_file(http, attachment, token, try_gzip=name == "text")
            entry["link"] = probes.url_facts(LINK_URL) | {"requested": False}

        with sections.run(findings, "W9_stability") as entry:
            time.sleep(PAUSE)
            full = json.loads(client.sync("*")).get("notes")
            secrets.add(full)
            entry["full_sync_file_url_equal"] = urls_equal(full)
            entry["archive_accepted"] = bool(commands.run("project_archive", {"id": project}))
            time.sleep(PAUSE)
            after = probes.comments_of(client, task_id=task)
            secrets.add(after)
            entry["after_archiving_file_url_equal"] = urls_equal(after)
            entry["after_archiving_in_sync"] = described(observer.changes().get("notes"))
            entry["after_archiving_download"] = {
                name: probes.fetch_chain(http, attachment["file_url"], token=token,
                                         file_size=attachment.get("file_size"))["ended"]
                for name, attachment in first.items()
            }

        with sections.run(findings, "W9_deleted_comment") as entry:
            name = "png" if "png" in first else next(iter(first), None)
            probes.require(name, "no uploaded comment to delete")
            accepted = bool(commands.run("note_delete", {"id": notes[name]}))
            entry["delete_accepted_in_archived_project"] = accepted
            if not accepted:
                entry["unarchive_accepted"] = bool(commands.run("project_unarchive", {"id": project}))
                accepted = bool(commands.run("note_delete", {"id": notes[name]}))
            entry["delete_accepted"] = accepted
            changes = observer.changes()
            secrets.add(changes.get("notes"))
            deleted = find(changes.get("notes"), notes[name])
            entry["in_sync"] = {"present": deleted is not None,
                                "is_deleted": (deleted or {}).get("is_deleted"),
                                "attachment": probes.attachment_facts((deleted or {}).get("file_attachment"))}
            try:
                secrets.add(probes.comment_by_id(client, notes[name]))
                entry["comment_endpoint"] = "found"
            except ApiError as exc:
                entry["comment_endpoint"] = f"HTTP {exc.http_status}"
            entry["download"] = probes.fetch_chain(http, first[name]["file_url"], token=token,
                                                   file_size=first[name].get("file_size"))
    finally:
        failed = []
        for project_id in reversed(projects):
            if commands.run("project_delete", {"id": project_id}):
                continue
            commands.run("project_unarchive", {"id": project_id})
            if not commands.run("project_delete", {"id": project_id}):
                failed.append(project_id)
        cleanup: dict = {"uploads_deleted": [], "file_after_upload_delete": []}
        for url in file_urls:
            query = urllib.parse.urlencode({"file_url": url})
            try:
                answer = http.request("DELETE", urllib.parse.urljoin(BASE_URL, f"{probes.UPLOADS}?{query}"),
                                      token=token)
                cleanup["uploads_deleted"].append(answer.status)
                cleanup["file_after_upload_delete"].append(probes.fetch_chain(http, url, token=token)["ended"])
            except OSError as exc:
                cleanup["uploads_deleted"].append(type(exc).__name__)
        created = set(projects) | {str(n) for n in notes.values() if n}
        full = json.loads(client.sync("*"))
        listed_archived = json.loads(client.get("projects/archived", {"limit": "200"})).get("results") or []
        cleanup["nothing_left_in_sync"] = not any(
            str(obj.get("id")) in created or str(obj.get("project_id")) in set(projects)
            for resource in ("projects", "items", "notes", "project_notes")
            for obj in full.get(resource) or [] if isinstance(obj, dict))
        cleanup["nothing_left_archived"] = not any(str(p.get("id")) in created for p in listed_archived
                                                   if isinstance(p, dict))
        cleanup["projects_not_deleted"] = len(failed)
        findings["cleanup"] = cleanup
        findings["rejected_commands"] = [r.command for r in commands.rejected]
        problems = write_safely(probes, write_findings, "probe-attachments-write", findings, secrets, token)

    assert not failed, f"delete these projects of the test account by hand: {failed}"
    assert cleanup["nothing_left_in_sync"] and cleanup["nothing_left_archived"], "the probe left objects behind"
    assert not problems, f"findings not written, they may hold account data: {problems}"
    assert not sections.crashed, f"probes crashed: {sections.crashed}"
