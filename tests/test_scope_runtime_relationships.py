"""Scope feedback follows the same relationship resolution as actual evaluation."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from bonus_agent.scope_diagnostics import scope_diagnostics
from bonus_core.data.mapping import DatasetMapping
from bonus_core.data.table import from_records
from bonus_core.dsl.models import RuleSet
from bonus_core.engine.run import Engine
from bonus_core.engine.issues import EngineError


def setup(parent_keys=('202609|C1|P1', '202609|C1|P2'), detail_keys=('202609C1P1',)):
    rs = RuleSet.model_validate({
        'name': 'links', 'version': '1',
        'datasets': {
            'projects': {'key': 'pid', 'fields': {'pid': {'type': 'id'}, 'cid': {'type': 'id'}}},
            'unpaid': {'link': 'pid', 'unmatched': 'ignore', 'fields': {'pid': {'type': 'id'}}},
            'customers': {'key': 'cid', 'fields': {'cid': {'type': 'id'}}},
            'history': {'link': 'pid', 'unmatched': 'ignore', 'window': 'year_to_date',
                        'fields': {'pid': {'type': 'id'}, 'cid': {'type': 'id'}, 'amount': {'type': 'money'}}},
        },
        'variables': {'unpaid_flag': {'scope': 'project', 'expr': 'count(of: "unpaid") > 0'}},
        'pool': {'scope': 'project', 'expr': 'if(unpaid_flag, 0, 1)'},
        'allocation': [{'level': 'project', 'key': 'pid'}],
    })
    records = {
        'projects': [{'pid': key, 'cid': 'C1'} for key in parent_keys],
        'unpaid': [{'pid': key} for key in detail_keys],
        'customers': [{'cid': 'C1'}],
        'history': [{'pid': parent_keys[0], 'cid': 'C1', 'amount': 100},
                    {'pid': '202601|C1|P0', 'cid': 'C1', 'amount': 50}],
    }
    data = from_records(rs, records, file='source.xlsx')
    mapping = {name: DatasetMapping(file='source.xlsx', sheet=name,
                                   columns={field: field for field in spec.fields}) for name, spec in rs.datasets.items()}
    ws = SimpleNamespace(mapping=mapping, _scheme=lambda scheme: scheme or 'S',
                         _ruleset=lambda scheme: rs, _data=lambda rules: (data, []))
    return ws, rs, data


def test_separator_link_feedback_matches_real_engine_count_and_keeps_raw_statistics():
    ws, rs, data = setup()
    before = rs.model_dump_json(), deepcopy(data)
    engine = Engine.prepare_basis(rs, data)
    actual = sum(engine.ev.variable('unpaid_flag', node).v for node in engine.by_level['project'])
    report = scope_diagnostics(ws, focus='unpaid')
    item = report['comparisons'][0]
    assert report['runtime_relationships']['status'] == 'COMPLETE'
    assert actual == item['hypothetical_population_from']['retained_parent_keys'] == 1
    assert item['parent_without_detail'] == 1 and item['detail_without_parent'] == 0
    assert item['runtime_linked_rows'] == 1
    assert item['raw_exact']['matched_parent_keys'] == 0
    assert item['raw_exact']['parent_without_detail'] == 2
    assert item['raw_exact']['detail_without_parent'] == 1
    assert (rs.model_dump_json(), data) == before


def test_case_width_and_whitespace_canonicalization_is_the_engine_path():
    ws, rs, data = setup(parent_keys=('A', 'B'), detail_keys=(' ａ ',))
    engine = Engine.prepare_basis(rs, data)
    assert engine.ev.variable('unpaid_flag', engine.by_level['project'][0]).v is True
    item = scope_diagnostics(ws, focus='unpaid')['comparisons'][0]
    assert item['runtime_linked_rows'] == 1 and item['detail_without_parent'] == 0
    assert item['raw_exact']['matched_parent_keys'] == 0


def test_ambiguous_separator_collision_does_not_invent_a_runtime_parent():
    ws, rs, data = setup(parent_keys=('A-B', 'AB'), detail_keys=('A|B',))
    engine = Engine.prepare_basis(rs, data)
    assert not any(engine.ev.variable('unpaid_flag', node).v for node in engine.by_level['project'])
    item = scope_diagnostics(ws, focus='unpaid')['comparisons'][0]
    assert item['runtime_linked_rows'] == 0 and item['detail_without_parent'] == 1
    assert item['hypothetical_population_from']['retained_parent_keys'] == 0


def test_blocked_tree_preserves_raw_observation_without_partial_runtime_coverage():
    ws, rs, data = setup(detail_keys=('202609C1P1', 'missing'))
    rs.datasets['unpaid'].unmatched = 'block'
    report = scope_diagnostics(ws, focus='unpaid')
    assert report['runtime_relationships']['status'] == 'UNAVAILABLE'
    item = report['comparisons'][0]
    assert item['runtime_linked_rows'] is None
    assert item['key_match'] == 'raw_exact_only_runtime_unavailable'
    assert item['raw_exact']['matched_parent_keys'] == 0
    assert '部分挂接' in report['runtime_relationships']['note']


def test_historical_rows_remain_visible_through_real_customer_references():
    ws, rs, data = setup()
    engine = Engine.prepare_basis(rs, data)
    customer = engine.root.index['customers']['C1']
    assert len(customer.rows('history')) == 2
    item = scope_diagnostics(ws, focus='history')['comparisons'][0]
    assert item['detail_without_parent'] == 1 and item['runtime_linked_rows'] == 1
    assert item['other_reference_visibility'] == [{'entity': 'customers', 'rows': 2, 'objects': 1}]
    assert len(data['history'].rows) == 2


def test_scope_observation_does_not_evaluate_formulas_or_distribute(monkeypatch):
    ws, rs, _ = setup()
    rs.pool.expr = 'broken_name / 0'
    def forbidden(*args, **kwargs):
        raise AssertionError('diagnostics must not calculate amounts')
    monkeypatch.setattr(Engine, 'calculate_basis', forbidden)
    monkeypatch.setattr(Engine, 'compute', forbidden)
    report = scope_diagnostics(ws, focus='unpaid')
    assert report['runtime_relationships']['status'] == 'COMPLETE'
    assert report['comparisons'][0]['runtime_linked_rows'] == 1


def test_invalid_layer_link_cannot_claim_complete_runtime_aux_coverage():
    rs = RuleSet.model_validate({
        'name': 'aux-participants', 'version': '1',
        'datasets': {
            'projects': {'key': 'pid', 'fields': {'pid': {'type': 'id'}}},
            'people': {'key': 'person', 'fields': {'person': {'type': 'id'}}},
            'participation': {'key': 'rid', 'fields': {
                'rid': {'type': 'id'}, 'pid': {'type': 'id'}, 'person': {'type': 'id'}}},
        },
        'variables': {'participation_count': {'scope': 'people', 'expr': 'count(of: "participation")'}},
        'pool': {'scope': 'project', 'expr': '1'},
        'allocation': [{'level': 'project', 'key': 'pid'},
                       {'level': 'row', 'key': 'rid', 'parent_key': 'pid', 'weight': '1'}],
    })
    data = from_records(rs, {
        'projects': [{'pid': 'P'}], 'people': [{'person': 'A'}, {'person': 'B'}],
        'participation': [{'rid': '1', 'pid': 'P', 'person': ' ａ '}],
    }, file='source.xlsx')
    mapping = {name: DatasetMapping(file='source.xlsx', sheet=name,
                                   columns={field: field for field in spec.fields}) for name, spec in rs.datasets.items()}
    ws = SimpleNamespace(mapping=mapping, _scheme=lambda scheme: scheme or 'S',
                         _ruleset=lambda scheme: rs, _data=lambda rules: (data, []))
    engine = Engine.prepare(rs, data)
    person = engine.root.index['people']['A']
    assert engine.ev.variable('participation_count', person).v == 1
    assert all(node.level != 'people' for node in engine.by_level['row'][0].chain())
    valid = scope_diagnostics(ws, focus='participation')
    assert valid['runtime_relationships']['status'] == 'COMPLETE'
    assert not any(item['detail'] == 'participation' for item in valid['comparisons'])
    # Adding link to an allocation entity is rejected by the actual engine.
    # A partial tree that ignores it cannot certify runtime coverage instead.
    rs.datasets['participation'].link = 'person'
    with pytest.raises(EngineError, match='dataset_kind'):
        Engine.prepare(rs, data)
    blocked = scope_diagnostics(ws, focus='participation')
    assert blocked['runtime_relationships']['status'] == 'UNAVAILABLE'
    item = blocked['comparisons'][0]
    assert item['runtime_linked_rows'] is None
    assert item['key_match'] == 'raw_exact_only_runtime_unavailable'
    assert item['raw_exact']['matched_parent_keys'] == 0
