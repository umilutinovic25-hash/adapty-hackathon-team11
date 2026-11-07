# Experiment Autopilot for Adapty

**Adapty Hackathon Belgrade, 7–8 Nov 2026 · Team 11**

Adapty's A/B page judges a test from the day it starts. Early on, part of the revenue hasn't happened yet: an
annual plan with a 7-day trial pays nothing in week one, a weekly plan pays on day one. Experiment Autopilot judges
every Adapty test on **matured 60-day revenue over the full history**. It runs the health checks of experimentation
platforms (traffic split, randomization balance, guardrails, revenue maturity, segment consistency), puts a **dollar
figure on keeping a decided test running**, and stops it through the Adapty CLI **only after a human approves**. A
Claude Code skill gives the same functions to an AI agent, with the same approval gate.

## Result on the Lumen demo app

| Test | Adapty dashboard after 15 h | Experiment Autopilot (60-day revenue, 4 months) | Verdict |
|---|---|---|---|
| Main paywall, US & CA (live) | P2BB 68%: keep waiting | Lifetime anchor $15.99 vs $9.23 (**+73%**, CI $4.79…$8.88); no segment reversed (8/10 clearly ahead, 2 too small to tell) | stop & roll out, **$354/mo** |
| Onboarding (live) | P2BB 100% for Weekly, but Annual trial shows $0 because its 7-day trial hasn't converted yet | Weekly $8.56 vs $4.51 (**+90%**), confirmed on matured revenue; ⏱️ immature-revenue warning | stop & roll out, **$1,396/mo** |
| Settings (completed) | completed | not significant (P 92%, CI −$0.61…$4.13) | ⚠️ result was trusted too early |
| AI limit (draft) | draft | ≈2,233 users/arm for a 10% lift ≈ 1,545 days at Lumen traffic (≈3 users/day at that placement) | 📐 don't start as designed |

**≈ $1,749/month** on the losing arms of the two live tests: one month of new installs, each followed for 60 days,
at Lumen scale. That is about a quarter of Lumen's $6.4k MRR.

## Who it is for

Growth and product people running paywall and onboarding experiments in Adapty. Today they read the A/B page, which
covers recent users only, often before trial revenue arrives. Experiment Autopilot changes **money** (no more paying
for decided losing arms) and **risk** (no rollout without health checks, guardrails, a segment check and an explicit
approval).

## How it works

1. **Adapty CLI**: `ab-tests list/get` for tests, arms, weights and audiences; `analytics chart --group-by flow_id
   --filter placement_id=…` for what the dashboard sees since the test started (the "live view").
2. **Full history**: the offline dataset says which arm every user saw. Net revenue in the first 60 days counts
   trials as $0 and removes refunds, and only users whose 60 days have passed are included.
3. **Decision**: revenue per user per arm, bootstrap 95% CI, P(winner better).
4. **Health** (as in GrowthBook): SRM on the traffic split; randomization balance on a pre-exposure intent score.
5. **Guardrails** (as in GrowthBook/Eppo): refund rate and billing-issue rate **per charge**. A per-payer rate would
   penalise weekly plans for charging more often. P-values come from a bootstrap over **users**, because renewals
   of one user are correlated.
6. **Revenue maturity**: share of 60-day revenue each arm has collected by day 1/7/30/60. If the arms differ by
   ≥ 25 points on day 1, an immature-revenue warning explains which arm an early read favours.
7. **Segments**: the same comparison inside intent (low/mid/high from a cross-fitted gradient-boosting model on
   day-one signals), platform and acquisition channel, Bonferroni-corrected.
8. **Planning**: sample size for a 10% lift, days to a decision, and the smallest lift visible in 30 days.
9. **Change Guard**: shows what changes, the audience, who is affected, the expected effect, and that a stop can't be
   undone. After approval it runs `adapty ab-tests stop <id> --winner <id>` and appends to `out/audit.log`.

## Run

```bash
cp .env.example .env                 # app id + path to the seed repo's data/offline
npm i -g adapty && adapty auth login
uv sync
uv run python -m pa.cli check        # Markdown brief (writes out/report.json)
uv run streamlit run app.py          # dashboard
sh .claude/verify.sh                 # checks; asserts the pre-demo verdicts, so it fails once a test is stopped
```

### For AI agents (Claude Code)

`.claude/skills/experiment-autopilot/SKILL.md` teaches the agent `pa.cli check → plan → stop`. `stop` needs
`--winner-id` (from the approved plan) and `--approved-by`. It re-judges on fresh data right before acting and
refuses if the verdict changed or the fresh winner differs from the approved one, then writes the audit log. The
dashboard's Stop button applies the same fresh re-check.

Tests: `uv run pytest` (13 behaviour tests, Adapty CLI mocked from fixtures; includes the post-stop state, a stale
approval, paywall tests and odd arm weights).

## Limits

- Full-history judgement needs the user→arm history (here the offline dataset; in production an Adapty S3/BigQuery
  export). Without it the tool still shows the live view and the plans.
- Paywall-type tests are listed as "can't judge on history yet" with what Adapty sees; the offline dataset has flows only.
- Dollar figures are scaled to Lumen's live size (5k of 50k offline users).

## Data and AI disclosure

- Only the synthetic Lumen demo data from `kpotehin/adapty-seed-data-hack`. No real user data.
- All code was written during the hackathon with Claude Code (Opus 5.5) as a pair programmer. Pre-existing: open-source
  libraries only (pandas, scikit-learn, scipy, streamlit, altair). The health-check patterns follow public
  GrowthBook/Statsig/Eppo documentation; no code was copied.
