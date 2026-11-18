"""Per-source adapters. Every inbound channel lands on one webhook and becomes one Lead shape.

Add a source by adding a function here and a template in touch.py. Nothing else changes.
"""
import time

FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "proton.me", "protonmail.com"}

BASE = {
    "name": None, "email": None, "company": None, "domain": None, "phone": None,
    "entity_type": "unknown", "country": "US", "industry": "unknown",
    "stage": "unknown", "balance_band": "unknown", "intent": "unknown",
    "current_provider": "unknown", "multi_client": False, "referral": False,
    "book_call_requested": False, "notes": "",
}


def _domain(email):
    if not email or "@" not in email:
        return None
    d = email.split("@", 1)[1].lower().strip()
    return None if d in FREE_MAIL else d


def _balance_band(amount):
    if amount is None:
        return "unknown"
    try:
        a = float(amount)
    except (TypeError, ValueError):
        return "unknown"
    if a < 25_000:
        return "lt_25k"
    if a < 100_000:
        return "25k_100k"
    if a < 500_000:
        return "100k_500k"
    return "500k_plus"


# ---- adapters ---------------------------------------------------------------

def web_form(p):
    """Website 'Request demo' or 'Open account' form."""
    intent = "demo" if (p.get("form") or "").lower().startswith("demo") else "open_account"
    return dict(
        name=p.get("name"), email=p.get("email"), company=p.get("company"), phone=p.get("phone"),
        intent=intent, stage=p.get("stage", "unknown"), balance_band=_balance_band(p.get("cash_on_hand")),
        current_provider=(p.get("current_bank") or "unknown").lower(), notes=p.get("message", ""),
        industry=p.get("industry", "unknown"), entity_type=p.get("entity_type", "unknown"),
    )


def stripe_atlas(p):
    """Partner webhook when a founder selects Acme during Atlas incorporation. Pre-EIN by definition."""
    return dict(
        name=p.get("founder_name"), email=p.get("founder_email"), company=p.get("company_name"),
        entity_type=(p.get("entity_type") or "c_corp").lower(), stage="pre_ein", intent="open_account",
        balance_band=_balance_band(p.get("expected_raise")), current_provider="none",
        notes=f"Atlas status: {p.get('incorporation_status', 'in_progress')}",
    )


def brex_switcher(p):
    """Landing page form behind the $1,000 switching credit. The offer requires a call, so book_call is true."""
    return dict(
        name=p.get("name"), email=p.get("email"), company=p.get("company"), phone=p.get("phone"),
        intent="switch", current_provider="brex", balance_band=_balance_band(p.get("balance")),
        stage=p.get("stage", "seed"), book_call_requested=True, notes=p.get("message", ""),
    )


def mercury_switcher(p):
    """'Mercury alternative' comparison page or switch form. Usually seed-stage with idle cash, came for yield or a no-PG card.

    Unlike the Brex page there is no offer that requires a call, so book_call is only true if they asked.
    """
    reason = (p.get("reason") or "").lower()
    intent = "treasury" if ("yield" in reason or "treasury" in reason) else ("card" if "card" in reason or "guarantee" in reason else "switch")
    return dict(
        name=p.get("name"), email=p.get("email"), company=p.get("company"), phone=p.get("phone"),
        intent=intent, current_provider="mercury", balance_band=_balance_band(p.get("balance")),
        stage=p.get("stage", "seed"), book_call_requested=bool(p.get("book_call")), notes=p.get("reason", ""),
    )


def application_abandoned(p):
    """Product event: started the application, stalled at a KYB step, no activity for N minutes."""
    return dict(
        name=p.get("name"), email=p.get("email"), company=p.get("company"), phone=p.get("phone"),
        intent="application_abandoned", entity_type=p.get("entity_type", "unknown"),
        notes=f"Stalled at step: {p.get('step', 'unknown')}",
    )


def opened_not_funded(p):
    """Product event: account approved N days ago, balance still zero. Warm by definition: they already said yes."""
    return dict(
        name=p.get("name"), email=p.get("email"), company=p.get("company"), phone=p.get("phone"),
        intent="opened_not_funded", stage=p.get("stage", "unknown"),
        balance_band=_balance_band(p.get("expected_deposit")),
        notes=f"Opened {p.get('days_since_open', '?')} days ago, balance $0",
    )


def referral(p):
    return dict(
        name=p.get("name"), email=p.get("email"), company=p.get("company"), phone=p.get("phone"),
        intent="open_account", referral=True, notes=f"Referred by {p.get('referrer_email', 'unknown')}",
    )


def accountant(p):
    """Accountant partner form. One accountant brings many clients, so multi_client is true."""
    return dict(
        name=p.get("name"), email=p.get("email"), company=p.get("firm"), phone=p.get("phone"),
        intent="demo", multi_client=True, stage="revenue_smb",
        notes=f"Firm with ~{p.get('clients_count', '?')} clients",
    )


def chat(p):
    """Intercom/Drift transcript with sales intent detected."""
    text = (p.get("transcript") or "").lower()
    intent = "treasury" if "treasury" in text or "yield" in text else ("card" if "card" in text else "demo")
    return dict(name=p.get("name"), email=p.get("email"), company=p.get("company"), intent=intent, notes=text[:200])


def event(p):
    """Founder dinner, Atlanta week, billboard-photo promo entries."""
    return dict(
        name=p.get("name"), email=p.get("email"), company=p.get("company"), phone=p.get("phone"),
        intent="content", stage=p.get("stage", "unknown"), notes=f"Event: {p.get('event', 'unknown')}",
    )


def inbound_phone(p):
    """Missed call or voicemail on the sales line."""
    return dict(
        name=p.get("caller_name"), phone=p.get("phone"), email=p.get("email"), intent="demo",
        notes=f"Voicemail: {(p.get('voicemail_transcript') or '')[:200]}",
    )


def free_tool(p):
    """SAFE generator, invoice generator, perks marketplace signups."""
    return dict(
        name=p.get("name"), email=p.get("email"), company=p.get("company"), intent="free_tool",
        stage=p.get("stage", "pre_seed"), notes=f"Tool: {p.get('tool', 'unknown')}",
    )


# ---- CRM and marketing systems ------------------------------------------------------
# These parse the payload shapes HubSpot, Salesforce, Intercom, and Calendly actually send.

_FIELDMAP = None


def fieldmap():
    global _FIELDMAP
    if _FIELDMAP is None:
        import json, os
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "fieldmap.json"), encoding="utf-8") as f:
            _FIELDMAP = json.load(f)
    return _FIELDMAP


def _apply_conversion_page(out, page):
    """Signup Conversion Page tells us which landing page they came from; infer provider, intent, vertical."""
    if not page:
        return out
    p = page.lower()
    for prefix, hints in fieldmap()["conversion_page_hints"].items():
        if prefix.startswith("_"):
            continue
        if p.startswith(prefix) or prefix in p:
            for k, v in hints.items():
                if out.get(k) in (None, "unknown", False):
                    out[k] = v
            break
    out["conversion_page"] = page
    return out


def _hs_value(props, key):
    """HubSpot workflow webhooks send either {"key": "value"} or {"key": {"value": "..."}} depending on version."""
    v = props.get(key)
    if isinstance(v, dict):
        v = v.get("value")
    return v


def hubspot_form(p):
    """HubSpot workflow 'Send webhook' payload for a contact, or a raw form submission.

    Accepts {"properties": {...}} (workflow webhook, v3 or legacy) or a flat dict of form field names.
    Reads Acme's hidden fields: Inbound Type, Lead source, Signup Conversion Page, Salesforce Inclusion, utm_*.
    """
    hs = fieldmap()["hubspot"]
    props = p.get("properties") if isinstance(p.get("properties"), dict) else p
    get = lambda k: _hs_value(props, k)
    std = hs["standard"]
    first, last = get(std["first"]) or "", get(std["last"]) or ""
    inbound_type = get(hs["inbound_type"])
    lead_source = get(hs["lead_source"])
    intent_by_type = {"Bottom Funnel - Warm": "demo", "Hand Raiser": "demo", "Contact Sales": "demo",
                      "Self-Serve Signup": "open_account", "Partner Referral": "open_account", "Marketing Lead": "content"}
    out = dict(
        name=(f"{first} {last}").strip() or None, email=get(std["email"]), company=get(std["company"]),
        phone=get(std["phone"]), inbound_type=inbound_type, lead_source=lead_source,
        intent=intent_by_type.get(inbound_type, "unknown"),
        referral=(inbound_type == "Partner Referral") or (lead_source in ("Referral", "Partner Inbound", "Partner")),
        hubspot_contact_id=str(p.get("objectId") or p.get("vid") or get("hs_object_id") or "") or None,
        salesforce_inclusion=get(hs["salesforce_inclusion"]),
        utm={k: get(k) for k in hs["utm"] if get(k)},
        notes=f"HubSpot form · {inbound_type or 'no inbound type'} · {lead_source or 'no lead source'}",
    )
    if get("cash_on_hand"):
        out["balance_band"] = _balance_band(get("cash_on_hand"))
    if get("current_bank"):
        out["current_provider"] = str(get("current_bank")).lower()
    return _apply_conversion_page(out, get(hs["conversion_page"]))


def salesforce_lead(p):
    """A Salesforce Lead, from an Outbound Message (parsed to a flat dict by server.py) or a Flow HTTP callout (JSON)."""
    sf = fieldmap()["salesforce"]
    std, cin = sf["standard"], sf["custom_in"]
    g = lambda k: p.get(k)
    first, last = g(std["first"]) or "", g(std["last"]) or ""
    inbound_type = g(cin["inbound_type"])
    lead_source = g(std["lead_source"])
    out = dict(
        name=(f"{first} {last}").strip() or None, email=g(std["email"]), company=g(std["company"]), phone=g(std["phone"]),
        inbound_type=inbound_type, lead_source=lead_source, sfdc_id=g(std["id"]),
        intent={"Bottom Funnel - Warm": "demo", "Hand Raiser": "demo", "Contact Sales": "demo",
                "Self-Serve Signup": "open_account", "Partner Referral": "open_account"}.get(inbound_type, "unknown"),
        referral=(inbound_type == "Partner Referral") or (lead_source in ("Referral", "Partner Inbound")),
        notes=f"Salesforce Lead {g(std['id']) or ''} · status {g(std['status']) or '?'}",
    )
    return _apply_conversion_page(out, g(cin["conversion_page"]))


def intercom(p):
    """Intercom webhook: topics conversation.user.created / conversation.user.replied.

    Payload: {"topic": ..., "data": {"item": {"source": {"body": "<p>...</p>", "author": {"email":..., "name":...}},
              "contacts": {"contacts": [{"email":..., "name":...}]}, "id": ...}}}
    """
    item = ((p.get("data") or {}).get("item")) or p
    src = item.get("source") or {}
    author = src.get("author") or {}
    contacts = ((item.get("contacts") or {}).get("contacts")) or []
    c0 = contacts[0] if contacts else {}
    import re as _re
    body = _re.sub(r"<[^>]+>", " ", src.get("body") or item.get("body") or "").strip()
    text = body.lower()
    intent = "treasury" if ("treasury" in text or "yield" in text) else ("card" if ("card" in text or "guarantee" in text) else "demo")
    return dict(
        name=author.get("name") or c0.get("name"), email=author.get("email") or c0.get("email"),
        company=(c0.get("companies") or {}).get("companies", [{}])[0].get("name") if c0.get("companies") else None,
        intent=intent, lead_source="Intercom", inbound_type="Hand Raiser",
        intercom_conversation_id=str(item.get("id") or ""), notes=f"Intercom: {body[:200]}",
    )


def calendly(p):
    """Calendly webhook: invitee.created. A booking is both a lead and a meeting.

    Payload: {"event": "invitee.created", "payload": {"email":..., "name":..., "scheduled_event": {"start_time": ISO8601},
              "questions_and_answers": [{"question":..., "answer":...}], "tracking": {"utm_source":...}}}
    """
    from datetime import datetime, timezone
    pl = p.get("payload") or p
    qa = {q.get("question", "").lower(): q.get("answer") for q in (pl.get("questions_and_answers") or [])}
    company = next((v for k, v in qa.items() if "company" in k), None)
    phone = next((v for k, v in qa.items() if "phone" in k), None) or (pl.get("text_reminder_number"))
    start = ((pl.get("scheduled_event") or {}).get("start_time"))
    meeting_at = None
    if start:
        try:
            meeting_at = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
        except ValueError:
            meeting_at = None
    return dict(
        name=pl.get("name"), email=pl.get("email"), company=company, phone=phone,
        intent="demo", lead_source="Calendly", inbound_type="Bottom Funnel - Warm", book_call_requested=True,
        meeting_at=meeting_at, calendly_uri=pl.get("uri"), notes=f"Calendly booking for {start or '?'}",
    )


ADAPTERS = {
    "hubspot_form": hubspot_form,
    "salesforce_lead": salesforce_lead,
    "intercom": intercom,
    "calendly": calendly,
    "web_form": web_form,
    "stripe_atlas": stripe_atlas,
    "brex_switcher": brex_switcher,
    "mercury_switcher": mercury_switcher,
    "application_abandoned": application_abandoned,
    "opened_not_funded": opened_not_funded,
    "referral": referral,
    "accountant": accountant,
    "chat": chat,
    "event": event,
    "inbound_phone": inbound_phone,
    "free_tool": free_tool,
}


def normalize(source, payload, received_at=None):
    fn = ADAPTERS.get(source)
    if fn is None:
        raise ValueError(f"unknown source '{source}'. Known: {', '.join(ADAPTERS)}")
    lead = dict(BASE, inbound_type=None, lead_source=None, conversion_page=None, sfdc_id=None, hubspot_contact_id=None)
    lead.update({k: v for k, v in fn(payload).items() if v is not None})
    lead["source"] = source
    lead["received_at"] = received_at or time.time()
    if lead.get("email"):
        lead["email"] = lead["email"].lower().strip()
    lead["domain"] = lead.get("domain") or _domain(lead.get("email"))
    lead["raw"] = payload
    return lead
