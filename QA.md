# Q&A card: numbers and answers

## Numbers (Lumen demo app, 7 Nov 2026)

| What | Value |
|---|---|
| Money on losing arms of decided live tests | **≈ $1,749 / month** (onboarding $1,396 + main $354) |
| Today's MRR (Adapty analytics) | $6,351, so the loss is ≈ 28% of MRR |
| Onboarding: Weekly first vs Annual trial | $8.56 vs $4.51 per user, 60-day net revenue, **+90%**, 95% CI of gap $3.52…$4.60 |
| Main (US & CA): Lifetime anchor vs 3 plans | $15.99 vs $9.23, **+73%**, CI $4.79…$8.88 |
| Adapty right now, onboarding | Annual trial $0 vs Weekly $1,528; **0 of 138 trials finished** |
| Adapty right now, main (test users only) | 3 plans $63 (40 users) vs Lifetime $113 (37 users); dashboard P2BB 68% |
| Revenue maturity at day 1 | Weekly 46% of its 60-day revenue, Annual trial 0% |
| Settings (completed) | $12.60 vs $10.80, CI −$0.61…$4.13, P 92%: **not significant** |
| AI-limit draft | ≈2,233 users/arm for a 10% lift ≈ 1,545 days at ≈3 users/day; 30 days detect only ≥70% |
| Health | SRM p = 0.55 / 0.38; balance p = 0.16 / 0.65; guardrails OK (per charge, user bootstrap) |
| Segments | onboarding 10/10 clearly ahead; main 8/10 clearly ahead, 2 unclear, 0 reversed |

## Likely questions

**Why does this need Adapty?** It reads Adapty's tests, arms, weights, audiences, flow metrics and MRR through the
Adapty CLI, and acts through it. The gap it closes comes from how Adapty attributes results: from the moment a test
starts, before trial revenue arrives.

**Isn't this Autopilot?** No, it complements Autopilot. Autopilot proposes the *next* test; we close *running* tests
safely, on matured revenue with health checks.

**Why AI?** (1) An intent model checks whether one winner is right for low-, mid- and high-intent users. It is here,
so the verdict is "roll out to everyone". (2) The agent interface: a Claude Code skill reads the account, explains the
numbers, and stops a test only after an explicit yes. That human-in-the-loop design is the point.

**Is $1,749 real money?** It is expected 60-day net revenue from new cohorts enrolled over 30 days on verified
losing arms. It is an estimate, not booked revenue or MRR. Today's $6,351 MRR is only a size reference: the ≈28%
comparison uses different revenue windows and does not mean a 28% MRR increase.

**Can I audit the headline?** Open the expander under the hero number. Daily traffic is scaled by
**5,000 / 50,000 = 0.1**, with unrounded inputs retained in the report:

- Onboarding: 27,569 offline users / 120 days × 0.1 = 22.974166… daily users;
  × 50% losing share × $4.05 revenue gap × 30 = **$1,395.68/month**.
- Main: 4,185 / 120 × 0.1 = 3.4875 daily users;
  × 50% × $6.76 × 30 = **$353.63/month**.
- Total: **$1,749.31/month**. The existing gap bootstrap gives two-arm 95% monthly intervals of
  **$1,211.86…$1,581.51** and **$250.84…$462.25**, respectively. They hold traffic and allocation fixed;
  they exclude traffic uncertainty. No aggregate multi-arm interval is claimed, and unverified arms contribute
  no claimed loss.

**Why trust the stats?** 60-day net revenue (trials $0, refunds removed), only users whose 60 days have passed,
bootstrap CI, SRM and balance checks, guardrails per charge with a user-level bootstrap, Bonferroni on segments.

**What if the data changes between plan and click?** Stop re-judges on fresh Adapty data and refuses if the verdict or
the winner changed. Every stop is written to an audit log with app, test, winner and approver.

**Can you demonstrate a refused stop?** Run
`uv run pytest -q tests/test_autopilot.py -k fresh_rejudge_refuses_stop`.
Two fixture-only cases approve an initial plan, then re-judge with a different winner or a failed traffic-split
health check. Both raise `StopRefused` before the stop adapter is called, with no success audit entry. The test
does not call Adapty, change a real app, or require a recording.

**Does it work on a real app?** The live view, plans and actions already use the real Adapty API. Full-history
judgement needs user→arm history, which in production comes from Adapty's S3/BigQuery export in place of our CSV.
Apps without it are shown as "can't judge on history yet", never guessed.

**How do we rehearse without using up the main app?** Create a new app, seed its catalog with
`python3 seed_adapty.py --app-id <id> --skip-data`, and append its ID to `OFFLINE_APP_IDS` in the shell that runs
the CLI and dashboard. [Exact commands and paths](README.md#seed-a-rehearsal-app). No purchase replay or secret key
is needed; the matured analysis uses the synthetic history. Keep the main Lumen tests live for the demo.

**What did you build during the hackathon?** All of it: analysis, dashboard, agent CLI, Claude Code skill, Slack
digest, **67 tests**. Pre-existing: open-source libraries only. AI tools: Claude Code, disclosed in the README.

**What's next?** S3/BigQuery history, a daily Slack digest with an approve link, and a home inside Adapty next to
Autopilot.
