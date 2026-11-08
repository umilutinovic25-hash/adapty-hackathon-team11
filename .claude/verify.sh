#!/bin/sh
# Pre-commit check: code compiles, the analysis runs end to end against Adapty, the safety refusal works.
set -e
cd "$(dirname "$0")/.."
uv run python -m compileall -q pa app.py
uv run pytest -q
uv run python -m pa.cli check > /dev/null
uv run python - <<'EOF'
import json
r = json.load(open("out/report.json"))
v = {t["title"]: t["verdict"] for t in r["tests"]}
# before the demo both tests are decided; after a stop on stage a test is completed_confirmed and its money drops out
money = {"Onboarding: annual trial vs weekly first": 1395.68, "Main: 3 plans vs lifetime anchor (NA)": 353.63}
for title in money:
    assert v[title] in ("stop_and_roll_out", "completed_confirmed"), v
assert v["Settings: upgrade vs lifetime anchor"] == "completed_inconclusive", v
expected = sum(m for title, m in money.items() if v[title] == "stop_and_roll_out")
assert abs(r["total_lost_per_month_usd"] - expected) < 1, (r["total_lost_per_month_usd"], expected)
print("verdicts OK:", v)
EOF
if ! uv run python -m pa.cli plan 1b9ba3a4-e3a0-49b7-b7e7-70cd09fead40 2>&1 \
    | grep -q "refusing: verdict is completed_inconclusive"; then
  echo "FAIL: plan should refuse the completed Settings test with the verdict message"; exit 1
fi
echo "verify OK"
