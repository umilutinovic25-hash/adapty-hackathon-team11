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
    monkeypatch.setenv("AUTOPILOT_OUT_DIR", str(tmp_path / "out"))  # never touch the project's real out/
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
            x["winner_flow_id"] = next(a["flow_id"] for a in x["flows"] if a["flow_name"] == "Onboarding - Weekly first")
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


def _synthetic(means, weights, state="live", n=600, seed=1):
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(seed)
    rows = []
    for (code, mean) in means:
        for i in range(n):
            rows.append({"user_id": f"{code}{i}", "ab_test": "TS", "flow": code, "matured": True,
                         "revenue": float(rng.exponential(mean)), "payer": 1, "intent_score": rng.random(),
                         "intent": "mid", "platform": "iOS", "acquisition_channel": "organic",
                         "install_at": pd.Timestamp("2026-08-01") + pd.Timedelta(days=i % 60)})
    u = pd.DataFrame(rows)
    t = pd.DataFrame({"user_id": u.user_id, "paid": True, "refunded_at": pd.NaT, "billing_issue_detected_at": pd.NaT,
                      "price_usd": u.revenue, "day": 0.5})
    test = {"ab_test_id": "ts", "title": "TS", "state": state, "type": "flow", "goal": None, "segment_ids": [],
            "started_at": None, "placement": {"title": "P", "placement_id": "p"},
            "flows": [{"flow_name": c.upper(), "flow_id": c.upper(), "weight": w} for (c, _), w in zip(means, weights)]}
    return test, u, t, {c: c.upper() for c, _ in means}


def test_completed_test_is_confirmed_only_if_health_checks_pass(monkeypatch):
    """Finding 3: a completed test with a broken traffic split must not be labelled 'confirmed'."""
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _synthetic([("a", 4.0), ("b", 10.0)], [90, 10], state="completed")  # equal users, 90/10 split
    res = experiments.judge(test, u, t, names)
    assert res["health"]["healthy"] is False
    assert res["verdict"] == "completed_check_failed"


def test_zero_weight_arm_does_not_break_srm(monkeypatch):
    """Finding 6: an arm at weight 0 (switched off) made the chi-square expectation 0 and the SRM p-value NaN."""
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _synthetic([("a", 4.0), ("b", 10.0), ("c", 5.0)], [50, 50, 0])
    res = experiments.judge(test, u, t, names)
    import math
    assert not math.isnan(res["health"]["srm_pvalue"]) and res["health"]["srm_pvalue"] > 0.01
    assert res["verdict"] == "stop_and_roll_out"


def test_bonferroni_bootstrap_has_enough_draws_in_each_tail():
    """Finding 5: 1,000 draws at alpha 0.05/10 leave 2.5 draws per tail, so 'reversed'/'unclear' were partly noise."""
    assert experiments.boot_n(1000, 0.05 / 10) * (0.05 / 10) / 2 >= experiments.MIN_TAIL_DRAWS
    import numpy as np
    rng = np.random.default_rng(3)
    a, b = rng.exponential(5, 400), rng.exponential(4, 400)
    lo1, hi1, _ = experiments.boot(a, b, 1000, alpha=0.005)
    lo2, hi2, _ = experiments.boot(a, b, 1000, alpha=0.005, seed=99)
    assert abs(lo1 - lo2) < 0.15 and abs(hi1 - hi2) < 0.15  # bounds no longer move with the random seed


def test_intent_model_trains_only_on_matured_users(monkeypatch):
    """Finding 9: a user installed 10 days ago can't be a 60-day payer yet; training on them teaches the model
    'recent install = low intent', so intent groups partly measure user age."""
    import numpy as np
    import pandas as pd
    from sklearn.dummy import DummyClassifier
    seen = []

    class Spy(DummyClassifier):
        def fit(self, X, y):
            seen.append(X.index)
            return super().fit(X, y)
    monkeypatch.setattr(experiments, "build_model", lambda: Spy(strategy="prior"))
    n = 400
    u = pd.DataFrame({c: "x" for c in experiments.CAT} | {c: 1.0 for c in experiments.NUM}, index=range(n))
    u["matured"] = np.arange(n) % 2 == 0
    u["payer"] = np.arange(n) % 4 == 0
    out = experiments.add_intent(u)
    assert seen and all(u.loc[idx].matured.all() for idx in seen)
    assert out.intent_score.notna().all()  # everyone is still scored


def test_demo_retains_its_normal_approximation_gate(monkeypatch):
    """Compatibility check for the synthetic demo, not certification of the approximation's error rate."""
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _synthetic([("a", 5.0), ("b", 5.6)], [50, 50], n=2000, seed=1)
    res = experiments.judge(test, u, t, names)
    assert res["diff_ci95"][0] > 0 and res["p_winner_better"] > 0.99  # the old rule would stop this test
    assert res["sequential_p"] > 0.05 and res["verdict"] == "keep_running"


def test_draft_without_arms_does_not_crash(mocked):
    draft = next(x for x in mocked["tests"] if x["state"] == "draft")
    draft["flows"], draft["paywalls"] = [], []
    d = next(t for t in experiments.build_report()["tests"] if t["state"] == "draft")
    assert d["verdict"] == "draft_plan" and d["days_to_decision"] is None


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


def test_cli_concurrency_is_capped(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import subprocess as sp
    import threading
    assert adapty_cli.MAX_CONCURRENT_CALLS <= 3
    workers = adapty_cli.MAX_CONCURRENT_CALLS + 3
    slots = adapty_cli._slots
    lock = threading.Lock()
    attempted, at_capacity, release = threading.Event(), threading.Event(), threading.Event()
    attempts = active = peak = 0

    class ObservedSlots:
        def __enter__(self):
            nonlocal attempts
            with lock:
                attempts += 1
                if attempts == workers:
                    attempted.set()
            return slots.__enter__()

        def __exit__(self, *args):
            return slots.__exit__(*args)

    def fake_run(cmd, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == adapty_cli.MAX_CONCURRENT_CALLS:
                at_capacity.set()
        try:
            assert release.wait(timeout=5)
            return sp.CompletedProcess(cmd, 0, '{"data": []}', "")
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(adapty_cli, "_slots", ObservedSlots())
    monkeypatch.setattr(adapty_cli.subprocess, "run", fake_run)
    with ThreadPoolExecutor(workers) as pool:
        calls = [pool.submit(adapty_cli.run, "ab-tests", "list") for _ in range(workers)]
        try:
            assert attempted.wait(timeout=3), "each call must pass through the concurrency limiter"
            assert at_capacity.wait(timeout=3)
            assert peak == adapty_cli.MAX_CONCURRENT_CALLS
        finally:
            release.set()
        assert all(call.result(timeout=5) == {"data": []} for call in calls)
    assert peak <= adapty_cli.MAX_CONCURRENT_CALLS


# ---------------------------------------------------------------- review round 5 (written before the fixes)
def test_copy_of_a_test_does_not_inherit_the_original_history(mocked):
    """Finding 1: a copy keeps the title; history must stay with the original, or a copy with zero users is
    judged 'stop & roll out' and offered for an irreversible stop."""
    orig = next(x for x in mocked["tests"] if x["title"] == ONB)
    orig["state"] = "completed"
    orig["winner_flow_id"] = next(a["flow_id"] for a in orig["flows"] if a["flow_name"] == "Onboarding - Weekly first")
    copy_ = copy.deepcopy(orig)
    copy_.update(ab_test_id="copy-1", state="live", started_at="2026-11-07T10:00:00Z")
    mocked["tests"].append(copy_)
    r = experiments.build_report()
    got = {t["ab_test_id"]: t for t in r["tests"]}
    assert got["copy-1"]["verdict"] == "unsupported"
    assert got[orig["ab_test_id"]]["verdict"] == "completed_confirmed"
    assert r["total_lost_per_month_usd"] == pytest.approx(353.63, abs=1)  # only Main is still decided


def test_successful_stop_with_non_json_output_is_still_a_success(monkeypatch):
    """Finding 2: exit code 0 means Adapty stopped the test; odd stdout must not turn that into an error that skips
    the audit log and leaves the Stop button on the card."""
    import subprocess as sp
    monkeypatch.setattr(adapty_cli.subprocess, "run",
                        lambda cmd, **kw: sp.CompletedProcess(cmd, 0, "✔ A/B test stopped\n", ""))
    res = adapty_cli.stop_test("t1", "w1", app_id="a1")
    assert res["ok"] is True and "stopped" in res["raw"]


def _main_winner():
    main_test = next(x for x in TESTS if x["title"] == MAIN)
    return main_test["ab_test_id"], next(a["flow_id"] for a in main_test["flows"]
                                         if a["flow_name"] == "Main - Lifetime anchor")


def test_cli_stop_refreshes_the_saved_report(mocked, monkeypatch):
    """Finding 7: after a CLI stop the saved report must show the test as completed, like the dashboard does."""
    tid, winner = _main_winner()
    monkeypatch.setattr("sys.argv", ["pa.cli", "stop", tid, "--winner-id", winner, "--approved-by", "tester"])
    cli.main()
    saved = {t["ab_test_id"]: t for t in json.loads(Path("out/report.json").read_text())["tests"]}
    assert saved[tid]["state"] == "completed" and saved[tid]["verdict"] == "completed_confirmed"


def test_one_stop_path_needs_a_named_approver(mocked):
    tid, winner = _main_winner()
    with pytest.raises(cli.StopRefused, match="approver"):
        cli.execute_stop(tid, winner, approved_by="  ")
    assert mocked["stopped"] == []


def test_audit_is_written_even_if_the_report_update_fails(mocked, monkeypatch):
    tid, winner = _main_winner()
    def boom(*a):
        raise OSError("disk full")
    monkeypatch.setattr(cli, "mark_stopped", boom)
    with pytest.raises(OSError):
        cli.execute_stop(tid, winner, approved_by="Uroš")
    entry = json.loads(Path("out/audit.log").read_text().splitlines()[-1])
    assert entry["approved_by"] == "Uroš" and mocked["stopped"] == [(tid, winner)]


def test_outputs_do_not_depend_on_the_working_directory(monkeypatch, tmp_path):
    """Finding 8: a cron job started from another folder must write the same out/ the dashboard reads."""
    import pa
    from pa import notify
    monkeypatch.delenv("AUTOPILOT_OUT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    root_out = pa.ROOT / "out"
    assert experiments.report_path() == root_out / "report.json"
    assert cli.audit_path() == root_out / "audit.log"
    assert notify.message_path() == root_out / "slack_message.json"


def test_lift_against_a_zero_revenue_arm_never_prints_none(mocked):
    """Finding 10: '+None%' in Slack and the dashboard when the other arm earned $0."""
    from pa import notify
    r = experiments.build_report()
    for t in r["tests"]:
        if t["title"] == MAIN:
            t["lift_pct"], t["loser"]["rev_per_user"] = None, 0.0
    assert "None" not in json.dumps(notify.digest(r, "Lumen"))
    assert notify.lift_text({"lift_pct": None, "loser": {"rev_per_user": 0.0}}) == "vs $0.00"
    assert notify.lift_text({"lift_pct": 12.5, "loser": {"rev_per_user": 1.0}}) == "+12.5%"
    assert "lift_pct']}%" not in (Path(__file__).parents[1] / "app.py").read_text()


def test_dashboard_uses_the_shared_stop_path():
    src = (Path(__file__).parents[1] / "app.py").read_text()
    assert "execute_stop(" in src and "adapty_cli.stop_test(" not in src


def test_three_arm_outlier_blocks_rollout_and_unverified_loss(monkeypatch):
    import numpy as np
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _synthetic([("a", 10), ("b", 9), ("c", 8)], [1, 1, 1])
    u.loc[u.flow == "a", "revenue"] = np.tile([9.8, 10.2], 300)
    u.loc[u.flow == "b", "revenue"] = np.tile([8.8, 9.2], 300)
    u.loc[u.flow == "c", "revenue"] = 0.0
    u.loc[u.user_id == "c0", "revenue"] = 4800.0
    t["price_usd"] = u.revenue
    res = experiments.judge(test, u, t, names)
    assert res["verdict"] == "keep_running"
    assert "C" in " ".join(res["reasons"]) and "not verified" in " ".join(res["reasons"])
    comparisons = {c["other"]["name"]: c for c in res["comparisons"]}
    assert comparisons["B"]["passed"] and not comparisons["C"]["passed"]
    assert comparisons["C"]["diff_ci95"][0] < 0 < comparisons["C"]["diff_ci95"][1]
    daily = len(u) / 59 * 0.1
    assert res["lost_per_month_usd"] == round(daily * 30 / 3 * (10 - 9), 2)


@pytest.mark.parametrize("check, verdict", [("guardrail", "guardrail_failed"), ("segment", "split_by_segment")])
def test_third_arm_safety_checks_can_block_rollout(monkeypatch, check, verdict):
    import numpy as np
    import pandas as pd
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _synthetic([("a", 12), ("b", 8), ("c", 4)], [1, 1, 1])
    if check == "guardrail":
        t.loc[u.flow.isin(["a", "b"]) & (u.index % 600 < 120), "refunded_at"] = pd.Timestamp("2026-09-01")
    else:
        for flow, mean in [("a", 12), ("b", 8), ("c", 0.5)]:
            u.loc[u.flow == flow, "revenue"] = mean + np.tile([-0.2, 0.2], 300)
        u.loc[u.flow.eq("c") & (u.index % 600 < 100), "revenue"] += 19.5
        u["platform"] = np.where(u.index % 600 < 100, "Android", "iOS")
        t["price_usd"] = u.revenue
    res = experiments.judge(test, u, t, names)
    assert res["winner"]["name"] == "A" and res["loser"]["name"] == "B"
    assert res["verdict"] == verdict
    comparisons = {c["other"]["name"]: c for c in res["comparisons"]}
    assert comparisons["B"]["passed"] and not comparisons["C"]["passed"]


@pytest.mark.parametrize("applied, verdict", [("A", "completed_disagrees"),
                                               (None, "completed_winner_unknown"),
                                               ("B", "completed_confirmed")])
def test_completed_confirmation_requires_the_applied_winner(monkeypatch, applied, verdict):
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _synthetic([("a", 4), ("b", 10)], [1, 1], state="completed")
    test["winner_flow_id"] = applied
    res = experiments.judge(test, u, t, names)
    assert res["verdict"] == verdict
    if applied != "B":
        assert "applied winner" in " ".join(res["reasons"])


def test_draft_sizing_uses_the_smallest_traffic_share(data):
    import math
    u, _ = data
    base = {"placement_code": "ai_limit_reached", "horizon_days": 60}
    balanced = experiments.plan_draft(base, [{"weight": 50}, {"weight": 50}], u, {})
    skewed = experiments.plan_draft(base, [{"weight": 90}, {"weight": 10}], u, {})
    pl = u[u.placement == base["placement_code"]]
    daily = len(pl) / (pl.install_at.max() - pl.install_at.min()).days * 0.1
    assert skewed["days_to_decision"] == math.ceil(skewed["sample_size_per_arm"] / (daily * 0.1)) + 60
    assert skewed["days_to_decision"] > balanced["days_to_decision"]
    assert skewed["mde_with_30_days_pct"] == round(100 * 0.1 * math.sqrt(skewed["sample_size_per_arm"] / (daily * 30 * 0.1)))


@pytest.mark.parametrize("case", ["missing", "sparse", "zero_revenue", "zero_share"])
def test_unsizeable_draft_never_recommends_start(data, case):
    u, _ = data
    u = u.copy()
    base = {"placement_code": "ai_limit_reached", "horizon_days": 60}
    arms = [{"weight": 50}, {"weight": 50}]
    if case == "missing":
        base["placement_code"] = "missing"
    elif case == "sparse":
        u = u.head(20)
    elif case == "zero_revenue":
        u["revenue"] = 0.0
    else:
        arms[1]["weight"] = 0
    res = experiments.plan_draft(base, arms, u, {})
    assert res["days_to_decision"] is None
    assert res["recommendation"] == "can't size: not enough history/traffic at this placement"


def test_dashboard_draft_summary_matches_recommendation(mocked, monkeypatch):
    from streamlit.testing.v1 import AppTest
    r = experiments.build_report()
    draft = next(t for t in r["tests"] if t["state"] == "draft")
    draft["recommendation"] = "start it"
    experiments.write_atomic(experiments.report_path(), json.dumps(r))
    monkeypatch.setattr(adapty_cli, "list_apps", lambda: [{"id": adapty_cli.DEFAULT_APP_ID, "title": "Lumen"}])
    ui = AppTest.from_file(str(Path(__file__).parents[1] / "app.py")).run(timeout=30)
    assert not ui.exception
    table = ui.dataframe[0].value.set_index("test")
    assert "start it" in table.loc[draft["title"], "verdict"]


def test_report_records_analysis_time_and_data_sources(mocked):
    from datetime import datetime, timezone
    before = datetime.now(timezone.utc)
    r = experiments.build_report()
    assert before <= datetime.fromisoformat(r["analyzed_at"]) <= datetime.now(timezone.utc)
    assert r["sources"] == {"adapty": "Adapty API (live)", "history": "synthetic history"}
    assert r["data_end"].startswith("2026-11-03")


@pytest.mark.parametrize("legacy", [False, True])
def test_dashboard_saved_report_has_source_dates_and_age(mocked, monkeypatch, legacy):
    from datetime import datetime, timedelta, timezone
    from streamlit.testing.v1 import AppTest
    import streamlit as st
    r = experiments.build_report()
    r["analyzed_at"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    if legacy:
        r["schema"] = experiments.REPORT_SCHEMA - 1
        r.pop("analyzed_at")
        for t in r["tests"]:
            t.pop("comparisons", None)
    experiments.write_atomic(experiments.report_path(), json.dumps(r))
    monkeypatch.setattr(adapty_cli, "list_apps", lambda: [{"id": adapty_cli.DEFAULT_APP_ID, "title": "Lumen"}])
    def offline(*args):
        raise RuntimeError("offline")
    monkeypatch.setattr(experiments, "build_report", offline)
    st.cache_data.clear()
    ui = AppTest.from_file(str(Path(__file__).parents[1] / "app.py")).run(timeout=30)
    assert not ui.exception
    warnings = " ".join(w.value for w in ui.warning)
    assert "saved report (offline)" in warnings
    assert "analysis time unknown" in warnings if legacy else "3.0 hours old" in warnings
    captions = " ".join(c.value for c in ui.caption)
    assert "synthetic history" in captions and "2026-11-03" in captions and "Last analysis" in captions
    assert "Adapty API (live)" not in captions


def test_dashboard_recent_saved_report_is_not_called_offline(mocked, monkeypatch):
    """Demo flow: `pa.cli check` a few minutes before `streamlit run` must not look offline on stage."""
    from datetime import datetime, timedelta, timezone
    from streamlit.testing.v1 import AppTest
    import streamlit as st
    r = experiments.build_report()
    r["analyzed_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    experiments.write_atomic(experiments.report_path(), json.dumps(r))
    monkeypatch.setattr(adapty_cli, "list_apps", lambda: [{"id": adapty_cli.DEFAULT_APP_ID, "title": "Lumen"}])
    st.cache_data.clear()
    ui = AppTest.from_file(str(Path(__file__).parents[1] / "app.py")).run(timeout=30)
    assert not ui.exception
    assert not any("saved report" in w.value for w in ui.warning)
    captions = " ".join(c.value for c in ui.caption)
    assert "Adapty API, read 5 min ago" in captions
    assert "offline" not in captions.replace("Default app (offline)", "")


def test_dashboard_live_badge_requires_a_fresh_analysis(mocked, monkeypatch):
    from streamlit.testing.v1 import AppTest
    import streamlit as st
    r = experiments.build_report()
    monkeypatch.setattr(adapty_cli, "list_apps", lambda: [{"id": adapty_cli.DEFAULT_APP_ID, "title": "Lumen"}])
    monkeypatch.setattr(experiments, "build_report", lambda *a: r)
    st.cache_data.clear()
    ui = AppTest.from_file(str(Path(__file__).parents[1] / "app.py")).run(timeout=30)
    ui.button[0].click().run(timeout=30)
    assert not ui.exception
    assert "Adapty API (live)" in " ".join(c.value for c in ui.caption)
    assert not any("saved report (offline)" in w.value for w in ui.warning)


def test_monthly_loss_calculation_reconciles_demo_headline(mocked):
    r = experiments.build_report()
    tests = by_title(r)
    assert r["total_lost_per_month_usd"] == 1749.31
    for title, expected in [(ONB, 1395.68), (MAIN, 353.63)]:
        t = tests[title]
        calc = t["loss_calculation"]
        assert calc["live_population"] == 5000 and calc["offline_population"] == 50000
        assert calc["live_size_scale"] == 0.1
        assert calc["daily_users"] == calc["offline_users_in_test"] / calc["history_span_days"] * 0.1
        assert round(sum(calc["daily_users"] * arm["losing_share"] * arm["gap_per_user"] * 30
                         for arm in calc["arms"]), 2) == t["lost_per_month_usd"] == expected
        assert t["lost_per_month_ci95_usd"][0] < expected < t["lost_per_month_ci95_usd"][1]


def test_monthly_loss_interval_scales_the_existing_bootstrap_gap(monkeypatch):
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _synthetic([("a", 4), ("b", 10)], [1, 1])
    res = experiments.judge(test, u, t, names)
    lo, hi, _ = experiments.boot(u[u.flow == "b"].revenue.values, u[u.flow == "a"].revenue.values)
    factor = len(u) / 59 * 0.1 * 0.5 * 30
    assert res["lost_per_month_ci95_usd"] == [round(factor * lo, 2), round(factor * hi, 2)]
    # The interval is per two-arm comparison; no sum of CIs is claimed for a multi-arm test.
    test3, u3, t3, names3 = _synthetic([("a", 4), ("b", 10), ("c", 5)], [1, 1, 1])
    assert experiments.judge(test3, u3, t3, names3)["lost_per_month_ci95_usd"] is None


def test_dashboard_exposes_the_headline_calculation(mocked, monkeypatch):
    from streamlit.testing.v1 import AppTest
    import streamlit as st
    experiments.build_report()
    monkeypatch.setattr(adapty_cli, "list_apps", lambda: [{"id": adapty_cli.DEFAULT_APP_ID, "title": "Lumen"}])
    st.cache_data.clear()
    ui = AppTest.from_file(str(Path(__file__).parents[1] / "app.py")).run(timeout=30)
    assert not ui.exception
    audit = next(e for e in ui.expander if e.label == "How is the monthly loss calculated?")
    text = " ".join(m.value for m in audit.markdown)
    assert "5,000 / 50,000 = 0.1" in text
    assert "daily users" in text and "losing arm share" in text and "revenue gap" in text
    assert all(value in text for value in ["$1,395.68", "$353.63", "$1,749.31", "95%", "new cohorts"])
    hero = next(m.value for m in ui.markdown if "font-size:56px" in m.value)
    assert "$1,749.31" in hero
    table = ui.dataframe[0].value.set_index("test")
    assert table.loc[MAIN, "$ / month"] == "$353.63"
    assert table.loc[ONB, "$ / month"] == "$1,395.68"
    digest = next(e for e in ui.expander if e.label == "📬 Daily digest for Slack")
    assert "$1,749.31" in " ".join(m.value for m in digest.markdown)
    assert len([s for s in ui.success if "**Matured 60-day revenue" in s.value]) == 2
    assert not any("one winner for everyone is the right call" in c.value for c in ui.caption)


@pytest.mark.parametrize("change", ["winner", "health"])
def test_fresh_rejudge_refuses_stop_without_calling_adapty(mocked, monkeypatch, data, change):
    u, tx = data
    fresh_u = u.copy()
    monkeypatch.setattr(experiments, "load", lambda horizon=60: (fresh_u.copy(), tx.copy(), tx.purchased_at.max()))
    monkeypatch.setattr(experiments, "add_intent", lambda frame: frame)  # keep the existing cross-fitted scores
    approved = experiments.build_report()
    tid, _ = _main_winner()
    plan = cli.make_plan(approved, tid)
    if change == "winner":
        in_test = fresh_u.ab_test.eq(MAIN)
        fresh_u.loc[in_test, "flow"] = fresh_u.loc[in_test, "flow"].map({"main": "main_lifetime", "main_lifetime": "main"})
    else:
        main = next(t for t in mocked["tests"] if t["title"] == MAIN)
        main["flows"][0]["weight"], main["flows"][1]["weight"] = 90, 10
    reads = []
    original_report = cli.report
    def observe_report(fresh, app_id=None):
        reads.append(fresh)
        return original_report(fresh, app_id)
    monkeypatch.setattr(cli, "report", observe_report)
    message = "not the approved winner" if change == "winner" else "verdict is check_data"
    with pytest.raises(cli.StopRefused, match=message):
        cli.execute_stop(tid, plan["winner_id"], approved_by="tester", app_id=adapty_cli.DEFAULT_APP_ID)
    assert reads == [True]
    assert mocked["stopped"] == [] and mocked["apps"] == []
    assert not cli.audit_path().exists()
    fresh = by_title(original_report(fresh=False))[MAIN]
    if change == "winner":
        assert fresh["verdict"] == "stop_and_roll_out" and fresh["winner"]["content_id"] != plan["winner_id"]
    else:
        assert fresh["health"]["healthy"] is False and fresh["verdict"] == "check_data"


@pytest.mark.parametrize("change", ["winner", "health"])
def test_dashboard_refusal_refreshes_the_card_and_requires_new_approval(mocked, monkeypatch, change):
    from streamlit.testing.v1 import AppTest
    import streamlit as st
    r = experiments.build_report()
    tid, _ = _main_winner()
    monkeypatch.setattr(adapty_cli, "list_apps", lambda: [{"id": r["app_id"], "title": "Lumen"}])

    def refuse(*args, **kwargs):
        fresh = copy.deepcopy(r)
        main = by_title(fresh)[MAIN]
        if change == "health":
            main["verdict"] = "check_data"
            main["health"]["healthy"] = False
        else:
            main["winner"], main["loser"] = main["loser"], main["winner"]
        fresh["total_lost_per_month_usd"] = experiments.decided_total(fresh["tests"])
        experiments.write_atomic(experiments.report_path(), json.dumps(fresh))
        raise cli.StopRefused("fresh plan changed")

    monkeypatch.setattr(cli, "execute_stop", refuse)
    st.cache_data.clear()
    ui = AppTest.from_file(str(Path(__file__).parents[1] / "app.py")).run(timeout=30)
    ui.text_input(key=f"who-{tid}").set_value("tester")
    ui.checkbox(key=f"ok-{tid}").check().run(timeout=30)
    ui.button(key=f"go-{tid}").click().run(timeout=30)
    assert not ui.exception
    assert any("fresh plan changed" in e.value for e in ui.error)
    table = ui.dataframe[0].value.set_index("test")
    if change == "health":
        assert table.loc[MAIN, "verdict"] == "🧯 check data"
        assert not any(b.key == f"go-{tid}" for b in ui.button)
    else:
        assert "3 plans" in table.loc[MAIN, "matured 60-day"]
        assert not ui.checkbox(key=f"ok-{tid}").value
        assert ui.button(key=f"go-{tid}").disabled
    assert mocked["stopped"] == [] and not cli.audit_path().exists()


def test_concurrent_report_writers_have_separate_temporary_files(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    target = tmp_path / "report.json"
    barrier = threading.Barrier(2)
    real_replace = experiments.os.replace
    sources = []

    def replace(src, dst):
        sources.append(src)
        barrier.wait(timeout=5)  # both writes finish before either writer replaces the target
        real_replace(src, dst)

    monkeypatch.setattr(experiments.os, "replace", replace)
    with ThreadPoolExecutor(2) as pool:
        writes = [pool.submit(experiments.write_atomic, target, json.dumps({"writer": name})) for name in ("A", "B")]
        for write in writes:
            write.result(timeout=10)
    assert len(set(sources)) == 2
    assert json.loads(target.read_text()) in [{"writer": "A"}, {"writer": "B"}]
    assert list(tmp_path.iterdir()) == [target]


def test_failed_report_replace_preserves_report_and_cleans_temp(monkeypatch, tmp_path):
    target = tmp_path / "report.json"
    target.write_text('"previous"')

    def fail(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(experiments.os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        experiments.write_atomic(target, '"new"')
    assert json.loads(target.read_text()) == "previous"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("horizon", [30, 60])
def test_live_only_app_does_not_need_local_history_files(mocked, monkeypatch, horizon):
    def missing(*args):
        raise FileNotFoundError("users.csv.gz")

    monkeypatch.setattr(experiments, "load", missing)
    monkeypatch.setattr(experiments, "add_intent", lambda *args: pytest.fail("no history to train on"))
    monkeypatch.setattr(experiments, "flow_live_metrics", lambda *args: {
        "users": 12, "revenue": 24.0, "rev_per_user": 2.0, "trials": 0, "trials_finished": 0, "purchases": 2})
    for test in mocked["tests"]:
        test["goal"] = f"maximise {horizon}-day revenue"
    r = experiments.build_report("other-app")
    assert not r["has_history"] and r["data_end"] is None and r["intent"] is None
    assert r["total_lost_per_month_usd"] == 0
    tests = by_title(r)
    assert tests[MAIN]["verdict"] == "unsupported"
    assert all(a["users"] == 12 for a in tests[MAIN]["live_view"]["arms"].values())
    draft = next(t for t in r["tests"] if t["state"] == "draft")
    assert draft["verdict"] == "draft_plan" and draft["recommendation"].startswith("can't size")
    assert experiments.report_path("other-app").exists()


@pytest.mark.parametrize("state", ["live", "completed"])
@pytest.mark.parametrize("case", ["constant", "zero_scale", "all_zero"])
def test_inapplicable_sequential_model_requires_review(monkeypatch, state, case):
    import numpy as np
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _synthetic([("a", 10), ("b", 1)], [50, 50], state=state)
    if case == "constant":
        a, b = np.full(600, 10.0), np.zeros(600)
    elif case == "zero_scale":
        a, b = np.tile([0.9, 1.1], 300), np.tile([-1.1, -0.9], 300)
    else:
        a, b = np.zeros(600), np.zeros(600)
    u.loc[u.flow == "a", "revenue"], u.loc[u.flow == "b", "revenue"] = a, b
    t["price_usd"] = u.revenue
    res = experiments.judge(test, u, t, names)
    assert res["health"]["healthy"]
    assert res["sequential_p"] is None
    assert all(c["sequential_p"] is None and not c["passed"] for c in res["comparisons"])
    assert res["verdict"] == ("check_data" if state == "live" else "completed_check_failed")
    reasons = " ".join(res["reasons"])
    assert "not applicable" in reasons and "needs ≈" not in reasons
    assert res["lost_per_month_usd"] == 0
    json.dumps(res, allow_nan=False)


@pytest.mark.parametrize("reverse", [False, True])
def test_winner_ranking_uses_revenue_before_rounding(monkeypatch, reverse):
    import numpy as np
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _synthetic([("a", 1), ("b", 1)], [50, 50])
    u.loc[u.flow == "a", "revenue"] = np.tile([1.0039, 1.0041], 300)
    u.loc[u.flow == "b", "revenue"] = np.tile([1.0009, 1.0011], 300)
    t["price_usd"] = u.revenue
    if reverse:
        test["flows"].reverse()
    res = experiments.judge(test, u, t, names)
    assert res["winner"]["name"] == "A"
    assert res["p_winner_better"] == 1 and res["verdict"] == "stop_and_roll_out"


def test_lift_with_negative_baseline_uses_dollars_instead_of_percent(monkeypatch):
    import numpy as np
    from pa import notify
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _synthetic([("a", 1), ("b", 1)], [50, 50])
    u.loc[u.flow == "a", "revenue"] = np.tile([0.9, 1.1], 300)
    u.loc[u.flow == "b", "revenue"] = np.tile([-0.11, -0.09], 300)
    t["price_usd"] = u.revenue
    res = experiments.judge(test, u, t, names)
    assert res["winner"]["name"] == "A" and res["lift_pct"] is None
    assert notify.lift_text(res) == "vs $-0.10"


def test_cli_read_timeout_is_bounded_and_releases_its_slot(monkeypatch):
    import subprocess as sp
    timeouts = []

    def stall(cmd, **kwargs):
        timeout = kwargs.get("timeout")
        assert timeout is not None, "read call has no timeout"
        timeouts.append(timeout)
        raise sp.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(adapty_cli.subprocess, "run", stall)
    with pytest.raises(adapty_cli.AdaptyError, match="read timed out"):
        adapty_cli.run("ab-tests", "list")
    assert timeouts == [adapty_cli.READ_TIMEOUT_SECONDS]  # a timeout is not retried
    acquired = 0
    try:
        for _ in range(adapty_cli.MAX_CONCURRENT_CALLS):
            assert adapty_cli._slots.acquire(blocking=False)
            acquired += 1
    finally:
        for _ in range(acquired):
            adapty_cli._slots.release()


def test_dashboard_failed_refresh_keeps_saved_data_without_live_badge(mocked, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from streamlit.testing.v1 import AppTest
    import streamlit as st
    r = experiments.build_report()
    r["analyzed_at"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    experiments.write_atomic(experiments.report_path(), json.dumps(r))
    monkeypatch.setattr(adapty_cli, "list_apps", lambda: [{"id": r["app_id"], "title": "Lumen"}])

    def fail(*args):
        raise adapty_cli.AdaptyError("Adapty read timed out")

    monkeypatch.setattr(experiments, "build_report", fail)
    st.cache_data.clear()
    ui = AppTest.from_file(str(Path(__file__).parents[1] / "app.py")).run(timeout=30)
    ui.button[0].click().run(timeout=30)
    assert not ui.exception
    assert any("read timed out" in w.value for w in ui.warning)
    assert "Adapty API (live)" not in " ".join(c.value for c in ui.caption)
    assert "saved report (offline)" in " ".join(c.value for c in ui.caption)
    assert "Lifetime anchor" in ui.dataframe[0].value.set_index("test").loc[MAIN, "matured 60-day"]


def _history_csvs(monkeypatch, users, transactions):
    def read(path, **kwargs):
        return (users if str(path).endswith("users.csv.gz") else transactions).copy()
    monkeypatch.setattr(experiments.pd, "read_csv", read)


def _exposure_history():
    import pandas as pd
    users = pd.DataFrame({"user_id": ["returning"],
                          "install_at": pd.to_datetime(["2026-01-01"], utc=True),
                          "exposed_at": ["2026-04-01T00:00:00Z"]})
    tx = pd.DataFrame({"user_id": ["returning", "returning"],
                       "purchased_at": pd.to_datetime(["2026-01-10", "2026-04-02"], utc=True),
                       "refunded_at": pd.to_datetime([None, None], utc=True),
                       "billing_issue_detected_at": pd.to_datetime([None, None], utc=True),
                       "offer": ["paid", "paid"], "price_usd": [10.0, 100.0]})
    return users, tx


def test_returning_user_revenue_and_maturity_start_at_exposure(monkeypatch):
    users, tx = _exposure_history()
    _history_csvs(monkeypatch, users, tx)
    u, t, _ = experiments.load()
    assert u.revenue.iloc[0] == 100.0
    assert not bool(u.matured.iloc[0])
    assert t.day.tolist() == [-81.0, 1.0]
    assert t.paid.tolist() == [False, True]


def test_legacy_synthetic_history_keeps_its_install_clock(monkeypatch):
    users, tx = _exposure_history()
    _history_csvs(monkeypatch, users.drop(columns="exposed_at"), tx)
    u, t, _ = experiments.load()
    assert u.revenue.iloc[0] == 10.0 and bool(u.matured.iloc[0])
    assert u.exposure_basis.iloc[0] == "install_at_proxy"


def test_explicit_history_cutoff_excludes_future_purchases_and_refunds(monkeypatch):
    import pandas as pd
    users, tx = _exposure_history()
    tx.loc[1, "refunded_at"] = pd.Timestamp("2026-04-10", tz="UTC")
    future = tx.iloc[[1]].copy()
    future["purchased_at"] = pd.Timestamp("2026-04-05", tz="UTC")
    future["refunded_at"] = pd.NaT
    future["price_usd"] = 200.0
    _history_csvs(monkeypatch, users, pd.concat([tx, future], ignore_index=True))
    u, t, end = experiments.load(observed_through="2026-04-02T23:59:59Z")
    assert u.revenue.iloc[0] == 100.0 and not bool(u.matured.iloc[0])
    assert end == pd.Timestamp("2026-04-02T23:59:59Z")
    assert t.loc[t.price_usd.eq(100), "refunded_at"].isna().all()
    assert not t.price_usd.eq(200).any()


@pytest.mark.parametrize("exposed_at", [None, "invalid", "2025-12-31T00:00:00Z"])
def test_explicit_exposure_never_falls_back_to_install_when_invalid(monkeypatch, exposed_at):
    users, tx = _exposure_history()
    users["exposed_at"] = exposed_at
    _history_csvs(monkeypatch, users, tx)
    with pytest.raises(ValueError, match="exposed_at"):
        experiments.load()


def _identified_history():
    test, u, t, names = _synthetic([("a", 4), ("b", 10)], [50, 50])
    u["exposed_at"] = u.install_at
    u["exposure_basis"] = "exposed_at"
    u["ab_test_id"] = test["ab_test_id"]
    return test, u, t, names


def test_history_matches_test_id_even_if_test_was_renamed(monkeypatch):
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _identified_history()
    test["title"] = "Renamed experiment"
    res = experiments.judge(test, u, t, names, owners={test["title"]: "another-id"})
    assert res["verdict"] != "unsupported" and res["matured_users"] == 1200


def test_same_title_does_not_borrow_another_test_id(monkeypatch):
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _identified_history()
    u["ab_test_id"] = "different-id"
    assert experiments.judge(test, u, t, names)["verdict"] == "unsupported"


def test_explicit_exposures_before_test_start_are_not_current_evidence(monkeypatch):
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _identified_history()
    test["started_at"] = "2026-11-06T00:00:00Z"
    assert experiments.judge(test, u, t, names)["verdict"] == "unsupported"


def test_exposure_history_requires_stable_test_ids(monkeypatch):
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _identified_history()
    res = experiments.judge(test, u.drop(columns="ab_test_id"), t, names)
    assert res["verdict"] == "unsupported" and "ab_test_id" in " ".join(res["reasons"])


def test_exposure_window_does_not_change_with_refunds_after_the_horizon(monkeypatch):
    import pandas as pd
    users, tx = _exposure_history()
    tx.loc[1, "refunded_at"] = pd.Timestamp("2026-06-10", tz="UTC")
    _history_csvs(monkeypatch, users, tx)
    u, t, _ = experiments.load(observed_through="2026-07-01T00:00:00Z")
    assert bool(u.matured.iloc[0]) and u.revenue.iloc[0] == 100.0
    assert t.loc[t.price_usd.eq(100), "refunded_at"].isna().all()


def _hoeffding_p(a, b, **kwargs):
    return experiments.bounded_revenue_check(a, b, **kwargs)["p"]


def test_bounded_anytime_check_matches_a_known_result():
    import numpy as np
    a, b = np.full(100, 0.8), np.full(100, 0.2)
    result = experiments.bounded_revenue_check(a, b, bounds=[0, 1])
    assert result["p"] == pytest.approx(4.732290e-8, rel=1e-5)
    assert result["ci95"][0] > 0 and result["ci95"][1] > 0.6


def test_bounded_anytime_check_blocks_an_unseen_rare_payer():
    import numpy as np
    a, b = np.zeros(100), np.tile([0.99, 1.01], 50)
    result = experiments.bounded_revenue_check(a, b, bounds=[0, 1000])
    assert result["p"] == 1 and result["ci95"][0] < 0 < result["ci95"][1]


@pytest.mark.parametrize("bounds", [None, [0, 0], [1, 0], [0, float("inf")], [0], "invalid"])
def test_bounded_anytime_check_requires_fixed_valid_bounds(bounds):
    import numpy as np
    assert _hoeffding_p(np.ones(100), np.zeros(100), bounds=bounds) is None


def test_bounded_anytime_check_refuses_values_outside_the_declared_range():
    import numpy as np
    assert _hoeffding_p(np.array([0, 1001]), np.array([0, 1]), bounds=[0, 1000]) is None
    assert _hoeffding_p(np.array([float("nan")]), np.array([1]), bounds=[0, 1000]) is None


def test_bounded_anytime_check_accounts_for_all_arm_pairs():
    import numpy as np
    a, b = np.full(100, 0.8), np.full(100, 0.2)
    single = experiments.bounded_revenue_check(a, b, bounds=[0, 1])
    three = experiments.bounded_revenue_check(a, b, bounds=[0, 1], pair_count=3)
    assert three["p"] == pytest.approx(3 * single["p"])
    assert three["ci95"][0] < single["ci95"][0]


def test_bounded_anytime_check_allows_zero_variance_with_known_support():
    import numpy as np
    for check in (_hoeffding_p, experiments.always_valid_p):
        assert check(np.ones(1000), np.zeros(1000), bounds=[0, 1]) < 0.05
        assert check(np.zeros(1000), np.zeros(1000), bounds=[0, 1]) == 1
        assert check(np.full(1000, 0.2), np.full(1000, -0.2), bounds=[-1, 1]) < 0.05


def _zero_inflated(rng, n, payer_rate, price=40.0):
    return price * (rng.random(n) < payer_rate)


def test_betting_check_decides_a_realistic_gap_that_hoeffding_cannot():
    """$8 vs $4 per user, 7,000 users per arm, $200 declared bound: variance-adaptive evidence decides."""
    import numpy as np
    rng = np.random.default_rng(20261108)
    a, b = _zero_inflated(rng, 7000, 0.2), _zero_inflated(rng, 7000, 0.1)
    hoeffding = experiments.bounded_revenue_check(a, b, bounds=[0, 200])
    betting = experiments.betting_revenue_check(a, b, bounds=[0, 200])
    assert hoeffding["p"] >= 0.05 and hoeffding["ci95"][0] < 0
    assert betting["p"] < 0.05 and betting["ci95"][0] > 0
    assert betting["ci95"][0] <= a.mean() - b.mean() <= betting["ci95"][1]
    assert betting["ci95"][1] - betting["ci95"][0] < (hoeffding["ci95"][1] - hoeffding["ci95"][0]) / 3


def test_betting_check_is_inconclusive_for_equal_arms():
    import numpy as np
    rng = np.random.default_rng(7)
    a, b = _zero_inflated(rng, 5000, 0.1), _zero_inflated(rng, 5000, 0.1)
    result = experiments.betting_revenue_check(a, b, bounds=[0, 200])
    assert result["p"] >= 0.05 and result["ci95"][0] < 0 < result["ci95"][1]


def test_betting_check_blocks_an_unseen_rare_payer():
    import numpy as np
    a, b = np.zeros(100), np.tile([0.99, 1.01], 50)
    result = experiments.betting_revenue_check(a, b, bounds=[0, 1000])
    assert result["p"] >= 0.05 and result["ci95"][0] < 0 < result["ci95"][1]


@pytest.mark.parametrize("bounds", [None, [0, 0], [1, 0], [0, float("inf")], [0], "invalid"])
def test_betting_check_requires_fixed_valid_bounds(bounds):
    import numpy as np
    result = experiments.betting_revenue_check(np.ones(100), np.zeros(100), bounds=bounds)
    assert result["p"] is None and result["ci95"] is None and "revenue_bounds_usd" in result["reason"]


def test_betting_check_refuses_values_outside_the_declared_range():
    import numpy as np
    assert experiments.betting_revenue_check(np.array([0, 1001]), np.array([0, 1]), bounds=[0, 1000])["p"] is None
    assert experiments.betting_revenue_check(np.array([np.nan]), np.array([1]), bounds=[0, 1000])["p"] is None


def test_betting_check_splits_error_across_every_arm():
    import numpy as np
    rng = np.random.default_rng(3)
    a, b = _zero_inflated(rng, 3000, 0.2), _zero_inflated(rng, 3000, 0.1)
    two = experiments.betting_revenue_check(a, b, bounds=[0, 200])
    three = experiments.betting_revenue_check(a, b, bounds=[0, 200], arm_count=3)
    assert three["p"] > two["p"]
    assert three["ci95"][0] < two["ci95"][0] and three["ci95"][1] > two["ci95"][1]


def test_betting_confidence_sequence_covers_every_prefix():
    """One arm's sequence: the running interval at every look contains the true mean (seeded check)."""
    import numpy as np
    rng = np.random.default_rng(11)
    misses = 0
    for _ in range(200):
        x = _zero_inflated(rng, 2000, 0.1) / 200  # true mean 0.02 on the [0, 1] scale
        lo, hi = experiments.betting_cs_path(x, level=0.05)
        assert lo.shape == hi.shape == (2000,)
        misses += bool(((lo > 0.02) | (hi < 0.02)).any())
    assert misses <= 10  # Ville: P(any miss) <= 5%; 10/200 leaves room for sampling noise


def test_judge_uses_exposure_order_for_the_betting_sequence(monkeypatch):
    import numpy as np
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _identified_history()
    test["revenue_bounds_usd"] = [0, 200]
    rng = np.random.default_rng(5)
    u.loc[u.flow.eq("a"), "revenue"] = _zero_inflated(rng, 600, 0.6)
    u.loc[u.flow.eq("b"), "revenue"] = _zero_inflated(rng, 600, 0.2)
    t["price_usd"] = u.revenue
    shuffled = u.sample(frac=1, random_state=1).reset_index(drop=True)
    res = experiments.judge(test, shuffled, t, names)
    ordered = shuffled.sort_values(["exposed_at", "user_id"], kind="stable")
    expected = experiments.betting_revenue_check(ordered.loc[ordered.flow.eq("a"), "revenue"].values,
                                                 ordered.loc[ordered.flow.eq("b"), "revenue"].values,
                                                 bounds=[0, 200], arm_count=2)
    assert res["inference"]["method"] == "betting_cs" and res["inference"]["arm_count"] == 2
    assert res["sequential_p"] == expected["p"] and res["anytime_ci95"] == expected["ci95"]


def test_real_exposure_history_cannot_roll_out_without_declared_bounds(monkeypatch):
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _identified_history()
    res = experiments.judge(test, u, t, names)
    assert res["verdict"] == "check_data" and res["sequential_p"] is None
    assert "revenue_bounds_usd" in " ".join(res["reasons"])


def test_full_judge_blocks_the_rare_payer_false_winner(monkeypatch):
    import numpy as np
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _identified_history()
    test["revenue_bounds_usd"] = [0, 1000]
    u.loc[u.flow.eq("a"), "revenue"] = 0.0
    u.loc[u.flow.eq("b"), "revenue"] = np.tile([0.99, 1.01], 300)
    t["price_usd"] = u.revenue
    t["paid"] = u.revenue.gt(0)
    res = experiments.judge(test, u, t, names)
    assert res["verdict"] == "keep_running" and res["sequential_p"] >= 0.05
    assert res["lost_per_month_usd"] == 0 and res["anytime_ci95"][0] < 0 < res["anytime_ci95"][1]


def test_real_exposure_history_can_decide_a_large_bounded_effect(monkeypatch):
    import numpy as np
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _identified_history()
    test["revenue_bounds_usd"] = [0, 1]
    u.loc[u.flow.eq("a"), "revenue"] = np.tile([0.79, 0.81], 300)
    u.loc[u.flow.eq("b"), "revenue"] = np.tile([0.19, 0.21], 300)
    t["price_usd"] = u.revenue
    res = experiments.judge(test, u, t, names)
    assert res["verdict"] == "stop_and_roll_out" and res["winner"]["name"] == "A"
    assert res["sequential_p"] < 0.05 and res["anytime_ci95"][0] > 0
    assert res["inference"]["method"] == "betting_cs" and res["inference"]["arm_count"] == 2


def test_real_exposure_history_refuses_a_broken_bound(monkeypatch):
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _identified_history()
    test["revenue_bounds_usd"] = [0, 1]  # exponential values exceed this declared support
    res = experiments.judge(test, u, t, names)
    assert res["verdict"] == "check_data" and res["sequential_p"] is None
    assert "outside" in " ".join(res["reasons"])


def test_legacy_demo_is_explicitly_labeled_an_approximation(monkeypatch):
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _synthetic([("a", 4), ("b", 10)], [50, 50])
    res = experiments.judge(test, u, t, names)
    assert res["verdict"] == "stop_and_roll_out"
    assert res["inference"]["method"] == "normal_approximation_demo"
    assert res["anytime_ci95"] is None and "not validated" in " ".join(res["reasons"])


def test_build_report_accepts_predeclared_per_test_bounds_and_cutoff(mocked, monkeypatch):
    import numpy as np
    import pandas as pd
    test, u, t, names = _identified_history()
    u.loc[u.flow.eq("a"), "revenue"] = np.tile([0.79, 0.81], 300)
    u.loc[u.flow.eq("b"), "revenue"] = np.tile([0.19, 0.21], 300)
    u["ai_edits_first_day"] = 0
    u["intent"] = np.tile(["low", "mid", "high"], 400)
    t["price_usd"] = u.revenue
    mocked["tests"] = [test]
    cutoff = "2026-12-01T00:00:00Z"
    reads = []

    def load(horizon=60, *, observed_through=None):
        reads.append((horizon, observed_through))
        return u.copy(), t.copy(), pd.Timestamp(cutoff)

    monkeypatch.setattr(experiments, "load", load)
    monkeypatch.setattr(experiments, "add_intent", lambda frame: frame)
    monkeypatch.setattr(experiments, "flow_names", lambda: names)
    r = experiments.build_report(revenue_bounds_usd={test["ab_test_id"]: [0, 1]}, observed_through=cutoff)
    assert reads == [(60, cutoff)]
    assert r["sources"]["history"] == "experiment exposure history"
    judged = r["tests"][0]
    assert judged["verdict"] == "stop_and_roll_out"
    assert judged["inference"]["revenue_bounds_usd"] == [0.0, 1.0]
    assert judged["loss_calculation"]["live_size_scale"] == 1.0
    assert judged["loss_calculation"]["population_basis"] == "observed_exposures"
    assert "revenue_bounds_usd" not in mocked["tests"][0]  # API metadata is not mutated
    from streamlit.testing.v1 import AppTest
    import streamlit as st
    monkeypatch.setattr(adapty_cli, "list_apps", lambda: [{"id": r["app_id"], "title": "Exposure test app"}])
    st.cache_data.clear()
    ui = AppTest.from_file(str(Path(__file__).parents[1] / "app.py")).run(timeout=30)
    assert not ui.exception
    captions = " ".join(c.value for c in ui.caption)
    assert "experiment exposure history" in captions and "anytime betting" in captions
    assert "synthetic history" not in captions
    audit = next(e for e in ui.expander if e.label == "How is the monthly loss calculated?")
    assert "observed exposures" in " ".join(m.value for m in audit.markdown)
    assert "95% anytime" in " ".join(m.value for m in ui.markdown)
    fresh = experiments.build_report()  # the Change Guard rebuild must retain the approved metric protocol
    assert fresh["tests"][0]["verdict"] == "stop_and_roll_out"
    assert fresh["tests"][0]["inference"]["revenue_bounds_usd"] == [0.0, 1.0]
    fresh["tests"][0]["horizon_days"] = 30
    experiments.write_atomic(experiments.report_path(), json.dumps(fresh))
    changed = experiments.build_report()
    assert changed["tests"][0]["verdict"] == "check_data"  # a different horizon needs a new bounds declaration
    experiments.write_atomic(experiments.report_path(), "[]")
    rebuilt = experiments.build_report()
    assert rebuilt["tests"][0]["verdict"] == "check_data"  # malformed cached protocols are ignored safely


def test_real_history_preserves_sub_cent_revenue_in_the_estimate(monkeypatch):
    import numpy as np
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _identified_history()
    test["revenue_bounds_usd"] = [0.8, 0.81]
    u.loc[u.flow.eq("a"), "revenue"] = np.tile([0.8039, 0.8041], 300)
    u.loc[u.flow.eq("b"), "revenue"] = np.tile([0.8009, 0.8011], 300)
    t["price_usd"] = u.revenue
    res = experiments.judge(test, u, t, names)
    assert res["verdict"] == "stop_and_roll_out"
    assert res["winner"]["rev_per_user"] == pytest.approx(0.804)
    assert res["loser"]["rev_per_user"] == pytest.approx(0.801)
    assert res["lost_per_month_usd"] == 0.92  # 1200 / 59 * 0.5 * 0.003 * 30


def test_real_multi_arm_decision_corrects_for_every_arm(monkeypatch):
    import numpy as np
    monkeypatch.setattr(adapty_cli, "run", fake_run)
    test, u, t, names = _synthetic([("a", 1), ("b", 1), ("c", 1)], [1, 1, 1])
    u["exposed_at"], u["exposure_basis"], u["ab_test_id"] = u.install_at, "exposed_at", test["ab_test_id"]
    test["revenue_bounds_usd"] = [0, 1]
    for flow, mean in [("a", 0.9), ("b", 0.5), ("c", 0.1)]:
        u.loc[u.flow.eq(flow), "revenue"] = mean + np.tile([-0.01, 0.01], 300)
    t["price_usd"] = u.revenue
    res = experiments.judge(test, u, t, names)
    assert res["verdict"] == "stop_and_roll_out" and res["inference"]["arm_count"] == 3
    ordered = u.sort_values(["exposed_at", "user_id"], kind="stable")
    for comparison in res["comparisons"]:
        other = comparison["other"]["name"].lower()
        p = experiments.always_valid_p(ordered.loc[ordered.flow.eq("a"), "revenue"].values,
                                       ordered.loc[ordered.flow.eq(other), "revenue"].values, bounds=[0, 1],
                                       arm_count=3)
        assert comparison["sequential_p"] == p


@pytest.mark.parametrize("case, seed", [("uniform_AA", 801), ("zero_inflated_AA", 802),
                                      ("rare_equal_means", 803), ("uneven_allocation_AA", 804),
                                      ("three_arm_AA", 805), ("positive_effect", 806)])
@pytest.mark.slow
def test_bounded_policy_calibration(case, seed):
    """Reproducible empirical checks of the decision rule (betting sequences at alpha/arms per arm).

    Ville's inequality, not simulation, supplies the conditional guarantee; this guards the implementation.
    """
    from itertools import combinations
    import numpy as np
    from scipy import stats
    rng = np.random.default_rng(seed)
    repetitions, detected = 300, 0
    looks = list(range(100, 1001, 100))
    for _ in range(repetitions):
        bounds = [0, 1]
        if case == "rare_equal_means":
            arms = [1000.0 * (rng.random(1000) < 0.001), 1.0 + rng.choice([-0.01, 0.01], 1000)]
            bounds = [0, 1000]
        elif case == "zero_inflated_AA":
            arms = [50.0 * (rng.random(1000) < 0.1) for _ in range(2)]
            bounds = [0, 50]
        elif case == "uneven_allocation_AA":
            arms = [rng.random(9000), rng.random(1000)]
        elif case == "three_arm_AA":
            arms = [rng.random(1000), rng.binomial(1, 0.5, 1000), np.full(1000, 0.5)]
        elif case == "positive_effect":
            arms = [rng.uniform(0.7, 0.9, 1000), rng.uniform(0.1, 0.3, 1000)]
        else:
            arms = [rng.random(1000), rng.random(1000)]
        level = 0.05 / len(arms)
        paths = [experiments.betting_cs_path((arm - bounds[0]) / (bounds[1] - bounds[0]), level=level)
                 for arm in arms]
        for n in looks:
            idx = [n * 9 - 1 if case == "uneven_allocation_AA" and i == 0 else n - 1 for i in range(len(arms))]
            if any(paths[a][0][idx[a]] > paths[b][1][idx[b]] or paths[b][0][idx[b]] > paths[a][1][idx[a]]
                   for a, b in combinations(range(len(arms)), 2)):
                detected += 1
                break
    interval = stats.binomtest(detected, repetitions).proportion_ci(0.95, method="wilson")
    if case == "positive_effect":
        assert interval.low > 0.8
    else:
        assert interval.high < 0.05
    print("CALIBRATION " + json.dumps({"case": case, "seed": seed, "repetitions": repetitions, "looks": looks,
                                      "method": "betting_cs", "detected": detected,
                                      "rate": detected / repetitions, "ci95": [interval.low, interval.high]}))


def test_betting_path_matches_the_two_arm_check():
    """The calibration's path shortcut and the judge's check agree on the final look."""
    import numpy as np
    rng = np.random.default_rng(9)
    a, b = _zero_inflated(rng, 3000, 0.2), _zero_inflated(rng, 3000, 0.1)
    check = experiments.betting_revenue_check(a, b, bounds=[0, 200])
    (la, ha), (lb, hb) = (experiments.betting_cs_path(x / 200, level=0.025) for x in (a, b))
    assert check["ci95"] == pytest.approx([200 * (la[-1] - hb[-1]), 200 * (ha[-1] - lb[-1])])


@pytest.mark.parametrize("timestamp", ["2026-04-01T02:00:00+02:00", "2026-04-01T00:00:00Z"])
def test_exposure_clock_normalizes_timezones(monkeypatch, timestamp):
    import pandas as pd
    users, tx = _exposure_history()
    users["exposed_at"] = timestamp
    _history_csvs(monkeypatch, users, tx)
    u, t, _ = experiments.load()
    assert u.exposed_at.iloc[0] == pd.Timestamp("2026-04-01T00:00:00Z")
    assert t.day.tolist() == [-81.0, 1.0] and u.revenue.iloc[0] == 100.0


def test_exposure_horizon_includes_its_boundary_and_excludes_later_charges(monkeypatch):
    import pandas as pd
    users, tx = _exposure_history()
    tx["purchased_at"] = pd.to_datetime(["2026-05-31T00:00:00Z", "2026-05-31T00:00:01Z"], utc=True)
    tx["price_usd"] = [100.0, 200.0]
    _history_csvs(monkeypatch, users, tx)
    u, t, _ = experiments.load(observed_through="2026-06-01T00:00:00Z")
    assert u.revenue.iloc[0] == 100.0 and bool(u.matured.iloc[0])
    assert t.paid.tolist() == [True, False]


def test_explicit_cutoff_retains_users_without_any_purchases(monkeypatch):
    users, tx = _exposure_history()
    _history_csvs(monkeypatch, users, tx.iloc[0:0])
    u, t, _ = experiments.load(observed_through="2026-06-01T00:00:00Z")
    assert len(u) == 1 and t.empty and u.revenue.iloc[0] == 0 and bool(u.matured.iloc[0])
