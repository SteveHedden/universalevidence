"""Capability routing for numerical results, independent of registry discovery."""
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
ALIASES = {'CT.gov':'ctgov','ClinicalTrials.gov':'ctgov','ISRCTN':'isrctn','AEA':'aea','WHO ICTRP':'who-ictrp'}
CONFIG = Path(__file__).resolve().parents[1] / 'config/result-sources.json'

def no_posted_results(study):
    """Only explicit absence permits fallback; missing/error responses do not."""
    return (study.get('hasResults') is False
            and not study.get('resultsSection'))

def fallback_ids(config):
    ref = config.get('api_fallback_evidence')
    if not ref:
        return set()
    root = CONFIG.parent.parent
    path = (root / ref['path']).resolve()
    if not path.is_relative_to(root.resolve()):
        return set()
    try:
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref['sha256']:
            return set()
        capture = json.loads(raw)
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(capture['captured_at'])).total_seconds()
        if not 0 <= age <= ref.get('max_age_seconds', 86400):
            return set()
        return {s['protocolSection']['identificationModule']['nctId']
                for s in capture['studies'] if no_posted_results(s)}
    except (OSError, ValueError, KeyError, TypeError):
        return set()

def source_lane(source, config=None):
    config = config if config is not None else json.loads(CONFIG.read_text())
    return config['sources'].get(ALIASES.get(source, source), {}).get('results_lane', 'unresolved')

def study_lane(study, config=None):
    config = config if config is not None else json.loads(CONFIG.read_text())
    records = [*study.get('registry_records', []), *study.get('records', [])]
    if study.get('source'): records.append(study)
    sources = [r.get('source') for r in records]
    lanes = {source_lane(s, config) for s in sources}
    if 'api-result-mapping' in lanes:
        api_records = [r for r in records if source_lane(r.get('source'), config) == 'api-result-mapping']
        allowed = ({sid for sid, eligible in study['api_result_checks'].items() if eligible is True}
                   if 'api_result_checks' in study else fallback_ids(config))
        if api_records and all(ALIASES.get(r.get('source'), r.get('source')) == 'ctgov'
                               and r.get('study_id', r.get('record_id')) in allowed for r in api_records):
            return 'local-result-extraction'
        return 'api-result-mapping'
    return next(iter(lanes)) if len(lanes) == 1 else 'unresolved'

def local_eligible(study, config=None):
    return study_lane(study, config) == 'local-result-extraction'

def enrichment_result_lane(payload, config):
    """Teacher project hook; rechecks the captured API evidence at claim/commit."""
    return study_lane(payload)
