"""A failed formula proposal must never replace verified rules or evidence."""
import copy

import pytest

from bonus_agent import case
from bonus_agent.construction import TaskBoard
from tests.test_staged_workflow import make_workspace

pytestmark = pytest.mark.evidence


def planned(tmp_path):
    ws = make_workspace(tmp_path)
    ws.prepare_data()
    ws.plan_formula_tasks([{'id': 'base', 'scheme': 'S', 'target': '/variables/base',
                           'facts': ['提成率'], 'clauses': ['C1']}])
    return ws


def bindings(ws):
    fact = ws.facts[0]
    return [{'name': fact.name, 'targets': fact.targets + ['S::/variables/base/expr']}]


def test_submission_saves_evidence_and_receipt_together(tmp_path):
    ws = planned(tmp_path)
    result = ws.submit_formula_task('base', {'scope': '项目', 'expr': 'amount * 0.1'}, bindings(ws))
    assert result['status'] == 'PASS', result
    assert result['saved'] is True
    assert TaskBoard(ws).statuses()['base']['status'] == 'PASS'
    assert ws.drafts['S']['variables']['base']['expr'] == 'amount * 0.1'
    assert 'S::/variables/base/expr' in ws.facts[0].targets
    assert not ws.results and not ws.llm.calls


def test_invalid_constant_rolls_back_formula_binding_and_receipts(tmp_path):
    ws = planned(tmp_path)
    assert ws.submit_formula_task('base', {'scope': '项目', 'expr': 'amount * 0.1'}, bindings(ws))['status'] == 'PASS'
    before = copy.deepcopy(case.snapshot(ws))
    result = ws.submit_formula_task('base', {'scope': '项目', 'expr': 'amount * 0.2'})
    assert result['status'] == 'FAIL' and result['saved'] is False
    assert case.snapshot(ws) == before
    assert TaskBoard(ws).statuses()['base']['status'] == 'PASS'


def test_unknown_dependency_does_not_save_candidate(tmp_path):
    ws = planned(tmp_path)
    before = copy.deepcopy(case.snapshot(ws))
    result = ws.submit_formula_task('base', {'scope': '项目', 'expr': 'missing_amount * 0.1'}, bindings(ws))
    assert result['status'] == 'FAIL' and result['saved'] is False
    assert case.snapshot(ws) == before


def test_partial_evidence_failure_does_not_leave_partial_facts(tmp_path):
    ws = planned(tmp_path)
    before = copy.deepcopy(case.snapshot(ws))
    result = ws.submit_formula_task('base', {'scope': '项目', 'expr': 'amount * 0.1'}, facts=[
        {'name': '重复提成率', 'file': 'policy.md', 'quote': '销售金额的10%', 'value': '10%'},
        {'name': '无来源比例', 'file': 'policy.md', 'quote': '无此原文20%', 'value': '20%'},
    ])
    assert result['status'] == 'FAIL' and result['saved'] is False
    assert case.snapshot(ws) == before


def test_local_submission_automatically_preserves_other_components_evidence(tmp_path):
    ws = planned(tmp_path)
    old_targets = set(ws.facts[0].targets)
    result = ws.submit_formula_task('base', {'scope': '项目', 'expr': 'amount * 0.1'},
                                    [{'name': '提成率', 'targets': ['S::/variables/base/expr']}])
    assert result['status'] == 'PASS', result
    assert set(ws.facts[0].targets) == old_targets | {'S::/variables/base/expr'}


def test_local_submission_cannot_add_unrelated_bindings(tmp_path):
    ws = planned(tmp_path)
    before = copy.deepcopy(case.snapshot(ws))
    result = ws.submit_formula_task('base', {'scope': '项目', 'expr': 'amount * 0.1'},
                                    [{'name': '提成率', 'targets': ['S::/variables/unrelated/expr']}])
    assert result['status'] == 'FAIL' and '当前任务' in str(result['issues'])
    assert case.snapshot(ws) == before
