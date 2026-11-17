"""Exercise constructive progress, dependency invalidation and source semantics, without providers."""
import copy
import json
from decimal import Decimal

import pytest

from bonus_agent import case, facts as fx
from bonus_agent.agent import Workspace, Step, ToolError, run_agent
from bonus_agent.auto import build_inventory
from bonus_agent.construction import TaskBoard
from bonus_agent.context import task_card
from bonus_agent.llm import FakeLLM
from bonus_agent.progress import ProgressWatch, discover
from bonus_agent.semantics import semantic_gates
from tests.test_staged_workflow import make_workspace, prepared

pytestmark = pytest.mark.evidence


def tasks():
    return [{'id':'base','scheme':'S','target':'/variables/base','facts':['提成率'],'clauses':['C1']},
            {'id':'pool','scheme':'S','target':'/pool','depends_on':['base']},
            {'id':'allocation','scheme':'S','target':'/allocation','depends_on':['pool']},
            {'id':'report','scheme':'S','target':'/report','depends_on':['pool']}]


def test_construct_validate_and_invalidate_dependent_tasks(tmp_path):
    ws=make_workspace(tmp_path)
    ws.prepare_data()
    board=TaskBoard(ws)
    ws.plan_formula_tasks(tasks())
    assert 'base' in ws._flow().hint() and 'set_rules' in ws._flow().hint()
    assert ws.check_formula_task('pool')['status']=='FAIL'
    assert ws.compile_formulas()['status']=='FAIL'
    f=ws.facts[0]
    ws.set_rules(scheme='S', variables={'base':{'scope':'项目','expr':'amount * 0.1'}},
                 bindings=[{'name':f.name,'targets':f.targets+['S::/variables/base/expr']}])
    assert ws.check_formula_task('base')['status']=='PASS'
    ws.set_rules(scheme='S',pool={'scope':'项目','expr':'base'},
                 bindings=[{'name':f.name,'targets':[t for t in ws.facts[0].targets if t != 'S::/pool/expr']}])
    for n in ('pool','allocation','report'):
        assert ws.check_formula_task(n)['status']=='PASS'
    assert ws.compile_formulas()['status']=='PASS'
    assert board.summary()['active'] is None
    ws.set_rules(scheme='S',variables={'base':{'scope':'项目','expr':'amount * 0.2'}})
    status=board.statuses()
    assert status['base']['status']=='PENDING' and status['pool']['status']=='WAITING'
    assert not ws._flow().current('formulas')
    assert ws.check_formula_task('base')['status']=='FAIL'
    assert not ws.llm.calls


def test_plan_cycle_and_runtime_cycle_never_approve_or_break_reading(tmp_path):
    ws=make_workspace(tmp_path); original=copy.deepcopy(ws.drafts)
    with pytest.raises(ToolError,match='construction.cycle'):
        ws.plan_formula_tasks([{'id':'a','scheme':'S','target':'/variables/a','depends_on':['b']},
                               {'id':'b','scheme':'S','target':'/variables/b','depends_on':['a']}])
    assert ws.drafts==original
    ws.plan_formula_tasks([{'id':'a','scheme':'S','target':'/variables/a'}, {'id':'b','scheme':'S','target':'/variables/b'}])
    ws.set_rules(scheme='S',variables={'a':{'expr':'b'},'b':{'expr':'a'}})
    assert ws.check_formula_task('a')['status']=='FAIL'
    assert ws.read_workspace('construction')['items']
    assert task_card(ws)


def test_root_task_clauses_have_real_content_and_changed_content_is_visible(tmp_path):
    ws=make_workspace(tmp_path)
    ws.record_clauses([{'file':'policy.md','point':'名单分配','quote':'按名单比例分配'}])
    ws.prepare_data()
    # Adopt the fixture's complete rules through a task for each existing component.
    plan=[p for p in tasks() if p['id']!='base'];plan[0]['depends_on']=[];plan[0]['clauses']=['C1']
    plan[1]['clauses']=['C2']
    ws.plan_formula_tasks(plan,replace=True)
    for name in ('pool','allocation','report'):
        assert ws.check_formula_task(name)['status']=='PASS'
    clause=next(c for c in ws.clauses if c.id=='C2')
    assert clause.where=='allocation' and clause.checked
    before=clause.checked
    ws.drafts['S']['allocation'][1]['weight']='1'
    assert ws._clause_sig(clause,ws._element_texts())!=before


def test_future_obligations_do_not_mask_current_task_progress(tmp_path):
    ws=make_workspace(tmp_path);ws.prepare_data();ws.plan_formula_tasks(tasks())
    assert ws.blocker_frontier()=={'construction:base:/variables/base'}
    seen=set(); watch=ProgressWatch()
    for n in range(18):
        step=Step('read_workspace', {'section':'clauses','offset':n},
                  json.dumps({'section':'clauses','items':[{'id':f'C{n}','point':'原文'}]}), True)
        assert discover(step,seen)
        assert watch.observe(ws.blocker_frontier(), discovery=True)==0
    for n in range(12):
        same=Step('read_workspace', {'section':'clauses','query':f'不同查询{n}'},
                  json.dumps({'section':'clauses','items':[{'id':'C1','point':'原文'}]}),True)
        assert not discover(same,seen)
        count=watch.observe(ws.blocker_frontier())
    assert count==12


def test_finite_discovery_and_task_receipts_survive_resume(tmp_path):
    ws=prepared(make_workspace(tmp_path)); before=TaskBoard(ws).statuses()
    clone=Workspace(FakeLLM([]),ws.inv);case.restore(clone,case.snapshot(ws))
    assert TaskBoard(clone).statuses()==before
    seen=set(); step=Step('read_workspace',{},json.dumps({'section':'facts','items':[{'name':'rate'}]}),True)
    assert discover(step,seen)
    restored=set(json.loads(json.dumps(sorted(seen))))
    assert not discover(step,restored)
    spec=copy.deepcopy(TaskBoard(clone).state['tasks'])
    first=next(iter(spec.values())); first['title']='更新来源规划'
    clone.plan_formula_tasks([first])
    assert not clone._flow().current('formulas')


def test_scope_variants_do_not_merge_identifiers_or_hide_wrong_pages(tmp_path):
    assert fx.scope_norm('PI 事業部 銷售執行人')==fx.scope_norm('PI事业部销售执行人')
    assert fx.norm('事業部客户001')!=fx.norm('事业部客户001')
    from pptx import Presentation
    from pptx.util import Inches
    prs=Presentation()
    for caption in ('PI 事業部銷售提成總包分成比例','其他事业部销售执行人比例'):
        slide=prs.slides.add_slide(prs.slide_layouts[6])
        t=slide.shapes.add_table(2,2,Inches(0),Inches(0),Inches(6),Inches(2)).table
        t.cell(0,1).text='新项目';t.cell(1,0).text='销售執行人';t.cell(1,1).text='35%'
        slide.shapes.add_textbox(Inches(0),Inches(3),Inches(6),Inches(1)).text=caption
    path=tmp_path/'policy.pptx';prs.save(path)
    ws=Workspace(FakeLLM([]),build_inventory([path]))
    spec={'name':'PI事业部销售执行人比例','value':'35%','file':path.name,'page':1,
          'source_cell':{'table':1,'row':2,'column':2},'quote':'销售執行人\n新项目\n35%'}
    ws.record_facts([spec])
    assert len(ws.facts)==1
    with pytest.raises(ToolError,match='其他事业部'):
        ws.record_facts([{**spec,'name':'其他事业部销售执行人比例'}])


def mode_workspace(tmp_path):
    p=tmp_path/'policy.md';p.write_text('按超额累进计算提成。低档比例10%，高档比例20%，档位100元。',encoding='utf8')
    ws=Workspace(FakeLLM([]),build_inventory([p]))
    ws.record_facts([{'name':'low','value':'10%','file':p.name,'quote':'低档比例10%'},
                     {'name':'high','value':'20%','file':p.name,'quote':'高档比例20%'},
                     {'name':'band','value':'100元','file':p.name,'quote':'档位100元'}])
    ws.record_clauses([{'file':p.name,'point':'必须超额累进','quote':'按超额累进计算提成'}])
    return ws,{'file':p.name,'quote':'按超额累进计算提成'}


def test_explicit_algorithm_cannot_compile_as_whole_and_gate_follows_dependencies(tmp_path):
    ws,basis=mode_workspace(tmp_path)
    rows=[{'value':'low'},{'lower':'band','value':'high'}]
    with pytest.raises(ToolError,match='algorithm.mode'):
        ws.compile_table('rate',rows,scheme='S',mode='whole',algorithm_basis=basis)
    assert not ws.drafts
    ws.compile_table('rate',rows,scheme='S',algorithm_basis=basis)
    assert ws.drafts['S']['tables']['rate']['mode']=='excess'
    ws.set_rules(scheme='S', variables={'bonus':{'expr':'tier(rate, 150)'}}, pool={'expr':'bonus'})
    ws.cover_clauses({'C1':'pool'})
    assert not semantic_gates(ws)
    ws.drafts['S']['tables']['rate']['mode']='whole'
    assert any('algorithm.mode' in e for e in semantic_gates(ws))
    # Same source rates produce different money; the mode guard has a financial consequence.
    from bonus_core.engine.evaluator import tier_amount
    from bonus_core.dsl.models import TierTable
    assert tier_amount(TierTable(mode='excess',steps=[(0,'.1'),(100,'.2')]),Decimal(150))==20
    assert tier_amount(TierTable(mode='whole',steps=[(0,'.1'),(100,'.2')]),Decimal(150))==30


def test_explicit_negative_policy_is_checked_without_changing_the_default(tmp_path):
    ws,basis=mode_workspace(tmp_path)
    p=tmp_path/'negative.md';p.write_text('负数按阶梯照算，保留负奖金。',encoding='utf8')
    new=Workspace(FakeLLM([]),build_inventory([*ws.inv.files,p]))
    new.record_clauses([{'file':p.name,'point':'保留负值','quote':'负数按阶梯照算，保留负奖金'}])
    new.set_rules(scheme='S',tables={'rate':{'kind':'tier','mode':'excess','steps':[[0,'.1']]}},pool={'expr':'tier(rate, -10)'})
    new.cover_clauses({'C1':'pool'})
    assert any('algorithm.negative' in x for x in semantic_gates(new))
    new.drafts['S']['tables']['rate']['below_first']='first'
    assert not semantic_gates(new)
    from bonus_core.dsl.models import TierTable
    assert TierTable(mode='excess',steps=[(0,'.1')]).below_first=='zero'


def test_backtracking_keeps_actual_tool_history(tmp_path):
    ws=prepared(make_workspace(tmp_path));out=tmp_path/'out'
    case.save_case(out,{'rounds':[{'n':1,'at':'2026-11-17','files':case.fingerprint(ws.inv.files),'status':'FAILED'}],
                        'workspace':case.snapshot(ws)})
    fake=FakeLLM([{'tool':'read_workspace','args':{'section':'mapping'}},
        {'tool':'set_rules','args':{'scheme':'S','report':{'people':'unknown_person','totals':[]}}},
        {'tool':'note','args':{'topic':'end','text':'stop'}}])
    fake.workflow_mode='staged'
    ar=run_agent(fake,ws.inv.files,out,max_steps=3,reflect=0)
    assert any(s.tool=='（恢复当前任务）' for s in ar.steps)
    final_messages=fake.calls[-1]
    assert any(tc['function']['name']=='read_workspace' for m in final_messages
               for tc in m.get('tool_calls',[]))
