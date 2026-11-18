"""Basis calculations use real structure but never require unfinished payouts."""
from datetime import date
from decimal import Decimal

import pytest

from bonus_core.data.table import from_records
from bonus_core.dsl.models import Check, ReportTotal, RuleSet, Variable
from bonus_core.dsl.validator import basis_rules, validate, validate_basis, validate_structure
from bonus_core.engine.issues import EngineError
from bonus_core.engine.run import Engine, compute


def example(*, allocation=True):
    rs = RuleSet.model_validate({
        'name': 'basis', 'version': '1',
        'datasets': {
            'projects': {'key': 'pid', 'fields': {'pid': {'type': 'id'}}},
            'sales': {'link': 'pid', 'fields': {
                'pid': {'type': 'id'}, 'sales_amount': {'type': 'money'},
                'first_date': {'type': 'date'}}},
            'people': {'key': 'rid', 'fields': {
                'rid': {'type': 'id'}, 'pid': {'type': 'id'},
                'person': {'type': 'text'}, 'share': {'type': 'ratio'}}},
        },
        'variables': {
            'net': {'scope': 'Project' if allocation else 'projects',
                    'expr': 'sum(sales_amount, of: "sales")'},
            'bonus': {'scope': 'Project' if allocation else 'projects', 'expr': 'net * 0.1'},
            'unfinished_weight': {'scope': 'Person' if allocation else 'people', 'expr': 'missing_factor * share'},
        },
        'pool': {'scope': 'Project' if allocation else 'projects', 'expr': 'bonus'},
        'allocation': [
            {'level': 'Project', 'key': 'pid'},
            {'level': 'Person', 'key': 'rid', 'parent_key': 'pid', 'weight': 'unfinished_weight'},
        ] if allocation else [],
        'report': {'people': 'person', 'details': [
            {'name': 'project_bonus', 'level': 'Project' if allocation else 'projects', 'expr': 'bonus',
             'sources': [{'doc': 'policy.md', 'quote': '每个项目按销售金额的10%计算'}]}]},
    })
    records = {
        'projects': [{'pid': 'A'}, {'pid': 'B'}],
        'sales': [{'pid': 'A', 'sales_amount': 100, 'first_date': date(2026, 1, 1)},
                  {'pid': 'B', 'sales_amount': 200, 'first_date': date(2026, 2, 1)}],
        'people': [{'rid': '1', 'pid': 'A', 'person': '甲', 'share': 1},
                   {'rid': '2', 'pid': 'B', 'person': '乙', 'share': 1}],
    }
    return rs, records


def errors(issues):
    return [i for i in issues if i.severity == 'blocking']


def test_basis_executes_before_distribution_formula_exists_and_does_not_mutate_rules():
    rs, records = example()
    rs.allocation[-1].weight = ''
    original = rs.model_dump_json()
    assert not errors(validate_structure(rs))
    assert not errors(validate_basis(rs))
    assert {i.code for i in errors(validate(rs))} >= {'weight_missing', 'name_unknown'}
    engine = Engine.prepare_basis(rs, from_records(rs, records))
    got = engine.calculate_basis()
    assert got['pool'] == '30.00'
    assert {k: v['amount'] for k, v in got['objects'].items()} == {'Project:A': '10.00', 'Project:B': '20.00'}
    assert set(engine.rs.variables) == {'net', 'bonus'}
    assert rs.model_dump_json() == original
    with pytest.raises(EngineError, match='基础核算上下文'):
        engine.compute()
    with pytest.raises(EngineError):
        compute(rs, from_records(rs, records))


def test_basis_can_compute_from_entity_relationships_before_allocation_exists():
    rs, records = example(allocation=False)
    assert not errors(validate_structure(rs))
    assert not errors(validate_basis(rs))
    assert any(i.code == 'no_levels' for i in validate(rs))
    result = Engine.prepare_basis(rs, from_records(rs, records)).calculate_basis()
    assert result['pool'] == '30.00'
    assert set(result['objects']) == {'projects:A', 'projects:B'}
    assert {k: v['amount'] for k, v in result['details']['project_bonus'].items()} == {'projects:A': '10.00', 'projects:B': '20.00'}


def test_t18_detail_cannot_masquerade_as_allocation_entity():
    rs, records = example()
    rs.datasets['people'].key = None
    rs.datasets['people'].link = 'pid'
    assert any(i.code == 'level_no_entity' for i in validate_structure(rs))
    assert any(i.code == 'leaf_no_entity' for i in validate_basis(rs))
    with pytest.raises(EngineError, match='实体表'):
        Engine.prepare_basis(rs, from_records(rs, records))


@pytest.mark.parametrize('allocation', [False, True])
def test_basis_retains_real_missing_reference_checks(allocation):
    rs, records = example(allocation=allocation)
    records['sales'][0]['pid'] = 'MISSING'
    with pytest.raises(EngineError, match='找不到'):
        Engine.prepare_basis(rs, from_records(rs, records))


def test_scope_validation_does_not_disappear_when_allocation_is_absent():
    rs, _ = example(allocation=False)
    rs.variables['net'].expr = 'sales_amount'
    assert any(i.code == 'detail_outside_agg' for i in validate_basis(rs))


def test_distribution_checks_and_report_rows_are_deferred_only_explicitly():
    rs, records = example()
    rs.checks = [Check(id='future', scope='Person', expr='unfinished_weight > 0', stage='distribution')]
    rs.report.totals = [ReportTotal(name='future_total', level='Person', expr='unfinished_weight', stage='distribution')]
    assert not errors(validate_basis(rs))
    basis = Engine.prepare_basis(rs, from_records(rs, records)).calculate_basis()
    assert basis['totals'] == {}
    assert errors(validate(rs))
    rs.checks[0].stage = 'basis'
    assert any(i.code == 'name_unknown' for i in validate_basis(rs))


def test_basis_check_failure_still_blocks_amounts():
    rs, records = example()
    rs.checks = [Check(id='positive', scope='Project', expr='net > 0')]
    records['sales'][0]['sales_amount'] = 0
    with pytest.raises(EngineError, match='positive'):
        Engine.prepare_basis(rs, from_records(rs, records)).calculate_basis()


def test_basis_dependency_errors_are_not_filtered_away():
    rs, _ = example()
    rs.variables['net'].expr = 'bad_field + 1'
    assert any(i.code == 'name_unknown' and i.where == 'variables.net' for i in validate_basis(rs))
    rs.variables['net'].expr = 'bonus'
    assert any(i.code == 'cycle' for i in validate_basis(rs))


@pytest.mark.parametrize('expr', [
    'sum(first_date, of: "sales")',
    'avg(first_date, of: "sales")',
    'sum(chosen_date, of: "projects")',
])
def test_date_aggregation_is_not_a_valid_scope_repair(expr):
    rs, _ = example()
    rs.variables['chosen_date'] = Variable(scope='Project', expr='min(first_date, of: "sales")')
    rs.variables['net'].expr = expr
    assert any(i.code == 'fn_type' for i in validate_basis(rs))


def test_explicit_date_selection_and_month_difference_remain_supported():
    rs, records = example()
    rs.variables['net'].expr = 'months_between(min(first_date, of: "sales"), date("2026-09-01"))'
    assert not errors(validate_basis(rs))
    result = Engine.prepare_basis(rs, from_records(rs, records)).calculate_basis()
    assert Decimal(result['pool']) == Decimal('1.50')


def test_entity_parent_cycle_is_a_structure_error_even_without_money_formulas():
    rs, _ = example(allocation=False)
    rs.datasets['projects'].fields['rid'] = rs.datasets['people'].fields['rid'].model_copy()
    assert any(i.code == 'dataset_cycle' for i in validate_structure(rs))
