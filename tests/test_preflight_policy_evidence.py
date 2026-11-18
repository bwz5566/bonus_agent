"""A scalar was evaluated is different evidence from its amount policy passed."""
from copy import deepcopy

import pytest

from bonus_core.data.table import from_records
from bonus_core.dsl.models import Check, Report
from bonus_core.engine.preflight import preflight_basis
from tests.test_core_basis_preflight import example

pytestmark = pytest.mark.evidence


def negative_case():
    rs, records = example('sum(amount, of: "sales")')
    records['sales'] = [{'pid': '202601-A', 'amount': -50}, {'pid': '202609-B', 'amount': 100}]
    return rs, records


def test_negative_scalar_trace_is_not_a_policy_success_and_has_canonical_identity():
    rs, records = negative_case()
    before = (rs.model_dump(), deepcopy(records))
    report = preflight_basis(rs, from_records(rs, records, file='input.xlsx'))
    issue = report['issues'][0]
    assert {k: issue[k] for k in ('code', 'node_id', 'node_scope', 'target', 'rule_item', 'evidence_kind')} == {
        'code': 'pool_negative', 'node_id': 'projects:202601-A', 'node_scope': 'projects',
        'target': '/pool', 'rule_item': 'pool', 'evidence_kind': 'policy_failure'}
    assert issue['source'] == 'input.xlsx/projects!1' and issue['formula'] == 'bonus'
    evidence = report['execution_evidence']
    assert {'node_id': 'projects:202601-A', 'rule_item': 'base'} in evidence['successful']
    assert {'node_id': 'projects:202601-A', 'rule_item': 'pool'} not in evidence['successful']
    assert evidence['policy_successful'] == [] and evidence['policy_checks_complete'] is False
    assert evidence['successful_scope'] == 'expression_evaluated_only'
    assert (rs.model_dump(), records) == before


def test_material_backed_allow_can_pass_execution_contract_without_approving_amounts():
    rs, records = negative_case()
    rs.pool.negative = 'allow'
    report = preflight_basis(rs, from_records(rs, records))
    assert report['status'] == 'PASS'
    evidence = report['execution_evidence']
    assert {'node_id': 'projects:202601-A', 'rule_item': 'pool', 'target': '/pool',
            'evidence_kind': 'execution_contract_passed'} in evidence['policy_successful']
    assert evidence['policy_checks_complete'] is True
    assert 'not_business_or_amount_approval' in evidence['policy_success_scope']
    assert not {'pool', 'pool_raw', 'objects', 'totals', 'details', 'traces', 'approved'} & report.keys()


def test_global_negative_uses_real_global_identity_when_engine_where_is_empty():
    rs, records = negative_case()
    rs.pool.scope, rs.pool.expr = 'global', '-1'
    report = preflight_basis(rs, from_records(rs, records))
    issue = report['issues'][0]
    assert issue['where'] == '' and issue['node_scope'] == 'global'
    assert issue['node_id'] == report['execution_evidence']['object_ids'][0] == '全部'
    assert issue['target'] == '/pool' and issue['rule_item'] == 'pool'
    assert {'node_id': '全部', 'rule_item': 'pool'} not in report['execution_evidence']['successful']


def test_failed_boolean_check_has_true_source_identity_not_a_project_variable():
    rs, records = negative_case()
    rs.checks = [Check(id='required', scope='projects', expr='base >= 0')]
    report = preflight_basis(rs, from_records(rs, records, file='input.xlsx'))
    issue = report['issues'][0]
    assert issue['code'] == 'check_failed' and issue['evidence_kind'] == 'policy_failure'
    assert issue['node_id'] == 'projects:202601-A' and issue['node_scope'] == 'projects'
    assert issue['target'] == '/checks' and issue['rule_item'] == 'checks.required'
    assert issue['formula'] == 'base >= 0' and issue['source'] == 'input.xlsx/projects!1'
    assert not any(t['rule_item'] == 'checks.required' for t in report['execution_evidence']['successful'])
    assert not report['execution_evidence']['policy_successful']


def test_unbalanced_parts_do_not_turn_completed_scalar_traces_into_contract_success():
    rs, records = negative_case()
    rs.pool.negative = 'allow'
    rs.report = Report.model_validate({'balances': [{'name': 'pieces', 'level': 'projects',
        'total': 'bonus', 'parts': ['bonus + 1'], 'sources': [{'doc': 'policy.txt', 'quote': 'the parts must equal the total'}]}]})
    report = preflight_basis(rs, from_records(rs, records))
    issue = report['issues'][0]
    assert issue['code'] == 'report_unbalanced' and issue['target'] == '/report/balances'
    assert issue['rule_item'] == 'report.balances' and issue['node_id'] == 'projects:202601-A'
    assert issue['evidence_kind'] == 'policy_failure'
    evidence = report['execution_evidence']
    assert not any(t['node_id'] == issue['node_id'] and t['rule_item'] == 'report.balances'
                   for t in evidence['successful'])
    assert evidence['policy_successful'] == [] and evidence['policy_checks_complete'] is False
    rs.report.balances[0].parts = ['bonus']
    passed = preflight_basis(rs, from_records(rs, records))
    assert passed['status'] == 'PASS'
    assert {'node_id': issue['node_id'], 'rule_item': 'report.balances', 'target': '/report/balances',
            'evidence_kind': 'execution_contract_passed'} in passed['execution_evidence']['policy_successful']


def test_failed_warning_is_not_an_individually_passed_predicate():
    rs, records = negative_case()
    rs.pool.negative = 'allow'
    rs.checks = [Check(id='positive', scope='projects', expr='base >= 0', severity='warning')]
    report = preflight_basis(rs, from_records(rs, records))
    assert report['status'] == 'PASS' and report['warning_count'] == 1
    assert report['execution_evidence']['policy_checks_complete'] is False
    passed = report['execution_evidence']['policy_successful']
    assert not any(p['rule_item'] == 'checks.positive' and p['node_id'] == 'projects:202601-A' for p in passed)
    assert any(p['rule_item'] == 'checks.positive' and p['node_id'] == 'projects:202609-B' for p in passed)


def test_large_population_keeps_bounded_identities_and_explicit_full_execution_status():
    rs, records = negative_case()
    records['projects'] = [{'pid': str(i)} for i in range(200)]
    records['sales'] = [{'pid': str(i), 'amount': 1} for i in range(200)]
    records['history'] = []
    report = preflight_basis(rs, from_records(rs, records))
    evidence = report['execution_evidence']
    assert report['status'] == 'PASS' and report['phase'] == 'evaluate' and report['issues'] == []
    assert evidence['policy_checks_complete'] is True
    assert len(evidence['policy_successful']) == 128 and evidence['policy_successful_complete'] is False
    assert evidence['object_scope'] == 'projects' and evidence['objects_complete'] is False
