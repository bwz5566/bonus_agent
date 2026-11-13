import pytest
from pptx import Presentation
from pptx.util import Inches
from bonus_agent.agent import Workspace, ToolError, Step, _deep_reason, _resolve_dup_columns
from bonus_agent.auto import build_inventory
from bonus_agent.llm import FakeLLM

pytestmark=pytest.mark.evidence

def materials(tmp_path):
    p=tmp_path/'policy.pptx'; prs=Presentation()
    for title in ['PI 事業部','PI事业部']:
        s=prs.slides.add_slide(prs.slide_layouts[6])
        s.shapes.add_textbox(Inches(0),Inches(0),Inches(5),Inches(1)).text_frame.text=title
        t=s.shapes.add_table(2,2,Inches(0),Inches(1),Inches(5),Inches(2)).table
        for r,vals in enumerate([['角色','新项目'],['销售執行人','40 非南方區\n42.5 南方區']]):
            for c,text in enumerate(vals):t.cell(r,c).text=text
    prs.save(p)
    return p

def test_explicit_heading_beats_name_heuristic_and_cell_rejects_swapped_value(tmp_path):
    p=materials(tmp_path);ws=Workspace(FakeLLM([]),build_inventory([p]))
    fact={'name':'PI事业部-销售执行人-非南方区','file':p.name,'page':1,'context':'PI 事業部',
          'quote':'40 非南方區 42.5 南方區','value':'40', 'source_cell':{'table':1,'row':2,'column':2,'line':1}}
    assert ws.read_parameter_table(p.name,1)['tables'][0]['rows'][1]['cells'][1]['lines'][0]=='40 非南方區'
    assert '已登记' in ws.record_facts([fact])
    assert '非南方區' in ws.facts[0].meaning
    with pytest.raises(ToolError,match='source.cell'):
        ws.record_facts([{**fact,'name':'wrong','value':'42.5'}])
    with pytest.raises(ToolError,match='source.cell'):
        ws.record_facts([{k:v for k,v in {**fact,'quote':'| 40 非南方區 | 42.5 南方區 |'}.items() if k!='source_cell'}])

def test_informational_notes_do_not_spend_pro_budget():
    ws=Workspace(FakeLLM([]),build_inventory([]))
    assert not _deep_reason([Step('define_dataset',{},'已保存\n注意：当期62个项目，1719条明细',True)],0,ws,{})
    assert _deep_reason([Step('define_dataset',{},'错误：关联失败',False)],0,ws,{})

def test_first_duplicate_alias_resolves_in_share_from():
    from types import SimpleNamespace
    st=SimpleNamespace(headers=['姓名','比例','比例(D列)'])
    _,_,unpivot,_=_resolve_dup_columns(st,{},None,{'columns':['姓名'],'share_from':'比例(B列)'},None)
    assert unpivot['share_from']=='比例'

def test_dataset_patch_preserves_identity_and_survives_restore(tmp_path):
    import openpyxl
    from bonus_agent import case
    p=tmp_path/'data.xlsx';wb=openpyxl.Workbook();s=wb.active
    s.append(['月份','客户','型号','金额']);s.append(['202609','A','M',100]);wb.save(p);wb.close()
    ws=Workspace(FakeLLM([]),build_inventory([p]))
    fields={'period':{'column':'月份','type':'text'},'pid':{'column':'pid','type':'id'},'amount':{'column':'金额','type':'money'}}
    ws.define_dataset('sales',p.name,'Sheet',fields,combine={'pid':['月份','客户','型号']},period='period')
    clone=Workspace(FakeLLM([]),ws.inv);case.restore(clone,case.snapshot(ws))
    before=clone.mapping['sales'].model_copy(deep=True)
    clone.update_dataset('sales',{'link':'pid'})
    assert clone.mapping['sales']==before
    assert clone.datasets['sales'].link=='pid' and clone.datasets['sales'].period=='period'


def test_empty_validation_never_claims_full_recalculation():
    ws=Workspace(FakeLLM([]),build_inventory([]))
    checks={c['id']:c['status'] for c in ws.validation_report()['checks']}
    assert checks['independent.full']=='INCOMPLETE'
    assert checks['program.contracts']=='INCOMPLETE'


def test_stop_during_diagnosis_prevents_next_primary_write(tmp_path):
    from bonus_agent.agent import run_agent
    p=tmp_path/'rule.md';p.write_text('金额100元',encoding='utf8')
    cancelled=[False]
    def diagnose(messages):
        cancelled[0]=True
        return '{"diagnosis":"source missing","next_action":"inspect","evidence_needed":"rate","acceptance":"bind"}'
    bad={'tool':'compile_table','args':{'name':'T','rows':[{'value':'missing'}]}}
    llm=FakeLLM([bad,bad,{'tool':'note','args':{'topic':'late','text':'must not write'}}])
    llm.recovery_mode='advisor';llm.think_llm=FakeLLM(diagnose);llm.think_llm.usage=llm.usage
    result=run_agent(llm,[p],tmp_path/'out',max_steps=6,should_stop=lambda:cancelled[0])
    assert '已手动停止' in result.error and 'late' not in result.ws.notes
