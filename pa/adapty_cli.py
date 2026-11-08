"""Thin wrapper around the Adapty Developer CLI (`adapty ... --json`).

The app a call acts on lives in a ContextVar, not a module global: Streamlit serves every browser session from a
thread of the same process, so one session switching apps must never redirect another session's calls (or its
irreversible `stop`). Worker threads get the caller's app through `in_current_app`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar

DEFAULT_APP_ID = os.getenv("ADAPTY_APP_ID", "f414265a-e67a-41ce-85b1-c4c1d02c3816")
_current_app: ContextVar[str] = ContextVar("adapty_app", default=DEFAULT_APP_ID)


class AdaptyError(RuntimeError):
    """A failed CLI call, with Adapty's own message when it sent one."""


def _message(raw: str) -> str:
    try:
        err = json.loads(raw).get("error", {})
        return err.get("message") or raw
    except (ValueError, AttributeError):
        return raw


MAX_CONCURRENT_CALLS = 2  # be nice to Adapty's API: it throttles bursts of parallel requests
MAX_RETRIES = 4
READ_TIMEOUT_SECONDS = 30
_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CALLS)


def _throttle_wait(message: str) -> float | None:
    """Seconds to wait if Adapty throttled the call ("… Expected available in N second(s)."), else None."""
    if "throttled" not in message.lower():
        return None
    m = re.search(r"available in (\d+(?:\.\d+)?)", message)
    return float(m.group(1)) + 0.25 if m else 1.0


def _call(cmd: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess:
    """One CLI call: capped concurrency, optional read timeout, and retries when Adapty asks us to slow down."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            with _slots:
                out = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
        except subprocess.TimeoutExpired as err:
            raise AdaptyError(f"Adapty read timed out after {timeout} seconds") from err
        except FileNotFoundError as err:  # CLI not installed or not on PATH
            raise AdaptyError("the Adapty CLI is not available (npm i -g adapty)") from err
        wait = _throttle_wait((out.stderr or out.stdout or "")) if out.returncode != 0 else None
        if wait is None or attempt == MAX_RETRIES:
            return out
        time.sleep(wait * (attempt + 1))
    return out


def _exec(args: list[str]) -> dict:
    out = _call(["adapty", *args, "--json"], timeout=READ_TIMEOUT_SECONDS)
    if out.returncode != 0:
        raise AdaptyError(_message((out.stderr or out.stdout).strip()))
    try:
        return json.loads(out.stdout) if out.stdout.strip() else {}
    except ValueError as err:
        raise AdaptyError(f"unexpected CLI output: {out.stdout[:200]}") from err


def current_app() -> str:
    return _current_app.get()


def run(*args: str) -> dict:
    return _exec([*args, "--app", current_app()])


@contextmanager
def use_app(app_id: str):
    token = _current_app.set(app_id)
    try:
        yield
    finally:
        _current_app.reset(token)


def in_current_app(fn):
    """Wrap fn so it runs with the caller's app inside a worker thread (threads don't inherit context)."""
    app = current_app()

    def wrapped(*args, **kwargs):
        with use_app(app):
            return fn(*args, **kwargs)
    return wrapped


def list_apps() -> list[dict]:
    return _exec(["apps", "list"])["data"]


def list_tests() -> list[dict]:
    tests = run("ab-tests", "list")["data"]
    with ThreadPoolExecutor(max_workers=8) as pool:  # each CLI call is ~0.5 s of waiting
        return list(pool.map(in_current_app(lambda t: run("ab-tests", "get", t["ab_test_id"])), tests))


def stop_command(test_id: str, winner_id: str, app_id: str | None = None) -> list[str]:
    return ["adapty", "ab-tests", "stop", test_id, "--app", app_id or current_app(), "--winner", winner_id,
            "--yes", "--json"]


def stop_test(test_id: str, winner_id: str, app_id: str | None = None) -> dict:
    """Irreversible. Callers should pass app_id explicitly. Exit code 0 is the success signal: once Adapty has
    stopped the test, unexpected stdout must not raise, or the caller would skip the audit log."""
    out = _call(stop_command(test_id, winner_id, app_id))
    if out.returncode != 0:
        raise AdaptyError(_message((out.stderr or out.stdout).strip()))
    try:
        res = json.loads(out.stdout) if out.stdout.strip() else {}
    except ValueError:
        return {"ok": True, "raw": out.stdout.strip()[:500]}
    return res if isinstance(res, dict) and res else {"ok": True, "raw": res}
