"""Touch one: an automated, source-specific first reply that goes out inside 60 seconds.

Rules baked in here:
  - Hand-written templates. Exactly ONE personalized variable per template, filled from the lead. No AI body copy.
  - Email always. SMS only if a phone number came in on the form.
  - Every message offers a concrete next step: a booking link or a reply-to-schedule line.
  - Messages are written to ./outbox as files so a demo shows exactly what would have gone out.
Swap `_deliver` for SendGrid/Postmark + Twilio. The contract does not change.
"""
import os
import time

OUTBOX = os.path.join(os.path.dirname(os.path.dirname(__file__)), "outbox")

EMAIL = {
    "web_form": (
        "Re: your Acme demo request",
        "Hi {first},\n\nRobert at Acme. Saw your request a minute ago. I can walk you through the account, cards, and treasury in 20 minutes, or answer one question by reply if that's faster.\n\nGrab a time: {booking}\n\nRobert",
    ),
    "stripe_atlas": (
        "Your Acme account while the EIN is pending",
        "Hi {first},\n\nRobert at Acme. You picked Acme inside Atlas, so you can bank before the IRS letter arrives. I'll get {company} set up pre-EIN and make sure the deposit bonus applies.\n\nQuick call today: {booking}\n\nRobert",
    ),
    "brex_switcher": (
        "Your Acme switching credit, and the low-risk first step",
        "Hi {first},\n\nRobert at Acme. The $1,000 credit needs a short call, so here's the link. On it I'll show you the treasury-first sequence most Brex founders use: idle cash earns this week, payroll moves last.\n\n{booking}\n\nRobert",
    ),
    "mercury_switcher": (
        "Acme next to Mercury, without moving payroll",
        "Hi {first},\n\nRobert at Acme. You don't have to leave Mercury to use Acme. Most founders open Acme alongside it, put idle cash in treasury this week, and take the card with no personal guarantee. Payroll stays where it is until you decide.\n\nTen minutes and I'll show you the numbers for {company}: {booking}\n\nRobert",
    ),
    "application_abandoned": (
        "Finishing your Acme application",
        "Hi {first},\n\nRobert at Acme. Your application stopped at one step, and it's almost always a missing owner ID. Tell me who owns 25% or more of {company} and I'll get it through today.\n\nOr grab me: {booking}\n\nRobert",
    ),
    "opened_not_funded": (
        "Your Acme account is open. Funding it takes two minutes.",
        "Hi {first},\n\nRobert at Acme. {company} is approved and the account is empty, which means it's not earning anything yet. I can pull the first deposit by ACH today, free, or send wire details for a larger amount.\n\nTen minutes: {booking}\n\nRobert",
    ),
    "referral": (
        "A Acme referral from a friend of yours",
        "Hi {first},\n\nRobert at Acme. {referrer} sent you our way. Happy to set up {company} today and make sure you both get the referral reward.\n\n{booking}\n\nRobert",
    ),
    "accountant": (
        "Acme for your clients",
        "Hi {first},\n\nRobert at Acme. Thanks for reaching out from {company}. Firms usually start by moving one or two clients and getting read-only access across all of them. I'll show you that in 20 minutes.\n\n{booking}\n\nRobert",
    ),
    "chat": (
        "Following up on your chat with Acme",
        "Hi {first},\n\nRobert at Acme, following up on your chat about {topic}. Fastest way to get a straight answer is a ten-minute call, or reply here with the question.\n\n{booking}\n\nRobert",
    ),
    "event": (
        "Good to meet you at {event_name}",
        "Hi {first},\n\nRobert at Acme. Good to meet you at {event_name}. If it's useful, I can set {company} up in a short call and show you what the treasury account is paying this week.\n\n{booking}\n\nRobert",
    ),
    "inbound_phone": (
        "Sorry we missed your call",
        "Hi {first},\n\nRobert at Acme. Sorry we missed you. I'm calling you back now; if that's a bad time, pick one here: {booking}\n\nRobert",
    ),
    "free_tool": (
        "Your Acme {tool} and what's next",
        "Hi {first},\n\nRobert at Acme. Glad the {tool} was useful. When you're ready to bank, the account takes minutes and there's no fee. Reply with a question anytime, or: {booking}\n\nRobert",
    ),
}

SMS = "Hi {first}, Robert at Acme. Got your note just now. Calling shortly; or pick a time: {booking}"


def _fields(lead, booking):
    name = (lead.get("name") or "there").strip()
    first = name.split(" ")[0] if name else "there"
    notes = lead.get("notes") or ""
    return dict(
        first=first, company=lead.get("company") or "your company", booking=booking,
        referrer=notes.replace("Referred by ", "") if notes.startswith("Referred by") else "a customer",
        topic=(lead.get("intent") or "your question").replace("_", " "),
        event_name=notes.replace("Event: ", "") if notes.startswith("Event:") else "the event",
        tool=notes.replace("Tool: ", "") if notes.startswith("Tool:") else "tool",
    )


def render(lead, booking):
    subj, body = EMAIL.get(lead["source"], EMAIL["web_form"])
    f = _fields(lead, booking)
    out = {"email_subject": subj.format(**f), "email_body": body.format(**f)}
    if lead.get("phone"):
        out["sms"] = SMS.format(**f)
    return out


def _deliver(lead, msg, live=True):
    from . import deliver
    os.makedirs(OUTBOX, exist_ok=True)
    path = os.path.join(OUTBOX, f"{lead['id']}.txt")
    via = {"email": "outbox", "sms": None}
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"TO: {lead.get('email') or lead.get('phone')}\nSOURCE: {lead['source']}\nSUBJECT: {msg['email_subject']}\n\n{msg['email_body']}\n")
        if msg.get("sms"):
            fh.write(f"\n--- SMS to {lead.get('phone')} ---\n{msg['sms']}\n")
    if live:
        try:
            if lead.get("email"):
                via["email"] = deliver.send_email(lead["email"], msg["email_subject"], msg["email_body"])
            if msg.get("sms") and lead.get("phone"):
                via["sms"] = deliver.send_sms(lead["phone"], msg["sms"])
        except Exception as e:  # delivery failure must not stop the alert or the clock
            via["error"] = str(e)
    return path, via


def send_touch_one(lead, booking, live=True):
    msg = render(lead, booking)
    path, via = _deliver(lead, msg, live=live)
    return dict(sent_at=time.time(), channels=["email"] + (["sms"] if msg.get("sms") else []), path=path, via=via, **msg)
