"""Behaviour tests with the Adapty CLI mocked from fixtures captured on the Lumen app (no network, no side effects)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pa import adapty_cli, cli, experiments

FIX = Path(__file__).parent / "fixtures"
TESTS = json.loads((FIX / "ab_tests.json").read_text())
SEGMENTS = json.loads((FIX / "segments.json").read_text())
PLACEMENTS = json.loads((FIX / "placements.json").read_text())
ONB, MAIN, SETTINGS = ("Onboarding: annual trial vs weekly first", "Main: 3 plans vs lifetime anchor (NA)",
                       "Settings: upgrade vs lifetime anchor")


@pytest.fixture(scope="module")
def data():
    u, t, end = experiments.load()
    return experiments.add_intent(u), t


def fake_run(*args):
    if args[:2] == ("segments", "list"):
        return SEGMENTS
    if args[:2] == ("placements", "list"):
        return PLACEMENTS
    if args[:2] == ("analytics", "chart"):
        return {"data": {"segments": []}}
    if args[:3] == ("flows", "metrics", "get"):
        return {"data": {"items": []}}
    raise AssertionError(f"unexpected CLI call {args}")


@pytest.fixture
def mocked(monkeypatch, tmp_path):
    state = {"tests": copy.deepcopy(TESTS), "stopped": [], "apps": []}
    monkeypatch.setattr(adapty_cli, "list_tests", lambda: copy.deepcopy(state["tests"]))
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    monkeypatch.setattr(adapty_cli, "stop_test",
                        lambda tid, wid, app_id=None: state["stopped"].append((tid, wid)) or state["apps"].append(app_id)
                        or {"ok": True})
    monkeypatch.chdir(tmp_path)
    return state


def by_title(report):
    return {t["title"]: t for t in report["tests"]}


def test_pre_demo_verdicts_and_money(mocked):
    r = experiments.build_report()
    t = by_title(r)
    assert t[ONB]["verdict"] == "stop_and_roll_out"
    assert t[MAIN]["verdict"] == "stop_and_roll_out"
    assert t[SETTINGS]["verdict"] == "completed_inconclusive"
    assert r["total_lost_per_month_usd"] == pytest.approx(1749.31, abs=1)
    assert Path("out/report.json").exists() and not Path("out/report.tmp").exists()


def test_immature_revenue_warning_only_where_trials_differ(mocked):
    t = by_title(experiments.build_report())
    assert t[ONB]["maturity"]["early_bias"] and "Weekly first" in t[ONB]["maturity"]["early_bias"]
    assert t[MAIN]["maturity"]["early_bias"] is None


def test_guardrails_are_per_charge(mocked):
    g = by_title(experiments.build_report())[ONB]["guardrails"]
    assert g["winner"]["billing_issue_rate"] == pytest.approx(g["winner"]["billing_issues"] / g["winner"]["paid_tx"], abs=1e-4)
    assert g["failed"] == []


def test_after_stop_test_is_completed_and_money_drops(mocked):
    for x in mocked["tests"]:
        if x["title"] == ONB:
            x["state"] = "completed"
    r = experiments.build_report()
    assert by_title(r)[ONB]["verdict"] == "completed_confirmed"
    assert r["total_lost_per_month_usd"] == pytest.approx(353.63, abs=1)


def test_plan_refuses_anything_not_decided(mocked):
    experiments.build_report()
    settings_id = next(x["ab_test_id"] for x in TESTS if x["title"] == SETTINGS)
    with pytest.raises(SystemExit):
        cli.plan(cli.report(fresh=False), settings_id)


def test_stop_rejudges_and_refuses_if_test_changed(mocked, monkeypatch):
    onb_id = next(x["ab_test_id"] for x in TESTS if x["title"] == ONB)
    for x in mocked["tests"]:
        if x["title"] == ONB:
            x["state"] = "completed"  # someone stopped it in the dashboard meanwhile
    monkeypatch.setattr("sys.argv", ["pa.cli", "stop", onb_id, "--winner-id", "x", "--approved-by", "tester"])
    with pytest.raises(SystemExit):
        cli.main()
    assert mocked["stopped"] == []


def test_stop_refuses_a_winner_nobody_approved(mocked, monkeypatch):
    main_test = next(x for x in TESTS if x["title"] == MAIN)
    loser = next(a["flow_id"] for a in main_test["flows"] if a["flow_name"] == "Main - 3 plans")
    monkeypatch.setattr("sys.argv", ["pa.cli", "stop", main_test["ab_test_id"], "--winner-id", loser,
                                     "--approved-by", "tester"])
    with pytest.raises(SystemExit, match="not the approved winner"):
        cli.main()
    assert mocked["stopped"] == []


def test_stop_calls_adapty_with_winner_and_writes_audit(mocked, monkeypatch):
    main_test = next(x for x in TESTS if x["title"] == MAIN)
    winner = next(a["flow_id"] for a in main_test["flows"] if a["flow_name"] == "Main - Lifetime anchor")
    monkeypatch.setattr("sys.argv", ["pa.cli", "stop", main_test["ab_test_id"], "--winner-id", winner,
                                     "--approved-by", "tester"])
    cli.main()
    assert mocked["stopped"] == [(main_test["ab_test_id"], winner)]
    assert mocked["apps"] == [adapty_cli.DEFAULT_APP_ID]  # the app is passed explicitly, not via a global
    entry = json.loads(Path("out/audit.log").read_text().splitlines()[-1])
    assert entry["approved_by"] == "tester" and entry["winner"] == "Main - Lifetime anchor"


def test_draft_plan_counts_only_its_placement(mocked, data):
    u, _ = data
    d = next(t for t in experiments.build_report()["tests"] if t["state"] == "draft")
    assert d["placement_code"] == "ai_limit_reached"
    pl = u[u.placement == "ai_limit_reached"]
    expected_daily = len(pl) / (pl.install_at.max() - pl.install_at.min()).days * 0.1
    assert d["daily_users_at_placement"] == pytest.approx(expected_daily, abs=0.2)
    assert d["verdict"] == "draft_plan" and d["recommendation"].startswith("don't start")


def test_paywall_test_is_shown_as_unsupported_not_dropped(mocked):
    pw = copy.deepcopy(next(x for x in TESTS if x["title"] == MAIN))
    pw.update(ab_test_id="pw-1", title="Paywall price test", type="paywall", flows=[],
              paywalls=[{"paywall_id": "p1", "paywall_name": "A", "weight": 50},
                        {"paywall_id": "p2", "paywall_name": "B", "weight": 50}])
    mocked["tests"].append(pw)
    t = by_title(experiments.build_report())
    assert t["Paywall price test"]["verdict"] == "unsupported"


def test_srm_accepts_weights_not_summing_to_100(mocked):
    for x in mocked["tests"]:
        if x["title"] == MAIN:
            for a in x["flows"]:
                a["weight"] = 1  # shares instead of percentages
    t = by_title(experiments.build_report())
    assert t[MAIN]["health"]["srm_pvalue"] > 0.01


def test_segment_reason_does_not_count_unclear_as_holding(mocked):
    t = by_title(experiments.build_report())[SETTINGS]
    line = next(x for x in t["reasons"] if x.startswith("segments"))
    assert f"unclear in {t['segments_unclear']}" in line and t["segments_unclear"] > 0


def test_old_report_schema_is_rebuilt(mocked):
    Path("out").mkdir()
    Path("out/report.json").write_text(json.dumps({"tests": []}))  # written by an older version
    assert cli.report(fresh=False)["schema"] == experiments.REPORT_SCHEMA


def test_live_view_counts_only_users_inside_the_test(monkeypatch):
    tree = {"data": {"items": [
        {"level": "audience", "title": "All Users", "unique_profiles_views": 135, "revenue": 36.0, "items": []},
        {"level": "audience", "title": "US & Canada", "items": [
            {"level": "ab_test", "title": MAIN, "unique_profiles_views": 40, "revenue": 63.0, "trials": 2,
             "trials_finished": 0, "purchases": 3}]}]}}
    monkeypatch.setattr(adapty_cli, "run", lambda *a: tree)
    m = experiments.flow_live_metrics("flow-a", MAIN, "2026-11-06")
    assert m["users"] == 40 and m["revenue"] == 63.0 and m["rev_per_user"] == pytest.approx(63 / 40, abs=0.01)


def test_other_apps_have_no_borrowed_history(mocked, monkeypatch):
    monkeypatch.setattr(adapty_cli, "list_apps", lambda: [{"id": adapty_cli.DEFAULT_APP_ID, "title": "Lumen"},
                                                         {"id": "other-app", "title": "Other"}])
    rows = {r["app"]: r for r in experiments.portfolio()}
    assert rows["Lumen"]["has_history"] and rows["Lumen"]["decided_live"] == 2
    assert not rows["Other"]["has_history"] and rows["Other"]["decided_live"] == 0  # same tests, but no history
    assert Path("out/report-other-app.json").exists()


def test_slack_digest_lists_decided_tests_without_posting(mocked, monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    from pa import notify
    r = experiments.build_report()
    res = notify.send(r)
    msg = json.loads(Path("out/slack_message.json").read_text())
    assert res["sent"] is False and "2 live test(s) are decided" in json.dumps(msg)


def test_three_arm_test_compares_winner_with_runner_up(monkeypatch):
    import numpy as np
    import pandas as pd
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    rng = np.random.default_rng(1)
    rows = []
    for code, mean in [("a", 4.0), ("b", 8.0), ("c", 10.0)]:
        for i in range(600):
            rows.append({"user_id": f"{code}{i}", "ab_test": "T3", "flow": code, "matured": True,
                         "revenue": float(rng.exponential(mean)), "payer": 1, "intent_score": rng.random(),
                         "intent": "mid", "platform": "iOS", "acquisition_channel": "organic",
                         "install_at": pd.Timestamp("2026-08-01") + pd.Timedelta(days=i % 60)})
    u = pd.DataFrame(rows)
    t = pd.DataFrame({"user_id": u.user_id, "paid": True, "refunded_at": pd.NaT, "billing_issue_detected_at": pd.NaT,
                      "price_usd": u.revenue, "day": 0.5})
    test = {"ab_test_id": "t3", "title": "T3", "state": "live", "type": "flow", "goal": None, "segment_ids": [],
            "started_at": None, "placement": {"title": "P", "placement_id": "p"},
            "flows": [{"flow_name": n, "flow_id": n, "weight": 33} for n in ("A", "B", "C")]}
    res = experiments.judge(test, u, t, {"a": "A", "b": "B", "c": "C"})
    assert res["winner"]["name"] == "C" and res["loser"]["name"] == "B" and [o["name"] for o in res["others"]] == ["A"]
    assert res["losing_share"] == pytest.approx(2 / 3, abs=0.01)


def test_horizon_comes_from_the_test_goal():
    assert experiments.horizon_of({"goal": "maximise 30-day revenue per install"}) == 30
    assert experiments.horizon_of({"goal": "no horizon here"}) == experiments.HORIZON_DAYS


def test_mark_stopped_updates_card_and_money_without_rebuilding(mocked):
    experiments.build_report()
    onb_id = next(x["ab_test_id"] for x in TESTS if x["title"] == ONB)
    r = experiments.mark_stopped(adapty_cli.DEFAULT_APP_ID, onb_id)
    assert by_title(r)[ONB]["verdict"] == "completed_confirmed"
    assert r["total_lost_per_month_usd"] == pytest.approx(353.63, abs=1)


# ---------------------------------------------------------------- review round 4 (written before the fixes)
def test_concurrent_sessions_keep_their_own_app(monkeypatch):
    """Streamlit runs each browser session in a thread of one process: one session's app must not leak."""
    import threading
    import time as _t
    seen = {}
    monkeypatch.setattr(adapty_cli, "_exec", lambda args: {"app": args[args.index("--app") + 1]})

    def session(app, delay):
        with adapty_cli.use_app(app):
            _t.sleep(delay)
            seen[app] = adapty_cli.run("ab-tests", "list")["app"]
    a = threading.Thread(target=session, args=("app-A", 0.05))  # A leaves while B is still working
    b = threading.Thread(target=session, args=("app-B", 0.2))
    a.start(); _t.sleep(0.02); b.start(); a.join(); b.join()
    assert seen == {"app-A": "app-A", "app-B": "app-B"}


def test_worker_threads_inherit_the_session_app(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(adapty_cli, "_exec", lambda args: {"app": args[args.index("--app") + 1]})
    with adapty_cli.use_app("app-W"):
        with ThreadPoolExecutor(2) as pool:
            got = list(pool.map(adapty_cli.in_current_app(lambda _: adapty_cli.run("x")["app"]), range(3)))
    assert got == ["app-W"] * 3


def test_horizon_ignores_trial_lengths():
    assert experiments.horizon_of({"goal": "Annual with 7-day trial vs weekly: which earns more per install?"}) == 60
    assert experiments.horizon_of({"goal": "Find which offer maximises 30-day revenue per install"}) == 30


def test_default_app_always_has_its_history(monkeypatch):
    monkeypatch.setenv("OFFLINE_APP_IDS", "some-other-app")
    assert adapty_cli.DEFAULT_APP_ID in experiments.history_apps()


def test_live_metrics_prefer_the_node_with_the_test_id(monkeypatch):
    tree = {"data": {"items": [
        {"level": "ab_test", "title": MAIN, "ab_test_id": "old", "unique_profiles_views": 900, "revenue": 1.0},
        {"level": "ab_test", "title": MAIN, "ab_test_id": "new", "unique_profiles_views": 40, "revenue": 63.0}]}}
    monkeypatch.setattr(adapty_cli, "run", lambda *a: tree)
    assert experiments.flow_live_metrics("f", MAIN, "2026-11-06", test_id="new")["users"] == 40


def test_empty_live_view_is_none(monkeypatch):
    monkeypatch.setattr(adapty_cli, "run", lambda *a: {"data": {"items": []}})
    test = {"title": MAIN, "type": "flow", "started_at": "2026-11-06T00:00:00Z", "ab_test_id": "x",
            "placement": {"placement_id": "p", "title": "P"}}
    assert experiments.live_view(test, [{"name": "A", "content_id": "a"}]) is None


def test_digest_names_the_app_and_uses_live_immaturity(mocked):
    from pa import notify
    r = experiments.build_report()
    for t in r["tests"]:
        if t["title"] == MAIN:
            t["live_view"] = {"trials": 10, "trials_finished": 1, "immature": True, "arms": {}}
    text = json.dumps(notify.digest(r, "Lumen BACKUP"))
    assert "Lumen BACKUP" in text and "1 of 10 trials" in text


def test_notify_rebuilds_and_titles_the_digest_with_the_app_name(mocked, monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(adapty_cli, "list_apps", lambda: [{"id": adapty_cli.DEFAULT_APP_ID, "title": "Lumen MAIN"}])
    Path("out").mkdir()
    stale = {"schema": experiments.REPORT_SCHEMA, "app_id": adapty_cli.DEFAULT_APP_ID, "tests": [],
             "total_lost_per_month_usd": 0}
    Path("out/report.json").write_text(json.dumps(stale))  # days-old report on disk
    monkeypatch.setattr("sys.argv", ["pa.cli", "notify"])
    cli.main()
    msg = Path("out/slack_message.json").read_text()
    assert "Lumen MAIN" in msg and "2 live test(s) are decided" in msg


def test_maturity_days_follow_the_test_horizon(data):
    u, _ = data
    _, t30, _ = experiments.load(30)
    m = experiments.maturity(t30, u.user_id.head(2000), horizon=30)
    assert max(m) == 30 and m[30] == pytest.approx(1.0, abs=1e-6)


def test_money_total_has_one_definition(mocked):
    r = experiments.build_report()
    assert experiments.decided_total(r["tests"]) == r["total_lost_per_month_usd"]


def test_throttled_cli_calls_are_retried_after_the_wait_adapty_asks_for(monkeypatch):
    import subprocess as sp
    calls, waits = [], []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        if len(calls) == 1:
            return sp.CompletedProcess(cmd, 1, "", '{"error": {"message": "Request was throttled. Expected available in 1 second."}}')
        return sp.CompletedProcess(cmd, 0, '{"data": []}', "")
    monkeypatch.setattr(adapty_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(adapty_cli.time, "sleep", lambda s: waits.append(s))
    assert adapty_cli.run("ab-tests", "list") == {"data": []}
    assert len(calls) == 2 and waits and waits[0] >= 1


def test_cli_concurrency_is_capped():
    assert adapty_cli.MAX_CONCURRENT_CALLS <= 3
