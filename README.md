# Experiment Autopilot for Adapty

**Adapty Hackathon Belgrade, 7–8 Nov 2026 · Team 11**

Adapty attributes a purchase to an A/B test only if it happened after the test was set up. A test that has run for
months is therefore judged in the dashboard on recent traffic only. Experiment Autopilot judges every live
Adapty test on the **full history**, checks that the winner holds in **every segment** (including a purchase-intent
model on day-one signals), puts a **dollar figure on keeping a decided test running**, and stops it through the Adapty
CLI **only after a human approves the plan**.

## Result on the Lumen demo app

| Test (live in Adapty) | Winner | 60-day revenue / user | 95% CI of gap | Segments flipped | Cost of running |
|---|---|---|---|---|---|
| Onboarding: annual trial vs weekly first | Weekly first | $8.56 vs $4.51 (+90%) | $3.52 … $4.60 | 0 | ≈ $1,396 / month |
| Main: 3 plans vs lifetime anchor (US & Canada) | Lifetime anchor | $15.99 vs $9.23 (+73%) | $4.79 … $8.88 | 0 | ≈ $354 / month |

≈ **$1,749 / month** left on the table, about a quarter of Lumen's $6.4k MRR. The completed Settings test is
**not significant** on the full history (p = 0.92).

## Who it is for

Growth and product people running paywall and onboarding experiments in Adapty. Today they look at the A/B test page,
which shows recent users only, and decide by gut. Experiment Autopilot changes **money** (stops paying for losing arms) and
**risk** (no rollout without a segment check and an explicit approval).

## How it works

1. `adapty ab-tests list/get` → live and completed tests, their arms (flow IDs), weights, audiences.
2. Offline dataset (`users.csv.gz`, `transactions.csv.gz`) → which arm every user saw + their 60-day net revenue
   (trials $0, refunds removed, only users whose 60 days have passed).
3. Per test: revenue per user per arm, bootstrap 95% CI, P(winner better), SRM check on the traffic split.
4. Heterogeneity: the same comparison inside every segment: intent (low/mid/high from a cross-fitted gradient-boosting
   model on day-one signals), platform, acquisition channel.
5. Verdict: `stop_and_roll_out` / `split_by_segment` / `keep_running` / `completed_*`.
6. Change Guard: shows what changes, who is affected, expected effect, and that a stop can't be undone. On approval it
   runs `adapty ab-tests stop <id> --winner <flow_id>`.

## Run

```bash
cp .env.example .env            # app id + path to the seed repo's data/offline
npm i -g adapty && adapty auth login
uv sync
uv run python -m pa.experiments # writes out/report.json
uv run streamlit run app.py
```

## Data and AI disclosure

- Only the synthetic Lumen demo data from `kpotehin/adapty-seed-data-hack`. No real user data.
- All code was written during the hackathon with Claude Code (Opus 5.5) as a pair programmer. Pre-existing:
  open-source libraries only (pandas, scikit-learn, scipy, streamlit).
- Revenue at Lumen scale = offline traffic × (5,000 replayed users / 50,000 offline users).
