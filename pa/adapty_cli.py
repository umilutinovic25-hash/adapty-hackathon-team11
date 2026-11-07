"""Thin wrapper around the Adapty Developer CLI (`adapty ... --json`)."""
from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager

DEFAULT_APP_ID = os.getenv("ADAPTY_APP_ID", "f414265a-e67a-41ce-85b1-c4c1d02c3816")
APP_ID = DEFAULT_APP_ID  # the app the next calls act on; switch with use_app()


class AdaptyError(RuntimeError):
    """A failed CLI call, with Adapty's own message when it sent one."""


def _message(raw: str) -> str:
    try:
        err = json.loads(raw).get("error", {})
        return err.get("message") or raw
    except (ValueError, AttributeError):
        return raw


def _exec(args: list[str]) -> dict:
    out = subprocess.run(["adapty", *args, "--json"], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise AdaptyError(_message((out.stderr or out.stdout).strip()))
    return json.loads(out.stdout) if out.stdout.strip() else {}


def run(*args: str) -> dict:
    return _exec([*args, "--app", APP_ID])


@contextmanager
def use_app(app_id: str):
    global APP_ID
    previous, APP_ID = APP_ID, app_id
    try:
        yield
    finally:
        APP_ID = previous


def list_apps() -> list[dict]:
    return _exec(["apps", "list"])["data"]


def list_tests() -> list[dict]:
    tests = run("ab-tests", "list")["data"]
    return [run("ab-tests", "get", t["ab_test_id"]) for t in tests]


def stop_command(test_id: str, winner_id: str) -> list[str]:
    return ["adapty", "ab-tests", "stop", test_id, "--app", APP_ID, "--winner", winner_id, "--yes", "--json"]


def stop_test(test_id: str, winner_id: str) -> dict:
    out = subprocess.run(stop_command(test_id, winner_id), capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise AdaptyError(_message((out.stderr or out.stdout).strip()))
    return json.loads(out.stdout) if out.stdout.strip() else {"ok": True}
