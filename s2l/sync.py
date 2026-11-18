"""Write-back to the bank's systems of record. Runs off the critical path in a background thread.

Salesforce is the source of truth for the lead and the activity timeline. HubSpot gets the same speed-to-lead
properties so Marketing can report on them without a Salesforce login. Slack is handled in alert.py.
Without credentials every call is a dry run, recorded as an event so the demo shows exactly what would be written.
"""
import os
import threading
import time

from . import hubspot, sfdc, store


def status():
    return {
        "salesforce": "live" if sfdc.configured() else "dry-run",
        "hubspot": "live" if hubspot.configured() else "dry-run",
        "slack": "live" if os.environ.get("SLACK_WEBHOOK_URL") else "stdout",
        "email": "smtp" if os.environ.get("SMTP_HOST") else "outbox",
        "sms": "twilio" if os.environ.get("TWILIO_ACCOUNT_SID") else "outbox",
        "calendly": "webhook in" if True else "",
        "intercom": "webhook in",
    }


def _bg(fn, *a):
    t = threading.Thread(target=fn, args=a, daemon=True)
    t.start()
    return t


def _record(lead_id, system, action, payload, ident=None, err=None):
    if err:
        store.add_event(lead_id, "sync_error", f"{system}: {err}")
        return
    short = ", ".join(f"{k}={v}" for k, v in list(payload.items())[:6])
    store.add_event(lead_id, "sync", f"{system} {action}{' ' + str(ident) if ident else ''}: {short}")


def _push_lead(lead_id):
    lead = store.get_lead(lead_id)
    if not lead or lead.get("suppressed"):
        return
    try:
        sid, action, payload = sfdc.upsert_lead(lead)
        _record(lead_id, "salesforce", action, payload, sid)
        if sid and not lead.get("sfdc_id"):
            store.update_lead(lead_id, sfdc_id=sid)
    except Exception as e:
        _record(lead_id, "salesforce", "", {}, err=str(e))
    try:
        cid, action, payload = hubspot.upsert_contact(lead)
        _record(lead_id, "hubspot", action, payload, cid)
        if cid and not lead.get("hubspot_contact_id"):
            store.update_lead(lead_id, hubspot_contact_id=cid)
    except Exception as e:
        _record(lead_id, "hubspot", "", {}, err=str(e))


def _push_task(lead_id, subject, channel, at, outcome, done=True):
    lead = store.get_lead(lead_id)
    if not lead:
        return
    try:
        tid, action, payload = sfdc.log_task(lead, subject, channel, at, outcome, done=done)
        _record(lead_id, "salesforce task", action, payload, tid)
    except Exception as e:
        _record(lead_id, "salesforce task", "", {}, err=str(e))


# ---- hooks called by the pipeline --------------------------------------------

def on_lead_created(lead):
    _bg(_push_lead, lead["id"])


def on_touch(lead, channel, reached):
    outcome = "Connected" if reached else "No answer / voicemail"
    subj = {"call": "S2L first call", "email": "S2L first email", "sms": "S2L first SMS"}.get(channel, "S2L first touch")
    _bg(_push_task, lead["id"], f"{subj} ({outcome})", channel, lead.get("human_touch_at") or time.time(), outcome)
    _bg(_push_lead, lead["id"])


def on_stage(lead):
    _bg(_push_lead, lead["id"])


def _push_account(lead_id):
    lead = store.get_lead(lead_id)
    if not lead:
        return
    try:
        aid, action, payload = sfdc.update_account(lead)
        _record(lead_id, "salesforce account", action, payload, aid)
    except Exception as e:
        _record(lead_id, "salesforce account", "", {}, err=str(e))
    if lead.get("reactivation_tier") == "call":
        _push_task(lead_id, "S2L reactivation call", "call", lead.get("received_at") or time.time(), "Scheduled by weekly reactivation batch", done=False)


def on_reactivation(lead):
    _bg(_push_account, lead["id"])


def on_meeting(lead):
    _bg(_push_task, lead["id"], "S2L meeting booked", "email", time.time(), "Meeting booked via Calendly")
    _bg(_push_lead, lead["id"])
