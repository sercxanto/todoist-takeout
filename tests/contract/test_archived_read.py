"""Read-only contract test: the archived-projects endpoints against the shapes in tests/fixtures/shapes.json.

It sends the requests of `scripts/record_fixtures.py record`: the first page of archived projects
and the `/full` of up to all of them, comparing the one with the most content. It fails on added
keys, fields or types (add-archived-projects-backup, design D14).
"""

import json
import warnings

import pytest


@pytest.mark.parametrize("account", ["read", "write"])
def test_archived_endpoints_match_recorded_shapes(account, contract_support, recorder, client_for, write_findings):
    variable = contract_support.ACCOUNT_VARIABLES[account]
    token = contract_support.env_token(variable)
    if not token:
        pytest.skip(f"{variable} is not set")

    bodies = recorder.record_archived(client_for(token))

    expected = json.loads(recorder.SHAPES.read_bytes())["endpoints"]
    findings, added = {}, []
    for name, (new, removed) in recorder.compare_endpoint_bodies(expected, bodies).items():
        findings[name] = {"shape_sources": expected[name]["sources"], "added": new, "removed": removed}
        added += [f"{name}: {line}" for line in new]
        for line in removed:
            warnings.warn(f"{account} account, {name}: not in the response: {line}")
    for name in sorted(recorder.ARCHIVED_ENDPOINTS.keys() - bodies.keys()):
        findings[name] = "not requested"
        warnings.warn(f"{account} account: no archived project found; {name} not compared")
    write_findings(f"archived-read-{account}", findings, token)

    assert not added, "the responses have keys, fields or types that shapes.json lacks:\n" + "\n".join(added)
