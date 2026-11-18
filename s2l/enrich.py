"""Enrichment with a hard timeout. If it cannot finish inside the budget, the lead moves on without it.

Swap `_lookup` for Clay / Apollo / Clearbit. The contract: take a lead, return a dict of fields to merge, or {}.
The timeout is the whole point: enrichment must never be the reason a first touch is late.
"""
import hashlib
import threading

# Deterministic mock so demos are repeatable. Real implementation: HTTP call to an enrichment provider.
_PROVIDERS = ["mercury", "brex", "legacy_bank", "none", "ramp"]
_STAGES = ["pre_seed", "seed", "series_a_plus", "revenue_smb"]
_BANDS = ["lt_25k", "25k_100k", "100k_500k", "500k_plus"]


def _lookup(lead):
    domain = lead.get("domain")
    if not domain:
        return {}
    h = int(hashlib.sha1(domain.encode()).hexdigest(), 16)
    out = {"enriched": True, "company_site": f"https://{domain}"}
    if lead.get("current_provider") in (None, "unknown"):
        out["current_provider"] = _PROVIDERS[h % len(_PROVIDERS)]
    if lead.get("stage") in (None, "unknown"):
        out["stage"] = _STAGES[(h // 7) % len(_STAGES)]
    if lead.get("balance_band") in (None, "unknown"):
        out["balance_band"] = _BANDS[(h // 13) % len(_BANDS)]
    if lead.get("industry") in (None, "unknown"):
        out["industry"] = ["saas", "consumer_brand", "agency", "fintech", "marketplace"][(h // 17) % 5]
    return out


def enrich(lead, timeout_seconds=30.0):
    result = {}
    done = threading.Event()

    def run():
        try:
            result.update(_lookup(lead))
        except Exception as e:  # never let enrichment break the pipeline
            result["enrich_error"] = str(e)
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    if not done.wait(timeout_seconds):
        return {"enriched": False, "enrich_error": f"timeout after {timeout_seconds}s"}
    return result
