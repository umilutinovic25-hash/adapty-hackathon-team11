"""Thin wrapper around the Adapty Developer CLI (`adapty ... --json`)."""
from __future__ import annotations

import json
import os
import subprocess

APP_ID = os.getenv("ADAPTY_APP_ID", "f414265a-e67a-41ce-85b1-c4c1d02c3816")


def run(*args: str) -> dict:
    out = subprocess.run(["adapty", *args, "--app", APP_ID, "--json"], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise RuntimeError(f"adapty {' '.join(args)} failed: {out.stderr.strip() or out.stdout.strip()}")
    return json.loads(out.stdout)


def list_tests() -> list[dict]:
    tests = run("ab-tests", "list")["data"]
    return [run("ab-tests", "get", t["ab_test_id"]) for t in tests]


def stop_command(test_id: str, winner_id: str) -> list[str]:
    return ["adapty", "ab-tests", "stop", test_id, "--app", APP_ID, "--winner", winner_id, "--yes", "--json"]


def stop_test(test_id: str, winner_id: str) -> dict:
    out = subprocess.run(stop_command(test_id, winner_id), capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or out.stdout.strip())
    return json.loads(out.stdout) if out.stdout.strip() else {"ok": True}
