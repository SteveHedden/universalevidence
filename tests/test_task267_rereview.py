import pytest
from task267_pipeline_fixture import pipeline, A, B


@pytest.mark.parametrize('case', ['versions', 'branches', 'same_hash'])
@pytest.mark.parametrize('reverse', [False, True])
def test_discovery_and_projection_preserve_conflicts_before_filters(case, reverse):
    out = pipeline(case, reverse)
    reviewed = out['reviewed_results']
    assert {r['value'] for r in reviewed['rows']} == {'0.07', '99'}
    assert len(reviewed['rows']) == 2
    assert all(r.get('measurement_conflict') for r in reviewed['rows'])
    assert reviewed['partial']
    assert any(c['status'] == 'conflicting_measurement' for c in reviewed['coverage'])
    if case == 'branches':
        assert all(r['states'] == [A, B] for r in reviewed['rows'])


@pytest.mark.parametrize('reverse', [False, True])
def test_identical_discovery_payloads_still_coalesce(reverse):
    reviewed = pipeline('identical', reverse)['reviewed_results']
    assert len(reviewed['rows']) == 1
    assert reviewed['rows'][0]['states'] == [A, B]
    assert not reviewed['rows'][0].get('measurement_conflict')
    assert not reviewed['partial']
