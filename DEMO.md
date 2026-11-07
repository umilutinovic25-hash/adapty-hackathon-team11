# Demo script — 5 minutes + 2 min Q&A

**Before going on stage**
- `uv run python -m pa.cli check` (fresh report), then `uv run streamlit run app.py`
- Tabs, in this order: slide · Streamlit app · Adapty dashboard → A/B tests (Lumen) · terminal with Claude Code
- **Rehearse only on "Lumen BACKUP"** (app selector): start the app with
  `OFFLINE_APP_IDS=f414265a-e67a-41ce-85b1-c4c1d02c3816,df26756a-b66a-42ee-a1de-365829b7114d uv run streamlit run app.py`.
  Its Main test was already stopped in rehearsal (7 Nov, 13:10); its **Onboarding** test is still free for one more
  run. **Never click Stop on the main Lumen app before the demo.**
- On stage, select **Lumen - AI Photo Editor (Hackathon)** (the default).
- Backup: `~/Downloads/experiment_autopilot_demo_backup.gif` (flow up to approval), in case Wi-Fi or the Adapty API is down.

| Time | Screen | Say (approximately) |
|---|---|---|
| 0:00–0:30 | Slide | "Every subscription app runs paywall and onboarding A/B tests. Two questions nobody answers well: *is this test already decided?* and *what does it cost me to keep it running?*" |
| 0:30–1:15 | Adapty → A/B tests | "Lumen has two live tests. Main paywall: Adapty says probability to be best 68%, so keep waiting. Onboarding: 100% for Weekly, but look: Annual trial shows **$0**. **0 of 138 trials in this test have finished** (our summary table shows it from Adapty's own flow metrics). The dashboard reads tests before part of the revenue exists." |
| 1:15–2:15 | App → Main card | "We judge the same tests on 60-day revenue over four months of history. Main: the lifetime anchor earns **$15.99 vs $9.23**, +73%, CI far from zero. Split healthy, groups balanced, refunds and billing issues not worse. **Decided.** Every week at 50/50 costs money." |
| 2:15–3:00 | Onboarding card → ⏱️ warning + *Revenue maturity* | "Onboarding: Weekly wins on matured revenue too, +90%. Our maturity check shows why the dashboard looked so sure: by day 1 Weekly has 46% of its revenue and Annual trial 0%. Here the early read happened to be right. In another app the same bias picks the wrong winner." |
| 3:00–3:20 | Segments expander | "Is one winner right for everyone? Our intent model splits users into low, mid and high purchase intent, plus platform and channel. Onboarding: the winner is clearly ahead in all 10 segments. Main: no segment reverses; 2 are too small to tell. Bonferroni-corrected." |
| 3:20–3:35 | Top metric + Settings + Draft cards | "Together: **≈$1,749 a month**, a quarter of MRR. Bonus: the completed Settings test isn't significant at all, and the AI-limit draft would need ~1,500 days at this traffic. Don't start it as designed." |
| 3:35–4:30 | Main card → Change Guard → tick → **Stop** (≈12 s fresh re-check) → Adapty refresh | "The agent never acts alone: what changes, who's affected, dollar effect, can't be undone. I approve. *(click)* It re-checks on fresh data first, so a stale plan can't ship. … *(card turns to completed; dashboard)* Winner rolled out, and it's in the audit log." |
| 4:30–5:00 | Slack digest expander → slide 2 | "It runs without anyone opening a dashboard: a daily Slack digest, a portfolio scan across apps, and a Claude Code skill that asks before it touches anything. Business: it belongs inside Adapty next to Autopilot. Autopilot proposes the next test, we close the running ones. Who: growth teams. What changes: money and risk. Why Adapty: we read its tests and act through its CLI." |

## Likely jury questions

- **Why does this need Adapty?** It reads Adapty's tests, arms, weights, audiences and analytics, and acts through Adapty's CLI. The gap it fills follows from how Adapty attributes test results.
- **Isn't this Autopilot / the A/B page?** Autopilot proposes *what to test next*. The A/B page shows P2BB since the test started. We judge *running* tests on matured revenue, add health checks and guardrails, and stop them safely. These are complements.
- **Why AI?** (1) The intent model answers the rollout-safety question: does the winner hold for low- and high-intent users? (2) The agent interface: an AI that can read *and act* on an Adapty account needs exactly this kind of guardrail and approval gate.
- **How reliable is it?** 60-day net revenue (trials $0, refunds removed), matured users only, bootstrap CI, SRM and balance checks, guardrails per charge, Bonferroni on segments.
- **Production?** Needs user→arm history: an Adapty S3/BigQuery export in place of our offline CSV. The live view already comes from Adapty's analytics API.
- **Next step?** Daily run with a Slack message: "test X decided, $Y/month at stake. Approve?"
- **AI tools used?** Claude Code with the Adapty CLI. Disclosed in the README.
