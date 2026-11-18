"""Alert the human. Slack incoming webhook if SLACK_WEBHOOK_URL is set, otherwise stdout.

The alert is a one-line brief plus a click-to-dial link. The rep should be able to call from the alert
without opening the CRM.
"""
import json
import os
import urllib.request

from . import route


def _brief(lead, kind):
    tier = lead.get("tier")
    band = (lead.get("balance_band") or "unknown").replace("_", " ")
    who = lead.get("name") or lead.get("email") or lead.get("phone") or "unknown"
    co = lead.get("company") or "no company"
    prov = lead.get("current_provider") or "unknown"
    crm = route.cfg()["crm_base_url"] + lead["id"]
    dial = f"tel:{lead['phone']}" if lead.get("phone") else "no phone on file"
    head = {
        "new": f"NEW {tier} lead",
        "escalate": f"ESCALATION: {tier} lead untouched at 3 min",
        "miss": f"SLA MISS: {tier} lead untouched at 5 min",
        "after_hours": f"AFTER HOURS {tier} lead (callback booked for next open)",
    }[kind]
    return (
        f"{head} | {who} @ {co} | {lead['source']} | {lead.get('intent')} | ~{band} | on {prov} | "
        f"score {lead.get('score')} | owner {lead.get('owner')} | {dial} | {crm}"
    )


def send(lead, kind, mention=None):
    text = _brief(lead, kind)
    if mention:
        text = f"{mention} {text}"
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if url:
        try:
            req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
            return {"channel": "slack", "text": text}
        except Exception as e:
            print(f"[alert] slack failed ({e}); falling back to stdout", flush=True)
    print(f"[alert] {text}", flush=True)
    return {"channel": "stdout", "text": text}
