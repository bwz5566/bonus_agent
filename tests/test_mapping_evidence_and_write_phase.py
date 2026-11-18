"""Mapping evidence follows executable filters; draft feedback respects calculation stages."""
import copy

import pytest

from bonus_agent.agent import CHAT_SOURCE, ToolError, run_agent
from bonus_agent.llm import FakeLLM
from bonus_core.dsl.validator import validate
from tests.test_staged_workflow import make_workspace, save_staged_case


pytestmark = pytest.mark.evidence


def filtered_workspace(tmp_path):
    ws = make_workspace(tmp_path, mutate_workbook=lambda wb: wb['projects'].append(['C', 0]))
    ws.said = '销售金额为0的记录不参与计算。'
    ws.record_facts([{'name': '零金额筛选', 'value': '0', 'file': CHAT_SOURCE,
                     'quote': ws.said}])
    ws.update_dataset('projects', {'filter': {'amount': {'not': ['0']}}})
    return ws


def bind_filter(ws, target='S::/mapping/projects/filter/amount/not/0'):
    return ws.bind_facts([{'name': '零金额筛选', 'targets': [target]}])


def test_filter_binding_proves_executed_filter_and_survives_basis_projection(tmp_path):
    ws = filtered_workspace(tmp_path)
    assert any('零金额筛选' in gap for gap in ws._binding_gaps())
    before = copy.deepcopy(ws.drafts)
    bind_filter(ws)
    assert ws.drafts == before and 'mapping' not in ws.drafts['S']
    assert not ws._binding_gaps()
    assert not ws._flow().basis_view()._binding_gaps()
    data, _ = ws._data(ws._ruleset('S'))
    assert [row.values['pid'] for row in data['projects'].rows] == ['A', 'B']
    assert ws.prepare_data()['status'] == 'PASS'
    assert ws.compile_formulas()['status'] == 'PASS'


def test_filter_value_or_operator_change_invalidates_exact_evidence(tmp_path):
    ws = filtered_workspace(tmp_path)
    bind_filter(ws)
    ws.update_dataset('projects', {'filter': {'amount': {'not': ['1']}}})
    assert any('零金额筛选' in gap for gap in ws.validate_parameters()['gaps'])
    ws.update_dataset('projects', {'filter': {'amount': ['0']}})
    assert any('零金额筛选' in gap for gap in ws.validate_parameters()['gaps'])
    assert ws.facts[-1].targets == ['S::/mapping/projects/filter/amount/not/0']


def test_mapping_failure_cannot_be_pruned_when_rule_binding_still_matches(tmp_path):
    ws = filtered_workspace(tmp_path)
    ws.set_rules(scheme='S', variables={'declared_zero': {'expr': '0'}})
    ws.bind_facts([{'name': '零金额筛选', 'targets': [
        'S::/mapping/projects/filter/amount/not/0', 'S::/variables/declared_zero/expr']}])
    assert ws.prepare_data()['status'] == 'PASS'
    assert ws.compile_formulas()['status'] == 'PASS'
    assert ws._flow().current('data') and ws._flow().current('formulas')
    ws.update_dataset('projects', {'filter': {}})
    checked = ws.validate_parameters(repair=True)
    assert any('零金额筛选' in gap for gap in checked['gaps'])
    assert 'S::/mapping/projects/filter/amount/not/0' in ws.facts[-1].targets
    assert not ws._flow().current('data') and not ws._flow().current('formulas')


def test_removed_dataset_keeps_orphan_evidence_blocking(tmp_path):
    ws = filtered_workspace(tmp_path)
    bind_filter(ws)
    ws.remove_dataset('projects')
    assert any('零金额筛选' in gap for gap in ws.validate_parameters()['gaps'])
    assert ws.facts[-1].targets  # deletion cannot erase the obligation


@pytest.mark.parametrize('target', [
    'other::/mapping/projects/filter/amount/not/0',
    'S::/mapping/projects/default',
    'S::/mapping/projects/filter/amount/not/99',
])
def test_filter_binding_rejects_wrong_scope_metadata_and_missing_index(tmp_path, target):
    ws = filtered_workspace(tmp_path)
    before = [f.model_dump() for f in ws.facts]
    with pytest.raises(ToolError, match='parameter.binding'):
        bind_filter(ws, target)
    assert [f.model_dump() for f in ws.facts] == before


def test_filter_pointer_escaping_and_source_version_are_preserved(tmp_path):
    ws = filtered_workspace(tmp_path)
    ws.datasets['source/rows~'] = ws.datasets.pop('projects')
    ws.mapping['source/rows~'] = ws.mapping.pop('projects')
    bind_filter(ws, 'S::/mapping/source~1rows~0/filter/amount/not/0')
    assert not any('零金额筛选' in gap for gap in ws._binding_gaps())
    ws.said += ' 此后新增说明。'
    assert any('零金额筛选' in gap and '原文版本' in gap for gap in ws._binding_gaps())


def bad_distribution(ws):
    report = copy.deepcopy(ws.drafts['S']['report'])
    report['distributions'][0].update(rounding='after_sum', pending=['未定义暂扣'])
    return report


def test_basis_write_defers_distribution_errors_but_keeps_real_basis_failures(tmp_path):
    ws = make_workspace(tmp_path)
    report = bad_distribution(ws)
    output = ws.set_rules(scheme='S', report=report)
    assert '基础阶段静态校验通过' in output
    assert '后续分配阶段检查延期' in output
    assert 'recipient_rounding_scope' in output and 'distribution_pending_missing' in output
    assert not ws.last_blocking
    assert any(i.code == 'recipient_rounding_scope' for i in validate(ws._ruleset('S')))
    ws.set_rules(scheme='S', pool={'scope': '项目', 'expr': 'missing_amount'})
    assert any(code == 'name_unknown' for _, code, _ in ws.last_blocking)
    before = copy.deepcopy(ws.drafts)
    with pytest.raises(ToolError, match='解析不了'):
        ws.set_rules(scheme='S', variables={'bad': {'scope': '项目', 'expr': '1 +'}})
    assert ws.drafts == before


def test_distribution_write_after_basis_approval_keeps_all_contracts(tmp_path):
    ws = make_workspace(tmp_path)
    ws._flow().receipt('approved:S', kind='basis')
    output = ws.set_rules(scheme='S', report=bad_distribution(ws))
    assert ws._flow().current('approved:S')  # only deferred distribution data changed
    assert '后续分配阶段检查延期' not in output
    assert {'recipient_rounding_scope', 'distribution_pending_missing'} <= {
        code for _, code, _ in ws.last_blocking}
    assert ws.prepare_data()['status'] == 'PASS'
    assert ws._flow().compile(phase='basis')['status'] == 'PASS'
    full = ws._flow().compile(phase='all')
    assert full['status'] == 'FAIL'
    assert any('recipient_rounding_scope' in issue for issue in full['issues'])
    assert any('distribution_pending_missing' in issue for issue in full['issues'])


def test_runner_marks_basis_write_success_without_hiding_deferred_checks(tmp_path):
    ws = make_workspace(tmp_path)
    ws.set_rules(scheme='S', report=bad_distribution(ws))
    out = tmp_path / 'out'
    save_staged_case(ws, out)
    pool = copy.deepcopy(ws.drafts['S']['pool'])
    pool['expr'] += ' + 0'
    llm = FakeLLM([{'tool': 'set_rules', 'args': {'scheme': 'S', 'pool': pool}}])
    llm.workflow_mode = 'staged'
    result = run_agent(llm, ws.inv.files, out, max_steps=1, reflect=0)
    step = next(s for s in result.steps if s.tool == 'set_rules')
    assert step.ok and '基础阶段静态校验通过' in step.result
    assert 'recipient_rounding_scope' in step.result and '后续分配阶段检查延期' in step.result
    assert not result.results  # a successful save is not a calculation approval


def test_legacy_write_keeps_complete_validation(tmp_path):
    ws = make_workspace(tmp_path)
    ws.workflow['enabled'] = False
    ws.set_rules(scheme='S', report=bad_distribution(ws))
    assert any(code == 'recipient_rounding_scope' for _, code, _ in ws.last_blocking)
