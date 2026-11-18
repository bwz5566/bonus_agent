"""Real evidence checks for saved-draft recovery; never call a provider."""
import copy

import pytest

from bonus_agent.construction import TaskBoard
from tests.test_staged_workflow import make_workspace, prepared

pytestmark = pytest.mark.evidence


def test_equal_value_candidate_does_not_make_an_unrelated_fact_a_local_blocker(tmp_path):
    ws = make_workspace(tmp_path)
    ws.prepare_data()
    ws.record_facts([{'name': '另一模块的参数', 'file': 'policy.md', 'value': '10%',
                      'quote': '销售金额的10%'}])
    ws.plan_formula_tasks([{'id': 'pool', 'scheme': 'S', 'target': '/pool', 'facts': ['提成率']}])
    assert ws.check_formula_task('pool')['status'] == 'PASS'
    # Isolation must not waive the missing evidence at whole-program compilation.
    assert any('另一模块的参数' in gap for gap in ws._binding_gaps())
    assert ws.compile_formulas()['status'] == 'FAIL'


def test_description_and_unrelated_source_do_not_invalidate_local_receipts(tmp_path):
    ws = prepared(make_workspace(tmp_path))
    board = TaskBoard(ws)
    before = board.statuses()
    ws.datasets['people'] = ws.datasets['people'].model_copy(update={'desc': '名单说明'})
    assert board.statuses() == before
    ws.define_dataset('unused', 'data.xlsx', 'projects', {'unused_value': {'column': 'amount', 'type': 'money'}})
    pool = next(n for n,s in board.state['tasks'].items() if s['target'] == '/pool')
    assert board.statuses()[pool]['version'] == before[pool]['version']
    # A real change to the amount mapping is an executable change and must invalidate it.
    ws.mapping['projects'].columns['amount'] = 'pid'
    assert board.statuses()[pool]['version'] != before[pool]['version']


def test_stale_receipts_are_rechecked_by_program_in_dependency_order(tmp_path):
    ws = prepared(make_workspace(tmp_path))
    board = TaskBoard(ws)
    pool = next(n for n,s in board.state['tasks'].items() if s['target'] == '/pool')
    old = board.state['receipts'][pool]
    ws.drafts['S']['pool']['expr'] = 'amount * 0.1 + 0'
    assert board.statuses()[pool]['status'] == 'PENDING'
    assert board.summary()['active'] is None
    assert board.state['receipts'][pool] != old
    assert all(s['status'] == 'PASS' for s in board.statuses().values())
    assert not ws.llm.calls


def test_existing_and_new_components_are_enrolled_before_final_compile(tmp_path):
    ws = make_workspace(tmp_path)
    ws.plan_formula_tasks([{'id': 'pool', 'scheme': 'S', 'target': '/pool'}])
    ws.drafts['S']['variables'] = {'extra': {'expr': 'amount', 'scope': '项目'}}
    board = TaskBoard(ws)
    board.summary()
    assert not any('未安排局部检查任务' in gap for gap in board.coverage_gaps())
    extra = next(n for n,s in board.state['tasks'].items() if s['target'] == '/variables/extra')
    assert board.statuses()[extra]['status'] == 'PENDING'
    # The model can take ownership of an automatically enrolled component atomically.
    ws.plan_formula_tasks([{'id': 'extra', 'scheme': 'S', 'target': '/variables/extra'}])
    assert extra not in board.state['tasks'] and 'extra' in board.state['tasks']


def test_missing_structure_cannot_approve_a_formula_and_component_reads_are_targeted(tmp_path):
    ws = make_workspace(tmp_path)
    allocation = copy.deepcopy(ws.drafts['S'].pop('allocation'))
    ws.plan_formula_tasks([{'id': 'pool', 'scheme': 'S', 'target': '/pool'}])
    assert 'pool' in TaskBoard(ws).summary()['next']
    failed = ws.check_formula_task('pool')
    assert failed['status'] == 'FAIL'
    assert any('scope_bad' in issue for issue in failed['issues'])
    ws.drafts['S']['allocation'] = allocation
    assert ws.check_formula_task('pool')['status'] == 'PASS'
    got = ws.read_workspace('rules', scheme='S', query='allocation')
    assert got['items'][0]['definition'] == allocation
    assert 'tables' not in str(got)


def test_large_component_keeps_its_scope_across_fragment_pages(tmp_path):
    ws = make_workspace(tmp_path)
    ws.drafts['S']['allocation'] = [{'level': '层级' + str(i), 'key': 'key' + str(i),
        'desc': '分配对象说明' * 20} for i in range(40)]
    first = ws.read_workspace('rules', scheme='S', query='allocation')
    call = first['next_call']['args']
    assert call['query'] == 'allocation'
    second = ws.read_workspace(**call)
    assert second['item_fragment']['offset'] > 0
    assert second['item_fragment']['text']
