"""Command line for agents (Claude Code skill), schedulers and humans.

  uv run python -m pa.cli check [--app ID] [--json] [--cached]   # judge every Adapty A/B test, Markdown brief
  uv run python -m pa.cli plan <test_id> [--app ID]              # Change Guard plan for one test (no side effects)
  uv run python -m pa.cli stop <test_id> --winner-id <id from plan> --approved-by "<name>" [--app ID]
  uv run python -m pa.cli portfolio                              # every app the account can open
  uv run python -m pa.cli notify [--app ID]                      # Slack digest (posts only if SLACK_WEBHOOK_URL)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from pa import adapty_cli, notify
from pa.experiments import REPORT_SCHEMA, build_report, portfolio, report_path

AUDIT = Path("out/audit.log")


def report(fresh: bool, app_id: str | None = None) -> dict:
    path = report_path(app_id)
    if not fresh and path.exists():
        cached = json.loads(path.read_text())
        if cached.get("schema") == REPORT_SCHEMA:
            return cached
    return build_report(app_id)  # fresh, missing, or written by an older version


def brief(r: dict) -> str:
    lines = [f"# Adapty A/B tests — matured revenue, full history (app {r['app_id']})",
             f"Revenue lost per month on decided live tests: **${r['total_lost_per_month_usd']:,.0f}**", ""]
    if not r.get("has_history", True):
        lines += ["_No user→arm history for this app: tests are listed with what Adapty sees._", ""]
    for t in r["tests"]:
        lines.append(f"## {t['title']} ({t['state']}) → `{t['verdict']}`  ·  id `{t['ab_test_id']}`")
        lines += [f"- {x}" for x in t["reasons"] if x]
        if t.get("maturity", {}).get("early_bias"):
            lines.append(f"- ⏱️ {t['maturity']['early_bias']}")
        lv = t.get("live_view")
        if lv:
            arms = ", ".join(f"{k} {v.get('users', '?')} users ${v.get('revenue', 0):,.0f}" for k, v in lv["arms"].items())
            lines.append(f"- Adapty since {lv['since']}: {arms}; trials finished {lv['trials_finished']}/{lv['trials']}")
        if t["state"] == "live" and "lost_per_month_usd" in t:
            lines.append(f"- Cost of keeping it running: ${t['lost_per_month_usd']:,.0f}/month")
        lines.append("")
    return "\n".join(lines)


def plan(r: dict, test_id: str) -> dict:
    t = next((x for x in r["tests"] if x["ab_test_id"] == test_id), None)
    if t is None:
        sys.exit(f"unknown test {test_id}")
    if t["verdict"] != "stop_and_roll_out":
        sys.exit(f"refusing: verdict is {t['verdict']}, not stop_and_roll_out")
    names = r.get("segment_names", {})
    audience = ", ".join(names.get(s, s) for s in t["segment_ids"]) or "all users"
    losers = ", ".join(a["name"] for a in t["arms"][1:])
    command = " ".join(adapty_cli.stop_command(t["ab_test_id"], t["winner"]["content_id"], app_id=r["app_id"]))
    return {"app_id": r["app_id"], "test": t["title"], "placement": t["placement"], "audience": audience,
            "winner": t["winner"]["name"], "winner_id": t["winner"]["content_id"],
            "affected": f"{t['losing_share']:.0%} now on {losers} "
                        f"(≈{t['daily_users_in_test'] * t['losing_share']:.0f} new users/day)",
            "expected_effect_usd_per_month": t["lost_per_month_usd"], "reversible": False, "command": command}


def main() -> None:
    p = argparse.ArgumentParser(prog="pa.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--json", action="store_true")
    c.add_argument("--cached", action="store_true", help="use the saved report instead of re-reading Adapty")
    pl = sub.add_parser("plan")
    pl.add_argument("test_id")
    s = sub.add_parser("stop")
    s.add_argument("test_id")
    s.add_argument("--winner-id", required=True, help="winner_id from the plan the human approved")
    s.add_argument("--approved-by", required=True, help="the human who approved the plan")
    sub.add_parser("portfolio")
    sub.add_parser("notify")
    for sp in (c, pl, s, sub.choices["notify"]):
        sp.add_argument("--app", default=None, help="Adapty app id (default: ADAPTY_APP_ID)")
    a = p.parse_args()

    if a.cmd == "check":
        r = report(fresh=not a.cached, app_id=a.app)
        print(json.dumps(r, indent=2) if a.json else brief(r))
    elif a.cmd == "plan":
        print(json.dumps(plan(report(fresh=False, app_id=a.app), a.test_id), indent=2))
    elif a.cmd == "stop":
        pln = plan(report(fresh=True, app_id=a.app), a.test_id)  # re-judge on fresh data right before acting
        if pln["winner_id"] != a.winner_id:
            sys.exit(f"refusing: fresh data picks {pln['winner']} ({pln['winner_id']}), not the approved winner "
                     f"{a.winner_id}. Run `plan` again and get a new approval.")
        res = adapty_cli.stop_test(a.test_id, pln["winner_id"], app_id=pln["app_id"])
        AUDIT.parent.mkdir(exist_ok=True)
        with AUDIT.open("a") as f:
            f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "approved_by": a.approved_by,
                                "action": "stop", **pln, "result": res}) + "\n")
        print(json.dumps({"stopped": True, **pln}, indent=2))
    elif a.cmd == "portfolio":
        rows = portfolio()
        print("| app | tests | live | decided | $/month | history |\n|---|---|---|---|---|---|")
        for row in rows:
            print(f"| {row['app']} | {row['tests']} | {row['live']} | {row['decided_live']} | "
                  f"${row['lost_per_month_usd']:,.0f} | {'yes' if row['has_history'] else 'live only'} |")
    elif a.cmd == "notify":  # meant for a daily cron: always judge on fresh Adapty data
        r = report(fresh=True, app_id=a.app)
        title = next((x["title"] for x in adapty_cli.list_apps() if x["id"] == r["app_id"]), r["app_id"])
        print(json.dumps(notify.send(r, title), indent=2))


if __name__ == "__main__":
    main()
