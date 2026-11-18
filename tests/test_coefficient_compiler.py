import copy
import json
from decimal import Decimal as D

import pytest
from pptx import Presentation
from pptx.util import Inches

from bonus_agent.agent import Workspace, ToolError
from bonus_agent.auto import build_inventory
from bonus_agent.llm import FakeLLM
from bonus_core.dsl.models import RuleSet, TierTable
from bonus_core.dsl.validator import validate
from bonus_core.engine.evaluator import tier_rate, tier_amount, EvalError
from bonus_core.dsl.excel_formula import Params, Formula

pytestmark = pytest.mark.evidence


def workspace(tmp_path, labels=None, percent_values=False):
    labels = labels or ['<30% (含)', '30-65% (含)', '65-90%', '90% (含)+']
    prs=Presentation();slide=prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_textbox(Inches(0),Inches(0),Inches(7),Inches(1)).text='销量完成率系数表'
    table=slide.shapes.add_table(2,len(labels)+1, Inches(0),Inches(1), Inches(8),Inches(3)).table
    table.cell(1,0).text='指标系数(%)' if percent_values else '指标系数'
    values=['60','90','100','120'] if percent_values else ['0.6','0.9','1.0','1.2']
    for col,(label,value) in enumerate(zip(labels, values),1):
        table.cell(0,col).text=label;table.cell(1,col).text=value
    path=tmp_path/'rules.pptx';prs.save(path)
    ws=Workspace(FakeLLM([]),build_inventory([path]))
    args={'name':'coefficient','scheme':'S','applies_to':'销量完成率系数表',
          'cells_from':{'file':path.name,'page':1,'table':1},
          'rows':[{'range':f'R1C{n}','value':f'R2C{n}'} for n in range(2,len(labels)+2)]}
    return ws,args


def test_compile_all_source_endpoints_and_exact_mixed_boundaries(tmp_path):
    ws,args=workspace(tmp_path)
    assert ws.compile_coefficient_table(**args)['status']=='PASS'
    table=ws._ruleset('S').tables['coefficient']
    assert table.step_inclusive==[True,False,False,True]
    for amount,expected in [('-9','.6'),('0','.6'),('.3','.6'),('.30001','.9'),
                            ('.65','.9'),('.65001','1'),('.89999','1'),('.9','1.2'),('1.7','1.2')]:
        assert tier_rate(table,D(amount))==D(expected)
    assert not ws._binding_gaps()
    with pytest.raises(EvalError,match='tier_rate'):
        tier_amount(table,D('.4'))
    ws.set_rules(scheme='S',pool={'expr':'tier(coefficient, 0.4)'})
    assert any(i.code=='fn_table' and 'tier_rate' in i.message for i in validate(ws._ruleset('S')))
    assert not ws.llm.calls


def test_shared_boundary_evidence_survives_second_table_and_idempotent_recompile(tmp_path):
    ws,args=workspace(tmp_path)
    ws.compile_coefficient_table(**args)
    ws.compile_coefficient_table(**{**args,'name':'second'})
    assert not ws._binding_gaps()
    before=copy.deepcopy([f.model_dump() for f in ws.facts])
    drafts=copy.deepcopy(ws.drafts)
    ws.compile_coefficient_table(**args)
    assert [f.model_dump() for f in ws.facts]==before
    assert ws.drafts==drafts


def test_explicit_value_unit_header_is_used_without_borrowing_input_percentages(tmp_path):
    ws,args=workspace(tmp_path,percent_values=True)
    ws.compile_coefficient_table(**args)
    table=ws._ruleset('S').tables['coefficient']
    assert tier_rate(table,D('.4'))==D('.9')
    assert not ws._binding_gaps()


def test_inclusion_annotation_attached_to_lower_endpoint_is_not_upper_inclusion(tmp_path):
    ws,args=workspace(tmp_path,['<7%','7% (含)-13%','13% (含)-19%','19%(含)+'])
    ws.compile_coefficient_table(**args)
    table=ws._ruleset('S').tables['coefficient']
    assert table.step_inclusive==[True,True,True,True]
    for value,expected in [('.06999','.6'),('.07','.9'),('.13','1'),('.19','1.2')]:
        assert tier_rate(table,D(value))==D(expected)


def test_legacy_failure_returns_executable_source_based_repair(tmp_path):
    ws,args=workspace(tmp_path)
    legacy={'name':'coefficient','scheme':'S','mode':'whole','cells_from':args['cells_from'],
            'algorithm_basis':{'file':'rules.pptx','page':1,'quote':'销量完成率系数表'},
            'rows':[{'lower':'-1' if n==2 else f'R1C{n}','value':f'R2C{n}'} for n in range(2,6)]}
    before=copy.deepcopy(ws.drafts)
    with pytest.raises(ToolError,match='next_call=') as failure:
        ws.compile_table(**legacy)
    assert ws.drafts==before and not ws.facts
    action=json.loads(str(failure.value).split('next_call=',1)[1])
    assert action['tool']=='compile_coefficient_table'
    assert getattr(ws,action['tool'])(**action['args'])['status']=='PASS'
    assert not ws._binding_gaps()


def test_task_routes_explicit_coefficient_intent_and_keeps_amount_algorithm_gate(tmp_path):
    ws,args=workspace(tmp_path)
    ws.plan_calculation('核对原文区间系数',['系数表'],{'rules.pptx':'rule'},['S'])
    ws.plan_formula_tasks([{'id':'coef','scheme':'S','target':'/tables/coefficient','title':'指标区间系数表'},
                           {'id':'pool','scheme':'S','target':'/pool','depends_on':['coef']}])
    from bonus_agent.construction import TaskBoard
    assert 'compile_coefficient_table' in TaskBoard(ws).summary()['next']
    ws.compile_coefficient_table(**args)
    assert ws.check_formula_task('coef')['status']=='PASS'
    # Relabeling a table cannot bypass an explicit source requirement for marginal money.
    p=tmp_path/'money.md';p.write_text('奖金必须按超额累进计算。',encoding='utf-8')
    other=Workspace(FakeLLM([]),build_inventory([*ws.inv.files,p]))
    other.record_clauses([{'file':p.name,'point':'超额累进金额','quote':'奖金必须按超额累进计算'}])
    other.set_rules(scheme='S',tables=ws.drafts['S']['tables'],pool={'expr':'tier_rate(coefficient, 0.4) * 100'})
    other.cover_clauses({'C1':'pool'})
    from bonus_agent.semantics import semantic_gates
    assert any('algorithm.scope' in issue for issue in semantic_gates(other))


def test_ambiguous_or_wrong_source_never_partially_registers(tmp_path):
    ws,args=workspace(tmp_path,['<30%','30-65%','65%以上'])
    with pytest.raises(ToolError,match='等于0.65'):
        ws.compile_coefficient_table(**args)
    assert not ws.facts and not ws.drafts
    ws,args=workspace(tmp_path)
    args['rows'][0]['range']='R900C2'
    with pytest.raises(ToolError,match='source.cell'):
        ws.compile_coefficient_table(**args)
    assert not ws.facts and not ws.drafts
    ws,args=workspace(tmp_path)
    args['applies_to']='另一事业部'
    with pytest.raises(ToolError,match='不在'):
        ws.compile_coefficient_table(**args)
    assert not ws.facts and not ws.drafts


def test_malformed_boundary_metadata_and_legacy_behavior():
    with pytest.raises(ValueError,match='逐档对应'):
        TierTable(mode='whole',steps=[(0,1),(1,2)],step_inclusive=[True])
    with pytest.raises(ValueError,match='超额累进'):
        TierTable(mode='excess',purpose='coefficient',steps=[(0,1)])
    legacy=TierTable(mode='whole',steps=[(0,'.1'),(10,'.2')],lower_inclusive=False)
    assert tier_rate(legacy,D(10))==D('.1')
    assert tier_rate(legacy,D(0))==0


def test_exported_formula_keeps_each_original_boundary(tmp_path):
    ws,args=workspace(tmp_path);ws.compile_coefficient_table(**args)
    rs=ws._ruleset('S');tables=Params();tables.place(rs,{'coefficient'})
    from bonus_core.dsl.parser import parse
    formula=Formula(rs,tables,{}).tr(parse('tier_rate(coefficient, 0.9)'))
    # The final boundary includes equality; the earlier boundaries exclude it.
    assert '0.9>=' in formula and '0.9>' in formula
    assert rs.model_dump(mode='json')['tables']['coefficient']['step_inclusive']==[True,False,False,True]
