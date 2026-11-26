"""A verified closure archives one read halt; missing state or a later halt does not."""
import copy
import json

import pytest

from bonus_agent.agent import READ_ONLY, Step, ToolError, run_agent
from bonus_agent.progress import RepairReadBudget, repair_scope
from bonus_agent.recovery import RecoveryJournal, fingerprint, native_repair_feedback
from tests.test_repair_read_budget import read
from tests.test_repair_phase_scope import ROLE, workspace
from tests.test_staged_workflow import _staged_llm, make_workspace, save_staged_case


pytestmark = pytest.mark.evidence


def test_update_envelope_reaches_existing_dataset_validator(tmp_path):
    ws = make_workspace(tmp_path)
    out = tmp_path / 'out'
    save_staged_case(ws, out)
    args = {'name': 'projects', 'changes': {'fields': {'amount': {'desc': '净收入'}}}}
    calls = [{'tool': 'define_dataset', 'args': args}]
    calls.extend({'tool': 'read_workspace', 'args': {'section': 'facts', 'offset': i}} for i in range(8))
    ar = run_agent(_staged_llm(ws, calls), ws.inv.files, out, max_steps=len(calls), deep=0)
    patch = next(step for step in ar.steps if step.tool == 'update_dataset')
    assert patch.ok and '已按 name + changes' in patch.result
    assert ar.ws.datasets['projects'].fields['amount'].desc == '净收入'
    assert '已累计读取' not in ar.error


def test_argument_rejection_does_not_become_data_repair(tmp_path):
    ws = make_workspace(tmp_path)
    out = tmp_path / 'out'
    save_staged_case(ws, out)
    call = {'tool': 'define_dataset', 'args': {'name': 'projects', 'file': 'other', 'changes': {'filter': {}}}}
    ar = run_agent(_staged_llm(ws, [call] * 3), ws.inv.files, out, max_steps=6, deep=0)
    rejected = [step for step in ar.steps if step.tool == 'define_dataset']
    assert len(rejected) == 3 and all(not step.ok for step in rejected)
    assert '累计失败 3 次' in ar.error
    saved = json.loads((out / 'recovery.json').read_text(encoding='utf-8'))
    assert not saved.get('native_repairs')


@pytest.mark.parametrize('evidence', ['current', 'wrong_patch', 'changed_mapping'])
def test_legacy_cross_tool_halt_requires_accepted_and_current_patch(tmp_path, evidence):
    ws = make_workspace(tmp_path)
    journal = RecoveryJournal(None, 'same')
    args = {'name': 'projects', 'changes': {'fields': {'amount': {'desc': '净收入'}}}}
    reject = '错误：参数不对：define_dataset 没有参数 changes'
    item = native_repair_feedback('define_dataset', args, reject)
    journal.state['native_repairs'] = {
        fingerprint(['define_dataset', 'tool.arguments', ['projects']]): {**item, 'phase': None}}
    guard = RepairReadBudget(journal.state.setdefault('repair_reads', {}))
    budget = guard.observe([read(i) for i in range(8)], journal.state['native_repairs'], None, READ_ONLY)
    assert budget['halt']
    journal.halt(budget['message'], repair_read=budget)
    assert journal.error
    accepted = copy.deepcopy(args)
    if evidence == 'wrong_patch':
        accepted['changes']['fields']['amount']['desc'] = '别的口径'
    result = ws.update_dataset(**accepted)
    if evidence == 'changed_mapping':
        ws.update_dataset('projects', {'fields': {'amount': {'desc': '后来改过'}}})
    last = {'n': 1, 'status': 'FAILED', 'error': journal.error}
    log = tmp_path / 'history' / '第1轮' / 'agent_log.json'
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({'error': last['error'], 'steps': [
        {'tool': 'define_dataset', 'args': args, 'result': reject, 'ok': False},
        {'tool': 'update_dataset', 'args': accepted, 'result': result, 'ok': True},
    ]}, ensure_ascii=False), encoding='utf-8')
    check = journal.revalidate_saved_dataset_patches(ws, tmp_path, last)
    if evidence == 'current':
        assert check['dataset'] == 'projects' and not journal.error
    else:
        assert check is None and journal.error == last['error']


def halted_role(tmp_path):
    ws = workspace(tmp_path)
    journal = RecoveryJournal(tmp_path / 'recovery.json', 'same')
    args = {'bindings': [{'name': ROLE, 'targets': ['S::/variables/missing/expr']}]}
    with pytest.raises(ToolError) as failed:
        ws.bind_facts(**args)
    message = '错误：' + str(failed.value)
    journal.observe_native_feedback('bind_facts', args, message, False)
    guard = RepairReadBudget(journal.state.setdefault('repair_reads', {}))
    result = guard.observe([read(i) for i in range(8)], journal.state['native_repairs'], None, READ_ONLY)
    journal.halt(result['message'], repair_read=result)
    original = journal.state['halted']
    journal.set_repair_scope(repair_scope(ws))
    assert not journal.error
    return ws, journal, guard, original, args, message


def resolve_role(ws, journal, guard):
    ws.set_rules(scheme='S', variables={'role_share': {'expr': '0.25'}})
    args = {'bindings': [{'name': ROLE, 'targets': ['S::/variables/role_share/expr']}]}
    result = ws.bind_facts(**args)
    journal.observe_native_feedback('bind_facts', args, result, True)
    guard.observe([], journal.state['native_repairs'], None, READ_ONLY, scope=repair_scope(ws))
    return args


def test_real_binding_success_archives_halt_and_survives_full_phase_and_restart(tmp_path):
    ws, journal, guard, original, _, _ = halted_role(tmp_path)
    journal.state['failures']['preserved'] = {'attempts': 2}
    journal.state['advised'] = ['old advice']
    marker = copy.deepcopy(journal.state['read_budget_halt'])
    resolve_role(ws, journal, guard)
    assert not journal.state['native_repairs'] and not guard.state['records']
    assert not journal.error and journal.state['halted'] == original
    closed = guard.state['resolved_halts'][marker['id']]
    assert closed['halt'] == marker and closed['record']['reads'] == 8
    assert closed['verification']['tool'] == 'bind_facts' and closed['verification']['status'] == 'PASS'
    journal.set_repair_scope({'phase': 'all', 'deferred': []})
    assert not journal.error
    assert journal.state['failures']['preserved']['attempts'] == 2 and journal.state['advised'] == ['old advice']
    journal.save()
    restored = RecoveryJournal(journal.path, 'same')
    assert not restored.error and restored.state['halted'] == original


@pytest.mark.parametrize('missing', ['record', 'native', 'both'])
def test_missing_state_is_not_successful_validation(tmp_path, missing):
    ws, journal, guard, original, _, _ = halted_role(tmp_path)
    if missing in ('record', 'both'):
        guard.state['records'].clear()
    if missing in ('native', 'both'):
        journal.state['native_repairs'].clear()
    guard.observe([], journal.state['native_repairs'], None, READ_ONLY)
    journal.set_repair_scope({'phase': 'all', 'deferred': []})
    assert journal.error == original and not guard.state.get('resolved_halts')


@pytest.mark.parametrize('success', [
    ('bind_facts', {'bindings': [{'name': ROLE, 'targets': ['/variables/role_share/expr']}]}, 'no-op：绑定一致'),
    ('bind_facts', {'bindings': [{'name': 'unrelated', 'targets': ['/pool/expr']}]}, '已校验绑定'),
    ('read_workspace', {'section': 'facts', 'name': ROLE}, 'read'),
])
def test_noop_unrelated_success_and_reads_cannot_archive_halt(tmp_path, success):
    ws, journal, guard, original, _, _ = halted_role(tmp_path)
    journal.observe_native_feedback(*success, True)
    journal.set_repair_scope({'phase': 'all', 'deferred': []})
    assert journal.error == original and not guard.state.get('resolved_halts')


def test_current_receipt_for_same_table_closes_compile_error(tmp_path):
    ws = make_workspace(tmp_path)
    ws.plan_formula_tasks([{'id': 'rates', 'scheme': 'S', 'target': '/tables/rates', 'clauses': ['C1']}])
    ws.compile_table('rates', [{'value': '提成率'}], scheme='S')
    journal = RecoveryJournal(tmp_path / 'recovery.json', 'same')
    journal.observe_native_feedback('compile_table', {'scheme': 'S', 'name': 'rates'},
                                    '错误：parameter.ref：首档坐标是上限', False)
    guard = RepairReadBudget(journal.state.setdefault('repair_reads', {}))
    outcome = guard.observe([read(i) for i in range(8)], journal.state['native_repairs'], None, READ_ONLY)
    journal.halt(outcome['message'], repair_read=outcome)
    saved = copy.deepcopy(journal.state['read_budget_halt'])

    checked = ws.check_formula_task('rates')
    assert checked['status'] == 'PASS', checked
    for different in ({'scheme': 'other'}, {'target': '/tables/other'}, {'status': 'FAIL'},
                      {'checked_rule_source': {'state': 'PARTIAL'}}):
        journal.observe_native_feedback('check_formula_task', {'task': 'rates'},
                                        json.dumps({**checked, **different}), True)
        assert journal.error
    journal.observe_native_feedback('check_formula_task', {'task': 'rates'}, json.dumps(checked), True)
    assert not journal.error
    assert guard.state['resolved_halts'][saved['id']]['halt'] == saved
    assert not journal.state['native_repairs']


def test_same_table_name_in_different_schemes_keeps_each_failure():
    journal = RecoveryJournal(None, 'same')
    for scheme in ('first', 'second'):
        journal.observe_native_feedback('compile_table', {'scheme': scheme, 'name': 'rates'},
                                        '错误：parameter.ref：首档坐标是上限', False)
    pending = journal.state['native_repairs']
    assert {item['scheme'] for item in pending.values()} == {'first', 'second'}
    journal.observe_native_feedback('compile_table', {'scheme': 'first', 'name': 'rates'}, '参数表已编译并绑定', True)
    assert [item['scheme'] for item in pending.values()] == ['second']


@pytest.mark.parametrize('damage', ['missing', 'bad_type', 'wrong_identity', 'failed_verification', 'wrong_subject'])
def test_damaged_closure_cannot_prove_resolution(tmp_path, damage):
    ws, journal, guard, original, _, _ = halted_role(tmp_path)
    resolve_role(ws, journal, guard)
    journal.set_repair_scope({'phase': 'all', 'deferred': []})
    ident = journal.state['read_budget_halt']['id']
    proof = guard.state['resolved_halts'][ident]
    if damage == 'missing':
        proof.pop('verification')
    elif damage == 'bad_type':
        guard.state['resolved_halts'][ident] = 'broken'
    elif damage == 'wrong_identity':
        proof['halt']['id'] = 'different stop'
    elif damage == 'failed_verification':
        proof['verification']['status'] = 'FAIL'
    else:
        proof['record']['subject'] = 'unrelated'
    assert journal.error == original


def test_same_object_later_exhaustion_cannot_reuse_previous_closure(tmp_path):
    ws, journal, guard, original, failed_args, failed_message = halted_role(tmp_path)
    resolve_role(ws, journal, guard)
    old = copy.deepcopy(journal.state['read_budget_halt'])
    journal.set_repair_scope({'phase': 'all', 'deferred': []})
    journal.observe_native_feedback('bind_facts', failed_args, failed_message, False)
    result = guard.observe([read(i) for i in range(8)], journal.state['native_repairs'], None, READ_ONLY)
    assert not journal.error  # the earlier, actually resolved stop remains archived
    journal.halt(result['message'], repair_read=result)
    assert journal.state['halted'] == original  # even identical prose is a distinct stop
    assert journal.state['read_budget_halt']['id'] != old['id']
    assert journal.error == original


@pytest.mark.parametrize('reason', ['总tokens预算耗尽', '同一问题失败次数耗尽', '用户要求停止'])
def test_verified_read_closure_never_clears_other_halts(tmp_path, reason):
    ws, journal, guard, _, _, _ = halted_role(tmp_path)
    resolve_role(ws, journal, guard)
    journal.halt(reason)
    journal.set_repair_scope(repair_scope(ws))
    assert reason in journal.error


def test_real_complete_compilation_archives_only_the_checked_static_halt(tmp_path):
    ws = make_workspace(tmp_path)
    targets = list(ws.facts[0].targets)
    ws.facts[0].targets = []
    assert ws.prepare_data()['status'] == 'PASS'
    report = ws.compile_formulas()
    assert report['status'] == 'FAIL' and report['static_checks_complete']
    journal = RecoveryJournal(None, 'same')
    guard = RepairReadBudget(journal.state.setdefault('repair_reads', {}))
    guard.observe([], {}, report, READ_ONLY)
    attempt = Step('bind_facts', {'bindings': [{'name': '提成率', 'targets': ['/pool/expr']}]}, 'failed', False)
    result = guard.observe([attempt, *[read(i) for i in range(8)]], {}, report, READ_ONLY)
    journal.halt(result['message'], repair_read=result)
    original = journal.error
    incomplete = {'phase': 'basis', 'status': 'FAIL', 'issues': ['workflow.gate：data未通过']}
    guard.observe([], {}, incomplete, READ_ONLY)
    assert journal.error == original and not guard.state.get('resolved_halts')
    ws.bind_facts([{'name': '提成率', 'targets': targets}])
    from bonus_agent.construction import TaskBoard
    board = TaskBoard(ws)
    required = board.required('basis')
    for task in board.statuses():
        if task in required:
            assert ws.check_formula_task(task)['status'] == 'PASS'
    verified = ws.compile_formulas()
    assert verified['status'] == 'PASS', verified
    guard.observe([], {}, verified, READ_ONLY)
    assert not journal.error and journal.state['halted'] == original
    proof = guard.state['resolved_halts'][journal.state['read_budget_halt']['id']]
    assert proof['verification']['kind'] == 'static'


def test_runtime_halt_requires_its_executed_preflight_pass():
    journal = RecoveryJournal(None, 'same')
    guard = RepairReadBudget(journal.state.setdefault('repair_reads', {}))
    issue = '方案「S」基础求值预检：projects division by zero'
    failed = {'phase': 'basis', 'status': 'FAIL', 'static_checks_complete': True,
              'issues': [issue], 'preflight': {'S': {'status': 'FAIL'}}}
    guard.observe([], {}, failed, READ_ONLY)
    attempt = Step('update_dataset', {'name': 'projects'}, 'saved', True)
    result = guard.observe([attempt, *[read(i) for i in range(8)]], {}, failed, READ_ONLY)
    journal.halt(result['message'], repair_read=result)
    original = journal.error
    for preflight in ({}, {'S': {'status': 'DEFERRED'}}, {'other': {'status': 'PASS'}}):
        guard.observe([], {}, {**failed, 'status': 'PASS', 'issues': [], 'preflight': preflight}, READ_ONLY)
        assert journal.error == original and not guard.state.get('resolved_halts')
    guard.observe([], {}, {**failed, 'status': 'PASS', 'issues': [], 'preflight': {'S': {'status': 'PASS'}}}, READ_ONLY)
    assert not journal.error
    proof = guard.state['resolved_halts'][journal.state['read_budget_halt']['id']]
    assert proof['verification']['kind'] == 'runtime' and proof['verification']['scheme'] == 'S'
