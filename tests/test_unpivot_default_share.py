"""An explicit missing-person-share policy must execute at the exact evidence target."""
import copy
from decimal import Decimal

import pytest

from bonus_agent.agent import CHAT_SOURCE, ToolError, _unpivot_spec
from bonus_core.data.mapping import apply_mapping
from bonus_core.data.period import Period
from bonus_core.data.table_detect import ROW_ID, SheetTable, unpivot_by
from tests.test_staged_workflow import make_workspace


pytestmark = pytest.mark.evidence


def source(rows):
    return SheetTable('roster.xlsx', 'roles', 1, ['pid', 'seller', 'assistant', 'ratios'], rows)


def spec(**changes):
    return {'columns': ['seller', 'assistant'], 'value': 'person', 'share': 'share', **changes}


def shares(table):
    return list(zip(table.column('person'), table.column('share')))


@pytest.mark.parametrize('value,expected', [('0.4', '0.4'), (0, '0'), ('40%', '0.4'), ('100％', '1')])
def test_declared_default_executes_without_changing_explicit_ratios(value, expected):
    st = source([(2, ['A', 'Alice', None, None]), (3, ['B', 'Bob20%', None, None])])
    result = unpivot_by(st, spec(default_share=value))
    assert shares(result) == [('Alice', Decimal(expected)), ('Bob', Decimal('0.2'))]
    assert shares(unpivot_by(st, spec())) == [('Alice', Decimal(1)), ('Bob', Decimal('0.2'))]


def test_blank_person_is_whole_placeholder_not_a_person_missing_share():
    st = source([(2, ['A', 'Alice', None, None]), (3, ['B', None, None, None])])
    result = unpivot_by(st, spec(default_share='0.4', blank='{label}:unassigned', blank_except=['assistant']))
    assert shares(result) == [('Alice', Decimal('0.4')), ('seller:unassigned', Decimal(1))]


def test_shared_text_values_win_but_empty_text_uses_declared_default():
    st = source([(2, ['A', 'Alice', 'Bob', 'Alice80%,Bob20%']),
                 (3, ['B', 'Carol', None, ' ']),
                 (4, ['C', 'Dana60%', None, 'unrelated'])])
    result = unpivot_by(st, spec(default_share='0.4', share_from='ratios'))
    assert shares(result) == [('Alice', Decimal('.8')), ('Bob', Decimal('.2')),
                              ('Carol', Decimal('.4')), ('Dana', Decimal('.6'))]


@pytest.mark.parametrize('person,text', [('Alice', 'Bob40%'), ('Alice', 'Alice forty percent'), ('Alice', 0), ('Aliceoops%', None)])
def test_nonempty_unparseable_or_unmatched_share_is_never_defaulted(person, text):
    st = source([(7, ['A', person, None, text])])
    with pytest.raises(ValueError, match=r'unpivot.share_parse.*roster.xlsx/roles!7'):
        unpivot_by(st, spec(default_share='0.4', share_from='ratios'))


def test_shared_text_scope_does_not_apply_to_unrelated_role():
    st = source([(2, ['A', 'Alice', 'Bob', 'Alice80%'])])
    assert shares(unpivot_by(st, spec(default_share='.4', share_from='ratios', share_from_columns=['seller']))) == [
        ('Alice', Decimal('.8')), ('Bob', Decimal('.4'))]


@pytest.mark.parametrize('value', [None, True, False, '', 'NaN', 'Infinity', '-0.1', '1.1', 40, [], {}])
def test_invalid_declared_default_is_rejected_in_tool_and_executor(value):
    st = source([(2, ['A', 'Alice', None, None])])
    with pytest.raises(ValueError, match='unpivot.default_share'):
        unpivot_by(st, spec(default_share=value))
    with pytest.raises(ToolError, match='unpivot.default_share'):
        _unpivot_spec(spec(default_share=value), st)


def test_default_requires_output_and_rejects_unsupported_remainder_semantics():
    st = source([(2, ['A', 'Alice50%', None, None])])
    with pytest.raises(ValueError, match='须同时写 share'):
        unpivot_by(st, spec(share='', default_share='1'))
    with pytest.raises(ValueError, match='暂不支持组合'):
        unpivot_by(st, spec(default_share='.4', remainder='pending'))
    assert shares(unpivot_by(st, spec(default_share='1', remainder='pending'))) == [
        ('Alice', Decimal('.5')), ('pending', Decimal('.5'))]


def test_explicit_cell_supplies_all_recipients_and_one_remainder():
    st = source([(7, ['A', 'Alice', 'Bo 25%，Chen（75％）', None]),
                 (8, ['B', None, None, None]),
                 (9, ['C', None, 'Dana20%；Dana0%\nEli(50%)', None])])
    result = unpivot_by(st, spec(split_shares_columns=['assistant'], default_share='1',
                                blank='unassigned', blank_except=['seller'], remainder='unassigned'))
    assert shares(result) == [('Alice', Decimal(1)), ('Bo', Decimal('.25')), ('Chen', Decimal('.75')),
                              ('unassigned', Decimal(1)), ('Dana', Decimal('.2')), ('Dana', Decimal(0)),
                              ('Eli', Decimal('.5')), ('unassigned', Decimal('.3'))]
    assert [row for row, _ in result.rows] == [7, 7, 7, 8, 9, 9, 9, 9]


@pytest.mark.parametrize('text', ['Bo50%，备注', 'Bo80%，Chen40%', 'Bo-20%', '50%', 'Bo（100%'])
def test_invalid_explicit_cell_reports_its_source(text):
    st = source([(7, ['A', None, text, None])])
    with pytest.raises(ValueError, match=r'unpivot.share_parse.*roster.xlsx/roles!7.*assistant'):
        unpivot_by(st, spec(split_shares_columns=['assistant'], remainder='unassigned'))


def test_incomplete_share_requires_rule_and_legacy_does_not_create_a_compound_name():
    st = source([(7, ['A', None, 'Bo30%，Chen50%', None])])
    with pytest.raises(ValueError, match=r'unpivot.share_parse.*roster.xlsx/roles!7.*remainder'):
        unpivot_by(st, spec(split_shares_columns=['assistant']))
    with pytest.raises(ValueError, match=r'unpivot.share_parse.*roster.xlsx/roles!7.*split_shares_columns'):
        unpivot_by(st, spec(remainder='unassigned'))


def test_compound_role_survives_definition_edit_and_preparation_in_its_period(tmp_path):
    def roster(wb):
        table = wb.create_sheet('roster')
        table.append(['month', 'pid', 'names', 'names'])
        table.append([202608, 'A', 'last month has no explicit proportions', None])
        table.append([202609, 'A', 'Bo30%、Chen70%', 'not a recipient source'])
        table.append([202609, 'B', None, 'also unused'])

    ws = make_workspace(tmp_path, mutate_workbook=roster)
    ws.period = Period.parse('2026-09')
    fields = {'rid': {'column': ROW_ID, 'type': 'id'}, 'pid': {'column': 'pid', 'type': 'id'},
              'month': {'column': 'month', 'type': 'id'}, 'person': {'column': 'person', 'type': 'text'},
              'share': {'column': 'share', 'type': 'ratio'}}
    unpivot = spec(columns=['names(C列)'], split_shares_columns=['names(C列)'], blank='unassigned')
    ws.define_dataset('people', 'data.xlsx', 'roster', fields, key='rid', period='month', unpivot=unpivot)
    ws.update_dataset('people', {'unpivot': {'blank': 'role pending'}})
    assert ws.mapping['people'].unpivot['split_shares_columns'] == ['names']
    assert ws._values('people', 'person') == ['Bo', 'Chen', 'role pending']
    assert ws.prepare_data()['status'] == 'PASS'
    data, _ = ws._data(ws._ruleset('S'))
    assert [(row.src.row, row.values['rid'], row.values['person'], row.values['share'])
            for row in data['people'].rows] == [
        (3, '3-1', 'Bo', Decimal('.3')), (3, '3-2', 'Chen', Decimal('.7')), (4, '4', 'role pending', Decimal(1))]
    with pytest.raises(ToolError, match=r'unpivot.share_parse.*data.xlsx/roster!2') as failure:
        ws.update_dataset('people', {'window': 'year_to_date'})
    assert '姓名及比例1格' in str(failure.value) and '其他非空值1格' in str(failure.value)
    assert ws.datasets['people'].window == 'period'
    rules = ws._ruleset('S')
    rules.datasets = {name: definition.model_copy(deep=True) for name, definition in rules.datasets.items()}
    rules.datasets['people'].window = 'year_to_date'
    data, issues = apply_mapping(rules, [tmp_path / 'data.xlsx'], ws.mapping, ws.period)
    assert 'people' not in data
    assert any(i.code == 'unpivot_share' and 'data.xlsx/roster!2' in i.message for i in issues)


def test_separate_share_columns_are_read_and_preserved_by_mapping(tmp_path):
    def roster(wb):
        table = wb.create_sheet('roster')
        table.append(['month', 'pid', 'seller', 'share %', 'assistant', 'share %'])
        table.append([202608, 'A', 'Archie', 'old bad share', None, None])
        table.append([202609, 'A', 'Alice', .6, 'Bo', '75%'])
        table.append([202609, 'B', 'Chen', None, 'Dana', 0])

    ws = make_workspace(tmp_path, mutate_workbook=roster)
    ws.period = Period.parse('2026-09')
    fields = {
        'rid': {'column': ROW_ID, 'type': 'id'}, 'pid': {'column': 'pid', 'type': 'id'},
        'month': {'column': 'month', 'type': 'id'}, 'person': {'column': 'person', 'type': 'text'},
        'share': {'column': 'share', 'type': 'ratio'},
    }
    config = spec(share_columns={'seller': 'share %(D列)', 'assistant': 'share %(F列)'},
                  default_share='1', remainder='{label}未分配')
    ws.define_dataset('people', 'data.xlsx', 'roster', fields, key='rid', period='month', unpivot=config)
    original_mapping = ws.mapping['people'].unpivot['share_columns'].copy()
    ws.update_dataset('people', {'unpivot': {'blank': '{label}未分配'}})
    assert ws.mapping['people'].unpivot['share_columns'] == original_mapping
    assert ws.prepare_data()['status'] == 'PASS'
    data, _ = ws._data(ws._ruleset('S'))
    assert [(row.src.row, row.values['person'], row.values['share']) for row in data['people'].rows] == [
        (3, 'Alice', Decimal('.6')), (3, 'seller未分配', Decimal('.4')),
        (3, 'Bo', Decimal('.75')), (3, 'assistant未分配', Decimal('.25')),
        (4, 'Chen', Decimal(1)), (4, 'Dana', Decimal(0)), (4, 'assistant未分配', Decimal(1)),
    ]


@pytest.mark.parametrize('name,value', [('Alice', 60), ('Alice', 'invalid'), ('Alice50%', '60%'), (None, .6)])
def test_nonempty_separate_share_does_not_use_default(name, value):
    st = source([(7, ['A', name, None, value])])
    with pytest.raises(ValueError, match=r'unpivot.share_parse.*roster.xlsx/roles!7.*ratios'):
        unpivot_by(st, spec(share_columns={'seller': 'ratios'}, default_share='1', remainder='pending'))


def workspace(tmp_path, default='0.4'):
    def roster(wb):
        table = wb.create_sheet('roster')
        table.append(['rid', 'pid', 'seller', 'assistant'])
        table.append(['1', 'A', 'Alice', None])
        table.append(['2', 'B', 'Bob', None])
    ws = make_workspace(tmp_path, mutate_workbook=roster)
    fields = {'rid': {'column': 'rid', 'type': 'id'}, 'pid': {'column': 'pid', 'type': 'id'},
              'person': {'column': 'person', 'type': 'text'},
              'share': {'column': 'share', 'type': 'ratio', 'if_empty': '0.9'}}
    config = spec(**({'default_share': default} if default is not None else {}))
    ws.define_dataset('people', 'data.xlsx', 'roster', fields, key='rid', unpivot=config)
    ws.said = '人员名单比例为空时按40%处理。'
    ws.record_facts([{'name': '缺省比例', 'value': '40%', 'file': CHAT_SOURCE, 'quote': ws.said}])
    return ws


TARGET = 'S::/mapping/people/unpivot/default_share'


def test_executable_binding_survives_basis_and_field_if_empty_cannot_override(tmp_path):
    ws = workspace(tmp_path)
    before = copy.deepcopy(ws.drafts)
    ws.bind_facts([{'name': '缺省比例', 'targets': [TARGET]}])
    assert ws.drafts == before and 'mapping' not in ws.drafts['S']
    assert not ws._binding_gaps()
    assert not ws._flow().basis_view()._binding_gaps()
    data, _ = ws._data(ws._ruleset('S'))
    assert [row.values['share'] for row in data['people'].rows] == [Decimal('.4'), Decimal('.4')]
    assert ws.mapping['people'].unpivot['default_share'] == '0.4'


def test_omitted_legacy_default_cannot_be_bound_and_feedback_gives_real_path(tmp_path):
    ws = workspace(tmp_path, default=None)
    assert 'default_share' not in ws.mapping['people'].unpivot
    assert 'unpivot' not in ws._binding_scopes()['S']['mapping']['people']
    data, _ = ws._data(ws._ruleset('S'))
    assert [row.values['share'] for row in data['people'].rows] == [Decimal(1), Decimal(1)]
    with pytest.raises(ToolError, match='parameter.binding'):
        ws.bind_facts([{'name': '缺省比例', 'targets': [TARGET]}])
    gap = next(g for g in ws._binding_gaps() if '缺省比例' in g)
    assert '/unpivot/default_share' in gap and 'update_dataset' in gap
    ws.update_dataset('people', {'unpivot': {'default_share': '40%'}})
    ws.bind_facts([{'name': '缺省比例', 'targets': [TARGET]}])
    assert not ws._binding_gaps()


def test_changed_or_deleted_default_invalidates_binding_and_receipts(tmp_path):
    ws = workspace(tmp_path)
    ws.set_rules(scheme='S', variables={'declared': {'expr': '0.4'}})
    ws.bind_facts([{'name': '缺省比例', 'targets': [TARGET, 'S::/variables/declared/expr']}])
    assert ws.prepare_data()['status'] == 'PASS'
    assert ws.compile_formulas()['status'] == 'PASS'
    assert ws._flow().current('data') and ws._flow().current('formulas')
    ws.update_dataset('people', {'unpivot': {'default_share': '.6'}})
    assert any('缺省比例' in gap for gap in ws.validate_parameters(repair=True)['gaps'])
    assert not ws._flow().current('data') and not ws._flow().current('formulas')
    ws.mapping['people'].unpivot.pop('default_share')
    assert any('缺省比例' in gap for gap in ws.validate_parameters(repair=True)['gaps'])
    assert TARGET in ws.facts[-1].targets  # another valid rule target cannot prune the broken mapping evidence


def test_unused_output_and_removed_dataset_do_not_prove_default(tmp_path):
    ws = workspace(tmp_path)
    ws.mapping['people'].columns.pop('share')
    with pytest.raises(ToolError, match='parameter.binding'):
        ws.bind_facts([{'name': '缺省比例', 'targets': [TARGET]}])
    ws.mapping['people'].columns['share'] = 'share'
    ws.bind_facts([{'name': '缺省比例', 'targets': [TARGET]}])
    ws.remove_dataset('people')
    assert any('缺省比例' in gap for gap in ws.validate_parameters(repair=True)['gaps'])
    assert TARGET in ws.facts[-1].targets
