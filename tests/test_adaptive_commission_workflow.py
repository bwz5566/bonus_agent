"""Formal-entry, two-stage acceptance for a genuinely different business shape.

The simulated models provide only source bindings and rule contracts. The
same production runner, approval checks, compiler and workbook writer are
used as in a real run. No reference amounts are included in model input.
"""
from copy import deepcopy
from decimal import Decimal as D
import json
from pathlib import Path
from types import SimpleNamespace

from openpyxl import Workbook,load_workbook

from bonus_agent.agent import run_agent
from bonus_agent import case as cs,commission_revision as rv
from bonus_agent.llm import FakeLLM
from bonus_agent.commission_chain import ADAPTIVE_MODE,SCHEME
from test_adaptive_program import fixture,op,ref,fld,key,param,query


def _write_sources(directory):
    directory.mkdir()
    c,ds,params,plan,roster=fixture()
    paths=[]
    for sid,s in c.sheets.items():
        path=directory/s.file;paths.append(path)
        if path.suffix=='.csv':
            import csv
            with path.open('w',newline='',encoding='utf8') as f:csv.writer(f).writerows(s.rows)
        else:
            wb=Workbook();ws=wb.active;ws.title=s.name
            for row in s.rows:ws.append(row)
            wb.save(path);wb.close()
    policy=directory/'entirely_new_rules.md'
    policy.write_text('\n'.join([
        '项目由办事处与合同编号组成，仅本核算期交易参与。',
        '回款及时率按合同关联，实际回款数据每合同唯一。',
        '净销售额等于本期交易的净收入求和；提成费率取制度费率表。',
        '回款及时率系数取该合同实际回款比例。',
        '奖金总额=净销售额×提成率×回款及时率系数',
        '履约暂扣=奖金总额×暂扣率',
        '正常可分配=奖金总额-履约暂扣',
        '项目分配金额=ROUND(正常可分配,2)',
        '合作伙伴角色占60%，售后服务角色占40%。人员名单按合同和角色、员工编号精确匹配。',
        '未指定实名的份额保留待分配；员工按编号汇总后四舍五入2位。',
        '输出项目核算底稿和实名人员及待分配汇总。',
    ]),encoding='utf8');paths.append(policy)
    return paths


class Contracts:
    def __init__(self):
        _,self.ds,self.params,self.plan,self.roster=fixture();self.rules={};self.units={};self.bad_gross=True;self.corrections=0;self.gross_multiplier=None

    def handle_units(self,data):
        decisions={}
        for uid,source in data['units'].items():
            txt=source['text'];self.units[uid]=source
            # Real original contents, not known expected money. A model in
            # production does this interpretation for the supplied clauses.
            settings=[]
            if txt.startswith('项目由'):settings=[('scope','scope_mapping','policy',txt)]
            elif txt.startswith('回款及时率按'):settings=[('ref','reference_mapping','policy',txt)]
            elif txt.startswith('净销售额等于'):settings=[('sales','project_policy','policy',txt)]
            elif txt.startswith('回款及时率系数取'):settings=[('coeff','coefficient_policy','policy',txt)]
            elif txt.startswith('奖金总额='):
                settings=[('gross','hold_policy','formula',txt)]
                self.gross_multiplier='0.5' if txt.endswith('×0.5') else None
            elif txt.startswith('履约暂扣='):settings=[('hold','hold_policy','formula',txt)]
            elif txt.startswith('正常可分配='):settings=[('net','hold_policy','formula',txt)]
            elif txt.startswith('项目分配金额='):settings=[('pay','hold_policy','formula',txt)]
            elif txt.startswith('合作伙伴角色'):settings=[('roles','roster','policy',txt)]
            elif txt.startswith('未指定实名'):settings=[('round','roster','policy',txt)]
            elif txt.startswith('输出项目'):settings=[('output','export','output',txt)]
            elif 'Service' in txt and '10%' in txt and '20%' in txt:settings=[('rate','project_policy','table','10%'),('reserve','hold_policy','table','20%')]
            clauses=[]
            for i,(name,phase,kind,quote) in enumerate(settings,1):
                self.rules[name]=(f'{uid}:{i}',uid,quote);clauses.append({'quote':quote,'meaning':name,'phase':phase,'kind':kind})
            decisions[uid]={'disposition':('output' if settings and settings[0][2]=='output' else 'operative' if settings else 'context'),
                            'reason':'实际制度条款' if settings else '字段标题','clauses':clauses}
        return {'units':decisions}

    def rid(self,name):return self.rules[name][0]
    def _fragment(self,metrics,params=None,formulas=None):
        return {'metrics':metrics,'parameters':params or {},'schedules':{},'checks':[],'formula_bindings':formulas or {}}

    def reply(self,msg):
        payload=json.loads(msg[-1]['content']);node=payload['current_node'];data=payload['input']
        if node=='adaptive/source_inventory':
            return {'sheets':{s['id']:{'kind':'rule' if s['file']=='rate.xlsx' else 'data','reason':'费率原表' if s['file']=='rate.xlsx' else '业务数据','rule_ranges':[]} for s in data['sheets']},'unreadable':{}}
        if node.startswith('adaptive/rules/'):return self.handle_units(data)
        if node=='scope_mapping':
            d=deepcopy(self.ds['orders']);d['rules']=[self.rid('scope')];p=deepcopy(self.plan['population']);p['rules']=[self.rid('scope')]
            return {'datasets':{'orders':d},'population':p}
        if node=='reference_mapping':
            d=deepcopy(self.ds['collection']);d['rules']=[self.rid('ref')];d['grain']['rules']=[self.rid('ref')]
            return {'datasets':{'collection':d},'checks':[]}
        if node=='adaptive/project_policy/outline':return {'groups':[{'id':'base','purpose':'销售基数和费率','rules':[self.rid('sales'),self.rid('rate')],'metrics':{'sales_net':'净销售额'}}]}
        if node=='adaptive/project_policy/group/base':
            m=deepcopy(self.plan['metrics']['sales_net']);m['rules']=[self.rid('sales')]
            p=deepcopy(self.params['commission']);p['rules']=[self.rid('rate')]
            return self._fragment({'sales_net':m},{'commission':p})
        if node=='adaptive/coefficient_policy/outline':return {'groups':[{'id':'collection','purpose':'新增回款指标','rules':[self.rid('coeff')],'metrics':{'collect_factor':'回款系数'}}]}
        if node=='adaptive/coefficient_policy/group/collection':
            m=deepcopy(self.plan['metrics']['collect_factor']);m['rules']=[self.rid('coeff')];return self._fragment({'collect_factor':m})
        if node=='adaptive/hold_policy/outline':return {'groups':[{'id':'amounts','purpose':'资金去向','rules':[self.rid(x) for x in ('gross','hold','net','pay','reserve')],
            'metrics':{k:self.plan['metrics'][k]['label'] for k in ('gross','reserve','net','pay')}}]}
        if node=='adaptive/hold_policy/group/amounts':
            m={k:deepcopy(self.plan['metrics'][k]) for k in ('gross','reserve','net','pay')}
            for k,r in [('gross','gross'),('reserve','hold'),('net','net'),('pay','pay')]:m[k]['rules']=[self.rid(r)]
            p=deepcopy(self.params['reserve']);p['rules']=[self.rid('reserve')]
            formulas={self.rid('gross'):{'target':'gross','symbols':{'净销售额':ref('sales_net'),'提成率':param('commission'),'回款及时率系数':ref('collect_factor')}},
                self.rid('hold'):{'target':'reserve','symbols':{'奖金总额':ref('gross'),'暂扣率':param('reserve')}},
                self.rid('net'):{'target':'net','symbols':{'奖金总额':ref('gross'),'履约暂扣':ref('reserve')}},
                self.rid('pay'):{'target':'pay','symbols':{'正常可分配':ref('net')}}}
            if self.bad_gross:
                m['gross']['expr']=op('mul',ref('sales_net'),param('commission'))  # deliberate material-coverage counterexample
            elif self.gross_multiplier:m['gross']['expr']=op('mul',ref('sales_net'),param('commission'),ref('collect_factor'),{'num':self.gross_multiplier})
            return self._fragment(m,{'reserve':p},formulas)
        if node=='adaptive/hold_policy/funding':
            f=deepcopy(self.plan['funding']);f['rules']=[self.rid(x) for x in ('gross','hold','net','pay')];return f
        if node=='adaptive/roster/mapping':
            d=deepcopy(self.ds['staff']);d['rules']=[self.rid('roles')];return {'datasets':{'staff':d}}
        if node=='roster':
            r=deepcopy(self.roster);rule,uid,quote=self.rules['roles']
            params={}
            for role,value in [('partner','60%'),('service','40%')]:
                params[role+'_rate']={'source':{'block':uid,'quote':quote,'value':value},'kind':'ratio','unit':'raw','rules':[rule]}
                r['roles'][role]['rate']=param(role+'_rate');r['roles'][role]['rules']=[rule];r['roles'][role]['recipients']['rules']=[self.rid('round')]
            r['rounding']['rules']=[self.rid('round')];r['rules']=[rule,self.rid('round')];r.update(parameters=params,schedules={},formula_bindings={});return r
        if node=='adaptive/export/layout':
            return {'columns':[{'label':'合同号','source':key('agreement'),'rules':[self.rid('output')]}],
                    'sections':{'summary':{'rules':[self.rid('output')]},'project':{'rules':[self.rid('output')]}},'rules':[],'formula_bindings':{},'groups':[]}
        raise AssertionError(f'Unexpected primary request {node}')

    def primary(self,msg):return json.dumps(self.reply(msg),ensure_ascii=False)
    def reviewer(self,msg):
        payload=json.loads(msg[-1]['content']);node=payload['current_node']
        if node=='adaptive/hold_policy/group/amounts' and 'specific_counterexample' in payload and self.bad_gross:
            assert '计算树不同' in payload['specific_counterexample']
            self.bad_gross=False;self.corrections+=1
            return json.dumps({'status':'REVISE','reason':'原文回款因子没有参与公式','replacement':self.reply(msg)},ensure_ascii=False)
        revision=payload['input'].get('financial_revision',{})
        reply={'status':'PASS','reason':'fixture independent contract review'}
        if node in ('basis_review','settlement_review') and revision.get('requests'):
            sources=revision['source_evidence'];sid=next((k for k in sources if k.startswith('Rrev_')),next(iter(sources)))
            quote=sources[sid]['text'];reply['feedback_findings']=[{'id':r['id'],'identified':r['description'],'checked':'实际来源及逐项计算','disposition':'corrected','conclusion':'按明确的新规则或名单处理，原始记录保留','evidence':[{'id':sid,'quote':quote}]} for r in revision['requests']]
        return json.dumps(reply,ensure_ascii=False)


def test_adaptive_formal_entry_full_cold_run_with_local_repair(tmp_path):
    paths=_write_sources(tmp_path/'materials');out=tmp_path/'case';c=Contracts()
    primary=FakeLLM(c.primary,name='fixture-flash');checker=FakeLLM(c.reviewer,name='fixture-pro');primary.checker_llm=checker
    scenario=SimpleNamespace(execution='sales_commission_v1',files=[],rule_store=None)  # existing local scenario migrates fresh cases
    ar=run_agent(primary,paths,out,scenario=scenario,period='2027-01',max_steps=250)
    assert ar.status=='AWAITING_TOTAL_CONFIRMATION',ar.error
    case=cs.load_case(out);state=rv.state_of(case);assert state['mode']==ADAPTIVE_MODE
    workflow=json.loads((out/'workflow.json').read_text(encoding='utf8'))
    assert workflow['mode']==ADAPTIVE_MODE and all('四键' not in n['name'] for n in workflow['nodes'])
    assert c.corrections==1 and D(state['basis_view']['pool'])==D('108.8')
    assert not (out/'result.xlsx').exists()
    token=rv.receipt_token(state);cs.confirm_totals(out,SCHEME,token,'测试财务（虚构用例）')
    calls=len(primary.calls)
    ar=run_agent(primary,paths,out,scenario=scenario,period='2027-01',max_steps=250)
    assert ar.status=='OK',ar.error
    assert len(primary.calls)-calls==3  # roster mapping, roster, report layout; basis contracts reused
    result=json.loads((out/'commission_details.json').read_text(encoding='utf8'))
    people={(x['destination'],x['id']):D(x['amount']) for x in result['settlement']['people']}
    assert people=={('person','A001'):D('65.28'),('person','B001'):D('30.72'),('pending','售后服务待分配'):D('12.80')}
    assert D(result['settlement']['residual'])==0
    cs._checked_round(out)
    wb=load_workbook(out/'result.xlsx',data_only=False,read_only=True)
    try:
        labels=[x.value for x in next(wb['提成奖核算底稿'].iter_rows())];assert '合同号' in labels and '合作伙伴金额' in labels and '售后服务金额' in labels
        assert wb['角色分配']['H2'].data_type=='f'
    finally:wb.close()


def test_adaptive_failure_shows_specific_original_rule_and_responsibility(tmp_path):
    class InvalidSource(Contracts):
        def handle_units(self,data):
            proposals=super().handle_units(data)
            for uid, unit in data['units'].items():
                if unit['text'].startswith('项目由'):
                    proposals['units'][uid]['clauses'][0]['quote']='并不存在于原件的引用'
            return proposals
    paths=_write_sources(tmp_path/'materials');out=tmp_path/'case';c=InvalidSource()
    primary=FakeLLM(c.primary,name='fixture-flash');primary.checker_llm=FakeLLM(c.reviewer,name='fixture-pro')
    scenario=SimpleNamespace(execution=ADAPTIVE_MODE,files=[],rule_store=None)
    ar=run_agent(primary,paths,out,scenario=scenario,period='2027-01',max_steps=250)
    assert ar.status=='FAILED' and not (out/'result.xlsx').exists()
    case=cs.load_case(out);state=rv.state_of(case)
    detail=state['adaptive_diagnostic'];diagnosis=ar.failure_context
    assert detail['rules'] and 'entirely_new_rules.md' in str(detail['location'])
    assert 'entirely_new_rules.md' in diagnosis['item'] and '项目由办事处与合同编号组成' in diagnosis['identified']
    assert '并不存在于原件的引用' in diagnosis['missing'] and '总额' in diagnosis['impact']
    assert case['rounds'][-1]['failure_context']==diagnosis
    validation=json.loads((out/'validation.json').read_text(encoding='utf8'))
    assert validation['diagnostic']==detail and validation['failure_context']==diagnosis


def test_global_money_units_are_enforced_before_total_confirmation(tmp_path):
    class UnitPolicy(Contracts):
        def __init__(self,scale):super().__init__();self.scale=scale;self.bad_gross=False
        def handle_units(self,data):
            result=super().handle_units(data)
            for uid,unit in data['units'].items():
                if unit['text'].startswith('输入数据金额单位统一'):
                    self.rules['amount_unit']=(uid+':1',uid,unit['text'])
                    result['units'][uid]={'disposition':'operative','reason':'实际金额单位','clauses':[{'quote':unit['text'],'meaning':'万元金额','phase':'scope_mapping','kind':'policy'}]}
            return result
        def reply(self,msg):
            v=super().reply(msg);node=json.loads(msg[-1]['content'])['current_node']
            if node=='scope_mapping':
                v['datasets']['orders']['rules'].append(self.rid('amount_unit'))
                if self.scale:v['datasets']['orders']['fields']['revenue']['unit']='wan'
            if node=='reference_mapping' and self.scale:v['datasets']['staff']=deepcopy(self.ds['staff'])
            if node=='adaptive/roster/mapping' and self.scale:v['datasets']={}
            return v
    paths=_write_sources(tmp_path/'materials');policy=next(p for p in paths if p.suffix=='.md')
    with policy.open('a',encoding='utf8') as f:f.write('\n输入数据金额单位统一为万元。')
    c=UnitPolicy(False);primary=FakeLLM(c.primary,name='fixture-flash');primary.checker_llm=FakeLLM(c.reviewer,name='fixture-pro')
    out=tmp_path/'case';scenario=SimpleNamespace(execution=ADAPTIVE_MODE,files=[],rule_store=None)
    ar=run_agent(primary,paths,out,scenario=scenario,period='2027-01',max_steps=250)
    assert ar.status=='FAILED' and '金额单位' in ar.error and '万元' in ar.error
    assert not (out/'总额预核算.xlsx').exists()
    c=UnitPolicy(True);primary=FakeLLM(c.primary,name='fixture-flash');primary.checker_llm=FakeLLM(c.reviewer,name='fixture-pro')
    out=tmp_path/'correctly-scaled'
    ar=run_agent(primary,paths,out,scenario=scenario,period='2027-01',max_steps=250)
    assert ar.status=='AWAITING_TOTAL_CONFIRMATION',ar.error
    state=rv.state_of(cs.load_case(out));assert D(state['basis_view']['pool'])==D('1088000')
    cs.confirm_totals(out,SCHEME,rv.receipt_token(state),'测试财务')
    ar=run_agent(primary,paths,out,scenario=scenario,period='2027-01',max_steps=250)
    assert ar.status=='OK',ar.error


def test_cross_stage_foreign_key_is_repaired_before_metric_requests(tmp_path):
    class EarlyMaster(Contracts):
        def __init__(self):super().__init__();self.bad_gross=False;self.fixed=False;self.checked_at_ref=False
        def reply(self,msg):
            v=super().reply(msg);node=json.loads(msg[-1]['content'])['current_node']
            if node=='scope_mapping':
                v['datasets']['orders']['relationships']=[{'fields':['agreement'],'target':'staff','target_fields':['agreement'],'cardinality':'at_least_one','rules':[self.rid('scope')]}]
            if node=='reference_mapping' and self.fixed:v['datasets']['staff']=deepcopy(self.ds['staff'])
            if node=='adaptive/roster/mapping' and self.fixed:v['datasets']={}
            return v
        def reviewer(self,msg):
            data=json.loads(msg[-1]['content']);node=data['current_node']
            if node=='reference_mapping' and 'specific_counterexample' in data and '尚未绑定' in data['specific_counterexample']:
                self.fixed=True;self.checked_at_ref=True
                return json.dumps({'status':'REVISE','reason':'实际外键目标必须在计算前绑定','replacement':self.reply(msg)},ensure_ascii=False)
            if node.startswith('adaptive/project_policy/'):
                assert self.checked_at_ref, '模型不应先编公式再发现原始主表尚未绑定'
            return super().reviewer(msg)
    paths=_write_sources(tmp_path/'materials');out=tmp_path/'case';c=EarlyMaster()
    primary=FakeLLM(c.primary,name='fixture-flash');primary.checker_llm=FakeLLM(c.reviewer,name='fixture-pro')
    scenario=SimpleNamespace(execution=ADAPTIVE_MODE,files=[],rule_store=None)
    ar=run_agent(primary,paths,out,scenario=scenario,period='2027-01',max_steps=250)
    assert ar.status=='AWAITING_TOTAL_CONFIRMATION' and c.checked_at_ref,ar.error
    state=rv.state_of(cs.load_case(out));assert state['basis_view']['data_bindings']['orders']['relationships'][0]['target']=='staff'
    cs.confirm_totals(out,SCHEME,rv.receipt_token(state),'测试财务')
    ar=run_agent(primary,paths,out,scenario=scenario,period='2027-01',max_steps=250)
    assert ar.status=='OK',ar.error


def test_reference_rules_reuse_real_scope_contract_without_duplicate_datasets(tmp_path):
    class BoundOnce(Contracts):
        def __init__(self):super().__init__();self.bad_gross=False;self.fixed=False
        def reply(self,msg):
            data=json.loads(msg[-1]['content']);node=data['current_node'];v=super().reply(msg)
            if node=='scope_mapping':
                d=deepcopy(self.ds['collection']);d['rules']=[self.rid('ref')];d['grain']['rules']=[self.rid('ref')];v['datasets']['collection']=d
                v['datasets']['orders']['relationships']=[{'fields':['agreement'],'target':'collection','target_fields':['agreement'],'cardinality':'exactly_one','rules':[self.rid('ref')]}]
            if node=='reference_mapping':
                existing=data['input']['already_bound']
                # The independent reviewer must see actual physical bindings and relationships,
                # not only name/field summaries that appear to omit real constraints.
                assert existing['orders']['relationships'][0]['target']=='collection'
                assert existing['collection']['physical_bindings'][0]['header_row']==1
                v={'datasets':{},'checks':[] if self.fixed else [{'id':'fake','expr':'orders.A subset collection.A','message':'invalid prose check','rules':[self.rid('ref')]}]}
            return v
        def reviewer(self,msg):
            data=json.loads(msg[-1]['content']);node=data['current_node']
            if node=='reference_mapping' and 'specific_counterexample' in data and '布尔表达式' in data['specific_counterexample']:
                self.fixed=True
                return json.dumps({'status':'REVISE','reason':'使用既有真实外键检查而非伪表达式','replacement':self.reply(msg)},ensure_ascii=False)
            return super().reviewer(msg)
    paths=_write_sources(tmp_path/'materials');out=tmp_path/'case';c=BoundOnce()
    primary=FakeLLM(c.primary,name='fixture-flash');primary.checker_llm=FakeLLM(c.reviewer,name='fixture-pro')
    scenario=SimpleNamespace(execution=ADAPTIVE_MODE,files=[],rule_store=None)
    ar=run_agent(primary,paths,out,scenario=scenario,period='2027-01',max_steps=250)
    assert ar.status=='AWAITING_TOTAL_CONFIRMATION' and c.fixed,ar.error
    state=rv.state_of(cs.load_case(out));assert state['specs']['reference_mapping']=={'datasets':{},'checks':[]}
    proof=state['basis_view']['rule_coverage'][c.rid('ref')]
    assert any(x['phase']=='scope_mapping' and x['path'][0]=='datasets' for x in proof['components'])
    assert D(state['basis_view']['pool'])==D('108.8')


def test_bad_independent_replacement_rechecks_valid_candidate_without_restarting_flash(tmp_path):
    class ReviewFailsOnce(Contracts):
        def __init__(self):super().__init__();self.bad_gross=False;self.compiles=0;self.reviews=0
        def reply(self,msg):
            v=super().reply(msg)
            if json.loads(msg[-1]['content'])['current_node']=='adaptive/hold_policy/funding':self.compiles+=1
            return v
        def reviewer(self,msg):
            data=json.loads(msg[-1]['content'])
            if data['current_node']=='adaptive/hold_policy/funding':
                self.reviews+=1
                if self.reviews==1:
                    return json.dumps({'status':'REVISE','reason':'错误跨层建议','patches':[{'op':'set','path':['total'],'value':'future_person_amount'}]},ensure_ascii=False)
                assert 'future_person_amount' in data['specific_counterexample'] and data['candidate']['total']=='gross'
            return super().reviewer(msg)
    paths=_write_sources(tmp_path/'materials');out=tmp_path/'case';c=ReviewFailsOnce()
    primary=FakeLLM(c.primary,name='fixture-flash');primary.checker_llm=FakeLLM(c.reviewer,name='fixture-pro')
    scenario=SimpleNamespace(execution=ADAPTIVE_MODE,files=[],rule_store=None)
    ar=run_agent(primary,paths,out,scenario=scenario,period='2027-01',max_steps=250)
    assert ar.status=='AWAITING_TOTAL_CONFIRMATION',ar.error
    assert c.compiles==1 and c.reviews==2


def test_adaptive_revision_new_formula_and_dynamic_identity_roundtrip(tmp_path):
    paths=_write_sources(tmp_path/'materials');out=tmp_path/'case';c=Contracts();c.bad_gross=False
    primary=FakeLLM(c.primary,name='fixture-flash');primary.checker_llm=FakeLLM(c.reviewer,name='fixture-pro')
    scenario=SimpleNamespace(execution=ADAPTIVE_MODE,files=[],rule_store=None)
    def run():return run_agent(primary,paths,out,scenario=scenario,period='2027-01',max_steps=250)
    assert run().status=='AWAITING_TOTAL_CONFIRMATION'
    state=rv.state_of(cs.load_case(out));old_token=rv.receipt_token(state)
    cs.confirm_totals(out,SCHEME,old_token,'测试财务');assert run().status=='OK'
    original=cs.sha256(out/'result.xlsx');state=rv.state_of(cs.load_case(out));project=next(k for k,v in state['basis_view']['projects'].items() if v['key_values']['agreement']=='0007')
    r=rv.submit(out,by='测试财务',kind='recipient_change',description='合作伙伴真实更换',project_keys=[project],role='partner',shares='A003:新伙伴100%',approver=True)
    assert not r['basis_changed'];a=run();assert a.status=='OK',a.error
    assert rv.receipt_token(rv.state_of(cs.load_case(out)))==old_token
    details=json.loads((out/'commission_details.json').read_text(encoding='utf8'));assert any(x['id']=='A003' and D(x['amount'])==D('46.08') for x in details['settlement']['people'])
    assert cs.sha256(out/'revisions'/r['snapshot']/'result.xlsx')==original
    state=rv.state_of(cs.load_case(out));rule_field=next(k for k,v in rv.adaptive_rule_fields(state).items() if v['old'].startswith('奖金总额='))
    r2=rv.submit(out,by='测试财务',kind='rule_change',description='正式确认本期总额按新制度乘50%',field=rule_field,
                 value='奖金总额=净销售额×提成率×回款及时率系数×0.5',approver=True)
    assert r2['basis_changed'];a=run();assert a.status=='AWAITING_TOTAL_CONFIRMATION',a.error
    st=rv.state_of(cs.load_case(out));assert D(st['basis_view']['pool'])==D('54.4') and rv.receipt_token(st)!=old_token
    assert not (out/'result.xlsx').exists()
    import pytest
    with pytest.raises(cs.ConfirmError):cs.confirm_totals(out,SCHEME,old_token,'测试财务')
    cs.confirm_totals(out,SCHEME,rv.receipt_token(st),'测试财务');a=run();assert a.status=='OK',a.error
    from bonus_agent.commission_report import reexport_fixed_case
    before=cs.sha256(out/'case.json');report,meta=reexport_fixed_case(out,'测试财务');assert report.is_file() and meta['source_recomputed_exactly'] and meta['provider_calls']==0
    assert cs.sha256(out/'case.json')==before


def test_rule_reviewer_can_correct_one_unit_without_replacing_the_batch(tmp_path):
    class ScopedReview(Contracts):
        def __init__(self):super().__init__();self.corrected=None
        def reviewer(self,msg):
            data=json.loads(msg[-1]['content'])
            if data['current_node'].startswith('adaptive/rules/') and not self.corrected:
                original=data['candidate']['units'];uid=next(k for k,v in original.items() if v['clauses'])
                assert len(original)>1
                local=deepcopy(original[uid]);local['reason']='独立逐条核对该原文'
                self.corrected=(data['current_node'],uid,len(original))
                return json.dumps({'status':'REVISE','reason':'仅修改这个单元的覆盖说明','replacement':{'units':{uid:local}}},ensure_ascii=False)
            return super().reviewer(msg)
    paths=_write_sources(tmp_path/'materials');out=tmp_path/'case';c=ScopedReview()
    primary=FakeLLM(c.primary,name='fixture-flash');primary.checker_llm=FakeLLM(c.reviewer,name='fixture-pro')
    scenario=SimpleNamespace(execution=ADAPTIVE_MODE,files=[],rule_store=None)
    ar=run_agent(primary,paths,out,scenario=scenario,period='2027-01',max_steps=250)
    assert ar.status=='AWAITING_TOTAL_CONFIRMATION',ar.error
    task,uid,count=c.corrected;state=rv.state_of(cs.load_case(out));accepted=state['specs'][task]['units']
    assert len(accepted)==count and accepted[uid]['reason']=='独立逐条核对该原文'


def test_corrected_business_material_reuses_only_unchanged_reviewed_rule_units(tmp_path):
    paths=_write_sources(tmp_path/'materials');out=tmp_path/'case';c=Contracts();c.bad_gross=False
    primary=FakeLLM(c.primary,name='fixture-flash');primary.checker_llm=FakeLLM(c.reviewer,name='fixture-pro')
    scenario=SimpleNamespace(execution=ADAPTIVE_MODE,files=[],rule_store=None)
    def run():return run_agent(primary,paths,out,scenario=scenario,period='2027-01',max_steps=300)
    first=run();assert first.status=='AWAITING_TOTAL_CONFIRMATION',first.error
    before=rv.state_of(cs.load_case(out));old_token=rv.receipt_token(before);old_amount=D(before['basis_view']['pool'])
    old_rules=sum(json.loads(m[-1]['content'])['current_node'].startswith('adaptive/rules/') for m in primary.calls)
    data=next(p for p in paths if p.name=='transactions.csv');original=data.read_text(encoding='utf8');assert ',1000' in original
    data.write_text(original.replace(',1000',',1200',1),encoding='utf8')
    rv.submit(out,by='测试财务（虚构材料修正）',kind='material_correction',description='修改实际销售明细中的一笔交易',source_changes=[data],approver=True)
    second=run();assert second.status=='AWAITING_TOTAL_CONFIRMATION',second.error
    after=rv.state_of(cs.load_case(out));assert D(after['basis_view']['pool'])>old_amount and rv.receipt_token(after)!=old_token
    assert not after.get('approval') and sum(json.loads(m[-1]['content'])['current_node'].startswith('adaptive/rules/') for m in primary.calls)==old_rules
    assert any(e['node']=='adaptive/rules/材料更正后原文重验' and e['status']=='PASS' for e in after['events'])
    # Changed policy sources cannot inherit old unit reviews just because the
    # row happens to share an identical-looking prefix.
    from bonus_agent.commission_chain import unchanged_rule_candidates
    files=deepcopy(after['files']);policy=next(k for k in files if k.endswith('.md'));files[policy]='changed'
    assert all(x['file']!=policy for x in unchanged_rule_candidates(after,files,'fixture-pro').values())


def test_table_review_reuse_carries_full_unit_digest_not_reduced_display_index():
    from bonus_agent.commission_chain import unchanged_rule_candidates
    from bonus_agent.recovery import fingerprint
    source={'file':'actual-policy.pptx','where':'P2.T1.R2.C1','text':'服务 | 20%','table':'T1','row':2,'title':'服务费率'}
    candidate={'units':{'T1R2':{'disposition':'operative','reason':'费率','clauses':[{'quote':'服务 | 20%','meaning':'实际制度费率','kind':'table','level':'primary','operation':'parameter'}]}}}
    receipt={'status':'PASS','reviewer':'fixture-pro','output':fingerprint(candidate)}
    stored={'files':{source['file']:'same-source-version'},'adaptive_rule_sources':{'T1R2':{k:source[k] for k in ('file','where','text')}},
            'adaptive_rule_unit_proofs':{'T1R2':{'task':'review','digest':fingerprint(source),'receipt':receipt,'proposal':candidate['units']['T1R2'],'answer_key':fingerprint([])}},'specs':{'review':candidate}}
    assert fingerprint(source) in unchanged_rule_candidates(stored,stored['files'],'fixture-pro')
    assert not unchanged_rule_candidates(stored,{source['file']:'changed'},'fixture-pro')


class PersonalContracts(Contracts):
    """A different layer graph: direct attribution, then employee calculation."""
    def __init__(self):
        super().__init__();self.bad_gross=False

    def handle_units(self,data):
        result=super().handle_units(data)
        for uid,u in data['units'].items():
            txt=u['text'];name=None;kind='policy'
            prefixes={'每笔项目':'direct','人员主表':'master','人员应得=':'due','实际抵扣=':'applied','本次实发=':'paid','超预发=':'over','预发按':'advance'}
            for prefix,n in prefixes.items():
                if txt.startswith(prefix):name=n;kind='formula' if '=' in prefix else 'policy';break
            if name:
                self.rules[name]=(uid+':1',uid,txt)
                result['units'][uid]={'disposition':'operative','reason':'人员层实际规则','clauses':[{'quote':txt,'meaning':name,'phase':'roster','kind':kind}]}
        return result

    def reply(self,msg):
        payload=json.loads(msg[-1]['content']);node=payload['current_node'];data=payload['input']
        if node=='adaptive/roster/mapping':
            def dataset(label,sid,fields,cols,key_,rule):
                return {'label':label,'fields':{k:{'kind':v,'nullable':False} for k,v in fields.items()},'sources':[{'sheet':sid,'header_row':1,'columns':cols}],
                        'grain':{'kind':'unique','keys':[key_],'duplicates':'error','rules':[rule]},'rules':[rule]}
            staff=deepcopy(self.ds['staff']);staff['rules']=[self.rid('direct')]
            return {'datasets':{'staff':staff,
                'employees':dataset('人员全集','S6',{'id':'id','name':'text','factor':'ratio'},{'id':'A','name':'B','factor':'C'},'id',self.rid('master')),
                'advances':dataset('预发明细','S7',{'record':'id','id':'id','amount':'money'},{'record':'A','id':'B','amount':'C'},'record',self.rid('advance'))}}
        if node=='roster':
            rid=self.rid('direct');return {'roles':{'direct':{'label':'直接归属','rate':1,'rules':[rid],
                'recipients':{'kind':'rows','dataset':'staff','match':{'agreement':key('agreement')},'where':op('eq',fld('role'),'partner'),
                  'pairs':[{'name':'name','id':'employee'}],'missing_share':'single','default_share':1,'missing':'error','duplicates':'error','pending_label':'无','rules':[rid]}}},
                 'rounding':{'when':'none','digits':2,'rules':[rid]},'rules':[rid],'distribution_kind':'direct','person_program_required':True,'parameters':{},'schedules':{},'formula_bindings':{}}
        if node=='adaptive/roster/person_plan':
            def m(label,expr,rule):return {'label':label,'expr':expr,'rules':[self.rid(rule)]}
            metrics={'name':m('姓名',query('$project','unique',fld('name')),'master'),
              'factor':m('个人计奖系数',query('$project','unique',fld('factor')),'master'),
              'order_sum':m('归属合计',query('approved_contributions','sum',fld('raw'),match={'person_id':key('id')},empty='zero'),'master'),
              'due':m('人员应得',op('round',op('mul',ref('order_sum'),ref('factor')),2),'due'),
              'advance':m('预发合计',query('advances','sum',fld('amount'),match={'id':key('id')},empty='zero'),'advance'),
              'applied':m('实际抵扣',op('min',ref('due'),ref('advance')),'applied'),
              'paid':m('本次实发',op('max',op('sub',ref('due'),ref('advance')),0),'paid'),
              'over':m('超预发',op('max',op('sub',ref('advance'),ref('due')),0),'over')}
            forms={self.rid('due'):{'target':'person:due','symbols':{'归属合计':ref('order_sum'),'个人计奖系数':ref('factor')}},
               self.rid('applied'):{'target':'person:applied','symbols':{'人员应得':ref('due'),'预发合计':ref('advance')}},
               self.rid('paid'):{'target':'person:paid','symbols':{'人员应得':ref('due'),'预发合计':ref('advance')}},
               self.rid('over'):{'target':'person:over','symbols':{'人员应得':ref('due'),'预发合计':ref('advance')}}}
            return {'program':{'population':{'dataset':'employees','keys':['id'],'where':True,'rules':[self.rid('master')]},'metrics':metrics,'checks':[],
               'funding':{'total':'due','payable':'paid','alloc':'paid','destinations':{'applied':'实际抵扣'},'labels':{'total':'应得','alloc':'实发'},'summary_digits':2,'rules':[self.rid('paid'),self.rid('applied')]}},
                'identity_key':'id','name_metric':'name','attributed_metric':'order_sum','contribution_amount':'raw','rules':[self.rid('master')],
                'parameters':{},'schedules':{},'formula_bindings':forms}
        if node=='adaptive/export/layout':
            rid=self.rid('output');return {'columns':[{'label':'净销售额','source':ref('sales_net'),'rules':[rid]}],
                'person_columns':[{'label':'员工编号','source':key('id'),'rules':[rid]},{'label':'预发合计','source':ref('advance'),'rules':[rid]},{'label':'个人计奖系数','source':ref('factor'),'rules':[rid]}],
                'groups':[{'label':'合同','by':[key('agreement')],'metrics':['pay'],'rules':[rid]},
                          {'label':'员工','layer':'person','by':[{'source':key('id')}],'metrics':['paid','over'],'rules':[rid]}],
                'person_totals':[{'label':'超预发合计','metric':'over','rules':[rid]}],'sections':{'person':{'rules':[rid]},'project':{'rules':[rid]}},'rules':[],'formula_bindings':{}}
        return super().reply(msg)


def test_formal_person_stage_includes_zero_master_and_prepaid(tmp_path):
    paths=_write_sources(tmp_path/'materials');policy=paths[-1]
    original=policy.read_text(encoding='utf8').splitlines()[:8]
    policy.write_text('\n'.join(original+[
        '每笔项目仅归属合作伙伴登记的员工，按唯一编号直接归属，不分权重。',
        '人员主表全员输出，按人员编号汇总实际归属，读取姓名和个人计奖系数。',
        '人员应得=ROUND(归属合计×个人计奖系数,2)',
        '预发按唯一预发记录及人员编号汇总，多条累加，无记录为零。',
        '实际抵扣=min(人员应得,预发合计)',
        '本次实发=max(人员应得-预发合计,0)',
        '超预发=max(预发合计-人员应得,0)',
        '输出项目底稿：净销售额、agreement；输出人员核算底稿：员工编号、预发合计、个人计奖系数，并汇总各金额列。',
    ]),encoding='utf8')
    employees=policy.parent/'employees.csv';employees.write_text('ID,Name,Factor\nA001,同名甲,0.5\nB001,同名甲,1\nC001,无订单,1\n',encoding='utf8')
    advances=policy.parent/'advances.csv';advances.write_text('Record,ID,Amount\nR1,A001,20\nR2,A001,5\nR3,B001,3\n',encoding='utf8');paths += [employees,advances]
    out=tmp_path/'case';c=PersonalContracts();primary=FakeLLM(c.primary,name='fixture-flash');primary.checker_llm=FakeLLM(c.reviewer,name='fixture-pro')
    scenario=SimpleNamespace(execution=ADAPTIVE_MODE,files=[],rule_store=None)
    def run():return run_agent(primary,paths,out,scenario=scenario,period='2027-01',max_steps=300)
    a=run();assert a.status=='AWAITING_TOTAL_CONFIRMATION',a.error
    cs.confirm_totals(out,SCHEME,rv.receipt_token(rv.state_of(cs.load_case(out))),'测试财务（虚构数据）')
    a=run();assert a.status=='OK',a.error
    details=json.loads((out/'commission_details.json').read_text(encoding='utf8'));s=details['settlement'];people={p['id']:p for p in s['people']}
    assert len(people)==3 and D(people['A001']['amount'])==D('29.4') and D(people['B001']['metrics']['over'])==3 and D(people['C001']['amount'])==0
    assert D(s['funding_bridge']['entitled'])==D('54.4') and D(s['residual'])==0
    wb=load_workbook(out/'result.xlsx',read_only=True,data_only=True)
    try:
        pr=list(wb['人员核算底稿'].values);assert len(pr)==4 and len(set(pr[0]))==len(pr[0])
        groups={(g,json.loads(v)[0],m):D(str(a)) for g,v,m,a in list(wb['分组汇总'].values)[1:]}
        assert groups['员工','A001','paid']==D('29.4') and groups['员工','B001','over']==3 and groups['员工','C001','paid']==0
        assert sum(a for (g,_,m),a in groups.items() if g=='合同' and m=='pay')==D('108.8')
        totals={r[0]:r[1] for r in wb['核算汇总'].values if r[0]}
        assert '项目字段汇总：净销售额' in totals and D(str(totals['人员字段汇总：预发合计']))==28
        assert '人员字段汇总：个人计奖系数' not in totals
    finally:wb.close()


def test_report_group_cannot_mix_or_guess_calculation_layers():
    import pytest
    from bonus_agent.adaptive_export import validate_layout,default_layout
    from bonus_core.engine.adaptive import ProgramError
    basis={'key_fields':['id'],'metric_fields':{'project_amount':'金额','shared':'共同字段'}}
    settlement={'person_basis':{'key_fields':['id'],'metric_fields':{'person_amount':'金额','shared':'共同字段'}}}
    layout=default_layout();layout['groups']=[{'label':'人员','by':[{'source':key('id')}],'metrics':['person_amount'],'rules':[]}]
    assert validate_layout(layout,basis,settlement)['groups'][0]['layer']=='person'
    g=layout['groups'][0];g['metrics']=['project_amount']
    with pytest.raises(ProgramError,match='其它层级'):validate_layout(layout,basis,settlement)
    g.pop('layer');g['metrics']=['shared']
    with pytest.raises(ProgramError,match='未唯一指向'):validate_layout(layout,basis,settlement)


class GroupedContracts(Contracts):
    def __init__(self):super().__init__();self.bad_gross=False;self.extra=[]

    def handle_units(self,data):
        reply=super().handle_units(data)
        for uid,u in data['units'].items():
            if u['text'].startswith('共享角色政策'):
                rid=uid+':1';self.extra.append(rid)
                reply['units'][uid]={'disposition':'operative','reason':'共享角色口径','clauses':[{'quote':u['text'],'meaning':'实际名单份额守恒','phase':'roster','kind':'policy'}]}
        self.extra=list(dict.fromkeys(self.extra));return reply

    def reply(self,msg):
        payload=json.loads(msg[-1]['content']);node=payload['current_node']
        if node=='adaptive/roster/outline':
            return {'groups':[{'id':k,'roles':{k:v['label']},'rules':[self.rid('roles')],'purpose':'独立角色合同'} for k,v in self.roster['roles'].items()],
                'global_rules':[self.rid('round'),*self.extra],'distribution_kind':'roles','person_program_required':False}
        if node.startswith('adaptive/roster/role_group/'):
            role=node.split('/')[-1];payload['current_node']='roster';local=[*msg[:-1],{'role':'user','content':json.dumps(payload,ensure_ascii=False)}]
            whole=super().reply(local);return {'roles':{role:whole['roles'][role]},'parameters':{role+'_rate':whole['parameters'][role+'_rate']},'schedules':{},'formula_bindings':{}}
        if node=='roster':return {'rounding':{'when':'after_sum','digits':2,'rules':[self.rid('round')]},'rules':[self.rid('round'),*self.extra], 'formula_bindings':{}}
        return super().reply(msg)


def test_large_role_rules_are_compiled_as_separate_role_groups(tmp_path):
    paths=_write_sources(tmp_path/'materials');p=paths[-1]
    p.write_text(p.read_text(encoding='utf8')+'\n'+'\n'.join(f'共享角色政策附项{i}：实名按实际名单，未分配资金单列。' for i in range(22)),encoding='utf8')
    c=GroupedContracts();primary=FakeLLM(c.primary,name='fixture-flash');primary.checker_llm=FakeLLM(c.reviewer,name='fixture-pro')
    out=tmp_path/'case';scenario=SimpleNamespace(execution=ADAPTIVE_MODE,files=[],rule_store=None)
    def run():return run_agent(primary,paths,out,scenario=scenario,period='2027-01',max_steps=300)
    a=run();assert a.status=='AWAITING_TOTAL_CONFIRMATION',a.error
    cs.confirm_totals(out,SCHEME,rv.receipt_token(rv.state_of(cs.load_case(out))),'测试财务');a=run();assert a.status=='OK',a.error
    calls=[json.loads(m[-1]['content'])['current_node'] for m in primary.calls]
    assert 'adaptive/roster/role_group/partner' in calls and 'adaptive/roster/role_group/service' in calls


def test_early_inventory_failure_does_not_discard_retained_original_rule_proofs():
    from bonus_agent.commission_chain import unchanged_rule_candidates
    from bonus_agent.recovery import fingerprint
    digest=fingerprint({'file':'policy.docx','where':'p2','text':'实际制度'})
    selected={'disposition':'operative','reason':'原件','clauses':[]}
    record={'file':'policy.docx','sha256':'unchanged','answer_key':fingerprint([]),'proposal':selected,
            'reviewer':'pro','review_receipt':{'status':'PASS','reviewer':'pro','output':fingerprint(selected)},
            'historical_task':'real-rule-review','historical_unit':'R1'}
    failed={'files':{'policy.docx':'unchanged'},'status':'FAILED','unchanged_rule_candidates':{digest:record},'adaptive_rule_unit_proofs':{}}
    assert unchanged_rule_candidates(failed,failed['files'],'pro')=={digest:record}
    assert unchanged_rule_candidates(failed,{'policy.docx':'changed'},'pro')=={}
    assert unchanged_rule_candidates(failed,failed['files'],'other-pro')=={}


def test_source_binding_epoch_is_independent_of_code_but_not_financial_revision():
    from bonus_agent.commission_chain import source_binding_epoch
    a={'files':{'data':'hash'},'source_order':['data'],'period':'2027-01','revision_id':'v1','active_rules':[{'id':'rule1'}],
       'questions':[{'id':'q','answer':'actual answer'}],'profile':'old code'}
    b=deepcopy(a);b['profile']='new code'
    assert source_binding_epoch(a)==source_binding_epoch(b)
    b['revision_id']='v2';assert source_binding_epoch(a)!=source_binding_epoch(b)
    b=deepcopy(a);b['questions'][0]['answer']='new decision';assert source_binding_epoch(a)!=source_binding_epoch(b)


def test_actual_authority_rule_proof_reuse_requires_the_same_financial_version():
    from bonus_agent.commission_chain import unchanged_rule_candidates
    from bonus_agent.recovery import fingerprint
    v={'id':'real-revision','field':'adaptive:r','statement':'本批实际新规则','owner':'scope_mapping'}
    source={'file':'财务正式规则修订','where':v['id'],'text':v['statement']}
    selected={'disposition':'operative','reason':'真实财务原文','clauses':[]}
    uid='Rrev_'+v['id'];candidate={'units':{uid:selected}}
    digest=fingerprint({**source,'authority':v,'phase':v['owner']})
    receipt={'status':'PASS','reviewer':'pro','output':fingerprint(candidate)}
    state={'files':{},'active_rules':[v],'adaptive_rule_sources':{uid:source},'specs':{'rule-task':candidate},
           'adaptive_rule_unit_proofs':{uid:{'task':'rule-task','digest':digest,'receipt':receipt,'proposal':selected,'answer_key':fingerprint([])}}}
    recovered=unchanged_rule_candidates(state,{},'pro')
    assert recovered[digest]['authority_id']==v['id'] and recovered[digest]['sha256']==fingerprint(v)
    stale={'files':{},'active_rules':[dict(v,statement='不同的确认')],'unchanged_rule_candidates':recovered}
    assert unchanged_rule_candidates(stale,{},'pro')=={}
