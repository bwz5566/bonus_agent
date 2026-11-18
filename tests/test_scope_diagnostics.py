"""Scope observations guide source review without inventing business policy."""
from copy import deepcopy
from types import SimpleNamespace

from bonus_agent.scope_diagnostics import scope_diagnostics
from bonus_core.data.mapping import DatasetMapping
from bonus_core.data.table import from_records
from tests.test_core_basis_preflight import example
from tests.test_staged_workflow import make_workspace


def workspace():
    rs, records = example()
    rs.datasets['history'].window = 'year_to_date'
    mappings = {name: DatasetMapping(file=f'{name}.xlsx', sheet=name,
                                   columns={field: field for field in spec.fields})
                for name, spec in rs.datasets.items()}
    data = from_records(rs, records, file='sources.xlsx')
    ws = SimpleNamespace(mapping=mappings, _scheme=lambda scheme: scheme or 'S',
                         _ruleset=lambda scheme: rs, _data=lambda rules: (data, []))
    return ws, rs, data


def test_cross_source_gap_and_historical_impact_are_observed_without_mutation():
    ws, rs, data = workspace()
    before = rs.model_dump_json(), deepcopy(data)
    report = scope_diagnostics(ws, focus='sales')
    assert report['status'] == 'OBSERVATION'
    item = report['comparisons'][0]
    assert (item['parent'], item['detail']) == ('projects', 'sales')
    assert item['parent_keys'] == 2 and item['detail_keys'] == 1
    assert item['parent_without_detail'] == 1
    assert item['parent_mapping']['file'] != item['detail_mapping']['file']
    assert item['parent_without_detail_examples'][0]['key'] == '202601-A'
    impact = item['hypothetical_population_from']
    assert impact['retained_parent_keys'] == 1
    assert impact['other_details_losing_parent'][0]['dataset'] == 'history'
    assert impact['other_details_losing_parent'][0]['rows_losing_parent'] == 1
    assert impact['other_details_losing_parent'][0]['window'] == 'year_to_date'
    assert '不是业务范围结论' in report['notes'][0]
    assert '不能据此批准' in impact['note']
    assert '原文无法确定' in report['notes'][-1]
    assert (rs.model_dump_json(), data) == before
    assert not {'approval', 'actions', 'amount', 'pool'} & report.keys()


def test_existing_population_is_respected_but_remaining_history_is_not_deleted():
    ws, rs, data = workspace()
    rs.datasets['projects'].population_from = 'sales'
    report = scope_diagnostics(ws)
    assert all(item['detail'] != 'sales' for item in report['comparisons'])
    history = next(item for item in report['comparisons'] if item['detail'] == 'history')
    assert history['parent_keys'] == 1 and history['detail_without_parent'] == 1
    assert len(data['projects'].rows) == 2 and len(data['history'].rows) == 2


def test_focus_and_output_limit_do_not_guess_which_detail_defines_population():
    ws, rs, data = workspace()
    for number in range(7):
        name = f'detail_{number}'
        rs.datasets[name] = rs.datasets['sales'].model_copy(deep=True)
        ws.mapping[name] = ws.mapping['sales'].model_copy(deep=True)
        data[name] = deepcopy(data['sales'])
    report = scope_diagnostics(ws, focus='detail_5', limit=1)
    assert report['comparisons'][0]['detail'] == 'detail_5'
    assert len(report['comparisons']) == 1 and report['omitted'] == 7
    assert len(report['comparisons'][0]['hypothetical_population_from']['other_details_losing_parent']) <= 3
    assert not any(spec.population_from for spec in rs.datasets.values())


def test_spelling_differences_are_labelled_exact_coverage_not_missing_business_objects():
    ws, _, data = workspace()
    data['sales'].rows[0].values['pid'] = '202609-b'
    item = scope_diagnostics(ws, focus='sales')['comparisons'][0]
    assert item['key_match'] == 'exact'
    assert item['detail_without_parent'] == 1
    assert '大小写' in scope_diagnostics(ws)['notes'][0]


def test_data_read_failure_stays_unavailable_not_empty():
    ws, _, _ = workspace()
    def fail(rules):
        raise ValueError('amount is invalid at data.xlsx/sales!4')
    ws._data = fail
    report = scope_diagnostics(ws)
    assert report['status'] == 'UNAVAILABLE'
    assert 'data.xlsx/sales!4' in report['error']
    assert 'comparisons' not in report


def test_full_detail_coverage_does_not_invent_scope_gap():
    ws, rs, data = workspace()
    data['sales'].rows = deepcopy(data['history'].rows)
    report = scope_diagnostics(ws)
    assert report['status'] == 'OBSERVATION'
    assert report['comparisons'] == [] and report['comparison_count'] == 0


def test_returned_mapping_blocker_cannot_turn_partial_data_into_scope_evidence():
    from bonus_core.engine.issues import Issue
    ws, _, data = workspace()
    ws._data = lambda rules: (data, [Issue.block('mapping', 'sales!4 has invalid amount')])
    report = scope_diagnostics(ws)
    assert report['status'] == 'UNAVAILABLE'
    assert 'sales!4' in report['errors'][0]
    assert 'comparisons' not in report


def test_actual_workflow_failure_exposes_scope_and_keeps_all_gates(tmp_path):
    def workbook(wb):
        sheet = wb.create_sheet('sales')
        sheet.append(['pid', 'sales_amount'])
        sheet.append(['B', 200])
    ws = make_workspace(tmp_path, mutate_workbook=workbook)
    ws.define_dataset('sales', 'data.xlsx', 'sales', {
        'pid': {'column': 'pid', 'type': 'id'},
        'sales_amount': {'column': 'sales_amount', 'type': 'money'}}, link='pid')
    ws.drafts['S']['pool']['expr'] = 'min(sales_amount, of: "sales") * 0.1'
    assert ws.prepare_data()['status'] == 'PASS'
    before = deepcopy(ws.drafts), deepcopy(ws.datasets), deepcopy(ws.mapping)
    result = ws.compile_formulas()
    assert result['status'] == 'FAIL'
    scope = result['preflight']['S']['scope_context']
    assert scope['comparisons'][0]['parent_without_detail'] == 1
    detail = ws.read_workspace(section='checks', name='preflight:S')
    assert detail['items'][0]['scope_context'] == scope
    for name in ('formulas', 'basis:S', 'review:S', 'approved:S'):
        assert not ws._flow().current(name)
    assert not ws.results and not ws.llm.calls
    assert (ws.drafts, ws.datasets, ws.mapping) == before
