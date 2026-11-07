"""Experiment Autopilot: judge live Adapty A/B tests on the full offline history.

Adapty attributes purchases to an A/B test only after the test exists, so the dashboard shows only today's users.
The offline dataset records which arm every user saw over 4 months, so we can decide tests the dashboard can't.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.model_selection import KFold

from pa import adapty_cli
from pa.model import CAT, NUM, DATA_DIR, build_model

HORIZON_DAYS = 60  # the onboarding test's own goal: "60-day revenue per install"
# offline flow code -> Adapty flow name (flows/*.json in the seed repo)
FLOW_NAMES = {
    "onb_trial": "Onboarding - Annual trial", "onb_weekly": "Onboarding - Weekly first",
    "main": "Main - 3 plans", "main_lifetime": "Main - Lifetime anchor",
    "settings": "Settings - Upgrade", "ai_limit": "AI limit reached - Pro", "winback": "Win-back - Monthly",
}
LIVE_USERS = 5000      # users replayed into the Adapty account
OFFLINE_USERS = 50000  # same population, offline copy
SEED = 7
OUT = Path("out")


def load(horizon: int = HORIZON_DAYS) -> tuple[pd.DataFrame, pd.Timestamp]:
    u = pd.read_csv(f"{DATA_DIR}/users.csv.gz", parse_dates=["install_at"])
    t = pd.read_csv(f"{DATA_DIR}/transactions.csv.gz", parse_dates=["purchased_at", "refunded_at"])
    end = t.purchased_at.max()
    t = t.merge(u[["user_id", "install_at"]], on="user_id")
    paid = (t.offer.ne("free_trial") & t.refunded_at.isna()
            & (t.purchased_at <= t.install_at + pd.Timedelta(days=horizon)))
    u["revenue"] = u.user_id.map(t[paid].groupby("user_id").price_usd.sum()).fillna(0.0)
    u["payer"] = (u.revenue > 0).astype(int)
    u["matured"] = u.install_at <= end - pd.Timedelta(days=horizon)
    return u, end


def add_intent(u: pd.DataFrame) -> pd.DataFrame:
    """Cross-fitted intent score (2 folds) so no user is scored by a model that saw them."""
    u = u.copy()
    u["intent_score"] = np.nan
    for tr, te in KFold(2, shuffle=True, random_state=SEED).split(u):
        m = build_model().fit(u.iloc[tr][CAT + NUM], u.iloc[tr].payer)
        u.iloc[te, u.columns.get_loc("intent_score")] = m.predict_proba(u.iloc[te][CAT + NUM])[:, 1]
    lo, hi = np.quantile(u.intent_score, [0.4, 0.8])
    u["intent"] = pd.cut(u.intent_score, [-1, lo, hi, 2], labels=["low", "mid", "high"]).astype(str)
    return u


def boot(a: np.ndarray, b: np.ndarray, n: int = 3000) -> tuple[float, float, float]:
    """95% CI of mean(a)-mean(b) and P(mean(a) > mean(b))."""
    rng = np.random.default_rng(SEED)
    d = np.array([rng.choice(a, a.size).mean() - rng.choice(b, b.size).mean() for _ in range(n)])
    return float(np.percentile(d, 2.5)), float(np.percentile(d, 97.5)), float((d > 0).mean())


def srm_pvalue(counts: list[int], weights: list[float]) -> float:
    from scipy.stats import chisquare  # noqa: PLC0415
    exp = np.array(weights) / sum(weights) * sum(counts)
    return float(chisquare(counts, exp).pvalue)


def judge(test: dict, u: pd.DataFrame, end: pd.Timestamp) -> dict | None:
    e_all = u[u.ab_test == test["title"]]
    if e_all.empty:
        return None
    e = e_all[e_all.matured]
    arms_cfg = {a["flow_name"]: a for a in test["flows"]} if test["type"] == "flow" else {}
    code_by_name = {v: k for k, v in FLOW_NAMES.items()}
    arms = []
    for name, cfg in arms_cfg.items():
        s = e[e.flow == code_by_name.get(name)]
        arms.append({"name": name, "flow_id": cfg["flow_id"], "weight": cfg["weight"], "users": int(len(s)),
                     "rev_per_user": round(float(s.revenue.mean()), 2), "payer_rate": round(float(s.payer.mean()), 4)})
    if len(arms) != 2 or min(a["users"] for a in arms) < 100:
        return None
    win, lose = sorted(arms, key=lambda a: -a["rev_per_user"])
    w_rev = e[e.flow == code_by_name[win["name"]]].revenue.values
    l_rev = e[e.flow == code_by_name[lose["name"]]].revenue.values
    lo, hi, p = boot(w_rev, l_rev)

    # money left on the table: daily installs entering the test * share on the losing arm * RPU gap
    days = (e_all.install_at.max() - e_all.install_at.min()).days or 1
    daily_users = len(e_all) / days * (LIVE_USERS / OFFLINE_USERS)
    gap = win["rev_per_user"] - lose["rev_per_user"]
    lost_month = daily_users * 30 * (lose["weight"] / 100) * gap

    # heterogeneity: does the winner hold in every segment?
    segs = []
    for col in ["intent", "platform", "acquisition_channel"]:
        for val, g in e.groupby(col):
            a = g[g.flow == code_by_name[win["name"]]].revenue.values
            b = g[g.flow == code_by_name[lose["name"]]].revenue.values
            if min(a.size, b.size) < 80:
                continue
            slo, shi, sp = boot(a, b, 1000)
            segs.append({"dimension": col, "value": str(val), "users": int(a.size + b.size),
                         "winner_rpu": round(float(a.mean()), 2), "other_rpu": round(float(b.mean()), 2),
                         "p_winner_better": round(sp, 3), "ci95": [round(slo, 2), round(shi, 2)]})
    flipped = [s for s in segs if s["ci95"][1] < 0]
    unclear = [s for s in segs if s["ci95"][0] <= 0 <= s["ci95"][1]]

    decided = lo > 0 and p > 0.99
    if test["state"] == "completed":
        verdict = "completed_confirmed" if decided else "completed_inconclusive"
    else:
        verdict = ("stop_and_roll_out" if decided and not flipped else
                   "split_by_segment" if decided and flipped else "keep_running")
    return {
        "ab_test_id": test["ab_test_id"], "title": test["title"], "state": test["state"],
        "placement": test["placement"]["title"], "segment_ids": test.get("segment_ids", []),
        "goal": test.get("goal"), "horizon_days": HORIZON_DAYS, "matured_users": int(len(e)),
        "arms": arms, "winner": win, "loser": lose,
        "lift_pct": round(100 * gap / lose["rev_per_user"], 1) if lose["rev_per_user"] else None,
        "diff_ci95": [round(lo, 2), round(hi, 2)], "p_winner_better": round(p, 4),
        "srm_pvalue": round(srm_pvalue([a["users"] for a in arms], [a["weight"] for a in arms]), 4),
        "daily_users_in_test": round(daily_users, 1), "lost_per_month_usd": round(lost_month, 2),
        "lost_per_month_per_1k_installs_usd": round(1000 * (lose["weight"] / 100) * gap, 2),
        "segments": segs, "segments_flipped": len(flipped), "segments_unclear": len(unclear),
        "verdict": verdict,
        "dashboard_note": "Adapty shows this test with today's users only; history is 'No Data' there.",
    }


def build_report() -> dict:
    u, end = load()
    u = add_intent(u)
    tests = adapty_cli.list_tests()
    judged = [r for t in tests if t["state"] in ("live", "completed") and (r := judge(t, u, end))]
    drivers = (u[u.matured].groupby(pd.cut(u.ai_edits_first_day, [-1, 0, 2, 5, 100]), observed=True)
               .payer.mean().round(3))
    report = {
        "app_id": adapty_cli.APP_ID, "data_end": str(end), "horizon_days": HORIZON_DAYS,
        "segment_names": {s["id"]: s["title"] for s in adapty_cli.run("segments", "list")["data"]},
        "tests": judged,
        "total_lost_per_month_usd": round(sum(t["lost_per_month_usd"] for t in judged if t["state"] == "live"
                                              and t["verdict"] != "keep_running"), 2),
        "intent": {"payer_rate_by_ai_edits_day1": {str(k): v for k, v in drivers.items()},
                   "payer_rate_by_intent": u[u.matured].groupby("intent").payer.mean().round(3).to_dict(),
                   "rev_per_user_by_intent": u[u.matured].groupby("intent").revenue.mean().round(2).to_dict()},
    }
    OUT.mkdir(exist_ok=True)
    (OUT / "report.json").write_text(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    r = build_report()
    for t in r["tests"]:
        print(f"{t['title']} [{t['state']}] -> {t['verdict']} | winner {t['winner']['name']} "
              f"{t['winner']['rev_per_user']} vs {t['loser']['rev_per_user']} (+{t['lift_pct']}%) "
              f"CI{t['diff_ci95']} p={t['p_winner_better']} SRM p={t['srm_pvalue']} "
              f"lost/mo ${t['lost_per_month_usd']} flipped={t['segments_flipped']} unclear={t['segments_unclear']}")
    print("total lost/mo (live, decided):", r["total_lost_per_month_usd"])
    print(json.dumps(r["intent"], indent=1))
