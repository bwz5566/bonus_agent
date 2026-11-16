"""Real staged receipts, independent amounts and human-only confirmation boundaries."""
import copy
import json
from decimal import Decimal
from types import SimpleNamespace

import openpyxl
import pytest

from bonus_agent import case
from bonus_agent.agent import Workspace, ToolError, run_agent
from bonus_agent.auto import build_inventory
from bonus_agent.context import guard_messages, task_card
from bonus_agent.llm import FakeLLM, LLM, LLMError
from bonus_agent.recovery import fingerprint
from bonus_core.engine.run import Engine

pytestmark=pytest.mark.evidence


def make_workspace(tmp_path):
    policy=tmp_path/'policy.md';policy.write_text('每个项目按销售金额的10%计算奖金，按名单比例分配。',encoding='utf8')
    p=tmp_path/'data.xlsx';wb=openpyxl.Workbook();s=wb.active;s.title='projects'
    s.append(['pid','amount']);s.append(['A',100]);s.append(['B',200])
    r=wb.create_sheet('people');r.append(['rid','pid','person','share'])
    r.append(['1','A','甲',1]);r.append(['2','B','甲',.5]);r.append(['3','B','乙',.5])
    wb.save(p);wb.close()
    llm=FakeLLM([])
    llm.checker_required=True;llm.checker_llm=FakeLLM([json.dumps({
        'pool':'amount * 0.1','helpers':{},'tables':{},'details':{'项目奖':'amount * 0.1'},
        'totals':{'项目奖合计':'sum(amount, of: "projects") * 0.1'},'unclear':[]})])
    ws=Workspace(llm,build_inventory([policy,p]));ws.workflow['enabled']=True
    ws.plan_calculation('按项目核算，再按名单分配',['项目底稿','总额','人员金额'],{policy.name:'rule',p.name:'data'},['S'])
    ws.record_facts([{'name':'提成率','file':policy.name,'quote':'销售金额的10%','value':'10%'}])
    ws.record_clauses([{'file':policy.name,'quote':'每个项目按销售金额的10%计算奖金','point':'项目销售金额乘提成率'}])
    f=lambda c,t:{'column':c,'type':t}
    ws.define_dataset('projects',p.name,'projects',{'pid':f('pid','id'),'amount':f('amount','money')},key='pid')
    ws.define_dataset('people',p.name,'people',{'rid':f('rid','id'),'pid':f('pid','id'),
        'person':f('person','text'),'share':f('share','ratio')},key='rid')
    source=[{'doc':policy.name,'quote':'每个项目按销售金额的10%计算奖金'}]
    ws.set_rules(scheme='S',pool={'scope':'项目','expr':'amount * 0.1','sources':source},
        allocation=[{'level':'项目','key':'pid'},{'level':'参与','key':'rid','parent_key':'pid','weight':'share'}],
        report={'people':'person','details':[{'name':'项目奖','level':'项目','expr':'amount * 0.1','sources':source}],
                'distributions':[{'name':'项目分配去向','level':'项目','total':'amount * 0.1','sources':source}],
                'totals':[{'name':'项目奖合计','level':'global','expr':'sum(amount, of: "projects") * 0.1','sources':source}]})
    ws.cover_clauses({'C1':'pool'})
    ws.bind_facts([{'name':'提成率','targets':['S::/pool/expr','S::/report/details/0/expr','S::/report/totals/0/expr','S::/report/distributions/0/total']}])
    return ws


def prepared(ws):
    assert ws.prepare_data()['status']=='PASS'
    assert ws.compile_formulas()['status']=='PASS'
    return ws


def reviewed(ws):
    prepared(ws);ws.calculate_basis('S')
    result=ws.review_basis('S')
    assert result['status']=='PASS',result
    return ws


def test_pool_preview_never_allocates_and_requires_human_approval(tmp_path,monkeypatch):
    ws=prepared(make_workspace(tmp_path))
    monkeypatch.setattr(Engine,'_apply',lambda *a:pytest.fail('preview must not allocate'))
    ws.calculate_basis('S')
    assert not ws.results
    basis=ws.workflow['basis']['S']
    assert Decimal(basis['pool'])==30
    assert {k:Decimal(v['amount']) for k,v in basis['objects'].items()}=={'项目:A':Decimal(10),'项目:B':Decimal(20)}
    with pytest.raises(ToolError,match='review:S'):ws.trial_run('S')
    assert ws.review_basis('S')['status']=='PASS'
    with pytest.raises(ToolError,match='approved:S'):ws.trial_run('S')


def test_confirmed_basis_allocation_matches_exact_people_and_reports(tmp_path):
    ws=reviewed(make_workspace(tmp_path));basis=ws.workflow['basis']['S']
    ws._flow().approve('S','财务',fingerprint(basis))
    ws.trial_run('S')
    got={p['name']:Decimal(p['final']) for p in ws.results['S']['R0'].people}
    assert got=={'甲':Decimal(20),'乙':Decimal(10)}
    assert dict(ws.results['S']['R0'].totals)==basis['totals']


def test_equal_total_changed_object_invalidates_confirmation_after_restore(tmp_path):
    ws=reviewed(make_workspace(tmp_path));ws._flow().approve('S','财务',fingerprint(ws.workflow['basis']['S']))
    snap=case.snapshot(ws)
    clone=Workspace(FakeLLM([]),ws.inv);case.restore(clone,snap)
    assert clone._flow().current('approved:S')
    # Reversing entitlement across objects keeps the same total but changes financial semantics.
    clone.drafts['S']['pool']['expr']='30 - amount * 0.1'
    assert not clone._flow().current('approved:S')
    with pytest.raises(ToolError):clone.trial_run('S')


def test_raw_query_survives_broken_unrelated_parent_and_uses_decimal(tmp_path):
    ws=make_workspace(tmp_path)
    # Simulate a saved, unresolved mapping. Raw source statistics must stay usable.
    ws.datasets['people'].link='missing_parent'
    result=ws.query_dataset('projects','sum(amount, of: "projects")')
    assert Decimal(result['value'])==300
    assert result['rows']==2
    assert ws.prepare_data()['status']=='FAIL'


def test_bad_independent_formula_never_gets_review_or_approval_receipt(tmp_path):
    ws=prepared(make_workspace(tmp_path));ws.calculate_basis('S')
    ws.llm.checker_llm=FakeLLM([json.dumps({'pool':'amount * 0.2','helpers':{},'tables':{},
        'details':{'项目奖':'amount * 0.2'},'totals':{'项目奖合计':'sum(amount, of: "projects") * 0.2'},'unclear':[]})])
    assert ws.review_basis('S')['status']=='FAIL'
    assert not ws._flow().current('review:S')
    with pytest.raises(ToolError):ws._flow().approve('S','财务',fingerprint(ws.workflow['basis']['S']))
    assert ws.review_basis('S')['status']=='FAIL'
    assert ws.review_basis('S')['cached']
    assert len(ws.llm.checker_llm.calls)==1
    assert ws.workflow['last_review']['examples'][0]['object'].startswith('项目:')


def test_independent_reviewer_failure_is_blocking(tmp_path):
    ws=prepared(make_workspace(tmp_path));ws.calculate_basis('S')
    def unavailable(*a,**k):raise LLMError('temporary outage')
    ws.llm.checker_llm.chat=unavailable
    assert ws.review_basis('S')['status']=='ERROR'
    assert ws.done and 'temporary outage' in ws.workflow['review_error']['error']
    assert not ws._flow().current('review:S')


def test_same_version_resume_waits_without_calling_primary_or_exporting_people(tmp_path):
    ws=reviewed(make_workspace(tmp_path));out=tmp_path/'out'
    files=case.fingerprint(ws.inv.files)
    case.save_case(out,{'rounds':[{'n':1,'at':'2026-11-16','files':files,'status':'AWAITING_TOTAL_CONFIRMATION'}],
                        'workspace':case.snapshot(ws)})
    llm=FakeLLM(lambda *a:pytest.fail('waiting for human must not call the model'));llm.workflow_mode='staged'
    ar=run_agent(llm,ws.inv.files,out,max_steps=3)
    assert ar.status=='AWAITING_TOTAL_CONFIRMATION',ar.error
    assert not ar.results and not list(out.glob('result*.xlsx'))
    assert (out/'总额预核算.xlsx').is_file()


def test_bounded_context_has_indices_not_full_original_documents(tmp_path):
    ws=make_workspace(tmp_path)
    text=task_card(ws)
    assert '当前任务' not in text and 'read_workspace' in text
    assert '每个项目按销售金额的10%' not in text
    llm=FakeLLM([]);llm.max_input_tokens=100
    with pytest.raises(LLMError,match='预算'):guard_messages([{'role':'user','content':'x'*200}],llm)


def test_official_context_metadata_is_recognized(monkeypatch):
    llm=LLM('test','deepseek-flash','http://localhost','')
    client=SimpleNamespace(models=SimpleNamespace(list=lambda:[SimpleNamespace(id='deepseek-flash',
        context_window=1048576,max_output_tokens=393216,model_extra={})]))
    monkeypatch.setattr('bonus_agent.llm.openai_client',lambda *a:client)
    assert llm.context_window()==1048576
    assert llm._max_output_tokens==393216


def save_staged_case(ws, out):
    case.save_case(out,{'rounds':[{'n':1,'at':'2026-11-16','files':case.fingerprint(ws.inv.files),
        'source_inputs':[{'name':p.name,'path':str(p),'hash':case.sha256(p)} for p in ws.inv.files],
        'status':'AWAITING_TOTAL_CONFIRMATION'}],'workspace':case.snapshot(ws)})


def test_primary_stops_after_review_even_with_later_writes_in_same_reply(tmp_path):
    ws=prepared(make_workspace(tmp_path));out=tmp_path/'out';save_staged_case(ws,out)
    llm=FakeLLM([{'tool':'calculate_basis','args':{'scheme':'S'}},[
        {'tool':'review_basis','args':{'scheme':'S'}},
        {'tool':'note','args':{'topic':'late','text':'must not execute after stopping'}}]])
    llm.workflow_mode='staged';llm.checker_required=True;llm.checker_llm=ws.llm.checker_llm
    ar=run_agent(llm,ws.inv.files,out,max_steps=5)
    assert ar.status=='AWAITING_TOTAL_CONFIRMATION',[(s.tool,s.result[:300]) for s in ar.steps if not s.ok]
    assert not ar.results and 'late' not in ar.ws.notes
    assert (out/'总额预核算.xlsx').exists() and not (out/'result.xlsx').exists()
    assert not any(s.tool=='trial_run' for s in ar.steps)


def test_case_confirmation_recomputes_and_rejects_changed_input(tmp_path):
    ws=reviewed(make_workspace(tmp_path));out=tmp_path/'out';save_staged_case(ws,out)
    basis=ws.workflow['basis']['S']
    rec=case.confirm_totals(out,'S',fingerprint(basis),'财务')
    assert rec['status']=='PASS'
    clone=Workspace(FakeLLM([]),ws.inv);case.restore(clone,case.load_case(out)['workspace'])
    assert clone._flow().current('approved:S')
    (tmp_path/'policy.md').write_text('原始规则已修改',encoding='utf8')
    with pytest.raises(case.ConfirmError,match='变化'):case.confirm_totals(out,'S',fingerprint(basis),'财务')


def test_modified_basis_cannot_be_approved_even_if_browser_digest_matches(tmp_path):
    ws=reviewed(make_workspace(tmp_path));ws.workflow['basis']['S']['pool']='999.00'
    out=tmp_path/'out';save_staged_case(ws,out)
    with pytest.raises(case.ConfirmError,match='不一致'):
        case.confirm_totals(out,'S',fingerprint(ws.workflow['basis']['S']),'财务')
    assert 'approved:S' not in case.load_case(out)['workspace']['workflow']['receipts']


def test_approved_resume_runs_allocation_but_never_skips_final_validation(tmp_path):
    ws=reviewed(make_workspace(tmp_path));out=tmp_path/'out';save_staged_case(ws,out)
    case.confirm_totals(out,'S',fingerprint(ws.workflow['basis']['S']),'财务')
    llm=FakeLLM([{'tool':'trial_run','args':{'scheme':'S'}}]);llm.workflow_mode='staged'
    ar=run_agent(llm,ws.inv.files,out,max_steps=1)
    assert not ar.ok and not ar.results and not (out/'result.xlsx').exists()
    assert ar.ws.results['S']['R0'].pool=='30.00'
    assert ar.status=='FAILED' and '最大步数' in ar.error


def test_recipient_sum_rounding_is_preserved_in_basis_and_final_engine():
    from bonus_core.dsl.models import RuleSet
    from bonus_core.data.table import from_records
    rs=RuleSet.model_validate({'name':'按人汇总后舍入','version':'v1','datasets':{
        'staff':{'key':'emp','fields':{'emp':{'type':'id'}}},
        'work':{'link':'emp','fields':{'emp':{'type':'id'},'amount':{'type':'money'}}}},
        'pool':{'scope':'人员','expr':'sum(amount, of: "work")'},
        'allocation':[{'level':'人员','key':'emp'}]})
    data=from_records(rs,{'staff':[{'emp':'A'}],'work':[{'emp':'A','amount':'.015'},{'emp':'A','amount':'.015'}]})
    basis=Engine.prepare(rs,data).calculate_basis()
    final=Engine(rs,data).compute()
    assert basis['objects']['人员:A']['amount']==final.nodes['人员:A'].final=='0.03'


@pytest.mark.reread
@pytest.mark.verify
def test_full_staged_financial_validation_without_skipping_checker_or_samples(tmp_path):
    ws=reviewed(make_workspace(tmp_path))
    ws._flow().approve('S','财务',fingerprint(ws.workflow['basis']['S']))
    ws.trial_run('S')
    ws.llm.checker_llm.reread=json.dumps({'items':[{'name':'项目奖金','columns':['projects/amount'],
        'constants':[{'value':10,'unit':'%'}]}]})
    ws.llm.checker_llm.calc=json.dumps({'schemes':{'S':{'pool':'amount * 0.1',
        'helpers':{},'tables':{},'details':{'项目奖':'amount * 0.1'},
        'people':{'key':'person','mode':'allocation','weights':{'参与':'share'}}}}})
    assert ws.review_calculation()['status']=='PASS'
    targets=ws._verify_targets('S')
    assert targets
    for node,kind,_ in targets:
        expr=('sum(amount, of: "projects") * 0.1' if node=='全部' else
              'amount * 0.1' if node.startswith('项目:') else 'amount * 0.1 * share')
        ws.verify(node,expr,'每个项目按销售金额的10%计算奖金，按名单比例分配。',scheme='S',amount=kind)
    assert ws.validation_report()['status']=='PASS'
    assert ws.finish('项目按10%核算，按名单比例分配',review=False)=='已完成'


def test_rewording_production_formula_cannot_resample_a_disagreeing_reference(tmp_path):
    ws=prepared(make_workspace(tmp_path));ws.calculate_basis('S')
    ws.llm.checker_llm=FakeLLM([json.dumps({'pool':'amount * 0.2','helpers':{},'tables':{},
        'details':{'项目奖':'amount * 0.2'},'totals':{'项目奖合计':'sum(amount, of: "projects") * 0.2'},'unclear':[]})])
    assert ws.review_basis('S')['status']=='FAIL'
    ws.set_rules(scheme='S',pool={'scope':'项目','expr':'amount * 0.1 + 0'})
    assert ws.compile_formulas()['status']=='PASS'
    ws.calculate_basis('S')
    report=ws.review_basis('S')
    assert report['status']=='FAIL' and report['cached']
    assert len(ws.llm.checker_llm.calls)==1


def test_new_material_version_requires_a_new_inventory_plan(tmp_path):
    ws=make_workspace(tmp_path);saved=case.snapshot(ws)
    extra=tmp_path/'补充说明.md';extra.write_text('不改变金额的补充规则也须重新核查。',encoding='utf8')
    clone=Workspace(FakeLLM([]),build_inventory([*ws.inv.files,extra]));case.restore(clone,saved)
    assert clone._flow().stage()[0]==0
    with pytest.raises(ToolError,match='材料清单'):clone.prepare_data()
