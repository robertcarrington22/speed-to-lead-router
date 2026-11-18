"""The orchestrator. One inbound payload in, one routed, touched, alerted, clocked lead out.

    normalize -> dedupe -> enrich (30s budget) -> score -> route -> persist -> touch one -> alert -> start SLA clock
"""
import time

from . import alert, enrich, normalize, route, score, sla, store, sync, touch

ENRICH_BUDGET_SECONDS = 30.0


def process(source, payload, received_at=None, live=True):
    lead = normalize.normalize(source, payload, received_at=received_at)
    received_at = lead["received_at"]

    # Dedupe: same email inside 24h is a second signal on the same lead, not a new clock.
    existing = store.find_recent_by_email(lead.get("email"))
    if existing:
        store.add_event(existing["id"], "duplicate_signal", f"{source}: {lead.get('intent')}")
        fields = dict(notes=(existing.get("notes") or "") + f" | +{source}", duplicate_count=(existing.get("duplicate_count") or 0) + 1)
        # A Calendly booking on an existing lead is a meeting, not a new lead.
        if lead.get("meeting_at") and not existing.get("meeting_at"):
            fields.update(meeting_at=lead["meeting_at"], stage_pipeline="qualified",
                          stage_times=dict(existing.get("stage_times") or {}, qualified=received_at))
            store.add_event(existing["id"], "meeting_booked", "via Calendly")
            updated = store.update_lead(existing["id"], **fields)
            if live:
                sync.on_meeting(updated)
            return updated
        # A Salesforce or HubSpot id arriving later gets attached so write-back targets the right record.
        for k in ("sfdc_id", "hubspot_contact_id", "inbound_type", "lead_source", "conversion_page"):
            if lead.get(k) and not existing.get(k):
                fields[k] = lead[k]
        return store.update_lead(existing["id"], **fields)

    lead["id"] = store.new_id()
    lead["stage_pipeline"] = "qualified" if lead.get("meeting_at") else "lead"
    lead["stage_times"] = {"qualified": received_at} if lead.get("meeting_at") else {}
    lead["sla_result"] = "pending"

    # Enrichment has a hard budget. It is allowed to fail; it is not allowed to be late.
    budget = ENRICH_BUDGET_SECONDS / sla.SPEED if live else 0.5
    lead.update(enrich.enrich(lead, timeout_seconds=budget))

    lead.update(score.score(lead))
    lead["after_hours"] = not route.is_business_hours(received_at)
    lead["sla_start_at"] = route.next_open(received_at)
    lead.update(route.assign(lead, score.needs_closer(lead)))
    if lead.get("suppressed"):
        lead["sla_result"] = "exempt"
    elif lead["owner_kind"] == "rep_light":
        lead["sla_result"] = "exempt"

    store.insert_lead(lead)
    store.add_event(lead["id"], "received", f"{source} -> tier {lead['tier']} score {lead['score']} owner {lead['owner']}")

    if lead.get("suppressed"):
        store.add_event(lead["id"], "suppressed", lead.get("suppress_reason") or "")
        return lead

    # Touch zero: automated, source-specific, inside 60 seconds.
    msg = touch.send_touch_one(lead, route.cfg()["booking_link"], live=live)
    auto_at = time.time() if live else received_at + 8 + (hash(lead["id"]) % 40)
    lead = store.update_lead(lead["id"], auto_touch_at=auto_at, touch_one=msg)
    store.add_event(lead["id"], "auto_touch", ", ".join(f"{c} via {msg['via'].get(c) or 'outbox'}" for c in msg["channels"]), at=auto_at)

    # Alert the human with a click-to-dial brief.
    kind = "after_hours" if lead["after_hours"] else "new"
    if live:
        a = alert.send(lead, kind, mention=lead.get("owner_slack"))
        store.add_event(lead["id"], "alerted", f"{a['channel']}: {lead.get('owner')}")
        sla.schedule(lead)
        sync.on_lead_created(lead)   # Salesforce + HubSpot write-back, off the critical path
    else:
        store.add_event(lead["id"], "alerted", f"stdout: {lead.get('owner')}", at=auto_at + 1)
    return lead
