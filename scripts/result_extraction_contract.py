"""Validate optional, source-reported arm statistics without imputing missing data.

Legacy rows remain valid. New versioned extraction packets explicitly account for
the assigned coverage, so figure-only or unextracted cells cannot masquerade as
completed extraction. This validates declarations, not scientific truth.
"""
import math

MISSING = {'not-reported', 'not-applicable', 'not-extracted'}


def number(value, label, minimum=None, maximum=None, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(label + ' must be a finite number')
    if integer and not isinstance(value, int):
        raise ValueError(label + ' must be an integer')
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ValueError(label + ' is out of range')


def validate_arm(arm):
    if not isinstance(arm, dict) or arm.get('status') not in MISSING | {'reported'}:
        raise ValueError('Arm statistic requires an explicit status')
    for key in ('arm', 'timepoint', 'source_locator'):
        if not isinstance(arm.get(key), str) or not arm[key].strip():
            raise ValueError('Arm statistic requires ' + key)
    if arm['status'] != 'reported':
        if not arm.get('reason') or any(k in arm for k in ('n', 'events', 'percent', 'mean', 'sd')):
            raise ValueError('Missing arm statistic requires reason and no invented numbers')
        return
    if not any(k in arm for k in ('n', 'events', 'percent', 'mean', 'sd')):
        raise ValueError('Reported arm statistic requires a measurement')
    for key in ('n', 'events'):
        if key in arm:
            number(arm[key], key, 1 if key == 'n' else 0, integer=True)
    if 'events' in arm and ('n' not in arm or arm['events'] > arm['n']):
        raise ValueError('Events require a matching denominator and cannot exceed it')
    if 'percent' in arm:
        number(arm['percent'], 'percent', 0, 100)
    if 'mean' in arm:
        number(arm['mean'], 'mean')
    if 'sd' in arm:
        number(arm['sd'], 'sd', 0)
        if arm.get('sd_source') not in ('study-sample', 'instrument-norm', 'not-reported'):
            raise ValueError('SD requires its source')
    if any(k in arm for k in ('mean', 'sd')) and not arm.get('unit'):
        raise ValueError('Continuous arm statistic requires unit')


def validate_finding(finding):
    comparison = finding.get('comparison') or {}
    arms = comparison.get('arm_statistics')
    if arms is not None:
        if not isinstance(arms, dict) or set(arms) != {'intervention', 'comparator'}:
            raise ValueError('arm_statistics requires intervention and comparator roles')
        if comparison.get('kind') != 'between-groups':
            raise ValueError('Comparison arm statistics require a between-groups finding')
        for arm in arms.values():
            validate_arm(arm)
        if comparison.get('baseline') != arms['comparator']:
            raise ValueError('baseline compatibility alias must equal the comparator statistic')
    timing = finding.get('timing') or {}
    if 'age_at_assessment_months' in timing:
        number(timing['age_at_assessment_months'], 'age_at_assessment_months', 0)
        if timing.get('age_statistic') not in ('mean', 'median', 'scheduled'):
            raise ValueError('Assessment age requires mean, median or scheduled statistic')
    uncertainty = finding.get('uncertainty') or {}
    if 'cluster_adjusted' in uncertainty and not (type(uncertainty['cluster_adjusted']) is bool or uncertainty['cluster_adjusted'] is None):
        raise ValueError('cluster_adjusted must be true, false or null (unknown)')


def validate_coverage(submission):
    coverage = submission.get('extraction_coverage')
    if coverage is None:
        return  # Historical sealed packets are not retroactively invalidated.
    if not isinstance(coverage, dict) or coverage.get('version') != 1 or not coverage.get('scope'):
        raise ValueError('Extraction coverage requires version 1 and explicit assigned scope')
    entries = coverage.get('entries')
    if not isinstance(entries, list) or not entries:
        raise ValueError('Extraction coverage requires source sections')
    complete = submission.get('disposition') == 'results-found'
    for entry in entries:
        if not entry.get('locator') or not entry.get('document_id'):
            raise ValueError('Coverage entry requires document and locator')
        status = entry.get('status')
        if status not in {'extracted', 'not-reported', 'not-applicable', 'not-extracted', 'out-of-scope', 'inaccessible'}:
            raise ValueError('Unknown coverage status')
        if status == 'extracted' and not entry.get('finding_ids'):
            raise ValueError('Extracted coverage requires finding identifiers')
        if status != 'extracted' and not entry.get('reason'):
            raise ValueError('Coverage omission requires a reason')
        if complete and status == 'not-extracted':
            raise ValueError('Unextracted assigned results cannot be declared complete')
    if complete:
        for finding in submission.get('batch', {}).get('findings', []):
            for arm in (finding.get('comparison', {}).get('arm_statistics') or {}).values():
                if arm['status'] == 'not-extracted':
                    raise ValueError('Unextracted assigned arm statistics cannot be declared complete')
