"""Generalized regressions for shared units, atomic evidence, real progress and money flow."""
import copy
from decimal import Decimal as D

import pytest

from bonus_agent.agent import ToolError, Workspace
from bonus_agent.auto import build_inventory
from bonus_agent.facts import Fact, check_fact, interval, norm, numeric
from bonus_agent.llm import FakeLLM, LLM, load_config, DEFAULT_CONFIG
from bonus_agent.progress import ProgressWatch

pytestmark = [pytest.mark.evidence, pytest.mark.verify, pytest.mark.reread]


def workspace(tmp_path):
    p = tmp_path / 'policy.md'
    p.write_text('A组\n金额范围：200-750万元，比例3%。\n角色分配比例(%)：负责人15。', encoding='utf8')
    return Workspace(FakeLLM([]), build_inventory([p]))


@pytest.mark.parametrize('text,expected', [('200-750万元', (2000000,7500000)),
    ('1.5至2.5亿', (150000000,250000000)), ('-5～-2万',(-50000,-20000)),
    ('20%—40%', (D('.2'),D('.4'))), ('750万元-2亿元',(7500000,200000000))])
def test_shared_units_and_endpoints(text, expected):
    assert interval(text) == expected
    assert numeric(Fact(name='x', value=text, file='p', quote=text, component='lower')) == expected[0]
    assert numeric(Fact(name='x', value=text, file='p', quote=text, component='upper')) == expected[1]


def test_typed_units_need_verbatim_header(tmp_path):
    ws = workspace(tmp_path)
    ws.record_facts([{'name':'owner', 'value':'15', 'unit':'%', 'file':'policy.md', 'quote':'角色分配比例(%)：负责人15'}])
    assert numeric(ws.facts[0]) == D('.15')
    assert '单位' in check_fact(Fact(name='x',value='15',unit='%',file='p',quote='负责人15'), {'p':norm('负责人15')})
    q='负责人15，其他角色5%'
    assert check_fact(Fact(name='x',value='15',unit='%',file='p',quote=q), {'p':norm(q)})


def test_endpoint_from_shared_unit_is_grounded_not_substring_guess():
    q = '金额范围200-750万元'
    assert check_fact(Fact(name='x',value='200万元',file='p',quote=q), {'p':norm(q)}) is None
    assert check_fact(Fact(name='x',value='201万元',file='p',quote=q), {'p':norm(q)})
    q='达成系数1.05'
    assert check_fact(Fact(name='x',value='1',file='p',quote=q), {'p':norm(q)})


def seed(ws):
    ws.record_facts([{'name':'threshold','value':'200-750万元','file':'policy.md','quote':'金额范围：200-750万元'},
                     {'name':'rate','value':'3%','file':'policy.md','quote':'比例3%'}])


def test_compile_projects_range_then_binds_exact_cell(tmp_path):
    ws=workspace(tmp_path); seed(ws)
    ws.compile_table('A', [{'value':'rate'},{'lower':'threshold','value':'rate'}], scheme='S')
    assert ws.drafts['S']['tables']['A']['steps'][1][0] == '2000000'
    assert ws.facts[0].component == 'lower'
    assert not ws._binding_gaps()


def test_repeated_page_quote_can_be_registered_before_the_table_exists(tmp_path):
    from pptx import Presentation
    from pptx.util import Inches
    prs=Presentation()
    for heading in ['A组','B组']:
        slide=prs.slides.add_slide(prs.slide_layouts[6])
        slide.shapes.add_textbox(Inches(1),Inches(1),Inches(4),Inches(1)).text_frame.text=heading
        slide.shapes.add_textbox(Inches(1),Inches(2),Inches(4),Inches(1)).text_frame.text='费率3%'
    path=tmp_path/'policy.pptx';prs.save(path)
    ws=Workspace(FakeLLM([]),build_inventory([path]))
    ws.record_facts([{'name':'A组费率','value':'3%','file':path.name,'quote':'费率3%','page':1,'context':'A组'}])
    ws.compile_table('A',[{'value':'A组费率'}],scheme='S')
    assert ws.facts[0].page==1 and ws.drafts['S']['tables']['A']['steps'][0][1]=='0.03'


def test_atomic_binding_and_noop_does_not_invalidate_money(tmp_path):
    ws=workspace(tmp_path); seed(ws)
    ws.set_rules(scheme='S', tables={'A':{'kind':'tier','mode':'whole','steps':[[0,'.03'],[2000000,'.03']]}})
    ws.untested.clear()
    binding={'name':'threshold','targets':['S::/tables/A/steps/1/0']}
    assert '已校验' in ws.bind_facts([binding])
    assert ws.facts[0].component == 'lower' and not ws.untested
    before=copy.deepcopy(ws.facts)
    assert 'no-op' in ws.bind_facts([binding])
    with pytest.raises(ToolError,match='未生效'):
        ws.bind_facts([{'name':'rate','targets':['S::/tables/A/steps/0/1']},
                       {'name':'threshold','targets':['S::/tables/A/steps/0/1']}])
    assert ws.facts == before and not ws.untested


def test_stale_or_retyped_fact_can_be_reregistered_but_not_falsely_bound(tmp_path):
    ws=workspace(tmp_path)
    fact={'name':'owner','value':'15','file':'policy.md','quote':'角色分配比例(%)：负责人15'}
    ws.record_facts([fact])
    ws.set_rules(scheme='S',tables={'R':{'kind':'tier','mode':'whole','steps':[[0,'.15']]}})
    ws.record_facts([{**fact,'unit':'%'}])
    assert len(ws.facts)==1 and numeric(ws.facts[0])==D('.15')
    ws.facts[0].source_hash='old-version'
    target={'name':'owner','targets':['S::/tables/R/steps/0/1']}
    with pytest.raises(ToolError,match='source_version'):
        ws.bind_facts([target])
    ws.record_facts([{**fact,'unit':'%'}])
    assert '已校验' in ws.bind_facts([target])


def test_local_recovery_of_legacy_range_bindings(tmp_path):
    ws=workspace(tmp_path); seed(ws)
    ws.set_rules(scheme='S', tables={'A':{'kind':'tier','mode':'whole','steps':[[0,'.03'],[2000000,'.03']]}})
    ws.facts[0].targets=['S::/tables/A/steps/1/0']
    assert any('parameter.type' in g for g in ws._binding_gaps())
    assert ws.validate_parameters()['repaired'] == ['threshold']
    assert ws.validate_parameters()['repaired'] == []
    # No guessing of an ambiguous rate or changing a numeric rule to fit evidence.
    ws.facts[0].component='literal'; ws.facts[0].targets=['S::/tables/A/steps/0/1']
    assert ws.validate_parameters()['errors']
    assert ws.facts[0].component == 'literal'


def test_progress_tracks_unresolved_problems_across_successful_operations():
    watch=ProgressWatch()
    assert watch.observe(['A','B']) == 0
    assert watch.observe(['A','B']) == 1
    assert watch.observe(['A','B','C']) == 2
    assert watch.observe(['B','C']) == 0
    assert watch.observe([]) == 0


def test_flash_routes_critical_checks_and_thinking_to_pro_without_recursion():
    llm=LLM.from_config('deepseek-flash', load_config(DEFAULT_CONFIG))
    assert llm.think_llm.name == llm.checker_llm.name == 'deepseek-v4-pro'
    assert llm.think_llm.can_think and llm.checker_llm.can_think
    assert llm.think_llm.vision_llm.name == 'deepseek-flash'
    assert llm.checker_llm.usage is llm.usage
    assert llm.checker_required


def test_required_checker_never_silently_self_verifies():
    from types import SimpleNamespace as NS
    from bonus_agent.llm import LLMError
    from bonus_agent.reread import _ask
    def unavailable(_):
        raise LLMError('service unavailable')
    primary=FakeLLM(lambda _: 'must not be used')
    primary.checker_llm=FakeLLM(unavailable,name='reviewer')
    primary.checker_required=True
    ws=NS(llm=primary,checker_down='')
    with pytest.raises(LLMError,match='不降级'):
        _ask(ws,[{'role':'user','content':'policy'}])
    assert not primary.calls


def test_context_overflow_recovers_with_structured_workspace_and_thinking(tmp_path):
    from bonus_agent.agent import run_agent
    from bonus_agent.llm import LLMError
    class ContextLLM(FakeLLM):
        def __init__(self):
            super().__init__([{'tool':'list_files'}])
            self.flags=[]
            self.think_body={'enable_thinking':True}
        def chat_tools(self,messages,tools,**kw):
            self.flags.append(kw.get('think',False))
            if len(self.flags)==1:
                raise LLMError('context_length_exceeded')
            return super().chat_tools(messages,tools,**kw)
    p=tmp_path/'policy.md';p.write_text('按材料执行',encoding='utf8')
    llm=ContextLLM()
    result=run_agent(llm,[p],max_steps=2)
    assert llm.flags == [False,True]
    assert any(s.tool=='（上下文恢复）' for s in result.steps)


def test_reviewer_change_invalidates_independent_reference_signature():
    from types import SimpleNamespace as NS
    from bonus_agent.reread import review_signature
    ws=NS(llm=FakeLLM([]))
    first=review_signature(ws,'policy')
    ws.llm.checker_llm=FakeLLM([],name='strong-reviewer')
    assert review_signature(ws,'policy') != first


def test_trial_cache_tracks_direct_mutations_and_preserves_source_only_changes(tmp_path,monkeypatch):
    from test_param_tables import case, rules
    from bonus_core.dsl.models import SourceRef
    from bonus_core.engine.run import Engine
    ws=case(tmp_path/'m');rules(ws,'amount * rate')
    original=Engine.compute
    seen=[]
    def count(engine,*a,**kw):
        seen.append(True)
        return original(engine,*a,**kw)
    monkeypatch.setattr(Engine,'compute',count)
    assert 'no-op' in ws.trial_run()
    assert not seen
    scheme=ws.schemes[0]
    ws.drafts[scheme]['variables']['oc']['sources']=[{'doc':'p','quote':'按费率'}]
    assert 'no-op' in ws.trial_run()  # metadata cannot change money
    assert not seen
    ws.drafts[scheme]['variables']['oc']['expr']='amount * rate * 2'
    ws.trial_run()
    assert seen and ws.results[scheme]['R0'].pool == '164.00'


def test_textual_and_date_bindings_do_not_pretend_to_be_numbers():
    from bonus_agent.facts import binding_errors
    f=Fact(name='status',value='已通过',file='p',quote='状态已通过',targets=['variables.ok.expr'])
    assert not binding_errors([f],{'S':{'variables':{'ok':{'expr':'status == "已通过"'}}}})
    assert binding_errors([f],{'S':{'variables':{'ok':{'expr':'status == "待审批"'}}}})
    f=Fact(name='day',value='2027年9月30日',file='p',quote='截至2027年9月30日',targets=['variables.ok.expr'])
    assert not binding_errors([f],{'S':{'variables':{'ok':{'expr':'date("2027-09-30")'}}}})


def test_binding_uses_finance_selected_value_without_rebinding_rejected_evidence(tmp_path):
    a=tmp_path/'A.md';b=tmp_path/'B.md'
    a.write_text('比例3%',encoding='utf8');b.write_text('比例4%',encoding='utf8')
    ws=Workspace(FakeLLM([]),build_inventory([a,b]))
    ws.record_facts([{'name':'费率','value':v,'file':p.name,'quote':'比例'+v} for p,v in [(a,'3%'),(b,'4%')]])
    ws.set_rules(scheme='S',tables={'R':{'kind':'tier','mode':'whole','steps':[[0,'.04']]}})
    spec={'name':'费率','targets':['S::/tables/R/steps/0/1']}
    with pytest.raises(ToolError,match='conflict'):
        ws.bind_facts([spec])
    ws.questions[0].answer=next(o for o in ws.questions[0].options if o.startswith('4%'))
    assert '已校验' in ws.bind_facts([spec])
    assert not ws.facts[0].targets and ws.facts[1].targets


def test_final_default_hazard_detected_through_alias():
    from test_validation_contracts import calculate
    from bonus_agent.contracts import ratio_default_gaps
    rs,_=calculate()
    from bonus_core.dsl.models import Variable
    rs.variables={'rate':Variable(expr='ratio(amount, budget, zero: 1)'),
                  'coef':Variable(expr='tier_rate(C, rate)')}
    assert ratio_default_gaps(rs)[0][0] == 'variables.coef'
    rs.variables['coef'].expr='tier_ratio(C, amount, budget, 1)'
    assert not ratio_default_gaps(rs)
    rs.variables['coef'].expr='if(budget == 0, 1, tier_rate(C, rate))'
    assert not ratio_default_gaps(rs)
    rs.variables['coef'].expr='budget * tier_rate(C, rate)'
    assert not ratio_default_gaps(rs)
    rs.variables['coef'].expr='tier_rate(C, rate)'
    rs.variables['coef'].ratio_default='rate'
    assert ratio_default_gaps(rs)  # cannot waive a hazard without material evidence
    from bonus_core.dsl.models import SourceRef
    rs.variables['coef'].sources=[SourceRef(doc='p',quote='分母0时达成率按100%继续查档')]
    assert not ratio_default_gaps(rs)


def test_distribution_rejects_hidden_zero_pool_and_requires_named_pending():
    from test_validation_contracts import calculate
    from bonus_core.engine.issues import EngineError
    sources=[{'doc':'policy','quote':'总额按角色分配，其中20%待分配'}]
    report={'people':'region','pending':[{'name':'待分配','level':'项目','expr':'amount * .2','sources':sources}],
            'distributions':[{'name':'去向','level':'项目','total':'amount','pending':['待分配'],'sources':sources}]}
    # DSL numbers require a leading zero.
    report['pending'][0]['expr']='amount * 0.2'
    _,result=calculate('amount * 0.8',report=report)
    assert result.people[-1]['pending']
    with pytest.raises(EngineError,match='去向不闭合'):
        calculate('0',report=report)
    report['distributions'][0]['pending']=[]
    with pytest.raises(EngineError,match='去向不闭合'):
        calculate('amount * 0.8',report=report)


def test_composite_identity_conflict_is_not_fuzzy_merge():
    from bonus_agent.contracts import identity_conflicts
    from bonus_core.data.mapping import DatasetMapping
    from bonus_core.data.table import from_records
    from bonus_core.dsl.models import RuleSet
    rs=RuleSet.model_validate({'name':'x','version':'1','datasets':{
        '订单':{'key':'pid','fields':{'pid':{'type':'id'}}},
        '名单':{'key':'rid','fields':{'rid':{'type':'id'},'pid':{'type':'id'}}}},
        'pool':{'scope':'订单','expr':'0'},'allocation':[{'level':'订单','key':'pid'},
            {'level':'名单','key':'rid','parent_key':'pid','weight':'1'}]})
    data=from_records(rs,{'订单':[{'pid':'202710|C057|别名甲|M71'}],
                          '名单':[{'rid':'x','pid':'202710|C057|别名乙|M71'}]})
    mapping={n:DatasetMapping(file='x',sheet='x',columns={},combine={'pid':['月','编号','简称','型号']}) for n in rs.datasets}
    mapping['名单'].combine['pid'][2]='正式简称'
    gaps=identity_conflicts(rs,data,mapping)
    assert gaps[0]['count'] == 1 and gaps[0]['examples'][0]['key'].endswith('别名乙|M71')
    data['名单'].rows[0].values['pid']='202710|C058|别名乙|M72'
    assert not identity_conflicts(rs,data,mapping)
