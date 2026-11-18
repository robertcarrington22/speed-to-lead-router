"""Transparent additive scoring. Returns the score, the breakdown, the tier, and whether to suppress."""
import json
import os

_CFG = None


def rubric():
    global _CFG
    if _CFG is None:
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "scoring.json")
        with open(path, encoding="utf-8") as f:
            _CFG = json.load(f)
    return _CFG


def score(lead):
    r = rubric()
    breakdown = {}

    dq = r["disqualify"]
    if lead.get("entity_type") in dq["entity_types"]:
        return dict(score=0, breakdown={}, tier="suppress", suppressed=True, suppress_reason=f"entity_type={lead.get('entity_type')}")
    if lead.get("country") and lead["country"] not in dq["countries_not"]:
        return dict(score=0, breakdown={}, tier="suppress", suppressed=True, suppress_reason=f"country={lead.get('country')}")
    if lead.get("industry") in dq["industries"]:
        return dict(score=0, breakdown={}, tier="suppress", suppressed=True, suppress_reason=f"industry={lead.get('industry')}")

    for key in ("balance_band", "stage", "intent", "current_provider"):
        table = r[key]
        val = lead.get(key) or "unknown"
        pts = table.get(val, table.get("unknown", 0))
        breakdown[f"{key}={val}"] = pts

    # The bank's own taxonomy, scored only when present so synthetic leads are not inflated.
    for key in ("inbound_type", "lead_source"):
        val = lead.get(key)
        if val and val in r.get(key, {}):
            breakdown[f"{key}={val}"] = r[key][val]

    b = r["bonuses"]
    if lead.get("multi_client"):
        breakdown["multi_client_partner"] = b["multi_client_partner"]
    if lead.get("referral"):
        breakdown["referral"] = b["referral"]
    if lead.get("phone"):
        breakdown["phone_provided"] = b["phone_provided"]
    if lead.get("book_call_requested"):
        breakdown["booked_call_requested"] = b["booked_call_requested"]

    total = sum(breakdown.values())
    tiers = r["tiers"]
    tier = "A" if total >= tiers["A"] else ("B" if total >= tiers["B"] else "C")
    return dict(score=total, breakdown=breakdown, tier=tier, suppressed=False, suppress_reason=None)


def needs_closer(lead):
    """Tier A and balance at or above the configured band goes to the closing team."""
    if lead.get("tier") != "A":
        return False
    order = ["unknown", "lt_25k", "25k_100k", "100k_500k", "500k_plus"]
    min_band = rubric()["closer_handoff"]["min_balance_band"]
    return order.index(lead.get("balance_band") or "unknown") >= order.index(min_band)
