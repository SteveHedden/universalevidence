"""Reviewed, reusable IATI matching; no organization or activity catalogue.

Only explicitly approved rules are served. Candidate bundles are usable solely by
an injected offline/review harness, never through the public route's configuration.
"""
from __future__ import annotations
import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from scripts import iati_actors as actors

ROOT = Path(__file__).resolve().parents[1]
RULE_VERSION = "iati-live-1"
MATCH_CONTRACT_VERSION = "iati-relations-2"
# Conservative English-only semantics. Unsupported languages remain unconfirmed.
NEGATIVE = re.compile(r"\b(no|not|without|neither|unrelated|exclude[ds]?|excluding|discontinued)\b", re.I)
RESEARCH = re.compile(r"\b(research|study|studies|trial|trials|randomi[sz]ed|evaluate|evaluation)\b", re.I)
OBJECTIVE = re.compile(r"\b(reduc(?:e|es|ing|tion)|prevent(?:ion|ing|s)?|address(?:es|ing)?|combat|treat(?:ment|ing)?|improv(?:e|es|ing)|provid(?:e|es|ing)|distribut(?:e|es|ing|ion)|deliver(?:s|ing|y)?|implement(?:s|ing|ation)?|support(?:s|ing)?|supply|supplies)\b", re.I)
BACKGROUND = re.compile(r"\b(prevalence|prevalent|suffer|suffers|suffering|affected|background|survey|estimated|percent|%)", re.I)

# Relation grammar is intentionally small. These are reviewed-candidate semantics,
# not a dependency parser or permission to infer delivery from nearby verbs.
DIRECT_VERB = r"(?:provide[sd]?|distribute[sd]?|deliver(?:s|ed)?|supply|supplies|supplied|administer(?:s|ed)?|prescribe[sd]?|disburse[sd]?)"
DETERMINER = r"(?:(?:the|these|those|some|daily|weekly|monthly|regular|free|oral|[0-9,]+)\s+){0,3}"
ACTIVE_PROVISION = re.compile(r"\b" + DIRECT_VERB + r"\s+" + DETERMINER + r"$", re.I)
PASSIVE_PROVISION = re.compile(r"^\s+(?:is|are|was|were)\s+(?:being\s+)?(?:provided|distributed|delivered|supplied|administered|prescribed|disbursed)\b", re.I)
# An information/advice/proposal frame cannot establish actual delivery, even when
# it embeds an otherwise well-formed phrase such as 'to provide supplements'.
NON_DELIVERY = re.compile(
    r"\b(?:information|inform(?:s|ed|ing)?|advice|advis(?:e[sd]?|ing)|guidance|"
    r"awareness|advoca(?:cy|te[sd]?|ting)|refer(?:ral|rals|red|ring)?|"
    r"discuss(?:ion|ions|es|ed|ing)?|debate[sd]?|mention(?:s|ed|ing)?|"
    r"explain(?:s|ed|ing)?|describ(?:e[sd]?|ing)|recommend(?:s|ed|ation|ations|ing)?|"
    r"counsel(?:s|led|ling|ing)?|train(?:s|ed|ing)?|teach(?:es|ing)?|"
    r"encourag(?:e[sd]?|ing)|promot(?:e[sd]?|ing|ion)|campaign(?:s|ing)?|"
    r"plan(?:s|ned|ning)?|proposal|propos(?:e[sd]?|ing)|intend(?:s|ed|ing)?|"
    r"aim(?:s|ed|ing)?|seek(?:s|ing)?|consider(?:s|ed|ing)?|"
    r"procure(?:s|d|ment)?|procuring|purchas(?:e[sd]?|ing)|buy(?:s|ing)?|"
    r"prepar(?:e[sd]?|ing|ation)|scheduled|expected|potential|possible|"
    r"may|might|could|would|should|will|whether|if)\b", re.I)


def phrase_pattern(phrase):
    return r"(?<!\w)" + re.escape(phrase).replace(r"\ ", r"\s+") + r"(?!\w)"


def relation(sentence, rule):
    """Return an auditable relation decision, separate from lexical retrieval."""
    contract = rule.get('text_relation')
    if contract == 'condition-objective-v1':
        explicit = bool(OBJECTIVE.search(sentence)) and not BACKGROUND.search(sentence)
        return explicit, 'condition-objective' if explicit else 'condition-context'
    if contract != 'direct-provision-v1':
        return False, 'unsupported-relation-contract'
    # Mask only the matched concept terms so a legitimate intervention called
    # 'nutrition education' does not become its own information-context exclusion.
    framing = sentence
    for phrase in rule.get('phrases', []):
        framing = re.sub(phrase_pattern(phrase), 'INTERVENTION', framing, flags=re.I)
    if NON_DELIVERY.search(framing) or '?' in sentence:
        return False, 'information-advice-referral-or-planned-context'
    for phrase in rule.get('phrases', []):
        for hit in re.finditer(phrase_pattern(phrase), sentence, re.I):
            before, after = sentence[:hit.start()], sentence[hit.end():]
            active = ACTIVE_PROVISION.search(before)
            if active and not re.search(r'\bto\s*$', before[:active.start()], re.I):
                return True, 'intervention-is-direct-object-of-provision'
            if PASSIVE_PROVISION.search(after):
                return True, 'intervention-is-passive-subject-of-provision'
    return False, 'no-supported-provision-relation'


def contains(text, phrase):
    return re.search(phrase_pattern(phrase), text, re.I) is not None


class RuleBook:
    def __init__(self, path=None, *, candidate_review=False):
        path = path or ROOT / "vocabularies/rules/iati-organizations.json"
        self.payload = json.loads(Path(path).read_text())
        if self.payload.get("schema") != RULE_VERSION:
            raise ValueError("Unsupported IATI rule schema")
        self.version = hashlib.sha256(Path(path).read_bytes() + RULE_VERSION.encode() + MATCH_CONTRACT_VERSION.encode()).hexdigest()
        self.rules = {}
        self.candidate_review = candidate_review
        for rule in self.payload.get("mappings", []):
            review = rule.get("review", {})
            if review.get("status") != "approved" and not (candidate_review and review.get("status") == "candidate"):
                continue
            if not all(review.get(k) for k in ("reviewer", "date", "rationale", "sources")):
                raise ValueError("IATI rule lacks review provenance")
            axis, uri = rule["axis"], rule["concept"]
            actors.concept(uri, axis)
            expected = 'condition-objective-v1' if axis == 'state' else 'direct-provision-v1'
            if rule.get('phrases') and rule.get('text_relation') != expected:
                raise ValueError('IATI lexical mapping needs an explicit supported relation contract')
            if not rule.get("phrases") and not rule.get("codes"):
                raise ValueError("IATI rule has no reusable source terms")
            if len(rule.get("phrases", [])) > 30 or any(not isinstance(p, str) or not p or len(p)>120 for p in rule.get("phrases", [])):
                raise ValueError("Invalid bounded IATI phrase rule")
            for code in rule.get("codes", []):
                if not all(code.get(k) for k in ("code", "vocabulary", "relation", "lifecycle")):
                    raise ValueError("Incomplete controlled vocabulary identity")
                if code["vocabulary"] in ("98", "99") and not code.get("publisher_ref"):
                    raise ValueError("Local IATI code needs publisher scope")
                if code["relation"] not in ("exact", "broad"):
                    raise ValueError("Unsupported code relationship")
            if (axis, uri) in self.rules:
                raise ValueError("Duplicate IATI concept rule")
            self.rules[axis, uri] = rule

    def get(self, axis, uri):
        return self.rules.get((axis, uri)) if uri else None


def quote(term):
    return '"' + term.replace('\\', '\\\\').replace('"', '\\"') + '"'


def query(rule):
    terms = [f"{field}:{quote(phrase)}" for field in ("title_narrative", "description_narrative") for phrase in rule.get("phrases", [])]
    # These are discovery filters only. Vocabulary/code pairs are checked against
    # the SAME nested sector entry after retrieval, never zipped flat Solr arrays.
    terms += [f"(sector_code:{quote(c['code'])} AND sector_vocabulary:{quote(c['vocabulary'])})" for c in rule.get("codes", [])]
    return "(" + " OR ".join(terms) + ")"


def assess(item, rule):
    support = []
    rejected = False
    research_title = any(RESEARCH.search(t) for t in actors.narratives(item['source']['titles']))
    for field in ("titles", "descriptions"):
        for i, entry in enumerate(item['source'][field]):
            for j, narrative in enumerate(entry.get('narrative', [])):
                text = narrative.get('text()', '')
                lang = narrative.get('@xml:lang', item['source'].get('language') or 'en')
                if not isinstance(text, str) or not str(lang).lower().startswith('en'):
                    continue
                for sentence in re.split(r"[.!;\n]+", text):
                    if not any(contains(sentence, p) for p in rule.get('phrases', [])):
                        continue
                    excluded = any(contains(sentence, p) for p in rule.get('exclude_phrases', []))
                    if excluded or NEGATIVE.search(sentence) or RESEARCH.search(sentence) or research_title:
                        rejected = True
                        continue
                    explicit, reason = relation(sentence, rule)
                    support.append({'path': [field, i, 'narrative', j, 'text()'], 'text': sentence.strip(),
                                    'kind': 'objective' if explicit else 'context', 'concept': rule['concept'],
                                    'relation_contract': rule.get('text_relation'), 'relation_reason': reason})
    for i, sector in enumerate(item['source']['sectors']):
        for code in rule.get('codes', []):
            published = sector['published']
            if sector['level'] != 'activity' or str(published.get('@code')) != code['code'] or str(sector['vocabulary']) != code['vocabulary']:
                continue
            if code.get('vocabulary_uri') != sector['vocabulary_uri'] or code.get('version') != sector['version']:
                continue
            if code.get('publisher_ref') and code['publisher_ref'] not in [p.get('@ref') for p in sector['publisher']]:
                continue
            support.append({'path': ['sectors', i], 'value': sector, 'code_rule': code,
                            'kind': 'objective' if code['relation']=='exact' else 'broad', 'concept': rule['concept']})
    return {'explicit': not rejected and any(s['kind']=='objective' for s in support),
            'relevant': not rejected and any(s['kind'] in ('objective', 'context') for s in support),
            'rejected_context': rejected, 'support': support}


def freshness(item, today=None):
    today = today or datetime.now(timezone.utc).date()
    source = item['source']
    statuses = {str(x.get('@code')) for x in source['status']}
    dates, issues = {}, []
    for entry in source['dates']:
        kind = str(entry.get('@type'))
        try:
            value = date.fromisoformat(entry['@iso-date'])
        except (ValueError, KeyError, TypeError):
            issues.append('invalid-date'); continue
        dates.setdefault(kind, set()).add(value)
    if any(len(v)>1 for v in dates.values()) or len(statuses)>1:
        issues.append('conflicting-metadata')
    # An unambiguous actual end/closed status excludes current-work cards.
    ended = bool(statuses and statuses <= {'3','4','5'}) or (len(dates.get('4', set()))==1 and next(iter(dates['4']))<=today)
    if ended:
        return {'status': 'ended', 'issues': issues, 'dates': source['dates'], 'updated_at': source['source_updated_at']}
    if statuses != {'2'} or not dates.get('2') or not dates.get('3') or not source['source_updated_at']:
        issues.append('incomplete-current-metadata')
    if any(d>today for d in dates.get('2',set())) or dates.get('4'):
        issues.append('contradictory-dates')
    if any(d<today for d in dates.get('3',set())):
        issues.append('planned-end-passed')
    if dates.get('2') and dates.get('3') and max(dates['2'])>min(dates['3']):
        issues.append('end-before-start')
    if source['source_updated_at']:
        try:
            updated = datetime.fromisoformat(source['source_updated_at'].replace('Z','+00:00')).date()
            if updated>today or (today-updated).days>365: issues.append('update-old-or-future')
        except (ValueError, TypeError): issues.append('invalid-update')
    return {'status': 'unverified' if issues else 'consistent-current', 'issues': sorted(set(issues)),
            'dates': source['dates'], 'updated_at': source['source_updated_at']}


def safe_url(value):
    if isinstance(value, str) and urlsplit(value).scheme in ('https','http') and urlsplit(value).netloc:
        return value
    return None


def activity(doc, retrieved_at, state_rule, intervention_rule):
    item = actors.normalize(doc, retrieved_at)
    source = item['source']
    if len(actors.canonical(source)) > 100_000 or len(source['participants']) > 200:
        raise ValueError('Activity matching metadata exceeds processing cap')
    raw = doc['iati_json']
    if isinstance(raw, str): raw=json.loads(raw)
    raw=raw['iati-activity'][0]
    item['project_url'] = next((url for x in raw.get('activity-website',[]) if (url:=safe_url(x.get('text()')))), None)
    item['freshness'] = freshness(item)
    item['matches'] = {axis: assess(item, rule) for axis, rule in [('state',state_rule),('intervention',intervention_rule)] if rule}
    return item
