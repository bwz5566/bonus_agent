"""Structural warnings must arrive before computation without inventing policy."""
from bonus_agent.lint import compile_warnings
from bonus_core.dsl.models import Report, RuleSet


def rules(window='period'):
    return RuleSet.model_validate({'name':'generic','version':'1',
        'datasets':{'entries':{'period':'month_key','window':window,'fields':{'month_key':{'type':'text'},'value':{'type':'money'}}}},
        'tables':{'unused':{'kind':'tier','mode':'whole','steps':[[0,1]]}},
        'variables':{'mean':{'expr':'sum(value, of: "entries") / count_distinct(month_key, of: "entries")'}},
        'allocation':[],'pool':{'expr':'mean'}})


def test_current_period_count_is_warned_but_accumulated_window_is_not():
    assert any('最多为1' in x for x in compile_warnings(rules()))
    assert not any('最多为1' in x for x in compile_warnings(rules('year_to_date')))


def test_unused_table_warning_respects_cross_scheme_usage_and_report_usage():
    rs=rules()
    assert any('unused' in x for x in compile_warnings(rs))
    assert not any('unused' in x for x in compile_warnings(rs,{'unused'}))
    rs=rs.model_copy(update={'report': Report(
        details=[{'name':'inspection','level':'global','expr':'tier_rate(unused, 1)'}])})
    assert not any('unused' in x for x in compile_warnings(rs))
