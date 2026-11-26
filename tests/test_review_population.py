"""Population comparison must detect mistakes even when two amount formulas agree."""
import copy
import json
from types import SimpleNamespace

import pytest

from bonus_agent.llm import FakeLLM
from bonus_agent.facts import norm
from bonus_agent.review_population import _source_error, check_populations, requirements
from bonus_core.dsl.phases import basis_rules
from bonus_core.engine.run import Engine
from tests.test_staged_workflow import make_workspace, population_spec, prepared


def inspect(ws, populations):
    rs = basis_rules(ws._ruleset('S'))
    data, _ = ws._data(rs)
    engine = Engine.prepare_basis(rs, data)
    return check_populations(ws, rs, {'populations': populations}, engine.root, engine.by_level)


def test_independent_source_population_sees_rows_removed_by_production_filter(tmp_path):
    ws = make_workspace(tmp_path)
    ws.mapping['projects'].filter = {'pid': ['A']}
    ws.mapping['people'].filter = {'pid': ['A']}
    result = inspect(ws, population_spec())
    assert result['status'] == 'FAIL' and result['contract_valid']
    assert result['checks'][0]['missing'] == ['B']
    assert ws.mapping['projects'].filter == {'pid': ['A']}
    assert not ws.llm.calls and not ws.workflow['basis']


@pytest.mark.parametrize('second_period, expected', [('202609', 'PASS'), ('202608', 'FAIL')])
def test_review_can_read_an_unmapped_period_header_without_changing_production(tmp_path, second_period, expected):
    from bonus_core.data.period import Period

    def add_period(book):
        for row, value in enumerate(('出货月份', '202609', second_period), 1):
            book['projects'].cell(row, 3, value)

    ws = make_workspace(tmp_path, mutate_workbook=add_period)
    ws.period = Period.parse('2026-09')
    spec = population_spec()
    for entry in spec.values():
        entry['period'] = '出货月份'
    result = inspect(ws, spec)
    assert result['contract_valid'] and result['status'] == expected
    if expected == 'FAIL':
        assert next(c for c in result['checks'] if c['scope'] == '项目')['extra'] == ['B']
    assert ws.datasets['projects'].period is None and '出货月份' not in ws.mapping['projects'].columns.values()


def test_equal_count_with_different_object_ids_is_not_a_match(tmp_path):
    ws = make_workspace(tmp_path)
    ws.mapping['projects'].filter = {'pid': ['A']}
    ws.mapping['people'].filter = {'pid': ['A']}
    spec = population_spec()
    spec['项目']['filter'] = {'pid': ['B']}
    result = inspect(ws, spec)
    assert result['status'] == 'FAIL'
    assert result['checks'][0]['actual_count'] == result['checks'][0]['expected_count'] == 1
    assert result['checks'][0]['extra'] == ['A'] and result['checks'][0]['missing'] == ['B']


def test_missing_or_unverified_population_contract_cannot_pass(tmp_path):
    ws = make_workspace(tmp_path)
    assert not inspect(ws, {})['contract_valid']
    spec = population_spec()
    spec['项目']['sources'][0]['quote'] = '材料没有这个规则'
    assert not inspect(ws, spec)['contract_valid']
    spec = population_spec()
    del spec['项目']['filter']
    assert not inspect(ws, spec)['contract_valid']


@pytest.mark.parametrize('with_label', [False, True])
def test_registered_policy_in_workbook_can_look_like_a_header(with_label):
    quote = '只核算实际发货的项目'
    policy = 'policy.xlsx'
    full_row = '参与条件 | ' + quote
    ws = SimpleNamespace(
        inv=SimpleNamespace(sheets=[SimpleNamespace(file=policy, sheet='规则', headers=['参与条件', quote])]),
        workflow={'plan': {'files': {policy: 'rule'}}},
        clauses=[],
        mapping={},
        _corpus=lambda: {policy: norm(full_row)},
    )
    mapping = SimpleNamespace(file='details.xlsx', sheet='明细')
    scope = {'sources': [{'doc': policy, 'quote': full_row if with_label else quote}]}
    assert _source_error(ws, mapping, scope, 'objects')[0] == 'scope_header_other_table'

    ws.clauses.append(SimpleNamespace(file=policy, quote=quote))
    assert _source_error(ws, mapping, scope, 'objects') is None

    ws.mapping['projects'] = SimpleNamespace(file=policy, sheet='规则')
    assert _source_error(ws, mapping, scope, 'objects')[0] == 'scope_header_other_table'


def test_global_only_pool_checks_physical_source_rows(tmp_path):
    ws = make_workspace(tmp_path)
    ws.drafts['S']['pool'] = {'scope': 'global', 'expr': 'sum(amount, of: "projects") * 0.1'}
    ws.drafts['S']['report'] = None
    ws.mapping['projects'].filter = {'pid': ['A']}
    ws.mapping['people'].filter = {'pid': ['A']}
    rs = basis_rules(ws._ruleset('S'))
    assert requirements(rs) == {'rows:projects': {'kind': 'rows', 'dataset': 'projects'}}
    spec = {'rows:projects': {k: v for k, v in population_spec()['项目'].items() if k != 'key'}}
    result = inspect(ws, spec)
    assert result['status'] == 'FAIL' and result['checks'][0]['expected_count'] == 2


def test_agreeing_amounts_do_not_approve_wrong_population(tmp_path):
    ws = make_workspace(tmp_path)
    ws.mapping['projects'].filter = {'pid': ['A']}
    ws.mapping['people'].filter = {'pid': ['A']}
    prepared(ws)
    ws.calculate_basis('S')
    before = copy.deepcopy(ws.workflow['basis'])
    result = ws.review_basis('S')
    assert result['status'] == 'FAIL' and result['population']['checks'][0]['missing'] == ['B']
    assert not ws._flow().current('review:S')
    assert ws.workflow['basis'] == before
    assert ws.review_basis('S')['cached']
    assert len(ws.llm.checker_llm.calls) == 1


def test_correct_objects_and_agreeing_formulas_do_not_hide_filtered_business_rows(tmp_path):
    ws = make_workspace(tmp_path)
    ws.define_dataset('sales', 'data.xlsx', 'projects', {
        'pid': {'column': 'pid', 'type': 'id'},
        'sale_amount': {'column': 'amount', 'type': 'money'},
    }, link='pid')
    ws.mapping['sales'].filter = {'pid': ['A']}
    expr = 'sum(sale_amount, of: "sales") * 0.1'
    ws.drafts['S']['pool']['expr'] = expr
    ws.drafts['S']['report'] = None
    rs = basis_rules(ws._ruleset('S'))
    data, _ = ws._data(rs)
    engine = Engine.prepare_basis(rs, data)
    amounts = engine.calculate_basis()
    spec = {'pool': expr, 'helpers': {}, 'tables': {}, 'populations': population_spec()}
    source_rows = copy.deepcopy(spec['populations']['项目'])
    source_rows.update(dataset='sales')
    source_rows.pop('key')
    spec['populations']['rows:sales'] = source_rows
    _, root, levels, evaluator = ws._independent_env('S', spec, basis=True)
    independent = ws._independent_eval(evaluator, expr, levels[rs.pool.scope], 'review')
    # Both formulas see the incorrectly filtered child table and agree on 10.
    # The fixture's source has A=100 and B=200, so its policy requires 30.
    assert amounts['pool'] == '10.00'
    assert sum(independent.values()) == 10
    assert len(amounts['objects']) == 2
    result = check_populations(ws, rs, spec, root, levels)
    assert result['status'] == 'FAIL' and result['contract_valid']
    assert next(c for c in result['checks'] if c['scope'] == rs.pool.scope)['status'] == 'PASS'
    rows = next(c for c in result['checks'] if c['scope'] == 'rows:sales')
    assert rows['expected_count'] == 2 and rows['actual_count'] == 1
    assert len(rows['missing']) == 1
    assert not ws.llm.calls and not ws.llm.checker_llm.calls


def test_valid_distinct_entities_compare_source_rows_before_grouping(tmp_path):
    ws = make_workspace(tmp_path, lambda wb: wb['projects'].append(['A', 100]), project_distinct=True)
    result = inspect(ws, population_spec())
    assert result['status'] == 'PASS', result
    rows = next(c for c in result['checks'] if c['scope'] == 'rows:projects')
    assert rows['actual_count'] == rows['expected_count'] == 3


def test_derived_entity_scope_preserves_duplicate_physical_rows_for_active_keys(tmp_path):
    ws = make_workspace(tmp_path, lambda wb: wb['projects'].append(['A', 100]), project_distinct=True)
    ws.define_dataset('current_sales', 'data.xlsx', 'projects', {
        'pid': {'column': 'pid', 'type': 'id'}, 'sale_amount': {'column': 'amount', 'type': 'money'},
    }, link='pid', filter={'pid': ['A']})
    ws.datasets['projects'].population_from = 'current_sales'
    ws.mapping['people'].filter = {'pid': ['A']}
    ws.drafts['S']['report'] = None
    ws.drafts['S']['pool']['expr'] = 'amount * 0.1 + sum(sale_amount, of: "current_sales") * 0'
    spec = population_spec()
    spec['项目'].update(dataset='current_sales', filter={'pid': ['A']})
    spec['rows:current_sales'] = {k: v for k, v in spec['项目'].items() if k != 'key'}
    spec['rows:projects']['filter'] = {'pid': ['A']}
    result = inspect(ws, spec)
    assert result['status'] == 'PASS', result
    rows = next(c for c in result['checks'] if c['scope'] == 'rows:projects')
    assert rows['actual_count'] == rows['expected_count'] == 2
    spec['rows:projects']['filter'] = {}
    assert inspect(ws, spec)['status'] == 'FAIL'


def test_blank_value_exclusion_has_specific_evidence_and_keeps_zero(tmp_path):
    def values(workbook):
        workbook['projects']['B2'] = 0
        workbook['projects']['B3'] = None

    ws = make_workspace(tmp_path, values)
    ws.update_dataset('projects', {'fields': {'amount': {'if_empty': 'skip'}}})
    rs = basis_rules(ws._ruleset('S'))
    scopes = population_spec()
    label = 'rows:projects'
    result = check_populations(ws, rs, {'populations': scopes}, None, {}, scope_names=[label])
    observed, = result['checks']
    assert result['status'] == 'FAIL' and result['contract_valid']
    assert observed['difference'] == {'code': 'scope_blank_value_policy', 'source_columns': ['amount'],
                                      'source_cells': [{'file': 'data.xlsx', 'sheet': 'projects', 'row': 3,
                                                        'empty_columns': ['amount']}]}
    assert observed['missing'] == ['data.xlsx/projects!3']
    scopes[label]['filter'] = {'amount': {'not': ['']}}
    corrected = check_populations(ws, rs, {'populations': scopes}, None, {}, scope_names=[label])
    assert corrected['status'] == 'PASS'
    assert corrected['checks'][0]['expected_count'] == corrected['checks'][0]['actual_count'] == 1


def test_proven_identifier_spelling_keeps_alias_evidence(tmp_path):
    def alter(wb):
        sheet = wb.create_sheet('sales')
        sheet.append(['pid'])
        sheet.append(['a'])
        sheet.append(['a'])
        sheet.append(['B'])
    ws = make_workspace(tmp_path, alter)
    ws.define_dataset('sales', 'data.xlsx', 'sales', {'pid': {'column': 'pid', 'type': 'id'}}, link='pid')
    spec = population_spec()
    spec['项目']['dataset'] = 'sales'
    result = inspect(ws, spec)
    assert result['status'] == 'PASS', result
    objects = next(c for c in result['checks'] if c['scope'] == '项目')
    assert objects['verified_spelling_aliases'] == {'a': 'A'}
    assert (objects['reference_rows'], objects['expected_count'], objects['repeated_key_rows']) == (3, 2, 1)


def test_source_page_is_checked_when_declared(tmp_path):
    ws = make_workspace(tmp_path)
    spec = population_spec()
    spec['项目']['sources'][0]['page'] = 999999
    result = inspect(ws, spec)
    assert not result['contract_valid'] and '999999' in str(result['issues'])


def test_numeric_filter_literal_uses_existing_cell_match_without_losing_leading_zeroes():
    from bonus_core.data.mapping import DatasetMapping, row_filter

    mapping = DatasetMapping.model_validate({'columns': {}, 'filter': {'amount': {'not': [0]}, 'id': ['0012']}})
    keep = row_filter(['amount', 'id'], mapping.filter)
    assert keep([1, '0012'])
    assert keep([None, '0012'])
    assert not keep([0, '0012']) and not keep(['0.00', '0012'])
    assert not keep([1, 12])
