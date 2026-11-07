# Demo script — 5 minutes + 2 min Q&A

**Before going on stage**
- `uv run python -m pa.cli check` (fresh report), then `uv run streamlit run app.py`
- Tabs, in this order: slide · Streamlit app · Adapty dashboard → A/B tests (Lumen) · terminal in the repo
- **Never click Stop on the main Lumen app before the demo.** Both rehearsal stops on "Lumen BACKUP" are used up
  (Main 13:10, Onboarding 13:56, both worked end to end). For another rehearsal, seed a new copy and add its id to
  `OFFLINE_APP_IDS`.
- On stage the app opens on **Lumen - AI Photo Editor (Hackathon)** (the default).
- Backups if Wi-Fi or the Adapty API fail: `~/Downloads/experiment_autopilot_full_stop_flow.gif` (whole flow including
  the stop, recorded on the rehearsal copy) and `docs/dashboard.jpg`. The app also opens the last saved analysis offline.
- Agent demo (pre-typed in the terminal, read-only):
  `claude -p "Are my Adapty A/B tests decided? What do they cost me? Use the experiment-autopilot skill. Read only: do not stop anything." --allowedTools "Bash(uv run python -m pa.cli check*)" "Skill" "Read"`
  It takes ≈60 s, so **start it at 0:30**, while you talk, and show the answer at 4:15.
- Numbers for Q&A: `QA.md`.

| Time | Screen | Say (approximately) |
|---|---|---|
| 0:00–0:30 | Slide | "Every subscription app runs paywall and onboarding A/B tests. Two questions nobody answers well: *is this test already decided?* and *what does it cost me to keep it running?*" |
| 0:30–1:15 | Adapty → A/B tests | "Lumen has two live tests. Main paywall: Adapty says probability to be best 68%, so keep waiting. Onboarding: 100% for Weekly, but look: Annual trial shows **$0**. **0 of 138 trials in this test have finished** (our summary table shows it from Adapty's own flow metrics). The dashboard reads tests before part of the revenue exists." |
| 1:15–2:15 | App → Main card | "We judge the same tests on 60-day revenue over four months of history. Main: the lifetime anchor earns **$15.99 vs $9.23**, +73%, CI far from zero. Split healthy, groups balanced, refunds and billing issues not worse. **Decided.** Every week at 50/50 costs money." |
| 2:15–3:00 | Onboarding card → ⏱️ warning + *Revenue maturity* | "Onboarding: Weekly wins on matured revenue too, +90%. Our maturity check shows why the dashboard looked so sure: by day 1 Weekly has 46% of its revenue and Annual trial 0%. Here the early read happened to be right. In another app the same bias picks the wrong winner." |
| 3:00–3:15 | Segments expander | "Is one winner right for everyone? Our intent model checks low, mid and high purchase intent, plus platform and channel: no segment reverses. One winner for everyone." |
| 3:15–3:30 | Hero number + summary table | "Together: **≈$1,749 a month**, 28% of today's MRR. Bonus: the completed Settings test isn't significant, and the AI-limit draft would need ~1,500 days at this traffic. Don't start it as designed." |
| 3:30–4:15 | Main card → Change Guard → tick → **Stop** (≈12 s fresh re-check) → Adapty refresh | "Nothing ships without a human: what changes, who's affected, dollar effect, can't be undone. I approve. *(click)* It re-checks on fresh data first, so a stale plan can't ship. … *(card turns to completed; dashboard)* Winner rolled out, and it's in the audit log." |
| 4:15–4:40 | Terminal: Claude Code answer | "The same brain works as an agent. I asked Claude Code in plain English whether my tests are decided. It answers with these numbers, flags the immature read, and offers to stop a test only after an explicit yes." |
| 4:40–5:00 | Slide 2 | "Business: it belongs inside Adapty next to Autopilot. Autopilot proposes the next test, we close the running ones, daily via Slack and across a whole portfolio. Who: growth teams. What changes: money and risk." |

## Likely jury questions

- **Why does this need Adapty?** It reads Adapty's tests, arms, weights, audiences and analytics, and acts through Adapty's CLI. The gap it fills follows from how Adapty attributes test results.
- **Isn't this Autopilot / the A/B page?** Autopilot proposes *what to test next*. The A/B page shows P2BB since the test started. We judge *running* tests on matured revenue, add health checks and guardrails, and stop them safely. These are complements.
- **Why AI?** (1) The intent model answers the rollout-safety question: does the winner hold for low- and high-intent users? (2) The agent interface: an AI that can read *and act* on an Adapty account needs exactly this kind of guardrail and approval gate.
- **How reliable is it?** 60-day net revenue (trials $0, refunds removed), matured users only, bootstrap CI, SRM and balance checks, guardrails per charge, Bonferroni on segments.
- **Production?** Needs user→arm history: an Adapty S3/BigQuery export in place of our offline CSV. The live view already comes from Adapty's analytics API.
- **Next step?** Daily run with a Slack message: "test X decided, $Y/month at stake. Approve?"
- **AI tools used?** Claude Code with the Adapty CLI. Disclosed in the README.
