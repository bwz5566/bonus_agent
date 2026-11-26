"""Recover a rejected entity declaration using only the current case's detail and rows."""
import copy
import json
from decimal import Decimal

import openpyxl
import pytest

from bonus_agent import case
from bonus_agent.agent import ToolError, Workspace, _resume_task
from bonus_agent.auto import build_inventory
from bonus_agent.dataset_projection import recover_reference_dimensions
from bonus_agent.llm import FakeLLM
from bonus_agent.recovery import RecoveryJournal
from bonus_core.data.mapping import apply_mapping
from bonus_core.data.period import Period
from bonus_core.dsl.models import RuleSet
from bonus_core.engine.run import Engine


def orders(tmp_path, stores=('A', 'B')):
    path = tmp_path / '业务.xlsx'
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = '退货'
    sheet.append(['批次', '门店', '变动额', '经办人', '审批'])
    for month, store, amount, person, status in [
        ('202701', stores[0], 100, '甲', '准'), ('202701', stores[0], -12, '乙', '准'),
        ('202701', stores[1], 30, '丙', '准'), ('202701', stores[1], 2, '丙', '准'),
        ('202701', stores[0], 99, '甲', '退'), ('202612', stores[1], 500, '丙', '准')]:
        sheet.append([month, store, amount, person, status])
    book.save(path)
    book.close()
    ws = Workspace(FakeLLM([]), build_inventory([path]))
    ws.period = Period.parse('2027-01')
    fields = {'lot': {'column': '订单组', 'type': 'id'}, 'month': {'column': '批次', 'type': 'text'},
              'store': {'column': '门店', 'type': 'text'}, 'net': {'column': '变动额', 'type': 'money'},
              'by': {'column': '经办人', 'type': 'text'}}
    source = dict(name='流水', file=path.name, sheet='退货', fields=fields, period='month',
                  filter={'审批': ['准']}, combine={'订单组': ['批次', '门店']})
    ws.define_dataset(**source)
    entity = {k: copy.deepcopy(v) for k, v in source.items() if k != 'combine'}
    entity.update(name='门店批次', key='lot', distinct=True)
    return ws, source, entity


def mapped_rows(ws):
    rs = RuleSet.model_validate({'name': '退货奖', 'version': '1', 'datasets': ws.datasets,
                                 'pool': {'expr': '0'}, 'allocation': [{'level': '门店批次', 'key': 'lot'}]})
    data, issues = apply_mapping(rs, ws.data_files, ws.mapping, period=ws.period)
    assert not [issue for issue in issues if issue.severity == 'blocking']
    return data['流水'].rows


def test_missing_combination_preserves_raw_amounts_and_negative_transactions(tmp_path):
    ws, _, request = orders(tmp_path)
    before = mapped_rows(ws)
    assert '已验证' in ws.define_dataset(**request)
    rows = mapped_rows(ws)
    assert rows == before
    assert [row.values['net'] for row in rows] == list(map(Decimal, ['100', '-12', '30', '2']))
    assert ws.datasets['流水'].link == 'lot'
    assert set(ws.datasets['门店批次'].fields) == {'lot', 'month', 'store'}
    proof = ws.workflow['dataset_repairs']['门店批次']
    assert (proof['entities'], proof['detail_rows'], proof['detail_fields']) == (2, 4, ['by', 'net'])
    assert ws.define_dataset(**request).startswith('no-op')
    restored = Workspace(FakeLLM([]), ws.inv)
    case.restore(restored, case.snapshot(ws))
    restored.period = ws.period
    assert mapped_rows(restored) == before
    assert restored.workflow['dataset_repairs']['门店批次'] == proof
    assert not ws.llm.calls


@pytest.mark.parametrize(('rate', 'expected'), [('0.12', ('10.56', '3.84')), ('0.05', ('4.40', '1.60'))])
def test_engine_computes_renamed_recipients_after_repair(tmp_path, rate, expected):
    ws, _, entity = orders(tmp_path, ('甲', '乙'))
    ws.define_dataset(**entity)
    rs = RuleSet.model_validate({
        'name': '当期业绩奖', 'version': '1', 'datasets': ws.datasets,
        'pool': {'scope': '收款对象', 'expr': f'sum(net, of: "流水") * {rate}'},
        'allocation': [{'level': '收款对象', 'key': 'lot'}], 'report': {'people': 'store'}})
    rows, issues = apply_mapping(rs, ws.inv.files, ws.mapping, period=ws.period)
    assert not [i for i in issues if i.severity == 'blocking']
    awards = Engine(rs, rows).compute()
    assert {row['name']: row['final'] for row in awards.people} == dict(zip(('甲', '乙'), expected))


@pytest.mark.parametrize('mismatch', ['filter', 'window', 'unknown_column', 'amount_unit', 'other_formula',
                                      'two_sources', 'already_linked', 'separator'])
def test_mismatch_never_changes_the_live_mapping(tmp_path, mismatch):
    ws, source, request = orders(tmp_path, ('A|B', 'C') if mismatch == 'separator' else ('A', 'B'))
    if mismatch == 'filter':
        request['filter'] = {}
    elif mismatch == 'window':
        request['window'] = 'year_to_date'
    elif mismatch == 'unknown_column':
        request['fields']['missing'] = {'column': '另一张表的列', 'type': 'text'}
    elif mismatch == 'amount_unit':
        request['fields']['net']['unit'] = '万元'
    elif mismatch == 'other_formula':
        request['combine'] = {'订单组': ['门店', '批次']}
    elif mismatch == 'two_sources':
        ws.define_dataset(**{**source, 'name': '另一份流水'})
    elif mismatch == 'already_linked':
        ws.define_dataset(**{**source, 'link': 'store'})
    before = case.snapshot(ws)
    with pytest.raises(ToolError):
        ws.define_dataset(**request)
    assert case.snapshot(ws) == before


@pytest.mark.parametrize('scope', ['same', 'new_code', 'files_changed', 'wrong_period', 'other_halt', 'wrong_log'])
def test_archived_rejection_requires_matching_evidence(tmp_path, scope):
    ws, _, request = orders(tmp_path)
    journal = RecoveryJournal(tmp_path / 'recovery.json', 'old-build')
    reason = '错误：未保存，订单组不存在于物理表头'
    for _ in range(journal.limit):
        journal.failure('define_dataset', request, reason)
    last = dict(n=1, status='FAILED', files=ws._source_version, period=ws.period.label, error=journal.error)
    log = tmp_path / 'history' / '第1轮' / 'agent_log.json'
    log.parent.mkdir(parents=True)
    step = {'tool': 'define_dataset', 'ok': False, 'args': request, 'result': reason}
    log.write_text(json.dumps({'error': last['error'], 'steps': [None if scope == 'wrong_log' else step]}),
                   encoding='utf-8')
    if scope == 'new_code':
        journal = RecoveryJournal(journal.path, 'new-build')
    elif scope == 'files_changed':
        last['files'] = {}
    elif scope == 'wrong_period':
        last['period'] = '2027-02'
    elif scope == 'other_halt':
        journal.halt('不同的阻断')
    journal.state['failures']['unrelated'] = {'attempts': 1}
    before = case.snapshot(ws)
    result = journal.resume_entity_definition(ws, tmp_path, last)
    if scope in ('same', 'new_code'):
        assert result['status'] == 'PASS' and (result['entities'], result['detail_rows']) == (2, 4)
        assert not journal.error and journal.state['failures'] == {'unrelated': {'attempts': 1}}
        assert journal.resume_entity_definition(ws, tmp_path, last) is None
    else:
        assert result is None and case.snapshot(ws) == before
        assert journal.error


def test_rejected_proposal_is_distinguished_from_the_verified_live_key(tmp_path):
    ws, source, entity = orders(tmp_path)
    ws.define_dataset(**entity)
    patch = {'fields': {'lot': {'column': '批次', 'type': 'id', 'desc': '按批次与门店汇总'}}}
    before = case.snapshot(ws)
    with pytest.raises(ToolError, match='原映射继续生效'):
        ws.update_dataset('流水', patch)
    assert case.snapshot(ws) == before
    patch['combine'] = source['combine']
    assert '已按本次明确提交的合成规则校正字段绑定' in ws.update_dataset('流水', patch)
    assert ws.mapping['流水'].columns['lot'] == '订单组'
    assert ws.datasets['流水'].fields['lot'].desc == '按批次与门店汇总'
    saved = list(mapped_rows(ws))
    response = ws.update_dataset('流水', patch)
    assert response.startswith('no-op') and mapped_rows(ws) == saved
    proof = ws.workflow['dataset_repairs']['门店批次']
    last = {'status': 'FAILED', 'error': '历史故障123'}
    repaired = _resume_task(ws, last, {}, [], data_repair={**proof, 'dataset': '门店批次'})
    assert '历史故障123' not in repaired and '2 个「门店批次」' in repaired and '4 行「流水」' in repaired
    assert '历史故障123' in _resume_task(ws, last, {}, [])


def test_changed_key_recipe_is_never_substituted_with_previous_verified_recipe(tmp_path):
    ws, _, entity = orders(tmp_path)
    ws.define_dataset(**entity)
    before = case.snapshot(ws)
    with pytest.raises(ToolError):
        ws.update_dataset('流水', {'fields': {'lot': {'column': '批次'}},
                                  'combine': {'订单组': ['门店', '批次']}})
    assert case.snapshot(ws) == before


@pytest.mark.parametrize('explicit', [True, False])
def test_reference_lookup_requires_named_join_and_keeps_unmatched_evidence(tmp_path, explicit):
    original, primary, entity = orders(tmp_path)
    ref = tmp_path / '服务台账.xlsx'
    book = openpyxl.Workbook()
    for row in [('门店', '费用'), ('A', 4), ('B', 0), ('C', 9)]:
        book.active.append(row)
    book.save(ref)
    book.close()
    policy = tmp_path / '材料.md'
    join = '返修费用按“门店”与退货表的门店匹配。'
    missing = '参考表里有、但主表中没有的金额，不参与核算。'
    policy.write_text((join if explicit else '') + '\n' + missing, encoding='utf-8')
    ws = Workspace(FakeLLM([]), build_inventory([*original.inv.files, ref, policy]))
    ws.period = original.period
    ws.workflow['enabled'] = True
    ws.plan_calculation('按当期退货核算', ['金额'], {primary['file']: 'data', ref.name: 'data', policy.name: 'rule'}, ['S'])
    quotes = (join, missing) if explicit else (missing,)
    ws.record_clauses([{'file': policy.name, 'quote': quote, 'point': quote} for quote in quotes])
    ws.define_dataset(**primary)
    ws.define_dataset(**entity)
    ws.define_dataset('返修费用', ref.name, 'Sheet', {'store': {'column': '门店', 'type': 'text'},
                      'cost': {'column': '费用', 'type': 'money'}}, link='store', unmatched='ignore',
                      unmatched_basis={'file': policy.name, 'quote': missing})
    before = mapped_rows(ws)
    fixed = recover_reference_dimensions(ws)
    if explicit:
        proof, = fixed
        assert (proof['keys'], proof['matched_reference_keys'], proof['outside_reference_keys']) == (2, 2, 1)
        assert ws.datasets[proof['dataset']].key == 'store' and mapped_rows(ws) == before
        assert not recover_reference_dimensions(ws)
    else:
        assert not fixed and all(ds.key != 'store' for ds in ws.datasets.values())
