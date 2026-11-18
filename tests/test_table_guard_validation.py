"""Table applicability is executable code, including its hidden dependencies."""
import pytest

from bonus_core.data.table import from_records
from bonus_core.dsl.models import RuleSet, Variable
from bonus_core.dsl.phases import basis_rules
from bonus_core.dsl.validator import validate, validate_basis
from bonus_core.engine.run import Engine


def rules(guard='region == "North"'):
    return RuleSet.model_validate({
        'name': 'guards', 'version': '1',
        'datasets': {'projects': {'key': 'pid', 'fields': {
            'pid': {'type': 'id'}, 'region': {'type': 'text'}, 'amount': {'type': 'money'}}}},
        'tables': {'rates': {'kind': 'tier', 'mode': 'whole', 'steps': [[0, '0.1']], 'applies_when': guard}},
        'pool': {'scope': 'Project', 'expr': 'tier(rates, amount)'},
        'allocation': [{'level': 'Project', 'key': 'pid'}],
    })


@pytest.mark.parametrize('guard,code', [
    ('区域 == "North" and 新老项目 == "新项目"', 'name_unknown'),
    ('region ==', 'syntax'), ('invented(region)', 'fn_unknown'),
    ('count(of: "missing") > 0', 'fn_of'),
])
def test_invalid_guard_is_rejected_before_execution_at_exact_table_path(guard, code):
    rs = rules(guard)
    for check in (validate_basis, validate):
        issues = check(rs)
        assert any(i.code == code and i.where == 'tables.rates.applies_when' for i in issues)


def test_valid_guard_uses_caller_scope_and_preserves_guard_only_helper_dependency():
    rs = rules('allowed')
    rs.variables['allowed'] = Variable(scope='Project', expr='region == "North"')
    projected = basis_rules(rs)
    assert 'allowed' in projected.variables
    assert not [i for i in validate_basis(rs) if i.severity == 'blocking']
    data = from_records(rs, {'projects': [{'pid': 'A', 'region': 'North', 'amount': 100}]})
    assert Engine.prepare_basis(rs, data).calculate_basis()['pool'] == '10.00'


def test_guard_field_is_allowed_in_row_aggregate_but_not_direct_global_call():
    rs = rules()
    rs.pool.scope = 'global'
    rs.pool.expr = 'sum(tier(rates, amount), of: "projects")'
    assert not [i for i in validate_basis(rs) if i.severity == 'blocking']
    rs.pool.expr = 'tier(rates, 100)'
    assert any(i.where == 'tables.rates.applies_when' and i.code == 'scope_field' for i in validate_basis(rs))


@pytest.mark.parametrize('indirect', [False, True])
def test_guard_cycles_through_tables_or_helpers_are_rejected_without_recursing(indirect):
    rs = rules('allowed' if indirect else 'tier(rates, 1) > 0')
    if indirect:
        rs.variables['allowed'] = Variable(scope='Project', expr='tier(rates, 1) > 0')
    assert any(i.code == 'cycle' for i in validate_basis(rs))


def test_distribution_only_guard_stays_deferred_until_full_validation():
    rs = rules()
    rs.tables['later'] = rs.tables['rates'].model_copy(update={'applies_when': 'undefined_later > 0'})
    assert not [i for i in validate_basis(rs) if i.severity == 'blocking']
    assert any(i.where == 'tables.later.applies_when' and i.code == 'name_unknown' for i in validate(rs))
