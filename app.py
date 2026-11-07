"""Experiment Autopilot for Adapty — Streamlit UI.

Run:  uv run streamlit run app.py
"""
from __future__ import annotations

import json
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from pa import adapty_cli
from pa.experiments import REPORT_SCHEMA, build_report

st.set_page_config(page_title="Experiment Autopilot", page_icon="🧪", layout="wide")
REPORT = Path("out/report.json")
VERDICT = {
    "stop_and_roll_out": ("🛑 Decided — stop and roll out the winner", "error"),
    "split_by_segment": ("🔀 Winner differs by segment — split the audience", "warning"),
    "guardrail_failed": ("🚧 Winner on revenue, but a guardrail got worse — review before rollout", "warning"),
    "check_data": ("🧯 Data health check failed — fix the split before trusting results", "warning"),
    "keep_running": ("⏳ Not decided yet — keep running", "info"),
    "completed_confirmed": ("✅ Completed — full history confirms the result", "success"),
    "completed_inconclusive": ("⚠️ Completed — on full history the result is NOT significant", "warning"),
    "draft_plan": ("📐 Draft — sample-size plan before you start", "info"),
    "unsupported": ("➖ Can't judge on history yet — showing what Adapty sees", "info"),
}


@st.cache_data(show_spinner="Reading Adapty tests and 4 months of history…", max_entries=1)
def load_report(mtime_ns: int) -> dict:  # keyed on the file version only
    return json.loads(REPORT.read_text())


def report_mtime() -> int:
    try:
        return REPORT.stat().st_mtime_ns
    except OSError:
        return 0


def bar(df: pd.DataFrame, x: str, y: str, x_title: str, y_title: str, fmt: str = ",.2f") -> alt.Chart:
    """Bar chart that keeps the row order of df (st.bar_chart sorts the x axis alphabetically)."""
    return (alt.Chart(df).mark_bar(color="#F2A65A").encode(
        x=alt.X(x, sort=None, title=x_title, axis=alt.Axis(labelAngle=0)),
        y=alt.Y(y, title=y_title), tooltip=[x, alt.Tooltip(y, format=fmt)]).properties(height=280))


if "log" not in st.session_state:
    st.session_state.log = []

st.title("🧪 Experiment Autopilot for Adapty")
st.caption("Adapty's A/B page judges a test from the day it starts: before trial revenue has arrived and without "
           "the months of history before it. We judge every test on matured 60-day revenue over 4 months of history, "
           "run the health checks of experimentation platforms (split, balance, guardrails, maturity, segments), and "
           "stop decided tests in Adapty — only after a human approves.")
if st.button("↻ Re-run analysis (Adapty CLI + offline history)"):
    with st.spinner("Reading Adapty tests and 4 months of history…"):
        build_report()
    load_report.clear()
if not REPORT.exists() or json.loads(REPORT.read_text()).get("schema") != REPORT_SCHEMA:
    with st.spinner("First run: reading Adapty tests and 4 months of history…"):
        build_report()
    load_report.clear()
r = load_report(report_mtime())
if st.session_state.get("flash"):
    st.success(st.session_state.pop("flash"))

tests = r["tests"]
live = [t for t in tests if t["state"] == "live"]
decided = [t for t in live if t["verdict"] == "stop_and_roll_out"]
c1, c2, c3, c4 = st.columns(4)
c1.metric("Live tests", len(live))
c2.metric("Decided, still running", len(decided))
c3.metric("Revenue lost per month", f"${r['total_lost_per_month_usd']:,.0f}",
          help="One month of new installs on the losing arms, each followed for 60 days (Lumen scale).")
c4.metric("Judged on", f"{r['horizon_days']}-day revenue")
names = r.get("segment_names", {})


def adapty_view(t: dict) -> str:
    lv = t.get("live_view")
    return " · ".join(f"{k.split(' - ')[-1]} ${v:,.0f}" for k, v in lv["revenue"].items()) if lv else "–"


def our_view(t: dict) -> str:
    if t["verdict"] == "unsupported":
        return "–"
    if t["verdict"] == "draft_plan":
        return f"needs ≈{t['days_to_decision'] or '?'} days → {t['recommendation'].split(':')[0]}"
    return f"{t['winner']['name'].split(' - ')[-1]} +{t['lift_pct']}% (P {t['p_winner_better']:.0%})"


st.dataframe(pd.DataFrame([{
    "test": t["title"], "state": t["state"],
    "Adapty since start (revenue/arm)": adapty_view(t),
    f"Matured {r['horizon_days']}-day revenue": our_view(t),
    "verdict": VERDICT.get(t["verdict"], (t["verdict"],))[0],
    "$ / month": f"${t['lost_per_month_usd']:,.0f}" if t["state"] == "live" and t.get("lost_per_month_usd") else "",
} for t in tests]), hide_index=True, use_container_width=True)

for t in tests:
    label, kind = VERDICT.get(t["verdict"], (t["verdict"], "info"))
    with st.container(border=True):
        st.subheader(f"{t['title']}  ·  {t['placement']}  ·  `{t['state']}`")
        getattr(st, kind)(label)

        if t["verdict"] == "draft_plan":
            a, b, c = st.columns(3)
            a.metric("Users needed per arm", f"{t['sample_size_per_arm']:,}" if t["sample_size_per_arm"] else "–",
                     help="to detect a 10% revenue lift, 80% power")
            b.metric("Days to a decision", t["days_to_decision"] or "–")
            c.metric("Smallest lift visible in 30 days",
                     f"{t['mde_with_30_days_pct']:.0f}%" if t.get("mde_with_30_days_pct") else "–")
            st.markdown(f"**Recommendation:** {t['recommendation']}.")
            st.markdown("\n".join(f"- {x}" for x in t["reasons"] if x))
            continue
        if t["verdict"] == "unsupported":
            st.markdown("\n".join(f"- {x}" for x in t["reasons"]))
            st.markdown(f"**What Adapty sees:** {adapty_view(t)}")
            continue

        a, b, c = st.columns([2, 2, 3])
        a.metric(f"🏆 {t['winner']['name']}", f"${t['winner']['rev_per_user']:.2f} / user",
                 f"+{t['lift_pct']}% vs other arm")
        a.caption(f"payer rate {t['winner']['payer_rate']:.1%} · {t['winner']['users']:,} matured users")
        b.metric(t["loser"]["name"], f"${t['loser']['rev_per_user']:.2f} / user")
        b.caption(f"payer rate {t['loser']['payer_rate']:.1%} · {t['loser']['users']:,} matured users")
        c.markdown("**Why this verdict**\n" + "\n".join(f"- {x}" for x in t["reasons"]))
        if t["state"] == "live":
            c.markdown(f"- Cost of keeping it running: **${t['lost_per_month_usd']:,.0f}/month**")

        lv = t.get("live_view")
        if lv:
            lv_txt = " · ".join(f"{k}: ${v:,.0f}" for k, v in lv["revenue"].items())
            st.markdown(f"**What Adapty sees since {lv['since']}:** {lv_txt}")
        if t["maturity"]["early_bias"]:
            st.warning("⏱️ Immature revenue: " + t["maturity"]["early_bias"])

        h1, h2, h3 = st.columns(3)
        with h1.expander("Health checks"):
            hc = t["health"]
            st.markdown(f"- Traffic split (SRM): p = {hc['srm_pvalue']} {'✅' if hc['srm_pvalue'] > 0.01 else '❌'}\n"
                        f"- Randomization balance (intent score): p = {hc['balance_pvalue']} "
                        f"{'✅' if hc['balance_pvalue'] > 0.001 else '❌'}")
        with h2.expander("Guardrails (per charge)"):
            g = t["guardrails"]
            st.dataframe(pd.DataFrame([
                {"metric": "refund rate", "winner": f"{g['winner']['refund_rate']:.2%}",
                 "other": f"{g['other']['refund_rate']:.2%}", "p": g["refund_p"]},
                {"metric": "billing-issue rate", "winner": f"{g['winner']['billing_issue_rate']:.2%}",
                 "other": f"{g['other']['billing_issue_rate']:.2%}", "p": g["billing_p"]}]), hide_index=True)
        with h3.expander("Revenue maturity"):
            mat = pd.DataFrame([{"day": d, "arm": arm, "share of 60-day revenue": t["maturity"][k][str(d)]
                                 if str(d) in t["maturity"][k] else t["maturity"][k][d]}
                                for k, arm in [("winner", t["winner"]["name"]), ("other", t["loser"]["name"])]
                                for d in [1, 7, 30, 60]])
            st.altair_chart(alt.Chart(mat).mark_line(point=True).encode(
                x=alt.X("day:O", title="days since install", axis=alt.Axis(labelAngle=0)),
                y=alt.Y("share of 60-day revenue:Q", title="revenue collected",
                        scale=alt.Scale(domain=[0, 1]), axis=alt.Axis(format="%", tickCount=5)),
                color=alt.Color("arm:N", legend=alt.Legend(orient="bottom", title=None, columns=1))
            ).properties(height=300),
                use_container_width=True)

        with st.expander("Does the winner hold in every segment? (intent model, platform, channel · Bonferroni)"):
            seg = pd.DataFrame(t["segments"])
            if not seg.empty:
                seg["ci"] = seg["ci"].map(lambda x: f"{x[0]:+.2f} … {x[1]:+.2f}")
                st.dataframe(seg, hide_index=True, use_container_width=True)

        if t["verdict"] == "stop_and_roll_out":
            st.markdown("#### Change Guard — plan before anything reaches users")
            audience = ("users in segment " + ", ".join(f"**{names.get(s, s)}**" for s in t["segment_ids"])
                        if t["segment_ids"] else "**all users**")
            st.markdown(
                f"- **What changes:** placement *{t['placement']}* stops splitting traffic; {audience} see "
                f"**{t['winner']['name']}** only.\n"
                f"- **Who is affected:** the {t['loser']['weight']}% who now get *{t['loser']['name']}* "
                f"(≈{t['daily_users_in_test'] * t['loser']['weight'] / 100:.0f} new users/day).\n"
                f"- **Expected effect:** +${t['lost_per_month_usd']:,.0f}/month in {t['horizon_days']}-day revenue.\n"
                f"- **Can't be undone:** a stopped test can't restart; to test again, create a copy.")
            st.code(" ".join(adapty_cli.stop_command(t["ab_test_id"], t["winner"]["content_id"])), language="bash")
            ok = st.checkbox("I reviewed the plan and approve stopping this test", key=f"ok-{t['ab_test_id']}")
            if st.button("Stop test & roll out winner", key=f"go-{t['ab_test_id']}", disabled=not ok, type="primary"):
                try:
                    with st.spinner("Re-checking on fresh Adapty data before acting…"):
                        fresh = {x["ab_test_id"]: x for x in build_report()["tests"]}.get(t["ab_test_id"])
                    if (not fresh or fresh["verdict"] != "stop_and_roll_out"
                            or fresh["winner"]["content_id"] != t["winner"]["content_id"]):
                        load_report.clear()
                        raise RuntimeError("the fresh data no longer supports this plan (test changed or another "
                                           "winner); review the updated card and approve again")
                    res = adapty_cli.stop_test(t["ab_test_id"], t["winner"]["content_id"])
                    entry = {"at": pd.Timestamp.now(tz="UTC").isoformat(), "approved_by": "dashboard user",
                             "action": "stop", "test": t["title"], "winner": t["winner"]["name"], "result": res}
                    st.session_state.log.append(entry)
                    with Path("out/audit.log").open("a") as f:
                        f.write(json.dumps(entry) + "\n")
                    build_report()  # the card now shows the test as completed; no second click possible
                    load_report.clear()
                    st.session_state.flash = f"Stopped “{t['title']}” in Adapty — {t['winner']['name']} rolled out."
                    st.rerun()
                except RuntimeError as err:
                    st.error(f"Not stopped: {err}")

st.divider()
st.subheader("Who buys? (intent model on day-one signals)")
i1, i2 = st.columns(2)
rev = r["intent"]["rev_per_user_by_intent"]
i1.altair_chart(bar(pd.DataFrame({"intent": list(rev), "revenue": list(rev.values())}), "intent", "revenue",
                    "purchase intent (model)", f"{r['horizon_days']}-day revenue / user ($)"),
                use_container_width=True)
ed = r["intent"]["payer_rate_by_ai_edits_day1"]
i2.altair_chart(bar(pd.DataFrame({"AI edits on day 1": list(ed), "payers": [100 * v for v in ed.values()]}),
                    "AI edits on day 1", "payers", "AI edits on day 1", "users who pay (%)", ",.1f"),
                use_container_width=True)
st.caption("The intent model checks whether a winner is the same for low-, mid- and high-intent users. "
           "Here it is, so one winner for everyone is the right call.")
if st.session_state.log:
    st.subheader("Audit log")
    st.json(st.session_state.log)
