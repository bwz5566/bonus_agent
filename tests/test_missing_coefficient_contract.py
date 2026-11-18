"""Missing rows and a zero denominator must not change a final default coefficient."""
from bonus_agent.contracts import ratio_default_gaps
from bonus_core.dsl.models import RuleSet, Variable, SourceRef


def rules(expr):
    return RuleSet(name='generic', version='1', datasets={}, allocation=[], pool={'expr': '0'},
                   variables={'metric': Variable(expr='if(count(of: "rows") == 0, 1, ratio(value, budget))'),
                              'alias': Variable(expr='metric'), 'factor': Variable(expr=expr)})


def test_missing_record_fallback_through_alias_is_not_a_final_coefficient():
    gaps = ratio_default_gaps(rules('tier_rate(C, alias)'))
    assert len(gaps) == 1 and gaps[0][0] == 'variables.factor'


def test_final_fallback_outside_lookup_and_explicit_rate_policy_are_distinct():
    rs = rules('if(count(of: "rows") == 0, 1, tier_rate(C, ratio(value, budget)))')
    assert not ratio_default_gaps(rs)
    rs = rules('tier_rate(C, metric)')
    rs.variables['factor'].ratio_default = 'rate'
    assert ratio_default_gaps(rs)
    rs.variables['factor'].sources = [SourceRef(doc='policy', quote='缺记录时达成率按100%继续查档')]
    assert not ratio_default_gaps(rs)


def test_category_selection_is_not_assumed_to_be_missing_data():
    rs = rules('tier_rate(C, if(category == "A", 1, value))')
    assert not ratio_default_gaps(rs)
