"""Only declared direct fields have enough evidence for these aggregate type gates."""
import pytest

from bonus_core.data.table import from_records
from bonus_core.dsl.models import FieldDef, Variable
from bonus_core.dsl.validator import validate, validate_basis
from bonus_core.engine.preflight import preflight_basis
from tests.test_core_basis_preflight import example


pytestmark = pytest.mark.evidence


def rules(fn, kind):
    rs, records = example(f'{fn}(item, of: "sales")')
    rs.datasets['sales'].fields['item'] = FieldDef(type=kind)
    return rs, records


def types(rs):
    return [issue for issue in validate_basis(rs) if issue.code == 'fn_type']


@pytest.mark.parametrize(('fn', 'kind'), [
    ('min', 'text'), ('max', 'enum'), ('min', 'id'), ('max', 'bool'),
    ('sum', 'text'), ('avg', 'enum'), ('sum', 'date'), ('avg', 'bool'),
])
def test_declared_unsupported_field_is_blocked_at_its_variable(fn, kind):
    rs, _ = rules(fn, kind)
    failures = types(rs)
    assert len(failures) == 1
    assert failures[0].where == 'variables.base'
    assert f'「sales.item」声明为 {kind}' in failures[0].message
    assert fn in failures[0].message
    assert any(issue.code == 'fn_type' and issue.where == 'variables.base' for issue in validate(rs))


@pytest.mark.parametrize('kind', ['money', 'number', 'int', 'ratio'])
def test_numeric_field_types_keep_existing_runtime_support(kind):
    rs, records = rules('sum', kind)
    for row in records['sales']:
        row['item'] = 2
    assert not types(rs)
    assert preflight_basis(rs, from_records(rs, records))['status'] == 'PASS'


@pytest.mark.parametrize('fn', ['min', 'max'])
def test_date_min_max_remain_valid_and_evaluate_as_dates(fn):
    rs, records = rules(fn, 'date')
    rs.variables['base'].expr = f'days_between(date("2026-01-01"), {fn}(item, of: "sales"))'
    records['sales'].append({'pid': '202601-A', 'amount': 1})
    for row in records['sales']:
        row['item'] = '2026-09-01'
    assert not types(rs)
    assert preflight_basis(rs, from_records(rs, records))['status'] == 'PASS'


@pytest.mark.parametrize('fn', ['count', 'count_distinct'])
def test_text_counts_are_not_numeric_aggregations(fn):
    rs, records = rules(fn, 'text')
    if fn == 'count':
        rs.variables['base'].expr = 'count(of: "sales", where: item == "West")'
    records['sales'][0]['item'] = 'West'
    assert not types(rs)
    assert preflight_basis(rs, from_records(rs, records))['status'] == 'PASS'


@pytest.mark.parametrize('expr', [
    'min(item)',
    'min(item, of: "missing")',
    'min(unknown, of: "sales")',
    'min(if(amount > 0, item, "other"), of: "sales")',
    'max(item, 0)',
])
def test_unknown_or_compound_operands_are_not_guessed(expr):
    rs, _ = rules('min', 'text')
    rs.variables['base'].expr = expr
    assert not types(rs)


def test_explicit_dataset_owns_field_even_with_different_type_elsewhere():
    rs, _ = rules('min', 'text')
    rs.datasets['projects'].fields['item'] = FieldDef(type='number')
    assert len(types(rs)) == 1
    rs.datasets['sales'].fields['item'] = FieldDef(type='number')
    rs.datasets['projects'].fields['item'] = FieldDef(type='text')
    assert not types(rs)


def test_existing_date_expression_check_is_kept_without_duplicate_direct_errors():
    rs, _ = rules('avg', 'date')
    assert len(types(rs)) == 1
    rs.variables['when'] = Variable(expr='date("2026-01-01")')
    rs.variables['base'].expr = 'sum(when, of: "sales")'
    failures = types(rs)
    assert len(failures) == 1 and '日期' in failures[0].message


@pytest.mark.parametrize(('kind', 'value', 'bounds'), [
    ('text', '202608', '"202601", "202612"'),
    ('date', '2026-08-01', '"2026-01-01", "2026-12-31"'),
    ('number', 8, '1, 12'),
])
def test_between_checks_the_declared_field_type_in_a_filter(kind, value, bounds):
    rs, records = rules('count', kind)
    rs.variables['base'].expr = f'count(of: "sales", where: between(item, {bounds}))'
    records['sales'][0]['item'] = value
    if kind == 'text':
        failure, = types(rs)
        assert failure.where == 'variables.base' and '「sales.item」声明为 text' in failure.message
    else:
        assert not types(rs)
        assert preflight_basis(rs, from_records(rs, records))['status'] == 'PASS'
