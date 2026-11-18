"""Recompiling table rows must preserve independently confirmed execution policy."""
import copy
from decimal import Decimal as D

import pytest

from bonus_agent.agent import CHAT_SOURCE, ToolError, Workspace
from bonus_agent.auto import build_inventory
from bonus_agent.facts import binding_errors
from bonus_agent.llm import FakeLLM
from bonus_core.engine.evaluator import tier_amount, tier_rate

pytestmark = pytest.mark.evidence


def workspace(tmp_path):
    policy = tmp_path / 'policy.md'
    policy.write_text('低档10%，高档20%，边界100，另一个边界200，默认40%。负值按首档照算。', encoding='utf8')
    ws = Workspace(FakeLLM([]), build_inventory([policy]))
    ws.record_facts([{'name': name, 'value': value, 'file': policy.name, 'quote': quote}
                     for name, value, quote in [('low', '10%', '低档10%'), ('high', '20%', '高档20%'),
                                                ('limit', '100', '边界100'), ('other', '200', '另一个边界200'),
                                                ('default', '40%', '默认40%')]])
    return ws


ROWS = [{'value': 'low'}, {'lower': 'limit', 'value': 'high'}]


def original(ws, **changes):
    table = {'kind': 'tier', 'mode': 'excess', 'below_first': 'first', 'lower_inclusive': False,
             'applies_when': '1 == 1', 'steps': [[0, '.1'], [100, '.2']],
             'sources': [{'doc': 'policy.md', 'quote': '负值按首档照算。'}], **changes}
    ws.set_rules(scheme='S', tables={'rate': table})
    return table


def test_omitted_policies_survive_row_recompile_and_negative_values_stay_negative(tmp_path):
    ws = workspace(tmp_path)
    original(ws)
    ws.compile_table('rate', ROWS, scheme='S')
    table = ws._ruleset('S').tables['rate']
    assert (table.mode, table.lower_inclusive, table.below_first, table.applies_when) == (
        'excess', False, 'first', '1 == 1')
    assert tier_amount(table, D(-50)) == D(-5)
    assert tier_rate(table, D(100)) == D('.1')
    assert any(source.quote == '负值按首档照算。' for source in table.sources)
    before = copy.deepcopy(ws.drafts)
    ws.compile_table('rate', ROWS, scheme='S')
    assert ws.drafts == before
    assert not ws.llm.calls


def test_explicit_false_empty_and_zero_overrides_are_not_treated_as_omission(tmp_path):
    ws = workspace(tmp_path)
    original(ws, lower_inclusive=True)
    ws.compile_table('rate', ROWS, scheme='S', lower_inclusive=False, applies_when='',
                     below_first='zero', mode='whole')
    table = ws._ruleset('S').tables['rate']
    assert (table.lower_inclusive, table.applies_when, table.below_first, table.mode) == (False, '', 'zero', 'whole')
    assert tier_amount(table, D(-50)) == 0
    ws.compile_table('rate', ROWS, scheme='S', lower_inclusive=True)
    assert ws._ruleset('S').tables['rate'].lower_inclusive is True


def test_new_table_defaults_and_same_name_in_other_scheme_do_not_inherit(tmp_path):
    ws = workspace(tmp_path)
    original(ws)
    ws.compile_table('rate', ROWS, scheme='T')
    table = ws._ruleset('T').tables['rate']
    assert (table.kind, table.mode, table.lower_inclusive, table.below_first, table.applies_when) == (
        'tier', 'whole', True, 'zero', '')
    assert ws._ruleset('S').tables['rate'].below_first == 'first'


def test_lookup_kind_default_and_nonrow_parameter_binding_survive(tmp_path):
    ws = workspace(tmp_path)
    ws.set_rules(scheme='S', tables={'rate': {'kind': 'lookup', 'mapping': {'A': '.1'},
                                            'default': '.4', 'applies_when': '1 == 1'}})
    target = 'S::/tables/rate/default'
    ws.bind_facts([{'name': 'default', 'targets': [target]}])
    ws.compile_table('rate', [{'key': 'A', 'value': 'high'}], scheme='S')
    table = ws._ruleset('S').tables['rate']
    assert table.kind == 'lookup' and table.mapping == {'A': D('.2')} and table.default == D('.4')
    fact = next(f for f in ws.facts if f.name == 'default')
    assert fact.targets == [target]
    assert not binding_errors([fact], ws._binding_scopes())


def test_per_step_boundary_only_survives_an_unchanged_lower_sequence(tmp_path):
    ws = workspace(tmp_path)
    original(ws, mode='whole', purpose='coefficient', step_inclusive=[True, False])
    ws.compile_table('rate', ROWS, scheme='S')
    table = ws._ruleset('S').tables['rate']
    assert table.step_inclusive == [True, False] and table.purpose == 'coefficient'
    before = copy.deepcopy((ws.drafts, [f.model_dump() for f in ws.facts]))
    with pytest.raises(ToolError, match='algorithm.boundary'):
        ws.compile_table('rate', [ROWS[0], {'lower': 'other', 'value': 'high'}], scheme='S')
    assert (ws.drafts, [f.model_dump() for f in ws.facts]) == before
    with pytest.raises(ToolError, match='algorithm.boundary'):
        ws.compile_table('rate', ROWS, scheme='S', lower_inclusive=True)


def test_coefficient_rows_rederive_boundaries_without_dropping_applicability_or_its_binding(tmp_path):
    from tests.test_coefficient_compiler import workspace as coefficient_workspace
    ws, args = coefficient_workspace(tmp_path)
    ws.compile_coefficient_table(**args)
    table = copy.deepcopy(ws.drafts['S']['tables']['coefficient'])
    table.update(applies_when='1 == 1', below_first='zero', step_inclusive=[False] * 4)
    ws.set_rules(scheme='S', tables={'coefficient': table})
    ws.said = '适用标记为1。'
    ws.record_facts([{'name': 'applicability', 'value': '1', 'file': CHAT_SOURCE, 'quote': ws.said}])
    target = 'S::/tables/coefficient/applies_when'
    ws.bind_facts([{'name': 'applicability', 'targets': [target]}])
    ws.compile_coefficient_table(**args)
    actual = ws._ruleset('S').tables['coefficient']
    assert actual.applies_when == '1 == 1'
    assert actual.step_inclusive == [True, False, False, True] and actual.below_first == 'first'
    fact = next(f for f in ws.facts if f.name == 'applicability')
    assert fact.targets == [target] and not binding_errors([fact], ws._binding_scopes())
