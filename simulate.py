"""Synthetic traffic so the dashboard shows a week of history and a live demo.

    python simulate.py --fresh --backfill 7      # wipe, then generate 7 days of leads with realistic outcomes
    python simulate.py --live 6                  # POST 6 leads to a running server, one every few seconds
    python simulate.py --live 6 --auto-touch     # ...and play the rep: log human touches at realistic delays

Distributions are deliberately imperfect: some misses, some after-hours, some suppressed. The point of the
dashboard is to show those honestly, not to look good.
"""
import argparse
import json
import os
import random
import sys
import threading
import time
import urllib.request
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from s2l import followup, pipeline, route, sla, store  # noqa: E402

FIRST = ["Maya", "Dev", "Priya", "Jordan", "Sam", "Alex", "Nina", "Leo", "Grace", "Omar", "Ivy", "Theo", "Zoe", "Kai", "Ana"]
LAST = ["Chen", "Patel", "Okafor", "Lindqvist", "Garcia", "Nakamura", "Rossi", "Haddad", "Novak", "Silva", "Brooks", "Mensah"]
COS = ["Northwind", "Lumen", "Halcyon", "Brightloop", "Fieldstack", "Verdant", "Orbit", "Tessel", "Quill", "Marrow", "Solstice", "Kestrel", "Anvil", "Cobalt", "Juniper"]
SUFFIX = ["Labs", "AI", "Health", "Co", "Studio", "Systems", "Goods", "Bio", "Robotics", ""]

# Source mix: rough guess at what a fintech's inbound pipe looks like once Atlas and the Brex page are live.
SOURCE_WEIGHTS = {
    # CRM and marketing systems, in their real payload shapes
    "hubspot_form": 26, "salesforce_lead": 6, "intercom": 6, "calendly": 5,
    # Partner and product signals that would need to be wired in
    "stripe_atlas": 18, "application_abandoned": 10, "opened_not_funded": 8, "brex_switcher": 5, "mercury_switcher": 5,
    "referral": 3, "accountant": 2, "event": 3, "inbound_phone": 1, "free_tool": 2,
}

INBOUND_TYPES = ["Bottom Funnel - Warm", "Hand Raiser", "Contact Sales", "Self-Serve Signup", "Partner Referral", "Marketing Lead"]
CONVERSION_PAGES = ["Contact Sales - Startups", "Startup Banking LP", "Direct Signup Page", "Mercury Competitor LP", "Ramp Competitor LP",
                    "Bill.com Competitor LP", "Treasury - Website Page", "Corporate Cards - Website Page", "For Accountants", "Pricing",
                    "Nerd Wallet LP", "Capterra Form", "Construction LP", "Healthcare LP", "Google Ads LP", "Linkedin Ad Form Submit", "Gusto LP"]
LEAD_SOURCES = ["Inbound", "Inbound", "Inbound", "Referral", "Partner Inbound", "Events", "Social", "Paid Influencers"]


def _company():
    return f"{random.choice(COS)} {random.choice(SUFFIX)}".strip()


def _person():
    return f"{random.choice(FIRST)} {random.choice(LAST)}"


def _email(name, company, free=False):
    n = name.lower().replace(" ", ".")
    if free:
        return f"{n}{random.randint(1, 99)}@gmail.com"
    d = company.lower().replace(" ", "") or "example"
    return f"{n}@{d}{random.randint(1, 999)}.com"


def payload(source):
    name, co = _person(), _company()
    phone = f"+1212555{random.randint(1000, 9999)}" if random.random() < 0.55 else None
    stage = random.choices(["pre_seed", "seed", "series_a_plus", "revenue_smb"], [35, 35, 15, 15])[0]
    cash = random.choices([10_000, 60_000, 250_000, 900_000], [30, 35, 25, 10])[0]
    p = {"name": name, "email": _email(name, co, free=random.random() < 0.25), "company": co, "phone": phone}
    first, last = name.split(" ", 1)
    if source == "hubspot_form":
        # Shape of a HubSpot workflow "Send webhook" for a contact, with Acme's hidden fields.
        it = random.choices(INBOUND_TYPES, [22, 20, 18, 22, 8, 10])[0]
        p = {"portalId": 12345678, "objectId": random.randint(10_000_000, 99_999_999), "properties": {
            "email": p["email"], "firstname": first, "lastname": last, "company": co, "phone": phone or "",
            "inbound_type": it, "leadsource": random.choice(LEAD_SOURCES),
            "signup_conversion_page": random.choice(CONVERSION_PAGES), "salesforce_inclusion": "true",
            "utm_source": random.choice(["google", "linkedin", "direct", "nerdwallet", ""]), "utm_medium": "cpc",
            "cash_on_hand": random.choice(["", "", 40000, 150000, 600000]), "current_bank": random.choice(["", "mercury", "chase", "brex", ""]),
        }}
    elif source == "salesforce_lead":
        # Shape of a Flow HTTP callout (JSON). The XML Outbound Message path is exercised separately.
        p = {"Id": "00Q" + "".join(random.choices("0123456789ABCDEFGHJKLMNPQRSTUVWXYZ", k=15)), "Email": p["email"], "FirstName": first,
             "LastName": last, "Company": co, "Phone": phone or "", "LeadSource": random.choice(LEAD_SOURCES), "Status": "Open - Not Contacted",
             "Inbound_Type__c": random.choice(INBOUND_TYPES[:4]), "Signup_Conversion_Page__c": random.choice(CONVERSION_PAGES)}
    elif source == "intercom":
        body = random.choice(["Hi, what's the current treasury yield?", "Can we get a card without a personal guarantee?",
                              "How long does it take to open an account?", "Do you support QuickBooks sync?"])
        p = {"topic": "conversation.user.created", "data": {"item": {"id": str(random.randint(10**9, 10**10)),
             "source": {"body": f"<p>{body}</p>", "author": {"type": "user", "email": p["email"], "name": name}},
             "contacts": {"contacts": [{"email": p["email"], "name": name}]}}}}
    elif source == "calendly":
        from datetime import datetime, timedelta, timezone
        start = (datetime.now(timezone.utc) + timedelta(days=random.randint(1, 4), hours=random.randint(0, 6))).strftime("%Y-%m-%dT%H:%M:%SZ")
        p = {"event": "invitee.created", "payload": {"email": p["email"], "name": name, "uri": "https://api.calendly.com/scheduled_events/x/invitees/y",
             "scheduled_event": {"start_time": start, "name": "Acme walkthrough"},
             "questions_and_answers": [{"question": "Company name", "answer": co}, {"question": "Phone number", "answer": phone or ""}],
             "tracking": {"utm_source": "website"}}}
    elif source == "web_form":
        p.update(form=random.choice(["demo", "open"]), stage=stage, cash_on_hand=cash,
                 current_bank=random.choice(["mercury", "brex", "legacy_bank", "none", "ramp"]),
                 industry=random.choices(["saas", "consumer_brand", "agency", "crypto_exchange"], [60, 20, 17, 3])[0],
                 entity_type=random.choices(["c_corp", "llc", "sole_prop"], [70, 27, 3])[0])
    elif source == "stripe_atlas":
        p = {"founder_name": name, "founder_email": p["email"], "company_name": co,
             "entity_type": random.choice(["c_corp", "llc"]), "incorporation_status": "in_progress",
             "expected_raise": random.choice([0, 50_000, 500_000, 1_500_000])}
    elif source == "brex_switcher":
        p.update(balance=random.choice([80_000, 250_000, 600_000, 2_000_000]), stage=random.choice(["seed", "series_a_plus"]),
                 message="saw the Capital One news")
    elif source == "mercury_switcher":
        p.update(balance=random.choice([40_000, 150_000, 400_000, 1_200_000]), stage=random.choice(["pre_seed", "seed", "seed", "series_a_plus"]),
                 reason=random.choice(["looking at treasury yield", "need a card without a personal guarantee", "FDIC coverage above 5M", "just comparing"]),
                 book_call=random.random() < 0.3)
    elif source == "application_abandoned":
        p.update(step=random.choice(["beneficial_owners", "formation_docs", "ein"]), entity_type=random.choice(["c_corp", "llc"]))
    elif source == "opened_not_funded":
        p.update(days_since_open=random.randint(3, 21), expected_deposit=cash, stage=stage)
    elif source == "referral":
        p.update(referrer_email=f"{random.choice(FIRST).lower()}@{random.choice(COS).lower()}.com")
    elif source == "accountant":
        p = {"name": name, "email": p["email"], "firm": f"{random.choice(LAST)} & Co CPAs", "phone": phone, "clients_count": random.choice([12, 30, 80])}
    elif source == "chat":
        p.update(transcript=random.choice(["do you have a treasury product and what's the yield", "can I get a card without a personal guarantee", "how do I open an account"]))
    elif source == "event":
        p.update(event=random.choice(["SF founder dinner", "Atlanta week", "billboard promo"]), stage=stage)
    elif source == "inbound_phone":
        p = {"caller_name": name, "phone": phone or "+12125550199", "voicemail_transcript": "hi, calling about opening an account for my company"}
    elif source == "free_tool":
        p.update(tool=random.choice(["SAFE generator", "invoice generator", "perks"]), stage="pre_seed")
    return p


def backfill(days):
    """Generate leads across the last N days, then simulate the humans: touches, meetings, funding."""
    now = time.time()
    start = now - days * 86400
    n_leads = int(days * random.uniform(14, 22))
    made = 0
    for _ in range(n_leads):
        # Weight receipt time toward business hours, leave a real after-hours tail.
        t = start + random.random() * (now - start - 600)
        d = route.local(t)
        if not route.is_business_hours(t) and random.random() < 0.75:
            t = d.replace(hour=random.randint(9, 17), minute=random.randint(0, 59)).timestamp()
            if d.weekday() >= 5:
                t -= timedelta(days=d.weekday() - 4).total_seconds()
            if t > now - 600:
                continue
        source = random.choices(list(SOURCE_WEIGHTS), list(SOURCE_WEIGHTS.values()))[0]
        lead = pipeline.process(source, payload(source), received_at=t, live=False)
        made += 1
        if lead.get("suppressed") or "sla_start_at" not in lead:
            continue
        # The rep: log-normal-ish delay. Closers are a bit slower; Tier C gets a light touch hours later.
        kind = lead["owner_kind"]
        if kind == "rep":
            delay = random.lognormvariate(4.6, 0.55)      # median ~100s, tail past 5 min
        elif kind == "closer":
            delay = random.lognormvariate(5.1, 0.5)       # median ~165s
        else:
            delay = random.uniform(1800, 4 * 3600)
        if random.random() < 0.06:
            delay = random.uniform(400, 1500)              # the honest misses
        touch_at = lead["sla_start_at"] + delay
        if touch_at > now:
            continue
        reached = random.random() < 0.62  # a third of first calls go to voicemail; that starts the no-contact sequence
        lead = sla.record_human_touch(lead["id"], "Robert" if kind != "closer" else lead["owner"], random.choice(["call", "call", "email", "sms"]), at=touch_at, reached=reached)
        times = dict(lead.get("stage_times") or {})
        # Downstream: meeting, application, funding. Faster human touch and a reached contact -> better odds, on purpose.
        fast = delay <= 300
        p_meet = {"A": 0.55, "B": 0.35, "C": 0.12}[lead["tier"]] * (1.0 if fast else 0.6) * (1.0 if reached else 0.45)
        if random.random() < p_meet:
            meet_at = touch_at + random.uniform(1800, 36 * 3600)
            if meet_at < now:
                times["qualified"] = meet_at
                store.update_lead(lead["id"], meeting_at=meet_at, stage_pipeline="qualified", stage_times=times)
                store.add_event(lead["id"], "meeting_booked", "", at=meet_at)
                if random.random() < 0.65:
                    app_at = meet_at + random.uniform(600, 24 * 3600)
                    if app_at < now:
                        times["applied"] = app_at
                        store.update_lead(lead["id"], stage_pipeline="applied", stage_times=times)
                        store.add_event(lead["id"], "stage", "applied", at=app_at)
                        if random.random() < 0.8:
                            appr_at = app_at + random.uniform(1800, 24 * 3600)
                            if appr_at < now:
                                times["approved"] = appr_at
                                store.update_lead(lead["id"], stage_pipeline="approved", stage_times=times)
                                store.add_event(lead["id"], "stage", "approved", at=appr_at)
                                if random.random() < 0.6:
                                    fund_at = appr_at + random.uniform(3600, 48 * 3600)
                                    if fund_at < now:
                                        amt = {"lt_25k": 15_000, "25k_100k": 60_000, "100k_500k": 220_000, "500k_plus": 800_000, "unknown": 40_000}[lead.get("balance_band") or "unknown"]
                                        amt = amt * random.uniform(0.6, 1.4)
                                        times["funded"] = fund_at
                                        store.update_lead(lead["id"], stage_pipeline="funded", funded_at=fund_at, funded_amount=round(amt, -3), stage_times=times)
                                        store.add_event(lead["id"], "stage", f"funded ${amt:,.0f}", at=fund_at)
        # Some no-contact leads reply to a follow-up email; that stops the sequence.
        if not reached and random.random() < 0.2:
            store.update_lead(lead["id"], replied_at=touch_at + random.uniform(3 * 3600, 3 * 86400))
    fired = followup.run_once(now=now)
    print(f"backfilled {made} leads over {days} days; {fired} follow-up steps fired historically")


def gen_accounts(n, now=None):
    """Dormant-account book with realistic shapes. Roughly 35% active, the rest spread across the segments."""
    from s2l import reactivation
    now = now or time.time()
    rows = []
    for i in range(n):
        name, co = _person(), _company()
        opened = now - random.uniform(60, 700) * 86400
        kind = random.choices(["active", "opened_never_funded", "funded_then_drained", "card_only", "went_quiet", "closed"], [35, 22, 14, 10, 14, 5])[0]
        peak = random.choice([0, 2_000, 30_000, 80_000, 200_000, 600_000, 1_500_000])
        a = dict(account_id=f"acct_{i:04d}", sfdc_account_id="001" + "".join(random.choices("0123456789ABCDEFGHJKLMNPQRSTUVWXYZ", k=15)),
                 company=co, contact_name=name, email=_email(name, co, free=random.random() < 0.2), phone=f"+1212555{random.randint(1000, 9999)}" if random.random() < 0.6 else None,
                 stage=random.choice(["pre_seed", "seed", "seed", "series_a_plus", "revenue_smb"]), opened_at=opened, products=["checking"],
                 status="open", support_ticket_open=random.random() < 0.04, compliance_hold=random.random() < 0.02, opted_out=random.random() < 0.03, triggers=[])
        if kind == "active":
            a.update(peak_balance=max(peak, 30_000), current_balance=max(peak, 30_000) * random.uniform(0.5, 1.2), last_deposit_at=now - random.uniform(1, 20) * 86400,
                     last_card_swipe_at=now - random.uniform(0, 5) * 86400, last_login_at=now - random.uniform(0, 3) * 86400, products=["checking", "card"])
        elif kind == "opened_never_funded":
            a.update(peak_balance=random.choice([0, 0, 500, 2_000]), current_balance=0, last_deposit_at=None, last_card_swipe_at=None, last_login_at=opened + random.uniform(0, 10) * 86400)
        elif kind == "funded_then_drained":
            pk = max(peak, 50_000)
            a.update(peak_balance=pk, current_balance=pk * random.uniform(0, 0.08), last_deposit_at=now - random.uniform(100, 400) * 86400,
                     last_card_swipe_at=now - random.uniform(60, 300) * 86400, last_login_at=now - random.uniform(30, 200) * 86400,
                     outbound_to=random.choice(["mercury", "brex", "chase", "ramp", "svb"]), products=["checking", "card"])
        elif kind == "card_only":
            a.update(peak_balance=max(peak, 20_000), current_balance=random.uniform(1_000, 15_000), last_deposit_at=now - random.uniform(100, 300) * 86400,
                     last_card_swipe_at=now - random.uniform(0, 10) * 86400, last_login_at=now - random.uniform(0, 10) * 86400, products=["checking", "card"])
        elif kind == "went_quiet":
            a.update(peak_balance=max(peak, 10_000), current_balance=random.uniform(500, 20_000), last_deposit_at=now - random.uniform(100, 400) * 86400,
                     last_card_swipe_at=now - random.uniform(70, 300) * 86400, last_login_at=now - random.uniform(61, 300) * 86400)
        else:
            a.update(status="closed", peak_balance=max(peak, 20_000), current_balance=0, last_deposit_at=now - random.uniform(200, 500) * 86400,
                     last_card_swipe_at=None, last_login_at=now - random.uniform(150, 400) * 86400, outbound_to=random.choice(["mercury", "brex"]))
        # Triggers: some accounts have a fresh one, most have none or a stale one.
        r = random.random()
        if r < 0.18:
            t = random.choice(list(reactivation.TRIGGER_WEIGHT))
            a["triggers"].append(dict(type=t, at=now - random.uniform(1, 40) * 86400, detail={
                "new_raise": f"raised ${random.choice([1, 2, 4, 8, 12])}M", "brex_migration": "on Brex; Capital One integration", "incentive_deadline": "bonus window closes in 14 days",
                "hiring_spike": f"{random.randint(3, 12)} open roles", "rate_change": "treasury +15 bps", "product_launch": "invoice card payments", "anniversary": "1 year since open"}[t]))
        elif r < 0.30:
            a["triggers"].append(dict(type=random.choice(["hiring_spike", "anniversary", "product_launch"]), at=now - random.uniform(60, 180) * 86400, detail="older signal"))
        rows.append(a)
    reactivation.save(rows)
    return rows


def backfill_reactivation(weeks, now=None):
    """Run past weekly batches and simulate outcomes so the report shows whether the score works."""
    from s2l import reactivation
    now = now or time.time()
    for w in range(weeks, 0, -1):
        at = now - w * 7 * 86400
        res = reactivation.run_week(now=at, live=False, batch_label=time.strftime("wk-%m-%d", time.localtime(at)))
        batch = [l for l in store.list_leads(since=at - 60) if l.get("source") == "reactivation" and l.get("received_at") == at]
        for l in batch:
            seg, trig, tier, sc = l["reactivation_segment"], l.get("reactivation_trigger"), l["reactivation_tier"], l["score"]
            p = {"opened_never_funded": 0.14, "card_only": 0.16, "funded_then_drained": 0.12, "went_quiet": 0.07, "closed": 0.03}[seg]
            p *= 1.6 if trig in ("new_raise", "brex_migration", "incentive_deadline") else (1.2 if trig else 0.7)
            p *= 1.5 if tier == "call" else 1.0
            p *= 1.0 + (sc - 50) / 100.0  # the score is designed to carry signal; the sim honors that so the check is meaningful
            if tier == "call":
                reached = random.random() < 0.4
                sla.record_human_touch(l["id"], "Robert", "call", at=at + random.uniform(3600, 2 * 86400), reached=reached)
            if random.random() < 0.15:
                store.update_lead(l["id"], replied_at=at + random.uniform(3600, 5 * 86400))
            if random.random() < p:
                fund_at = at + random.uniform(1, 25) * 86400
                if fund_at < now:
                    amt = max(5_000, (l.get("peak_balance") or 20_000) * random.uniform(0.3, 1.1))
                    reactivation.record_outcome(l["account_id"], round(amt, -3), at=fund_at)
        print(f"reactivation batch {res['batch']}: {res['calls']} calls, {res['emails']} emails, {res['hold']} held")


def live(n, base, auto_touch):
    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=10).read())

    # Use the server's clock speed, not this process's, so simulated touches land where intended.
    speed = json.loads(urllib.request.urlopen(base + "/api/config", timeout=10).read())["speed"]
    print(f"server clock {speed:g}x", flush=True)

    def rep(lead_id, delay):
        time.sleep(delay)
        reached = random.random() < 0.62
        r = post(f"/lead/{lead_id}/touch", {"by": "Robert", "channel": "call", "reached": reached})
        print(f"  rep called {lead_id} at {delay * speed:.0f}s on the clock -> {r['sla_result']}{'' if reached else ' (voicemail; no-contact sequence starts)'}", flush=True)

    for i in range(n):
        source = random.choices(list(SOURCE_WEIGHTS), list(SOURCE_WEIGHTS.values()))[0]
        r = post(f"/webhook/{source}", payload(source))
        print(f"posted {source}: tier {r.get('tier')} score {r.get('score')} -> {r.get('owner')} ({r.get('owner_kind')}){' after hours' if r.get('after_hours') else ''}{' SUPPRESSED' if r.get('suppressed') else ''}", flush=True)
        if auto_touch and not r.get("suppressed") and r.get("owner_kind") in ("rep", "closer"):
            # Mostly inside the window, sometimes not, so escalation and misses fire on camera.
            delay = random.choice([70, 110, 150, 200, 250, 340]) / speed
            threading.Thread(target=rep, args=(r["id"], delay), daemon=True).start()
        time.sleep(random.uniform(2, 5))
    if auto_touch:
        time.sleep(380 / speed)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="wipe the database first")
    ap.add_argument("--backfill", type=int, default=0, help="days of history to generate")
    ap.add_argument("--accounts", type=int, default=0, help="generate N dormant/active accounts for the reactivation motion")
    ap.add_argument("--reactivation-weeks", type=int, default=0, help="run N past weekly reactivation batches with simulated outcomes")
    ap.add_argument("--live", type=int, default=0, help="number of leads to POST to a running server")
    ap.add_argument("--auto-touch", action="store_true", help="simulate the rep logging touches")
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    random.seed(a.seed)
    if a.fresh:
        store.reset()
        print("database reset")
    if a.backfill:
        backfill(a.backfill)
    if a.accounts:
        gen_accounts(a.accounts)
        print(f"generated {a.accounts} accounts")
    if a.reactivation_weeks:
        backfill_reactivation(a.reactivation_weeks)
        from s2l import followup
        followup.run_once()
    if a.live:
        live(a.live, a.base, a.auto_touch)
