"""Exact-context reviewed display/attribution adapters; no numerical catalogue."""
from functools import lru_cache
from ctgov_result_rules import load_rules
from ctgov_results import digest, semantic_context
from ctgov_indicator_context import indicator_key, parent_indicator_key
import live_result_identity as identity

@lru_cache(maxsize=2)
def _load(sig):
    return load_rules(sig[0][0])["outcomes"]

def context_contract(study, outcome):
    path = identity.ROOT / "vocabularies/crosswalks/ctgov-results-rules.ttl"
    if not path.exists():
        return {}
    key = indicator_key(study["protocolSection"]["identificationModule"]["nctId"], outcome)
    configs = [r for r in _load(identity.signature([path]))
               if parent_indicator_key(r.get("key", "")) == key and r.get("live_context")]
    if not configs:
        return {}
    fingerprint = digest(semantic_context(study, outcome))
    if any(r.get("context_sha256") != fingerprint for r in configs):
        return {}
    contracts = {digest(r["live_context"]): r["live_context"] for r in configs}
    return next(iter(contracts.values())) if len(contracts) == 1 else {}
