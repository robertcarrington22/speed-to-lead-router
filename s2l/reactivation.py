"""Reactivation motion for dormant accounts.

The inbound engine works leads that raised a hand. This works accounts that already said yes once and went quiet.
It answers three questions for every dormant account, in order:

  1. WHY did it go dormant?      -> segment      (opened never funded, funded then drained, card only, went quiet, closed)
  2. WHY NOW?                    -> trigger      (new raise, Brex migration, hiring spike, rate change, product launch,
                                                  incentive deadline, anniversary)
  3. WHAT is it worth to wake?   -> value        (prior peak balance)

Score = value + trigger recency x trigger weight + segment prior. Suppress if there is an open support ticket, a
compliance hold, an opt-out, or a contact in the last 30 days. Every week the top of the list becomes a call list,
the next tier becomes a one-variable email, and everything else waits. Outcomes (funded within 30 days) are written
back against the score so the weights can be checked, not guessed.

Accounts come from the product database, a Salesforce Account report, or a warehouse export (Snowflake). The
prototype loads data/accounts.json. Field names are in the ACCOUNT_FIELDS comment below.
"""
import json
import math
import os
import time

from . import alert, followup, route, store, touch

D = 86400
DATA = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "accounts.json")

# ACCOUNT_FIELDS: account_id, sfdc_account_id, company, contact_name, email, phone, stage, opened_at, peak_balance,
#   current_balance, last_deposit_at, last_card_swipe_at, last_login_at, products[], outbound_to, status,
#   support_ticket_open, compliance_hold, opted_out, last_reactivation_at, triggers[{type, at, detail}]

TRIGGER_WEIGHT = {"new_raise": 1.0, "brex_migration": 1.0, "incentive_deadline": 0.8, "hiring_spike": 0.7,
                  "rate_change": 0.5, "product_launch": 0.4, "anniversary": 0.3}
SEGMENT_PRIOR = {"opened_never_funded": 20, "card_only": 20, "funded_then_drained": 15, "went_quiet": 12, "closed": 5}
CALL_LIST_SIZE = 20
EMAIL_LIST_SIZE = 100

_accounts = None


def load(path=DATA):
    global _accounts
    if _accounts is None:
        _accounts = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else []
    return _accounts


def save(accounts, path=DATA):
    global _accounts
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(accounts, open(path, "w", encoding="utf-8"))
    _accounts = accounts


def import_accounts(rows):
    """Replace the account list. Rows use ACCOUNT_FIELDS; anything missing is treated as unknown."""
    save(list(rows))
    return len(rows)


# ---- 1. why dormant ----------------------------------------------------------

def segment(a, now=None):
    now = now or time.time()
    if a.get("status") == "closed":
        return "closed"
    peak = a.get("peak_balance") or 0
    bal = a.get("current_balance") or 0
    last_dep = a.get("last_deposit_at") or 0
    last_swipe = a.get("last_card_swipe_at") or 0
    last_login = a.get("last_login_at") or 0
    if peak < 5_000:
        return "opened_never_funded"
    if bal < 0.10 * peak and a.get("outbound_to"):
        return "funded_then_drained"
    if last_swipe > now - 30 * D and last_dep < now - 90 * D:
        return "card_only"
    if last_login < now - 60 * D and bal < 25_000:
        return "went_quiet"
    if last_dep < now - 90 * D and last_swipe < now - 60 * D:
        return "went_quiet"
    return "active"


# ---- 2. why now, 3. what it is worth ----------------------------------------

def latest_trigger(a, now=None):
    now = now or time.time()
    trig = [t for t in (a.get("triggers") or []) if t.get("at") and t["at"] <= now]
    if not trig:
        return None
    return max(trig, key=lambda t: t["at"])


def score(a, now=None):
    now = now or time.time()
    seg = segment(a, now)
    breakdown = {}
    if seg == "active":
        return dict(score=0, segment=seg, trigger=None, suppressed=True, suppress_reason="account is active", breakdown={})
    for flag, reason in (("support_ticket_open", "open support ticket"), ("compliance_hold", "compliance hold"), ("opted_out", "opted out")):
        if a.get(flag):
            return dict(score=0, segment=seg, trigger=latest_trigger(a, now), suppressed=True, suppress_reason=reason, breakdown={})
    if (a.get("last_reactivation_at") or 0) > now - 30 * D:
        return dict(score=0, segment=seg, trigger=latest_trigger(a, now), suppressed=True, suppress_reason="contacted in last 30 days", breakdown={})

    peak = max(a.get("peak_balance") or 0, 1_000)
    value = min(40, round((math.log10(peak) - 3) / 3 * 40))  # $1K -> 0, $1M -> 40
    breakdown["value(peak $%s)" % f"{int(peak):,}"] = value

    t = latest_trigger(a, now)
    if t:
        age = (now - t["at"]) / D
        rec = 30 if age <= 14 else (20 if age <= 30 else (10 if age <= 90 else 0))
        pts = round(rec * TRIGGER_WEIGHT.get(t["type"], 0.3))
        breakdown[f"trigger {t['type']} ({int(age)}d ago)"] = pts
    else:
        breakdown["no trigger"] = 0

    breakdown[f"segment {seg}"] = SEGMENT_PRIOR.get(seg, 0)
    total = sum(breakdown.values())
    return dict(score=total, segment=seg, trigger=t, suppressed=False, suppress_reason=None, breakdown=breakdown)


# ---- the weekly queue --------------------------------------------------------

def build_queue(now=None):
    """Score every account, rank, and split into call / email / hold. Returns the ranked list with tiers."""
    now = now or time.time()
    ranked = []
    for a in load():
        s = score(a, now)
        if s["suppressed"]:
            continue
        ranked.append(dict(account=a, **s))
    ranked.sort(key=lambda r: -r["score"])
    for i, r in enumerate(ranked):
        r["tier"] = "call" if i < CALL_LIST_SIZE else ("email" if i < CALL_LIST_SIZE + EMAIL_LIST_SIZE else "hold")
    return ranked


EMAIL = {
    "opened_never_funded": ("Your Acme account is open and empty",
        "Hi {first},\n\nRobert at Acme. {company}'s account has been open since {opened} with nothing in it, so it isn't earning anything. {why_now} I can pull a first deposit by ACH in two minutes, free, or send wire details.\n\n{booking}\n\nRobert"),
    "funded_then_drained": ("One reason to move cash back to Acme",
        "Hi {first},\n\nRobert at Acme. You moved {company}'s balance out a while back, which is fair. {why_now} Treasury is paying a bit over 4% net this week from $50K, and it sits next to the checking account you already have. Ten minutes to turn it back on?\n\n{booking}\n\nRobert"),
    "card_only": ("The other half of your Acme account",
        "Hi {first},\n\nRobert at Acme. {company} uses the card but banks elsewhere. {why_now} Anything idle in that other account could be earning 4%+ in Acme Treasury next to the card that already works. Want the numbers?\n\n{booking}\n\nRobert"),
    "went_quiet": ("Checking in on {company}",
        "Hi {first},\n\nRobert at Acme. It's been a while since anyone logged into {company}'s account. {why_now} If banking isn't a priority right now, tell me when it is. If something broke, tell me that too and I'll fix it.\n\n{booking}\n\nRobert"),
    "closed": ("If {company} ever needs a bank again",
        "Hi {first},\n\nRobert at Acme. You closed the account, so I'll keep this short. {why_now} If it's useful, the door is open and setup is minutes.\n\n{booking}\n\nRobert"),
}

WHY_NOW = {
    "new_raise": "Congratulations on the round, by the way.",
    "brex_migration": "With Brex moving under Capital One, a lot of founders are setting up a second rail this month.",
    "incentive_deadline": "Your deposit bonus window is still open for a couple of weeks.",
    "hiring_spike": "Looks like the team is growing, which usually means payroll and cards get more complicated.",
    "rate_change": "Treasury yields moved this week and I wanted you to have the number.",
    "product_launch": "We just launched card payments on invoices, which a few customers like you had asked for.",
    "anniversary": "It's been about a year since the account opened.",
}


def _render(a, s):
    first = (a.get("contact_name") or "there").split(" ")[0]
    why = WHY_NOW.get(s["trigger"]["type"], "") if s.get("trigger") else ""
    subj, body = EMAIL.get(s["segment"], EMAIL["went_quiet"])
    f = dict(first=first, company=a.get("company") or "your company", booking=route.cfg()["booking_link"],
             opened=time.strftime("%B %Y", time.localtime(a.get("opened_at") or time.time())), why_now=why)
    return subj.format(**f), body.format(**f).replace("  ", " ")


def run_week(now=None, live=True, batch_label=None):
    """Create this week's batch: a lead per account in the call or email tier, an email out, a task for calls."""
    now = now or time.time()
    label = batch_label or time.strftime("wk-%Y-%m-%d", time.localtime(now))
    queue = build_queue(now)
    made = []
    accounts = load()
    by_id = {a["account_id"]: a for a in accounts}
    for r in queue:
        if r["tier"] == "hold":
            continue
        a = r["account"]
        subj, body = _render(a, r)
        lead = dict(
            id=store.new_id("re"), source="reactivation", received_at=now, name=a.get("contact_name"), email=(a.get("email") or "").lower() or None,
            company=a.get("company"), phone=a.get("phone"), domain=None, entity_type="unknown", country="US", industry="unknown",
            stage=a.get("stage", "unknown"), balance_band="unknown", intent="reactivation", current_provider=a.get("outbound_to") or "unknown",
            multi_client=False, referral=False, book_call_requested=False, notes=f"Reactivation {label} · {r['segment']} · {r['trigger']['type'] if r['trigger'] else 'no trigger'}",
            inbound_type=None, lead_source="Warm Outbound", conversion_page=None, sfdc_id=None, hubspot_contact_id=None,
            sfdc_account_id=a.get("sfdc_account_id"), account_id=a["account_id"], reactivation_batch=label,
            reactivation_segment=r["segment"], reactivation_trigger=(r["trigger"] or {}).get("type"), reactivation_tier=r["tier"],
            score=r["score"], breakdown=r["breakdown"], tier="B" if r["tier"] == "call" else "C", suppressed=False,
            owner=route.cfg()["owners"]["smb_rep"]["name"], owner_kind="rep_light", owner_slack=route.cfg()["owners"]["smb_rep"]["slack"],
            after_hours=False, sla_start_at=now, sla_result="exempt", stage_pipeline="lead", stage_times={}, peak_balance=a.get("peak_balance"),
        )
        store.insert_lead(lead)
        store.add_event(lead["id"], "received", f"reactivation {label}: {r['segment']} score {r['score']} tier {r['tier']}", at=now)
        # The email goes to everyone in the batch. Calls are a task for the rep on top.
        path, via = touch._deliver(lead, {"email_subject": subj, "email_body": body}, live=live)
        store.update_lead(lead["id"], auto_touch_at=now + 5, touch_one=dict(email_subject=subj, email_body=body, channels=["email"], via=via, path=path))
        store.add_event(lead["id"], "auto_touch", "reactivation email", at=now + 5)
        if r["tier"] == "call":
            store.add_event(lead["id"], "task", f"reactivation call: {r['segment']}, peak ${int(a.get('peak_balance') or 0):,}", at=now + 5)
            if live:
                alert.send(dict(lead, tier="RE"), "new", mention=f"[REACTIVATION CALL] {lead['owner_slack']}")
        by_id[a["account_id"]]["last_reactivation_at"] = now
        by_id[a["account_id"]]["last_reactivation_lead_id"] = lead["id"]
        made.append(lead)
    save(accounts)
    if live:
        from . import sync
        for lead in made:
            sync.on_reactivation(lead)
    return dict(batch=label, calls=sum(1 for l in made if l["reactivation_tier"] == "call"),
                emails=sum(1 for l in made if l["reactivation_tier"] == "email"), hold=sum(1 for r in queue if r["tier"] == "hold"))


def record_outcome(account_id, funded_amount, at=None):
    """An account in a batch crossed the funded threshold. Stops sequences, writes the outcome against the score."""
    at = at or time.time()
    accounts = load()
    a = next((x for x in accounts if x["account_id"] == account_id), None)
    if not a:
        return None
    a["current_balance"] = max(a.get("current_balance") or 0, funded_amount)
    a["last_deposit_at"] = at
    save(accounts)
    lid = a.get("last_reactivation_lead_id")
    if lid and store.get_lead(lid):
        store.add_event(lid, "stage", f"funded ${funded_amount:,.0f} (reactivated)", at=at)
        return store.update_lead(lid, stage_pipeline="funded", funded_at=at, funded_amount=funded_amount,
                                 stage_times=dict(store.get_lead(lid).get("stage_times") or {}, funded=at))
    return a


# ---- reporting ---------------------------------------------------------------

def report(now=None, window_days=90):
    now = now or time.time()
    leads = [l for l in store.list_leads(since=now - window_days * D) if l.get("source") == "reactivation"]
    accounts = load()
    segs = {}
    for a in accounts:
        s = segment(a, now)
        d = segs.setdefault(s, dict(accounts=0, peak_total=0))
        d["accounts"] += 1
        d["peak_total"] += a.get("peak_balance") or 0

    def bucket(rows, key):
        out = {}
        for l in rows:
            k = l.get(key) or "(none)"
            d = out.setdefault(k, dict(contacted=0, called=0, replied=0, funded=0, dollars=0))
            d["contacted"] += 1
            d["called"] += l.get("reactivation_tier") == "call"
            d["replied"] += bool(l.get("replied_at"))
            if l.get("funded_at") and l["funded_at"] - l["received_at"] <= 30 * D:
                d["funded"] += 1
                d["dollars"] += l.get("funded_amount") or 0
        for d in out.values():
            d["funded_rate"] = d["funded"] / d["contacted"] if d["contacted"] else None
        return out

    by_segment = bucket(leads, "reactivation_segment")
    for k, v in by_segment.items():
        v.update(segs.get(k, {}))
    by_trigger = bucket(leads, "reactivation_trigger")
    by_tier = bucket(leads, "reactivation_tier")
    by_batch = bucket(leads, "reactivation_batch")
    # Score validation: did higher scores actually fund more often?
    def sb(l):
        s = l.get("score") or 0
        return "70+" if s >= 70 else ("50-69" if s >= 50 else "<50")
    for l in leads:
        l["_sb"] = sb(l)
    by_score = bucket(leads, "_sb")
    queue = build_queue(now)
    return dict(
        dormant_accounts=sum(v["accounts"] for k, v in segs.items() if k != "active"),
        active_accounts=segs.get("active", {}).get("accounts", 0),
        dormant_peak_total=sum(v["peak_total"] for k, v in segs.items() if k != "active"),
        contacted=len(leads), funded=sum(1 for l in leads if l.get("funded_at")),
        dollars=sum(l.get("funded_amount") or 0 for l in leads if l.get("funded_at")),
        funded_rate=(sum(1 for l in leads if l.get("funded_at")) / len(leads)) if leads else None,
        by_segment=by_segment, by_trigger=by_trigger, by_tier=by_tier, by_batch=by_batch, by_score=by_score,
        queue=[dict(account_id=r["account"]["account_id"], company=r["account"].get("company"), contact=r["account"].get("contact_name"),
                    phone=r["account"].get("phone"), segment=r["segment"], trigger=(r["trigger"] or {}).get("type"),
                    trigger_detail=(r["trigger"] or {}).get("detail"), peak=r["account"].get("peak_balance"), score=r["score"], tier=r["tier"],
                    breakdown=r["breakdown"]) for r in queue[:CALL_LIST_SIZE + EMAIL_LIST_SIZE]],
        queue_sizes=dict(call=sum(1 for r in queue if r["tier"] == "call"), email=sum(1 for r in queue if r["tier"] == "email"),
                         hold=sum(1 for r in queue if r["tier"] == "hold")),
    )
