"""Amount conservation must hold before confirming the pre-distribution basis."""
from decimal import Decimal

import pytest

from bonus_core.data.table import from_records
from bonus_core.dsl.models import ReportBalance, RuleSet
from bonus_core.dsl.phases import basis_rules
from bonus_core.dsl.validator import validate_basis
from bonus_core.engine.issues import EngineError
from bonus_core.engine.run import Engine, compute


def example():
    rs = RuleSet.model_validate({
        'name': 'balance', 'version': '1',
        'datasets': {'projects': {'key': 'pid', 'fields': {
            'pid': {'type': 'id'}, 'base_amount': {'type': 'money'},
            'debt_amount': {'type': 'money'}, 'adjustment': {'type': 'money'},
        }}},
        'tables': {'debt_coeff': {'kind': 'lookup', 'mapping': {'standard': 1}}},
        'variables': {
            'gross': {'scope': 'Project', 'expr': 'base_amount'},
            'release': {'scope': 'Project', 'expr': 'gross - debt_amount'},
            'withheld': {'scope': 'Project', 'expr': 'debt_amount * lookup(debt_coeff, "standard")'},
            'future': {'scope': 'Project', 'expr': 'unfinished_person_rule'},
        },
        'allocation': [{'level': 'Project', 'key': 'pid'}],
        'pool': {'scope': 'Project', 'expr': 'release'},
        'report': {'balances': [{
            'name': 'debt decomposition', 'level': 'Project', 'total': 'gross',
            'parts': ['release', 'withheld'],
            'sources': [{'doc': 'policy.md', 'quote': '应得总额等于可发金额与欠款暂扣之和'}],
        }]},
    })
    records = {'projects': [{'pid': 'A', 'base_amount': 1000, 'debt_amount': 100, 'adjustment': 0}]}
    return rs, records


def basis(rs, records):
    return Engine.prepare_basis(rs, from_records(rs, records)).calculate_basis()


def test_basis_preserves_balance_only_dependencies_tables_and_source():
    rs, records = example()
    original = rs.model_dump_json()
    projected = basis_rules(rs)
    assert set(projected.variables) == {'gross', 'release', 'withheld'}
    assert set(projected.tables) == {'debt_coeff'}
    assert projected.report.balances[0].sources == rs.report.balances[0].sources
    got = basis(rs, records)
    assert got['pool'] == '900.00'
    assert got['traces']['Project:A/对账/debt decomposition/2']['value'] == '100'
    assert rs.model_dump_json() == original


def test_duplicate_debt_deduction_blocks_basis_before_confirmation():
    rs, records = example()
    rs.variables['release'].expr = 'gross - debt_amount * 2'
    with pytest.raises(EngineError) as error:
        basis(rs, records)
    assert error.value.issues[0].code == 'report_unbalanced'
    assert error.value.issues[0].where == 'Project:A'
    assert '1000' in str(error.value) and '800' in str(error.value)


def test_basis_checks_each_object_even_when_errors_cancel_in_the_total():
    rs, records = example()
    records['projects'][0]['adjustment'] = 1
    records['projects'].append({'pid': 'B', 'base_amount': 1000, 'debt_amount': 100, 'adjustment': -1})
    rs.variables['release'].expr = 'gross - debt_amount + adjustment'
    # 901 + 899 + 100 + 100 == 2000, but neither object's decomposition is valid.
    with pytest.raises(EngineError, match='不闭合'):
        basis(rs, records)


def test_distribution_balance_is_deferred_explicitly_but_checked_in_final_run():
    rs, records = example()
    future = ReportBalance(name='later', level='Project', total='future', parts=['release'],
                           stage='distribution', sources=rs.report.balances[0].sources)
    rs.report.balances.append(future)
    assert not [i for i in validate_basis(rs) if i.severity == 'blocking']
    assert basis(rs, records)['pool'] == '900.00'
    assert [b.name for b in basis_rules(rs).report.balances] == ['debt decomposition']
    rs.variables['future'].expr = 'release + 1'
    with pytest.raises(EngineError, match='later'):
        compute(rs, from_records(rs, records))
    future.stage = 'basis'
    with pytest.raises(EngineError, match='later'):
        basis(rs, records)


@pytest.mark.parametrize('final', [False, True])
def test_source_is_required_in_both_stages(final):
    rs, records = example()
    rs.variables.pop('future')
    rs.report.balances[0].sources = []
    with pytest.raises(EngineError) as error:
        (compute(rs, from_records(rs, records)) if final else basis(rs, records))
    assert error.value.issues[0].code == 'report_balance_source'


def test_no_implicit_tolerance_is_added_to_basis_checks():
    rs, records = example()
    rs.report.balances[0].parts[-1] = 'withheld + 0.004'
    with pytest.raises(EngineError, match='不闭合'):
        basis(rs, records)
    rs.report.balances[0].rounding = 'cent'
    assert Decimal(basis(rs, records)['pool']) == Decimal(900)
    rs.report.balances[0].parts[-1] = 'withheld + 0.006'
    with pytest.raises(EngineError, match='不闭合'):
        basis(rs, records)


def test_invalid_balance_only_dependency_is_not_pruned_from_basis_validation():
    rs, _ = example()
    rs.variables['withheld'].expr = 'missing_debt_field'
    assert any(i.code == 'name_unknown' and i.where == 'variables.withheld'
               for i in validate_basis(rs))
