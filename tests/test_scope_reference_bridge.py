"""A detail row's auxiliary key is not an attribute of its parent object."""
from decimal import Decimal

import pytest

from bonus_core.data.table import from_records
from bonus_core.dsl.models import DatasetDef, FieldDef, RuleSet
from bonus_core.dsl.validator import validate, validate_basis
from bonus_core.engine.evaluator import EvalError, Evaluator
from bonus_core.engine.run import Engine
from bonus_core.engine.tree import build_tree


def example(expr='region_factor', *, entity_key=False, auxiliary=False):
    scope = 'projects' if auxiliary else 'Project'
    rs = RuleSet.model_validate({
        'name': 'reference bridge', 'version': '1',
        'datasets': {
            'projects': {'key': 'pid', 'fields': {
                'pid': {'type': 'id'}, **({'region': {'type': 'id'}} if entity_key else {})}},
            'sales': {'link': 'pid', 'fields': {
                'pid': {'type': 'id'}, 'region': {'type': 'id'}, 'amount': {'type': 'money'}}},
            'regions': {'key': 'region', 'fields': {
                'region': {'type': 'id'}, 'budget': {'type': 'money'}}},
            'company_rows': {'fields': {'region': {'type': 'id'}}},
        },
        'variables': {
            'actual': {'scope': 'regions', 'expr': 'sum(amount, of: "sales")'},
            'region_factor': {'scope': 'regions', 'expr': 'ratio(actual, budget)'},
            'total': {'scope': scope, 'expr': expr},
        },
        'pool': {'scope': scope, 'expr': 'total'},
        'allocation': [] if auxiliary else [{'level': 'Project', 'key': 'pid'}],
    })
    records = {
        'projects': [{'pid': pid, **({'region': 'north'} if entity_key else {})} for pid in ('A', 'B')],
        'sales': [{'pid': 'A', 'region': 'north', 'amount': 10},
                  {'pid': 'A', 'region': 'north', 'amount': 20},
                  {'pid': 'B', 'region': 'north', 'amount': 30}],
        'regions': [{'region': 'north', 'budget': 120}],
        'company_rows': [],
    }
    return rs, records


def test_scope_hint_names_only_reachable_detail_bridges_without_adding_coefficients():
    rs, _ = example()
    issues = [i for i in validate(rs) if i.code == 'scope_aux']
    assert len(issues) == 1
    msg = issues[0].message
    assert 'sales' in msg and 'company_rows' not in msg
    assert 'single_value(region_factor, of: "sales")' in msg
    assert '原文明确' in msg and '空值或多值仍阻断' in msg
    assert 'sum(region_factor' not in msg


def test_detail_reference_does_not_allow_direct_auxiliary_aggregation():
    rs, records = example('sum(region_factor, of: "regions")')
    for check in (validate, validate_basis):
        issues = [i for i in check(rs) if i.code == 'agg_above']
        assert len(issues) == 1
        assert 'single_value(region_factor, of: "sales")' in issues[0].message
        assert 'company_rows' not in issues[0].message
    # The tightened static check agrees with the unmodified runtime semantics.
    root, levels, issues = build_tree(rs, from_records(rs, records))
    assert not [i for i in issues if i.severity == 'blocking']
    with pytest.raises(EvalError, match='取不到'):
        Evaluator(rs, root, levels).variable('total', levels['Project'][0])


@pytest.mark.parametrize('auxiliary', [False, True])
def test_explicit_detail_bridge_uses_all_region_sales_once_per_project(auxiliary):
    rs, records = example('single_value(region_factor, of: "sales")', auxiliary=auxiliary)
    assert not [i for i in validate_basis(rs) if i.severity == 'blocking']
    engine = Engine.prepare_basis(rs, from_records(rs, records))
    node = engine.ev.index['projects']['A'] if auxiliary else engine.by_level['Project'][0]
    assert engine.ev.variable('total', node).v == Decimal('0.5')
    assert engine.ev.traces['regions:north/actual'].value == '60'


@pytest.mark.parametrize('expr', ['region_factor', 'sum(region_factor, of: "regions")'])
def test_entity_with_its_own_reference_retains_direct_access(expr):
    rs, records = example(expr, entity_key=True)
    assert not [i for i in validate(rs) if i.severity == 'blocking']
    engine = Engine.prepare(rs, from_records(rs, records))
    assert engine.ev.variable('total', engine.by_level['Project'][0]).v == Decimal('0.5')


def test_transitive_entity_reference_remains_valid_for_direct_variable_access():
    rs, records = example()
    rs.datasets['projects'].fields['customer'] = FieldDef(type='id')
    rs.datasets['customers'] = DatasetDef.model_validate({
        'key': 'customer', 'fields': {'customer': {'type': 'id'}, 'region': {'type': 'id'}}})
    for row in records['projects']:
        row['customer'] = 'C'
    records['customers'] = [{'customer': 'C', 'region': 'north'}]
    assert not [i for i in validate(rs) if i.severity == 'blocking']
    engine = Engine.prepare(rs, from_records(rs, records))
    assert engine.ev.variable('total', engine.by_level['Project'][0]).v == Decimal('0.5')
