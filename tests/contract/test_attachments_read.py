"""Read-only contract test of the attachment download (add-attachment-downloads, task 7.2).

It looks for an uploaded file among the comments of a full sync, downloads the first one on a
Todoist host through the tool's `Client.download` into a temporary directory, checks its length
against `file_size` and the shape of the uploads' `file_attachment` against shapes.json, and removes
the file again. It skips when the synced comments hold no uploaded file. It sends no command. The
findings hold counts and booleans only.

The write test `test_attachments_write.py` runs the same download check on a file it uploads.
"""

import json
import warnings

import pytest

from todoist_takeout import attachments
from todoist_takeout.api import is_todoist_file_url


@pytest.mark.parametrize("account", ["read", "write"])
def test_one_attachment_downloads_with_its_stated_size(
    account, contract_support, recorder, client_for, tmp_path, write_findings
):
    variable = contract_support.ACCOUNT_VARIABLES[account]
    token = contract_support.env_token(variable)
    if not token:
        pytest.skip(f"{variable} is not set")
    client = client_for(token)

    response = json.loads(client.sync("*"))
    comments = [c for resource in attachments.SYNC_COMMENT_RESOURCES for c in response.get(resource) or []]
    uploads = [a for a in map(attachments.uploaded_file, comments) if a is not None]
    candidates = [a for a in uploads if is_todoist_file_url(a.url)]
    findings: dict = {"comments": len(comments), "uploads": len(uploads), "on_todoist_hosts": len(candidates)}
    added: list[str] = []
    if uploads:
        expected = json.loads(recorder.SHAPES.read_bytes())["file_attachments"]
        added, removed = recorder.compare_shapes(
            expected, recorder.attachment_shape_of(recorder.uploaded_attachments(response))
        )
        findings |= {"shape_sources": expected["sources"], "added": added, "removed": removed}
        for line in removed:
            warnings.warn(f"{account} account: not in the uploads' file_attachment: {line}")
    if candidates:
        findings["download"] = contract_support.check_download(client, candidates[0], tmp_path / "download")
    write_findings(f"attachments-read-{account}", findings, token)

    if not candidates:
        pytest.skip("the synced comments hold no uploaded file on a Todoist host")
    assert not (tmp_path / "download").exists(), "the download left something behind"
    assert not added, "the uploads have fields or types that shapes.json lacks:\n" + "\n".join(added)
    if findings["download"]["size_stated"]:
        assert findings["download"]["size_matches"]
