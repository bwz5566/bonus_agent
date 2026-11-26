"""Real-object preflight catches runtime gaps without changing accounting data."""
from copy import deepcopy

import pytest

from bonus_core.data.table import from_records
from bonus_core.dsl.models import Check, FieldDef, RuleSet, TierTable, Variable
from bonus_core.engine.preflight import preflight_basis
from bonus_core.engine.run import Engine


def example(expr='min(amount, of: "sales")'):
    rs = RuleSet.model_validate({
        'name': 'preflight', 'version': '1',
        'datasets': {
            'projects': {'key': 'pid', 'fields': {'pid': {'type': 'id'}}},
            'sales': {'link': 'pid', 'fields': {
                'pid': {'type': 'id'}, 'amount': {'type': 'money'}}},
            'history': {'link': 'pid', 'fields': {
                'pid': {'type': 'id'}, 'historical_amount': {'type': 'money'}}},
        },
        'variables': {'base': {'scope': 'projects', 'expr': expr},
                      'bonus': {'scope': 'projects', 'expr': 'base * 0.1'}},
        'pool': {'scope': 'projects', 'expr': 'bonus'},
        'allocation': [],
    })
    records = {
        'projects': [{'pid': '202601-A'}, {'pid': '202609-B'}],
        'sales': [{'pid': '202609-B', 'amount': 100}],
        'history': [{'pid': '202601-A', 'historical_amount': 50},
                    {'pid': '202609-B', 'historical_amount': 100}],
    }
    return rs, records


def test_first_missing_detail_reports_real_object_formula_source_and_coverage():
    rs, records = example()
    report = preflight_basis(rs, from_records(rs, records, file='input.xlsx'))
    assert report['status'] == 'FAIL'
    issue = report['issues'][0]
    assert issue['node_id'] == 'projects:202601-A'
    assert issue['source'] == 'input.xlsx/projects!1'
    assert issue['formula'] == 'min(amount, of: "sales")'
    assert issue['rule_item'] == 'base'
    assert [f['rule_item'] for f in issue['dependency_chain']] == ['base', 'bonus', 'pool']
    coverage = issue['coverage'][0]
    assert (coverage['dataset'], coverage['dataset_rows']) == ('sales', 1)
    assert coverage['visible_rows'] == coverage['selected_rows'] == 0
    assert coverage['relationship']['link'] == 'pid'
    assert '历史' in ' '.join(issue['repair'])
    assert report['summary']['candidate_objects'] == 2
    assert report['summary']['completed_expression_traces'] == 0


def test_equally_close_references_cannot_silently_choose_the_first_budget_field():
    rules = RuleSet.model_validate({
        'name': 'budget origins',
        'version': '1',
        'datasets': {
            'orders': {'key': 'order_id', 'fields': {
                'order_id': {'type': 'id'}, 'unit_id': {'type': 'id'},
                'market_id': {'type': 'id'}, 'alternate_market': {'type': 'id'}}},
            'unit_targets': {'key': 'unit_id', 'fields': {
                'unit_id': {'type': 'id'}, 'month_target': {'type': 'money'}}},
            'market_targets': {'key': 'market_id', 'fields': {
                'market_id': {'type': 'id'}, 'month_target': {'type': 'money'}}},
        },
        'pool': {'scope': 'orders', 'expr': 'sum(month_target, of: "orders")'},
        'allocation': [],
    })
    records = {'orders': [
        {'order_id': '001', 'unit_id': 'Unit1', 'market_id': 'North', 'alternate_market': 'North'},
        {'order_id': '002', 'unit_id': 'Unit1', 'market_id': 'A9', 'alternate_market': 'NORTH'}],
               'unit_targets': [{'unit_id': 'Unit1', 'month_target': 500}],
               'market_targets': [{'market_id': 'North', 'month_target': 500}]}
    data = from_records(rules, records, file='source.xlsx')
    report = preflight_basis(rules, data)
    assert report['status'] == 'FAIL'
    issue, = report['issues']
    assert issue['field_conflict']['field'] == 'month_target'
    assert {item['source'] for item in issue['field_conflict']['sources']} == {
        'source.xlsx/unit_targets!1', 'source.xlsx/market_targets!1'}
    assert issue['coverage'][0]['active_row_source'] == 'source.xlsx/orders!1'
    market = next(scope for scope in issue['field_conflict']['source_scopes'] if scope['scope'] == 'market_targets')
    assert market['pool_objects_reaching_scope'] == 1
    assert market['key_candidates'] == [
        {'field': 'alternate_market', 'exact_pool_objects': 1, 'unique_normalized_only_pool_objects': 1}]
    selected = rules.model_copy(update={'pool': rules.pool.model_copy(update={
        'scope': 'global', 'expr': 'sum(month_target, of: "unit_targets")'})})
    assert preflight_basis(selected, data)['status'] == 'PASS'
    assert Engine.prepare_basis(selected, data).calculate_basis()['pool'] == '500.00'


def test_failed_table_applicability_includes_evaluated_source_values():
    rules, records = example('tier(scale, sum(amount, of: "sales"))')
    rules.datasets['projects'].fields['route'] = FieldDef(type='text')
    rules.tables['scale'] = TierTable(mode='whole', steps=[(0, '0.1')], applies_when='route == "North"')
    records['projects'][0]['route'] = 'North'
    records['projects'][1]['route'] = 'North-east'
    report = preflight_basis(rules, from_records(rules, records, file='data.xlsx'))
    assert report['status'] == 'FAIL'
    guard = report['issues'][0]['table_applicability']
    assert guard['table'] == 'scale' and guard['condition'] == 'route == "North"'
    assert guard['evaluated'] == '"North-east" == "North"'
    assert guard['source'] == 'data.xlsx/projects!2'
    assert not report['execution_evidence']['policy_successful']


def test_filter_empty_is_distinguished_from_missing_relationship_rows():
    rs, records = example('min(amount, of: "sales", where: amount > 1000)')
    records['sales'].insert(0, {'pid': '202601-A', 'amount': 50})
    report = preflight_basis(rs, from_records(rs, records, file='input.xlsx'))
    issue = report['issues'][0]
    coverage = issue['coverage'][0]
    assert (coverage['visible_rows'], coverage['selected_rows']) == (1, 0)
    assert coverage['visible_sources'] == ['input.xlsx/sales!1']
    assert coverage['selected_sources'] == []
    assert coverage['where'] == 'amount > 1000'
    assert 'where' in issue['repair'][0]


@pytest.mark.parametrize('field', ['amount', 'region'])
def test_empty_aggregate_keeps_failure_and_never_invents_a_numeric_branch(field):
    rs, records = example(f'min({field}, of: "sales")')
    rs.datasets['sales'].fields['region'] = FieldDef(type='text')
    records['sales'][0]['region'] = 'West'
    before = deepcopy(records)
    report = preflight_basis(rs, from_records(rs, records))
    assert report['status'] == 'FAIL'
    issue = report['issues'][0]
    if field == 'region':
        # A declared text operand is rejected before evaluating any object;
        # do not require runtime coverage from a check that never ran.
        assert report['phase'] == 'prepare'
        assert issue['code'] == 'fn_type' and issue['where'] == 'variables.base'
        assert '不能用数值默认值' in issue['message']
        assert 'coverage' not in issue
    else:
        assert issue['coverage'][0]['selected_rows'] == 0
        assert '原文规定' in issue['message'] and '字段类型一致' in issue['message']
        assert '历史' in issue['message'] and '关联键' in issue['message']
    assert '>= 0' not in issue['message'] and ', 0)' not in issue['message']
    assert '合计用 sum' not in issue['message']
    assert records == before


def test_if_guarded_empty_set_is_not_rejected():
    rs, records = example('if(count(of: "sales") > 0, min(amount, of: "sales"), 0)')
    report = preflight_basis(rs, from_records(rs, records))
    assert report['status'] == 'PASS', report
    assert report['issues'] == []
    assert report['summary']['candidate_objects'] == 2


def test_legitimate_historical_aggregation_is_preserved_without_implicit_cropping():
    rs, records = example('if(count(of: "sales") > 0, min(amount, of: "sales"), 0) + sum(historical_amount, of: "history")')
    data = from_records(rs, records)
    before = deepcopy(data)
    report = preflight_basis(rs, data)
    assert report['status'] == 'PASS', report
    assert report['summary']['dataset_rows'] == {'projects': 2, 'sales': 1, 'history': 2}
    assert data == before
    # Independently call the real public basis API to ensure historical money
    # remains in its own calculation. It is deliberately absent from preflight.
    basis = Engine.prepare_basis(rs, data).calculate_basis()
    assert basis['objects']['projects:202601-A']['amount'] == '5.00'
    assert basis['pool'] == '25.00'
    assert not {'pool', 'pool_raw', 'objects', 'totals', 'details', 'traces'} & report.keys()


def test_failed_detail_row_outside_source_sample_is_identified_exactly():
    rs, records = example('sum(amount, of: "sales")')
    records['sales'] = [{'pid': '202601-A', 'amount': n} for n in range(5)]
    records['sales'][-1]['amount'] = None
    report = preflight_basis(rs, from_records(rs, records, file='input.xlsx'))
    assert report['status'] == 'FAIL'
    coverage = report['issues'][0]['coverage'][0]
    assert coverage['selected_rows'] == 5
    assert coverage['active_row_source'] == 'input.xlsx/sales!5'
    assert coverage['active_row_source'] not in coverage['selected_sources']


def test_preflight_keeps_real_relationship_validation():
    rs, records = example()
    records['sales'][0]['pid'] = 'unknown'
    report = preflight_basis(rs, from_records(rs, records))
    assert report['status'] == 'FAIL'
    assert report['phase'] == 'prepare'
    assert any('unknown' in issue['message'] for issue in report['issues'])
    assert 'summary' not in report


def test_preflight_does_not_distribute_or_mutate_inputs(monkeypatch):
    rs, records = example('sum(amount, of: "sales")')
    # A downstream formula can remain incomplete: preflight is basis-only.
    rs.variables['unfinished'] = Variable(scope='projects', expr='unknown_weight')
    data = from_records(rs, records)
    before = (rs.model_dump_json(), deepcopy(data))
    def forbidden(*args, **kwargs):
        pytest.fail('preflight must not distribute or invoke compute')
    monkeypatch.setattr(Engine, '_apply', forbidden)
    monkeypatch.setattr(Engine, 'compute', forbidden)
    report = preflight_basis(rs, data)
    assert report['status'] == 'PASS', report
    assert rs.model_dump_json() == before[0]
    assert data == before[1]


def test_basis_business_check_failure_remains_blocking_with_source():
    rs, records = example('sum(amount, of: "sales")')
    rs.checks = [Check(id='positive', scope='projects', expr='base > 0')]
    report = preflight_basis(rs, from_records(rs, records, file='input.xlsx'))
    assert report['status'] == 'FAIL'
    issue = report['issues'][0]
    assert issue['code'] == 'check_failed'
    assert issue['failed_objects'] == 1
    assert issue['examples'][0] == {
        'node_id': 'projects:202601-A', 'source': 'input.xlsx/projects!1',
        'formula': 'base > 0', 'rows': [],
    }


def test_unexpected_engine_exception_is_never_reported_as_pass(monkeypatch):
    rs, records = example('sum(amount, of: "sales")')
    def fail(self):
        raise RuntimeError('implementation failure')
    monkeypatch.setattr(Engine, 'calculate_basis', fail)
    report = preflight_basis(rs, from_records(rs, records))
    assert report['status'] == 'FAIL'
    assert report['issues'][0]['code'] == 'basis_preflight_internal'
    assert 'RuntimeError' in report['issues'][0]['message']
