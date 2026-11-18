"""Changing one property must not recopy or silently replace verified table rows."""
import copy

import pytest

from bonus_agent import case
from bonus_agent.agent import CHAT_SOURCE, ToolError
from bonus_agent.construction import TaskBoard
from bonus_agent.rule_package import submit
from tests.test_rule_package import planned, bindings
from tests.test_staged_workflow import make_workspace

pytestmark = pytest.mark.evidence


def tier_workspace(tmp_path):
    ws = make_workspace(tmp_path)
    ws.prepare_data()
    ws.said = '档位下限：' + '；'.join(f'第{i}档{100 * i}' for i in range(1, 7)) + '。'
    ws.record_facts([{'name': f'下限{i}', 'file': CHAT_SOURCE, 'quote': f'第{i}档{100 * i}', 'value': str(100 * i)}
                     for i in range(1, 7)])
    table = {'kind': 'tier', 'mode': 'whole', 'below_first': 'first', 'lower_inclusive': False,
             'applies_when': 'amount > 0', 'steps': [[100 * i, '0.1'] for i in range(7)],
             'sources': [{'doc': 'policy.md', 'quote': '每个项目按销售金额的10%计算奖金'}]}
    ws.set_rules(scheme='S', tables={'rate': table})
    rate = next(f for f in ws.facts if f.name == '提成率')
    ws.bind_facts([{'name': rate.name, 'targets': [*rate.targets, *[f'S::/tables/rate/steps/{i}/1' for i in range(7)]]},
                   *[{'name': f'下限{i}', 'targets': [f'S::/tables/rate/steps/{i}/0']} for i in range(1, 7)]])
    ws.plan_formula_tasks([{'id': 'rate', 'scheme': 'S', 'target': '/tables/rate',
                           'facts': ['提成率', *[f'下限{i}' for i in range(1, 7)]]}])
    assert ws.check_formula_task('rate')['status'] == 'PASS'
    return ws


def test_single_guard_change_preserves_all_seven_rows_policies_sources_and_bindings(tmp_path):
    ws = tier_workspace(tmp_path)
    before = copy.deepcopy(ws.drafts['S']['tables']['rate'])
    evidence = copy.deepcopy([f.model_dump() for f in ws.facts])
    result = submit(ws, 'rate', changes={'applies_when': 'amount >= 0'})
    assert result['status'] == 'PASS' and result['saved']
    assert result['updated_fields'] == ['applies_when']
    after = ws.drafts['S']['tables']['rate']
    assert after == {**before, 'applies_when': 'amount >= 0'} and len(after['steps']) == 7
    assert [f.model_dump() for f in ws.facts] == evidence
    assert not ws.results and not ws.llm.calls


@pytest.mark.parametrize('changes', [
    {'unknown_policy': True}, {'applies_when': 'unknown_field > 0'},
    {'steps': [[0, '0.1'], [100, '0.1'], [200, '0.1'], [300, '0.1']]},
    {'steps': {'3': [300, '0.1']}},
])
def test_invalid_patch_rolls_back_rules_bindings_and_prior_receipt(tmp_path, changes):
    ws = tier_workspace(tmp_path)
    before = copy.deepcopy(case.snapshot(ws))
    result = submit(ws, 'rate', changes=changes)
    assert result['status'] == 'FAIL' and result['saved'] is False
    assert case.snapshot(ws) == before
    assert TaskBoard(ws).statuses()['rate']['status'] == 'PASS'


def test_explicit_list_property_is_replaced_as_a_whole_not_merged_by_position(tmp_path):
    ws = tier_workspace(tmp_path)
    original = copy.deepcopy(ws.drafts['S']['tables']['rate'])
    replacement = [*original['sources'], {'doc': 'policy.md', 'quote': '按名单比例分配。'}]
    result = submit(ws, 'rate', changes={'sources': replacement})
    assert result['status'] == 'PASS', result
    assert ws.drafts['S']['tables']['rate'] == {**original, 'sources': replacement}


@pytest.mark.parametrize('kwargs', [{'value': {}, 'changes': {'expr': '1'}}, {'changes': {}}, {'changes': []}, {}])
def test_missing_or_conflicting_submission_modes_are_rejected_without_write(tmp_path, kwargs):
    ws = planned(tmp_path)
    before = copy.deepcopy(case.snapshot(ws))
    with pytest.raises(ToolError, match='construction.package'):
        submit(ws, 'base', **kwargs)
    assert case.snapshot(ws) == before


def test_new_object_and_existing_list_target_require_complete_value(tmp_path):
    ws = planned(tmp_path)
    with pytest.raises(ToolError, match='已存在的对象'):
        submit(ws, 'base', changes={'expr': 'amount * .1'})
    ws.plan_formula_tasks([{'id': 'allocation', 'scheme': 'S', 'target': '/allocation'}])
    with pytest.raises(ToolError, match='列表目标'):
        submit(ws, 'allocation', changes={'0': {'level': 'new'}})


def test_existing_variable_patch_keeps_source_and_respects_binding_transaction(tmp_path):
    ws = planned(tmp_path)
    assert submit(ws, 'base', {'scope': '项目', 'expr': 'amount * 0.1'}, bindings(ws))['status'] == 'PASS'
    before = copy.deepcopy(case.snapshot(ws))
    result = submit(ws, 'base', changes={'expr': 'amount * 0.2'})
    assert result['status'] == 'FAIL' and case.snapshot(ws) == before


def test_basis_report_patch_preserves_stored_distribution_configuration(tmp_path):
    ws = make_workspace(tmp_path)
    ws.prepare_data()
    ws.plan_formula_tasks([{'id': 'report', 'scheme': 'S', 'target': '/report'}])
    original = copy.deepcopy(ws.drafts['S']['report'])
    assert original['distributions']
    assert TaskBoard(ws).value(TaskBoard(ws).task_spec('report'))['distributions'] == []
    totals = copy.deepcopy(original['totals'])
    totals[0]['name'] = '项目奖金总计'
    result = submit(ws, 'report', changes={'totals': totals})
    assert result['status'] == 'PASS', result
    assert ws.drafts['S']['report'] == {**original, 'totals': totals}
