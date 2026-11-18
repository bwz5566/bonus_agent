"""Only concrete unresolved repairs consume the persistent exploration allowance."""
import copy
import json

import pytest

from bonus_agent.agent import READ_ONLY, Step, ToolError, Workspace, run_agent
from bonus_agent.llm import FakeLLM
from bonus_agent.progress import RepairReadBudget
from bonus_agent.recovery import RecoveryJournal
from tests.test_staged_workflow import make_workspace, save_staged_case


pytestmark = pytest.mark.evidence


def failure(journal, name='rate'):
    args = {'bindings': [{'name': name, 'targets': ['/variables/rate/expr']}]}
    journal.observe_native_feedback('bind_facts', args,
                                   f'错误：parameter.binding：{name}取值与目标不一致', False)
    return args


def read(index):
    return Step('read_workspace', {'section': 'rules', 'name': f'/variables/v{index}'},
                json.dumps({'section': 'rules', 'items': [{'name': f'v{index}'}]}), True)


def observe(guard, journal, steps=(), report=None):
    return guard.observe(steps, journal.state.get('native_repairs', {}), report, READ_ONLY)


def test_initial_inventory_and_first_compile_diagnosis_are_not_limited():
    journal = RecoveryJournal(None, 'same')
    guard = RepairReadBudget({})
    report = {'phase': 'basis', 'status': 'FAIL', 'issues': ['rate：缺少绑定']}
    journal.observe_native_feedback('compile_formulas', {},
                                   json.dumps({**report, 'issues': ['parameter.unit：缺少单位']}), False)
    for n in range(40):
        assert not observe(guard, journal, [read(n)], report)['active']


def test_new_pages_cannot_extend_failed_object_budget_and_batch_counts_every_read():
    journal = RecoveryJournal(None, 'same'); failure(journal)
    guard = RepairReadBudget({})
    result = observe(guard, journal, [read(n) for n in range(4)])
    assert result['warn'] and not result['halt'] and result['reads'] == 4
    assert 'rate' in result['message'] and 'bind_facts' in result['message'] and 'v3' in result['message']
    result = observe(guard, journal, [read(n) for n in range(4, 8)])
    assert result['halt'] and result['reads'] == 8


@pytest.mark.parametrize('read_first', [True, False])
def test_same_response_reads_are_charged_only_after_its_first_failure(read_first):
    journal = RecoveryJournal(None, 'same'); guard = RepairReadBudget({})
    sequence = ['read'] * 4 + ['fail'] if read_first else ['fail'] + ['read'] * 4
    for index, kind in enumerate(sequence):
        if kind == 'fail':
            failure(journal)
            result = observe(guard, journal)
        else:
            result = observe(guard, journal, [read(index)])
    assert result['reads'] == (0 if read_first else 4)
    assert result['warn'] is (not read_first)


def test_note_noop_description_and_restart_cannot_replenish_allowance(tmp_path):
    path = tmp_path / 'recovery.json'
    journal = RecoveryJournal(path, 'same'); args = failure(journal)
    guard = RepairReadBudget(journal.state.setdefault('repair_reads', {}))
    observe(guard, journal, [read(n) for n in range(6)])
    journal.observe_native_feedback('bind_facts', args, 'no-op：绑定一致', True)
    observe(guard, journal, [Step('note', {'topic': 'new', 'text': 'done'}, 'saved', True),
                             Step('set_rules', {'variables': {'rate': {'expr': '1', 'desc': 'new'}}}, 'saved', True)])
    journal.save()
    resumed = RecoveryJournal(path, 'same')
    guard = RepairReadBudget(resumed.state['repair_reads'])
    assert observe(guard, resumed)['reads'] == 6
    assert observe(guard, resumed, [read(90), read(91)])['halt']
    resumed.save()
    second = RecoveryJournal(path, 'same')
    assert observe(RepairReadBudget(second.state['repair_reads']), second)['halt']


def test_only_resolution_of_the_focused_object_releases_its_allowance():
    journal = RecoveryJournal(None, 'same'); args = failure(journal)
    guard = RepairReadBudget({})
    observe(guard, journal, [read(n) for n in range(5)])
    other = failure(journal, 'other')
    journal.observe_native_feedback('bind_facts', other, '已校验绑定，更新1处', True)
    assert observe(guard, journal)['reads'] == 5
    journal.observe_native_feedback('bind_facts', args, '已校验绑定，更新1处', True)
    assert not observe(guard, journal)['active']


def test_real_binding_noop_revalidates_before_clearing_only_its_object(tmp_path):
    ws = make_workspace(tmp_path)
    journal = RecoveryJournal(None, 'same'); failure(journal, '提成率')
    guard = RepairReadBudget({}); observe(guard, journal, [read(n) for n in range(5)])
    correct = {'bindings': [{'name': '提成率', 'targets': list(ws.facts[0].targets)}]}
    result = ws.bind_facts(**correct)
    assert result.startswith('no-op') and '已复验类型、数值、目标、出处' in result
    journal.observe_native_feedback('bind_facts', correct, result, True)
    assert not observe(guard, journal)['active']
    # A changed target cannot claim successful revalidation: Workspace must
    # reject it before producing the program-owned success marker.
    ws.drafts['S']['pool']['expr'] = 'amount * 0.2'
    with pytest.raises(ToolError, match='parameter.binding'):
        ws.bind_facts(**correct)
    with pytest.raises(ToolError, match='parameter.binding'):
        ws.bind_facts([{'name': '提成率', 'targets': ['S::/variables/missing/expr']}])


def test_compile_failure_arms_only_after_corresponding_repair_attempt():
    journal = RecoveryJournal(None, 'same'); guard = RepairReadBudget({})
    report = {'phase': 'basis', 'status': 'FAIL', 'static_checks_complete': True,
              'issues': ['rate：variables.rate缺少绑定']}
    assert not observe(guard, journal, [], report)['active']
    unrelated = Step('set_rules', {'variables': {'other': {'expr': '1'}}}, 'saved', True)
    assert not observe(guard, journal, [unrelated, read(1)], report)['active']
    attempted = Step('set_rules', {'variables': {'rate': {'expr': '1'}}}, 'saved', True)
    assert observe(guard, journal, [attempted, read(2)], report)['reads'] == 1
    for i in range(3, 6):
        desc = Step('set_rules', {'variables': {'rate': {'expr': '1', 'desc': str(i)}}}, 'saved', True)
        observe(guard, journal, [desc, read(i)], report)
    assert observe(guard, journal, [], report)['reads'] == 4
    assert not observe(guard, journal, [], {**report, 'issues': ['other：缺少绑定']})['active']


def test_incomplete_compiler_failure_cannot_hide_old_unresolved_object():
    journal = RecoveryJournal(None, 'same'); guard = RepairReadBudget({})
    report = {'phase': 'basis', 'status': 'FAIL', 'static_checks_complete': True,
              'issues': ['rate：variables.rate缺少绑定']}
    observe(guard, journal, [], report)
    attempt = Step('set_rules', {'variables': {'rate': {'expr': '1'}}}, 'saved', True)
    observe(guard, journal, [attempt, read(1)], report)
    incomplete = {'phase': 'basis', 'status': 'FAIL', 'issues': ['workflow.gate：data未通过']}
    assert observe(guard, journal, [read(2)], incomplete)['reads'] == 2
    assert observe(guard, journal, [], report)['reads'] == 2


def test_real_workflow_early_gate_does_not_clear_parameter_repair_allowance(tmp_path):
    ws = make_workspace(tmp_path)
    assert ws.prepare_data()['status'] == 'PASS'
    ws.facts[0].targets = []
    report = ws.compile_formulas()
    assert report['status'] == 'FAIL' and report['static_checks_complete']
    journal = RecoveryJournal(None, 'same'); guard = RepairReadBudget({})
    observe(guard, journal, [], report)
    attempted = Step('bind_facts', {'bindings': [{'name': '提成率', 'targets': ['/pool/expr']}]}, 'failed', False)
    observe(guard, journal, [attempted, read(1)], report)
    ws.mapping['projects'].filter = {'amount': ['999']}
    with pytest.raises(ToolError, match='data'):
        ws.compile_formulas()
    early = ws.workflow['formula_report']['report']
    assert early['static_checks_complete'] is False and len(early['issues']) == 1
    retained = observe(guard, journal, [read(2)], early)
    assert retained['active'] and retained['reads'] == 2


def test_real_runtime_repair_budget_survives_static_gap_skipping_preflight(tmp_path):
    ws = make_workspace(tmp_path)
    original_pool = copy.deepcopy(ws.drafts['S']['pool'])
    runtime = {'scope': '项目', 'expr': 'amount * 0.1 / (amount - amount)', 'sources': original_pool['sources']}
    ws.set_rules(scheme='S', variables={'runtime_amount': runtime}, pool={**original_pool, 'expr': 'runtime_amount'})
    original_targets = [target for target in ws.facts[0].targets if target != 'S::/pool/expr']
    original_targets.append('S::/variables/runtime_amount/expr')
    ws.bind_facts([{'name': '提成率', 'targets': original_targets}])
    assert ws.prepare_data()['status'] == 'PASS'
    first = ws.compile_formulas()
    assert first['status'] == 'FAIL' and first['preflight']['S']['status'] == 'FAIL'
    journal = RecoveryJournal(None, 'same'); guard = RepairReadBudget({})
    observe(guard, journal, [], first)
    # A real, unsuccessful data repair still leaves the failing source table's
    # denominator at zero. Its later static failure must not reset this quota.
    changes = {'filter': {'pid': {'not': ['MISSING']}}}
    output = ws.update_dataset('projects', changes)
    assert ws.prepare_data()['status'] == 'PASS'
    second = ws.compile_formulas()
    attempt = Step('update_dataset', {'name': 'projects', 'changes': changes}, str(output), True)
    observed = observe(guard, journal, [attempt, read(1)], second)
    assert observed['subject'] == '方案「S」基础求值预检' and observed['reads'] == 1

    ws.facts[0].targets = []
    static_failure = ws.compile_formulas()
    assert static_failure['status'] == 'FAIL' and static_failure['static_checks_complete']
    assert not static_failure['preflight']
    retained = observe(guard, journal, [read(2)], static_failure)
    assert retained['active'] and retained['reads'] == 2
    # A different scheme's successful preflight or an explicit dependency
    # deferral does not prove this scheme's runtime error was corrected.
    deferred = {**static_failure, 'status': 'PASS', 'issues': [],
                'preflight': {'S': {'status': 'DEFERRED'}, 'other': {'status': 'PASS'}}}
    assert observe(guard, journal, [], deferred)['reads'] == 2

    ws.bind_facts([{'name': '提成率', 'targets': original_targets}])
    still_runtime = ws.compile_formulas()
    assert still_runtime['preflight']['S']['status'] == 'FAIL'
    assert observe(guard, journal, [], still_runtime)['reads'] == 2
    ws.set_rules(scheme='S', variables={'runtime_amount': {**runtime, 'expr': 'amount * 0.1'}})
    fixed = ws.compile_formulas()
    assert fixed['preflight']['S']['status'] == 'PASS'
    assert not observe(guard, journal, [], fixed)['active']


def test_failed_unit_to_value_diagnostic_does_not_start_a_new_allowance():
    journal = RecoveryJournal(None, 'same'); args = failure(journal)
    guard = RepairReadBudget({}); observe(guard, journal, [read(n) for n in range(7)])
    journal.observe_native_feedback('bind_facts', args, '错误：parameter.unit：rate缺少明确单位', False)
    assert observe(guard, journal, [read(8)])['halt']


def test_real_runner_stops_after_eight_reads_without_advisor(tmp_path):
    ws = make_workspace(tmp_path); out = tmp_path / 'out'; save_staged_case(ws, out)
    args = {'bindings': [{'name': '提成率', 'targets': ['S::/variables/does_not_exist/expr']}]}
    script = [{'tool': 'bind_facts', 'args': args}]
    script += [{'tool': 'read_workspace', 'args': {'section': 'rules', 'name': name}} for name in
               ['/pool', '/allocation', '/report', '/report/totals', '/report/details', '/report/people',
                '/report/distributions', '/report/totals/0/expr', '/pool/expr']]
    llm = FakeLLM(script); llm.workflow_mode = 'staged'
    result = run_agent(llm, ws.inv.files, out, max_steps=15, reflect=0)
    assert '读取 8/8 次' in result.error
    assert len([step for step in result.steps if step.tool == 'read_workspace']) == 8
    assert not any(step.tool in ('（局部诊断）', '（反思）') for step in result.steps)
    assert not result.results


@pytest.mark.parametrize('read_first', [True, False])
def test_runner_preserves_read_failure_order_in_one_response(tmp_path, read_first):
    ws = make_workspace(tmp_path); out = tmp_path / 'out'; save_staged_case(ws, out)
    failed = {'tool': 'bind_facts', 'args': {'bindings': [
        {'name': '提成率', 'targets': ['S::/variables/does_not_exist/expr']}]}}
    reads = [{'tool': 'read_workspace', 'args': {'section': 'rules', 'name': name}} for name in
             ['/pool', '/allocation', '/report', '/report/totals']]
    llm = FakeLLM([reads + [failed] if read_first else [failed] + reads]); llm.workflow_mode = 'staged'
    run_agent(llm, ws.inv.files, out, max_steps=1, reflect=0)
    state = json.loads((out / 'recovery.json').read_text(encoding='utf8'))['repair_reads']
    assert state['records'][state['focus']]['reads'] == (0 if read_first else 4)


def test_runner_counts_each_batched_read_before_next_model_call(tmp_path):
    ws = make_workspace(tmp_path); out = tmp_path / 'out'; save_staged_case(ws, out)
    failed = {'tool': 'bind_facts', 'args': {'bindings': [
        {'name': '提成率', 'targets': ['S::/variables/does_not_exist/expr']}]}}
    reads = [{'tool': 'read_workspace', 'args': {'section': 'rules', 'name': name}} for name in
             ['/pool', '/allocation', '/report', '/report/totals', '/report/details', '/report/people',
              '/report/distributions', '/report/totals/0/expr']]
    llm = FakeLLM([[failed, *reads], {'tool': 'note', 'args': {'topic': 'later', 'text': 'must not run'}}])
    llm.workflow_mode = 'staged'
    result = run_agent(llm, ws.inv.files, out, max_steps=3, reflect=0)
    assert '读取 8/8 次' in result.error and 'later' not in result.ws.notes


def test_runner_manual_compile_failure_arms_before_later_reads_in_same_batch(tmp_path):
    ws = make_workspace(tmp_path)
    pool = copy.deepcopy(ws.drafts['S']['pool']); pool['expr'] = 'amount * 0.3'
    ws.set_rules(scheme='S', pool=pool)
    assert ws.prepare_data()['status'] == 'PASS'
    assert ws.compile_formulas()['status'] == 'FAIL'
    out = tmp_path / 'out'; save_staged_case(ws, out)
    pool['expr'] = 'amount * 0.2'
    reads = [{'tool': 'read_workspace', 'args': {'section': 'rules', 'name': name}} for name in
             ['/pool', '/allocation', '/report', '/report/totals', '/report/details', '/report/people',
              '/report/distributions', '/report/totals/0/expr']]
    llm = FakeLLM([[{'tool': 'set_rules', 'args': {'scheme': 'S', 'pool': pool}},
                   {'tool': 'compile_formulas', 'args': {}}, *reads],
                  {'tool': 'note', 'args': {'topic': 'later', 'text': 'must not run'}}])
    llm.workflow_mode = 'staged'
    result = run_agent(llm, ws.inv.files, out, max_steps=3, reflect=0)
    assert '读取 8/8 次' in result.error and 'later' not in result.ws.notes


def test_resume_exhausted_budget_stops_before_any_model_request(tmp_path, monkeypatch):
    ws = make_workspace(tmp_path); out = tmp_path / 'out'; save_staged_case(ws, out)
    llm = FakeLLM([]); llm.workflow_mode = 'staged'
    # Establish the runner's exact context without a model call, then seed an
    # exhausted journal as if the process ended between save and halt.
    run_agent(llm, ws.inv.files, out, max_steps=0, reflect=0)
    path = out / 'recovery.json'; state = json.loads(path.read_text(encoding='utf8'))
    journal = RecoveryJournal(None, 'temporary'); args = failure(journal, '提成率')
    guard = RepairReadBudget({}); observe(guard, journal, [read(n) for n in range(8)])
    state['native_repairs'] = journal.state['native_repairs']; state['repair_reads'] = guard.state
    state['halted'] = ''
    path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf8')
    second = FakeLLM([{'tool': 'note', 'args': {'topic': 'unexpected', 'text': 'must not run'}}])
    second.workflow_mode = 'staged'
    def forbidden_auxiliary(self):
        raise AssertionError('exhausted resume must not enter auxiliary model stages')
    monkeypatch.setattr(Workspace, 'transcribe_all', forbidden_auxiliary)
    monkeypatch.setattr(Workspace, 'triage_all', forbidden_auxiliary)
    result = run_agent(second, ws.inv.files, out, max_steps=2, reflect=0)
    assert '读取 8/8 次' in result.error
    assert not any(step.tool == 'note' for step in result.steps)
    assert second.usage.prompt_tokens == 0 and second.usage.completion_tokens == 0
