from copy import deepcopy

import pytest

from bonus_agent.indicator_scope import global_indicator_findings, global_probe_variables, indicator_period_findings
from bonus_core.data.table import RowRef, from_records
from bonus_core.dsl.models import RuleSet


def example():
    unique = 'single_value(amount, of: "movements")'
    rs = RuleSet.model_validate({
        'name': 'scope checks', 'version': '1',
        'datasets': {
            'accounts': {'key': 'account_id', 'fields': {'account_id': {'type': 'id'}}},
            'movements': {'link': 'account_id', 'fields': {
                'account_id': {'type': 'id'}, 'amount': {'type': 'money'}}},
        },
        'variables': {
            'first': {'expr': unique}, 'second': {'expr': unique + ' + 1'},
            'company': {'expr': 'sum(amount, of: "movements")'},
            'by_account': {'scope': 'account', 'expr': 'ratio(company, 10)'},
            'constant': {'expr': 'min(5, 8)'},
            'use_constant': {'scope': 'account', 'expr': 'constant + 2'},
            'guarded': {'expr': f'if(false, {unique}, 0)'},
            'filtered': {'expr': 'single_value(amount, of: "movements", where: amount == 7)'},
            'outer': {'expr': f'first + {unique}'},
        },
        'pool': {'expr': '0'}, 'allocation': [{'level': 'account', 'key': 'account_id'}],
    })
    rows = {'accounts': [{'account_id': 'A'}, {'account_id': 'B'}],
            'movements': [{'account_id': 'A', 'amount': 7}, {'account_id': 'B', 'amount': 19}]}
    data = from_records(rs, rows)
    for i, row in enumerate(data['movements'].rows, 2):
        row.src = RowRef('sales.xlsx', 'sales', i)
    return rs, data


def test_independent_errors_use_engine_selected_rows_and_owner():
    rs, data = example()
    result = global_indicator_findings(rs, data)
    assert set(result) == {'first', 'second', 'company', 'guarded', 'filtered'}
    for name in ('first', 'second'):
        issue, = result[name]
        assert issue['state'] == 'CONFLICT' and issue['code'] == 'single_value_multiple'
        assert issue['selected_rows'] == 2
        assert [v['aggregate_source'] for v in issue['competing_values']] == [
            'sales.xlsx/sales!2', 'sales.xlsx/sales!3']
    # An organisation-wide total in a grouped calculation can be required by policy.
    clue, run = result['company']
    assert clue['state'] == 'REVIEW' and clue['code'] == 'global_grouped_consumer'
    assert clue['consumers'] == [{'variable': 'by_account', 'scope': 'account', 'objects': 2}]
    assert run['state'] == 'VERIFIED' and run['dimension'] == 'execution' and run['value'] == '26'
    for name, value in [('guarded', '0'), ('filtered', '7')]:
        executed, = result[name]
        assert executed['code'] == 'global_execution' and executed['value'] == value


def test_valid_global_unique_value_and_per_object_consumer_with_single_object():
    rs, data = example()
    data['accounts'].rows.pop()
    data['movements'].rows[1].values.update(account_id='A', amount=data['movements'].rows[0].values['amount'])
    result = global_indicator_findings(rs, data)
    assert all(row['state'] == 'VERIFIED' for rows in result.values() for row in rows)


def test_preparation_and_repair_probe_do_not_change_raw_values():
    rs, data = example()
    frozen_rs, frozen_rows = rs.model_dump(), deepcopy(data)
    result = global_indicator_findings(rs, data, variables=['second', 'guarded', 'filtered'])
    assert set(result) == {'second', 'guarded', 'filtered'}
    assert data == frozen_rows
    assert rs.model_dump() == frozen_rs


def test_query_without_io_or_aggregates():
    rs, _ = example()
    assert global_probe_variables(rs) == {'first', 'second', 'company', 'guarded', 'filtered', 'outer'}
    rs.variables = {'constant': rs.variables['constant']}
    assert global_probe_variables(rs) == set()


def test_history_requires_selected_source_and_observed_alternative_months():
    report = [{'dataset': 'ledger', 'window': 'year_to_date', 'requested_period': '2026-01 至 2026-09',
               'file': 'september.xlsx', 'sheet': 'sales', 'mapped_months': {'2026-09': 12},
               'alternatives': [{'file': 'history.xlsx', 'sheet': 'summary',
                                 'additional_months': ['2026-07', '2026-08']}]}]
    expr = 'sum(amount, of: "ledger") / count_distinct(month(date), of: "ledger")'
    clue, = indicator_period_findings(expr, report)
    assert clue['state'] == 'REVIEW' and clue['code'] == 'additional_source_months'
    assert clue['alternatives'][0]['additional_months'] == ['2026-07', '2026-08']
    assert not indicator_period_findings('sum(amount, of: "current")', report)
    # Both January and a customer whose only active month is September are legitimate.
    for month in ('2026-01', '2026-09'):
        report[0].update(mapped_months={month: 3}, alternatives=[])
        assert not indicator_period_findings(expr, report)


@pytest.mark.parametrize('expr', ['min(4, 5)', 'if(true, 1, 0)', 'sum(amount, of: "'])
def test_unrelated_or_incomplete_formulas_produce_no_fabricated_evidence(expr):
    assert not indicator_period_findings(expr, [])
