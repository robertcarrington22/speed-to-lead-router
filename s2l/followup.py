"""Follow-up automation: sequences triggered by the ABSENCE of the next event.

The first-touch system owns minutes zero to five. This owns everything after, and it is where most of the
funded-account number actually comes from. Each sequence is keyed on a pipeline state, starts from the
timestamp that put the lead in that state, and runs a few steps. Every step is skipped the moment a
stop condition is true: the lead replied, booked, advanced a stage, funded, or opted out.

Actions:
  email  -> rendered from a hand-written template, written to the outbox (swap for SendGrid/Postmark)
  sms    -> same, only if a phone is on file
  task   -> a to-do for the rep (surfaced as an alert with a click-to-dial link)
  ops    -> escalation to onboarding/compliance ops (an alert to the ops channel)

Delays are in seconds and scale with S2L_SPEED like everything else.
"""
import os
import time

from . import alert, route, store, touch

H, D = 3600, 86400

# (delay_from_trigger, action, template_key)
SEQUENCES = {
    # Human tried, did not reach them (or Tier C with no human touch yet). Goal: a live conversation or a booking.
    "no_contact": dict(
        trigger=lambda l: (l.get("human_touch_at") if l.get("human_touch_at") and not l.get("contact_reached") else None)
                          or (l["received_at"] if l.get("owner_kind") == "rep_light" and not l.get("human_touch_at") else None),
        stop=lambda l: l.get("replied_at") or l.get("meeting_at") or l.get("contact_reached") or _stage_at_least(l, "applied"),
        steps=[(2 * H, "email", "tried_you"), (1 * D, "email", "one_number"), (3 * D, "task", "call_again"),
               (3 * D + 300, "email", "second_call_note"), (7 * D, "email", "breakup")],
    ),
    # Reached them, no meeting booked and no application. Goal: the booking.
    "contacted_no_meeting": dict(
        trigger=lambda l: l.get("human_touch_at") if l.get("contact_reached") and not l.get("meeting_at") else None,
        stop=lambda l: l.get("meeting_at") or _stage_at_least(l, "applied") or l.get("replied_at"),
        steps=[(1 * D, "email", "book_reminder"), (4 * D, "sms", "still_open"), (4 * D + 60, "email", "still_open_email")],
    ),
    # Meeting on the calendar. Goal: they show up.
    "meeting_booked": dict(
        trigger=lambda l: l.get("meeting_at") - 1 * D if l.get("meeting_at") else None,
        stop=lambda l: _stage_at_least(l, "applied") or l.get("no_show_at"),
        steps=[(0, "email", "meeting_confirm"), (23 * H, "sms", "meeting_reminder")],
    ),
    # Applied, sitting in KYB. Goal: unblock the missing owner ID, then escalate to ops.
    "applied_pending": dict(
        trigger=lambda l: _stage_time(l, "applied") if _stage_is(l, "applied") else None,
        stop=lambda l: _stage_at_least(l, "approved"),
        steps=[(1 * D, "email", "what_is_missing"), (3 * D, "task", "call_kyb"), (5 * D, "ops", "kyb_stalled")],
    ),
    # Approved, balance zero. Goal: the first deposit. This is the funded-account sequence.
    "approved_not_funded": dict(
        trigger=lambda l: _stage_time(l, "approved") if _stage_is(l, "approved") and not l.get("funded_at") else None,
        stop=lambda l: l.get("funded_at"),
        steps=[(1 * H, "email", "ach_pull_offer"), (3 * D, "email", "wire_details"), (7 * D, "task", "call_to_fund"),
               (14 * D, "email", "bonus_deadline"), (45 * D, "email", "bonus_expires")],
    ),
    # Reactivation batch member with no reply. Goal: one conversation. Shorter and quieter than inbound.
    "reactivation": dict(
        trigger=lambda l: l.get("received_at") if l.get("source") == "reactivation" else None,
        stop=lambda l: l.get("replied_at") or l.get("meeting_at") or l.get("contact_reached") or l.get("funded_at") or _stage_at_least(l, "applied"),
        steps=[(2 * D, "task", "reactivation_call"), (5 * D, "email", "reactivation_two"), (14 * D, "email", "reactivation_breakup")],
    ),
    # Funded, not yet running on Acme. Goal: card swipe, treasury allocation, accounting sync.
    "funded_not_active": dict(
        trigger=lambda l: l.get("funded_at") if l.get("funded_at") and not _stage_is(l, "active") else None,
        stop=lambda l: _stage_is(l, "active"),
        steps=[(1 * D, "email", "first_swipe"), (5 * D, "email", "treasury_allocation"), (10 * D, "task", "activation_call")],
    ),
}

STAGES = ["lead", "contacted", "qualified", "applied", "approved", "funded", "active"]

TEMPLATES = {
    "tried_you": ("Tried you just now", "Hi {first}, Robert at Acme. Tried your line a minute ago. Reply with a time, or grab one here: {booking}"),
    "one_number": ("One number for {company}", "Hi {first}, the number I'd want if I were you: treasury is paying a bit over 4% net this week from $50K, and there's no fee to open. Ten minutes to set it up: {booking}"),
    "call_again": ("TASK: second call attempt", "Call {name} at {company}. Last attempt did not connect. {phone}"),
    "second_call_note": ("Left you a voicemail", "Hi {first}, left a voicemail just now. If banking isn't the priority this week, tell me when it is and I'll come back then. {booking}"),
    "breakup": ("Closing the loop", "Hi {first}, I'll stop here so I'm not noise in your inbox. When {company} wants a bank that grows from now through Series B, this link works anytime: {booking}"),
    "book_reminder": ("Picking up where we left off", "Hi {first}, good talking. Here's the link for the walkthrough we discussed: {booking}. If it's easier, reply with a day and I'll send the invite."),
    "still_open": ("", "Hi {first}, Robert at Acme. Still happy to do the 20-minute setup for {company} whenever works: {booking}"),
    "still_open_email": ("Still open on my side", "Hi {first}, no pressure. The account takes minutes and treasury can start the same day. {booking}"),
    "meeting_confirm": ("Confirmed: Acme walkthrough", "Hi {first}, confirmed for our call. Have your EIN and a rough cash number handy and we'll open the account live. Robert"),
    "meeting_reminder": ("", "Reminder: Acme call in about an hour. Reply here if you need to move it. Robert"),
    "what_is_missing": ("What's holding up your Acme application", "Hi {first}, your application is waiting on one thing, almost always an ID for anyone who owns 25% or more. Tell me who that is and I'll get it through today. Robert"),
    "call_kyb": ("TASK: call about stalled application", "Call {name} at {company}. Application pending 3 days. Find the missing owner. {phone}"),
    "kyb_stalled": ("OPS: application stalled 5 days", "{company} ({email}) has been pending 5 days. Rep has attempted twice. Please review or tell us what is missing."),
    "ach_pull_offer": ("Your Acme account is open. Two minutes to fund it.", "Hi {first}, {company} is approved. I can pull the first deposit from your current bank by ACH today, free, or send wire instructions for a larger amount. Which do you want? Robert"),
    "wire_details": ("Wire instructions for {company}", "Hi {first}, wire details are in the dashboard under Move Money. Send before early afternoon ET and it lands the same day. If you want me on the phone while you do it: {booking}"),
    "call_to_fund": ("TASK: funding call", "Call {name} at {company}. Approved 7 days, balance $0. Offer to walk the first deposit live. {phone}"),
    "bonus_deadline": ("Your deposit bonus has a date on it", "Hi {first}, the cash bonus on your account needs the first $20K in within 60 days of opening. You have time, but I'd rather you have it than miss it. {booking}"),
    "bonus_expires": ("Fifteen days left on your bonus", "Hi {first}, fifteen days until the deposit bonus window closes for {company}. Ten minutes and I'll get it done with you: {booking}"),
    "reactivation_call": ("TASK: reactivation call", "Call {name} at {company}. Dormant account, emailed 2 days ago, no reply. {phone}"),
    "reactivation_two": ("The number for {company}", "Hi {first}, one specific number: idle cash in Acme Treasury is paying a bit over 4% net this week. On what {company} had with us at peak, that's real money. Ten minutes: {booking}"),
    "reactivation_breakup": ("I'll leave it here", "Hi {first}, I'll stop nudging. Your Acme account is still open and takes nothing to restart. Whenever it's useful: {booking}"),
    "first_swipe": ("Put one subscription on the card", "Hi {first}, money's in. Easiest next step: create one virtual card and move one SaaS subscription to it. Cashback starts on the first swipe. Robert"),
    "treasury_allocation": ("Idle cash in {company}'s checking", "Hi {first}, anything above a payroll cycle in checking can sit in treasury earning 4%+. Allocation takes one screen. Want me to walk it? {booking}"),
    "activation_call": ("TASK: activation check-in", "Call {name} at {company}. Funded 10 days. Confirm card, treasury, and accounting sync are live. {phone}"),
}


def _stage_index(l):
    st = l.get("stage_pipeline") or "lead"
    return STAGES.index(st) if st in STAGES else 0


def _stage_is(l, s):
    return (l.get("stage_pipeline") or "lead") == s


def _stage_at_least(l, s):
    return _stage_index(l) >= STAGES.index(s)


def _stage_time(l, s):
    return (l.get("stage_times") or {}).get(s)


def _fields(l):
    name = (l.get("name") or "there").strip()
    return dict(first=name.split(" ")[0], name=name, company=l.get("company") or "your company",
                booking=route.cfg()["booking_link"], phone=l.get("phone") or "no phone on file", email=l.get("email") or "")


def due_steps(lead, now):
    """All (seq, idx, due_at, action, key) whose due time has passed, not yet sent, and not stopped."""
    out = []
    sent = lead.get("followup_sent") or {}
    for seq, spec in SEQUENCES.items():
        try:
            t0 = spec["trigger"](lead)
        except Exception:
            t0 = None
        if not t0 or spec["stop"](lead) or lead.get("opted_out"):
            continue
        for idx, (delay, action, key) in enumerate(spec["steps"]):
            due = t0 + delay
            tag = f"{seq}:{idx}"
            if due <= now and tag not in sent:
                if action == "sms" and not lead.get("phone"):
                    continue
                out.append((seq, idx, due, action, key))
    return out


def _deliver(lead, seq, idx, due, action, key, speed):
    subj, body = TEMPLATES[key]
    f = _fields(lead)
    text = body.format(**f)
    subj = subj.format(**f)
    if action in ("email", "sms"):
        os.makedirs(touch.OUTBOX, exist_ok=True)
        with open(os.path.join(touch.OUTBOX, f"{lead['id']}.txt"), "a", encoding="utf-8") as fh:
            fh.write(f"\n--- FOLLOW-UP {seq} step {idx + 1} ({action}) ---\n{('SUBJECT: ' + subj + chr(10)) if subj else ''}{text}\n")
    elif action == "task":
        alert.send(lead, "new", mention=f"[TASK {seq}] {lead.get('owner_slack') or ''}")
    elif action == "ops":
        alert.send(lead, "new", mention="[OPS ESCALATION] @onboarding-ops")
    return f"{seq} step {idx + 1}: {action} '{key}'"


def run_once(now=None, speed=1.0, since_days=60):
    """Fire every due step for every recent lead. Event timestamps use the scheduled due time so history reads correctly."""
    now = now or time.time()
    fired = 0
    for lead in store.list_leads(since=now - since_days * D):
        steps = due_steps(lead, now)
        if not steps:
            continue
        sent = dict(lead.get("followup_sent") or {})
        for seq, idx, due, action, key in steps:
            detail = _deliver(lead, seq, idx, due, action, key, speed)
            sent[f"{seq}:{idx}"] = due
            store.add_event(lead["id"], "followup", detail, at=due)
            fired += 1
        store.update_lead(lead["id"], followup_sent=sent)
    return fired


def summary(leads, window_start, now):
    """Per-sequence counts for the dashboard: leads currently in the sequence, steps sent in the window."""
    out = {}
    for seq, spec in SEQUENCES.items():
        active = 0
        sent = 0
        for l in leads:
            try:
                t0 = spec["trigger"](l)
            except Exception:
                t0 = None
            if t0 and not spec["stop"](l) and not l.get("opted_out"):
                active += 1
            for tag, at in (l.get("followup_sent") or {}).items():
                if tag.startswith(seq + ":") and at >= window_start:
                    sent += 1
        out[seq] = dict(active=active, sent=sent, steps=len(spec["steps"]))
    return out
