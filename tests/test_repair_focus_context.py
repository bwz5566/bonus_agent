"""Global failures regain a bounded repair focus without inventing ownership or changing gates."""
import copy
import json

import pytest

from bonus_agent.construction import TaskBoard
from bonus_agent.context import task_card, _fact_evidence
from tests.test_auto_basis_compile import ready_workspace
from tests.test_staged_workflow import make_workspace

pytestmark = pytest.mark.evidence


def card(ws):
    return json.loads(task_card(ws))


def current_failure(ws, issues):
    ws._flow()._save_compile_report({'status': 'FAIL', 'phase': 'basis', 'issues': issues,
                                    'warnings': [], 'graph': {}, 'repaired': [], 'preflight': {}})


def test_unowned_checked_parameter_gets_one_focus_and_neutral_task_index(tmp_path):
    ws = ready_workspace(tmp_path, unbound=True)
    narrative = ws.facts[0].model_copy(deep=True, update={'name': '不参与数值的说明', 'value': '按名单分配', 'targets': []})
    ws.facts.insert(0, narrative)
    ws._flow().recheck(initial_compile=True)
    report = ws.workflow['formula_report']['report']
    assert report['status'] == 'FAIL'
    assert not any(message.startswith(narrative.name + '：') for message in report['issues'])
    before = copy.deepcopy(ws.workflow['construction'])
    result = card(ws); focus = result['repair_focus']
    assert result['workflow']['construction']['active'] is None
    assert focus['kind'] == 'parameter_ownership' and focus['fact'] == '待绑定提成率'
    assert focus['remaining_issues'] == len(report['issues']) - 1
    assert len(result['current_task_evidence']['facts']) == 1
    assert result['current_task_evidence']['facts'][0]['quote'] == ws.facts[-1].quote
    assert all('target' in item and 'deferred' in item for item in focus['candidate_tasks'])
    assert '相同数字' in focus['instruction'] and 'plan_formula_tasks' in focus['plan_usage']
    assert len(result['workflow']['construction']['compile_failure']['issues']) == 1
    assert ws.workflow['construction'] == before


def test_initial_discovery_and_stale_failure_do_not_invent_a_focus(tmp_path):
    ws = make_workspace(tmp_path)
    assert 'repair_focus' not in card(ws)
    (tmp_path / 'ready').mkdir()
    ws = ready_workspace(tmp_path / 'ready', unbound=True)
    ws._flow().recheck(initial_compile=True)
    ws.workflow['formula_report']['phase_version'] = 'old-code-or-source'
    assert 'repair_focus' not in card(ws)


def test_explicit_active_task_is_not_displaced_by_unrelated_global_error(tmp_path):
    ws = ready_workspace(tmp_path, unbound=True)
    board = TaskBoard(ws)
    ident = next(n for n, task in board.state['tasks'].items() if task['target'] == '/pool')
    board.state['receipts'].pop(ident)
    ws.compile_formulas()
    result = card(ws)
    assert result['workflow']['construction']['active']['id'] == ident
    assert 'repair_focus' not in result


def test_exact_target_wins_but_example_paths_and_substrings_do_not(tmp_path):
    ws = ready_workspace(tmp_path, unbound=True)
    report_task = next(t for t in TaskBoard(ws).state['tasks'].values() if t['target'] == '/report')
    issues = ['待绑定提成率：缺少精确规则位置。示例 report.totals 不代表此参数归属',
              '方案「S」有多项汇总但未声明 report.balances 对账关系']
    current_failure(ws, issues)
    result = card(ws)
    assert result['repair_focus']['kind'] == 'task'
    assert result['repair_focus']['task']['id'] == report_task['id']
    assert result['repair_focus']['issue'] == issues[1]
    current_failure(ws, [issues[0]])
    assert card(ws)['repair_focus']['kind'] == 'parameter_ownership'
    current_failure(ws, ['没有完整report；请参考 report.totals 示例'])
    assert card(ws)['repair_focus']['kind'] == 'compile_issue'


def test_declared_ownership_and_verified_binding_recompute_focus(tmp_path):
    ws = ready_workspace(tmp_path, unbound=True)
    ws._flow().recheck(initial_compile=True)
    assert card(ws)['repair_focus']['kind'] == 'parameter_ownership'
    board = TaskBoard(ws)
    owner = next(copy.deepcopy(t) for t in board.state['tasks'].values() if t['target'] == '/pool')
    owner['facts'] = ['待绑定提成率']
    ws.plan_formula_tasks([owner])
    result = card(ws)
    assert result['workflow']['construction']['active']['id'] == owner['id']
    assert 'repair_focus' not in result  # The real current task now owns the problem.
    ws.bind_facts([{'name': '待绑定提成率', 'targets': ['S::/pool/expr']}])
    assert ws.check_formula_task(owner['id'])['status'] == 'PASS'
    assert ws.compile_formulas()['status'] == 'PASS'
    assert 'repair_focus' not in card(ws)


def test_multiple_explicit_owners_are_ambiguous_and_deferred_is_only_metadata(tmp_path):
    ws = ready_workspace(tmp_path, unbound=True)
    board = TaskBoard(ws)
    owners = [t for t in board.state['tasks'].values() if t['target'] in ('/report', '/allocation')]
    for task in owners:
        task['facts'] = ['待绑定提成率']
    # Test presentation of an already current compiler diagnostic independently of task scheduling.
    current_failure(ws, ['待绑定提成率：缺少精确规则位置'])
    from bonus_agent.context import _repair_focus
    focus = _repair_focus(ws, ws._flow(), {'active': None})
    assert focus['kind'] == 'parameter_owner_ambiguous' and focus['candidate_count'] == 2
    assert {t['target']: t['deferred'] for t in focus['candidate_tasks']} == {'/allocation': True, '/report': False}
    assert not ws.facts[-1].targets


def test_existing_unit_evidence_and_exact_financial_excerpt_are_reused(tmp_path):
    ws = ready_workspace(tmp_path)
    ws.said = '无关前言\n本案 policy.md 的提成率单位为百分数（%）。\n用户已确认该单位。\n其他无关口径'
    basis = {'file': '财务对话', 'quote': '本案 policy.md 的提成率单位为百分数（%）。'}
    fact = ws.facts[0].model_copy(deep=True, update={'unit_basis': basis,
                        'source_cell': {'page': 1, 'table': 1, 'row': 2, 'column': 2}})
    before = fact.model_dump()
    evidence = _fact_evidence(ws, fact)
    assert evidence['unit_basis'] == basis and evidence['source_cell'] == fact.source_cell
    assert evidence['existing_financial_context']['excerpts'] == ['本案 policy.md 的提成率单位为百分数（%）。\n用户已确认该单位。']
    assert evidence['existing_financial_context']['next_call']['args'] == {'file': '财务对话'}
    assert fact.model_dump() == before
