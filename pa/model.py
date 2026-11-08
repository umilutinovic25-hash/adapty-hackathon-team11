"""Intent model + per-segment flow policy, evaluated on the randomized onboarding A/B test.

Pipeline:
  1. Label each offline user with net revenue in the first 14 days after install (trials are $0, refunds subtract).
  2. Train a purchase-intent model on day-one signals only (no flow/placement = no treatment leakage).
  3. Bucket users into low / mid / high intent.
  4. Inside the randomized test "Onboarding: annual trial vs weekly first", compare revenue per user of each arm
     per bucket on a held-out half -> per-bucket best flow vs. the single global winner.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from pa import project_path  # noqa: E402

DATA_DIR = project_path(os.getenv("OFFLINE_DATA_DIR", "../hackaton_adapty/adapty-seed-data-hack/data/offline"))
HORIZON_DAYS = 14
ONB_TEST = "Onboarding: annual trial vs weekly first"
CAT = ["platform", "country", "acquisition_channel", "onboarding_goal", "push_opt_in", "app_version"]
NUM = ["ai_edits_first_day", "photos_imported"]
BUCKETS = ["low", "mid", "high"]
SEED = 7


def load() -> pd.DataFrame:
    u = pd.read_csv(f"{DATA_DIR}/users.csv.gz", parse_dates=["install_at"])
    t = pd.read_csv(f"{DATA_DIR}/transactions.csv.gz", parse_dates=["purchased_at", "refunded_at"])
    t = t.merge(u[["user_id", "install_at"]], on="user_id")
    in_window = t.purchased_at <= t.install_at + pd.Timedelta(days=HORIZON_DAYS)
    paid = t.offer.ne("free_trial")
    t["net_usd"] = np.where(paid & in_window, t.price_usd, 0.0)
    t.loc[t.refunded_at.notna() & paid & in_window, "net_usd"] = 0.0
    rev = t.groupby("user_id").net_usd.sum()
    u["revenue_14d"] = u.user_id.map(rev).fillna(0.0)
    u["payer_14d"] = (u.revenue_14d > 0).astype(int)
    return u


def build_model() -> Pipeline:
    pre = ColumnTransformer([("cat", OneHotEncoder(handle_unknown="ignore", min_frequency=50, sparse_output=False), CAT)],
                            remainder="passthrough")
    clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.06, max_leaf_nodes=31, random_state=SEED)
    return Pipeline([("pre", pre), ("clf", clf)])


@dataclass
class Result:
    auc: float
    thresholds: tuple[float, float]
    arms_by_bucket: dict
    policy: dict
    global_winner: dict
    lift: dict
    drivers: list


def bootstrap_diff(a: np.ndarray, b: np.ndarray, n: int = 2000) -> tuple[float, float]:
    from pa.experiments import boot  # noqa: PLC0415 - one bootstrap for the project; experiments imports this module
    lo, hi, _ = boot(a, b, n)
    return lo, hi


def run() -> tuple[Result, Pipeline, pd.DataFrame]:
    u = load()
    train, test = train_test_split(u, test_size=0.5, random_state=SEED, stratify=u.payer_14d)
    model = build_model().fit(train[CAT + NUM], train.payer_14d)
    test = test.copy()
    test["intent_score"] = model.predict_proba(test[CAT + NUM])[:, 1]
    auc = roc_auc_score(test.payer_14d, test.intent_score)

    t_lo, t_hi = np.quantile(test.intent_score, [0.4, 0.8])
    test["bucket"] = pd.cut(test.intent_score, [-1, t_lo, t_hi, 2], labels=BUCKETS)

    exp = test[test.ab_test == ONB_TEST]
    arms = sorted(exp.flow.unique())
    arms_by_bucket, policy = {}, {}
    for b in BUCKETS:
        seg = exp[exp.bucket == b]
        stats = {a: {"users": int((seg.flow == a).sum()),
                     "rev_per_user": round(float(seg[seg.flow == a].revenue_14d.mean()), 3),
                     "payer_rate": round(float(seg[seg.flow == a].payer_14d.mean()), 4)} for a in arms}
        best = max(arms, key=lambda a: stats[a]["rev_per_user"])
        other = [a for a in arms if a != best][0]
        ci = bootstrap_diff(seg[seg.flow == best].revenue_14d.values, seg[seg.flow == other].revenue_14d.values)
        arms_by_bucket[b] = {"arms": stats, "best": best, "diff_ci95": [round(ci[0], 3), round(ci[1], 3)]}
        policy[b] = best

    overall = {a: float(exp[exp.flow == a].revenue_14d.mean()) for a in arms}
    winner = max(overall, key=overall.get)
    # Policy value: for each bucket, revenue/user of the policy arm, weighted by bucket share of the experiment.
    shares = exp.bucket.value_counts(normalize=True)
    policy_rpu = sum(shares[b] * arms_by_bucket[b]["arms"][policy[b]]["rev_per_user"] for b in BUCKETS)
    winner_rpu = overall[winner]
    lift = {"policy_rev_per_user": round(policy_rpu, 3), "global_winner_rev_per_user": round(winner_rpu, 3),
            "lift_pct": round(100 * (policy_rpu / winner_rpu - 1), 2)}

    drivers = (test.groupby(pd.cut(test.ai_edits_first_day, [-1, 0, 2, 5, 100]), observed=True)
               .payer_14d.mean().round(4).rename_axis("ai_edits_first_day").reset_index()
               .astype({"ai_edits_first_day": str}).to_dict("records"))
    res = Result(round(auc, 4), (round(float(t_lo), 4), round(float(t_hi), 4)), arms_by_bucket, policy,
                 {"flow": winner, "rev_per_user": round(winner_rpu, 3)}, lift, drivers)
    return res, model, test


if __name__ == "__main__":
    res, _, _ = run()
    print(json.dumps(res.__dict__, indent=2, default=str))
