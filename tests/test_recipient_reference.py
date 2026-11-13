"""Project conservation must not hide a different cent allocation between recipients."""
import copy
from decimal import Decimal as D

import openpyxl
import pytest

from bonus_agent.agent import ToolError, Workspace
from bonus_agent.auto import build_inventory
from bonus_agent.llm import FakeLLM
from bonus_agent.reread import _disagree, calc_gaps

pytestmark=[pytest.mark.verify,pytest.mark.reread,pytest.mark.evidence]


def case(tmp_path):
    wb=openpyxl.Workbook();p=wb.active;p.title='orders'
    p.append(['pid','amount']);p.append(['P',.01])
    s=wb.create_sheet('shares');s.append(['rid','pid','person','weight'])
    for rid,person in [('1','A'),('2','B'),('3','B')]:s.append([rid,'P',person,1])
    path=tmp_path/'data.xlsx';wb.save(path)
    ws=Workspace(FakeLLM([]),build_inventory([path]))
    ws.define_dataset('orders',path.name,'orders',key='pid',fields={
        'pid':{'column':'pid','type':'id'},'amount':{'column':'amount','type':'money','unit':'元'}})
    ws.define_dataset('shares',path.name,'shares',key='rid',fields={
        'rid':{'column':'rid','type':'id'},'pid':{'column':'pid','type':'id'},
        'person':{'column':'person','type':'text'},'weight':{'column':'weight','type':'number'}})
    ws.set_rules(scheme='S',pool={'expr':'amount','scope':'orders'},allocation=[{'level':'orders','key':'pid'},
        {'level':'shares','key':'rid','parent_key':'pid','weight':'weight'}],report={'people':'person'})
    ws.trial_run()
    return ws


def test_independent_weights_cover_actual_recipient_allocations(tmp_path):
    ws=case(tmp_path)
    spec={'pool':'amount','people':{'mode':'allocation','key':'person','weights':{'shares':'weight'}}}
    assert ws._independent_people('S',spec)=={'A':D('.01'),'B':D('0')}
    assert not _disagree(ws,'S',spec)
    spec['people']['weights']={}
    with pytest.raises(ToolError,match='独立权重'):
        ws._independent_people('S',spec)


def test_project_match_cannot_hide_different_recipient_rounding(tmp_path):
    ws=case(tmp_path)
    spec={'pool':'amount','people':{'mode':'after_sum','key':'person','expr':'amount / 3'}}
    diffs=_disagree(ws,'S',spec)
    assert {d[1] for d in diffs if d[0]=='人员汇总'}=={'A','B'}
    gaps=calc_gaps(ws,{'calc':{'schemes':{'S':spec}}})
    assert any(k.startswith('复读 人员汇总') for k,_ in gaps)


def test_required_people_and_unclear_policy_block_completion(tmp_path):
    ws=case(tmp_path);ws.report_required=True
    with pytest.raises(ToolError,match='缺少人员汇总'):
        ws._independent_people('S',{'pool':'amount'})
    cfg={'pool':'amount','people':{'mode':'allocation','key':'person','weights':{'shares':'weight'}}}
    assert any(k.startswith('复读 未定论') for k,_ in calc_gaps(ws,{'calc':{'schemes':{'S':cfg},'unclear':['舍入时点未明确']}}))


def test_stale_results_cannot_be_reported_as_verified(tmp_path):
    ws=case(tmp_path)
    ws.drafts['S']['pool']['expr']='amount * 2'
    report=ws.validation_report()
    assert 'S' in ws.untested and report['status'] != 'PASS'
    with pytest.raises(ToolError,match='试算'):
        ws.finish('不能交付旧金额')


def test_asking_question_keeps_draft_but_finance_answer_invalidates_it(tmp_path):
    from bonus_agent.extract_rules import Question
    ws=case(tmp_path)
    version=ws._calculation_version()
    ws.questions.append(Question(id='q1',text='确认本批舍入时点',blocking=True))
    assert ws._calculation_version()==version
    assert '等待财务答复' in ws.finish('等待确认，保留已有草稿')
    assert not ws.untested and ws.results
    ws.questions[0].answer='先汇总到人员再舍入'
    ws.validation_report()
    assert 'S' in ws.untested
