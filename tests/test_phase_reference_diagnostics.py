"""A node omitted from a stage is distinct from a nonexistent node, and both stay gated."""
import copy
import json

import pytest

from bonus_agent.construction import TaskBoard
from bonus_agent.context import workspace_view
from bonus_agent.case import snapshot
from tests.test_staged_workflow import make_workspace

pytestmark = pytest.mark.evidence


def alias_workspace(tmp_path, *, bind_alias=False):
    ws = make_workspace(tmp_path)
    ws.set_rules(scheme='S', variables={'old_rate': {'expr': '0.1', 'scope': 'global',
        'sources': [{'doc': 'policy.md', 'quote': '每个项目按销售金额的10%计算奖金'}]}})
    ws.cover_clauses({'C1': 'variables.old_rate、pool'})
    assert ws.prepare_data()['status'] == 'PASS'
    board = TaskBoard(ws); board.adopt()
    alias = next(t for t in board.state['tasks'].values() if t['target'] == '/variables/old_rate')
    pool = next(copy.deepcopy(t) for t in board.state['tasks'].values() if t['target'] == '/pool')
    pool['depends_on'] = [alias['id']]
    board.plan([pool])
    if bind_alias:
        ws.bind_facts([{'name': '提成率', 'targets': ['S::/variables/old_rate/expr']}])
    return ws


def test_existing_readable_alias_is_reported_as_outside_basis_dependencies(tmp_path):
    ws = alias_workspace(tmp_path)
    original = copy.deepcopy(ws.drafts)
    node = workspace_view(ws, 'rules', name='S::/variables/old_rate')['items'][0]
    assert node['definition']['expr'] == '0.1'
    assert not ws._clause_gaps(set())
    view = ws._flow().basis_view()
    assert 'old_rate' not in view.drafts['S']['variables']
    gaps = view._clause_gaps(set())
    assert len(gaps) == 1 and 'clause.phase_reference' in gaps[0]
    assert '在完整草稿中存在，但未进入本阶段' in gaps[0]
    assert '"name": "S::/variables/old_rate"' in gaps[0]
    assert 'read_workspace(section="checks", name="graph")' in gaps[0]
    assert 'old_rate 不存在' not in gaps[0]
    report = ws.compile_formulas()
    assert report['status'] == 'FAIL'
    assert any('clause.phase_reference' in issue for issue in report['issues'])
    assert not ws._flow().current('formulas') and ws.drafts == original
    graph = workspace_view(ws, 'checks', name='graph', scheme='S')
    assert graph['items'] and graph['items'][0]['phase'] == 'basis'
    assert all('old_rate' not in item.get('order', []) for item in graph['items'])


def test_real_missing_reference_is_not_reclassified_as_projection(tmp_path):
    ws = alias_workspace(tmp_path, bind_alias=True)
    del ws.drafts['S']['variables']['old_rate']
    view = ws._flow().basis_view()
    clause_gaps, binding_gaps = view._clause_gaps(set()), view._binding_gaps()
    assert any('old_rate 不存在' in issue for issue in clause_gaps)
    assert not any('phase_reference' in issue for issue in clause_gaps + binding_gaps)
    assert any('parameter.binding.path_missing' in issue for issue in binding_gaps)


def test_required_parameter_bound_to_pruned_alias_still_blocks_with_true_reason(tmp_path):
    ws = alias_workspace(tmp_path, bind_alias=True)
    assert not ws._binding_gaps()
    view = ws._flow().basis_view()
    gaps = view._binding_gaps()
    assert len(gaps) == 1 and 'parameter.binding.phase_reference' in gaps[0]
    assert 'S::/variables/old_rate/expr' in gaps[0]
    assert 'parameter.binding.path_missing' not in gaps[0]
    report = ws.compile_formulas()
    assert report['status'] == 'FAIL'
    assert any('parameter.binding.phase_reference' in issue for issue in report['issues'])
    assert not ws._flow().current('formulas')


def test_full_phase_does_not_use_projection_diagnostics(tmp_path):
    ws = alias_workspace(tmp_path, bind_alias=True)
    ws._flow().basis_view()  # A temporary view cannot mutate the original workspace.
    assert not hasattr(ws, '_phase_binding_gaps')
    assert not ws._clause_gaps(set()) and not ws._binding_gaps()
    report = ws._flow().compile('all')
    assert not any('phase_reference' in issue for issue in report['issues'])
    assert all(issue.startswith('construction.task：') for issue in report['issues'])
    assert 'old_rate' in ws.drafts['S']['variables']
    serialized = json.dumps(snapshot(ws), default=str)
    assert '_phase_source_drafts' not in serialized


def test_declared_distribution_parameter_stays_deferred_without_changing_basis(tmp_path):
    ws = make_workspace(tmp_path)
    allocation = copy.deepcopy(ws.drafts['S']['allocation'])
    allocation[-1]['weight'] = 'share * pay_factor'
    ws.set_rules(scheme='S', allocation=allocation, variables={'pay_factor': {'expr': '0.1', 'scope': 'global',
        'sources': [{'doc': 'policy.md', 'quote': '每个项目按销售金额的10%计算奖金'}]}})
    ws.facts.append(ws.facts[0].model_copy(deep=True, update={'name': '分配系数', 'targets': ['S::/variables/pay_factor/expr']}))
    assert ws.prepare_data()['status'] == 'PASS'
    board = TaskBoard(ws); board.adopt()
    owner = next(t for t in board.state['tasks'].values() if t['target'] == '/variables/pay_factor')
    assert owner['id'] not in board.required('basis')
    view = ws._flow().basis_view()
    assert 'pay_factor' not in view.drafts['S']['variables']
    assert not any('分配系数' in issue for issue in view._binding_gaps())
    assert ws.compile_formulas()['status'] == 'PASS'


def test_new_unowned_omitted_binding_invalidates_existing_basis_pass(tmp_path):
    ws = make_workspace(tmp_path)
    ws.set_rules(scheme='S', variables={'unused': {'expr': '0.1', 'scope': 'global'}})
    assert ws.prepare_data()['status'] == 'PASS'
    assert ws.compile_formulas()['status'] == 'PASS'
    board = TaskBoard(ws)
    unused = next(n for n, t in board.state['tasks'].items() if t['target'] == '/variables/unused')
    # No basis task/receipt depends on this existing, unassigned component.
    assert unused not in board.required('basis')
    board.state['tasks'].pop(unused)
    before = ws._flow().version('basis')
    assert ws._flow().current('formulas')
    ws.facts.append(ws.facts[0].model_copy(deep=True, update={
        'name': '新增待归属参数', 'targets': ['S::/variables/unused/expr']}))
    assert ws._flow().version('basis') != before
    assert not ws._flow().current('formulas')
    assert any('新增待归属参数：parameter.binding.phase_reference' in issue
               for issue in ws._flow().basis_view()._binding_gaps())


def test_shared_fact_keeps_explicit_distribution_target_deferred(tmp_path):
    ws = make_workspace(tmp_path)
    allocation = copy.deepcopy(ws.drafts['S']['allocation'])
    allocation[-1]['weight'] = 'share * pay_factor'
    ws.set_rules(scheme='S', allocation=allocation,
                 variables={'pay_factor': {'expr': '0.1', 'scope': 'global'}})
    ws.bind_facts([{'name': '提成率', 'targets': [*ws.facts[0].targets, 'S::/variables/pay_factor/expr']}])
    assert ws.prepare_data()['status'] == 'PASS'
    board = TaskBoard(ws); board.adopt()
    assignments = [copy.deepcopy(t) for t in board.state['tasks'].values()
                   if t['target'] in ('/pool', '/variables/pay_factor')]
    for task in assignments:
        task['facts'] = ['提成率']
    board.plan(assignments)
    view = ws._flow().basis_view()
    fact = next(f for f in view.facts if f.name == '提成率')
    assert 'S::/pool/expr' in fact.targets
    assert 'S::/variables/pay_factor/expr' not in fact.targets
    assert not view._phase_binding_gaps
    assert not view._binding_gaps()

    # A basis-required fact with no remaining basis target must not disappear.
    report = next(copy.deepcopy(t) for t in board.state['tasks'].values() if t['target'] == '/report')
    report['facts'] = ['提成率']
    board.plan([report])
    ws.bind_facts([{'name': '提成率', 'targets': ['S::/variables/pay_factor/expr']}])
    view = ws._flow().basis_view()
    assert any('提成率：parameter.binding.phase_reference' in issue for issue in view._binding_gaps())
    assert ws.compile_formulas()['status'] == 'FAIL'
