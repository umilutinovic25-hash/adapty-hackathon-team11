"""Experiment Autopilot: judge Adapty A/B tests on the full history, with experiment-platform health checks.

Why: Adapty credits a purchase to an A/B test only if it happened after the test was set up, so the dashboard judges
a test on recent traffic, often before trial revenue has had time to arrive. The offline dataset records which arm
every user saw over 4 months, so we can judge tests on matured revenue.

Checks borrowed from experimentation platforms (GrowthBook / Statsig / Eppo):
  - SRM (traffic split) and intent-score balance (causal interpretation requires pre-exposure features)
  - guardrail metrics (refund rate, billing-issue rate)
  - metric maturity (how much of 60-day revenue each arm has collected by day 1 / 7 / 30)
  - segment consistency with a Bonferroni correction
  - sample size / days to a decision for undecided and draft tests
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.model_selection import KFold

from pa import adapty_cli, out_dir, project_path
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


def flow_names() -> dict[str, str]:
    """Offline flow code -> Adapty flow name, read from the seed script that created the flows."""
    try:
        found = dict(re.findall(r'Flow\("(\w+)",\s*"([^"]+)"', SEED_SCRIPT.read_text()))
    except OSError:
        found = {}
    return {**FALLBACK_FLOW_NAMES, **found}  # a flow the regex misses still maps


REPORT_SCHEMA = 8  # bump when the report's keys change; readers rebuild older reports
def history_apps() -> set[str]:
    """Apps seeded from the offline dataset: the default app always, plus OFFLINE_APP_IDS (comma-separated),
    e.g. a rehearsal copy. Read at call time so a changed environment takes effect."""
    extra = os.getenv("OFFLINE_APP_IDS", os.getenv("OFFLINE_APP_ID", ""))
    return {adapty_cli.DEFAULT_APP_ID, *(a.strip() for a in extra.split(",") if a.strip())}


def placement_codes() -> dict[str, str]:
    """Adapty placement id -> developer id (the offline dataset's placement code)."""
    try:
        return {p["id"]: p["developer_id"] for p in adapty_cli.run("placements", "list")["data"]}
    except (RuntimeError, KeyError):
        return {}


# ---------------------------------------------------------------- data
def load(horizon: int = HORIZON_DAYS, *, observed_through: str | pd.Timestamp | None = None
         ) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    """Revenue since exposure, with an explicit observation cutoff when available.

    Old synthetic CSVs have no exposure timestamp; retain their install-time proxy and label it as such.
    An explicitly supplied exposure must be valid: missing values never silently become installation dates.
    """
    u = pd.read_csv(f"{DATA_DIR}/users.csv.gz", parse_dates=["install_at"])
    t = pd.read_csv(f"{DATA_DIR}/transactions.csv.gz",
                    parse_dates=["purchased_at", "refunded_at", "billing_issue_detected_at"])
    explicit = "exposed_at" in u
    u["install_at"] = pd.to_datetime(u.install_at, utc=True)
    u["exposed_at"] = pd.to_datetime(u.exposed_at if explicit else u.install_at,
                                     utc=True, errors="coerce", format="mixed")
    if u.exposed_at.isna().any() or (u.exposed_at < u.install_at).any():
        raise ValueError("exposed_at must be valid, non-missing and no earlier than install_at")
    u["exposure_basis"] = "exposed_at" if explicit else "install_at_proxy"
    for col in ("purchased_at", "refunded_at", "billing_issue_detected_at"):
        t[col] = pd.to_datetime(t[col], utc=True)
    end = pd.to_datetime(observed_through, utc=True) if observed_through is not None else t.purchased_at.max()
    if pd.isna(end):
        raise ValueError("observed_through is required when history has no transactions")
    if explicit or observed_through is not None:
        t = t[t.purchased_at <= end].copy()
        for col in ("refunded_at", "billing_issue_detected_at"):
            t.loc[t[col] > end, col] = pd.NaT
    t = t.merge(u[["user_id", "install_at", "exposed_at"]], on="user_id", validate="many_to_one")
    if explicit:  # each mature user's metric is fixed at their horizon; later events cannot rewrite past looks
        cutoff = t.exposed_at + pd.Timedelta(days=horizon)
        for col in ("refunded_at", "billing_issue_detected_at"):
            t.loc[t[col] > cutoff, col] = pd.NaT
    t["day"] = (t.purchased_at - t.exposed_at).dt.total_seconds() / 86400
    t["paid"] = t.offer.ne("free_trial") & t.day.between(0, horizon)
    net = t[t.paid & t.refunded_at.isna()]
    u["revenue"] = u.user_id.map(net.groupby("user_id").price_usd.sum()).fillna(0.0)
    u["payer"] = (u.revenue > 0).astype(int)
    u["matured"] = u.exposed_at <= end - pd.Timedelta(days=horizon)
    return u, t, end


def add_intent(u: pd.DataFrame) -> pd.DataFrame:
    """Cross-fitted intent score (2 folds): no user is scored by a model that saw them. Trained only on matured users
    (their payer label is final), so the score doesn't learn 'recent install = not a payer yet'; everyone is scored."""
    u = u.copy()
    u["intent_score"] = np.nan
    matured = u.matured.to_numpy()
    for tr, te in KFold(2, shuffle=True, random_state=SEED).split(u):
        tr = tr[matured[tr]]
        m = build_model().fit(u.iloc[tr][CAT + NUM], u.iloc[tr].payer)
        u.iloc[te, u.columns.get_loc("intent_score")] = m.predict_proba(u.iloc[te][CAT + NUM])[:, 1]
    lo, hi = np.quantile(u.intent_score, [0.4, 0.8])
    u["intent"] = pd.cut(u.intent_score, [-1, lo, hi, 2], labels=["low", "mid", "high"]).astype(str)
    return u


# ---------------------------------------------------------------- statistics
MIN_TAIL_DRAWS = 50  # bootstrap draws beyond each CI bound, so a Bonferroni-corrected bound isn't set by a few draws


def boot_n(n: int, alpha: float) -> int:
    """Draws needed so each tail of a (1-alpha) interval holds at least MIN_TAIL_DRAWS of them."""
    return max(n, int(np.ceil(MIN_TAIL_DRAWS / (alpha / 2))))


def _boot_means(x: np.ndarray, n: int, rng: np.random.Generator, chunk: int = 500) -> np.ndarray:
    return np.concatenate([x[rng.integers(0, x.size, (min(chunk, n - i), x.size))].mean(axis=1)
                           for i in range(0, n, chunk)])


def boot(a: np.ndarray, b: np.ndarray, n: int = 3000, alpha: float = 0.05,
         seed: int = SEED) -> tuple[float, float, float]:
    """(1-alpha) CI of mean(a)-mean(b) and P(mean(a) > mean(b)), with enough draws for the tails alpha asks for."""
    rng = np.random.default_rng(seed)
    n = boot_n(n, alpha)
    d = _boot_means(np.asarray(a, float), n, rng) - _boot_means(np.asarray(b, float), n, rng)
    return (float(np.percentile(d, 100 * alpha / 2)), float(np.percentile(d, 100 * (1 - alpha / 2))),
            float((d > 0).mean()))



def _demo_normal_p(a: np.ndarray, b: np.ndarray, mde: float = MDE) -> float | None:
    """Legacy normal-mixture approximation for the synthetic demo, without a distribution-free validity claim.

    Retained to preserve the documented demo verdicts. Actual exposure data uses bounded_revenue_check instead.
    """
    a, b = np.asarray(a, float), np.asarray(b, float)
    v = a.var(ddof=1) / a.size + b.var(ddof=1) / b.size
    baseline = float(np.concatenate([a, b]).mean())
    tau2 = (mde * baseline) ** 2
    if not np.isfinite(v) or not np.isfinite(tau2) or v <= 0 or baseline <= 0 or tau2 <= 0:
        return None
    d = a.mean() - b.mean()
    log_lr = 0.5 * np.log(v / (v + tau2)) + tau2 * d * d / (2 * v * (v + tau2))
    return float(min(1.0, np.exp(-log_lr)))


def _bounded_arms(a, b, bounds) -> tuple:
    """Validate two user-level revenue vectors against fixed, predeclared bounds.

    Returns (a, b, lower, upper, None) or (None, None, None, None, unavailable-result).
    """
    unavailable = {"p": None, "ci95": None, "bounds_usd": None}
    if isinstance(bounds, (str, bytes)):
        return None, None, None, None, {**unavailable, "reason": "declare numeric revenue_bounds_usd [lower, upper] "
                                                                  "before collecting data"}
    try:
        lower, upper = map(float, bounds)
        a, b = np.asarray(a, float), np.asarray(b, float)
    except (TypeError, ValueError):
        return None, None, None, None, {**unavailable, "reason": "declare fixed revenue_bounds_usd [lower, upper] "
                                                                  "before collecting data"}
    width = upper - lower
    if not np.isfinite([lower, upper, width]).all() or width <= 0:
        return None, None, None, None, {**unavailable, "reason": "revenue_bounds_usd must be finite and lower < upper"}
    unavailable["bounds_usd"] = [lower, upper]
    if a.ndim != 1 or b.ndim != 1 or min(a.size, b.size) == 0:
        return None, None, None, None, {**unavailable, "reason": "each arm needs a non-empty vector of independent users"}
    if not all(np.isfinite(x).all() and (x >= lower).all() and (x <= upper).all() for x in (a, b)):
        return None, None, None, None, {**unavailable, "reason": "revenue is non-finite or outside the predeclared "
                                                                  "bounds; review the metric"}
    return a, b, lower, upper, None


def bounded_revenue_check(a: np.ndarray, b: np.ndarray, *, bounds=None, pair_count: int = 1,
                          alpha: float = 0.05) -> dict:
    """Time-uniform Hoeffding test for independent users with fixed, predeclared horizon-revenue bounds.

    Reference method: valid but very wide, because it ignores the observed variance. Decisions use
    betting_revenue_check. For fixed n,m: P(|gap| >= d) <= 2 exp(-2 d^2 / (R^2(1/n+1/m))). Spending weight
    1/[n(n+1)m(m+1)] sums to one over all sample-size pairs. A union bound therefore covers arbitrary
    repeated looks; pair_count additionally covers all possible arm pairs before a winner is selected.
    Bounds must be known before observation, never fitted to the sample maximum. No observations are clipped.
    Source for the fixed-sample inequality: https://doi.org/10.1080/01621459.1963.10500830
    """
    a, b, lower, upper, unavailable = _bounded_arms(a, b, bounds)
    if unavailable:
        return unavailable
    width = upper - lower
    if not isinstance(pair_count, (int, np.integer)) or pair_count < 1 or not 0 < alpha < 1:
        return {"p": None, "ci95": None, "bounds_usd": [lower, upper], "reason": "invalid pair count or significance level"}
    n, m = a.size, b.size
    gap = float(a.mean() - b.mean())
    log_budget = float(np.log(2.0 * pair_count) + np.log(n) + np.log(n + 1) + np.log(m) + np.log(m + 1))
    variance_scale = 1 / n + 1 / m
    log_p = log_budget - 2 * (gap / width) ** 2 / variance_scale
    p = float(np.exp(max(log_p, np.log(np.finfo(float).tiny)))) if log_p < 0 else 1.0
    radius = width * float(np.sqrt(variance_scale / 2 * (log_budget - np.log(alpha))))
    return {"p": p, "ci95": [max(-width, gap - radius), min(width, gap + radius)],
            "bounds_usd": [lower, upper], "reason": None}


# ------------------------------------------------- betting confidence sequences
# Waudby-Smith & Ramdas (2024), "Estimating means of bounded random variables by betting", JRSS-B,
# https://doi.org/10.1093/jrsssb/qkad009 — hedged capital process with predictable plug-in (PrPl-EB) bets.
BETTING_GRID = 1001          # candidate means on the [0, 1] scale; intervals are widened by one grid step per side
BETTING_TRUNCATION = 0.5     # c in the paper: bets never exceed c/m or c/(1-m), so capital stays positive
_BETTING_CHUNK_CELLS = 2_000_000


def _betting_bets(x: np.ndarray, tuning_level: float) -> np.ndarray:
    """Predictable bet sizes: bet t uses only observations 1..t-1."""
    t = np.arange(1, x.size + 1, dtype=float)
    mu_hat = (0.5 + np.cumsum(x)) / (t + 1)
    sigma2 = (0.25 + np.cumsum((x - mu_hat) ** 2)) / (t + 1)
    sigma2_prev = np.concatenate([[0.25], sigma2[:-1]])
    return np.sqrt(2 * np.log(2 / tuning_level) / (sigma2_prev * t * np.log1p(t)))


def _betting_log_capital(x: np.ndarray, bets: np.ndarray, m: np.ndarray, cumulative: bool) -> np.ndarray:
    """log max(K+/2, K-/2) for candidate means m: every prefix (cumulative) or only the last one."""
    with np.errstate(divide="ignore"):
        up = np.minimum(bets[:, None], BETTING_TRUNCATION / m[None, :])
        down = np.minimum(bets[:, None], BETTING_TRUNCATION / (1 - m[None, :]))
    diff = x[:, None] - m[None, :]
    plus, minus = np.log1p(up * diff), np.log1p(-down * diff)
    if cumulative:
        plus, minus = np.cumsum(plus, axis=0), np.cumsum(minus, axis=0)
    else:
        plus, minus = plus.sum(axis=0), minus.sum(axis=0)
    return np.log(0.5) + np.maximum(plus, minus)


def _grid_chunks(n: int):
    grid = np.linspace(0.0, 1.0, BETTING_GRID)
    step = max(1, _BETTING_CHUNK_CELLS // max(n, 1))
    for start in range(0, grid.size, step):
        yield start, grid[start:start + step]


def _final_log_capital(x: np.ndarray, tuning_level: float) -> np.ndarray:
    bets = _betting_bets(x, tuning_level)
    return np.concatenate([_betting_log_capital(x, bets, m, cumulative=False) for _, m in _grid_chunks(x.size)])


def _hull(log_capital: np.ndarray, threshold: float) -> tuple[float, float]:
    """Conservative interval around {m : log capital < threshold}.

    Capital is quasiconvex in m (K+ falls and K- rises as m grows), so the continuous confidence set is an interval
    and lies within one grid step of the accepted grid points, or of the grid minimum when no grid point is accepted.
    """
    accepted = np.flatnonzero(log_capital < threshold)
    first, last = (accepted[0], accepted[-1]) if accepted.size else (int(np.argmin(log_capital)),) * 2
    top = BETTING_GRID - 1
    return max(first - 1, 0) / top, min(last + 1, top) / top


def betting_cs_path(x: np.ndarray, *, level: float = 0.05, tuning_level: float | None = None
                    ) -> tuple[np.ndarray, np.ndarray]:
    """Running confidence sequence for the mean of [0, 1] observations in arrival order.

    P(some prefix interval misses the mean) <= level, by Ville's inequality, for independent observations with a
    common mean. Returns lower and upper bounds for every prefix length.
    """
    x = np.asarray(x, float)
    tuning_level = level if tuning_level is None else tuning_level
    bets, threshold, n = _betting_bets(x, tuning_level), np.log(1 / level), x.size
    first = np.full(n, BETTING_GRID)
    last = np.full(n, -1)
    best_value, best_index = np.full(n, np.inf), np.zeros(n, dtype=int)
    for start, m in _grid_chunks(n):
        w = _betting_log_capital(x, bets, m, cumulative=True)
        ok = w < threshold
        has = ok.any(axis=1)
        first = np.where(has, np.minimum(first, start + ok.argmax(axis=1)), first)
        last = np.where(has, np.maximum(last, start + m.size - 1 - ok[:, ::-1].argmax(axis=1)), last)
        local = w.argmin(axis=1)
        value = w[np.arange(n), local]
        better = value < best_value
        best_value, best_index = np.where(better, value, best_value), np.where(better, start + local, best_index)
    empty = last < 0
    first, last = np.where(empty, best_index, first), np.where(empty, best_index, last)
    top = BETTING_GRID - 1
    return np.maximum(first - 1, 0) / top, np.minimum(last + 1, top) / top


def betting_revenue_check(a: np.ndarray, b: np.ndarray, *, bounds=None, arm_count: int = 2,
                          alpha: float = 0.05) -> dict:
    """Anytime-valid comparison of two arms from per-arm betting confidence sequences.

    Each of the experiment's arm_count arms gets a confidence sequence at level alpha/arm_count, so every pairwise
    difference, every look and the data-driven choice of winner are covered at once (union bound over arms).
    Arrays must be in exposure order: each look must see a prefix of the same arrival sequence. Unlike Hoeffding,
    the bets adapt to the observed variance, so zero-inflated revenue with a wide declared bound stays decidable.
    p is arm_count times the smallest per-arm level at which the two intervals separate; bets are tuned at a fixed
    level, so the intervals are nested in the level and p < alpha exactly when ci95 excludes zero.
    """
    a, b, lower, upper, unavailable = _bounded_arms(a, b, bounds)
    if unavailable:
        return unavailable
    width = upper - lower
    if not isinstance(arm_count, (int, np.integer)) or arm_count < 2 or not 0 < alpha < 1:
        return {"p": None, "ci95": None, "bounds_usd": [lower, upper], "reason": "invalid arm count or significance level"}
    tuning = alpha / arm_count
    wa, wb = (_final_log_capital((x - lower) / width, tuning) for x in (a, b))

    def separated(threshold: float) -> bool:
        (la, ha), (lb, hb) = _hull(wa, threshold), _hull(wb, threshold)
        return la > hb or lb > ha

    ci_threshold = float(np.log(arm_count / alpha))
    (la, ha), (lb, hb) = _hull(wa, ci_threshold), _hull(wb, ci_threshold)
    ci = [width * (la - hb), width * (ha - lb)]
    if not separated(0.0):
        p = 1.0
    else:
        low, high = (ci_threshold, 745.0) if separated(ci_threshold) else (0.0, ci_threshold)
        if separated(high):
            low = high
        else:
            for _ in range(60):
                mid = (low + high) / 2
                low, high = (mid, high) if separated(mid) else (low, mid)
        p = float(min(1.0, arm_count * np.exp(-low)))
    return {"p": p, "ci95": ci, "bounds_usd": [lower, upper], "reason": None}


def always_valid_p(a: np.ndarray, b: np.ndarray, *, bounds=None, arm_count: int = 2) -> float | None:
    """Anytime-valid p-value used for decisions; None when the required support assumptions cannot be checked."""
    return betting_revenue_check(a, b, bounds=bounds, arm_count=arm_count)["p"]


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


def maturity(t: pd.DataFrame, users: pd.Series, horizon: int = HORIZON_DAYS) -> dict:
    """Share of each user group's horizon revenue already collected by day d (d = 1, 7, 30, … up to the horizon)."""
    tx = t[t.user_id.isin(users) & t.paid & t.refunded_at.isna()]
    total = tx.price_usd.sum()
    days = sorted({d for d in MATURITY_DAYS if d < horizon} | {horizon})
    return {d: round(float(tx[tx.day <= d].price_usd.sum() / total), 3) if total else 0.0 for d in days}


def current_mrr() -> float | None:
    """Today's MRR from Adapty analytics, to put the headline in proportion (None if unavailable)."""
    try:
        today = date.today()
        res = adapty_cli.run("analytics", "chart", "--chart", "mrr", "--date-from",
                             (today - pd.Timedelta(days=7)).isoformat(), "--date-to", today.isoformat())
        return round(float(res["data"]["value"]), 2) or None
    except (RuntimeError, KeyError, ValueError, TypeError):
        return None


def decided_total(tests: list[dict]) -> float:
    """Money lost per month on live tests whose verdict is stop_and_roll_out: the one definition of the headline."""
    return round(sum(x.get("lost_per_month_usd", 0) for x in tests
                     if x["state"] == "live" and x["verdict"] == "stop_and_roll_out"), 2)


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


def _find_all(node: dict, level: str, title: str) -> list[dict]:
    hits = [node] if node.get("level") == level and node.get("title") == title else []
    for child in node.get("items", []):
        hits += _find_all(child, level, title)
    return hits


def flow_live_metrics(flow_id: str, test_title: str, start: str, test_id: str | None = None) -> dict | None:
    """One arm as the Adapty flow metrics page shows it, counting ONLY users inside this A/B test: the same flow can
    also be shown outside the test (e.g. to other audiences at the same placement). Titles aren't unique (a copied
    test keeps its title), so a node carrying this test's id wins; otherwise the last matching node."""
    res = adapty_cli.run("flows", "metrics", "get", flow_id, "--date-type", "profile_install_date",
                         "--based-on", "audience", "--date-from", start, "--date-to", date.today().isoformat())
    hits = _find_all(res["data"], "ab_test", test_title)
    item = next((h for h in hits if test_id and h.get("ab_test_id") == test_id), hits[-1] if hits else None)
    if item is None:
        return None
    users = int(item.get("unique_profiles_views") or 0)
    return {"users": users, "revenue": round(float(item.get("revenue") or 0), 2),
            "rev_per_user": round(float(item.get("revenue") or 0) / users, 2) if users else 0.0,
            "trials": int(item.get("trials") or 0), "trials_finished": int(item.get("trials_finished") or 0),
            "purchases": int(item.get("purchases") or 0)}


def live_view(test: dict, arms: list[dict]) -> dict | None:
    """What the Adapty dashboard sees since the test started: users, revenue and trials per arm."""
    start = test["started_at"][:10] if test.get("started_at") else None
    if not start:
        return None
    pid = test["placement"]["placement_id"]
    try:
        if test["type"] == "flow":
            per_arm = {a["name"]: flow_live_metrics(a["content_id"], test["title"], start, test.get("ab_test_id"))
                       for a in arms}
            per_arm = {k: v for k, v in per_arm.items() if v}
        else:  # paywall tests: revenue per paywall from the analytics API
            res = adapty_cli.run("analytics", "chart", "--chart", "revenue", "--date-from", start,
                                 "--date-to", date.today().isoformat(), "--group-by", "paywall_id",
                                 "--filter", f"placement_id={pid}")
            by_id = {s["key"]: s["value"] for s in res["data"]["segments"] if not s["is_total"]}
            per_arm = {a["name"]: {"revenue": round(float(by_id.get(a["content_id"], 0.0)), 2)} for a in arms}
    except (RuntimeError, KeyError, ValueError, TypeError):
        return None
    if not per_arm:  # no arm found in Adapty's metrics: say "no live data", not an empty line
        return None
    trials = sum(v.get("trials", 0) for v in per_arm.values())
    finished = sum(v.get("trials_finished", 0) for v in per_arm.values())
    return {"since": start, "arms": per_arm,
            "revenue": {k: v.get("revenue", 0.0) for k, v in per_arm.items()},
            "trials": trials, "trials_finished": finished,
            "immature": trials > 0 and finished / trials < 0.5}


def horizon_of(test: dict) -> int:
    """Use the horizon the test owner wrote in the goal ("…60-day revenue…"), else the default."""
    # only "N-day revenue" counts: "7-day trial" is a product detail, not the horizon
    m = re.search(r"(\d{1,3})[- ]day\s+(?:net\s+|gross\s+)?revenue", test.get("goal") or "", re.I)
    return int(m.group(1)) if m and 7 <= int(m.group(1)) <= 365 else HORIZON_DAYS


def unsupported(base: dict, test: dict, why: str) -> dict:
    """A test we can't judge on history still shows up, with what Adapty sees, instead of vanishing."""
    return {**base, "verdict": "unsupported", "reasons": [why], "live_view": live_view(test, base["arms_config"])}


def history_owners(tests: list[dict]) -> dict[str, str]:
    """Title -> id of the test that owns the offline history for that title: the earliest started one. A copy keeps
    the title but starts later with no users, so it must not inherit the original's matured revenue."""
    owners: dict[str, tuple[str, str]] = {}
    for x in tests:
        if x.get("state") == "draft":
            continue
        key = (x.get("started_at") or "9999", x["ab_test_id"])
        if x["title"] not in owners or key < owners[x["title"]]:
            owners[x["title"]] = key
    return {title: key[1] for title, key in owners.items()}


def judge(test: dict, u: pd.DataFrame, t: pd.DataFrame, names: dict, placements: dict | None = None,
          owners: dict[str, str] | None = None) -> dict | None:
    arms = arms_of(test)
    code = {v: k for k, v in names.items()}
    base = {"ab_test_id": test["ab_test_id"], "title": test["title"], "state": test["state"], "type": test["type"],
            "placement": test["placement"]["title"], "placement_id": test["placement"]["placement_id"],
            "placement_code": (placements or {}).get(test["placement"]["placement_id"]),
            "segment_ids": test.get("segment_ids", []), "goal": test.get("goal"),
            "horizon_days": horizon_of(test), "arms_config": arms}
    if test["state"] == "draft":
        return plan_draft(base, arms, u, code)
    if test["type"] != "flow":
        return unsupported(base, test, "paywall test: the offline history has flows only, so it can't be judged on "
                                       "matured revenue yet (an Adapty S3/BigQuery export would add it)")
    if len(arms) < 2:
        return unsupported(base, test, "fewer than two arms")
    explicit_exposure = "exposed_at" in u and not (
        "exposure_basis" in u and u.exposure_basis.eq("install_at_proxy").all())
    if "ab_test_id" in u:
        e_all = u[u.ab_test_id.eq(test["ab_test_id"])]
    elif explicit_exposure:
        return unsupported(base, test, "exposure history requires stable ab_test_id values, not a title match")
    else:
        owner = (owners or {}).get(test["title"], test["ab_test_id"])
        if owner != test["ab_test_id"]:
            return unsupported(base, test, f"a copy of an earlier test with the same title (id {owner}): the offline "
                                           "history belongs to that test, and this copy has no matured users of its own")
        e_all = u[u.ab_test == test["title"]]
    if explicit_exposure and test.get("started_at"):
        start = pd.to_datetime(test["started_at"], utc=True)
        e_all = e_all[pd.to_datetime(e_all.exposed_at, utc=True) >= start]
    if e_all.empty:
        return unsupported(base, test, "no user→arm history for this test (the offline dataset covers the seeded "
                                       "Lumen app; in production this comes from an Adapty S3/BigQuery export)")
    horizon = base["horizon_days"]

    e = e_all[e_all.matured]
    if explicit_exposure:  # the betting sequence needs one arrival order; every look then sees a prefix of it
        e = e.sort_values(["exposed_at", "user_id"], kind="stable")
    stats_by_arm = []
    for a in arms:
        s = e[e.flow == code.get(a["name"])]
        rpu = float(s.revenue.mean())
        stats_by_arm.append({**a, "users": int(len(s)), "rev_per_user": rpu if explicit_exposure else round(rpu, 2),
                             "payer_rate": round(float(s.payer.mean()), 4), "_ids": s.user_id})
    if min(a["users"] for a in stats_by_arm) < 100:
        return unsupported(base, test, "fewer than 100 matured users in an arm: too early to judge")
    # Keep the runner-up in the compact view, but verify every losing arm independently.
    # Rank on raw means, even when two arms display the same RPU rounded to cents.
    ranked = sorted(stats_by_arm, key=lambda a: -float(e[e.user_id.isin(a["_ids"])].revenue.mean()))
    win, lose, others = ranked[0], ranked[1], ranked[2:]
    w_rev, l_rev = e[e.user_id.isin(win["_ids"])].revenue.values, e[e.user_id.isin(lose["_ids"])].revenue.values
    gap = win["rev_per_user"] - lose["rev_per_user"]

    # health: intent balance is a randomization diagnostic only if its features predate exposure
    # SRM uses the weights as configured now (Adapty keeps no weight history); an arm at weight 0 is switched off,
    # so it has no expected share and is left out instead of making the expectation 0
    w = np.array([a["weight"] for a in ranked], dtype=float)
    users = np.array([a["users"] for a in ranked])
    on = w > 0
    srm_p = (float(stats.chisquare(users[on], w[on] / w[on].sum() * users[on].sum()).pvalue)
             if on.sum() >= 2 else 1.0)
    bal_p = float(stats.f_oneway(*[e[e.user_id.isin(a["_ids"])].intent_score for a in ranked]).pvalue)

    pair_count = len(arms) * (len(arms) - 1) // 2
    inference_method = "betting_cs" if explicit_exposure else "normal_approximation_demo"
    comparisons = []
    for other in ranked[1:]:
        other_rev = e[e.user_id.isin(other["_ids"])].revenue.values
        clo, chi, cp = boot(w_rev, other_rev)
        if explicit_exposure:
            sequential = betting_revenue_check(w_rev, other_rev, bounds=test.get("revenue_bounds_usd"),
                                                arm_count=len(ranked))
        else:
            approx = _demo_normal_p(w_rev, other_rev)
            sequential = {"p": min(1.0, approx * pair_count) if approx is not None else None,
                          "ci95": None, "reason": None, "bounds_usd": None}
        cseq = sequential["p"]
        if other is lose:
            lo, hi, p, seq_p = clo, chi, cp, cseq
        # guardrails: the winner must not raise refunds or billing issues
        gw, gl = guardrails(t, win["_ids"]), guardrails(t, other["_ids"])
        refund_p = rate_p_by_user(t, win["_ids"], other["_ids"], "refunded_at")
        billing_p = rate_p_by_user(t, win["_ids"], other["_ids"], "billing_issue_detected_at")
        guard_fail = [(n, pv) for n, pv, worse in [
            ("refund rate", refund_p, gw["refund_rate"] > gl["refund_rate"]),
            ("billing-issue rate", billing_p, gw["billing_issue_rate"] > gl["billing_issue_rate"])] if worse and pv < 0.05]

        # segment consistency, Bonferroni-corrected
        segs = []
        groups = [(col, val, g) for col in ["intent", "platform", "acquisition_channel"] for val, g in e.groupby(col)]
        k = max(1, sum(1 for *_, g in groups if min(g.user_id.isin(win["_ids"]).sum(), g.user_id.isin(other["_ids"]).sum()) >= 80))
        for col, val, g in groups:
            a = g[g.user_id.isin(win["_ids"])].revenue.values
            b = g[g.user_id.isin(other["_ids"])].revenue.values
            if min(a.size, b.size) < 80:
                continue
            slo, shi, sp = boot(a, b, 1000, alpha=0.05 / k)
            segs.append({"dimension": col, "value": str(val), "users": int(a.size + b.size),
                         "winner_rpu": round(float(a.mean()), 2), "other_rpu": round(float(b.mean()), 2),
                         "p_winner_better": round(sp, 3), "ci": [round(slo, 2), round(shi, 2)]})
        flipped = [s for s in segs if s["ci"][1] < 0]
        unclear = [s for s in segs if s["ci"][0] <= 0 <= s["ci"][1]]

        significant = clo > 0 and cp > 0.99 and cseq is not None and cseq < 0.05
        comparisons.append({"other": {k: v for k, v in other.items() if k != "_ids"},
                            "diff_ci95": [round(clo, 2), round(chi, 2)],
                            "p_winner_better": round(cp, 4),
                            "sequential_p": (cseq if explicit_exposure else float(f"{cseq:.3g}"))
                            if cseq is not None else None,
                            "anytime_ci95": sequential["ci95"], "sequential_reason": sequential["reason"],
                            "revenue_bounds_usd": sequential["bounds_usd"],
                            "significant": significant,
                            "guardrails": {"winner": gw, "other": gl, "refund_p": round(refund_p, 4),
                                           "billing_p": round(billing_p, 4), "failed": [n for n, _ in guard_fail]},
                            "segments": segs, "segments_flipped": len(flipped), "segments_unclear": len(unclear),
                            "passed": bool(significant and srm_p > 0.01 and bal_p > 0.001
                                           and not guard_fail and not flipped)})
    first = comparisons[0]
    guard_fail = [(f"{c['other']['name']}: {n}", c["guardrails"]["refund_p" if n == "refund rate" else "billing_p"])
                  for c in comparisons for n in c["guardrails"]["failed"]]
    segs = [{**s, "other_arm": c["other"]["name"]} for c in comparisons for s in c["segments"]]
    flipped = [s for s in segs if s["ci"][1] < 0]
    unclear = [s for s in segs if s["ci"][0] <= 0 <= s["ci"][1]]

    # metric maturity: early comparisons favour the arm that collects its revenue sooner
    mw, ml = maturity(t, win["_ids"], horizon), maturity(t, lose["_ids"], horizon)
    early_bias = None
    if abs(mw[1] - ml[1]) >= 0.25:
        fast, slow = (win, lose) if mw[1] > ml[1] else (lose, win)
        fm, sm = (mw, ml) if fast is win else (ml, mw)
        early_bias = (f"By day 1, {fast['name']} has collected {fm[1]:.0%} of its {horizon}-day revenue and "
                      f"{slow['name']} {sm[1]:.0%}. A dashboard read in the first days favours {fast['name']}, "
                      f"whatever the final winner.")

    # Money counts only arms the winner verifiably beats, each followed for the horizon.
    clock = e_all.exposed_at if "exposed_at" in e_all else e_all.install_at
    days = (clock.max() - clock.min()).days or 1
    size_scale = 1.0 if explicit_exposure else LIVE_USERS / OFFLINE_USERS
    daily_users = len(e_all) / days * size_scale
    total_w = float(w.sum())
    lost_month = daily_users * 30 * sum(a["weight"] / total_w * (win["rev_per_user"] - a["rev_per_user"])
                                        for c in comparisons if c["passed"] for a in [c["other"]])
    losing_share = sum(a["weight"] for a in ranked[1:]) / total_w
    loss_calculation = {
        "offline_users_in_test": int(len(e_all)), "history_span_days": int(days),
        "live_population": None if explicit_exposure else LIVE_USERS,
        "offline_population": None if explicit_exposure else OFFLINE_USERS,
        "population_basis": "observed_exposures" if explicit_exposure else "synthetic_scaled",
        "live_size_scale": size_scale, "daily_users": daily_users, "month_days": 30,
        "arms": [{"name": c["other"]["name"], "losing_share": c["other"]["weight"] / total_w,
                  "gap_per_user": win["rev_per_user"] - c["other"]["rev_per_user"],
                  "monthly_loss_usd": round(daily_users * c["other"]["weight"] / total_w
                                            * (win["rev_per_user"] - c["other"]["rev_per_user"]) * 30, 2)}
                 for c in comparisons if c["passed"]],
    }
    # Scale the existing gap bootstrap, holding traffic and configured allocation fixed.
    # Unverified arms contribute no claimed loss; multi-arm intervals need a joint bootstrap.
    loss_ci = None
    if len(comparisons) == 1 and first["passed"]:
        factor = daily_users * lose["weight"] / total_w * 30
        interval = first["anytime_ci95"] if explicit_exposure else [lo, hi]
        loss_ci = [round(factor * interval[0], 2), round(factor * interval[1], 2)]

    decided = all(c["significant"] for c in comparisons)
    sequential_unavailable = any(c["sequential_p"] is None for c in comparisons)
    healthy = srm_p > 0.01 and bal_p > 0.001
    n_needed = sample_size_per_arm(np.concatenate([w_rev, l_rev]))
    applied_winner = test.get("winner_flow_id")
    if test["state"] == "completed":  # same checks as a live rollout: health, guardrails, segments
        verdict = ("completed_check_failed" if sequential_unavailable else
                   "completed_inconclusive" if not decided else
                   "completed_check_failed" if not healthy or guard_fail or flipped else
                   "completed_winner_unknown" if not applied_winner else
                   "completed_disagrees" if applied_winner != win["content_id"] else "completed_confirmed")
    elif not healthy or sequential_unavailable:
        verdict = "check_data"
    elif decided and guard_fail:
        verdict = "guardrail_failed"
    elif decided and flipped:
        verdict = "split_by_segment"
    elif decided:
        verdict = "stop_and_roll_out"
    else:
        verdict = "keep_running"

    seq_label = f"{seq_p:.2g}" if seq_p is not None else "not applicable"
    seq_name = "anytime betting p" if explicit_exposure else "demo normal-approximation p"
    reasons = [f"{win['name']} earns ${win['rev_per_user']:.2f} vs ${lose['rev_per_user']:.2f} per user in "
               f"{horizon}-day revenue (95% bootstrap CI of the gap ${lo:.2f}…${hi:.2f}, "
               f"P(better) {p:.1%}, {seq_name}={seq_label})",
               f"traffic split {'OK' if srm_p > 0.01 else 'BROKEN'} (SRM p={srm_p:.2f}), groups "
               f"{'balanced' if bal_p > 0.001 else 'UNBALANCED'} on intent (p={bal_p:.2f})",
               "guardrails OK (refunds, billing issues)" if not guard_fail else
               "guardrail failed: " + ", ".join(f"{n} worse (p={pv:.3f})" for n, pv in guard_fail),
               f"segments (Bonferroni-corrected): winner clearly ahead in {len(segs) - len(flipped) - len(unclear)} "
               f"of {len(segs)}, unclear in {len(unclear)}, reversed in {len(flipped)}"]
    for c in comparisons[1:]:
        seq_label = f"{c['sequential_p']:.2g}" if c["sequential_p"] is not None else "not applicable"
        reasons.append(f"vs {c['other']['name']}: 95% CI ${c['diff_ci95'][0]:.2f}…${c['diff_ci95'][1]:.2f}, "
                       f"P(better) {c['p_winner_better']:.1%}, {seq_name}={seq_label}; "
                       + ("all checks passed" if c["passed"] else "not verified: rollout blocked"))
    if sequential_unavailable:
        if explicit_exposure:
            notes = sorted({c["sequential_reason"] for c in comparisons if c["sequential_reason"]})
            reasons.append("sequential revenue check is not applicable: " + "; ".join(notes))
        else:
            reasons.append("sequential revenue check is not applicable with zero variance or a non-positive revenue "
                           "scale; review the statistical model before rollout, rather than just collecting more users")
    if not explicit_exposure:
        reasons.append("synthetic/legacy demo uses installation as an exposure proxy and a normal approximation; "
                       "not validated for real experiment decisions")
    if verdict == "completed_winner_unknown":
        reasons.append("applied winner is unknown: history cannot confirm what Adapty rolled out")
    elif verdict == "completed_disagrees":
        reasons.append(f"history disagrees with the applied winner {applied_winner}: history favours {win['name']}")
    if verdict == "keep_running":
        reasons.append("bounded anytime evidence remains inconclusive; the normal-approximation sample-size "
                       "estimate is not a decision deadline" if explicit_exposure else
                       f"needs ≈{n_needed:,} matured users per arm to detect a {MDE:.0%} lift")

    for a in ranked:
        a.pop("_ids")
    return {**base, "matured_users": int(len(e)), "comparisons": comparisons, "applied_winner_id": applied_winner,
            "arms": ranked, "winner": win, "loser": lose, "others": others,
            "losing_share": round(losing_share, 4),
            "lift_pct": round(100 * gap / lose["rev_per_user"], 1) if lose["rev_per_user"] > 0 else None,
            "diff_ci95": [round(lo, 2), round(hi, 2)], "p_winner_better": round(p, 4),
            "sequential_p": (seq_p if explicit_exposure else float(f"{seq_p:.3g}"))
            if seq_p is not None else None,
            "anytime_ci95": first["anytime_ci95"],
            "inference": {"method": inference_method, "pair_count": pair_count, "arm_count": len(ranked),
                          "revenue_bounds_usd": first["revenue_bounds_usd"]},
            "health": {"srm_pvalue": round(srm_p, 4), "balance_pvalue": round(bal_p, 4), "healthy": healthy},
            "guardrails": first["guardrails"],
            "maturity": {"winner": mw, "other": ml, "early_bias": early_bias},
            "live_view": live_view(test, arms),
            "daily_users_in_test": round(daily_users, 1), "lost_per_month_usd": round(lost_month, 2),
            "loss_calculation": loss_calculation, "lost_per_month_ci95_usd": loss_ci,
            "loss_interval_method": "anytime_betting" if explicit_exposure else "bootstrap",
            "segments": segs, "segments_flipped": len(flipped), "segments_unclear": len(unclear),
            "sample_size_per_arm": n_needed, "verdict": verdict, "reasons": reasons}


def plan_draft(base: dict, arms: list[dict], u: pd.DataFrame, code: dict) -> dict:
    """A draft test has no data yet: size it from the users who reached its placement."""
    # users who reached THIS placement (a flow can be shown at several placements)
    pl = u[u.placement == base["placement_code"]] if base.get("placement_code") else u.iloc[0:0]
    hist = pl[pl.matured]
    clock = pl.exposed_at if "exposed_at" in pl else pl.install_at if not pl.empty else None
    days = (clock.max() - clock.min()).days or 1 if not pl.empty else 1
    size_scale = 1.0 if "exposure_basis" in pl and pl.exposure_basis.eq("exposed_at").all() else LIVE_USERS / OFFLINE_USERS
    daily = len(pl) / days * size_scale if not pl.empty else 0
    n = sample_size_per_arm(hist.revenue.values) if len(hist) > 30 else None
    weights = [a["weight"] for a in arms]
    min_share = min(weights) / sum(weights) if weights and min(weights) > 0 else 0
    days_needed = int(np.ceil(n / (daily * min_share))) + base["horizon_days"] if n and daily and min_share else None
    # smallest lift detectable with 30 days of enrolment at this traffic (n scales with 1/MDE^2)
    n30 = daily * 30 * min_share
    mde30 = MDE * np.sqrt(n / n30) if n and n30 else None
    return {**base, "verdict": "draft_plan", "baseline_rev_per_user": round(float(hist.revenue.mean()), 2)
            if len(hist) else None, "sample_size_per_arm": n, "daily_users_at_placement": round(daily, 1),
            "days_to_decision": days_needed, "mde_with_30_days_pct": round(100 * mde30, 0) if mde30 else None,
            "recommendation": ("can't size: not enough history/traffic at this placement" if days_needed is None else
                               "don't start: at this traffic the test can't reach a decision in a useful time"
                               if days_needed and days_needed > 120 else "start it"),
            "reasons": [f"baseline ${hist.revenue.mean():.2f} per user at this placement ({len(hist):,} matured users)"
                        if len(hist) else "no history at this placement",
                        f"≈{n:,} users per arm to detect a {MDE:.0%} lift; at ≈{daily:.0f} users/day that is "
                        f"≈{days_needed} days including the {base['horizon_days']}-day revenue window"
                        if days_needed else "traffic too low to size",
                        f"with 30 days of traffic it can only detect a lift of ≈{100 * mde30:.0f}% or more"
                        if mde30 else ""]}


# ---------------------------------------------------------------- report
def write_atomic(path: Path, text: str) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def report_path(app_id: str | None = None) -> Path:
    """out/report.json for the default app, out/report-<app>.json for any other app of the portfolio."""
    app_id = app_id or adapty_cli.DEFAULT_APP_ID
    return out_dir() / ("report.json" if app_id == adapty_cli.DEFAULT_APP_ID else f"report-{app_id}.json")


def build_report(app_id: str | None = None, *, revenue_bounds_usd: dict | None = None,
                 observed_through: str | pd.Timestamp | None = None) -> dict:
    """Analyze the configured history. Bounds are a predeclared protocol keyed by test ID, not estimated from data."""
    app_id = app_id or adapty_cli.DEFAULT_APP_ID
    with adapty_cli.use_app(app_id):
        has_history = app_id in history_apps()  # the offline user→arm history belongs to the seeded app(s)
        datasets: dict[int, tuple[pd.DataFrame, pd.DataFrame]] = {}
        if has_history:
            base_u, base_t, end = (load(HORIZON_DAYS, observed_through=observed_through)
                                   if observed_through is not None else load(HORIZON_DAYS))
            base_u = add_intent(base_u)
        else:  # live-only apps must also work when the offline CSVs are not installed
            base_u = pd.DataFrame(columns=["user_id", "ab_test", "placement", "install_at", "intent"]).assign(
                matured=pd.Series(dtype=bool), revenue=pd.Series(dtype=float),
                ai_edits_first_day=pd.Series(dtype=float), intent_score=pd.Series(dtype=float))
            base_t, end = pd.DataFrame(), None
        datasets[HORIZON_DAYS] = (base_u, base_t)

        def data_for(h: int):
            if not has_history:
                return datasets[HORIZON_DAYS]
            if h not in datasets:  # another horizon: recompute revenue, keep the same intent scores
                u_h, t_h, _ = load(h, observed_through=observed_through) if observed_through is not None else load(h)
                u_h = u_h.merge(base_u[["user_id", "intent_score", "intent"]], on="user_id")
                datasets[h] = (u_h, t_h)
            return datasets[h]

        names, placements = flow_names(), placement_codes()
        all_tests = adapty_cli.list_tests()
        owners = history_owners(all_tests)
        tests = [x for x in all_tests if x["state"] in ("live", "completed", "draft")]
        bounds_by_test = dict(revenue_bounds_usd or {})
        if revenue_bounds_usd is None:
            try:
                previous = json.loads(report_path(app_id).read_text())
            except (OSError, ValueError):
                previous = {}
            if isinstance(previous, dict) and previous.get("app_id") == app_id and previous.get("schema") == REPORT_SCHEMA:
                fresh_horizons = {x["ab_test_id"]: horizon_of(x) for x in tests}
                for old in previous.get("tests", []):
                    if not isinstance(old, dict):
                        continue
                    if old.get("ab_test_id") not in fresh_horizons:
                        continue
                    inf = old.get("inference", {})
                    if isinstance(inf, dict) and inf.get("method") in ("betting_cs", "hoeffding_union") and old.get(
                            "horizon_days") == fresh_horizons.get(old.get("ab_test_id")):
                        bounds_by_test[old["ab_test_id"]] = inf.get("revenue_bounds_usd")
        for tst in tests:  # build the per-horizon datasets up front, before running judges in threads
            data_for(horizon_of(tst))
        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
        with ThreadPoolExecutor(max_workers=4) as pool:  # judges wait on Adapty CLI calls (live view)
            results = list(pool.map(adapty_cli.in_current_app(
                lambda x: judge({**x, "revenue_bounds_usd": bounds_by_test.get(
                    x["ab_test_id"], x.get("revenue_bounds_usd"))},
                    *data_for(horizon_of(x)), names, placements, owners)), tests))
        judged = [res for res in results if res is not None]
        m = base_u[base_u.matured]
        bins = pd.cut(m.ai_edits_first_day, [-1, 0, 2, 5, 1000], labels=["0", "1–2", "3–5", "6+"])
        report = {
            "schema": REPORT_SCHEMA, "app_id": app_id, "has_history": has_history,
            "data_end": str(end) if end is not None else None,
            "sources": {"adapty": "Adapty API (live)", "history":
                        ("experiment exposure history" if "exposure_basis" in base_u
                         and base_u.exposure_basis.eq("exposed_at").all() else "synthetic history")
                        if has_history else None},
            "horizon_days": HORIZON_DAYS,
            "segment_names": {s["id"]: s["title"] for s in adapty_cli.run("segments", "list")["data"]},
            "tests": judged,
            "total_lost_per_month_usd": decided_total(judged),
            "mrr_usd": current_mrr(),
            "intent": None if m.empty else {
                "payer_rate_by_ai_edits_day1": {str(k): round(float(v), 3) for k, v in
                                                m.groupby(bins, observed=False).payer.mean().fillna(0).items()},
                "rev_per_user_by_intent": {k: round(float(m[m.intent == k].revenue.mean() or 0), 2)
                                           for k in ["low", "mid", "high"]},
                "payer_rate_by_intent": {k: round(float(m[m.intent == k].payer.mean() or 0), 3)
                                         for k in ["low", "mid", "high"]}},
        }
    report["analyzed_at"] = datetime.now(timezone.utc).isoformat()
    out_dir().mkdir(parents=True, exist_ok=True)
    write_atomic(report_path(app_id), json.dumps(report, indent=2))
    return report


def mark_stopped(app_id: str, test_id: str) -> dict:
    """After a confirmed stop, update the saved report without re-running the whole analysis."""
    path = report_path(app_id)
    r = json.loads(path.read_text())
    for t in r["tests"]:
        if t["ab_test_id"] == test_id:
            t["state"], t["verdict"] = "completed", "completed_confirmed"
            t["applied_winner_id"] = t["winner"]["content_id"]  # just acknowledged by the guarded stop
    r["total_lost_per_month_usd"] = decided_total(r["tests"])
    write_atomic(path, json.dumps(r, indent=2))
    return r


def portfolio_path() -> Path:
    return out_dir() / "portfolio.json"


def portfolio() -> list[dict]:
    """One row per app the Adapty account can open: tests, decided live tests, money at stake."""
    rows = []
    for app in adapty_cli.list_apps():
        r = build_report(app["id"])
        live = [t for t in r["tests"] if t["state"] == "live"]
        rows.append({"app_id": app["id"], "app": app["title"], "tests": len(r["tests"]), "live": len(live),
                     "decided_live": sum(t["verdict"] == "stop_and_roll_out" for t in live),
                     "lost_per_month_usd": r["total_lost_per_month_usd"], "has_history": r["has_history"],
                     "analyzed_at": r["analyzed_at"], "history_through": r["data_end"] if r["has_history"] else None})
    out_dir().mkdir(parents=True, exist_ok=True)
    write_atomic(portfolio_path(), json.dumps(rows, indent=2))
    return rows


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
