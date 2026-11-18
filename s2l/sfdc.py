"""Salesforce REST client, stdlib only.

Auth, in order of preference:
  1. SFDC_ACCESS_TOKEN + SFDC_INSTANCE_URL        (a session token, for testing)
  2. SFDC_CLIENT_ID + SFDC_CLIENT_SECRET + SFDC_LOGIN_URL   (OAuth 2.0 client-credentials flow; the connected app
     must have that flow enabled and a "run as" user set)
If neither is present, every call is a dry run: it returns what it WOULD have sent and the pipeline logs it.
"""
import json
import os
import time
import urllib.parse
import urllib.request

from . import normalize

_token = {"access_token": None, "instance_url": None, "expires_at": 0}


def configured():
    return bool(os.environ.get("SFDC_ACCESS_TOKEN") and os.environ.get("SFDC_INSTANCE_URL")) or \
        bool(os.environ.get("SFDC_CLIENT_ID") and os.environ.get("SFDC_CLIENT_SECRET"))


def _auth():
    if os.environ.get("SFDC_ACCESS_TOKEN"):
        return os.environ["SFDC_ACCESS_TOKEN"], os.environ["SFDC_INSTANCE_URL"].rstrip("/")
    if _token["access_token"] and time.time() < _token["expires_at"]:
        return _token["access_token"], _token["instance_url"]
    login = os.environ.get("SFDC_LOGIN_URL", "https://login.salesforce.com").rstrip("/")
    data = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": os.environ["SFDC_CLIENT_ID"],
                                   "client_secret": os.environ["SFDC_CLIENT_SECRET"]}).encode()
    req = urllib.request.Request(f"{login}/services/oauth2/token", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.loads(r.read())
    _token.update(access_token=body["access_token"], instance_url=body["instance_url"].rstrip("/"), expires_at=time.time() + 50 * 60)
    return _token["access_token"], _token["instance_url"]


def _call(method, path, body=None, params=None):
    token, base = _auth()
    ver = normalize.fieldmap()["salesforce"]["api_version"]
    url = f"{base}/services/data/{ver}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def query(soql):
    return _call("GET", "/query", params={"q": soql})


def find_lead_by_email(email):
    if not email:
        return None
    email_esc = email.replace("'", "\\'")
    res = query(f"SELECT Id, Email, OwnerId, Status FROM Lead WHERE Email = '{email_esc}' AND IsConverted = false ORDER BY CreatedDate DESC LIMIT 1")
    recs = res.get("records") or []
    return recs[0] if recs else None


def _lead_fields(lead):
    """Map our lead to Salesforce field names from config/fieldmap.json."""
    sf = normalize.fieldmap()["salesforce"]
    std, wb = sf["standard"], sf["writeback"]
    name = (lead.get("name") or "").strip()
    first, last = (name.split(" ", 1) + [""])[:2] if name else ("", "")
    out = {
        std["email"]: lead.get("email"), std["first"]: first or None, std["last"]: last or (first or "Unknown"),
        std["company"]: lead.get("company") or "Unknown", std["phone"]: lead.get("phone"),
        wb["score"]: lead.get("score"), wb["tier"]: lead.get("tier"), wb["sla_result"]: lead.get("sla_result"),
    }
    if lead.get("lead_source"):
        out[std["lead_source"]] = lead["lead_source"]
    if lead.get("inbound_type"):
        out[sf["custom_in"]["inbound_type"]] = lead["inbound_type"]
    if lead.get("auto_touch_at"):
        out[wb["auto_touch_at"]] = _iso(lead["auto_touch_at"])
    if lead.get("human_touch_at"):
        out[wb["first_touch_at"]] = _iso(lead["human_touch_at"])
        out[wb["seconds_to_first_touch"]] = int(lead["human_touch_at"] - lead["sla_start_at"])
    if lead.get("miss_reason"):
        out[wb["miss_reason"]] = lead["miss_reason"]
    return {k: v for k, v in out.items() if v is not None}


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000+0000", time.gmtime(ts))


def upsert_lead(lead):
    """Update the Lead if we know its Id or can find it by email; otherwise create it. Returns (sfdc_id, action, payload)."""
    obj = normalize.fieldmap()["salesforce"]["object"]
    fields = _lead_fields(lead)
    if not configured():
        return None, "dry-run", fields
    sfdc_id = lead.get("sfdc_id")
    if not sfdc_id:
        found = find_lead_by_email(lead.get("email"))
        sfdc_id = found["Id"] if found else None
    if sfdc_id:
        fields.pop("Email", None)  # never overwrite the CRM's email
        _call("PATCH", f"/sobjects/{obj}/{sfdc_id}", body=fields)
        return sfdc_id, "updated", fields
    res = _call("POST", f"/sobjects/{obj}", body=fields)
    return res.get("id"), "created", fields


def update_account(lead):
    """Reactivation write-back on the Account: segment, score, last contacted. Field names are proposals."""
    fields = {"Reactivation_Segment__c": lead.get("reactivation_segment"), "Reactivation_Score__c": lead.get("score"),
              "Reactivation_Trigger__c": lead.get("reactivation_trigger"), "Reactivation_Last_Contacted__c": _iso(lead.get("received_at") or time.time()),
              "Reactivation_Batch__c": lead.get("reactivation_batch")}
    fields = {k: v for k, v in fields.items() if v is not None}
    if not configured() or not lead.get("sfdc_account_id"):
        return lead.get("sfdc_account_id"), "dry-run", fields
    _call("PATCH", f"/sobjects/Account/{lead['sfdc_account_id']}", body=fields)
    return lead["sfdc_account_id"], "updated", fields


def log_task(lead, subject, channel, at, outcome="", done=True):
    """A Task on the Lead or Account: completed for a touch that happened, open ('Not Started') for a to-do."""
    sf = normalize.fieldmap()["salesforce"]
    task = {
        "Subject": subject, "Status": sf["task"]["status_done"] if done else "Not Started",
        "Type": sf["task"]["type_call"] if channel in ("call", "sms") else sf["task"]["type_email"],
        "ActivityDate": time.strftime("%Y-%m-%d", time.gmtime(at)), "Description": outcome,
    }
    if lead.get("sfdc_id"):
        task["WhoId"] = lead["sfdc_id"]
    if lead.get("sfdc_account_id"):
        task["WhatId"] = lead["sfdc_account_id"]  # reactivation: the Task hangs off the Account
    if not configured():
        return None, "dry-run", task
    res = _call("POST", "/sobjects/Task", body=task)
    return res.get("id"), "created", task
