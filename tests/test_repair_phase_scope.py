"""Deferred distribution repairs retain their allowance without blocking basis work."""
import copy
import json

import pytest

from bonus_agent.agent import CHAT_SOURCE, READ_ONLY, Step, run_agent
from bonus_agent.llm import FakeLLM
from bonus_agent.progress import RepairReadBudget, repair_scope
from bonus_agent.recovery import RecoveryJournal
from tests.test_repair_read_budget import failure, read
from tests.test_staged_workflow import make_workspace, save_staged_case


pytestmark = pytest.mark.evidence
ROLE = 'role share'
PLAN = [
    {'id': 'ROLE', 'scheme': 'S', 'target': '/variables/role_share', 'facts': [ROLE]},
    {'id': 'ALLOC', 'scheme': 'S', 'target': '/allocation', 'depends_on': ['ROLE']},
]


def workspace(tmp_path, *, planned=True):
    ws = make_workspace(tmp_path)
    ws.said = '人员角色比例为25%。'
    ws.record_facts([{'name': ROLE, 'value': '25%', 'file': CHAT_SOURCE, 'quote': ws.said}])
    if planned:
        ws.plan_formula_tasks(PLAN)
    return ws


def observe(guard, journal, scope, steps=(), report=None):
    journal.set_repair_scope(scope)
    return guard.observe(steps, journal.state.get('native_repairs', {}), report, READ_ONLY, scope=scope)


def test_only_explicitly_deferred_ownership_suspends_recovery(tmp_path):
    ws = workspace(tmp_path, planned=False)
    assert ROLE not in repair_scope(ws)['deferred']
    ws.plan_formula_tasks(PLAN)
    scoped = repair_scope(ws)
    assert {ROLE, 'ROLE', 'ALLOC', '/variables/role_share'} <= set(scoped['deferred'])
    assert '提成率' not in scoped['deferred']
    # An actual basis dependency overrides the distribution owner.
    ws.plan_formula_tasks([{'id': 'POOL', 'scheme': 'S', 'target': '/pool', 'depends_on': ['ROLE']}])
    assert ROLE not in repair_scope(ws)['deferred']
    assert 'ROLE' not in repair_scope(ws)['deferred']


def test_required_omitted_evidence_remains_active_even_if_absent_from_view_facts(tmp_path):
    ws = workspace(tmp_path)
    ws.set_rules(scheme='S', variables={'role_share': {'expr': '0.25'}})
    ws.bind_facts([{'name': ROLE, 'targets': ['S::/variables/role_share/expr']}])
    ws.plan_formula_tasks([{'id': 'POOL', 'scheme': 'S', 'target': '/pool', 'facts': [ROLE]}])
    view = ws._flow().basis_view()
    assert ROLE not in {fact.name for fact in view.facts}
    assert any(row['fact']['name'] == ROLE for row in view._phase_binding_evidence)
    assert ROLE not in repair_scope(ws)['deferred']


def test_deferred_reads_pause_and_resume_original_allowance_without_clearing_failures(tmp_path):
    ws = workspace(tmp_path); scope = repair_scope(ws)
    path = tmp_path / 'recovery.json'
    journal = RecoveryJournal(path, 'same'); args = failure(journal, ROLE)
    journal.failure('bind_facts', args, 'original failure')
    journal.state['advised'] = ['existing advice']
    guard = RepairReadBudget(journal.state.setdefault('repair_reads', {}))
    assert observe(guard, journal, None, [read(i) for i in range(6)])['reads'] == 6
    history = copy.deepcopy(journal.state['native_repairs'])
    failures = copy.deepcopy(journal.state['failures'])
    for i in range(12):
        assert not observe(guard, journal, scope, [read(100 + i)])['active']
    assert not journal.native_guidance and not guard.active
    assert journal.state['native_repairs'] == history
    assert journal.state['failures'] == failures and journal.state['advised'] == ['existing advice']
    assert next(iter(guard.state['records'].values()))['reads'] == 6
    journal.save()
    restored = RecoveryJournal(path, 'same'); guard = RepairReadBudget(restored.state['repair_reads'])
    assert not observe(guard, restored, scope)['active']
    active = observe(guard, restored, {'phase': 'all', 'deferred': []})
    assert active['reads'] == 6 and ROLE in restored.native_guidance
    assert observe(guard, restored, {'phase': 'all', 'deferred': []}, [read(200), read(201)])['halt']
    # Deferral is never a successful bind; only actual revalidation can clear it.
    restored.observe_native_feedback('bind_facts', args, '已校验绑定，更新1处', True)
    assert not observe(guard, restored, {'phase': 'all', 'deferred': []})['active']


def test_mixed_native_batch_uses_current_object_without_losing_deferred_one(tmp_path):
    scope = repair_scope(workspace(tmp_path))
    journal = RecoveryJournal(None, 'same')
    args = {'bindings': [{'name': ROLE, 'targets': ['/variables/role_share/expr']},
                         {'name': 'current rate', 'targets': ['/pool/expr']}]}
    journal.observe_native_feedback('bind_facts', args, '错误：parameter.unit：缺少依据', False)
    guard = RepairReadBudget({})
    result = observe(guard, journal, scope, [read(1), read(2)])
    assert result['subject'] == 'current rate' and result['reads'] == 2
    assert 'current rate' in journal.native_guidance and ROLE not in journal.native_guidance
    assert len(next(iter(journal.state['native_repairs'].values()))['objects']) == 2
    assert next(row for row in guard.state['records'].values() if row['subject'] == ROLE)['reads'] == 0


def test_full_phase_label_cannot_defer_unknown_or_required_basis_errors(tmp_path):
    scope = repair_scope(workspace(tmp_path)); journal = RecoveryJournal(None, 'same')
    failure(journal, '提成率')
    item = next(iter(journal.state['native_repairs'].values()))
    item['phase'] = 'all'
    guard = RepairReadBudget({})
    result = observe(guard, journal, scope, [read(1)])
    assert result['active'] and result['reads'] == 1
    assert '提成率' in journal.native_guidance
    report = {'phase': 'all', 'status': 'FAIL', 'issues': ['parameter.unit：未定位错误必须继续显示']}
    journal.observe_native_feedback('compile_formulas', {}, json.dumps(report), False)
    assert '未定位错误' in journal.native_guidance


def test_old_compile_native_exact_fact_lines_defer_but_unknown_lines_remain(tmp_path):
    scope = repair_scope(workspace(tmp_path)); journal = RecoveryJournal(None, 'same')
    report = {'phase': 'basis', 'status': 'FAIL', 'issues': [ROLE + '：parameter.unit，需单位依据']}
    journal.observe_native_feedback('compile_formulas', {}, json.dumps(report), False)
    original = copy.deepcopy(journal.state['native_repairs'])
    journal.set_repair_scope(scope)
    assert not journal.native_guidance
    assert journal.state['native_repairs'] == original
    journal.set_repair_scope({'phase': 'all', 'deferred': []})
    assert ROLE in journal.native_guidance
    report['issues'].append('parameter.unit：未知归属必须继续显示')
    journal.observe_native_feedback('compile_formulas', {}, json.dumps(report), False)
    journal.set_repair_scope(scope)
    assert ROLE not in journal.native_guidance and '未知归属' in journal.native_guidance


def test_projected_compile_absence_preserves_deferred_records_until_full_validation(tmp_path):
    scope = repair_scope(workspace(tmp_path)); journal = RecoveryJournal(None, 'same')
    guard = RepairReadBudget({})
    report = {'phase': 'basis', 'status': 'FAIL', 'static_checks_complete': True,
              'issues': [ROLE + '：缺少绑定']}
    observe(guard, journal, None, report=report)
    attempted = Step('bind_facts', {'bindings': [{'name': ROLE, 'targets': ['/variables/role_share/expr']}]}, 'failed', False)
    assert observe(guard, journal, None, [attempted, read(1)], report)['reads'] == 1
    assert not observe(guard, journal, scope, report={**report, 'status': 'PASS', 'issues': []})['active']
    assert len(guard.state['records']) == 1
    assert observe(guard, journal, {'phase': 'all', 'deferred': []})['reads'] == 1
    assert not observe(guard, journal, {'phase': 'all', 'deferred': []},
                       report={**report, 'phase': 'all', 'status': 'PASS', 'issues': []})['active']


def test_real_runner_snapshots_scope_before_and_after_plan_in_same_batch(tmp_path):
    ws = workspace(tmp_path, planned=False); out = tmp_path / 'out'; save_staged_case(ws, out)
    failed = {'tool': 'bind_facts', 'args': {'bindings': [{'name': ROLE, 'targets': ['S::/variables/missing/expr']}]}}
    commands = [failed,
                {'tool': 'read_workspace', 'args': {'section': 'facts', 'name': ROLE}},
                {'tool': 'read_workspace', 'args': {'section': 'rules', 'name': '/pool'}},
                {'tool': 'plan_formula_tasks', 'args': {'tasks': PLAN}},
                {'tool': 'read_workspace', 'args': {'section': 'rules', 'name': '/report'}},
                {'tool': 'read_workspace', 'args': {'section': 'rules', 'name': '/report/totals'}}]
    llm = FakeLLM([commands]); llm.workflow_mode = 'staged'
    result = run_agent(llm, ws.inv.files, out, max_steps=1, reflect=0, instruction=ws.said)
    assert any(step.tool == 'plan_formula_tasks' and step.ok for step in result.steps)
    saved = json.loads((out / 'recovery.json').read_text(encoding='utf8'))
    row = next(row for row in saved['repair_reads']['records'].values() if row['subject'] == ROLE)
    assert row['reads'] == 2 and row['deferred']
    assert '读取预算' not in result.error and not result.results


@pytest.mark.parametrize('legacy', [False, True])
def test_resume_projects_real_halted_deferred_exhaustion_before_paid_work(tmp_path, legacy):
    ws = workspace(tmp_path); out = tmp_path / 'out'; save_staged_case(ws, out)
    initial = FakeLLM([]); initial.workflow_mode = 'staged'
    run_agent(initial, ws.inv.files, out, max_steps=0, reflect=0, instruction=ws.said)
    path = out / 'recovery.json'; state = json.loads(path.read_text(encoding='utf8'))
    journal = RecoveryJournal(None, 'seed'); failure(journal, ROLE)
    guard = RepairReadBudget({})
    exhausted = observe(guard, journal, None, [read(i) for i in range(8)])
    journal.halt(exhausted['message'], repair_read=exhausted)
    state['native_repairs'] = journal.state['native_repairs']; state['repair_reads'] = guard.state
    state['halted'] = journal.state['halted']
    if not legacy:
        state['read_budget_halt'] = journal.state['read_budget_halt']
    original_halt = state['halted']
    path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf8')
    llm = FakeLLM([{'tool': 'note', 'args': {'topic': 'basis work', 'text': 'continue current basis task'}}])
    llm.workflow_mode = 'staged'
    result = run_agent(llm, ws.inv.files, out, max_steps=1, reflect=0, instruction=ws.said)
    assert result.ws.notes.get('basis work') == 'continue current basis task'
    assert '读取 8/8' not in result.error
    saved = json.loads(path.read_text(encoding='utf8'))
    assert next(iter(saved['repair_reads']['records'].values()))['reads'] == 8
    assert saved['halted'] == original_halt
    assert saved['read_budget_halt']['subject'] == ROLE
    restored = RecoveryJournal(path, 'unrelated')
    restored.state = saved
    assert not restored.error
    restored.set_repair_scope({'phase': 'all', 'deferred': []})
    assert restored.error == original_halt  # no reset on re-entering its phase


@pytest.mark.parametrize('reason', ['用户停止', '作业tokens达到总上限', '同一问题累计失败3次', '未知停机原因'])
def test_other_halts_cannot_be_suspended_by_a_deferred_read_record(tmp_path, reason):
    scope = repair_scope(workspace(tmp_path)); journal = RecoveryJournal(None, 'same')
    failure(journal, ROLE)
    guard = RepairReadBudget(journal.state.setdefault('repair_reads', {}))
    exhausted = observe(guard, journal, None, [read(i) for i in range(8)])
    journal.halt(exhausted['message'], repair_read=exhausted)
    journal.set_repair_scope(scope)
    assert not journal.error
    journal.halt(reason)
    journal.set_repair_scope(scope)
    assert reason in journal.error
    assert 'read_budget_halt' not in journal.state
    assert next(iter(guard.state['records'].values()))['reads'] == 8


def test_legacy_halt_requires_full_matching_record_evidence(tmp_path):
    scope = repair_scope(workspace(tmp_path)); journal = RecoveryJournal(None, 'same')
    failure(journal, ROLE)
    guard = RepairReadBudget(journal.state.setdefault('repair_reads', {}))
    exhausted = observe(guard, journal, None, [read(i) for i in range(8)])
    journal.halt(exhausted['message'])  # legacy format without structured metadata
    original = journal.state['halted']
    record = next(iter(guard.state['records'].values()))
    record['issue'] += ' changed'
    journal.set_repair_scope(scope)
    assert journal.error == original and 'read_budget_halt' not in journal.state
