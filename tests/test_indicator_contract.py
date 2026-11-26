import copy

import pytest

from bonus_agent import case
from bonus_agent.agent import ToolError, Workspace
from bonus_agent.construction import TaskBoard
from bonus_agent.llm import FakeLLM
from tests.test_staged_workflow import make_workspace


def metric(**fields):
    return {'id': 'metric', 'scheme': 'S', 'target': '/variables/sales_base', **fields}


def test_global_formula_is_rejected_at_variable_submission_before_pool(tmp_path):
    ws = make_workspace(tmp_path)
    ws.plan_formula_tasks([metric(clauses=['C1'], expected_scope='项目')])
    valid = ws.submit_formula_task('metric', value={'scope': '项目', 'expr': 'amount'})
    assert valid['saved']
    receipt = TaskBoard(ws).state['receipts']['metric']
    rejected = ws.submit_formula_task('metric', value={'expr': 'sum(amount, of: "projects")'})
    assert not rejected['saved']
    issue = next(issue for issue in rejected['issues'] if 'indicator.scope_mismatch' in issue)
    assert all(text in issue for text in ('C1', '项目', 'global', '/variables/sales_base'))
    assert ws._ruleset('S').variables['sales_base'].scope == '项目'
    assert TaskBoard(ws).state['receipts']['metric'] == receipt


def test_contract_survives_partial_plan_and_resume_and_invalidates_only_affected_receipts(tmp_path):
    ws = make_workspace(tmp_path)
    other = {'id': 'other', 'scheme': 'S', 'target': '/variables/independent_sales'}
    ws.plan_formula_tasks([metric(clauses=['C1'], expected_scope='项目'), other])
    assert ws.submit_formula_task('metric', value={'scope': '项目', 'expr': 'amount'})['saved']
    assert ws.submit_formula_task('other', value={'scope': '项目', 'expr': 'amount'})['saved']
    ws.plan_formula_tasks([metric(title='更新指标说明')])
    resumed = Workspace(FakeLLM([]), ws.inv)
    case.restore(resumed, case.snapshot(ws))
    board = TaskBoard(resumed)
    spec = board.task_spec('metric')
    assert (spec['expected_scope'], spec['clauses']) == ('项目', ['C1'])
    assert resumed.check_formula_task('metric')['status'] == 'PASS'
    resumed.plan_formula_tasks([metric(expected_scope='global')])
    assert board.statuses()['metric']['status'] == 'PENDING'
    assert board.statuses()['other']['status'] == 'PASS'
    assert 'indicator.scope_mismatch' in ' '.join(resumed.check_formula_task('metric')['issues'])


@pytest.mark.parametrize('fields', (
    {'expected_scope': '项目'},
    {'expected_scope': '', 'clauses': ['C1']},
    {'expected_scope': '项目', 'clauses': ['unknown-id']},
    {'target': '/pool', 'expected_scope': '项目', 'clauses': ['C1']},
))
def test_contract_requires_a_variable_and_a_registered_clause(tmp_path, fields):
    ws = make_workspace(tmp_path)
    with pytest.raises(ToolError):
        ws.plan_formula_tasks([metric(**fields)])
    assert 'metric' not in TaskBoard(ws).state['tasks']


def test_retarget_cannot_move_contract_or_add_an_unrelated_task_on_failure(tmp_path):
    ws = make_workspace(tmp_path)
    ws.plan_formula_tasks([metric(clauses=['C1'], expected_scope='项目')])
    original = copy.deepcopy(TaskBoard(ws).state['tasks'])
    with pytest.raises(ToolError, match='construction.scope_contract'):
        ws.plan_formula_tasks([metric(id='second', target='/variables/proposed'),
                               metric(target='/variables/different')])
    assert TaskBoard(ws).state['tasks'] == original


def test_planned_entity_must_exist_in_actual_dsl_before_receipt(tmp_path):
    ws = make_workspace(tmp_path)
    ws.plan_formula_tasks([metric(expected_scope='待建分组', clauses=['C1'])])
    draft = ws.submit_formula_task('metric', value={'scope': '待建分组', 'expr': '1'})
    assert not draft['saved']
    assert '待建分组' in ' '.join(draft['issues'])
    assert 'metric' not in TaskBoard(ws).state['receipts']


def test_legacy_task_does_not_infer_grouping_from_variable_name_or_its_clause(tmp_path):
    ws = make_workspace(tmp_path)
    ws.plan_formula_tasks([metric(clauses=['C1'])])
    result = ws.submit_formula_task('metric', value={'scope': 'global', 'expr': 'sum(amount, of: "projects")'})
    assert result['saved']
    assert 'expected_scope' not in TaskBoard(ws).task_spec('metric')
