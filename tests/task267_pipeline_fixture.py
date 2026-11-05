"""Exercise the real discovery/projection boundary for backend and UI regressions."""
import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch
APP = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(APP / 'scripts'), str(APP)]
import query_v2 as q
from live_results import assemble
P = 'https://universalevidence.com/'
S = P + 'studies/ctgov/NCT12345678'
A, B = [P + 'vocab/states/Example' + name for name in ('A', 'B')]
I = P + 'vocab/interventions/Example'


def pipeline(case, reverse=False):
    def source(value, sha, intervention=I, state=A):
        return {'source': 'CT.gov', 'study_id': 'NCT12345678',
                'intervention_concept_uri': intervention,
                'api_results': {'status': 'available', 'sha256': sha, 'rows': [{
                    'study': S, 'measurement': S + '/outcome/one', 'states': [state],
                    'indicator': A, 'indicator_label': 'Example score', 'value': value,
                    'direction': 'increase', 'unit': 'z-score', 'result_interventions': [intervention],
                    'uncertainty': {'kind': 'confidence-interval', 'level': 95, 'lower': .01, 'upper': .12},
                    'summary_mapping': {'study': S, 'measurement': S + '/outcome/one',
                                        'indicator': A, 'unit': 'z-score', 'tested': [intervention],
                                        'comparator': [], 'background': []}}]}}
    left, right = source('0.07', 'version1'), source('99', 'version2')
    values = {'intervention': [I]}
    if case == 'branches':
        right = source('99', 'version2', I + '2', B)
        values = {'state': [A], 'intervention': [I]}
    elif case == 'identical':
        right = source('0.07', 'version2', state=B)
    elif case == 'same_hash':
        right['api_results']['sha256'] = 'version1'
    inputs = [right, left] if reverse else [left, right]
    original = copy.deepcopy(inputs)
    with patch.object(q, '_local_state_labels', return_value={A: 'A', B: 'B'}):
        result = assemble({'results': q.dedupe_presentation(inputs), 'meta': {}}, q.canonical_query(values, {}))
    assert inputs == original
    return result


if __name__ == '__main__':
    print(json.dumps({case: pipeline(case) for case in ('versions', 'branches', 'same_hash', 'identical')}))
