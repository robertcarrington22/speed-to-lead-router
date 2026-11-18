"""Message delivery for touch zero and follow-ups. Stdlib only.

Email: SMTP if SMTP_HOST/SMTP_USER/SMTP_PASS/FROM_EMAIL are set (Google Workspace works with an app password).
SMS:   Twilio REST if TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/TWILIO_FROM are set.
Either way the message is also written to ./outbox so the demo can show it.

If the team uses a sequencer on Salesforce (Outreach, Salesloft), replace send_email with 'add to sequence' and keep the
signature. The pipeline does not care how the message goes out, only that it went out inside 60 seconds.
"""
import base64
import os
import smtplib
import urllib.parse
import urllib.request
from email.message import EmailMessage


def email_configured():
    return all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "FROM_EMAIL"))


def sms_configured():
    return all(os.environ.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"))


def send_email(to, subject, body):
    if not email_configured():
        return "outbox"
    msg = EmailMessage()
    msg["From"] = os.environ["FROM_EMAIL"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=15) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)
    return "smtp"


def send_sms(to, body):
    if not sms_configured():
        return "outbox"
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    data = urllib.parse.urlencode({"To": to, "From": os.environ["TWILIO_FROM"], "Body": body}).encode()
    auth = base64.b64encode(f"{sid}:{os.environ['TWILIO_AUTH_TOKEN']}".encode()).decode()
    req = urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=data,
                                 headers={"Authorization": f"Basic {auth}"}, method="POST")
    urllib.request.urlopen(req, timeout=15).read()
    return "twilio"
