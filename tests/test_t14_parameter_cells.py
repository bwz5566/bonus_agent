"""Physical parameter identity and retrieval survive similar slides and retries."""
import copy
from decimal import Decimal

import openpyxl
import pytest
from pptx import Presentation
from pptx.util import Inches

from bonus_agent import case
from bonus_agent.agent import CHAT_SOURCE, Step, ToolError, Workspace, _deep_reason, run_agent
from bonus_agent.auto import build_inventory
from bonus_agent.llm import FakeLLM

pytestmark=pytest.mark.evidence


def workspace(tmp_path):
    path=tmp_path/'policy.pptx';prs=Presentation()
    for scope,rate in [('北区','0.25%'),('西区','0.50%'),('东区','0.70%')]:
        slide=prs.slides.add_slide(prs.slide_layouts[6])
        table=slide.shapes.add_table(3,3,Inches(0),Inches(0),Inches(7),Inches(3)).table
        for r,values in enumerate([['档位','新项目','老项目'],['首档',rate,'0.10%'],['角色比例(%)','40 非特区\n42.5 特区','10']]):
            for c,text in enumerate(values):table.cell(r,c).text=text
        # A caption created after the table must still be associated with the same slide.
        slide.shapes.add_textbox(Inches(0),Inches(4),Inches(7),Inches(1)).text_frame.text='提成规则\n'+scope
    prs.save(path)
    return Workspace(FakeLLM([]),build_inventory([path]))


def test_native_page_scope_and_coordinate_registration_reuse(tmp_path):
    ws=workspace(tmp_path);file='policy.pptx'
    index=ws.read_parameter_table(file)
    assert [p['scope'] for p in index['pages']]==['北区','西区','东区']
    assert ws.read_parameter_table(file,2)['page_context']==['提成规则\n西区']
    with pytest.raises(ToolError,match='有效范围1—3'):ws.read_parameter_table(file,4)
    result=ws.record_parameter_cells(file,2,1,applies_to='西区',cells=[{'row':2,'column':2}])
    name=result['facts'][0]['name'];assert result['facts'][0]['value']=='0.50%'
    assert not result['facts'][0]['reused'] and ws.facts[0].context=='西区'
    assert ws.facts[0].source_cell=={'table':1,'row':2,'column':2}
    assert ws.read_parameter_table(file,2)['registered_count']==1
    clone=Workspace(FakeLLM([]),ws.inv);case.restore(clone,case.snapshot(ws))
    result=clone.record_parameter_cells(file,2,1,applies_to='西区',cells=[{'row':2,'column':2}])
    assert result['facts'][0]['reused'] and result['facts'][0]['name']==name
    assert len(clone.facts)==1
    clone.compile_table('R',[{'value':name}],scheme='S')
    assert Decimal(clone.drafts['S']['tables']['R']['steps'][0][1])==Decimal('0.005')
    assert not clone._binding_gaps()


def test_wrong_page_and_value_still_rejected_and_alias_fields_forbidden(tmp_path):
    ws=workspace(tmp_path);file='policy.pptx'
    bad={'name':'西区首档','file':file,'page':3,'value':'0.70%','quote':'首档 | 0.70% | 0.10%',
         'source_cell':{'table':1,'row':2,'column':2}}
    with pytest.raises(ToolError,match='read_parameter_table'):ws.record_facts([bad])
    with pytest.raises(ToolError,match='source.cell'):
        ws.record_facts([{**bad,'name':'x','value':'0.10%'}])
    with pytest.raises(ToolError,match='不能传自拟'):
        ws.record_parameter_cells(file,3,1,applies_to='东区',cells=[{'row':2,'column':2,'name':'西区首档'}])
    with pytest.raises(ToolError,match='整数'):
        ws.record_parameter_cells(file,3,1,applies_to='东区',cells=[{'row':2.5,'column':2}])
    assert not ws.facts


def test_native_tools_work_through_agent_dispatch(tmp_path):
    ws=workspace(tmp_path)
    name='policy.pptx::P2.T1.R2.C2.L1'
    result=run_agent(FakeLLM([
        {'tool':'read_parameter_table','args':{'file':'policy.pptx'}},
        {'tool':'record_parameter_cells','args':{'file':'policy.pptx','page':2,'table':1,'applies_to':'西区','cells':[{'row':2,'column':2}]}},
        {'tool':'compile_table','args':{'name':'R','scheme':'S','rows':[{'value':name}]}},
        {'tool':'read_workspace','args':{'section':'facts','file':'policy.pptx','page':2,'query':name}},
    ]),ws.inv.files,tmp_path/'out',max_steps=4)
    assert all(s.ok for s in result.steps if not s.tool.startswith('（'))
    assert result.ws.facts[0].targets==['S::/tables/R/steps/0/1']


def test_multiline_requires_explicit_line_and_does_not_guess_units(tmp_path):
    ws=workspace(tmp_path);file='policy.pptx'
    with pytest.raises(ToolError,match='必须指定line'):
        ws.record_parameter_cells(file,1,1,applies_to='北区',cells=[{'row':3,'column':2}])
    result=ws.record_parameter_cells(file,1,1,applies_to='北区',cells=[{'row':3,'column':2,'line':1},
                                              {'row':3,'column':2,'line':2,'unit':'%'}])
    assert [f['value'] for f in result['facts']]==['40','42.5']
    assert ws.facts[0].unit is None and ws.facts[1].unit=='%'
    from bonus_agent.facts import numeric
    assert str(numeric(ws.facts[1]))=='0.425'
    # Removing an explicit unit must not inherit it from an earlier registration.
    ws.record_parameter_cells(file,1,1,applies_to='北区',cells=[{'row':3,'column':2,'line':2}])
    assert next(f for f in ws.facts if f.source_cell.get('line')==2).unit is None
    # An unrelated percentage elsewhere in the slide cannot supply a cell's unit.
    with pytest.raises(ToolError,match='单位'):
        ws.record_parameter_cells(file,1,1,applies_to='北区',cells=[{'row':3,'column':3,'unit':'万元'}])


def test_fact_lookup_filters_before_pagination_and_reuses_legacy_name(tmp_path):
    ws=workspace(tmp_path);file='policy.pptx'
    ws.record_facts([{'name':'海外西区费率','file':file,'page':2,'context':'西区','quote':'首档\n新项目\n0.50%',
                     'value':'0.50%','source_cell':{'table':1,'row':2,'column':2}}])
    ws.record_parameter_cells(file,1,1,applies_to='北区',cells=[{'row':2,'column':2}])
    out=ws.read_workspace('facts',file=file,page=2,query='西区',limit=1)
    assert out['total']==1 and out['items'][0]['name']=='海外西区费率'
    assert ws.record_parameter_cells(file,2,1,applies_to='西区',cells=[{'row':2,'column':2}])['facts'][0]['name']=='海外西区费率'
    assert len(ws.facts)==2
    ws.facts[0].source_hash='stale'
    assert not ws.record_parameter_cells(file,2,1,applies_to='西区',cells=[{'row':2,'column':2}])['facts'][0]['reused']


def test_partial_fact_batch_is_failed_in_agent_and_keeps_valid_rows(tmp_path):
    p=tmp_path/'policy.md';p.write_text('甲组费率3%',encoding='utf8')
    result=run_agent(FakeLLM([{'tool':'record_facts','args':{'facts':[
        {'name':'valid','file':p.name,'quote':'甲组费率3%','value':'3%'},
        {'name':'invalid','file':p.name,'quote':'甲组费率3%','value':'7%'}]}},
        {'tool':'note','args':{'topic':'next','text':'repair only rejected row'}}]),[p],tmp_path/'out',max_steps=2)
    step=next(s for s in result.steps if s.tool=='record_facts')
    assert not step.ok and '已登记 1 条' in step.result and '未登记' in step.result
    assert [f.name for f in result.ws.facts]==['valid']


def test_plan_role_mismatch_precise_and_deterministic(tmp_path):
    ws=workspace(tmp_path)
    with pytest.raises(ToolError,match='plan.ignore_role.*reference'):
        ws.plan_calculation('test',['report'],{'policy.pptx':'reference'},['S'],[{'ref':'policy.pptx','reason':'reference only'}])
    step=Step('plan_calculation',{},'错误：plan.ignore_role：reference',False)
    assert not _deep_reason([step],0,ws,{'plan_calculation':1})
    assert ws.plan_calculation('test',['report'],{'policy.pptx':'reference'},['S'])['status']=='PASS'
    assert '虚拟来源' in ws.read_document(CHAT_SOURCE)
    ws.said='按人员汇总再舍入'
    assert ws.said in ws.read_document(CHAT_SOURCE)


def test_period_alias_is_only_resolved_when_unique(tmp_path):
    p=tmp_path/'data.xlsx';wb=openpyxl.Workbook();s=wb.active
    s.append(['日期','金额']);s.append(['2026-09',100]);wb.save(p);wb.close()
    ws=Workspace(FakeLLM([]),build_inventory([p]))
    fields={'biz_date':{'column':'日期','type':'text'},'amount':{'column':'金额','type':'money'}}
    ws.define_dataset('sales',p.name,'Sheet',fields,period='日期')
    assert ws.datasets['sales'].period=='biz_date'
    before=copy.deepcopy(ws.datasets)
    with pytest.raises(ToolError,match='唯一'):
        ws.define_dataset('sales',p.name,'Sheet',{**fields,'other_date':{'column':'日期','type':'text'}},period='日期')
    assert ws.datasets==before


def test_partial_unpivot_patch_keeps_other_mapping_and_restores(tmp_path):
    p=tmp_path/'data.xlsx';wb=openpyxl.Workbook();s=wb.active
    s.append(['月份','客户','角色甲','角色乙','比例甲','比例乙'])
    s.append(['202609','A','张三','李四','张三(50%)、李四(50%)','张三(60%)、李四(40%)'])
    wb.save(p);wb.close();ws=Workspace(FakeLLM([]),build_inventory([p]))
    ws.define_dataset('roster',p.name,'Sheet',{
        'month':{'column':'月份','type':'text'},'pid':{'column':'pid','type':'id'},
        'person':{'column':'person','type':'text'},'share':{'column':'share','type':'ratio'}},
        link='pid',period='month',filter={'客户':'A'},combine={'pid':['月份','客户']},
        unpivot={'columns':['角色甲','角色乙'],'value':'person','share':'share','share_from':'比例甲'})
    before=copy.deepcopy(ws.mapping['roster'])
    ws.update_dataset('roster',{'unpivot':{'share_from':'比例乙'}})
    after=ws.mapping['roster']
    assert after.unpivot=={**before.unpivot,'share_from':'比例乙'}
    assert after.filter==before.filter and after.combine==before.combine and after.columns==before.columns
    clone=Workspace(FakeLLM([]),ws.inv);case.restore(clone,case.snapshot(ws))
    assert clone.mapping==ws.mapping
    saved=copy.deepcopy(ws.mapping)
    with pytest.raises(ToolError):ws.update_dataset('roster',{'unpivot':{'share_from':'missing'}})
    assert ws.mapping==saved
