"""Unique scalar assertions use real object scope and never select an arbitrary row."""
from datetime import date
from decimal import Decimal

import openpyxl
import pytest

from bonus_core.data.table import from_records
from bonus_core.dsl import excel_formula
from bonus_core.dsl.functions import describe
from bonus_core.dsl.models import RuleSet, Variable
from bonus_core.dsl.parser import parse, unparse
from bonus_core.dsl.scope import is_aggregate
from bonus_core.dsl.validator import validate, validate_basis
from bonus_core.engine.evaluator import EvalError
from bonus_core.engine.issues import EngineError
from bonus_core.engine.preflight import preflight_basis
from bonus_core.engine.run import Engine
from bonus_core.report.excel import write_excel


pytestmark = pytest.mark.evidence


def example(values=('north', 'north'), *, kind='text', expr=None):
    rs = RuleSet.model_validate({
        'name': 'unique attributes', 'version': '1',
        'datasets': {
            'projects': {'key': 'pid', 'fields': {'pid': {'type': 'id'}}},
            'details': {'link': 'pid', 'fields': {
                'pid': {'type': 'id'}, 'item': {'type': kind},
                'position': {'type': 'int'}}},
        },
        'variables': {
            'attribute': {'scope': 'Project', 'expr': expr or 'single_value(item, of: "details")'},
            'amount': {'scope': 'Project', 'expr': 'if(attribute == "north", 10, 20)'},
        },
        'pool': {'scope': 'Project', 'expr': 'amount'},
        'allocation': [{'level': 'Project', 'key': 'pid'}],
    })
    data = from_records(rs, {
        'projects': [{'pid': 'A'}],
        'details': [{'pid': 'A', 'item': value, 'position': index}
                    for index, value in enumerate(values, 1)],
    }, file='input.xlsx')
    return rs, data


def attribute(rs, data):
    eng = Engine.prepare(rs, data)
    return eng.ev.variable('attribute', eng.by_level['Project'][0]).v


def test_parser_signature_and_shared_primary_review_guidance():
    expr = 'single_value(item, of: "details", where: position > 0)'
    assert is_aggregate(parse(expr))
    assert parse(unparse(parse(expr))) == parse(expr)
    doc = describe()
    assert 'single_value' in doc and '不忽略空值' in doc and '不取第一条' in doc
    rs, _ = example(expr=expr)
    assert not [i for i in validate(rs) if i.severity == 'blocking']


@pytest.mark.parametrize('expr', [
    'single_value(of: "details")',
    'single_value(item, item, of: "details")',
    'single_value(item, of: "details", default: "north")',
    'single_value(item, of: "missing")',
])
def test_invalid_signature_or_dataset_is_blocked_before_execution(expr):
    rs, data = example(expr=expr)
    with pytest.raises(EngineError):
        Engine.prepare(rs, data)


@pytest.mark.parametrize(('kind', 'input_value', 'expected'), [
    ('text', 'north', 'north'), ('id', '001', '001'), ('enum', 'north', 'north'),
    ('number', '2.50', Decimal('2.50')), ('date', '2026-09-01', date(2026, 9, 1)),
    ('bool', False, False),
])
def test_duplicate_equal_values_preserve_scalar_type(kind, input_value, expected):
    rs, data = example([input_value, input_value], kind=kind)
    value = attribute(rs, data)
    assert type(value) is type(expected) and value == expected


@pytest.mark.parametrize('values', [('north', 'North'), ('north', ' north'), ('001', '1')])
def test_evaluator_does_not_normalize_distinct_text(values):
    rs, data = example()
    # Already-typed rows bypass the input reader's existing whitespace handling.
    for row, value in zip(data['details'].rows, values):
        row.values['item'] = value
    with pytest.raises(EvalError, match='single_value.multiple'):
        attribute(rs, data)


@pytest.mark.parametrize('missing', [None, '', '   '])
def test_any_blank_row_blocks_instead_of_using_other_rows(missing):
    rs, data = example()
    data['details'].rows[-1].values['item'] = missing
    report = preflight_basis(rs, data)
    assert report['status'] == 'FAIL'
    issue = report['issues'][0]
    # Null fields are rejected even earlier by the normal field accessor.
    assert ('字段 item 为空' if missing is None else 'single_value.missing') in issue['message']
    assert issue['coverage'][0]['active_row_source'] == 'input.xlsx/details!2'


@pytest.mark.parametrize('filtered', [False, True])
def test_zero_selected_rows_block_with_exact_scope_coverage(filtered):
    expr = 'single_value(item, of: "details", where: position > 9)' if filtered else None
    rs, data = example(['north'] if filtered else [], expr=expr)
    report = preflight_basis(rs, data)
    assert report['status'] == 'FAIL'
    issue = report['issues'][0]
    assert issue['node_id'] == 'Project:A'
    assert 'single_value.empty' in issue['message']
    coverage = issue['coverage'][0]
    assert coverage['selected_rows'] == 0
    assert coverage['visible_rows'] == int(filtered)


def test_where_selects_only_explicitly_matching_rows():
    rs, data = example(['north', 'south'], expr='single_value(item, of: "details", where: position == 1)')
    assert attribute(rs, data) == 'north'
    assert preflight_basis(rs, data)['status'] == 'PASS'


def test_conflicting_row_beyond_sample_is_identified_and_never_computed():
    rs, data = example(['north'] * 4 + ['south'])
    report = preflight_basis(rs, data)
    issue = report['issues'][0]
    assert report['status'] == 'FAIL' and 'single_value.multiple' in issue['message']
    coverage = issue['coverage'][0]
    assert coverage['conflicting_sources'] == ['input.xlsx/details!1', 'input.xlsx/details!5']
    assert coverage['active_row_source'] == 'input.xlsx/details!5'
    assert coverage['active_row_source'] not in coverage['selected_sources']
    with pytest.raises(EngineError, match='single_value.multiple'):
        Engine(rs, data).compute()


def test_boolean_and_number_are_distinct_even_when_python_considers_them_equal():
    rs, data = example(expr='single_value(if(position == 1, true, 1), of: "details")')
    with pytest.raises(EvalError, match='single_value.multiple'):
        attribute(rs, data)


@pytest.mark.parametrize('invalid', [Decimal('NaN'), float('inf'), ['north'], {'item': 'north'}])
def test_unsupported_or_nonfinite_runtime_scalar_is_rejected(invalid):
    rs, data = example(['north'])
    data['details'].rows[0].values['item'] = invalid
    with pytest.raises(EvalError, match='single_value.type'):
        attribute(rs, data)


def test_date_result_remains_date_for_static_numeric_aggregate_checks():
    rs, _ = example(['2026-09-01'], kind='date')
    rs.variables['total'] = Variable(scope='global', expr='sum(attribute, of: "projects")')
    failures = [i for i in validate(rs) if i.code == 'fn_type']
    assert len(failures) == 1 and failures[0].where == 'variables.total'


def test_unique_aggregate_preserves_scope_and_relationship_guards():
    rs, data = example()
    rs.variables['attribute'].expr = 'single_value(item, of: "projects")'
    assert any(i.severity == 'blocking' for i in validate_basis(rs))
    rs, data = example()
    data['details'].rows[0].values['pid'] = 'unknown'
    with pytest.raises(EngineError, match='unknown'):
        Engine.prepare_basis(rs, data)


def test_basis_full_compute_trace_and_export_keep_exact_object_values(tmp_path):
    rs, _ = example()
    data = from_records(rs, {
        'projects': [{'pid': 'A'}, {'pid': 'B'}],
        'details': [
            {'pid': 'A', 'item': 'north', 'position': 1},
            {'pid': 'A', 'item': 'north', 'position': 2},
            {'pid': 'B', 'item': 'south', 'position': 1},
        ],
    }, file='input.xlsx')
    assert preflight_basis(rs, data)['status'] == 'PASS'
    basis = Engine.prepare_basis(rs, data).calculate_basis()
    result = Engine(rs, data).compute()
    assert basis['pool'] == result.pool == '30.00'
    assert result.amount('Project:A') == Decimal('10.00')
    assert result.amount('Project:B') == Decimal('20.00')
    trace = result.traces['Project:A/attribute']
    assert trace.value == 'north'
    assert set(trace.rows) == {'input.xlsx/details!1', 'input.xlsx/details!2'}
    assert result.inputs['Project:A']['attribute'] == 'north'
    assert not excel_formula.translatable(rs.variables['attribute'].expr, rs)
    path = write_excel(rs, result, tmp_path / 'single_value.xlsx')
    book = openpyxl.load_workbook(path, data_only=False)
    sheet = book['Project核算底稿']
    headers = [cell.value for cell in sheet[2]]
    col = headers.index('attribute') + 1
    assert sheet.cell(1, col).value == '取数（引擎）'
    assert [sheet.cell(row, col).value for row in (3, 4)] == ['north', 'south']
