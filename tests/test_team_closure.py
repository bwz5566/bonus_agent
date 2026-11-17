"""Cross-role recovery and independent obligations using synthetic, source-bound data."""
import copy
import json

import pytest

from bonus_agent.agent import ToolError, Workspace
from bonus_agent.case import snapshot, restore
from bonus_agent.construction import TaskBoard
from bonus_agent.llm import FakeLLM
from bonus_agent.team import TeamSession
from bonus_agent.team_support import request_repair, finish_repair, coverage
from tests.test_staged_workflow import make_workspace, prepared

pytestmark=pytest.mark.evidence


def call(team,tool,owner='formula:a',spec=None):
    team.state['tasks'].setdefault(owner,{'attempts':{},'status':'READY'})
    team.state['histories'].setdefault(owner,[])
    return {'name':tool,'arguments':'{}','_owner':owner,'_spec':spec,'_version':team.version(spec)}


def test_missing_binding_returns_to_rules_then_original_formula(tmp_path):
    ws=make_workspace(tmp_path);ws.prepare_data()
    ws.plan_formula_tasks([{'id':'a','scheme':'S','target':'/variables/a'}])
    ws.set_rules(scheme='S',variables={'a':{'scope':'项目','expr':'amount * 0.1'}})
    team=TeamSession(ws,100_000);spec=ws.workflow['construction']['tasks']['a']
    check=call(team,'check_formula_task',spec=spec)
    result=team.execute(check,{'task':'a'})
    assert result['status']=='FAIL'
    team.observe(check,json.dumps(result,ensure_ascii=False),False)
    assert check['_routed'] and not team.state['tasks']['formula:a']['attempts']
    owner,_=team.owners()[0];assert owner.startswith('repair:')
    rid=owner.split(':',1)[1]
    fact=ws.facts[0]
    bind=call(team,'bind_facts',owner)
    team.execute(bind,{'bindings':[{'name':fact.name,'targets':fact.targets+['S::/variables/a/expr']}]})
    result=team.execute(call(team,'finish_module_repair',owner),{'repair':rid})
    assert result['status']=='REPAIR_RETURNED'
    assert team.state['repairs'][rid]['status']=='RETURNED'
    assert team.execute(call(team,'check_formula_task',spec=spec),{'task':'a'})['status']=='PASS'
    assert not ws.pending and not ws.llm.usage.calls


def test_repair_cannot_return_on_notes_or_target_another_request(tmp_path):
    ws=make_workspace(tmp_path);team=TeamSession(ws,100_000)
    rec=request_repair(ws,'rules','缺少原文依据')['repair'];owner='repair:'+rec['id']
    ws.note('反思','我已完成修复')
    with pytest.raises(ToolError,match='no_change'):finish_repair(ws,rec['id'])
    with pytest.raises(ToolError,match='permission'):
        team.execute(call(team,'finish_module_repair',owner),{'repair':'another'})
    saved=copy.deepcopy(snapshot(ws));clone=Workspace(FakeLLM([]),ws.inv);restore(clone,saved)
    assert TeamSession(clone,100_000).owners()[0][0]==owner


def test_data_repair_still_cannot_change_bonus_formula(tmp_path):
    ws=make_workspace(tmp_path);team=TeamSession(ws,100_000)
    rec=request_repair(ws,'data','关联错误')['repair'];owner='repair:'+rec['id']
    with pytest.raises(ToolError,match='permission'):
        team.execute(call(team,'set_rules',owner),{'scheme':'S','pool':{'scope':'项目','expr':'0'}})


def test_component_patch_preserves_siblings_and_rejects_escape(tmp_path):
    ws=make_workspace(tmp_path);ws.prepare_data()
    ws.plan_formula_tasks([{'id':'a','scheme':'S','target':'/allocation'}])
    team=TeamSession(ws,100_000);spec=ws.workflow['construction']['tasks']['a']
    original=copy.deepcopy(ws.drafts['S'])
    result=team.execute(call(team,'patch_rule_component',spec=spec),
                        {'scheme':'S','path':'/allocation/1/weight','value':'1'})
    assert result['status']=='DRAFT'
    assert ws.drafts['S']['allocation'][0]==original['allocation'][0]
    assert ws.drafts['S']['pool']==original['pool'] and ws.drafts['S']['report']==original['report']
    before=copy.deepcopy(ws.drafts)
    with pytest.raises(ToolError,match='permission'):
        team.execute(call(team,'patch_rule_component',spec=spec),{'scheme':'S','path':'/pool/expr','value':'0'})
    assert ws.drafts==before


def test_stale_candidates_reschedule_without_repair_failure(tmp_path):
    ws=make_workspace(tmp_path);ws.prepare_data();ws.plan_formula_tasks([{'id':'a','scheme':'S','target':'/pool'}])
    team=TeamSession(ws,100_000);spec=ws.workflow['construction']['tasks']['a']
    old=call(team,'set_rules',spec=spec);before=copy.deepcopy(ws.drafts)
    ws.said='新增财务确认'
    result=team.execute(old,{'scheme':'S','pool':{'scope':'项目','expr':'0'}})
    team.observe(old,json.dumps(result),True)
    assert old['_deferred'] and team.state['tasks']['formula:a']['status']=='STALE'
    assert not team.state['tasks']['formula:a']['attempts'] and ws.drafts==before


def reference():
    return {'pool':'amount * 0.1','helpers':{},'tables':{},'details':{'项目奖':'amount * 0.1'},
            'totals':{'项目奖合计':'sum(amount, of: "projects") * 0.1'},'unclear':[],
            'data_contracts':[{'dataset':name,'key':key,'link':None,'period':None,'window':'period',
                'population_from':None,'file':'policy.md','quote':'每个项目按销售金额的10%计算奖金'}
                for name,key in [('projects','pid'),('people','rid')]],
            'sources_reviewed':['policy.md'],'requirements':[{'file':'policy.md',
                'quote':'每个项目按销售金额的10%计算奖金','point':'按项目销售额计算提成','targets':['pool']}]}


def test_coverage_requires_real_sources_and_actual_formula_targets(tmp_path):
    ws=make_workspace(tmp_path);spec=reference()
    assert coverage(ws,spec)==[]
    spec['requirements'][0]['targets']=['helpers.invented']
    assert any('coverage.evidence' in x for x in coverage(ws,spec))
    spec=reference();spec['requirements'][0]['quote']='文件里没有这条规则'
    assert coverage(ws,spec)
    assert coverage(ws,{'pool':'amount * 0.1'})


def test_missing_obligations_never_approve_matching_totals_and_can_be_corrected(tmp_path):
    ws=prepared(make_workspace(tmp_path));ws.workflow['execution_mode']='multi_agent'
    ws.calculate_basis('S')
    incomplete=reference();incomplete.pop('requirements')
    ws.llm.checker_llm=FakeLLM([json.dumps(incomplete,ensure_ascii=False),json.dumps(reference(),ensure_ascii=False)])
    first=ws.review_basis('S')
    assert first['status']=='FAIL' and any(x.startswith('coverage.') for x in first['issues'])
    assert not ws._flow().current('review:S')
    second=ws.review_basis('S')
    assert second['status']=='PASS' and not second['cached']
    assert ws.workflow['team']['requirements']['S']['items']


def test_coverage_does_not_mask_wrong_independent_amounts(tmp_path):
    ws=prepared(make_workspace(tmp_path));ws.workflow['execution_mode']='multi_agent';ws.calculate_basis('S')
    spec=reference();spec['pool']='amount * 0.2'
    ws.llm.checker_llm=FakeLLM([json.dumps(spec,ensure_ascii=False)])
    report=ws.review_basis('S')
    assert report['status']=='FAIL' and '逐对象奖金池或对象集合不一致' in report['issues']
    assert not ws._flow().current('review:S')


def test_pending_repair_preempts_program_stage_and_nested_cycle_is_rejected(tmp_path,monkeypatch):
    ws=prepared(make_workspace(tmp_path));team=TeamSession(ws,100_000)
    root=request_repair(ws,'rules','缺证据')['repair'];owner='repair:'+root['id']
    def respond(self,messages,tools,**kwargs):
        assert json.loads(messages[1]['content'])['role']==owner
        return {'content':'','tool_calls':[{'name':'note','arguments':json.dumps({'topic':'定位','text':'核对原文'})}]}
    monkeypatch.setattr(FakeLLM,'chat_tools',respond)
    reply=team.propose('system','task',__import__('bonus_agent.tool_specs',fromlist=['TOOLS']).TOOLS)
    assert reply['tool_calls'][0]['name']=='note'
    child=team.execute(call(team,'request_module_repair',owner),{'kind':'data','reason':'还需修正映射'})['repair']
    child_owner='repair:'+child['id']
    assert team.owners()[0][0]==child_owner and child['parent']==root['id']
    with pytest.raises(ToolError,match='cycle'):
        team.execute(call(team,'request_module_repair',child_owner),{'kind':'rules','reason':'再次退回'})


def test_unrelated_fact_does_not_discard_candidate_but_global_gate_still_blocks(tmp_path):
    ws=make_workspace(tmp_path);ws.prepare_data()
    ws.plan_formula_tasks([{'id':'a','scheme':'S','target':'/variables/a'}])
    team=TeamSession(ws,100_000);spec=ws.workflow['construction']['tasks']['a']
    old=call(team,'set_rules',spec=spec)
    ws.record_facts([{'name':'另一模块比例','file':'policy.md','value':'10%','quote':'销售金额的10%'}])
    assert team.version(spec)==old['_version']
    team.execute(old,{'scheme':'S','variables':{'a':{'scope':'项目','expr':'amount'}}})
    assert any('另一模块比例' in x for x in ws._binding_gaps())
    assert ws.compile_formulas()['status']=='FAIL'


def test_wrong_data_window_blocks_even_when_all_amounts_match(tmp_path):
    ws=prepared(make_workspace(tmp_path));ws.workflow['execution_mode']='multi_agent';ws.calculate_basis('S')
    spec=reference();spec['data_contracts'][0]['window']='year_to_date'
    ws.llm.checker_llm=FakeLLM([json.dumps(spec,ensure_ascii=False)])
    report=ws.review_basis('S')
    assert report['status']=='FAIL' and any('coverage.grain' in x for x in report['issues'])
    assert not ws._flow().current('review:S')


def test_personnel_rules_are_deferred_and_final_finish_requires_current_full_review(tmp_path):
    ws=make_workspace(tmp_path)
    ws.record_clauses([{'file':'policy.md','quote':'按名单比例分配','point':'人员名单比例'}])
    spec=reference();spec['requirements'].append({'file':'policy.md','quote':'按名单比例分配',
                                               'point':'人员名单比例','targets':['allocation']})
    assert not coverage(ws,spec)
    team=TeamSession(ws,100_000)
    team.state['requirements']={'S':{'deferred':[spec['requirements'][-1]]}}
    with pytest.raises(ToolError,match='coverage.final'):
        team.execute(call(team,'finish','output'),{'summary':'完成'})


def test_public_run_handoff_repair_return_recheck_without_financial_question(tmp_path,monkeypatch):
    from bonus_agent.agent import run_agent
    from bonus_agent.case import save_case
    ws=make_workspace(tmp_path);ws.prepare_data()
    ws.plan_formula_tasks([{'id':'a','scheme':'S','target':'/variables/a'}])
    ws.set_rules(scheme='S',variables={'a':{'scope':'项目','expr':'amount * 0.1'}})
    ws.workflow['execution_mode']='multi_agent'
    out=tmp_path/'out';out.mkdir();save_case(out,{'workspace':snapshot(ws),'rounds':[]})
    visited=[]
    def response(self,messages,tools,**kwargs):
        packet=json.loads(messages[1]['content']);owner=packet['role'];visited.append(owner)
        if owner.startswith('repair:'):
            calls=[{'name':'bind_facts','arguments':json.dumps({'bindings':[{'name':ws.facts[0].name,
                    'targets':ws.facts[0].targets+['S::/variables/a/expr']}]},ensure_ascii=False)},
                   {'name':'finish_module_repair','arguments':json.dumps({'repair':packet['repair']['id']})}]
        else:
            calls=[{'name':'check_formula_task','arguments':json.dumps({'task':packet['owned_target']['id']})}]
        return {'content':'','tool_calls':calls}
    monkeypatch.setattr(FakeLLM,'chat_tools',response)
    llm=FakeLLM([]);llm.workflow_mode='staged'
    result=run_agent(llm,ws.inv.files,out,execution_mode='multi_agent',max_steps=3)
    relevant=[owner for owner in visited if owner=='formula:a' or owner.startswith('repair:')]
    assert relevant[0]=='formula:a' and relevant[1].startswith('repair:') and relevant[2]=='formula:a'
    assert TaskBoard(result.ws).statuses()['a']['status']=='PASS'
    assert not result.ws.pending and not result.results
    assert all(r['status']=='RETURNED' for r in result.ws.workflow['team']['repairs'].values())


def test_grain_contract_detects_window_mismatch_even_when_amounts_match(tmp_path):
    ws=prepared(make_workspace(tmp_path));ws.workflow['execution_mode']='multi_agent';ws.calculate_basis('S')
    spec=reference();spec['data_contracts'][0]['window']='year_to_date'
    ws.llm.checker_llm=FakeLLM([json.dumps(spec,ensure_ascii=False)])
    report=ws.review_basis('S')
    assert report['status']=='FAIL' and any('projects.window' in x for x in report['issues'])


def test_empty_data_stage_bootstraps_raw_headers_not_empty_mapping(tmp_path,monkeypatch):
    from bonus_agent.tool_specs import TOOLS
    ws=make_workspace(tmp_path);ws.datasets={};ws.mapping={}
    team=TeamSession(ws,100_000)
    assert ws._flow().stage()[0]==2 and 'define_dataset' in ws._flow().hint()
    monkeypatch.setattr(FakeLLM,'chat_tools',lambda *a,**k:pytest.fail('first manifest is a program action'))
    reply=team.propose('system','task',TOOLS)
    c=reply['tool_calls'][0]
    assert c['name']=='list_files'
    text=team.execute(c,{})
    assert 'projects' in text and 'people' in text


def test_optional_advisor_truncation_keeps_original_blocker(tmp_path,monkeypatch):
    from bonus_agent.tool_specs import TOOLS
    from bonus_agent.llm import LLMError
    ws=make_workspace(tmp_path);team=TeamSession(ws,100_000)
    monkeypatch.setattr(team,'owners',lambda:[('data',None)])
    team.primary.think_llm=FakeLLM([])
    team.state['tasks']['data']={'version':team.version(),'status':'BLOCKED','attempts':{},
                                'problem':'重复读取空mapping','diagnosis_pending':True}
    import bonus_agent.recovery as recovery
    def truncated(*a,**k):raise LLMError('finish_reason=length')
    monkeypatch.setattr(recovery,'local_advice',truncated)
    with pytest.raises(LLMError,match='重复读取空mapping'):
        team.propose('system','task',TOOLS)
    assert 'length' in team.state['tasks']['data']['diagnosis_error']


def test_missing_structure_stays_in_data_role_for_repair(tmp_path):
    ws=make_workspace(tmp_path);ws.workflow['execution_mode']='multi_agent'
    ws.drafts['S'].pop('allocation')
    report=ws.prepare_data()
    assert report['status']=='FAIL' and any('structure.required' in x for x in report['issues'])
    team=TeamSession(ws,100_000)
    assert team.owners()[0][0]=='data' and 'set_rules' in ws._flow().hint()
