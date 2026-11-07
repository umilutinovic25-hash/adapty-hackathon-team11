---
name: experiment-autopilot
description: Judge the Adapty A/B tests of an app on full-history, matured revenue and stop decided ones safely. Use when the user asks whether their Adapty paywall or onboarding tests are decided, what a running test costs, whether a winner is safe to roll out, or how long a draft test would need.
---

# Experiment Autopilot for Adapty

Works from the repository root with the Adapty CLI logged in (`adapty auth login`) and `uv` installed.

## 1. Read the state (no side effects)

```bash
uv run python -m pa.cli check          # Markdown brief of every test
uv run python -m pa.cli check --json   # full numbers when you need them
```

Explain the brief in plain language, test by test. Lead with money: what each decided live test costs per month.
Always mention:
- the verdict and the one-line reasons (effect + CI, split and balance health, guardrails, segments);
- an **immature revenue** warning when present: the Adapty dashboard is read before one arm's trial revenue arrives;
- for drafts, the sample-size plan and whether to start at all.

Never claim more than the numbers say. `completed_inconclusive` means the result someone acted on is not significant.

## 2. Plan a change (no side effects)

Only for a test whose verdict is `stop_and_roll_out`:

```bash
uv run python -m pa.cli plan <ab_test_id>
```

Show the plan to the user: what changes, audience, who is affected, expected effect, and that **a stopped test
can't be restarted**. Ask for an explicit yes. Do not continue on silence, on a vague answer, or on approval given
for a different test.

## 3. Act only after an explicit yes

```bash
uv run python -m pa.cli stop <ab_test_id> --winner-id <winner_id from the approved plan> --approved-by "<the user's name>"
```

`stop` re-judges on fresh data and refuses if the verdict changed or the fresh winner differs from the approved
`winner_id`. In that case show the new plan and ask again. It appends the action to `out/audit.log`.
Afterwards, tell the user to check Adapty → A/B tests, and run `check` again.

## Don't

- Don't stop a test whose verdict is anything other than `stop_and_roll_out` (the CLI refuses as well).
- Don't run `adapty ab-tests stop` directly; go through `pa.cli stop` so the re-check and the audit log run.
- Don't use real end-user data; this project uses the synthetic Lumen demo data.
