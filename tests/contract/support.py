"""Helpers for the contract tests: account variables, the write-account guard, sync commands.

The offline test `tests/test_contract_support.py` checks the guard, so that a misconfigured shell
can never send write commands to the read account.
"""

import json
import os
import uuid
from dataclasses import dataclass, field

from todoist_takeout.api import Client
from todoist_takeout.errors import ApiError

READ_TOKEN = "TODOIST_CONTRACT_TOKEN"
WRITE_TOKEN = "TODOIST_CONTRACT_WRITE_TOKEN"
ALLOW_WRITES = "TODOIST_CONTRACT_ALLOW_WRITES"

ACCOUNT_VARIABLES = {"read": READ_TOKEN, "write": WRITE_TOKEN}


class AccountGuardError(Exception):
    """The write account is, or may be, the read account."""


def env_token(variable: str, environ=os.environ) -> str:
    return environ.get(variable, "").strip()


def writes_allowed(environ=os.environ) -> bool:
    return environ.get(ALLOW_WRITES, "").strip() == "1"


def user_id(client: Client):
    """The `user.id` of the token's account, from a read-only sync of the user resource only."""
    response = json.loads(client.post("sync", {"sync_token": "*", "resource_types": '["user"]'}))
    return response["user"]["id"]


def check_write_account(write_token: str, read_token: str, client_for) -> None:
    """Raise AccountGuardError unless the write token clearly belongs to another account.

    Both tokens are required: without the read token, nothing shows that the write token is not the
    personal one. `client_for(token)` returns a Client. Only read-only requests are made.
    """
    if not write_token:
        raise AccountGuardError(f"{WRITE_TOKEN} is not set")
    if not read_token:
        raise AccountGuardError(
            f"{READ_TOKEN} is not set; the write test needs it to check that {WRITE_TOKEN} "
            "belongs to another account"
        )
    if write_token == read_token:
        raise AccountGuardError(f"{WRITE_TOKEN} equals {READ_TOKEN}; writes must go to a dedicated test account")
    if user_id(client_for(write_token)) == user_id(client_for(read_token)):
        raise AccountGuardError(
            f"{WRITE_TOKEN} and {READ_TOKEN} belong to the same Todoist user; "
            "writes must go to a dedicated test account"
        )


@dataclass
class Rejected:
    """A command the API did not execute, with the error it gave."""

    command: str
    error: object

    def as_finding(self) -> dict:
        return {"command": self.command, "error": self.error}


@dataclass
class Commands:
    """Sends sync commands one at a time and keeps every rejection."""

    client: Client
    rejected: list[Rejected] = field(default_factory=list)

    def run(self, command: str, args: dict, *, temp_id: str | None = None):
        """Run one command. Return the new object's ID for adds, True for others, None if rejected."""
        entry = {"type": command, "uuid": str(uuid.uuid4()), "args": args}
        if temp_id is not None:
            entry["temp_id"] = temp_id
        try:
            response = json.loads(self.client.post("sync", {"commands": json.dumps([entry])}))
        except ApiError as exc:
            error = {
                "http_status": exc.http_status,
                "error": exc.error,
                "error_tag": exc.error_tag,
                "error_code": exc.error_code,
            }
            self.rejected.append(Rejected(command, error))
            return None
        status = response.get("sync_status", {}).get(entry["uuid"])
        if status != "ok":
            self.rejected.append(Rejected(command, status))
            return None
        if temp_id is not None:
            return response.get("temp_id_mapping", {}).get(temp_id)
        return True

    def add(self, command: str, args: dict):
        return self.run(command, args, temp_id=str(uuid.uuid4()))
