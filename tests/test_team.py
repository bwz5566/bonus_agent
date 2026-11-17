"""No providers: exercise actual task isolation, commit and budget boundaries."""
import copy
import json
import threading

import pytest

from bonus_agent.agent import ToolError, run_agent
from bonus_agent.case import snapshot, restore
from bonus_agent.llm import FakeLLM, LLMError, Usage
from bonus_agent.team import TeamSession, Meter, BudgetModel
from bonus_agent.tool_specs import TOOLS
from tests.test_staged_workflow import make_workspace

pytestmark = pytest.mark.evidence


def session(tmp_path):
    ws = make_workspace(tmp_path)
    ws.prepare_data()
    ws.plan_formula_tasks([{'id':n, 'scheme':'S', 'target':'/variables/' + n} for n in ('a','b')])
    return ws, TeamSession(ws, 100_000)


def action(team, name='set_rules', target='a'):
    spec = team.ws.workflow['construction']['tasks'][target]
    return {'name':name, 'arguments':'{}', '_owner':'formula:'+target,
            '_spec':spec, '_version':team.version(spec)}


def test_ownership_and_transactional_rollback(tmp_path, monkeypatch):
    ws, team = session(tmp_path)
    call = action(team)
    before = copy.deepcopy(ws.drafts)
    with pytest.raises(ToolError, match='permission'):
        team.execute(call, {'scheme':'S','variables':{'b':{'expr':'amount'}}})
    assert ws.drafts == before
    original = type(ws).set_rules
    def broken(self, **args):
        original(self, **args)
        raise ToolError('injected after mutation')
    monkeypatch.setattr(type(ws), 'set_rules', broken)
    with pytest.raises(ToolError, match='injected'):
        team.execute(call, {'scheme':'S','variables':{'a':{'scope':'项目','expr':'amount'}}})
    assert ws.drafts == before


def test_single_writer_preserves_unrelated_updates_and_rejects_stale(tmp_path):
    ws, team = session(tmp_path)
    a, b = action(team), action(team, target='b')
    team.execute(a, {'scheme':'S','variables':{'a':{'scope':'项目','expr':'amount'}}})
    team.execute(b, {'scheme':'S','variables':{'b':{'scope':'项目','expr':'amount'}}})
    assert {'a','b'} <= ws.drafts['S']['variables'].keys()
    with pytest.raises(ToolError, match='stale'):
        team.execute(a, {'scheme':'S','variables':{'a':{'scope':'项目','expr':'0'}}})


def test_binding_cannot_remove_other_targets(tmp_path):
    ws, team = session(tmp_path)
    fact = ws.facts[0]
    with pytest.raises(ToolError, match='permission'):
        team.execute(action(team, name='bind_facts'), {'bindings':[{'name':fact.name,'targets':['S::/variables/a/expr']}]})


def test_parallel_proposals_have_private_histories_and_real_tools(tmp_path, monkeypatch):
    ws, team = session(tmp_path)
    barrier = threading.Barrier(2)
    packets=[]
    def propose(self, messages, tools, **kw):
        packet=json.loads(messages[1]['content']); packets.append(packet)
        barrier.wait(timeout=10)
        target=packet['owned_target']['id']
        return {'content':'','tool_calls':[{'id':target,'name':'set_rules',
                'arguments':json.dumps({'scheme':'S','variables':{target:{'scope':'项目','expr':'amount'}}})}]}
    monkeypatch.setattr(FakeLLM, 'chat_tools', propose)
    reply=team.propose('system','task',TOOLS)
    assert len(reply['tool_calls'])==2
    for call in reply['tool_calls']:
        team.execute(call,json.loads(call['arguments']))
        team.observe(call,'saved',True)
    assert {p['role'] for p in packets}=={'formula:a','formula:b'}
    assert all('check_formula_task' not in h[0]['content'] for h in team.state['histories'].values())
    assert len({id(h) for h in team.state['histories'].values()})==2
    assert {'a','b'} <= ws.drafts['S']['variables'].keys()


def test_meter_accounts_auxiliaries_and_survives_reconstruction(monkeypatch):
    llm=FakeLLM([]); llm.think_llm=None
    state={}; usage=Usage(); meter=Meter(state,usage,100_000)
    def chat(self, messages, **kwargs):
        self.usage += Usage(prompt_tokens=101, completion_tokens=17, calls=1)
        return '{}'
    monkeypatch.setattr(FakeLLM,'chat',chat)
    wrapped=BudgetModel(llm,meter)
    assert copy.copy(wrapped).chat_json([{'role':'user','content':'x'}])=={}
    assert usage.prompt_tokens==101 and state['tokens']==118
    recreated=Meter(json.loads(json.dumps(state)),usage,100)
    with pytest.raises(LLMError,match='budget'):
        BudgetModel(llm,recreated).chat([])


def test_budget_reservation_and_stop_do_not_call_provider(monkeypatch):
    llm=FakeLLM([])
    monkeypatch.setattr(FakeLLM,'chat',lambda *a,**k:pytest.fail('must not call'))
    with pytest.raises(LLMError,match='budget'):
        BudgetModel(llm,Meter({},Usage(),1)).chat([])
    with pytest.raises(LLMError,match='stopped'):
        BudgetModel(llm,Meter({},Usage(),100_000,lambda:True)).chat([])


def test_failed_partial_batch_is_not_committed(tmp_path,monkeypatch):
    ws,team=session(tmp_path)
    call=action(team, name='note');call['_spec']=None;call['_owner']='coordinator';call['_version']=team.version()
    before=copy.deepcopy(ws.notes)
    def partial(self, **args):
        self.notes['bad']='partial'
        return {'status':'PARTIAL'}
    monkeypatch.setattr(type(ws),'note',partial)
    assert team.execute(call,{'topic':'x','text':'x'})['status']=='PARTIAL'
    assert ws.notes==before


def test_role_permissions_are_enforced_at_execution(tmp_path):
    ws,team=session(tmp_path)
    call=action(team);call['_spec']=None;call['_owner']='rules';call['_version']=team.version()
    with pytest.raises(ToolError,match='permission'):
        team.execute(call,{'scheme':'S','pool':{'scope':'项目','expr':'amount'}})


def test_mode_cannot_silently_change_on_resume(tmp_path):
    ws=make_workspace(tmp_path);out=tmp_path/'out';out.mkdir()
    from bonus_agent.case import save_case
    save_case(out,{'workspace':snapshot(ws),'rounds':[]})
    llm=FakeLLM([]);llm.workflow_mode='staged'
    with pytest.raises(ToolError,match='执行模式'):
        run_agent(llm,ws.inv.files,out,execution_mode='multi_agent')


def test_public_loop_dispatch_and_checkpoint(tmp_path,monkeypatch):
    ws,team=session(tmp_path)
    ws.workflow['execution_mode']='multi_agent'
    out=tmp_path/'out';out.mkdir()
    from bonus_agent.case import save_case,load_case
    save_case(out,{'workspace':snapshot(ws),'rounds':[]})
    def propose(self,messages,tools,**kwargs):
        packet=json.loads(messages[1]['content']);target=packet['owned_target']['id']
        return {'content':'','tool_calls':[{'id':target,'name':'set_rules','arguments':json.dumps({
            'scheme':'S','variables':{target:{'scope':'项目','expr':'amount'}}})}]}
    monkeypatch.setattr(FakeLLM,'chat_tools',propose)
    llm=FakeLLM([]);llm.workflow_mode='staged'
    ar=run_agent(llm,ws.inv.files,out,execution_mode='multi_agent',max_steps=1)
    assert {'a','b'} <= ar.ws.drafts['S']['variables'].keys()
    assert all(s.agent.startswith('formula:') for s in ar.steps if s.tool=='set_rules')
    saved=load_case(out)
    assert saved['rounds'][-1]['execution_mode']=='multi_agent'
    assert len(saved['workspace']['workflow']['team']['histories'])==2


def test_comparison_never_infers_business_acceptance(tmp_path):
    from bonus_agent.team_report import compare_runs
    obj={'rounds':[{'files':{'a':'hash'},'model':'flash','limits':{'max_tokens':100},
                    'financial_digest':{'amount':'1'},'usage':{'calls':1}}]}
    a=tmp_path/'a.json';b=tmp_path/'b.json'
    a.write_text(json.dumps(obj));b.write_text(json.dumps(obj))
    report=compare_runs(a,b)
    assert report['comparable'] and report['financial_outputs_equal']
    assert report['business_acceptance']=='UNVERIFIED'
    obj['rounds'][0]['files']['a']='changed';b.write_text(json.dumps(obj))
    assert not compare_runs(a,b)['comparable']


def test_failed_program_check_keeps_diagnostics_without_issuing_pass(tmp_path):
    ws,team=session(tmp_path)
    call=action(team,name='check_formula_task')
    value=team.execute(call,{'task':'a'})
    assert value['status']=='FAIL'
    assert ws.workflow['construction']['reports']['a']['issues']
    assert 'a' not in ws.workflow['construction']['receipts']


def test_helper_usage_not_overwritten_by_private_workspace(tmp_path,monkeypatch):
    ws,team=session(tmp_path)
    call=action(team,name='note');call['_spec']=None;call['_owner']='coordinator';call['_version']=team.version()
    def helper(self,**args):
        team.meter.state['tokens']+=123
        return 'ok'
    monkeypatch.setattr(type(ws),'note',helper)
    team.execute(call,{'topic':'x','text':'x'})
    assert team.state['meter']['tokens']==123


def test_repeated_failure_remains_blocked_after_resume(tmp_path):
    ws,team=session(tmp_path)
    call=action(team)
    team.state['tasks']['formula:a']={'attempts':{},'status':'READY','version':call['_version']}
    team.state['histories']['formula:a']=[]
    team.observe(call,'same failure',False);team.observe(call,'same failure',False)
    assert team.state['tasks']['formula:a']['status']=='BLOCKED'
    snap=copy.deepcopy(snapshot(ws))
    from bonus_agent.agent import Workspace
    clone=Workspace(FakeLLM([]),ws.inv);restore(clone,snap)
    restored=TeamSession(clone,100_000)
    with pytest.raises(LLMError,match='blocked'):
        restored.propose('system','task',TOOLS)


def test_changed_public_policy_rejects_outstanding_formula(tmp_path):
    ws,team=session(tmp_path)
    call=action(team)
    ws.said='财务补充了新的本案口径'
    with pytest.raises(ToolError,match='stale'):
        team.execute(call,{'scheme':'S','variables':{'a':{'scope':'项目','expr':'amount'}}})


def test_program_stage_obeys_same_stop_boundary(tmp_path,monkeypatch):
    from tests.test_staged_workflow import prepared
    ws=prepared(make_workspace(tmp_path));team=TeamSession(ws,100_000)
    monkeypatch.setattr(FakeLLM,'chat_tools',lambda *a,**k:pytest.fail('program stage needs no planning call'))
    reply=team.propose('system','task',TOOLS)
    assert reply['tool_calls'][0]['name']=='calculate_basis'
    team.state['tasks']['basis'].update(status='BLOCKED',problem='same failure')
    with pytest.raises(LLMError,match='blocked'):
        team.propose('system','task',TOOLS)
