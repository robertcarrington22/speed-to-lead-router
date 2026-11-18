"""The SLA clock and the weekly metrics.

Definitions (state these in the interview):
  - Touch zero:   automated email/SMS. Target: inside 60 seconds of receipt. Should be ~100%.
  - First touch:  a human call, personal email, or SMS logged against the lead. Target: inside 5 minutes
                  of the SLA start. This is THE number.
  - SLA start:    receipt time during business hours; the next business-hours open otherwise.
  - Escalation:   untouched at 3 minutes -> re-page owner and page backup.
  - Miss:         untouched at 5 minutes -> logged with a reason code. Misses are never deleted.
  - Exempt:       suppressed leads (disqualified) and Tier C leads do not carry the 5-minute human SLA.
Set S2L_SPEED=10 to run the clock ten times faster for demos.
"""
import os
import statistics
import threading
import time

from . import alert, route, store

SPEED = float(os.environ.get("S2L_SPEED", "1"))
_timers = {}
_lock = threading.Lock()


def _scaled(seconds):
    return seconds / SPEED


def human_sla_applies(lead):
    return not lead.get("suppressed") and lead.get("owner_kind") in ("rep", "closer")


def elapsed(lead):
    """Seconds from SLA start to the human touch. An after-hours lead touched before the clock starts counts as 0."""
    if not lead.get("human_touch_at"):
        return None
    return max(0.0, lead["human_touch_at"] - lead["sla_start_at"])


def schedule(lead):
    """Start escalation and miss timers for a lead. Idempotent per lead id."""
    if not human_sla_applies(lead):
        return
    s = route.cfg()["sla_seconds"]
    delay_to_start = max(0.0, lead["sla_start_at"] - time.time())
    t_esc = threading.Timer(_scaled(delay_to_start + s["escalate"]), _escalate, args=(lead["id"],))
    t_miss = threading.Timer(_scaled(delay_to_start + s["human_touch"]), _miss, args=(lead["id"],))
    t_esc.daemon = t_miss.daemon = True
    with _lock:
        _timers[lead["id"]] = (t_esc, t_miss)
    t_esc.start()
    t_miss.start()


def cancel(lead_id):
    with _lock:
        pair = _timers.pop(lead_id, None)
    if pair:
        for t in pair:
            t.cancel()


def _escalate(lead_id):
    lead = store.get_lead(lead_id)
    if not lead or lead.get("human_touch_at"):
        return
    b = route.backup()
    store.update_lead(lead_id, escalated_at=time.time())
    store.add_event(lead_id, "escalated", f"paged {lead.get('owner')} again and backup {b['name']}")
    alert.send(lead, "escalate", mention=f"{lead.get('owner_slack')} {b['slack']}")


def _miss(lead_id):
    lead = store.get_lead(lead_id)
    if not lead or lead.get("human_touch_at"):
        return
    reason = "no_human_touch_5min"
    store.update_lead(lead_id, sla_result="miss", miss_reason=reason)
    store.add_event(lead_id, "sla_miss", reason)
    alert.send(lead, "miss", mention=lead.get("owner_slack"))


def record_human_touch(lead_id, by, channel, at=None, reached=True):
    """Called when a human calls, emails, or texts. Stops the clock and grades the lead.

    `reached` is whether a conversation happened. A call attempt that went to voicemail still stops the
    SLA clock (the human acted) but starts the no-contact follow-up sequence.
    """
    lead = store.get_lead(lead_id)
    if not lead:
        return None
    at = at or time.time()
    cancel(lead_id)
    fields = dict(human_touch_at=at, human_touch_by=by, human_touch_channel=channel, contact_reached=bool(reached))
    if lead.get("stage_pipeline") == "lead":
        fields["stage_pipeline"] = "contacted"
        fields["stage_times"] = dict(lead.get("stage_times") or {}, contacted=at)
    if human_sla_applies(lead) and not lead.get("sla_result") in ("miss",):
        secs = max(0.0, at - lead["sla_start_at"])
        fields["sla_result"] = "hit" if secs <= route.cfg()["sla_seconds"]["human_touch"] else "miss"
        if fields["sla_result"] == "miss":
            fields["miss_reason"] = "late_human_touch"
    store.add_event(lead_id, "human_touch", f"{by} via {channel}{'' if reached else ' (no answer)'}", at=at)
    updated = store.update_lead(lead_id, **fields)
    if at >= time.time() - 120:  # live touch, not a backfill: log the Task in Salesforce
        from . import sync
        sync.on_touch(updated, channel, reached)
    return updated


# ---- metrics ----------------------------------------------------------------

def _pct(values, p):
    if not values:
        return None
    vals = sorted(values)
    k = (len(vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _summ(secs):
    return dict(n=len(secs), median=statistics.median(secs) if secs else None, p90=_pct(secs, 0.9))


def metrics(leads):
    """Everything the Friday readout needs, computed from the lead list."""
    eligible = [l for l in leads if human_sla_applies(l)]
    graded = [l for l in eligible if l.get("sla_result") in ("hit", "miss")]
    hits = [l for l in graded if l["sla_result"] == "hit"]
    misses = [l for l in graded if l["sla_result"] == "miss"]
    human_secs = [elapsed(l) for l in eligible if l.get("human_touch_at")]
    auto_secs = [l["auto_touch_at"] - l["received_at"] for l in leads if l.get("auto_touch_at")]

    by_source, by_hour, by_tier = {}, {}, {}
    for l in leads:
        s = by_source.setdefault(l["source"], dict(n=0, eligible=0, hits=0, misses=0, secs=[], funded=0, meetings=0))
        s["n"] += 1
        if l.get("funded_at"):
            s["funded"] += 1
        if l.get("meeting_at"):
            s["meetings"] += 1
        if human_sla_applies(l):
            s["eligible"] += 1
            if l.get("sla_result") == "hit":
                s["hits"] += 1
            elif l.get("sla_result") == "miss":
                s["misses"] += 1
            if l.get("human_touch_at"):
                s["secs"].append(elapsed(l))
        t = by_tier.setdefault(l.get("tier") or "?", 0)
        by_tier[l.get("tier") or "?"] = t + 1
        if human_sla_applies(l) and l.get("human_touch_at"):
            h = route.local(l["received_at"]).hour
            by_hour.setdefault(h, []).append(elapsed(l))

    for s in by_source.values():
        s.update(_summ(s.pop("secs")))
        g = s["hits"] + s["misses"]
        s["hit_rate"] = (s["hits"] / g) if g else None

    funnel = {}
    for l in leads:
        st = l.get("stage_pipeline") or "lead"
        funnel[st] = funnel.get(st, 0) + 1

    from . import followup
    return dict(
        followup=followup.summary(leads, min([l["received_at"] for l in leads] or [time.time()]), time.time()),
        total=len(leads),
        suppressed=sum(1 for l in leads if l.get("suppressed")),
        after_hours=sum(1 for l in leads if l.get("after_hours")),
        eligible=len(eligible),
        graded=len(graded),
        hits=len(hits),
        misses=len(misses),
        pending=len([l for l in eligible if l.get("sla_result") == "pending"]),
        hit_rate=(len(hits) / len(graded)) if graded else None,
        human=_summ(human_secs),
        auto=_summ(auto_secs),
        auto_within_60=(sum(1 for s in auto_secs if s <= 60) / len(auto_secs)) if auto_secs else None,
        by_source=by_source,
        by_hour={h: _summ(v) for h, v in sorted(by_hour.items())},
        by_tier=by_tier,
        funnel=funnel,
        miss_list=[dict(id=l["id"], source=l["source"], tier=l.get("tier"), company=l.get("company"),
                        reason=l.get("miss_reason"), owner=l.get("owner"),
                        secs=(elapsed(l)) if l.get("human_touch_at") else None,
                        received_at=l["received_at"]) for l in misses],
        funded=sum(1 for l in leads if l.get("funded_at")),
        funded_amount=sum(l.get("funded_amount") or 0 for l in leads),
        meetings=sum(1 for l in leads if l.get("meeting_at")),
    )
