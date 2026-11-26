"""Resume real invalid shapes, then complete basis and distribution through public tools."""
import copy
from decimal import Decimal

import pytest

from bonus_agent import case
from bonus_agent.agent import ToolError, Workspace
from bonus_agent.construction import TaskBoard
from bonus_agent.llm import FakeLLM
from bonus_agent.recovery import fingerprint
from tests.test_staged_workflow import make_workspace, prepared


pytestmark = pytest.mark.evidence


def test_restored_receipts_do_not_hide_missing_leaf_entity(tmp_path):
    ws = prepared(make_workspace(tmp_path))
    assert all(s['status'] == 'PASS' for s in TaskBoard(ws).statuses().values())
    # T18's shape: the allocation declares a row entity but its table is only a detail link.
    ws.datasets['people'].key = None
    ws.datasets['people'].link = 'pid'
    clone = Workspace(FakeLLM([]), ws.inv)
    case.restore(clone, case.snapshot(ws))
    board = TaskBoard(clone)
    summary = board.summary()
    assert summary['active']['target'] == '/structure'
    assert 'level_no_entity' in summary['next']
    pool_task = next(n for n, s in board.state['tasks'].items() if s['target'] == '/pool')
    assert board.check(pool_task)['status'] == 'FAIL'
    assert any('construction.structure' in e for e in board.check(pool_task)['issues'])

    clone.datasets['people'].key = 'rid'
    clone.datasets['people'].link = None
    clone.drafts['S']['allocation'][1]['weight'] = ''
    assert not board.structure_issues('S')
    assert board.check(pool_task)['status'] == 'PASS'
    assert clone.prepare_data()['status'] == 'PASS'
    assert clone.compile_formulas()['status'] == 'PASS'
    assert Decimal(clone.calculate_basis('S')['pool']) == 30


def test_confirmed_basis_survives_later_weight_compilation_but_not_base_change(tmp_path):
    ws = make_workspace(tmp_path)
    ws.drafts['S']['allocation'][1]['weight'] = ''
    prepared(ws)
    assert ws.calculate_basis('S')['status'] == 'PASS'
    assert ws.review_basis('S')['status'] == 'PASS'
    flow = ws._flow()
    flow.approve('S', '财务', fingerprint(ws.workflow['basis']['S']))
    assert flow.current('approved:S')
    with pytest.raises(ToolError, match='人员分配公式'):
        ws.trial_run('S')

    ws.drafts['S']['allocation'][1]['weight'] = 'share'
    assert flow.current('formulas') and flow.current('approved:S')
    board = TaskBoard(ws)
    allocation = next(n for n, s in board.state['tasks'].items() if s['target'] == '/allocation')
    assert board.check(allocation)['status'] == 'PASS'
    assert ws.compile_formulas()['status'] == 'PASS'
    ws.trial_run('S')
    assert {p['name']: Decimal(p['final']) for p in ws.results['S']['R0'].people} == {'甲': Decimal(20), '乙': Decimal(10)}
    ws.drafts['S']['pool']['expr'] = 'amount * 0.2'
    assert not flow.current('approved:S')


def test_report_stage_projection_preserves_original_parameter_positions(tmp_path):
    ws = make_workspace(tmp_path)
    original = copy.deepcopy(ws.drafts['S']['report']['totals'][0])
    ws.drafts['S']['report']['totals'].insert(0, {**original, 'name': '人员阶段汇总', 'stage': 'distribution'})
    fact = ws.facts[0]
    fact.targets = [t.replace('/report/totals/0/', '/report/totals/1/') for t in fact.targets]
    view = ws._flow().basis_view()
    assert [t['name'] for t in view.drafts['S']['report']['totals']] == ['项目奖合计']
    assert 'S::/report/totals/0/expr' in view.facts[0].targets
    assert 'S::/report/totals/1/expr' in fact.targets
    assert not view._binding_gaps()


def test_invalid_new_allocation_exposes_missing_entity_with_a_verified_pool(tmp_path):
    ws = prepared(make_workspace(tmp_path))
    ws.calculate_basis('S')
    assert ws.review_basis('S')['status'] == 'PASS'
    flow = ws._flow()
    flow.approve('S', '财务', fingerprint(ws.workflow['basis']['S']))
    board = TaskBoard(ws)
    alloc = next(task for task, spec in board.state['tasks'].items() if spec['target'] == '/allocation')
    original = copy.deepcopy(ws.drafts['S']['allocation'])
    candidate = copy.deepcopy(original)
    candidate[-1]['key'] = 'missing_recipient_key'
    result = ws.submit_formula_task(alloc, candidate)
    assert result['status'] == 'FAIL' and not result['saved']
    assert any('level_no_entity' in issue and 'missing_recipient_key' in issue for issue in result['issues'])
    assert ws.drafts['S']['allocation'] == original and flow.current('approved:S')


def test_local_ratio_default_hazard_is_rejected_before_full_calculation(tmp_path):
    ws = make_workspace(tmp_path)
    ws.set_rules(scheme='S', tables={'coefficient': {'kind': 'tier', 'purpose': 'coefficient',
                 'mode': 'whole', 'steps': [[0, 1], [1, 2]], 'below_first': 'first'}},
                 variables={'rate': {'scope': '项目', 'expr': 'ratio(amount, amount, zero: 1)'},
                            'factor': {'scope': '项目', 'expr': 'tier_rate(coefficient, rate)'}})
    board = TaskBoard(ws)
    board.plan([{'id': 'factor', 'scheme': 'S', 'target': '/variables/factor'}])
    issues = board.issues(board.state['tasks']['factor'])
    assert any('tier_ratio' in issue for issue in issues)
    assert not ws.llm.calls


def test_entity_basis_can_be_independently_reviewed_before_allocation_exists(tmp_path):
    ws = make_workspace(tmp_path)
    ws.drafts['S'].pop('allocation')
    ws.drafts['S']['pool']['scope'] = 'projects'
    ws.drafts['S']['report']['details'][0]['level'] = 'projects'
    ws.plan_formula_tasks([{'id': 'pool', 'scheme': 'S', 'target': '/pool'},
                           {'id': 'report', 'scheme': 'S', 'target': '/report'}])
    assert ws.check_formula_task('pool')['status'] == 'PASS'
    assert ws.check_formula_task('report')['status'] == 'PASS'
    prepared(ws)
    assert Decimal(ws.calculate_basis('S')['pool']) == 30
    assert ws.review_basis('S')['status'] == 'PASS'
    assert not ws.results


def test_unfinished_distribution_dependency_is_deferred_but_unknown_evidence_is_not(tmp_path):
    ws = make_workspace(tmp_path)
    ws.drafts['S']['variables'] = {'unfinished_weight': {'scope': '参与', 'expr': 'not_defined_yet'}}
    ws.drafts['S']['allocation'][1]['weight'] = 'unfinished_weight'
    ws.drafts['S']['checks'] = [{'id': 'later', 'scope': '参与', 'stage': 'distribution',
                                 'expr': 'unfinished_weight >= 0', 'message': '分配权重非负'}]
    prepared(ws)
    board = TaskBoard(ws)
    deferred = {n for n, s in board.state['tasks'].items() if s['target'] in ('/variables/unfinished_weight', '/checks')}
    assert deferred and not deferred & board.required('basis')
    assert Decimal(ws.calculate_basis('S')['pool']) == 30
    ws.record_facts([{'name': '未归属参数', 'file': 'policy.md', 'quote': '销售金额的10%', 'value': '10%'}])
    report = ws.compile_formulas()
    assert report['status'] == 'FAIL'
    assert '未归属参数' in str(report['issues'])


def test_data_load_failure_keeps_structural_diagnosis_first(tmp_path, monkeypatch):
    ws = make_workspace(tmp_path)
    ws.datasets['people'].key = None
    ws.datasets['people'].link = 'pid'
    def unreadable(*args, **kwargs):
        raise ToolError('value_empty: 毛利率有空值，原表第50行；须查明缺失口径')
    monkeypatch.setattr(ws, '_data', unreadable)
    monkeypatch.setattr(ws, 'diagnose_datasets', unreadable)
    report = ws.prepare_data()
    assert report['status'] == 'FAIL'
    assert 'level_no_entity' in report['issues'][0]
    assert any('value_empty' in issue and '50' in issue for issue in report['issues'])
    assert ws.workflow['data_report']['report'] == report
    assert ws._flow().current('data') is False
    assert 'level_no_entity' in ws._flow().hint()


def test_data_stage_checks_inferred_auxiliary_parents_without_allocation(tmp_path):
    from bonus_agent.stuck_question import program_issue

    def add_reference(workbook):
        tab = workbook.create_sheet('rates')
        tab.append(['code', 'rate'])
        tab.append(['A', '20%'])

    ws = make_workspace(tmp_path, mutate_workbook=add_reference)
    ws.drafts['S']['allocation'] = []
    ws.drafts['S']['report'] = None
    ws.drafts['S']['pool']['scope'] = 'projects'
    group_field = {'type': 'id', 'column': 'pid'}
    ws.update_dataset('projects', {'fields': {'group_key': group_field}})
    ws.define_dataset('rates', 'data.xlsx', 'rates',
                      {'group_key': {'type': 'id', 'column': 'code'}, 'rate': {'type': 'ratio', 'column': 'rate'}},
                      key='group_key')
    failed = ws.prepare_data()
    assert failed['status'] == 'FAIL'
    link_issue, = [issue for issue in failed['issues'] if 'link_unmatched' in issue]
    assert '「projects」按同名字段 group_key 被推断挂到「rates」' in link_issue
    assert '1 行找不到上级（如 B）' in link_issue
    assert program_issue(f'错误：{link_issue}')

    ws.update_dataset('rates', {'key': None})
    ws.define_dataset('groups', 'data.xlsx', 'projects', {'group_key': group_field}, key='group_key', distinct=True)
    ws.update_dataset('rates', {'link': 'group_key'})
    repaired = ws.prepare_data()
    assert repaired['status'] == 'PASS', repaired['issues']
    assert repaired['counts']['projects'] == repaired['counts']['groups'] == 2
    assert repaired['counts']['rates'] == 1
