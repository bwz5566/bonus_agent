"""Only actual successful trace identities or complete populations prove progress."""
from bonus_core.data.table import from_records
from bonus_core.engine.preflight import preflight_basis
from test_core_basis_preflight import example


def test_failed_target_is_not_a_success_and_exact_objects_have_no_amounts():
    rs, records = example('sum(amount, of: "sales")')
    rs.variables['bonus'].expr = 'base / 0'
    report = preflight_basis(rs, from_records(rs, records))
    assert report['status'] == 'FAIL'
    proof = report['execution_evidence']
    assert {'node_id': 'projects:202601-A', 'rule_item': 'base'} in proof['successful']
    assert not any(item['rule_item'] == 'bonus' for item in proof['successful'])
    assert proof['objects_complete'] and proof['object_ids'] == ['projects:202601-A', 'projects:202609-B']
    assert all(set(item) == {'node_id', 'rule_item'} for item in proof['successful'])


def test_truncated_identities_never_claim_complete_coverage():
    rs, records = example('1')
    records = {'projects': [{'pid': str(i)} for i in range(140)], 'sales': [], 'history': []}
    proof = preflight_basis(rs, from_records(rs, records))['execution_evidence']
    assert len(proof['object_ids']) == 128 and not proof['objects_complete']
    assert len(proof['successful']) == 128 and not proof['successful_complete']


def test_trace_saved_before_numeric_contract_failure_is_not_success():
    rs, records = example('1')
    rs.pool.expr = '"North"'
    report = preflight_basis(rs, from_records(rs, records))
    assert report['status'] == 'FAIL' and '需要数字' in report['issues'][0]['message']
    assert report['summary']['completed_expression_traces'] > 0
    assert not any(item['rule_item'] == 'pool' for item in report['execution_evidence']['successful'])
