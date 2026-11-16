"""Source diagnosis and bounded advisory failures must not depend on a valid tree."""
import copy

import openpyxl
import pytest

from bonus_agent import case
from bonus_agent.agent import Step, ToolError, Workspace, _deep_reason, run_agent
from bonus_agent.auto import build_inventory
from bonus_agent.llm import FakeLLM
from bonus_core.data.period import Period


def workspace(tmp_path):
    p=tmp_path/'data.xlsx'
    wb=openpyxl.Workbook();s=wb.active;s.title='projects'
    s.append(['month','customer','model','amount'])
    s.append(['202608','A','X',100]);s.append(['202609','A','X',200])
    t=wb.create_sheet('roster');t.append(['month','customer','model','parent'])
    t.append(['202608','A','X','202608AX']);t.append(['202609','A','X','202609AX'])
    wb.save(p);wb.close()
    ws=Workspace(FakeLLM([]),build_inventory([p]));ws.period=Period.parse('2026-09')
    fields={'month':{'column':'month','type':'text'},'pid':{'column':'pid','type':'id'}}
    ws.define_dataset('projects',p.name,'projects',fields,key='pid',period='month',combine={'pid':['month','customer','model']})
    ws.define_dataset('roster',p.name,'roster',{'month':{'column':'month','type':'text'},'pid':{'column':'parent','type':'id'}},link='pid')
    return ws


def test_period_diagnosis_requires_no_tree_and_keeps_input_unchanged(tmp_path,monkeypatch):
    ws=workspace(tmp_path)
    before=case.snapshot(ws)
    monkeypatch.setattr(ws,'_trial',lambda *a:pytest.fail('diagnosis must not build a tree'))
    report=ws.diagnose_datasets()
    issue=report['issues'][0]
    assert issue['unmatched']==1
    proposal=issue['period_candidates'][0]
    assert proposal['keys']==proposal['matched']==1
    assert case.snapshot(ws)==before
    ws.update_dataset(**proposal['action']['args'])
    assert ws.diagnose_datasets()['status']=='PASS'


def test_partial_field_and_combine_patch_preserves_other_fields_and_restore(tmp_path):
    ws=workspace(tmp_path)
    old=copy.deepcopy(ws.mapping['roster'])
    ws.update_dataset('roster',{'fields':{'pid':{'column':'composite'},'extra':{'column':'customer','type':'text'}},
                               'combine':{'composite':['month','customer','model']},'period':'month'})
    assert ws.mapping['roster'].columns['month']==old.columns['month']
    assert ws.datasets['roster'].fields['pid'].type=='id'
    assert ws.mapping['roster'].filter==old.filter
    assert ws.mapping['roster'].unpivot==old.unpivot
    clone=Workspace(FakeLLM([]),ws.inv);case.restore(clone,case.snapshot(ws));clone.period=ws.period
    assert clone.diagnose_datasets()['status']=='PASS'
    saved=case.snapshot(clone)
    with pytest.raises(ToolError):clone.update_dataset('roster',{'fields':{'pid':{'column':'missing_column'}}})
    assert case.snapshot(clone)==saved


def test_population_scope_report_and_explicit_repair(tmp_path):
    ws=workspace(tmp_path)
    fields={'month':{'column':'month','type':'text'},'pid':{'column':'pid','type':'id'}}
    ws.define_dataset('projects','data.xlsx','projects',fields,key='pid',combine={'pid':['month','customer','model']})
    ws.define_dataset('sales','data.xlsx','projects',fields,link='pid',combine={'pid':['month','customer','model']},filter={'month':'202609'})
    issue=next(i for i in ws.diagnose_datasets()['issues'] if i['code']=='dataset.population')
    assert issue['extra_keys']==1
    ws.update_dataset(**issue['action']['args'])
    assert not any(i['code']=='dataset.population' for i in ws.diagnose_datasets()['issues'])
    roster=next(c for c in ws.diagnose_datasets()['coverage'] if c['dataset']=='roster')
    assert roster['parent_keys']==1 and roster['unmatched_keys']==1


def test_source_cell_coordinate_error_uses_native_tool_before_pro():
    ws=Workspace(FakeLLM([]),build_inventory([]))
    step=Step('record_facts',{},'错误：source.cell：请读取原始表格坐标',False)
    assert not _deep_reason([step],0,ws,{'record_facts':1})
    assert _deep_reason([step],0,ws,{'record_facts':2})


@pytest.mark.parametrize('advice',['not JSON','[]','{}'])
def test_optional_advisory_failure_does_not_abort_or_apply_advice(tmp_path,advice):
    p=tmp_path/'rule.md';p.write_text('金额100元',encoding='utf8')
    llm=FakeLLM([
        {'tool':'compile_table','args':{'name':'T','rows':[{'value':'missing'}]}},
        {'tool':'compile_table','args':{'name':'T','rows':[{'value':'missing'}]}},
        {'tool':'note','args':{'topic':'continued','text':'native recovery'}},
        {'tool':'finish','args':{}},
    ])
    llm.recovery_mode='advisor';llm.think_llm=FakeLLM(lambda _:advice);llm.think_llm.usage=llm.usage
    result=run_agent(llm,[p],tmp_path/'out',max_steps=4)
    assert result.ws.notes.get('continued')=='native recovery'
    assert not result.ok
    assert '局部诊断未完成' not in (result.error or '')
    failures=[s for s in result.steps if s.tool=='（局部诊断）' and not s.ok]
    assert failures and '未应用建议' in failures[0].result
    checks={c['id']:c['status'] for c in result.ws.validation_report()['checks']}
    assert checks['independent.full']=='INCOMPLETE'


def test_empty_diagnostic_is_incomplete():
    assert Workspace(FakeLLM([]),build_inventory([])).diagnose_datasets()['status']=='INCOMPLETE'


def test_allocation_parent_separator_requires_explicit_mapping_repair(tmp_path):
    from bonus_agent.agent import ROW_ID
    from bonus_core.engine.tree import build_tree
    ws=workspace(tmp_path)
    ws.update_dataset('projects',{'fields':{'customer':{'column':'customer','type':'text'},'model':{'column':'model','type':'text'}}})
    ws.update_dataset('roster',{'key':'rid','link':None,'period':'month','fields':{
        'rid':{'column':ROW_ID,'type':'id'},'parent_pid':{'column':'parent','type':'id'},
        'customer':{'column':'customer','type':'text'},'model':{'column':'model','type':'text'}}})
    ws.set_rules(scheme='S',pool={'expr':'1'},allocation=[
        {'level':'projects','key':'pid','weight':'1'},
        {'level':'roster','key':'rid','parent_key':'parent_pid','weight':'1'}])
    rs=ws._ruleset('S');data,_=ws._data(rs)
    assert any(i.code=='parent_unknown' for i in build_tree(rs,data)[2])
    report=ws.diagnose_datasets()
    identity=next(i for i in report['issues'] if i['code']=='dataset.identity')
    assert identity['separator_only_keys']==1
    ws.update_dataset(**identity['action']['args'])
    assert ws.diagnose_datasets()['status']=='PASS'
    rs=ws._ruleset('S');data,_=ws._data(rs)
    assert not any(i.severity=='blocking' for i in build_tree(rs,data)[2])


def test_first_tree_failure_returns_deterministic_feedback_before_advisor(tmp_path,monkeypatch):
    p=tmp_path/'rule.md';p.write_text('金额100元',encoding='utf8')
    def fail(*args,**kwargs):raise ToolError('建树失败：parent_unknown: prior month')
    monkeypatch.setattr(Workspace,'evaluate',fail)
    monkeypatch.setattr(Workspace,'diagnose_datasets',lambda self:{'status':'FAIL','issues':[{'code':'dataset.reference','period_field':'month'}]})
    llm=FakeLLM([{'tool':'evaluate','args':{'expr':'1'}}, {'tool':'note','args':{'topic':'next','text':'inspect month'}}])
    llm.recovery_mode='advisor';llm.think_llm=FakeLLM(lambda _:pytest.fail('native diagnosis should run first'))
    result=run_agent(llm,[p],tmp_path/'out',max_steps=2)
    step=next(s for s in result.steps if s.tool=='evaluate')
    assert not step.ok and '【原始数据诊断】' in step.result and 'period_field' in step.result
    assert result.ws.notes.get('next')=='inspect month'
