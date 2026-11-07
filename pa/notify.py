"""Daily digest for Slack: decided tests, money at stake, and a link to approve.

Posts only when SLACK_WEBHOOK_URL is set (in .env); otherwise writes out/slack_message.json so the message can be
previewed in the dashboard. Nothing in the digest acts on Adapty: approval still happens in the Change Guard.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests

DASHBOARD_URL = os.getenv("AUTOPILOT_DASHBOARD_URL", "http://localhost:8501")
OUT = Path("out/slack_message.json")


def digest(report: dict, app_title: str = "Lumen") -> dict:
    decided = [t for t in report["tests"] if t["state"] == "live" and t["verdict"] == "stop_and_roll_out"]
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"🧪 Experiment Autopilot · {app_title}"}}]
    if not decided:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "No live test is decided yet. ✅"}})
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text":
                       f"*{len(decided)} live test(s) are decided*, costing *${report['total_lost_per_month_usd']:,.0f}"
                       f"/month* on losing arms."}})
        for t in decided:
            warn = "\n⏱️ _Adapty's early read is immature for this test._" if t["maturity"].get("early_bias") else ""
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text":
                           f"*{t['title']}* · {t['placement']}\n🏆 {t['winner']['name']} "
                           f"+{t['lift_pct']}% {t['horizon_days']}-day revenue/user "
                           f"(P {t['p_winner_better']:.0%}) · ${t['lost_per_month_usd']:,.0f}/month{warn}"}})
        blocks.append({"type": "actions", "elements": [{"type": "button", "style": "primary",
                       "text": {"type": "plain_text", "text": "Review & approve"}, "url": DASHBOARD_URL}]})
    text = f"{len(decided)} decided Adapty test(s), ${report['total_lost_per_month_usd']:,.0f}/month at stake"
    return {"text": text, "blocks": blocks}


def send(report: dict, app_title: str = "Lumen") -> dict:
    msg = digest(report, app_title)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(msg, indent=2))
    url = os.getenv("SLACK_WEBHOOK_URL")
    if not url:
        return {"sent": False, "preview": str(OUT)}
    resp = requests.post(url, json=msg, timeout=10)
    return {"sent": resp.ok, "status": resp.status_code}
