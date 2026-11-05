"""Live CT.gov results -> transient UE/CIDS application-profile RDF.

No database/file writes, AI, or implicit statistical comparisons. Callers supply
reviewed indicator crosswalks and versioned semantic-context rules. See
ops/task258/CONTRACT.md for the supported profile and review boundary.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.request
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from rdflib import Graph, Literal, Namespace, RDF, RDFS, URIRef, XSD
from rdflib.namespace import DCTERMS, SKOS
try:
    from .ctgov_indicator_context import indicator_key, parent_indicator_key
except ImportError:
    from ctgov_indicator_context import indicator_key, parent_indicator_key

UE = Namespace('https://universalevidence.com/ontology/')
CIDS = Namespace('https://ontology.commonapproach.org/cids#')
I72 = Namespace('http://ontology.eil.utoronto.ca/ISO21972/iso21972#')
OBO = Namespace('http://purl.obolibrary.org/obo/')
PROV = Namespace('http://www.w3.org/ns/prov#')
BASE = 'https://clinicaltrials.gov/api/v2/studies/'
ROOT = '/resultsSection/outcomeMeasuresModule/outcomeMeasures'
VERSION = 'ctgov-results-v3'
NUMERIC = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def number(value):
    """No coercion of thresholds, NaN, missing data, commas or prose to scalars."""
    if isinstance(value, bool) or value is None or not NUMERIC.fullmatch(str(value).strip()):
        return None
    try:
        result = Decimal(str(value).strip())
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


class SourceDataError(ValueError):
    """Invalid external payload structure, not an adapter programming error."""


def validate_source(study, expected_id=None):
    """Validate containers before iterating them; absent optional fields are OK.

    Explicit null containers and malformed rows are failures, not empty data.
    Numeric strings (including thresholds) are assessed separately per component.
    """
    def obj(value, path):
        if not isinstance(value, dict):
            raise SourceDataError(f'{path}: expected an object')
        return value

    def child(parent, key, path, required=False):
        if key not in parent and not required:
            return {}
        return obj(parent.get(key), path+'/'+key)

    def array(parent, key, path, objects=True):
        values = parent.get(key, [])
        if not isinstance(values, list):
            raise SourceDataError(f'{path}/{key}: expected an array')
        for i, value in enumerate(values):
            if objects:
                obj(value, f'{path}/{key}/{i}')
            elif not isinstance(value, str):
                raise SourceDataError(f'{path}/{key}/{i}: expected text')
        return values

    def fields(parent, names, path, required=()):
        for key in names:
            value = parent.get(key)
            if key in required and (not isinstance(value, str) or not value.strip()):
                raise SourceDataError(f'{path}/{key}: expected nonempty text')
            if value is not None and not isinstance(value, str):
                raise SourceDataError(f'{path}/{key}: expected text or null')

    def scalars(parent, names, path):
        for key in names:
            if parent.get(key) is not None and not isinstance(parent[key], (str, int, float)):
                raise SourceDataError(f'{path}/{key}: expected a scalar result or null')

    def denoms(parent, path):
        for i, d in enumerate(array(parent, 'denoms', path)):
            dp = f'{path}/denoms/{i}'
            fields(d, ('units',), dp)
            for j, count in enumerate(array(d, 'counts', dp)):
                cp = f'{dp}/counts/{j}'
                fields(count, ('groupId',), cp, ('groupId',))
                scalars(count, ('value',), cp)

    obj(study, '/')
    protocol = child(study, 'protocolSection', '', required=True)
    identity = child(protocol, 'identificationModule', '/protocolSection', required=True)
    sid = identity.get('nctId')
    if not isinstance(sid, str) or not re.fullmatch(r'NCT\d{8}', sid):
        raise SourceDataError('/protocolSection/identificationModule/nctId: invalid study ID')
    if expected_id is not None and sid != expected_id:
        raise SourceDataError('Source study identity mismatch')
    if 'hasResults' in study and not isinstance(study['hasResults'], bool):
        raise SourceDataError('/hasResults: expected a boolean')
    for key in ('eligibilityModule', 'designModule', 'armsInterventionsModule'):
        child(protocol, key, '/protocolSection')
    results = child(study, 'resultsSection', '')
    module = child(results, 'outcomeMeasuresModule', '/resultsSection')
    for oi, outcome in enumerate(array(module, 'outcomeMeasures', ROOT.rsplit('/', 1)[0])):
        path = f'{ROOT}/{oi}'
        fields(outcome, ('title', 'description', 'timeFrame', 'populationDescription',
                        'unitOfMeasure', 'paramType', 'dispersionType', 'reportingStatus'), path, ('title',))
        for i, group in enumerate(array(outcome, 'groups', path)):
            fields(group, ('id', 'title', 'description'), f'{path}/groups/{i}', ('id',))
        denoms(outcome, path)
        for ci, cl in enumerate(array(outcome, 'classes', path)):
            cp = f'{path}/classes/{ci}'
            fields(cl, ('title',), cp)
            denoms(cl, cp)
            for ki, cat in enumerate(array(cl, 'categories', cp)):
                kp = f'{cp}/categories/{ki}'
                fields(cat, ('title',), kp)
                for mi, m in enumerate(array(cat, 'measurements', kp)):
                    mp = f'{kp}/measurements/{mi}'
                    fields(m, ('groupId',), mp, ('groupId',))
                    scalars(m, ('value', 'spread', 'lowerLimit', 'upperLimit'), mp)
        for ai, a in enumerate(array(outcome, 'analyses', path)):
            ap = f'{path}/analyses/{ai}'
            array(a, 'groupIds', ap, objects=False)
            fields(a, ('paramType', 'dispersionType', 'statisticalMethod', 'statisticalComment', 'groupDescription'), ap)
            scalars(a, ('paramValue', 'pValue', 'ciPctValue', 'ciLowerLimit', 'ciUpperLimit', 'dispersionValue'), ap)


PERCENT_UNITS = {'%', 'percent', 'percentage', 'percentage of participants'}
DISPERSION_TYPES = {
    'Standard Deviation': OBO.STATO_0000237, 'STANDARD_DEVIATION': OBO.STATO_0000237,
    'Standard Error': OBO.STATO_0000037, 'STANDARD_ERROR_OF_MEAN': OBO.STATO_0000037,
}


def numerical_validation(record):
    """Recheck mutable numeric slots, independently of semantic mapping support."""
    raw, ctx, rule = record['raw'], record['context'], record['comparison']
    unit = (record['unit'] or '').strip().lower()

    def scalar(value, domain=None):
        x = number(value)
        if value is None:
            return {'status': 'missing', 'reason': 'Not supplied'}
        if x is None:
            return {'status': 'nonscalar', 'reason': 'Threshold or nonnumeric source value'}
        reason = None
        if domain == 'percent' and not 0 <= x <= 100:
            reason = 'Percentage must be between 0 and 100'
        elif domain == 'ratio' and x <= 0:
            reason = 'Ratio estimate must be strictly positive'
        elif domain == 'nonnegative' and x < 0:
            reason = 'Value must be nonnegative'
        elif domain == 'count' and (x < 0 or x != x.to_integral_value()):
            reason = 'Participant count must be a nonnegative integer'
        elif domain == 'probability' and not 0 <= x <= 1:
            reason = 'Probability must be between 0 and 1'
        return {'status': 'invalid' if reason else 'valid', 'reason': reason}

    # Percent ranges constrain levels, not differences or changes in percentages.
    domain = ('ratio' if rule.get('statistic') == 'ratio' else
              'percent' if (record['kind'] == 'arm_level' and unit in PERCENT_UNITS
                            or record['kind'] == 'reported_arm_measurement'
                            and unit == 'percentage of participants') else
              'count' if record['kind'] == 'arm_level' and ctx.get('statistic') == 'COUNT_OF_PARTICIPANTS' else None)
    estimate = scalar(record.get('source_value', record['value']), domain)
    dispersion = raw.get('dispersionType') or ctx.get('dispersionType')
    spread = scalar(raw.get('dispersionValue', raw.get('spread')), 'nonnegative')
    if dispersion not in DISPERSION_TYPES:
        spread = {'status': 'unsupported', 'reason': 'No supported SD/SE type supplied'}
    match = re.fullmatch(r'(\d+(?:\.\d+)?)% Confidence Interval', dispersion or '', re.I)
    is_ci = match is not None or any(k in raw for k in ('ciLowerLimit', 'ciUpperLimit'))
    ci = {'status': 'missing', 'reason': 'No explicitly identified interval supplied'}
    if is_ci:
        low, high = raw.get('ciLowerLimit', raw.get('lowerLimit')), raw.get('ciUpperLimit', raw.get('upperLimit'))
        level = raw.get('ciPctValue', match.group(1) if match else None)
        bound_domain = 'nonnegative' if domain == 'ratio' else domain
        lower, upper = scalar(low, bound_domain), scalar(high, bound_domain)
        reasons = [x['reason'] for x in (lower, upper) if x['status'] in ('invalid', 'nonscalar')]
        if number(low) is not None and number(high) is not None and number(low) > number(high):
            reasons.append('Confidence lower bound exceeds upper bound')
        if level is not None and (number(level) is None or not 0 < number(level) <= 100):
            reasons.append('Confidence level must be numeric, greater than 0 and at most 100')
        ci = {'status': 'invalid' if reasons else 'missing' if low is None and high is None else 'valid',
              'reason': '; '.join(reasons) if reasons else 'No bounds supplied' if low is None and high is None else None,
              'lower': lower, 'upper': upper}
    return {'estimate': estimate, 'confidence_interval': ci, 'dispersion': spread,
            'p_value': scalar(raw.get('pValue'), 'probability')}


def semantic_context(study, outcome):
    """Pin all source context except explicitly enumerated numerical result slots.

    Changed structure, grouping, units, methods, population, titles or footnotes
    invalidate support. A value-only revision reuses the review, never old values.
    """
    o = deepcopy(outcome)
    def denoms(obj):
        for d in obj.get('denoms', []):
            for c in d.get('counts', []):
                c.pop('value', None)
    denoms(o)
    for cl in o.get('classes', []):
        denoms(cl)
        for cat in cl.get('categories', []):
            for m in cat.get('measurements', []):
                for k in ('value', 'spread', 'lowerLimit', 'upperLimit'):
                    m.pop(k, None)
    for a in o.get('analyses', []):
        for k in ('paramValue', 'pValue', 'ciLowerLimit', 'ciUpperLimit', 'dispersionValue'):
            a.pop(k, None)
    protocol = study.get('protocolSection', {})
    return {'outcome': o, 'protocol': {k: protocol.get(k) for k in
        ('eligibilityModule', 'designModule', 'armsInterventionsModule')}}


def classify(value, rule):
    """Direction of the named quantity, never direction of a broad state/benefit."""
    result = {'direction': 'unknown/unsupported', 'quantity': rule.get('quantity'),
              'orientation': rule.get('orientation'), 'reason': None}
    def unknown(reason):
        return dict(result, reason=reason)
    if not rule.get('quantity') or rule.get('semantics') != 'explicit':
        return unknown('Quantity semantics have not been explicitly reviewed')
    if rule.get('kind') not in ('within_arm_change', 'source_comparison', 'derived_comparison'):
        return unknown('An arm level has no comparative direction')
    statistic = rule.get('statistic')
    if statistic not in ('difference', 'ratio', 'log_ratio'):
        return unknown('Unsupported statistic')
    if rule.get('orientation') not in ('forward', 'reverse'):
        return unknown('Unknown comparison orientation')
    if rule.get('scale') not in ('identity', 'explicit_log_ratio', 'measured_transformed', 'measured_reverse_coded'):
        return unknown('Unsupported transformation or reverse coding')
    if statistic == 'log_ratio' and rule.get('scale') != 'explicit_log_ratio':
        return unknown('Log ratio not explicitly identified')
    if statistic != 'log_ratio' and rule.get('scale') == 'explicit_log_ratio':
        return unknown('Statistic and scale disagree')
    x = number(value)
    if x is None:
        return unknown('Missing, threshold or nonnumeric estimate')
    if statistic == 'ratio' and x <= 0:
        return unknown('Ratio must be strictly positive')
    null = Decimal(1) if statistic == 'ratio' else Decimal(0)
    sign = (x > null) - (x < null)
    if rule['orientation'] == 'reverse':
        sign = -sign
    result.update(direction='increase' if sign > 0 else 'decrease' if sign < 0 else 'exactly-zero-difference',
                  reason='Point estimate relative to the statistic null; uncertainty is separate',
                  null=str(null), reported_precision=str(value),
                  zero_interpretation='Reported null at source precision; not proof of an exact underlying null' if sign == 0 else None)
    return result


@dataclass(frozen=True)
class Response:
    payload: dict
    sha256: str
    retrieved_at: str
    fetch_seconds: float
    url: str


class LiveClient:
    """Request-scoped reuse. Bounded timeout, body size and number of requests."""
    def __init__(self, max_requests=3, timeout=20, opener=urllib.request.urlopen):
        if not 1 <= max_requests <= 10 or not 0 < timeout <= 30:
            raise ValueError('Request cap must be 1..10; timeout must be >0 and <=30 seconds')
        self.max_requests, self.timeout, self.opener = max_requests, timeout, opener
        self.cache, self.requests = {}, 0

    def get(self, study_id):
        if not re.fullmatch(r'NCT\d{8}', study_id):
            raise ValueError('Invalid CT.gov ID')
        if study_id in self.cache:
            return self.cache[study_id]
        if self.requests >= self.max_requests:
            raise ValueError('CT.gov request cap exhausted')
        self.requests += 1
        start = time.perf_counter()
        with self.opener(BASE + study_id, timeout=self.timeout) as handle:
            raw = handle.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ValueError('CT.gov response exceeds 8 MB limit')
        payload = json.loads(raw)
        validate_source(payload, study_id)
        response = Response(payload, hashlib.sha256(raw).hexdigest(),
                            datetime.now(timezone.utc).isoformat(), time.perf_counter()-start, BASE+study_id)
        self.cache[study_id] = response
        return response


def resolve_mapping(study, outcome, crosswalk, taxonomy, rules, class_index=None, category_index=None):
    sid = study['protocolSection']['identificationModule']['nctId']
    try:
        key = indicator_key(sid, outcome, class_index, category_index)
    except ValueError as e:
        return None, str(e)
    if class_index is not None:
        parent = indicator_key(sid, outcome)
        if any(crosswalk.subjects(UE.rawText, Literal(parent))) or any(r.get('key') == parent for r in rules.get('outcomes', [])):
            return None, 'Overlapping outcome and class mappings require review'
    if class_index is None:
        scoped_keys = [*crosswalk.objects(None, UE.rawText), *(r.get('key', '') for r in rules.get('outcomes', []))]
        if any(str(k) != key and parent_indicator_key(str(k)) == key for k in scoped_keys):
            return None, 'Outcome mapping overlaps class-specific scope'
    if class_index is not None:
        from urllib.parse import unquote
        import json
        scoped_keys = [*crosswalk.objects(None, UE.rawText), *(r.get('key', '') for r in rules.get('outcomes', []))]
        class_key=indicator_key(sid,outcome,class_index)
        if category_index is not None and any(str(k)==class_key for k in scoped_keys):
            return None,'Overlapping class and category mappings require review'
        if category_index is None:
            for k in scoped_keys:
                if parent_indicator_key(str(k))!=indicator_key(sid,outcome):continue
                ctx=json.loads(unquote(str(k).split('|',1)[1]))
                if ctx.get('class_selector',{}).get('index')==class_index and 'category_selector' in ctx:
                    return None,'Class has category-specific scope'
    entries = list(crosswalk.subjects(UE.rawText, Literal(key)))
    matches = [(entry, target) for entry in entries for pred in (SKOS.exactMatch, SKOS.closeMatch)
               for target in crosswalk.objects(entry, pred)]
    if len(matches) != 1:
        return None, 'Missing or ambiguous indicator crosswalk'
    entry, target = matches[0]
    if (target, RDF.type, UE.Indicator) not in taxonomy:
        return None, 'Crosswalk target is not a declared Indicator'
    if (list(taxonomy.objects(target, SKOS.broader)) or list(taxonomy.objects(target, SKOS.narrower))
        or list(taxonomy.subjects(SKOS.broader, target)) or list(taxonomy.subjects(SKOS.narrower, target))):
        return None, 'Indicator is not flat'
    configs = [x for x in rules.get('outcomes', []) if x.get('key') == key]
    if len(configs) != 1:
        return None, 'Missing or ambiguous semantic review'
    cfg = configs[0]
    if class_index is not None and (cfg.get('analysis_rules') or cfg.get('derived')):
        return None, 'Class mappings currently support arm measurements only'
    if cfg.get('context_sha256') != digest(semantic_context(study, outcome)):
        return None, 'Source semantic context changed; fresh review required'
    state = URIRef(cfg.get('state', ''))
    if (target, UE.measures, state) not in taxonomy or (state, RDF.type, UE.State) not in taxonomy:
        return None, 'Reviewed measured-State link is not declared'
    if cfg.get('indicator') != str(target) or not cfg.get('review_reason'):
        return None, 'Indicator review does not support this target'
    return dict(cfg, entry=str(entry), mapping_sha256=digest({'entry':str(entry),'key':key,'target':str(target),'predicates':sorted(str(p) for p in (SKOS.exactMatch,SKOS.closeMatch) if (entry,p,target) in crosswalk)})), None


def map_response(response, crosswalk, taxonomy, rules):
    study = response.payload
    validate_source(study)
    sid = study['protocolSection']['identificationModule']['nctId']
    records, coverage = [], []
    scoped_parents = {parent_indicator_key(str(key)) for key in
        [*crosswalk.objects(None, UE.rawText), *(r.get('key', '') for r in rules.get('outcomes', []))]
        if parent_indicator_key(str(key)) != str(key)}
    outcomes = study.get('resultsSection', {}).get('outcomeMeasuresModule', {}).get('outcomeMeasures', [])
    for oi, o in enumerate(outcomes):
        path = ROOT + '/' + str(oi)
        class_scoped = indicator_key(sid, o) in scoped_parents
        cfg, reason = (None, 'Class-scoped outcome; individual sections require review') if class_scoped else resolve_mapping(study, o, crosswalk, taxonomy, rules)
        parent_cfg, parent_reason = cfg, reason
        section_coverage = []
        posted = study.get('hasResults') is True and o.get('reportingStatus') == 'POSTED'
        if not posted:
            reason = 'Outcome numerical results are not POSTED'
        coverage.append({'study': sid, 'outcome': path, 'title': o.get('title'),
                         'supported': bool(cfg and posted), 'reason': reason})
        group_ids = [x.get('id') for x in o.get('groups', [])]
        if len(set(group_ids)) != len(group_ids):
            reason, cfg = 'Duplicate source group IDs', None
            coverage[-1].update(supported=False, reason=reason)
        groups = {x.get('id'): x for x in o.get('groups', [])}
        outcome_records = []
        def row(pointer, raw, kind, rule, context, value, unit):
            support = cfg if posted else None
            classification = classify(value, dict(rule, kind=kind)) if support else {
                'direction': 'unknown/unsupported', 'reason': reason, 'quantity': rule.get('quantity')}
            result = {'study': sid, 'outcome': path, 'locator': pointer, 'title': o.get('title'),
                      'kind': kind, 'value': value, 'source_value': value, 'unit': unit, 'raw': raw, 'context': context,
                      'classification': classification, 'mapping': support,
                      'comparison': dict(rule, kind=kind), 'reported': posted}
            result['numerical_validation'] = numerical_validation(result)
            if result['numerical_validation']['estimate']['status'] != 'valid':
                result['value'] = None
                if support:
                    result['classification'] = {'direction': 'unknown/unsupported',
                        'quantity': rule.get('quantity'),
                        'reason': result['numerical_validation']['estimate']['reason']}
            result['numerically_usable'] = bool(support and result['value'] is not None and unit)
            records.append(result); outcome_records.append(result)
            return result
        for ci, cl in enumerate(o.get('classes', [])):
            if class_scoped:
                cfg, reason = resolve_mapping(study, o, crosswalk, taxonomy, rules, ci)
                if len(set(group_ids)) != len(group_ids):
                    cfg, reason = None, 'Duplicate source group IDs'
                if reason!='Class has category-specific scope':
                    section_coverage.append({'locator': f'{path}/classes/{ci}', 'title': cl.get('title'),
                        'supported': bool(cfg and posted), 'reason': reason if posted else 'Outcome numerical results are not POSTED'})
            class_cfg,class_reason=cfg,reason
            for ki, cat in enumerate(cl.get('categories', [])):
                if class_scoped:
                    cfg,reason=resolve_mapping(study,o,crosswalk,taxonomy,rules,ci,ki) if class_reason=='Class has category-specific scope' else (class_cfg,class_reason)
                    if len(set(group_ids))!=len(group_ids):cfg,reason=None,'Duplicate source group IDs'
                    if class_reason=='Class has category-specific scope':
                        section_coverage.append({'locator':f'{path}/classes/{ci}/categories/{ki}','title':cat.get('title'),'supported':bool(cfg and posted),'reason':reason})
                for mi, m in enumerate(cat.get('measurements', [])):
                    pointer = f'{path}/classes/{ci}/categories/{ki}/measurements/{mi}'
                    rule = (cfg or {}).get('arm_rule', {})
                    kind = rule.get('kind', 'arm_level')
                    # Arm rows can never become source-reported comparative effects.
                    if kind not in ('arm_level', 'within_arm_change'):
                        kind, rule = 'arm_level', {}
                    context = {'group': groups.get(m.get('groupId')), 'class': cl.get('title'),
                               'category': cat.get('title'), 'population': o.get('populationDescription'),
                               'timeFrame': o.get('timeFrame'), 'statistic': o.get('paramType'),
                               'dispersionType': o.get('dispersionType'),
                               'outcome_denoms': o.get('denoms'), 'class_denoms': cl.get('denoms')}
                    r = row(pointer, m, kind, rule, context, m.get('value'), o.get('unitOfMeasure'))
                    if context['group'] is None:
                        r['classification'] = {'direction': 'unknown/unsupported', 'reason': 'Unknown source group ID'}
                        r['mapping'] = None
                        r['numerically_usable'] = False
        if class_scoped:
            cfg, reason = None, 'Outcome-level analyses cannot inherit a class indicator'
        for ai, a in enumerate(o.get('analyses', [])):
            rule = (cfg or {}).get('analysis_rules', {}).get(str(ai), {})
            # A reviewed adapter must agree with the exact reported parameter type.
            if rule.get('source_param_type') != a.get('paramType') or not a.get('paramType'):
                rule = {}
            context = {'groups': [groups.get(gid) for gid in a.get('groupIds', [])],
                       'population': o.get('populationDescription'), 'timeFrame': o.get('timeFrame'),
                       'statistic': a.get('paramType'), 'adjustment': a.get('statisticalComment'),
                       'groupDescription': a.get('groupDescription')}
            r = row(f'{path}/analyses/{ai}', a, 'source_comparison', rule, context,
                    a.get('paramValue'), rule.get('unit'))
            if not a.get('groupIds') or any(x is None for x in context['groups']):
                r['classification'] = {'direction': 'unknown/unsupported', 'reason': 'Missing source comparison groups'}
        if class_scoped:
            cfg, reason = parent_cfg, parent_reason
            coverage[-1].update(sections=section_coverage,
                supported=any(s['supported'] for s in section_coverage),
                mapping_complete=bool(section_coverage) and all(s['supported'] for s in section_coverage),
                reason='Class-scoped mapping; see section coverage')
        if cfg and posted:
            for di, rule in enumerate(cfg.get('derived', [])):
                operands = []
                for selector in rule.get('operands', []):
                    hits = [r for r in outcome_records if r['kind'] == 'arm_level' and
                            r['locator'].startswith(path+'/classes/') and
                            {k:r['context'].get(k) for k in ('class','category')} ==
                            {k:selector.get(k) for k in ('class','category')} and
                            r['raw'].get('groupId') == selector.get('groupId')]
                    operands.append(hits[0] if len(hits) == 1 else None)
                valid = (rule.get('formula') == 'left - right' and rule.get('statistic') == 'difference' and rule.get('scale') == 'identity' and len(operands) == 2 and
                         all(x and x['mapping'] and number(x['value']) is not None for x in operands))
                if valid:
                    valid = (operands[0]['unit'] == operands[1]['unit'] == rule.get('source_unit') and
                             operands[0]['raw'].get('groupId') != operands[1]['raw'].get('groupId') and
                             operands[0]['context']['class'] == operands[1]['context']['class'] and
                             operands[0]['context']['category'] == operands[1]['context']['category'])
                if valid and rule.get('source_unit') == 'percentage of participants':
                    valid = all(0 <= number(x['value']) <= 100 for x in operands)
                value = str(number(operands[0]['value'])-number(operands[1]['value'])) if valid else None
                context = {'operands': [x['locator'] for x in operands if x],
                           'operand_values': [x['value'] for x in operands if x],
                           'groups': [x['context']['group'] for x in operands if x],
                           'timeFrame': o.get('timeFrame'), 'population': o.get('populationDescription'),
                           'class': (rule.get('operands') or [{}])[0].get('class'),
                           'limitations': 'Unadjusted subtraction of rounded source summaries; no effect CI or p-value derived'}
                r = row(f'{path}/derived/{di}', {}, 'derived_comparison', rule, context, value, rule.get('unit'))
                if not valid:
                    r['classification'] = {'direction':'unknown/unsupported', 'reason':'Missing, invalid or incompatible derived operands'}
        coverage[-1].update(
            mapping_supported=coverage[-1]['supported'],
            usable_numeric_records=sum(r['numerically_usable'] for r in outcome_records),
            numerical_issues=[{'locator': r['locator'], 'component': component, **check}
                for r in outcome_records for component, check in r['numerical_validation'].items()
                if check['status'] in ('invalid', 'nonscalar')])
    return records, coverage


def build_graph(response, records, rules, taxonomy):
    g = Graph()
    for prefix, ns in [('ue',UE),('cids',CIDS),('i72',I72),('obo',OBO),('prov',PROV),('dcterms',DCTERMS)]:
        g.bind(prefix,ns)
    def add(s,p,o): g.add((s,p,o))
    def text(s,p,v):
        if v is not None: add(s,p,Literal(v if isinstance(v,str) else canonical(v)))
    sid = response.payload['protocolSection']['identificationModule']['nctId']
    study = URIRef('https://universalevidence.com/studies/ctgov/'+sid)
    doc = URIRef(response.url+'#sha256-'+response.sha256)
    add(study,RDF.type,UE.Study); text(study,DCTERMS.identifier,'ctgov:'+sid)
    add(doc,RDF.type,UE.ReportDocument); add(doc,RDF.type,PROV.Entity)
    add(doc,DCTERMS.source,URIRef(response.url)); text(doc,DCTERMS.identifier,'ctgov:'+sid)
    text(doc,DCTERMS.hasVersion,response.sha256); text(doc,DCTERMS.bibliographicCitation,response.url)
    add(doc,OBO.IAO_0000136,study); text(doc,UE.identityVerification,{'source_nctId':sid})
    text(doc,UE.reportStatus,'active')
    add(doc,PROV.generatedAtTime,Literal(response.retrieved_at,datatype=XSD.dateTime))
    add(doc,PROV.value,Literal(canonical(response.payload),datatype=RDF.JSON))
    run = URIRef(str(doc)+'/mapping/'+digest(rules)+'/'+VERSION)
    add(run,RDF.type,PROV.Activity); add(run,PROV.used,doc)
    text(run,DCTERMS.hasVersion,VERSION+';rules-sha256='+digest(rules)+';version='+str(rules.get('version')))
    for r in records:
        n = URIRef(str(doc)+r['locator'])
        add(n,RDF.type,PROV.Entity); add(n,PROV.wasDerivedFrom,doc); add(n,PROV.wasGeneratedBy,run)
        text(n,DCTERMS.type,r['kind'])
        text(n,DCTERMS.description,{'classification':r['classification'],'context':r['context'],'raw':r['raw'],
                                  'numerical_validation':r['numerical_validation'],
                                  'numerically_usable':r['numerically_usable']})
        cfg = r['mapping']
        if not cfg: continue
        indicator, state = URIRef(cfg['indicator']), URIRef(cfg['state'])
        # Carry a descriptive projection of existing concepts; never mutate taxonomy.
        pending=[indicator,state];seen=set()
        while pending:
            concept=pending.pop()
            if concept in seen:continue
            seen.add(concept)
            for pred in (RDF.type,SKOS.prefLabel,SKOS.definition,SKOS.inScheme,UE.bearer):
                for obj in taxonomy.objects(concept,pred):
                    add(concept,pred,obj)
                    if pred==UE.bearer:pending.append(obj)
        add(indicator,UE.measures,state)
        add(n,RDF.type,UE.OutcomeMeasurement); add(n,UE.measurementStudy,study)
        add(n,UE.ofIndicator,indicator); add(n,UE.ofState,state)
        text(n,UE.outcomeStatus,'reported'); text(n,UE.measurementStatus,'active')
        text(n,UE.measure,r['title']); text(n,UE.comparison,dict(r['comparison'],context=r['context'],classification=r['classification'],numerical_validation=r['numerical_validation']))
        text(n,UE.timing,{'source':r['context'].get('timeFrame'),'class':r['context'].get('class')})
        text(n,UE.subgroup,r['context'].get('population')); text(n,UE.mapping,cfg['review_reason'])
        direction = {'increase':'positive','decrease':'negative','exactly-zero-difference':'null'}.get(r['classification']['direction'],'uncertain')
        text(n,UE.observedDirection,direction)
        text(n,UE.derivation,{'rule_version':rules.get('version'),'adapter_version':VERSION,
                            'kind':r['kind'],'classification':r['classification'],
                            'formula':r['comparison'].get('formula'),'operands':r['context'].get('operands'),
                            'limitations':r['context'].get('limitations')})
        loc = URIRef(str(doc)+r['outcome'] if r['kind']=='derived_comparison' else str(doc)+r['locator']+'/locator')
        add(n,UE.sourceLocator,loc); add(loc,DCTERMS.source,doc)
        text(loc,DCTERMS.description,{'json_pointer':r['outcome'] if r['kind']=='derived_comparison' else r['locator'],
                                    'raw':r['raw'],'context':r['context']})
        text(loc,UE.supportedFields,['measure','comparison','timing','value','uncertainty','subgroup'])
        add(n,PROV.wasInfluencedBy,URIRef(cfg['entry']))
        text(URIRef(cfg['entry']),DCTERMS.hasVersion,cfg['mapping_sha256'])
        for index,operand in enumerate(r['context'].get('operands',[])):
            derivation=URIRef(str(n)+'/derivation/'+str(index))
            add(n,PROV.qualifiedDerivation,derivation);add(derivation,RDF.type,PROV.Derivation)
            add(derivation,PROV.entity,URIRef(str(doc)+operand))
        co, ci = URIRef(str(n)+'/outcome'), URIRef(str(n)+'/indicator')
        for node, typ, target in [(co,CIDS.Outcome,state),(ci,CIDS.Indicator,indicator)]:
            add(node,RDF.type,typ); code=URIRef(str(node)+'/code')
            add(node,CIDS.hasCode,code); add(code,RDF.type,CIDS.Code)
            add(code,CIDS.hasSpecification,Literal(str(target),datatype=XSD.anyURI))
        add(co,CIDS.hasIndicator,ci); add(ci,CIDS.forOutcome,co)
        text(ci,CIDS.hasName,r['title']); add(n,UE.contextualOutcome,co); add(n,UE.contextualIndicator,ci)
        value=number(r['value']) if r['numerically_usable'] else None
        if value is not None and r['unit']:
            add(n,UE.value,Literal(value,datatype=XSD.decimal)); text(n,UE.unit,r['unit'])
        else:
            text(n,UE.result_text,{'source_value':r['source_value'],'unit':r['unit'],'raw':r['raw'],
                                 'numerical_validation':r['numerical_validation']})
        if r['kind'] != 'derived_comparison':
            text(n,UE.reportedValue,r['source_value']); text(n,UE.reportedUnit,r['unit'])
        text(n,UE.uncertainty,{k:v for k,v in r['raw'].items() if k in (
            'spread','lowerLimit','upperLimit','dispersionType','dispersionValue','ciPctValue',
            'ciNumSides','ciLowerLimit','ciUpperLimit','pValue','pValueComment','statisticalMethod','statisticalComment')})
        if value is not None:
            estimate = URIRef(str(n)+'/estimate'); add(n,UE.estimate,estimate)
            add(estimate,RDF.type,OBO.STATO_0000039)
            add(estimate,OBO.STATO_0000129,Literal(value,datatype=XSD.decimal))
            add(estimate,PROV.wasDerivedFrom,loc)
            text(estimate,DCTERMS.type,r['context'].get('statistic') or r['comparison'].get('statistic'))
            # Only direct indicator levels receive the CIDS IndicatorReport type.
            if r['kind']=='arm_level' and r['unit']:
                add(n,RDF.type,CIDS.IndicatorReport); add(n,CIDS.forIndicator,ci); add(ci,CIDS.hasIndicatorReport,n)
                add(n,I72.value,estimate); add(estimate,RDF.type,I72.Measure)
                text(estimate,I72.numerical_value,str(r['value']))
                text(estimate,I72.hasNumericalValue,str(r['value']))
                unit=URIRef(str(n)+'/unit'); add(unit,RDF.type,I72.Unit_of_measure); text(unit,RDFS.label,r['unit'])
                add(estimate,I72.unit_of_measure,unit); add(n,I72.unit_of_measure,unit)
            add_uncertainty(g, n, estimate, loc, r)
    return g


def add_uncertainty(g, report, estimate, locator, record):
    """Only explicitly identified uncertainty gets a statistical type.

    Missing/threshold components stay in the original JSON. Do not turn SD/SE
    into a CI or copy a significance test to a separately derived contrast.
    """
    raw, ctx = record['raw'], record['context']
    def component(parent, suffix, typ, value):
        node = URIRef(str(parent)+'/'+suffix)
        g.add((parent,DCTERMS.hasPart,node));g.add((node,RDF.type,typ))
        g.add((node,PROV.wasDerivedFrom,locator))
        g.add((node,PROV.value,Literal(str(value))))
        scalar = number(value)
        if scalar is not None:g.add((node,OBO.STATO_0000129,Literal(scalar,datatype=XSD.decimal)))
        return node
    dispersion = raw.get('dispersionType') or ctx.get('dispersionType')
    level = raw.get('ciPctValue')
    match = re.fullmatch(r'(\d+(?:\.\d+)?)% Confidence Interval', dispersion or '', re.I)
    if match:level=match.group(1)
    is_ci = match is not None or any(k in raw for k in ('ciLowerLimit','ciUpperLimit'))
    validation = record['numerical_validation']
    if is_ci and validation['confidence_interval']['status'] == 'valid':
        interval=URIRef(str(estimate)+'/confidence-interval')
        g.add((report,UE.uncertaintyInterval,interval));g.add((interval,RDF.type,OBO.STATO_0000196))
        g.add((interval,OBO.IAO_0000136,estimate));g.add((interval,PROV.wasDerivedFrom,locator))
        for suffix,typ,value in [('lower',OBO.STATO_0000315,raw.get('ciLowerLimit',raw.get('lowerLimit'))),
                                 ('upper',OBO.STATO_0000314,raw.get('ciUpperLimit',raw.get('upperLimit')))]:
            if value is not None:component(interval,suffix,typ,value)
        if number(level) is not None and 0 < number(level) <= 100:
            component(interval,'level',OBO.STATO_0000561,str(number(level)/100))
    spread=raw.get('dispersionValue',raw.get('spread'))
    if validation['dispersion']['status'] == 'valid':
        component(estimate,'dispersion',DISPERSION_TYPES[dispersion],spread)
    if 'pValue' in raw and validation['p_value']['status'] != 'invalid':
        component(report,'p-value',OBO.STATO_0000700,raw['pValue'])


QUERY = '''PREFIX ue: <https://universalevidence.com/ontology/>
PREFIX dcterms: <http://purl.org/dc/terms/>
SELECT ?study ?measurement ?indicator ?kind ?value ?unit ?direction ?comparison ?source WHERE {
 ?measurement a ue:OutcomeMeasurement; ue:measurementStudy ?study; ue:ofIndicator ?indicator;
 dcterms:type ?kind; ue:observedDirection ?direction; ue:comparison ?comparison; ue:sourceLocator ?source.
 OPTIONAL { ?measurement ue:value ?value. } OPTIONAL { ?measurement ue:unit ?unit. }
} ORDER BY ?study ?measurement'''


def query_studies(study_ids, crosswalk, taxonomy, rules, client=None):
    ids = list(dict.fromkeys(study_ids))
    if not 1 <= len(ids) <= 10:
        raise ValueError('Expected 1..10 distinct study IDs')
    client = client or LiveClient(max_requests=len(ids))
    graph, records, coverage, timings, failures, responses = Graph(), [], [], [], [], []
    for sid in ids:
        try:
            response = client.get(sid)
        except (OSError, ValueError) as error:
            failures.append({'study':sid,'stage':'fetch','reason':str(error)})
            coverage.append({'study':sid,'outcome':None,'supported':False,'mapping_supported':False,
                             'usable_numeric_records':0,'reason':str(error)})
            continue
        try:
            validate_source(response.payload, sid)
            start=time.perf_counter(); rows,cov=map_response(response,crosswalk,taxonomy,rules); mapping=time.perf_counter()-start
            start=time.perf_counter(); study_graph = build_graph(response,rows,rules,taxonomy); construction=time.perf_counter()-start
        except SourceDataError as error:
            failures.append({'study':sid,'stage':'source_data','reason':str(error),
                             'source':response.url,'sha256':response.sha256,
                             'raw_response':response.payload})
            coverage.append({'study':sid,'outcome':None,'supported':False,'mapping_supported':False,
                             'usable_numeric_records':0,'reason':str(error)})
            continue
        # Commit an entire study only after validation, mapping and construction succeed.
        graph += study_graph
        responses.append(response)
        records.extend(rows);coverage.extend(cov)
        timings.append({'study':sid,'fetch_seconds':response.fetch_seconds,'mapping_seconds':mapping,'graph_seconds':construction})
    start=time.perf_counter(); results=[{str(k):str(v) for k,v in row.asdict().items()} for row in graph.query(QUERY)]
    return {'graph':graph,'records':records,'coverage':coverage,'failures':failures,'timings':timings,
            'sparql_seconds':time.perf_counter()-start,'rows':results,'responses':responses,
            'study_count':len(responses),'requested_studies':len(ids),'adapter_version':VERSION}
