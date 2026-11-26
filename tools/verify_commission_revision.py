"""Offline acceptance of financial revisions against a COPY of a real case.

No provider or expected-result workbook is read. The test double replays the
already accepted source-binding contract; its review response tests the
feedback protocol, not the independent model's intelligence. The application
entry, deterministic engine, approval checks, and Excel export are real.
"""
from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bonus_agent import case as cs, commission_revision as cr
from bonus_agent.agent import run_agent
from bonus_agent.commission_chain import MODE, SCHEME, basis_contract_digest
from bonus_agent.llm import FakeLLM
from bonus_agent.recovery import fingerprint
from bonus_agent.scenarios.context import AgentScenario
from bonus_core.engine.commission import project_money


def read(p):return json.loads(p.read_text(encoding='utf-8'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path,help='Original completed fixed case output (read only)')
    parser.add_argument('destination',type=Path,help='New isolated validation directory, must not exist')
    args=parser.parse_args()
    source=args.source.resolve();dest=args.destination.resolve()
    if dest.exists() or dest.is_relative_to(source) or source.is_relative_to(dest):raise SystemExit('Use a new independent output directory.')
    original=cs.load_case(source);state=cr.state_of(original);last=original['rounds'][-1]
    assert state and last['status']=='OK' and state.get('approval'), 'Need a completed approved fixed-chain case'
    original_digest=cs.sha256(source/'case.json')
    out=dest/'out';out.mkdir(parents=True)
    for p in source.iterdir():
        if p.is_file() and p.name not in ('.case.lock','commission_revisions.json'):
            shutil.copy2(p,out/p.name)
    # The input bytes are only read; preserve the exact original source order.
    paths=[Path(x['path']) for x in last['source_inputs']]
    assert cs.fingerprint(paths)==state['files']
    scenario=AgentScenario('sales_commission','',execution=MODE,rule_store=dest/'rules'/'versions.json')
    period=state['period'];model=last['model'];checker_name=state['receipts']['basis_review']['reviewer']
    audit=[];contracts=copy.deepcopy(state['specs'])
    original_detail=read(source/'commission_details.json')

    def parser_reply(messages):
        req=json.loads(messages[-1]['content']);node=req['current_node']
        audit.append({'model':'scripted binding','node':node,'feedback':bool(req['input'].get('financial_revision'))})
        return json.dumps(contracts[node],ensure_ascii=False)

    def checker_reply(messages):
        req=json.loads(messages[-1]['content']);node=req['current_node'];data=req['input']
        audit.append({'model':'scripted review','node':node,'feedback':bool(data.get('financial_revision'))})
        reply={'status':'PASS','reason':'离线协议演练：真实旧合同由程序重新验算；此处为测试双桩，不是在线独立意见。'}
        feedback=data.get('financial_revision',{})
        if feedback.get('requests') and node in ('basis_review','settlement_review'):
            evidence=feedback['source_evidence']
            rows=[]
            for r in feedback['requests']:
                preferred='U'+r['recipient']['id'] if r.get('recipient') else ''
                if preferred not in evidence:
                    preferred=next((k for k,v in evidence.items() if k.startswith('V') and r.get('rule',{}).get('id','not-found') in v['where']),next(iter(evidence)))
                raw=evidence[preferred]['text']
                rows.append({'id':r['id'],'identified':r['description'],'checked':'核对实际演练合同、来源及程序逐项目/角色复算输入',
                             'disposition':'verified','conclusion':'离线验证反馈回执、版本门禁和确定性计算；不声明语言模型真实判断通过。',
                             'evidence':[{'id':preferred,'quote':raw[:min(120,len(raw))]}]})
            reply['feedback_findings']=rows
        return json.dumps(reply,ensure_ascii=False)

    def run(**kw):
        llm=FakeLLM(parser_reply,name=model);llm.checker_llm=FakeLLM(checker_reply,name=checker_name)
        before=len(audit)
        ar=run_agent(llm,paths,out,scenario=scenario,period=period,max_tokens=None,**kw)
        print(json.dumps({'round':ar.round,'status':ar.status,'error':ar.error,'model_double_calls':audit[before:]},ensure_ascii=False),flush=True)
        if ar.error:raise AssertionError(ar.error)
        return ar

    def current():return cr.state_of(cs.load_case(out))

    def confirm(token):return cs.confirm_totals(out,SCHEME,token,'离线测试审批（非生产审批）')

    def reject(token):
        try:confirm(token)
        except cs.ConfirmError:return
        raise AssertionError('Superseded confirmation token was accepted')

    old_token=cr.receipt_token(state)
    rev=cr.submit(out,by='离线验证',stage='basis',owner='coefficient_policy',description='核对客户达成系数来源和默认参数；不改变原规则。')
    reject(old_token)
    try:cs._checked_round(out)
    except cs.ConfirmError:pass
    else:raise AssertionError('Superseded result remained confirmable')
    ar=run();assert ar.status=='AWAITING_TOTAL_CONFIRMATION'
    revised=current();token=cr.receipt_token(revised)
    assert revised['basis_digest']==state['basis_digest'] and token!=old_token
    assert any(x['node']=='coefficient_policy' and x['model']=='scripted binding' and x['feedback'] for x in audit)
    assert not any(x['node']=='scope_mapping' and x['model']=='scripted binding' for x in audit)
    reject(old_token);confirm(token)
    assert run().status=='OK'
    assert current()['approval']['token']==token
    dif=read(out/'revision_diff.json');assert dif['no_project_change'] and not dif['people'] and dif['findings']['basis_review']

    # A precise recipient change is not an instruction to recalculate the pool.
    before=read(out/'commission_details.json')
    candidate=next(r for r in before['settlement']['rows'] if r['destination']=='person' and Decimal(r['raw'])>0)
    k,role=candidate['project'],candidate['role']
    confirmations=len(cs.load_case(out)['total_confirmations'])
    cr.submit(out,by='离线测试审批',approver=True,kind='recipient_change',project_keys=[k],role=role,
              shares='演练人员甲70%，演练人员乙30%',description='只在副本中验证70/30人员份额修订')
    mark=len(audit);assert run().status=='OK'
    after=read(out/'commission_details.json');assert fingerprint(after['basis'])==fingerprint(before['basis'])
    assert len(cs.load_case(out)['total_confirmations'])==confirmations and cr.receipt_token(current())==token
    assert not any(x['node']=='basis_review' for x in audit[mark:])
    selected=[r for r in after['settlement']['rows'] if (r['project'],r['role'])==(k,role)]
    assert {r['name']:Decimal(r['share']) for r in selected}=={'演练人员甲':Decimal('.7'),'演练人员乙':Decimal('.3')}
    assert all(Decimal(r['raw'])==Decimal(after['basis']['projects'][k]['alloc'])*Decimal(r['role_rate'])*Decimal(r['share']) for r in selected)
    assert sorted(fingerprint(r) for r in after['settlement']['rows'] if (r['project'],r['role'])!=(k,role))==sorted(fingerprint(r) for r in before['settlement']['rows'] if (r['project'],r['role'])!=(k,role))
    assert not read(out/'revision_diff.json')['projects']['indicators']

    # Change the real hold algorithm on an isolated copy, then independently
    # recompute the affected project amounts from the source-derived factors.
    old_method=current()['specs']['hold_policy']['hold_method'];new_method='multiplicative' if old_method=='additive' else 'additive'
    cr.submit(out,by='离线测试审批',approver=True,kind='rule_change',field='hold_method',value=new_method,
              description='仅演练副本修改暂扣算法，验证影响范围及新总额审批',scope='this_case')
    reject(token);mark=len(audit);assert run().status=='AWAITING_TOTAL_CONFIRMATION'
    revised=current();new_token=cr.receipt_token(revised);assert new_token!=token
    assert revised['specs']['hold_policy']['hold_method']==new_method
    assert not any(x['model']=='scripted binding' for x in audit[mark:]), 'Typed amendment should not re-extract other rules'
    for key,project in revised['basis_view']['projects'].items():
        original_project=before['basis']['projects'][key]
        args=[Decimal(original_project[n]) for n in ('tier','region_factor','customer_factor','gross_factor','stock_factor','overdue_factor')]
        money=project_money(*args,original_project['frozen'],new_method,revised['specs']['hold_policy']['project_round_digits'])
        for f in ('total','hold','debt','alloc'):assert Decimal(project[f])==money[f]
    # The real September dataset may contain no project with both risk
    # factors below one. In that case both formulas produce identical money,
    # but the newly authorized policy STILL requires new approval.
    simultaneous_risks=sum(Decimal(p['stock_factor'])!=1 and Decimal(p['overdue_factor'])!=1 for p in before['basis']['projects'].values())
    if not simultaneous_risks:assert before['basis']['totals']==revised['basis_view']['totals']
    reject(token);confirm(new_token);assert run().status=='OK'

    # A second amendment must reach an actual changed branch, not merely
    # another successful no-op. Pick an observed age on the other side of
    # the current threshold; this is an exercise on a copy, not a new rule
    # for the production case.
    before=current();old_age=before['specs']['project_policy']['age']['months']
    ages=[p['age'] for p in before['basis_view']['projects'].values() if p['old']]
    if not ages:raise AssertionError('Exercise requires at least one observed old-project branch')
    new_age=min(ages)+5
    if before['specs']['project_policy']['age']['old_if']=='ge':new_age+=1
    cr.submit(out,by='离线测试审批',approver=True,kind='rule_change',field='age_months',value=str(new_age),
              description='仅在副本演练新老分界变化和实际受影响项目',scope='this_case')
    reject(new_token);assert run().status=='AWAITING_TOTAL_CONFIRMATION'
    aged=current();age_token=cr.receipt_token(aged);assert age_token!=new_token
    assert aged['specs']['project_policy']['age']['months']==new_age
    changed=[k for k,p in aged['basis_view']['projects'].items() if p['old']!=before['basis_view']['projects'][k]['old']]
    assert changed and aged['basis_view']['totals']!=before['basis_view']['totals']
    for key,p in aged['basis_view']['projects'].items():
        assert p['old']==(p['age']>new_age if aged['specs']['project_policy']['age']['old_if']=='gt' else p['age']>=new_age)
        money=project_money(*(Decimal(p[n]) for n in ('tier','region_factor','customer_factor','gross_factor','stock_factor','overdue_factor')),
                            p['frozen'],new_method,aged['specs']['hold_policy']['project_round_digits'])
        assert Decimal(p['alloc'])==money['alloc']
    confirm(age_token);assert run().status=='OK'

    # A rule scheduled after the current period must not create a new basis.
    year,month=int(period[:4]),int(period[4:]);future=f'{year+(month==12):04}{month%12+1:02}'
    current_revision=cr.active_id(cr.load(out));current_basis=current()['basis_digest']
    result=cr.submit(out,by='离线测试审批',approver=True,kind='rule_change',field='age_months',value=str(current()['specs']['project_policy']['age']['months']+1),
               description='演练未来生效，绝不改变本期',scope='future_periods',effective_from=future,rule_store=scenario.rule_store)
    assert result['status']=='SCHEDULED' and cr.active_id(cr.load(out))==current_revision
    assert result['rule'] not in cr.effective_rules(cr.load(out),scenario.rule_store,period)
    assert result['rule'] in cr.effective_rules({'case_rules':[]},scenario.rule_store,future)
    assert current()['basis_digest']==current_basis
    cs._checked_round(out)

    # Value-only replacement of a real bound budget workbook. The original
    # file remains read-only; only a copied numeric source cell is changed.
    from bonus_agent.commission_sources import Catalog, string
    from bonus_agent.commission_calculation import REF_FIELDS
    from openpyxl import load_workbook
    from openpyxl.utils import column_index_from_string
    catalog=Catalog(paths);before=current()
    mapping=before['specs']['reference_mapping']['region_budget'];sheet=catalog.sheet(mapping)
    budgets={p['region']:Decimal(p['region_budget']) for p in before['basis_view']['projects'].values() if p['region_budget'] is not None and Decimal(p['region_budget'])>0}
    selected=next((row,where) for row,where,_ in sheet.records(mapping,REF_FIELDS['region_budget']) if string(row['region']) in budgets and Decimal(str(row['amount']))==budgets[string(row['region'])])
    row,where=selected;region=string(row['region']);new_budget=Decimal(str(row['amount']))*2
    src=next(p for p in paths if p.name==sheet.file)
    copied=dest/'material_revision'/src.name;copied.parent.mkdir(exist_ok=True);shutil.copy2(src,copied);original_source_hash=cs.sha256(src)
    cr.submit(out,by='离线验证',kind='material_correction',description='仅在副本更正一个已绑定区域预算值，验证责任节点选择',source_changes=[copied])
    workbook=load_workbook(copied)
    assert not any(c.data_type=='f' for ws in workbook for cells in ws for c in cells), 'Exercise requires a value-only workbook'
    workbook[sheet.name].cell(int(where.rsplit('!',1)[1]),column_index_from_string(mapping['columns']['amount'])).value=float(new_budget)
    workbook.save(copied);workbook.close();paths=[copied if p==src else p for p in paths]
    mark=len(audit);assert run().status=='AWAITING_TOTAL_CONFIRMATION'
    material=current();assert set(material['revision_owners'])=={'inventory','reference_mapping','basis_review'}
    assert not any(x['model']=='scripted binding' and x['node']!='reference_mapping' for x in audit[mark:])
    assert all(Decimal(p['region_budget'])==new_budget for p in material['basis_view']['projects'].values() if p['region']==region)
    assert all(p['region_budget']==before['basis_view']['projects'][k]['region_budget'] for k,p in material['basis_view']['projects'].items() if p['region']!=region)
    assert cr.receipt_token(material)!=age_token;reject(age_token);confirm(cr.receipt_token(material));assert run().status=='OK'
    assert cs.sha256(src)==original_source_hash
    before_round=cs.load_case(out)['rounds'][-1]['n'];mark=len(audit)
    assert run(message='当前总额怎么算？').reply
    assert len(audit)==mark and cs.load_case(out)['rounds'][-1]['n']==before_round
    assert cs.sha256(source/'case.json')==original_digest
    report={'status':'PASS','provider_calls':0,'mode':'actual calculation and archive replay with scripted LLM responses',
            'unchanged_rejection_reconfirmed':True,'recipient_only_preserved_approval':True,'hold_rule_recalculated':True,
            'simultaneous_risk_projects':simultaneous_risks,'age_amendment_changed_projects':len(changed),
            'material_revision_routed':'reference_mapping','material_original_source_unchanged':True,
            'future_rule_isolated':True,'explanation_read_only':True,'original_case_sha256':original_digest,
            'original_basis':original_detail['basis']['totals'],'amended_copy_basis':current()['basis_view']['totals'],
            'project_count':len(current()['basis_view']['projects']),'test_double_calls':audit}
    (dest/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='test_double_calls'},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
