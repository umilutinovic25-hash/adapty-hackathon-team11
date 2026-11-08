"""Daily digest for Slack: decided tests, money at stake, and a link to approve.

Posts only when SLACK_WEBHOOK_URL is set (in .env); otherwise writes out/slack_message.json so the message can be
previewed in the dashboard. Nothing in the digest acts on Adapty: approval still happens in the Change Guard.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests

from pa import out_dir

DASHBOARD_URL = os.getenv("AUTOPILOT_DASHBOARD_URL", "http://localhost:8501")


def lift_text(t: dict) -> str:
    """'+12.5%', or the dollar baseline when the other arm has zero or negative revenue."""
    if t.get("lift_pct") is None:
        return f"vs ${t['loser']['rev_per_user']:.2f}"
    return f"{t['lift_pct']:+}%"


def digest(report: dict, app_title: str | None = None) -> dict:
    app_title = app_title or report.get("app_id", "Adapty app")
    decided = [t for t in report["tests"] if t["state"] == "live" and t["verdict"] == "stop_and_roll_out"]
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"🧪 Experiment Autopilot · {app_title}"}}]
    if not decided:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "No live test is decided yet. ✅"}})
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text":
                       f"*{len(decided)} live test(s) are decided*, costing *${report['total_lost_per_month_usd']:,.2f}"
                       f"/month* on losing arms."}})
        for t in decided:
            lv = t.get("live_view") or {}
            if lv.get("immature"):
                warn = (f"\n⏱️ _Adapty's own numbers are immature: {lv['trials_finished']} of {lv['trials']} trials "
                        f"in this test have finished._")
            elif t["maturity"].get("early_bias"):
                warn = "\n⏱️ _Early reads of this test favour the arm that charges first._"
            else:
                warn = ""
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text":
                           f"*{t['title']}* · {t['placement']}\n🏆 {t['winner']['name']} "
                           f"{lift_text(t)} {t['horizon_days']}-day revenue/user "
                           f"(P {t['p_winner_better']:.0%}) · ${t['lost_per_month_usd']:,.2f}/month{warn}"}})
        blocks.append({"type": "actions", "elements": [{"type": "button", "style": "primary",
                       "text": {"type": "plain_text", "text": "Review & approve"},
                       "url": f"{DASHBOARD_URL}?app={report['app_id']}"}]})
    text = f"{len(decided)} decided Adapty test(s), ${report['total_lost_per_month_usd']:,.2f}/month at stake"
    return {"text": text, "blocks": blocks}


def message_path() -> Path:
    return out_dir() / "slack_message.json"


def send(report: dict, app_title: str | None = None) -> dict:
    msg = digest(report, app_title)
    out = message_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(msg, indent=2))
    url = os.getenv("SLACK_WEBHOOK_URL")
    if not url:
        return {"sent": False, "preview": str(out)}
    resp = requests.post(url, json=msg, timeout=10)
    return {"sent": resp.ok, "status": resp.status_code}
