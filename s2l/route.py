"""Business hours, owner assignment, and the SLA clock start."""
import itertools
import json
import os
from datetime import datetime, timedelta, timezone

_CFG = None
_closer_cycle = None


def cfg():
    global _CFG, _closer_cycle
    if _CFG is None:
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "routing.json")
        with open(path, encoding="utf-8") as f:
            _CFG = json.load(f)
        _closer_cycle = itertools.cycle(_CFG["owners"]["closers"])
    return _CFG


def tz():
    return timezone(timedelta(hours=cfg()["timezone_offset_hours"]))


def local(ts):
    return datetime.fromtimestamp(ts, tz())


def is_business_hours(ts):
    bh = cfg()["business_hours"]
    d = local(ts)
    return d.weekday() in bh["days"] and bh["start"] <= d.hour < bh["end"]


def next_open(ts):
    """Epoch seconds of the next business-hours open at or after ts."""
    bh = cfg()["business_hours"]
    d = local(ts)
    if is_business_hours(ts):
        return ts
    candidate = d.replace(hour=bh["start"], minute=0, second=0, microsecond=0)
    if d.hour >= bh["end"] or d.weekday() not in bh["days"]:
        candidate += timedelta(days=1)
    while candidate.weekday() not in bh["days"]:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def assign(lead, needs_closer):
    o = cfg()["owners"]
    if lead.get("suppressed"):
        return dict(owner="nurture", owner_kind="suppressed", owner_slack=None)
    if needs_closer:
        c = next(_closer_cycle)
        return dict(owner=c["name"], owner_kind="closer", owner_slack=c["slack"], owner_calendar=c["calendar"])
    if lead.get("tier") == "C":
        # Still gets an automated touch and a human follow-up, but not a 5-minute human call.
        return dict(owner=o["smb_rep"]["name"], owner_kind="rep_light", owner_slack=o["smb_rep"]["slack"])
    return dict(owner=o["smb_rep"]["name"], owner_kind="rep", owner_slack=o["smb_rep"]["slack"])


def backup():
    return cfg()["owners"]["backup"]
