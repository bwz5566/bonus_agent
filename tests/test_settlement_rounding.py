"""Declared rounding stages keep bank amounts and theoretical funding separately reconcilable."""
import copy
from decimal import Decimal as D

import openpyxl
import pytest

from bonus_agent.agent import AgentResult, ToolError, _overview
from bonus_agent.compare import compare_expected
from bonus_agent.reread import _disagree
from bonus_agent.validation import result_integrity
from bonus_core.data.mapping import DatasetMapping
from bonus_core.dsl.models import DatasetDef,FieldDef
from bonus_core.engine.issues import EngineError
from bonus_core.report.excel import write_excel

from test_recipient_reference import case

pytestmark=[pytest.mark.verify,pytest.mark.reread,pytest.mark.evidence]


def recipient_model(tmp_path):
    ws=case(tmp_path)
    ws.datasets['shares'].fields['person']=FieldDef(type='id')
    ws.datasets['shares'].link='person'
    ws.datasets['people']=DatasetDef(key='person',distinct=True,population_from='shares',fields={'person':{'type':'id'}})
    ws.mapping['people']=DatasetMapping(file='data.xlsx',sheet='shares',columns={'person':'person'})
    src=[{'doc':'data.xlsx','quote':'amount'}]
    ws.set_rules(scheme='S',variables={'order_count':{'scope':'orders','expr':'count(of: "shares")'},
        'recipient_total':{'scope':'people','expr':'sum(amount / order_count, of: "shares")'}},
        allocation=[{'level':'people','key':'person'}],pool={'scope':'people','expr':'recipient_total'},
        report={'people':'person','totals':[{'name':'理论金额','level':'global','expr':'sum(amount, of: "orders")'}],
            'details':[{'name':'orders','level':'orders','expr':'amount','sources':src}],
            'balances':[{'name':'auxiliary balance','level':'orders','total':'amount','parts':['amount'],'sources':src}],
            'distributions':[{'name':'funding','level':'global','rounding':'after_sum',
                              'total':'sum(amount, of: "orders")','sources':src}]})
    ws.trial_run()
    return ws


def test_recipient_settlement_matches_group_then_round_and_exports_auxiliary_trace(tmp_path):
    ws=recipient_model(tmp_path);res=ws.results['S']['R0']
    assert {p['name']:D(p['final']) for p in res.people}=={'A':D(0),'B':D('.01')}
    assert res.rounding[0]['residual']=='0.00' and result_integrity(res).ok
    assert res.details['orders']['orders:P']['amount']=='0.01'
    assert '全部/汇总/理论金额' in res.traces
    assert 'orders:P' in res.auxiliary
    spec={'pool':'sum(amount / v_count, of: "shares")','helpers':{
        'v_count':{'scope':'orders','expr':'count(of: "shares")'}},
        'people':{'mode':'after_sum','key':'person','expr':'sum(amount / v_count, of: "shares")'},
        'details':{'orders':'amount'}}
    assert not _disagree(ws,'S',spec)
    path=tmp_path/'settlement.xlsx';write_excel(ws._ruleset('S'),res,path)
    book=openpyxl.load_workbook(path,read_only=True,data_only=True)
    assert {'舍入对账','orders核算底稿','orders核算结果'}<=set(book.sheetnames)


def test_primary_multischeme_workbook_contains_detail_people_and_rounding(tmp_path):
    ws = recipient_model(tmp_path)
    source = [{'doc': 'data.xlsx', 'quote': 'amount'}]
    ws.set_rules(scheme='项目资金', pool={'scope': 'orders', 'expr': 'amount', 'sources': source},
                 allocation=[{'level': 'orders', 'key': 'pid'}],
                 report={'details': [{'name': '项目拨款', 'level': 'orders', 'expr': 'amount', 'sources': source}]})
    ws.trial_run('项目资金')
    ws.report_required = True
    ar = AgentResult(ws, [], ws._ruleset('S'), ws.results['S']['R0'], results=ws.results)
    path = tmp_path / 'result.xlsx'
    _overview(ar, [], path)

    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        assert book.sheetnames[:2] == ['核算底稿', '汇总核算']
        details = list(book['核算底稿'].values)
        col = details[0].index('项目拨款')
        project, = (r for r in details[1:] if r[0] == '项目资金')
        assert project[3] == 'P' and D(str(project[col])) == D('.01')
        rows = list(book['汇总核算'].values)
        people = {r[2]: D(str(r[5])) for r in rows if r[3] == '人员'}
        assert people == {'A': D('0'), 'B': D('.01')}
        rounding, = (r for r in rows if r[2] == 'funding')
        assert tuple(map(lambda x: D(str(x)), rounding[3:7])) == (D('.01'), D('.01'), D(0), D(0))
    finally:
        book.close()


def test_independent_review_catches_changed_raw_share_with_same_display_cents(tmp_path):
    ws = recipient_model(tmp_path)
    ws.drafts['S']['report']['details'][0]['stage'] = 'distribution'
    ws.trial_run('S')
    per_person = 'sum(amount / independently_counted, of: "shares")'
    spec = {'pool': per_person,
            'helpers': {'independently_counted': {'scope': 'orders', 'expr': 'count(of: "shares")'}},
            'people': {'mode': 'after_sum', 'key': 'person', 'expr': per_person},
            'details': {'orders': 'amount + 0.001'}}
    differences = _disagree(ws, 'S', spec)
    assert differences and {kind for kind, *_ in differences} == {'明细原值'}


def test_independent_pending_amount_uses_auxiliary_projects_and_its_own_formula(tmp_path):
    ws = recipient_model(tmp_path)
    draft = ws.drafts['S']
    draft['variables']['recipient_total']['expr'] = 'sum(amount / 2 / order_count, of: "shares")'
    report = draft['report']
    report['pending'] = [{'name': 'Waiting', 'level': 'orders', 'expr': 'amount / 2',
                          'sources': [{'doc': 'data.xlsx', 'quote': 'amount'}]}]
    report['distributions'][0]['pending'] = ['Waiting']
    ws.trial_run()
    per_person = 'sum(amount / independent_count / 2, of: "shares")'
    spec = {'pool': per_person, 'helpers': {'independent_count': {'scope': 'orders', 'expr': 'count(of: "shares")'}},
            'people': {'mode': 'after_sum', 'key': 'person', 'expr': per_person},
            'details': {'orders': 'amount'}, 'pending': {'Waiting': 'amount * 0.5'}}
    assert ws._independent_people('S', spec)['Waiting'] == D('.005')
    assert not _disagree(ws, 'S', spec)
    spec['pending']['Waiting'] = 'amount * 2'
    assert any(row[1] == 'Waiting' for row in _disagree(ws, 'S', spec))
    del spec['pending']['Waiting']
    with pytest.raises(ToolError, match='缺少待分配项'):
        ws._independent_people('S', spec)
    report['pending'][0]['level'] = 'missing_projects'
    spec['pending']['Waiting'] = 'amount'
    with pytest.raises(ToolError, match='missing_projects.*没有对象'):
        ws._independent_people('S', spec)


def test_rounding_delta_is_visible_and_cannot_be_used_to_hide_lost_entitlements(tmp_path):
    ws=recipient_model(tmp_path)
    # Two recipients each accrue half a cent; rounding by recipient pays a cent each.
    ws.drafts['S']['variables']['recipient_total']['expr']='0.005'
    ws.trial_run()
    res=ws.results['S']['R0']
    assert res.pool=='0.02' and res.rounding[0]['residual']=='-0.01'
    assert result_integrity(res).ok
    res.rounding[0]['residual']='0.00'
    assert not result_integrity(res).ok
    ws.drafts['S']['variables']['recipient_total']['expr']='0'
    with pytest.raises(ToolError,match='舍入前金额去向不闭合'):
        ws.trial_run()


def test_after_sum_cannot_be_declared_on_a_record_tree_without_regrouping_people(tmp_path):
    ws=case(tmp_path)
    ws.drafts['S']['report']['distributions']=[{'name':'funding','level':'global','rounding':'after_sum',
        'total':'sum(amount, of: "orders")','sources':[{'doc':'data.xlsx','quote':'amount'}]}]
    with pytest.raises(ToolError,match='recipient_rounding_scope'):
        ws.trial_run()


def test_full_expected_sheet_with_missing_objects_is_not_silently_skipped(tmp_path):
    ws=recipient_model(tmp_path)
    wb=openpyxl.Workbook();s=wb.active;s.title='objects'
    s.append(['项目编号','期望应得']);s.append(['unknown',.01])
    p=tmp_path/'expected.xlsx';wb.save(p)
    matches=compare_expected(ws.results,p)
    assert any(m.missing and not m.ok for m in matches)


def test_composite_id_alias_is_unique_and_money_columns_are_never_chosen_by_agreement(tmp_path):
    ws=recipient_model(tmp_path);res=ws.results['S']['R0']
    res.details['orders']['orders:P']['key']='202710|C99|M2'
    wb=openpyxl.Workbook();s=wb.active;s.append(['项目编号','期望应得']);s.append(['202710C99M2',.01])
    p=tmp_path/'expected.xlsx';wb.save(p)
    assert any(m.ok and m.matched==1 for m in compare_expected(ws.results,p))
    s.cell(2,2).value=.02;wb.save(p)
    assert any(m.diffs for m in compare_expected(ws.results,p))


def test_explicit_summary_total_is_checked_even_when_table_detector_classifies_it_as_total(tmp_path):
    ws=recipient_model(tmp_path);res=ws.results['S']['R0']
    res.totals.append(('合计','0.01'))
    wb=openpyxl.Workbook();s=wb.active;s.append(['核算项目','金额'])
    s.append(['理论金额',.01]);s.append(['合计',.02])
    p=tmp_path/'expected.xlsx';wb.save(p)
    match=next(m for m in compare_expected(ws.results,p) if m.metric=='汇总核算')
    assert match.total==2 and match.matched==1 and match.diffs[0][0]=='合计'
    s.cell(3,2).value=.01;wb.save(p)
    assert next(m for m in compare_expected(ws.results,p) if m.metric=='汇总核算').ok
