import pytest
from hypothesis import example, given, strategies as st

from bonus_agent.indicator_confidence import assess_indicator, repair_priority_key


DIMENSIONS = ('rule_source', 'grain', 'source_period', 'execution', 'independent')


def evidence(states):
    return {name: {'state': state, 'reason': f'case/review/{name}'}
            for name, state in states.items()}


def test_empty_evidence_lists_all_unknown_checks():
    assessment = assess_indicator('/variables/metric')
    assert assessment['level'] == 'UNKNOWN'
    assert set(assessment['gaps']) == set(DIMENSIONS)
    assert all(record['state'] == 'UNKNOWN' and record['reason']
               for record in assessment['dimensions'].values())


def test_execution_does_not_substitute_for_semantics_or_independent_review():
    receipts = evidence({'execution': 'VERIFIED', 'rule_source': 'VERIFIED'})
    partial = assess_indicator('a', receipts)
    assert partial['level'] == 'LOW'
    assert set(partial['gaps']) == {'grain', 'source_period', 'independent'}

    receipts.update(evidence({'grain': 'VERIFIED', 'source_period': 'VERIFIED'}))
    supported = assess_indicator('a', receipts)
    assert supported['level'] == 'SUPPORTED'
    receipts.update(evidence({'independent': 'VERIFIED'}))
    checked = assess_indicator('a', receipts)
    assert checked['level'] == 'HIGH'
    assert repair_priority_key(partial) < repair_priority_key(supported) < repair_priority_key(checked)
    assert partial['dimensions']['grain']['state'] == 'UNKNOWN'


@given(st.dictionaries(st.sampled_from(DIMENSIONS), st.sampled_from(('UNKNOWN', 'PARTIAL', 'VERIFIED'))))
@example(dict.fromkeys(DIMENSIONS, 'VERIFIED'))
def test_counterexample_outranks_unchecked_dependencies_even_after_positive_reviews(states):
    seen = assess_indicator('a', evidence(states), counterexamples=['sheet!A3 differed', 'sheet!A3 differed'])
    assert seen['level'] == 'CONFLICT'
    assert seen['counterexamples'] == ['sheet!A3 differed']
    assert repair_priority_key(seen) < repair_priority_key(assess_indicator('b'))


@pytest.mark.parametrize('dimension', DIMENSIONS)
def test_conflicting_check_cannot_be_averaged_away(dimension):
    states = dict.fromkeys(DIMENSIONS, 'VERIFIED')
    states[dimension] = 'CONFLICT'
    seen = assess_indicator('a', evidence(states))
    assert seen['level'] == 'CONFLICT'
    assert seen['gaps'] == [dimension]
    assert seen['dimensions'][dimension]['reason'] == f'case/review/{dimension}'


@pytest.mark.parametrize('invalid', (
    {'model_confidence': {'state': 'VERIFIED', 'reason': 'model gave itself 100%'}},
    {'grain': {'state': 'PASS', 'reason': 'task complete'}},
    {'execution': {'state': 'VERIFIED', 'reason': '  '}},
))
def test_unsupported_states_or_missing_support_do_not_upgrade_indicators(invalid):
    with pytest.raises(ValueError):
        assess_indicator('a', invalid)
