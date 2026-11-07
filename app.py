"""Experiment Autopilot for Adapty — Streamlit UI.

Run:  uv run streamlit run app.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from pa import adapty_cli
from pa.experiments import build_report

st.set_page_config(page_title="Experiment Autopilot", page_icon="🧪", layout="wide")
REPORT = Path("out/report.json")
VERDICT = {
    "stop_and_roll_out": ("🛑 Decided — stop and roll out the winner", "error"),
    "split_by_segment": ("🔀 Winner differs by segment — split the audience", "warning"),
    "keep_running": ("⏳ Not decided yet — keep running", "info"),
    "completed_confirmed": ("✅ Completed — full history confirms the result", "success"),
    "completed_inconclusive": ("⚠️ Completed — full history says the result is not significant", "warning"),
}


@st.cache_data(show_spinner="Reading Adapty tests and 4 months of history…")
def get_report(refresh: int) -> dict:
    if refresh == 0 and REPORT.exists():
        return json.loads(REPORT.read_text())
    return build_report()


if "refresh" not in st.session_state:
    st.session_state.refresh = 0
if "log" not in st.session_state:
    st.session_state.log = []

st.title("🧪 Experiment Autopilot for Adapty")
st.caption("Adapty attributes A/B results only to users who arrived after a test started, so the dashboard judges "
           "live tests on today's traffic. We judge them on the full 4-month history, check every segment, and "
           "stop decided tests — after a human approves.")
if st.button("↻ Re-run analysis (Adapty CLI + offline history)"):
    st.session_state.refresh += 1
r = get_report(st.session_state.refresh)

live = [t for t in r["tests"] if t["state"] == "live"]
decided = [t for t in live if t["verdict"] != "keep_running"]
c1, c2, c3, c4 = st.columns(4)
c1.metric("Live tests", len(live))
c2.metric("Already decided", len(decided))
c3.metric("Revenue lost per month", f"${r['total_lost_per_month_usd']:,.0f}")
c4.metric("Judged on", f"{r['horizon_days']}-day revenue")

for t in r["tests"]:
    label, kind = VERDICT[t["verdict"]]
    with st.container(border=True):
        st.subheader(f"{t['title']}  ·  {t['placement']}  ·  `{t['state']}`")
        getattr(st, kind)(label)
        a, b, c = st.columns([2, 2, 3])
        a.metric(f"🏆 {t['winner']['name']}", f"${t['winner']['rev_per_user']:.2f} / user",
                 f"+{t['lift_pct']}% vs other arm")
        a.caption(f"payer rate {t['winner']['payer_rate']:.1%} · {t['winner']['users']:,} matured users")
        b.metric(t["loser"]["name"], f"${t['loser']['rev_per_user']:.2f} / user")
        b.caption(f"payer rate {t['loser']['payer_rate']:.1%} · {t['loser']['users']:,} matured users")
        c.markdown(
            f"- 95% CI of the gap: **${t['diff_ci95'][0]:.2f} … ${t['diff_ci95'][1]:.2f}** per user\n"
            f"- P(winner is better): **{t['p_winner_better']:.1%}**\n"
            f"- Split check (SRM): p = {t['srm_pvalue']} {'✅' if t['srm_pvalue'] > 0.01 else '❌ traffic split broken'}\n"
            f"- Segments where the winner flips: **{t['segments_flipped']}** · unclear: {t['segments_unclear']}\n"
            f"- Cost of keeping the test running: **${t['lost_per_month_usd']:,.0f}/month** "
            f"(${t['lost_per_month_per_1k_installs_usd']:,.0f} per 1k installs)")
        with st.expander("Does the winner hold in every segment? (intent model, platform, channel)"):
            seg = pd.DataFrame(t["segments"])
            if not seg.empty:
                seg["ci95"] = seg["ci95"].map(lambda x: f"{x[0]:+.2f} … {x[1]:+.2f}")
                st.dataframe(seg, hide_index=True, use_container_width=True)

        if t["verdict"] == "stop_and_roll_out":
            st.markdown("#### Change Guard — plan before anything reaches users")
            names = r.get("segment_names", {})
            audience = ("users in segment " + ", ".join(f"**{names.get(s, s)}**" for s in t["segment_ids"])
                        if t["segment_ids"] else "**all users**")
            st.markdown(
                f"- **What changes:** placement *{t['placement']}* stops splitting traffic; {audience} see "
                f"**{t['winner']['name']}** only.\n"
                f"- **Who is affected:** the {t['loser']['weight']}% who now get *{t['loser']['name']}* "
                f"(≈{t['daily_users_in_test'] * t['loser']['weight'] / 100:.0f} new users/day).\n"
                f"- **Expected effect:** +${t['lost_per_month_usd']:,.0f}/month in {t['horizon_days']}-day revenue.\n"
                f"- **Can't be undone:** a stopped test can't restart; to test again, create a copy.")
            st.code(" ".join(adapty_cli.stop_command(t["ab_test_id"], t["winner"]["flow_id"])), language="bash")
            ok = st.checkbox("I reviewed the plan and approve stopping this test", key=f"ok-{t['ab_test_id']}")
            if st.button("Stop test & roll out winner", key=f"go-{t['ab_test_id']}", disabled=not ok, type="primary"):
                try:
                    res = adapty_cli.stop_test(t["ab_test_id"], t["winner"]["flow_id"])
                    st.session_state.log.append({"test": t["title"], "action": "stopped", "result": res})
                    st.success("Stopped in Adapty. Check the dashboard → A/B tests.")
                except RuntimeError as err:
                    st.error(f"Adapty refused: {err}")

st.divider()
st.subheader("Who buys? (intent model on day-one signals)")
i1, i2 = st.columns(2)
i1.bar_chart(pd.Series(r["intent"]["rev_per_user_by_intent"]).reindex(["low", "mid", "high"]),
             y_label=f"{r['horizon_days']}-day revenue / user ($)")
i2.bar_chart(pd.Series(r["intent"]["payer_rate_by_ai_edits_day1"]), y_label="payer rate by AI edits on day 1")
st.caption("The intent model is used to check whether a test winner is the same for low-, mid- and high-intent "
           "users. In this data it is — so one winner for everyone is the right call.")
if st.session_state.log:
    st.subheader("Audit log")
    st.json(st.session_state.log)
