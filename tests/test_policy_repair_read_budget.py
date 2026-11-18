"""Execution-contract failures consume the same bounded repair exploration."""
import copy

import pytest

from bonus_agent.agent import Step
from bonus_agent.progress import RepairReadBudget
from bonus_core.data.table import from_records
from bonus_core.dsl.models import Check
from bonus_core.engine.preflight import preflight_basis
from test_core_basis_preflight import example
from test_runtime_repair_read_budget import observe, read, wrap


def negative_case():
    rs, records = example('1')
    rs.pool.expr = '-1'
    return rs, records


def compile_report(rs, records):
    return wrap(preflight_basis(rs, from_records(rs, records)))


def pool_write(**changes):
    return Step('set_rules', {'scheme': 'S', 'pool': changes}, 'saved', True)


def test_real_negative_pool_arms_first_failure_and_survives_restart_without_refill():
    rs, records = negative_case()
    report = compile_report(rs, records)
    issue = report['preflight']['S']['issues'][0]
    assert issue['code'] == 'pool_negative'
    guard = RepairReadBudget({})
    first = observe(guard, report, [read(i) for i in range(4)])
    assert first['warn'] and first['reads'] == 4 and first['subject'].endswith('/pool')
    restarted = RepairReadBudget(copy.deepcopy(guard.state))
    halted = observe(restarted, report, [read(i) for i in range(4, 8)])
    assert halted['halt'] and halted['reads'] == 8
    row = next(iter(restarted.state['records'].values()))
    assert row['runtime']['node_id'] == issue['node_id']
    assert row['runtime']['rule_item'] == 'pool'
    assert row['runtime']['evidence_kind'] == 'policy_failure'


def test_persisted_legacy_negative_failure_uses_structured_node_not_message():
    rs, records = negative_case()
    report = compile_report(rs, records)
    issue = report['preflight']['S']['issues'][0]
    for field in ('target', 'rule_item', 'evidence_kind', 'node_scope'):
        issue.pop(field, None)
    issue['message'] = 'arbitrary translated text with another node and target'
    guard = RepairReadBudget({})
    assert observe(guard, report, [read(i) for i in range(8)])['halt']
    runtime = next(iter(guard.state['records'].values()))['runtime']
    assert runtime['node_id'] == issue['node_id'] and runtime['target'] == '/pool'


@pytest.mark.parametrize('wrong', [
    'scalar_trace', 'bare_pass', 'deferred', 'different_node', 'different_target',
    'different_kind', 'different_scheme', 'different_phase',
])
def test_policy_failure_cannot_be_cleared_by_masking_or_unverified_labels(wrong):
    rs, records = negative_case()
    initial = compile_report(rs, records)
    guard = RepairReadBudget({})
    observe(guard, initial, [read(i) for i in range(5)])
    node = initial['preflight']['S']['issues'][0]['node_id']
    evidence = {
        'successful': [{'node_id': node, 'rule_item': 'pool'}],
        'policy_successful': [{
            'node_id': node, 'rule_item': 'pool', 'target': '/pool',
            'evidence_kind': 'execution_contract_passed'}],
    }
    result = {'status': 'PASS', 'execution_evidence': evidence}
    if wrong == 'scalar_trace':
        evidence.pop('policy_successful')
    elif wrong == 'bare_pass':
        result.pop('execution_evidence')
    elif wrong == 'deferred':
        result['status'] = 'DEFERRED'
    elif wrong == 'different_node':
        evidence['policy_successful'][0]['node_id'] = 'projects:other'
    elif wrong == 'different_target':
        evidence['policy_successful'][0]['target'] = '/checks'
    elif wrong == 'different_kind':
        evidence['policy_successful'][0]['evidence_kind'] = 'expression_evaluated'
    report = {'phase': 'basis', 'status': 'PASS', 'issues': [], 'preflight': {'S': result}}
    if wrong == 'different_scheme':
        report['preflight'] = {'other': result}
    elif wrong == 'different_phase':
        report['phase'] = 'distribution'
    current = observe(guard, report, [pool_write(negative='allow'), read(9)])
    assert current['active'] and current['reads'] == 6 and current['subject'].endswith('/pool')


def test_policy_change_requires_real_execution_but_legitimate_negative_policy_is_accepted():
    rs, records = negative_case()
    report = compile_report(rs, records)
    guard = RepairReadBudget({})
    observe(guard, report, [read(i) for i in range(5)])
    # Saving a permissive label alone has not executed the amended policy.
    assert observe(guard, report, [pool_write(negative='allow')])['reads'] == 5
    rs.pool.negative = 'allow'
    passed = compile_report(rs, records)
    assert passed['status'] == 'PASS'
    assert not observe(guard, passed)['active']
    # This proves execution only. A later rejected pool retains its old quota.
    rs.pool.negative = 'block'
    recurring = observe(guard, compile_report(rs, records), [read(10), read(11), read(12)])
    assert recurring['halt'] and recurring['reads'] == 8


def test_actual_formula_repair_closes_contract_but_unwritten_stale_success_does_not():
    rs, records = negative_case()
    guard = RepairReadBudget({})
    observe(guard, compile_report(rs, records), [read(i) for i in range(4)])
    rs.pool.expr = 'bonus'
    passed = compile_report(rs, records)
    assert passed['status'] == 'PASS'
    assert observe(guard, passed)['active']
    assert not observe(guard, passed, [pool_write(expr='bonus')])['active']


def test_real_false_check_is_a_bounded_contract_failure_and_actual_fix_releases_it():
    rs, records = example('1')
    rs.checks = [Check(id='positive', scope='projects', expr='base > 2')]
    report = compile_report(rs, records)
    assert report['preflight']['S']['issues'][0]['code'] == 'check_failed'
    guard = RepairReadBudget({})
    assert observe(guard, report, [read(i) for i in range(8)])['halt']
    row = next(iter(guard.state['records'].values()))
    assert row['runtime']['target'] == '/checks'
    assert row['runtime']['rule_item'] == 'checks.positive'
    rs.variables['base'].expr = '3'
    write = Step('set_rules', {'scheme': 'S', 'variables': {'base': {'expr': '3'}}}, 'saved', True)
    assert not observe(guard, compile_report(rs, records), [write])['active']


@pytest.mark.parametrize('wrong', [None, 'scope', 'purpose', 'phase', 'issues', 'complete'])
def test_complete_execution_contract_covers_truncated_proof_only_in_same_scope(wrong):
    rs, records = negative_case()
    report = compile_report(rs, records)
    guard = RepairReadBudget({})
    observe(guard, report, [read(i) for i in range(4)])
    proof = {'object_scope': 'projects', 'policy_checks_complete': True,
             'policy_successful': [], 'objects_complete': False}
    result = {'status': 'PASS', 'purpose': 'deterministic_basis_preflight', 'phase': 'evaluate',
              'issues': [], 'issue_count': 0, 'execution_evidence': proof}
    if wrong == 'scope':
        proof['object_scope'] = 'other'
    elif wrong == 'purpose':
        result['purpose'] = 'static_check'
    elif wrong == 'phase':
        result['phase'] = 'prepare'
    elif wrong == 'issues':
        result['issues'] = [{'code': 'pool_negative'}]
    elif wrong == 'complete':
        proof['policy_checks_complete'] = False
    passed = {'phase': 'basis', 'status': 'PASS', 'issues': [], 'preflight': {'S': result}}
    after = observe(guard, passed, [pool_write(expr='bonus')])
    assert after['active'] is (wrong is not None)


def test_policy_issue_scope_identifies_late_object_outside_bounded_membership_list():
    rs, records = negative_case()
    records = {'projects': [{'pid': str(i)} for i in range(140)], 'sales': [], 'history': []}
    rs.pool.expr = 'if(pid == "139", -1, 1)'
    report = compile_report(rs, records)
    issue = report['preflight']['S']['issues'][0]
    assert issue['node_id'] == 'projects:139' and issue['node_scope'] == 'projects'
    old_proof = report['preflight']['S']['execution_evidence']
    assert issue['node_id'] not in old_proof['object_ids'] and not old_proof['objects_complete']
    guard = RepairReadBudget({})
    observe(guard, report, [read(i) for i in range(8)])
    assert next(iter(guard.state['records'].values()))['runtime']['object_scope'] == 'projects'
    rs.pool.expr = '1'
    passed = compile_report(rs, records)
    proof = passed['preflight']['S']['execution_evidence']
    assert not any(item['node_id'] == issue['node_id'] for item in proof['policy_successful'])
    assert proof['policy_checks_complete'] and not proof['policy_successful_complete']
    assert not observe(guard, passed, [pool_write(expr='1')])['active']


def test_downgrading_failed_check_to_warning_is_not_predicate_success():
    rs, records = example('1')
    rs.checks = [Check(id='positive', scope='projects', expr='base > 2')]
    guard = RepairReadBudget({})
    observe(guard, compile_report(rs, records), [read(i) for i in range(4)])
    rs.checks[0].severity = 'warning'
    passed = compile_report(rs, records)
    assert passed['status'] == 'PASS'
    write = Step('set_rules', {'scheme': 'S', 'checks': [rs.checks[0].model_dump()]}, 'saved', True)
    after = observe(guard, passed, [write])
    assert after['active'] and after['reads'] == 4
