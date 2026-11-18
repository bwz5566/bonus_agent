"""Explicit tool repairs cannot turn repeated reads into repeated paid advice."""
import copy
import json

import pytest

from bonus_agent.agent import Step, _deep_reason, run_agent
from bonus_agent.llm import FakeLLM
from bonus_agent.recovery import RecoveryJournal, local_advice
from tests.test_staged_workflow import make_workspace, prepared, save_staged_case, _staged_llm
from tests.test_auto_basis_compile import ready_workspace


def advisor():
    return FakeLLM(lambda *a: pytest.fail('optional Pro must not be invoked for unresolved native repairs'))


def bad_binding():
    return {'tool': 'bind_facts', 'args': {'bindings': [{'name': '提成率', 'targets': ['S::/variables/missing/expr']}]}}


@pytest.mark.parametrize('code', ['parameter.binding', 'parameter.type', 'parameter.unit', 'tool.contract'])
def test_first_explicit_contract_failure_uses_program_feedback(code):
    step = Step('bind_facts', bad_binding()['args'], '错误：' + code + '：提成率目标不符合要求', False)
    assert _deep_reason([step], 0, object(), {'bind_facts': 1}) == ''


@pytest.mark.evidence
def test_binding_failure_followed_by_only_reads_stops_without_pro(tmp_path):
    ws = ready_workspace(tmp_path, unbound=True)
    out = tmp_path / 'out'
    save_staged_case(ws, out)
    reads = [{'tool': 'query_dataset', 'args': {'dataset': 'projects', 'expr': f'sum(amount, of: "projects") + {i} * 0'}} for i in range(18)]
    llm = _staged_llm(ws, [bad_binding(), *reads])
    llm.recovery_mode, llm.think_llm = 'advisor', advisor()
    ar = run_agent(llm, ws.inv.files, out, max_steps=20)
    assert any(s.tool == 'bind_facts' and not s.ok for s in ar.steps)
    assert len([s for s in ar.steps if s.tool == 'query_dataset']) >= 3
    assert not llm.think_llm.calls and not any(s.tool == '（局部诊断）' for s in ar.steps)
    assert '纠错预算耗尽' in ar.error
    state = json.loads((out / 'recovery.json').read_text(encoding='utf-8'))
    assert state['native_repairs'] and state['progress']['stagnant'] >= 12
    assert state['failures'] and not state['advised']


@pytest.mark.evidence
def test_successful_binding_on_resume_clears_pending_and_independent_review_still_runs(tmp_path):
    ws = prepared(make_workspace(tmp_path))
    out = tmp_path / 'out'
    save_staged_case(ws, out)
    first_llm = _staged_llm(ws, [bad_binding()])
    first_llm.recovery_mode, first_llm.think_llm = 'advisor', advisor()
    first = run_agent(first_llm, ws.inv.files, out, max_steps=1)
    assert not first_llm.think_llm.calls
    assert json.loads((out / 'recovery.json').read_text(encoding='utf-8'))['native_repairs']
    correct = {'tool': 'bind_facts', 'args': {'bindings': [{'name': '提成率', 'targets': list(ws.facts[0].targets)}]}}
    llm = _staged_llm(ws, [correct, {'tool': 'calculate_basis', 'args': {'scheme': 'S'}},
                            {'tool': 'review_basis', 'args': {'scheme': 'S'}}])
    llm.recovery_mode, llm.think_llm = 'advisor', advisor()
    result = run_agent(llm, ws.inv.files, out, max_steps=4)
    state = json.loads((out / 'recovery.json').read_text(encoding='utf-8'))
    assert not state['native_repairs']
    assert state['failures']  # Resolution does not erase the accumulated failure journal.
    assert not llm.think_llm.calls
    assert len(llm.checker_llm.calls) == 1
    assert result.ws._flow().current('review:S')
    assert not result.ws._flow().current('approved:S')


def test_pending_contract_blocks_changing_stagnation_reasons_and_survives_resume(tmp_path):
    path = tmp_path / 'recovery.json'
    journal = RecoveryJournal(path, {'source': 'same'})
    journal.observe_native_feedback('bind_facts', bad_binding()['args'], '错误：parameter.binding：提成率缺目标', False)
    resumed = RecoveryJournal(path, {'source': 'same'})
    for count in range(2, 8):
        assert not resumed.claim_advice(f'连续{count}次无进展')
        resumed.observe_native_feedback('read_workspace', {'section': 'rules'}, '{}', True)
        resumed.observe_native_feedback('note', {'topic': '改描述', 'text': str(count)}, 'saved', True)
    assert resumed.native_guidance and not resumed.state['advised']


def test_validation_only_clears_its_own_scope_and_partial_repairs_accumulate():
    journal = RecoveryJournal(None, {})
    args = {'bindings': [{'name': 'A'}, {'name': 'B'}]}
    journal.observe_native_feedback('bind_facts', args, '错误：parameter.binding：A、B缺目标', False)
    journal.observe_native_feedback('bind_facts', {'bindings': [{'name': 'A'}]}, 'validated', True)
    assert list(journal.state['native_repairs'].values())[0]['objects'] == ['B']
    journal.observe_native_feedback('bind_facts', {'bindings': [{'name': 'B'}]}, 'validated', True)
    assert not journal.native_guidance
    for tool, code in [('plan_formula_tasks', 'construction.task'), ('record_parameter_cells', 'source.cell'),
                       ('update_dataset', 'unmatched_basis'), ('bind_facts', 'parameter.unit')]:
        journal.observe_native_feedback(tool, {'name': 'A'}, '错误：' + code + '：A未解决', False)
    journal.observe_native_feedback('compile_formulas', {}, '{"phase":"basis","status":"PASS"}', True)
    assert len(journal.state['native_repairs']) == 4
    journal.observe_native_feedback('validate_parameters', {}, '{"status":"PASS"}', True)
    assert {v['code'] for v in journal.state['native_repairs'].values()} == {'construction.task', 'source.cell', 'unmatched_basis'}


def test_equivalent_verified_bindings_and_task_identity_resolve_only_their_objects():
    journal = RecoveryJournal(None, {})
    for name in ('A', 'B'):
        journal.observe_native_feedback('bind_facts', {'bindings': [{'name': name}]}, '错误：parameter.binding：' + name, False)
    journal.observe_native_feedback('set_rules', {'bindings': [{'name': 'A'}]}, 'saved', True)
    journal.observe_native_feedback('submit_formula_task', {'bindings': [{'name': 'B'}]}, '{"saved":false}', True)
    assert [v['objects'] for v in journal.state['native_repairs'].values()] == [['B']]
    journal.observe_native_feedback('submit_formula_task', {'bindings': [{'name': 'B'}]}, '{"saved":true}', True)
    assert not journal.native_guidance
    journal.observe_native_feedback('check_formula_task', {'task': 'factor', 'scheme': 'S'}, '错误：construction.task：factor未知', False)
    journal.observe_native_feedback('check_formula_task', {'task': 'T31'}, '{"target":"/variables/factor","scheme":"T"}', True)
    assert journal.native_guidance
    journal.observe_native_feedback('check_formula_task', {'task': 'T31'}, '{"target":"/variables/factor","scheme":"S"}', True)
    assert not journal.native_guidance


def test_atomic_submit_unknown_task_tracks_task_not_its_binding_and_resolves_alias():
    journal = RecoveryJournal(None, {})
    args = {'task': 'gm_factor', 'scheme': 'S', 'bindings': [{'name': '毛利默认系数'}]}
    journal.observe_native_feedback('submit_formula_task', args, '错误：construction.task：gm_factor未知，请使用T31', False)
    assert [item['objects'] for item in journal.state['native_repairs'].values()] == [['gm_factor']]
    repaired = {**args, 'task': 'T31'}
    journal.observe_native_feedback('submit_formula_task', repaired,
                                    '{"target":"/variables/gm_factor","scheme":"S","saved":false}', True)
    assert journal.native_guidance
    journal.observe_native_feedback('submit_formula_task', repaired,
                                    '{"target":"/variables/gm_factor","scheme":"S","saved":true}', True)
    assert not journal.native_guidance


@pytest.mark.evidence
def test_restored_automatic_contract_failure_blocks_stagnation_advice(tmp_path, monkeypatch):
    from bonus_agent.workflow import Workflow
    ws = prepared(make_workspace(tmp_path)); out = tmp_path / 'out'; save_staged_case(ws, out)
    original = Workflow.recheck
    def automatic_failure(self, **kwargs):
        if not self.state.get('test_auto_failure'):
            self.state['test_auto_failure'] = True
            self._save_compile_report({'status': 'FAIL', 'issues': ['parameter.unit：提成率单位未确认'],
                                       'warnings': [], 'graph': {}, 'repaired': [], 'phase': 'basis', 'preflight': {}})
            self._invalidate_compilation('basis')
            return [('compile_formulas', False, ['parameter.unit：提成率单位未确认'])]
        return original(self, **kwargs)
    monkeypatch.setattr(Workflow, 'recheck', automatic_failure)
    reads = [{'tool': 'query_dataset', 'args': {'dataset': 'projects', 'expr': f'sum(amount, of: "projects") + {i} * 0'}} for i in range(18)]
    llm = _staged_llm(ws, reads); llm.recovery_mode, llm.think_llm = 'advisor', advisor()
    result = run_agent(llm, ws.inv.files, out, max_steps=20)
    assert result.steps[0].tool == '（自动复核）' and not result.steps[0].ok
    assert '纠错预算耗尽' in result.error and not llm.think_llm.calls
    saved = json.loads((out / 'recovery.json').read_text(encoding='utf8'))
    assert any(v['code'] == 'parameter.unit' for v in saved['native_repairs'].values())


def test_adviser_gets_actual_binding_schema_and_older_matching_target_only():
    captured = []
    llm = FakeLLM(lambda messages: captured.append(json.loads(messages[-1]['content'])) or json.dumps({
        'diagnosis': '缺少公式分支', 'next_action': '核对原文修复公式', 'evidence_needed': '已提供原文', 'acceptance': '验证缺失值分支'}))
    old = Step('read_workspace', {'section': 'variables', 'scheme': 'S', 'name': 'factor'}, 'correct original formula', True)
    wrong = Step('read_workspace', {'section': 'variables', 'scheme': 'T', 'name': 'factor'}, 'wrong scheme formula', True)
    reads = [Step('read_workspace', {'section': 'notes'}, '{}', True) for _ in range(5)]
    failed = Step('bind_facts', {'bindings': [{'name': '默认系数', 'targets': ['S::/variables/factor/expr']}]},
                  '错误：parameter.binding：variables.factor.expr没有原文1', False)
    local_advice(llm, failed.result, [old, wrong, *reads, failed], [], '核对精确目标')
    packet = captured[0]
    contract = next(c for c in packet['tool_contracts'] if c['name'] == 'bind_facts')
    assert contract['schema']['properties']['bindings']['items']['properties']['targets']['description']
    assert 'literal可以绑定expr' in packet['constraints']
    assert '不能推断为此次绑定新引入' in packet['constraints']
    assert packet['related_nodes'][0]['result_excerpt'] == 'correct original formula'
    assert 'wrong scheme formula' not in json.dumps(packet['related_nodes'])
    assert len(json.dumps(packet, ensure_ascii=False)) <= 18000


def test_oversized_full_contract_is_rejected_before_optional_model_call(monkeypatch):
    import bonus_agent.tool_specs as specs
    definitions = copy.deepcopy(specs.TOOLS)
    next(t for t in definitions if t['name'] == 'set_rules')['parameters']['description'] = 'x' * 30000
    monkeypatch.setattr(specs, 'TOOLS', definitions)
    llm = advisor()
    with pytest.raises(ValueError, match='完整工具合同'):
        local_advice(llm, 'set_rules failed', [Step('set_rules', {'large': 'x' * 40000}, 'error', False)], [], 'read rules')
    assert not llm.calls
