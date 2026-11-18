"""HubSpot CRM v3 client, stdlib only. Needs a private app token with crm.objects.contacts read/write.

HUBSPOT_PRIVATE_APP_TOKEN unset -> dry run.
"""
import json
import os
import time
import urllib.request

from . import normalize

BASE = "https://api.hubapi.com"


def configured():
    return bool(os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN"))


def _call(method, path, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Authorization": f"Bearer {os.environ['HUBSPOT_PRIVATE_APP_TOKEN']}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def find_contact(email):
    res = _call("POST", "/crm/v3/objects/contacts/search", {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        "properties": ["email", "firstname", "lastname"], "limit": 1})
    results = res.get("results") or []
    return results[0] if results else None


def _props(lead):
    wb = normalize.fieldmap()["hubspot"]["writeback"]
    out = {wb["score"]: lead.get("score"), wb["tier"]: lead.get("tier"), wb["owner"]: lead.get("owner"),
           wb["sla_result"]: lead.get("sla_result")}
    if lead.get("auto_touch_at"):
        out[wb["auto_touch_at"]] = int(lead["auto_touch_at"] * 1000)  # HubSpot datetime = ms epoch
    if lead.get("human_touch_at"):
        out[wb["first_touch_at"]] = int(lead["human_touch_at"] * 1000)
        out[wb["seconds_to_first_touch"]] = int(lead["human_touch_at"] - lead["sla_start_at"])
    return {k: (str(v) if v is not None else None) for k, v in out.items() if v is not None}


def upsert_contact(lead):
    """Write the speed-to-lead properties onto the contact. Creates the contact only if it does not exist."""
    props = _props(lead)
    if not configured():
        return None, "dry-run", props
    cid = lead.get("hubspot_contact_id")
    if not cid and lead.get("email"):
        found = find_contact(lead["email"])
        cid = found["id"] if found else None
    if cid:
        _call("PATCH", f"/crm/v3/objects/contacts/{cid}", {"properties": props})
        return cid, "updated", props
    create = dict(props, email=lead.get("email"))
    name = (lead.get("name") or "").split(" ", 1)
    if name and name[0]:
        create["firstname"] = name[0]
    if len(name) > 1:
        create["lastname"] = name[1]
    if lead.get("company"):
        create["company"] = lead["company"]
    res = _call("POST", "/crm/v3/objects/contacts", {"properties": create})
    return res.get("id"), "created", create


def ensure_properties():
    """One-time: create the s2l_* contact properties if they do not exist. Safe to call repeatedly."""
    wb = normalize.fieldmap()["hubspot"]["writeback"]
    specs = {
        wb["score"]: ("number", "number"), wb["tier"]: ("string", "text"), wb["owner"]: ("string", "text"),
        wb["sla_result"]: ("string", "text"), wb["first_touch_at"]: ("datetime", "date"),
        wb["auto_touch_at"]: ("datetime", "date"), wb["seconds_to_first_touch"]: ("number", "number"),
    }
    if not configured():
        return "dry-run", list(specs)
    made = []
    for name, (typ, field) in specs.items():
        try:
            _call("POST", "/crm/v3/properties/contacts", {"name": name, "label": name.replace("_", " ").title(),
                                                           "type": typ, "fieldType": field, "groupName": "contactinformation"})
            made.append(name)
        except Exception:
            pass  # already exists
    return "ok", made
