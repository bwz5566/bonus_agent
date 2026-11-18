"""Real preflight failures arm bounded reads, and only verified progress releases them."""
import copy
import json

import pytest

from bonus_agent.agent import READ_ONLY, Step, run_agent
from bonus_agent.llm import FakeLLM
from bonus_agent.progress import RepairReadBudget
from bonus_agent.recovery import RecoveryJournal
from bonus_core.data.table import from_records
from bonus_core.engine.preflight import preflight_basis
from test_core_basis_preflight import example


def read(index):
    return Step('read_workspace', {'section': 'clauses', 'query': f'new clause {index}'},
                json.dumps({'section': 'clauses', 'items': [{'id': f'C{index}'}]}), True)


def write(expr='1', *, scheme='S'):
    return Step('set_rules', {'scheme': scheme, 'variables': {'base': {'expr': expr}}}, 'saved', True)


def runtime(item='base', node='projects:A', *, evidence=None):
    issue = {'code': 'basis_preflight_eval', 'node_id': node, 'rule_item': item,
             'where': node + '/' + item, 'message': 'failed expression'}
    result = {'status': 'FAIL', 'purpose': 'deterministic_basis_preflight', 'phase': 'evaluate', 'issues': [issue]}
    if evidence is not None:
        result['execution_evidence'] = evidence
    return {'phase': 'basis', 'status': 'FAIL', 'static_checks_complete': True,
            'issues': ['方案「S」基础求值预检：' + json.dumps(issue)], 'preflight': {'S': result}}


def success(item='base', node='projects:A'):
    return {'successful': [{'node_id': node, 'rule_item': item}], 'successful_complete': False,
            'object_ids': ['projects:A', 'projects:B'], 'objects_complete': True, 'object_scope': 'projects'}


def observe(guard, report=None, steps=(), **kwargs):
    return guard.observe(steps, {}, report, READ_ONLY, **kwargs)


def wrap(result):
    return {'phase': 'basis', 'status': result['status'], 'static_checks_complete': True,
            'issues': ['方案「S」基础求值预检：' + json.dumps(i) for i in result['issues']],
            'preflight': {'S': result}}


def test_first_executed_failure_arms_immediately_and_all_new_queries_count():
    guard = RepairReadBudget({})
    report = runtime()
    first = observe(guard, report)
    assert first['active'] and first['reads'] == 0 and '/variables/base' in first['subject']
    warning = observe(guard, report, [read(i) for i in range(4)])
    assert warning['warn'] and warning['reads'] == 4 and not warning['halt']
    halted = observe(guard, report, [read(i) for i in range(4, 8)])
    assert halted['halt'] and halted['reads'] == 8


def test_runtime_identity_survives_node_changes_notes_prose_and_restart(tmp_path):
    journal = RecoveryJournal(tmp_path / 'recovery.json', 'same')
    guard = RepairReadBudget(journal.state.setdefault('repair_reads', {}))
    observe(guard, runtime(), [read(i) for i in range(6)])
    observe(guard, runtime(node='projects:B'), [
        Step('note', {'topic': 'new', 'text': 'new analysis'}, 'saved', True),
        Step('set_rules', {'variables': {'base': {'desc': 'new description'}}}, 'saved', True)])
    journal.save()
    restored = RecoveryJournal(journal.path, 'same')
    resumed = RepairReadBudget(restored.state['repair_reads'])
    assert observe(resumed, runtime(node='projects:C'))['reads'] == 6
    assert observe(resumed, runtime(node='projects:C'), [read(6), read(7)])['halt']
    row = next(iter(resumed.state['records'].values()))
    assert row['runtime']['node_id'] == 'projects:A' and not row.get('runtime_written')


@pytest.mark.parametrize('obscured', ['static', 'deferred', 'different_scheme', 'no_evidence'])
def test_new_failure_or_masking_without_execution_proof_keeps_original_quota(obscured):
    guard = RepairReadBudget({})
    observe(guard, runtime(), [read(i) for i in range(5)])
    report = runtime('later', evidence=success())
    if obscured == 'static':
        report['preflight'] = {}
    elif obscured == 'deferred':
        report['preflight']['S']['status'] = 'DEFERRED'
    elif obscured == 'different_scheme':
        report['preflight'] = {'other': report['preflight']['S']}
    else:
        report['preflight']['S'].pop('execution_evidence')
    current = observe(guard, report, [write(), read(9)])
    assert '/variables/base' in current['subject'] and current['reads'] == 6


@pytest.mark.parametrize('attempt', [
    Step('note', {'topic': 'x', 'text': 'expr changed'}, 'saved', True),
    Step('set_rules', {'variables': {'base': {'desc': 'new'}}}, 'saved', True),
    Step('set_rules', {'variables': {'base': {'sources': [{'quote': 'expr: 1'}]}}}, 'saved', True),
    Step('set_rules', {'variables': {'base': {'expr': '1'}}}, 'no-op: unchanged', True),
    Step('set_rules', {'variables': {'base': {'expr': '1'}}}, 'not saved', False),
    Step('submit_formula_task', {'task': 'T', 'value': {'expr': '1'}}, '{"saved": false}', True),
    write(scheme='other'),
])
def test_prose_noop_failed_or_unrelated_scheme_write_does_not_release_quota(attempt):
    guard = RepairReadBudget({})
    observe(guard, runtime(), [read(i) for i in range(4)])
    after = observe(guard, runtime('later', evidence=success()), [attempt])
    assert after['reads'] == 4 and '/variables/base' in after['subject']


def test_real_successful_trace_allows_next_failure_but_recurrence_does_not_refill():
    rs, records = example()
    guard = RepairReadBudget({})
    first = wrap(preflight_basis(rs, from_records(rs, records)))
    assert first['preflight']['S']['issues'][0]['rule_item'] == 'base'
    observe(guard, first, [read(i) for i in range(6)])
    rs.variables['base'].expr = 'sum(amount, of: "sales")'
    rs.variables['bonus'].expr = 'base / 0'
    second = wrap(preflight_basis(rs, from_records(rs, records)))
    assert second['preflight']['S']['issues'][0]['rule_item'] == 'bonus'
    progress = observe(guard, second, [write('sum(amount, of: "sales")')])
    assert progress['reads'] == 0 and '/variables/bonus' in progress['subject']
    observe(guard, second, [read(10), read(11)])
    rs.variables['bonus'].expr = 'base * 0.1'
    passed = wrap(preflight_basis(rs, from_records(rs, records)))
    assert passed['status'] == 'PASS'
    assert not observe(guard, passed, [write('base * 0.1')])['active']
    recurrence = observe(guard, first)
    assert recurrence['reads'] == 6
    assert observe(guard, first, [read(12), read(13)])['halt']


@pytest.mark.parametrize('complete,scope,allowed', [
    (True, 'projects', True), (False, 'projects', False), (True, 'other', False),
])
def test_absent_object_only_proves_progress_for_complete_correct_scope(complete, scope, allowed):
    guard = RepairReadBudget({})
    observe(guard, runtime(evidence=success()), [read(i) for i in range(5)])
    evidence = {'successful': [], 'successful_complete': True, 'object_ids': ['projects:B'],
                'objects_complete': complete, 'object_scope': scope}
    attempt = Step('update_dataset', {'name': 'projects', 'changes': {'population_from': 'sales'}}, 'saved', True)
    progress = observe(guard, runtime('later', node='projects:B', evidence=evidence), [attempt])
    assert progress['reads'] == (0 if allowed else 5)
    assert ('/variables/later' in progress['subject']) is allowed


@pytest.mark.parametrize('old_scope', [None, 'projects:Sub'])
def test_population_absence_requires_prior_positive_membership_in_exact_scope(old_scope):
    guard = RepairReadBudget({})
    node = 'projects:Sub:A' if old_scope else 'projects:A'
    old_evidence = ({'object_scope': old_scope, 'object_ids': [node], 'objects_complete': True}
                    if old_scope else None)
    observe(guard, runtime(node=node, evidence=old_evidence), [read(i) for i in range(4)])
    evidence = {'successful': [], 'object_scope': 'projects', 'object_ids': ['projects:B'],
                'objects_complete': True}
    attempt = Step('update_dataset', {'name': 'projects', 'changes': {'population_from': 'sales'}}, 'saved', True)
    unchanged = observe(guard, runtime('later', node='projects:B', evidence=evidence), [attempt])
    assert unchanged['reads'] == 4 and '/variables/base' in unchanged['subject']


def test_saved_semantic_draft_is_a_write_but_still_requires_positive_execution_proof():
    guard = RepairReadBudget({})
    observe(guard, runtime(), [read(i) for i in range(4)])
    attempt = Step('set_rules', {'scheme': 'S', 'variables': {'base': {'expr': '1'}}},
                   '草稿已保存，校验未通过；尚不能计算金额。\nnext failure', False)
    assert observe(guard, runtime('later'), [attempt])['reads'] == 4
    current = observe(guard, runtime('later', evidence=success()))
    assert current['reads'] == 0 and '/variables/later' in current['subject']


def test_verified_runtime_halt_is_archived_and_recurrence_gets_new_halt_identity():
    journal = RecoveryJournal(None, 'same')
    guard = RepairReadBudget(journal.state.setdefault('repair_reads', {}))
    stopped = observe(guard, runtime(), [read(i) for i in range(8)])
    journal.halt(stopped['message'], repair_read=stopped)
    old_marker = copy.deepcopy(journal.state['read_budget_halt'])
    next_failure = runtime('later', evidence=success())
    observe(guard, next_failure, [write()])
    assert old_marker['id'] in guard.state['resolved_halts'] and not journal.error
    # Completing the next target cannot erase the remembered exhausted quota.
    passed = {'phase': 'basis', 'status': 'PASS', 'issues': [], 'preflight': {
        'S': {'status': 'PASS', 'execution_evidence': success('later')}}}
    observe(guard, passed, [write()])
    repeated = observe(guard, runtime())
    assert repeated['halt'] and repeated['reads'] == 8
    journal.halt(repeated['message'], repair_read=repeated)
    assert journal.state['read_budget_halt']['id'] != old_marker['id'] and journal.error


def test_unstructured_first_diagnostic_still_gets_initial_discovery():
    guard = RepairReadBudget({})
    report = runtime()
    report['preflight']['S']['issues'][0].pop('rule_item')
    assert not observe(guard, report, [read(i) for i in range(12)])['active']


def test_runner_first_runtime_failure_stops_new_clause_reads_before_another_model_call(tmp_path):
    from tests.test_staged_workflow import make_workspace, save_staged_case
    ws = make_workspace(tmp_path)
    pool = copy.deepcopy(ws.drafts['S']['pool'])
    ws.set_rules(scheme='S', variables={'runtime_amount': {
        'scope': '项目', 'expr': 'amount * 0.1 / (amount - amount)', 'sources': pool['sources']}},
        pool={**pool, 'expr': 'runtime_amount'})
    targets = [target for target in ws.facts[0].targets if target != 'S::/pool/expr']
    ws.bind_facts([{'name': '提成率', 'targets': [*targets, 'S::/variables/runtime_amount/expr']}])
    assert ws.prepare_data()['status'] == 'PASS'
    out = tmp_path / 'out'
    save_staged_case(ws, out)
    calls = [{'tool': 'read_workspace', 'args': {'section': 'rules', 'name': name}} for name in
             ['/pool', '/allocation', '/report', '/report/totals', '/report/details', '/report/people',
              '/report/distributions', '/pool/expr']]
    llm = FakeLLM([calls, {'tool': 'note', 'args': {'topic': 'extra', 'text': 'must not run'}}])
    llm.workflow_mode = 'staged'
    result = run_agent(llm, ws.inv.files, out, max_steps=3, reflect=0)
    assert '读取 8/8 次' in result.error
    assert '/variables/runtime_amount' in result.error
    assert 'extra' not in result.ws.notes and not result.results
    assert not any(s.tool == '（局部诊断）' for s in result.steps)
