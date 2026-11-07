"""Experiment Autopilot: judge Adapty A/B tests on the full history, with experiment-platform health checks.

Why: Adapty credits a purchase to an A/B test only if it happened after the test was set up, so the dashboard judges
a test on recent traffic, often before trial revenue has had time to arrive. The offline dataset records which arm
every user saw over 4 months, so we can judge tests on matured revenue.

Checks borrowed from experimentation platforms (GrowthBook / Statsig / Eppo):
  - SRM (traffic split) and randomization balance (pre-exposure intent score per arm)
  - guardrail metrics (refund rate, billing-issue rate)
  - metric maturity (how much of 60-day revenue each arm has collected by day 1 / 7 / 30)
  - segment consistency with a Bonferroni correction
  - sample size / days to a decision for undecided and draft tests
"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.model_selection import KFold

from pa import adapty_cli, project_path
from pa.model import CAT, NUM, DATA_DIR, build_model

HORIZON_DAYS = 60  # the onboarding test's own goal: "60-day revenue per install"
MATURITY_DAYS = [1, 7, 30, 60]
SEED_SCRIPT = project_path(os.getenv("SEED_SCRIPT", "../hackaton_adapty/adapty-seed-data-hack/seed_adapty.py"))
FALLBACK_FLOW_NAMES = {
    "onb_trial": "Onboarding - Annual trial", "onb_weekly": "Onboarding - Weekly first",
    "main": "Main - 3 plans", "main_lifetime": "Main - Lifetime anchor",
    "settings": "Settings - Upgrade", "ai_limit": "AI limit reached - Pro", "winback": "Win-back - Monthly",
}
LIVE_USERS, OFFLINE_USERS = 5000, 50000  # Adapty account vs offline copy of the same population
MDE = 0.10  # smallest relative revenue lift worth detecting, for sample-size planning
SEED = 7
OUT = Path("out")


def flow_names() -> dict[str, str]:
    """Offline flow code -> Adapty flow name, read from the seed script that created the flows."""
    try:
        found = dict(re.findall(r'Flow\("(\w+)",\s*"([^"]+)"', SEED_SCRIPT.read_text()))
    except OSError:
        found = {}
    return {**FALLBACK_FLOW_NAMES, **found}  # a flow the regex misses still maps


REPORT_SCHEMA = 3  # bump when the report's keys change; readers rebuild older reports


def placement_codes() -> dict[str, str]:
    """Adapty placement id -> developer id (the offline dataset's placement code)."""
    try:
        return {p["id"]: p["developer_id"] for p in adapty_cli.run("placements", "list")["data"]}
    except (RuntimeError, KeyError):
        return {}


# ---------------------------------------------------------------- data
def load(horizon: int = HORIZON_DAYS) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    u = pd.read_csv(f"{DATA_DIR}/users.csv.gz", parse_dates=["install_at"])
    t = pd.read_csv(f"{DATA_DIR}/transactions.csv.gz",
                    parse_dates=["purchased_at", "refunded_at", "billing_issue_detected_at"])
    end = t.purchased_at.max()
    t = t.merge(u[["user_id", "install_at"]], on="user_id")
    t["day"] = (t.purchased_at - t.install_at).dt.total_seconds() / 86400
    t["paid"] = t.offer.ne("free_trial") & (t.day <= horizon)
    net = t[t.paid & t.refunded_at.isna()]
    u["revenue"] = u.user_id.map(net.groupby("user_id").price_usd.sum()).fillna(0.0)
    u["payer"] = (u.revenue > 0).astype(int)
    u["matured"] = u.install_at <= end - pd.Timedelta(days=horizon)
    return u, t, end


def add_intent(u: pd.DataFrame) -> pd.DataFrame:
    """Cross-fitted intent score (2 folds): no user is scored by a model that saw them."""
    u = u.copy()
    u["intent_score"] = np.nan
    for tr, te in KFold(2, shuffle=True, random_state=SEED).split(u):
        m = build_model().fit(u.iloc[tr][CAT + NUM], u.iloc[tr].payer)
        u.iloc[te, u.columns.get_loc("intent_score")] = m.predict_proba(u.iloc[te][CAT + NUM])[:, 1]
    lo, hi = np.quantile(u.intent_score, [0.4, 0.8])
    u["intent"] = pd.cut(u.intent_score, [-1, lo, hi, 2], labels=["low", "mid", "high"]).astype(str)
    return u


# ---------------------------------------------------------------- statistics
def boot(a: np.ndarray, b: np.ndarray, n: int = 3000, alpha: float = 0.05) -> tuple[float, float, float]:
    """(1-alpha) CI of mean(a)-mean(b) and P(mean(a) > mean(b))."""
    rng = np.random.default_rng(SEED)
    d = np.array([rng.choice(a, a.size).mean() - rng.choice(b, b.size).mean() for _ in range(n)])
    return (float(np.percentile(d, 100 * alpha / 2)), float(np.percentile(d, 100 * (1 - alpha / 2))),
            float((d > 0).mean()))


def two_prop_p(x1: int, n1: int, x2: int, n2: int) -> float:
    if min(n1, n2) == 0:
        return 1.0
    p = (x1 + x2) / (n1 + n2)
    se = np.sqrt(p * (1 - p) * (1 / n1 + 1 / n2)) or 1e-12
    return float(2 * stats.norm.sf(abs(x1 / n1 - x2 / n2) / se))


def sample_size_per_arm(values: np.ndarray, mde: float = MDE, alpha: float = 0.05, power: float = 0.8) -> int:
    mean, sd = float(values.mean()), float(values.std(ddof=1))
    if mean <= 0:
        return 0
    z = stats.norm.ppf(1 - alpha / 2) + stats.norm.ppf(power)
    return int(np.ceil(2 * (z * sd / (mde * mean)) ** 2))


# ---------------------------------------------------------------- checks
def arms_of(test: dict) -> list[dict]:
    """Arms of a flow or paywall test, as {name, content_id, kind, weight}."""
    if test["type"] == "flow":
        return [{"name": a["flow_name"], "content_id": a["flow_id"], "kind": "flow", "weight": a["weight"]}
                for a in test.get("flows", [])]
    return [{"name": a.get("paywall_name") or a.get("name"), "content_id": a.get("paywall_id"), "kind": "paywall",
             "weight": a["weight"]} for a in test.get("paywalls", [])]


def maturity(t: pd.DataFrame, users: pd.Series) -> dict:
    """Share of each user group's 60-day revenue already collected by day d."""
    tx = t[t.user_id.isin(users) & t.paid & t.refunded_at.isna()]
    total = tx.price_usd.sum()
    return {d: round(float(tx[tx.day <= d].price_usd.sum() / total), 3) if total else 0.0 for d in MATURITY_DAYS}


def rate_p_by_user(t: pd.DataFrame, a_users: pd.Series, b_users: pd.Series, event: str, n: int = 2000) -> float:
    """Two-sided p-value for a per-charge rate difference, bootstrapping USERS (renewals of one user are
    correlated, so treating every charge as independent would make p too small)."""
    def per_user(users):
        tx = t[t.user_id.isin(users) & t.paid]
        g = tx.assign(ev=tx[event].notna()).groupby("user_id").agg(ev=("ev", "sum"), n=("ev", "size"))
        return g.ev.values, g.n.values
    (ea, na), (eb, nb) = per_user(a_users), per_user(b_users)
    if na.sum() == 0 or nb.sum() == 0:
        return 1.0
    rng = np.random.default_rng(SEED)
    diffs = np.empty(n)
    for i in range(n):
        ia, ib = rng.integers(0, ea.size, ea.size), rng.integers(0, eb.size, eb.size)
        diffs[i] = ea[ia].sum() / na[ia].sum() - eb[ib].sum() / nb[ib].sum()
    return float(min(1.0, 2 * min((diffs <= 0).mean(), (diffs >= 0).mean())))


def guardrails(t: pd.DataFrame, users: pd.Series) -> dict:
    """Rates per paid transaction (charge attempt), not per payer: weekly plans charge more often, so a per-payer
    rate would penalise them for exposure, not for worse quality."""
    tx = t[t.user_id.isin(users) & t.paid]
    n = len(tx)
    return {"paid_tx": int(n), "payers": int(tx.user_id.nunique()),
            "refunds": int(tx.refunded_at.notna().sum()),
            "refund_rate": round(float(tx.refunded_at.notna().mean()), 4) if n else 0.0,
            "billing_issues": int(tx.billing_issue_detected_at.notna().sum()),
            "billing_issue_rate": round(float(tx.billing_issue_detected_at.notna().mean()), 4) if n else 0.0}


def live_view(test: dict, arms: list[dict]) -> dict | None:
    """What the Adapty dashboard sees: revenue per arm since the test started (Adapty analytics API)."""
    try:
        start = test["started_at"][:10] if test.get("started_at") else None
        if not start:
            return None
        group = "flow_id" if test["type"] == "flow" else "paywall_id"
        res = adapty_cli.run("analytics", "chart", "--chart", "revenue", "--date-from", start,
                             "--date-to", date.today().isoformat(), "--group-by", group,
                             "--filter", f"placement_id={test['placement']['placement_id']}")
        by_id = {s["key"]: s["value"] for s in res["data"]["segments"] if not s["is_total"]}
        return {"since": start, "revenue": {a["name"]: round(float(by_id.get(a["content_id"], 0.0)), 2) for a in arms}}
    except (RuntimeError, KeyError, ValueError):
        return None


def unsupported(base: dict, test: dict, why: str) -> dict:
    """A test we can't judge on history still shows up, with what Adapty sees, instead of vanishing."""
    return {**base, "verdict": "unsupported", "reasons": [why], "live_view": live_view(test, base["arms_config"])}


def judge(test: dict, u: pd.DataFrame, t: pd.DataFrame, names: dict, placements: dict | None = None) -> dict | None:
    arms = arms_of(test)
    code = {v: k for k, v in names.items()}
    base = {"ab_test_id": test["ab_test_id"], "title": test["title"], "state": test["state"], "type": test["type"],
            "placement": test["placement"]["title"], "placement_id": test["placement"]["placement_id"],
            "placement_code": (placements or {}).get(test["placement"]["placement_id"]),
            "segment_ids": test.get("segment_ids", []), "goal": test.get("goal"), "horizon_days": HORIZON_DAYS,
            "arms_config": arms}
    if test["state"] == "draft":
        return plan_draft(base, arms, u, code)
    if test["type"] != "flow":
        return unsupported(base, test, "paywall test: the offline history has flows only, so it can't be judged on "
                                       "matured revenue yet (an Adapty S3/BigQuery export would add it)")
    if len(arms) != 2:
        return unsupported(base, test, f"{len(arms)} arms: only two-arm tests are judged")
    e_all = u[u.ab_test == test["title"]]
    if e_all.empty:
        return unsupported(base, test, "no history for this test in the offline dataset")

    e = e_all[e_all.matured]
    stats_by_arm = []
    for a in arms:
        s = e[e.flow == code.get(a["name"])]
        stats_by_arm.append({**a, "users": int(len(s)), "rev_per_user": round(float(s.revenue.mean()), 2),
                             "payer_rate": round(float(s.payer.mean()), 4), "_ids": s.user_id})
    if min(a["users"] for a in stats_by_arm) < 100:
        return unsupported(base, test, "fewer than 100 matured users in an arm: too early to judge")
    win, lose = sorted(stats_by_arm, key=lambda a: -a["rev_per_user"])
    w_rev, l_rev = e[e.user_id.isin(win["_ids"])].revenue.values, e[e.user_id.isin(lose["_ids"])].revenue.values
    lo, hi, p = boot(w_rev, l_rev)
    gap = win["rev_per_user"] - lose["rev_per_user"]

    # health: SRM + randomization balance on the pre-exposure intent score (GrowthBook threshold p < 0.001)
    w = np.array([win["weight"], lose["weight"]], dtype=float)
    srm_p = float(stats.chisquare([win["users"], lose["users"]], w / w.sum() * (win["users"] + lose["users"])).pvalue)
    bal_p = float(stats.ttest_ind(e[e.user_id.isin(win["_ids"])].intent_score,
                                  e[e.user_id.isin(lose["_ids"])].intent_score, equal_var=False).pvalue)

    # guardrails: the winner must not raise refunds or billing issues
    gw, gl = guardrails(t, win["_ids"]), guardrails(t, lose["_ids"])
    refund_p = rate_p_by_user(t, win["_ids"], lose["_ids"], "refunded_at")
    billing_p = rate_p_by_user(t, win["_ids"], lose["_ids"], "billing_issue_detected_at")
    guard_fail = [(n, pv) for n, pv, worse in [
        ("refund rate", refund_p, gw["refund_rate"] > gl["refund_rate"]),
        ("billing-issue rate", billing_p, gw["billing_issue_rate"] > gl["billing_issue_rate"])] if worse and pv < 0.05]

    # metric maturity: early comparisons favour the arm that collects its revenue sooner
    mw, ml = maturity(t, win["_ids"]), maturity(t, lose["_ids"])
    early_bias = None
    if abs(mw[1] - ml[1]) >= 0.25:
        fast, slow = (win, lose) if mw[1] > ml[1] else (lose, win)
        fm, sm = (mw, ml) if fast is win else (ml, mw)
        early_bias = (f"By day 1, {fast['name']} has collected {fm[1]:.0%} of its 60-day revenue and "
                      f"{slow['name']} {sm[1]:.0%}. A dashboard read in the first days favours {fast['name']}, "
                      f"whatever the final winner.")

    # segment consistency, Bonferroni-corrected
    segs = []
    groups = [(col, val, g) for col in ["intent", "platform", "acquisition_channel"] for val, g in e.groupby(col)]
    k = max(1, sum(1 for *_, g in groups if min(g.user_id.isin(win["_ids"]).sum(), g.user_id.isin(lose["_ids"]).sum()) >= 80))
    for col, val, g in groups:
        a = g[g.user_id.isin(win["_ids"])].revenue.values
        b = g[g.user_id.isin(lose["_ids"])].revenue.values
        if min(a.size, b.size) < 80:
            continue
        slo, shi, sp = boot(a, b, 1000, alpha=0.05 / k)
        segs.append({"dimension": col, "value": str(val), "users": int(a.size + b.size),
                     "winner_rpu": round(float(a.mean()), 2), "other_rpu": round(float(b.mean()), 2),
                     "p_winner_better": round(sp, 3), "ci": [round(slo, 2), round(shi, 2)]})
    flipped = [s for s in segs if s["ci"][1] < 0]
    unclear = [s for s in segs if s["ci"][0] <= 0 <= s["ci"][1]]

    # money: one month of new installs, each followed for 60 days
    days = (e_all.install_at.max() - e_all.install_at.min()).days or 1
    daily_users = len(e_all) / days * (LIVE_USERS / OFFLINE_USERS)
    lost_month = daily_users * 30 * (lose["weight"] / 100) * gap

    decided = lo > 0 and p > 0.99
    healthy = srm_p > 0.01 and bal_p > 0.001
    n_needed = sample_size_per_arm(np.concatenate([w_rev, l_rev]))
    if test["state"] == "completed":
        verdict = "completed_confirmed" if decided else "completed_inconclusive"
    elif not healthy:
        verdict = "check_data"
    elif decided and guard_fail:
        verdict = "guardrail_failed"
    elif decided and flipped:
        verdict = "split_by_segment"
    elif decided:
        verdict = "stop_and_roll_out"
    else:
        verdict = "keep_running"

    reasons = [f"{win['name']} earns ${win['rev_per_user']:.2f} vs ${lose['rev_per_user']:.2f} per user in "
               f"{HORIZON_DAYS}-day revenue (95% CI of the gap ${lo:.2f}…${hi:.2f}, P(better) {p:.1%})",
               f"traffic split {'OK' if srm_p > 0.01 else 'BROKEN'} (SRM p={srm_p:.2f}), groups "
               f"{'balanced' if bal_p > 0.001 else 'UNBALANCED'} on intent (p={bal_p:.2f})",
               "guardrails OK (refunds, billing issues)" if not guard_fail else
               "guardrail failed: " + ", ".join(f"{n} worse (p={pv:.3f})" for n, pv in guard_fail),
               f"segments (Bonferroni-corrected): winner clearly ahead in {len(segs) - len(flipped) - len(unclear)} "
               f"of {len(segs)}, unclear in {len(unclear)}, reversed in {len(flipped)}"]
    if verdict == "keep_running":
        reasons.append(f"needs ≈{n_needed:,} matured users per arm to detect a {MDE:.0%} lift")

    for a in (win, lose):
        a.pop("_ids")
    return {**base, "matured_users": int(len(e)), "arms": [win, lose], "winner": win, "loser": lose,
            "lift_pct": round(100 * gap / lose["rev_per_user"], 1) if lose["rev_per_user"] else None,
            "diff_ci95": [round(lo, 2), round(hi, 2)], "p_winner_better": round(p, 4),
            "health": {"srm_pvalue": round(srm_p, 4), "balance_pvalue": round(bal_p, 4), "healthy": healthy},
            "guardrails": {"winner": gw, "other": gl, "refund_p": round(refund_p, 4),
                           "billing_p": round(billing_p, 4), "failed": [n for n, _ in guard_fail]},
            "maturity": {"winner": mw, "other": ml, "early_bias": early_bias},
            "live_view": live_view(test, arms),
            "daily_users_in_test": round(daily_users, 1), "lost_per_month_usd": round(lost_month, 2),
            "segments": segs, "segments_flipped": len(flipped), "segments_unclear": len(unclear),
            "sample_size_per_arm": n_needed, "verdict": verdict, "reasons": reasons}


def plan_draft(base: dict, arms: list[dict], u: pd.DataFrame, code: dict) -> dict:
    """A draft test has no data yet: size it from the users who reached its placement."""
    # users who reached THIS placement (a flow can be shown at several placements)
    pl = u[u.placement == base["placement_code"]] if base.get("placement_code") else u.iloc[0:0]
    hist = pl[pl.matured]
    days = (pl.install_at.max() - pl.install_at.min()).days or 1 if not pl.empty else 1
    daily = len(pl) / days * (LIVE_USERS / OFFLINE_USERS) if not pl.empty else 0
    n = sample_size_per_arm(hist.revenue.values) if len(hist) > 30 else None
    days_needed = int(np.ceil(n * len(arms) / daily)) + HORIZON_DAYS if n and daily else None
    # smallest lift detectable with 30 days of enrolment at this traffic (n scales with 1/MDE^2)
    n30 = daily * 30 / len(arms)
    mde30 = MDE * np.sqrt(n / n30) if n and n30 else None
    return {**base, "verdict": "draft_plan", "baseline_rev_per_user": round(float(hist.revenue.mean()), 2)
            if len(hist) else None, "sample_size_per_arm": n, "daily_users_at_placement": round(daily, 1),
            "days_to_decision": days_needed, "mde_with_30_days_pct": round(100 * mde30, 0) if mde30 else None,
            "recommendation": ("don't start: at this traffic the test can't reach a decision in a useful time"
                               if days_needed and days_needed > 120 else "start it"),
            "reasons": [f"baseline ${hist.revenue.mean():.2f} per user at this placement ({len(hist):,} matured users)"
                        if len(hist) else "no history at this placement",
                        f"≈{n:,} users per arm to detect a {MDE:.0%} lift; at ≈{daily:.0f} users/day that is "
                        f"≈{days_needed} days including the {HORIZON_DAYS}-day revenue window"
                        if days_needed else "traffic too low to size",
                        f"with 30 days of traffic it can only detect a lift of ≈{100 * mde30:.0f}% or more"
                        if mde30 else ""]}


# ---------------------------------------------------------------- report
def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def build_report() -> dict:
    u, t, end = load()
    u = add_intent(u)
    names, placements = flow_names(), placement_codes()
    tests = adapty_cli.list_tests()
    judged = [r for tst in tests if tst["state"] in ("live", "completed", "draft")
              and (r := judge(tst, u, t, names, placements))]
    m = u[u.matured]
    bins = pd.cut(m.ai_edits_first_day, [-1, 0, 2, 5, 1000], labels=["0", "1–2", "3–5", "6+"])
    report = {
        "schema": REPORT_SCHEMA, "app_id": adapty_cli.APP_ID, "data_end": str(end), "horizon_days": HORIZON_DAYS,
        "segment_names": {s["id"]: s["title"] for s in adapty_cli.run("segments", "list")["data"]},
        "tests": judged,
        "total_lost_per_month_usd": round(sum(x.get("lost_per_month_usd", 0) for x in judged
                                              if x["state"] == "live" and x["verdict"] == "stop_and_roll_out"), 2),
        "intent": {
            "payer_rate_by_ai_edits_day1": {str(k): round(float(v), 3)
                                            for k, v in m.groupby(bins, observed=False).payer.mean().fillna(0).items()},
            "rev_per_user_by_intent": {k: round(float(m[m.intent == k].revenue.mean() or 0), 2)
                                       for k in ["low", "mid", "high"]},
            "payer_rate_by_intent": {k: round(float(m[m.intent == k].payer.mean() or 0), 3)
                                     for k in ["low", "mid", "high"]}},
    }
    OUT.mkdir(exist_ok=True)
    write_atomic(OUT / "report.json", json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    r = build_report()
    for x in r["tests"]:
        print(f"\n{x['title']} [{x['state']}] -> {x['verdict']}")
        for reason in x["reasons"]:
            print("   -", reason)
        if x.get("maturity", {}).get("early_bias"):
            print("   ! ", x["maturity"]["early_bias"])
        if x.get("live_view"):
            print("   live (Adapty since start):", x["live_view"]["revenue"])
    print("\ntotal lost/mo (live, stop_and_roll_out):", r["total_lost_per_month_usd"])
