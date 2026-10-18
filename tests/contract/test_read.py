"""Read-only contract test: the shape of a real full sync against tests/fixtures/shapes.json."""

import json
import warnings

import pytest


@pytest.mark.parametrize("account", ["read", "write"])
def test_full_sync_matches_recorded_shapes(account, contract_support, recorder, client_for, write_findings):
    variable = contract_support.ACCOUNT_VARIABLES[account]
    token = contract_support.env_token(variable)
    if not token:
        pytest.skip(f"{variable} is not set")

    response = json.loads(client_for(token).sync("*"))
    actual = recorder.shape_of(response)
    expected = json.loads(recorder.SHAPES.read_bytes())
    added, removed = recorder.compare_shapes(expected, actual)
    unclassified = sorted(key for key, entry in actual["keys"].items() if entry["kind"] == "unclassified")
    write_findings(
        f"read-{account}",
        {
            "shape_sources": expected["sources"],
            "added": added,
            "removed": removed,
            "unclassified": unclassified,
            "user_plan_limits": recorder.scrub(response.get("user_plan_limits")),
        },
        token,
    )

    for line in removed:
        warnings.warn(f"{account} account: not in the response: {line}")
    assert not unclassified, f"keys that resources.py classifies only by the fallback: {unclassified}"
    assert not added, "the response has keys, fields or types that shapes.json lacks:\n" + "\n".join(added)
