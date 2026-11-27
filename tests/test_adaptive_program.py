"""Independent, fictional rules/data; no September-specific fields or amounts."""
from copy import deepcopy
from decimal import Decimal as D
from fractions import Fraction as F

import pytest
from hypothesis import given, strategies as st, settings

from bonus_agent.adaptive_sources import compile_inputs, read_datasets
from bonus_agent.adaptive_rules import (coverage, parse_literal_formula, validate_clause_chunk,
                                        rule_units, validate_source_inventory)
from bonus_agent.commission_sources import Catalog, Sheet
from bonus_core.engine.adaptive import CalculationProgram, Dataset, Inputs, ProgramError, Record, apply_person_program


def op(name,*args): return {'op':name,'args':list(args)}
def ref(name):return {'ref':name}
def fld(name):return {'field':name}
def key(name):return {'key':name}
def param(name):return {'param':name}
def query(dataset,aggregate,value=None,*,match=None,where=None,empty='error',nulls='error',duplicates='error'):
    q={'dataset':dataset,'aggregate':aggregate,'match':match or {},'empty':empty,'nulls':nulls}
    if value is not None:q['value']=value
    if where is not None:q['where']=where
    if aggregate=='unique':q['duplicates']=duplicates
    return {'query':q}


def fixture():
    c=Catalog([])
    c.sheets={
        'S1':Sheet('S1','transactions.csv','transactions',[('Contract','Office','Period','Net'),('0007','N','202701',1000),('0008','S','202701',800)]),
        'S2':Sheet('S2','different.xlsx','ledger',[('January transactions',),('Revenue','Period','Office','Agreement'),(200,'202701','N','0007'),(999,'202612','S','0008')]),
        'S3':Sheet('S3','quality.xlsx','status',[('Agreement','Collection'),('0007','80%'),('0008','50%')]),
        'S4':Sheet('S4','rate.xlsx','rate',[('Scheme','Commission','Reserve'),('Service','10%','20%')]),
        'S5':Sheet('S5','people.xlsx','roster',[('Agreement','Employee ID','Person','Role','Share'),('0007','A001','同名甲','partner','100%'),('0007','B001','同名甲','service','100%'),('0008','A001','同名甲','partner','100%')]),
    }
    fields=lambda types:{k:{'kind':v,'nullable':False} for k,v in types.items()}
    def dataset(label,types,sources,keys=(),duplicates='error'):
        return {'label':label,'fields':fields(types),'sources':sources,'grain':{'kind':'unique' if keys else 'detail','keys':list(keys),'duplicates':duplicates,'rules':[]},'rules':[]}
    def source(sheet,cols,header=1):return {'sheet':sheet,'header_row':header,'columns':cols}
    ds={
        'orders':dataset('交易',{'agreement':'id','office':'text','month':'month','revenue':'money'},[source('S1',{'agreement':'A','office':'B','month':'C','revenue':'D'}),source('S2',{'agreement':'D','office':'C','month':'B','revenue':'A'},2)]),
        'collection':dataset('回款表现',{'agreement':'id','ratio':'ratio'},[source('S3',{'agreement':'A','ratio':'B'})],('agreement',)),
        'staff':dataset('员工分配',{'agreement':'id','employee':'id','name':'text','role':'text','share':'ratio'},[source('S5',{'agreement':'A','employee':'B','name':'C','role':'D','share':'E'})]),
    }
    parameters={k:{'source':{'sheet':'S4','row':2,'column':col},'kind':'ratio','unit':'raw','rules':[]} for k,col in [('commission','B'),('reserve','C')]}
    def metric(label,expr,owner='project_policy'):return {'label':label,'expr':expr,'rules':[],'owner':owner,'type':'number'}
    metrics={
        'sales_net':metric('净销售额',query('$project','sum',fld('revenue'))),
        'collect_factor':metric('回款及时率系数',query('collection','unique',fld('ratio'),match={'agreement':key('agreement')}),'coefficient_policy'),
        'gross':metric('奖金总额',op('mul',ref('sales_net'),param('commission'),ref('collect_factor')),'hold_policy'),
        'reserve':metric('履约暂扣',op('mul',ref('gross'),param('reserve')),'hold_policy'),
        'net':metric('未舍入可分配',op('sub',ref('gross'),ref('reserve')),'hold_policy'),
        'pay':metric('可分配',op('round',ref('net'),2),'hold_policy'),
    }
    plan={'population':{'dataset':'orders','keys':['office','agreement'],'where':op('eq',fld('month'),{'period':'month'}),'rules':[]},
          'metrics':metrics,'funding':{'total':'gross','payable':'net','alloc':'pay','destinations':{'reserve':'履约暂扣'},'labels':{'total':'合计','alloc':'可分配'},'summary_digits':2,'rules':[]}}
    def role(label,rate,role_id):
        return {'label':label,'rate':{'num':rate},'rules':[],'recipients':{'kind':'rows','dataset':'staff','match':{'agreement':key('agreement')},
            'where':op('eq',fld('role'),role_id),'pairs':[{'name':'name','id':'employee','share':'share'}],'missing_share':'error','missing':'pending','duplicates':'error','pending_label':label+'待分配','rules':[]}}
    roster={'roles':{'partner':role('合作伙伴','0.6','partner'),'service':role('售后服务','0.4','service')},'rounding':{'when':'after_sum','digits':2,'rules':[]},'rules':[]}
    return c,ds,parameters,plan,roster


def test_different_keys_sources_indicator_roles_and_same_name_identities():
    c,ds,params,plan,roster=fixture()
    p=CalculationProgram(compile_inputs(c,ds,params,{},period='202701'),plan,'2027-01')
    b=p.basis();s=p.settlement(roster,b)
    assert len(b['projects'])==2 and b['key_fields']==['office','agreement']
    project1=next(v for v in b['projects'].values() if v['key_values']['agreement']=='0007')
    assert project1['metrics']['sales_net']==1200 and project1['total']==D(96) and project1['alloc']==D('76.8')
    assert b['pool']==D('108.8')
    people={(r['destination'],r['id']):r['amount'] for r in s['people']}
    assert people=={('person','A001'):D('65.28'),('person','B001'):D('30.72'),('pending','售后服务待分配'):D('12.80')}
    assert s['residual']==0 and s['allocated']+s['pending']==b['pool']


def test_direct_roster_without_pending_needs_no_label_and_no_extra_rounding():
    from bonus_core.engine.adaptive import personal_program_inputs
    c,ds,params,plan,_=fixture();p=CalculationProgram(compile_inputs(c,ds,params,{},period='202701'),plan,'2027-01');b=p.basis()
    roster={'roles':{'direct':{'label':'直接归属','rate':1,'rules':[],'recipients':{'kind':'rows','dataset':'collection','match':{'agreement':key('agreement')},'pairs':[{'name':'agreement','id':'agreement'}],
                  'missing_share':'single','default_share':1,'missing':'error','duplicates':'error','rules':[]}}},'rounding':{'when':'none','rules':[]},'rules':[]}
    s=p.settlement(roster,b);assert len(s['people'])==2 and s['pending']==0 and s['allocated']==b['pool'] and s['rounding']['digits']==2
    assert all(r.get('name_is_identity') for r in s['rows'])
    q=deepcopy(roster);q['roles']['direct']['recipients']['missing']='pending'
    with pytest.raises(ProgramError,match='待分配.*名称'):p.validate_roster(q)
    record={'project':'X','role_id':'direct','person_id':'0001','name':'0001','raw':D('1.005'),'destination':'person'}
    for when,expected in [('none',D('1.005')),('after_sum',D('1.005')),('per_record',D('1.01'))]:
        inputs=personal_program_inputs(p.inputs,{'rows':[record],'rounding':{'when':when,'digits':2}})
        assert inputs.datasets['approved_contributions'].rows[0].values['rounded_record']==expected


def test_unambiguous_model_expression_aliases_preserve_financial_semantics():
    c,ds,params,plan,_=fixture()
    clean=CalculationProgram(compile_inputs(c,ds,params,{},period='202701'),deepcopy(plan),'2027-01').basis()
    candidate=deepcopy(plan)
    candidate['population']['where']=op('and',op('eq',fld('month'),{'value':'202701'}),op('in_period',fld('month'),{'period':'month'}))
    candidate['metrics']['pay']['expr']=op('round',ref('net'),{'value':2})
    out=CalculationProgram(compile_inputs(c,ds,params,{},period='202701'),candidate,'2027-01').basis()
    assert out['pool']==clean['pool'] and out['funding']==clean['funding']
    assert {k:v['metrics'] for k,v in out['projects'].items()}=={k:v['metrics'] for k,v in clean['projects'].items()}
    # A month alias must not change the meaning of an actual quarterly period.
    with pytest.raises(ProgramError,match='参数数量|期间'):
        CalculationProgram(compile_inputs(c,ds,params,{},period='2027Q1'),candidate,'2027Q1')


def test_binding_duplicate_grain_is_not_a_model_default():
    c,ds,params,plan,_=fixture()
    c.sheets['S3'].rows.append(('0007','80%'))
    with pytest.raises(ProgramError,match='粒度键.*重复') as exc:
        read_datasets(c,ds,period='202701')
    assert exc.value.dataset=='collection' and exc.value.kind=='duplicate'
    assert exc.value.details['first'] and exc.value.details['second']
    ds['collection']['grain']['duplicates']='identical'
    assert len(read_datasets(c,ds,period='202701')[0]['collection'].rows)==2
    c.sheets['S3'].rows[-1]=('0007','81%')
    with pytest.raises(ProgramError,match='粒度键.*重复'):read_datasets(c,ds,period='202701')


def test_literal_formula_rule_cannot_be_satisfied_by_a_citation_only():
    c,ds,params,plan,_=fixture()
    chunk={'R17':{'file':'policy.md','where':'第1行','text':'奖金总额=净销售额×提成率×回款及时率系数'}}
    proposal={'units':{'R17':{'disposition':'operative','reason':'总额公式','clauses':[{'quote':chunk['R17']['text'],'meaning':'三项乘积','phase':'hold_policy','kind':'formula'}]}}}
    obligations,_=validate_clause_chunk(chunk,proposal);rid='R17:1'
    metrics=plan['metrics'];metrics['gross']['rules']=[rid]
    binding={rid:{'target':'gross','symbols':{'净销售额':ref('sales_net'),'提成率':param('commission'),'回款及时率系数':ref('collect_factor')}}}
    params_read=compile_inputs(c,ds,params,{},period='202701').parameters
    components={'hold_policy':{'metrics':{'gross':metrics['gross']}}}
    assert rid in coverage(obligations,components,phases=['hold_policy'],formula_bindings=binding,metrics=metrics,parameters=params_read)
    metrics['gross']['expr']=op('mul',ref('sales_net'),param('commission'))
    with pytest.raises(ProgramError,match='实际计算树不同'):coverage(obligations,components,phases=['hold_policy'],formula_bindings=binding,metrics=metrics,parameters=params_read)
    binding[rid]['symbols'].pop('回款及时率系数')
    with pytest.raises(ProgramError,match='未绑定符号'):coverage(obligations,components,phases=['hold_policy'],formula_bindings=binding,metrics=metrics,parameters=params_read)


def test_singleton_raw_attributes_and_model_selected_query_operands_are_real_metrics():
    from bonus_agent.adaptive_compiler import source_attributes,bind_formula_atoms,_empty_fragment
    c,ds,params,plan,_=fixture();orders=deepcopy(ds['orders']);orders['sources']=orders['sources'][:1]
    orders['grain']={'kind':'unique','keys':['agreement'],'duplicates':'error','rules':[]}
    scope={'datasets':{'orders':orders},'population':{'dataset':'orders','keys':['agreement'],'where':True,'rules':[]}}
    attrs=source_attributes(scope);raw_id='source:orders:revenue';assert raw_id in attrs
    assert source_attributes({'datasets':{'orders':ds['orders']},'population':scope['population']})=={}
    u={'R1':{'file':'policy','where':'1','text':'计提基数=min(合同额,max(净回款,0))'},
       'R2':{'file':'policy','where':'2','text':'提成金额=计提基数×费率'}}
    ob,_=validate_clause_chunk(u,{'units':{k:{'disposition':'operative','reason':'真实公式','clauses':[{'quote':v['text'],'meaning':'计算','phase':'project_policy','kind':'formula'}]} for k,v in u.items()}})
    read_amount=query('$project','max',fld('revenue'))
    read_rate=query('collection','unique',fld('ratio'),match={'agreement':key('agreement')})
    available={**attrs,'net':{'label':'净回款','expr':ref(raw_id),'rules':[]}}
    fragment=_empty_fragment();fragment['metrics']={'base':{'label':'计提基数','expr':op('min',read_amount,op('max',ref('net'),0)),'rules':['R1:1']},
                                                         'commission':{'label':'提成金额','expr':op('mul',ref('base'),read_rate),'rules':['R2:1']}}
    fragment['formula_bindings']={'R1:1':{'target':'base','symbols':{'合同额':read_amount,'净回款':ref('net'),'0':{'value':0}}},
                                  'R2:1':{'target':'commission','symbols':{'计提基数':ref('base'),'费率':read_rate}}}
    bind_formula_atoms(fragment,available,scope,ob,'project_policy','2027-01')
    assert fragment['formula_bindings']['R1:1']['symbols']=={'合同额':ref(raw_id),'净回款':ref('net')}
    helper=fragment['formula_bindings']['R2:1']['symbols']['费率']['ref'];assert fragment['metrics'][helper]['expr']==read_rate
    metrics={**available,**fragment['metrics']}
    assert len(coverage(ob,{'project_policy':fragment},phases=['project_policy'],formula_bindings=fragment['formula_bindings'],metrics=metrics,keys=['agreement']))==2
    p=CalculationProgram(compile_inputs(c,{'orders':orders,'collection':ds['collection']},{},{},period='202701'),
                         {'population':scope['population'],'metrics':metrics,'funding':{'total':'commission','payable':'commission','alloc':'commission','destinations':{},'labels':{'total':'提成','alloc':'可分配'},'summary_digits':2,'rules':[]}},'2027-01')
    assert p.basis()['pool']==D(1200)


def test_source_coverage_cannot_skip_new_rule_rows():
    c=Catalog([]);c.sheets['S1']=Sheet('S1','novel.xlsx','policy',[('name','value'),('新指标','测试规则'),('结算','测试规则2')])
    inventory={'sheets':{'S1':{'kind':'rule','reason':'制度','rule_ranges':[]}},'unreadable':{}}
    units=rule_units(c,inventory);assert set(units)=={'S1R1','S1R2','S1R3'}
    inventory['sheets']['S1']['rule_ranges']=[[2,2]]
    assert set(rule_units(c,inventory))=={'S1R1','S1R2','S1R3'}
    with pytest.raises(ProgramError,match='逐原文'):validate_clause_chunk(units,{'units':{}})
    with pytest.raises(ProgramError,match='每张真实表'):validate_source_inventory(c,{'sheets':{},'unreadable':{}})


def test_mixed_ledger_side_note_is_addressed_by_actual_cell_not_every_record():
    from bonus_agent.adaptive_rules import source_index, explicit_formulas
    c=Catalog([]);c.sheets['S1']=Sheet('S1','orders.xlsx','orders',[
        ('month','customer','model','price','key','存在未交付的订单，整个订单暂停奖金发放。'),
        (202701,'A1','ABC12+DEF34',120,'202701A1ABC12+DEF34',None),
        (202702,'B8','ABC56+DEF78',150,'202702B8ABC56+DEF78',None)])
    profile=source_index(c)['sheets'][0]
    assert profile['sparse_columns']['F']=={'nonempty_count':1,'cells':[{'cell':'F1','text':'存在未交付的订单，整个订单暂停奖金发放。'}]}
    inv={'sheets':{'S1':{'kind':'mixed','reason':'原始订单及侧栏规则','rule_ranges':[],'rule_cells':['F1']}},'unreadable':{}}
    units=rule_units(c,inv);assert set(units)=={'S1CF1'} and units['S1CF1']['text']==profile['sparse_columns']['F']['cells'][0]['text']
    assert explicit_formulas('202701 | A1 | ABC12+DEF34 | 120 | 202701A1ABC12+DEF34')==[]
    assert explicit_formulas('202701 | A1 | ABC12+DEF34*2 | 未结算，未支付 | 120 | 202701A1ABC12+DEF34*2')==[]
    assert explicit_formulas('净额 | 收入-成本')==['收入-成本']
    assert explicit_formulas('公式 | 1 | 2 | amount2-cost1')==['amount2-cost1']
    assert explicit_formulas('1 | type | 2 | amount2-cost1')==['amount2-cost1']
    inv['sheets']['S1']['rule_cells']=['F2']
    with pytest.raises(ProgramError,match='实际为空'):validate_source_inventory(c,inv)


def test_model_cannot_reclassify_a_record_composite_key_as_formula():
    raw='202701 | A1 | ABC12+DEF34 | 120 | 202701A1ABC12+DEF34'
    u={'S1R2':{'file':'orders.xlsx','where':'orders!2','text':raw}}
    c={'quote':'202701A1ABC12+DEF34','meaning':'实际拼接主键','level':'source','operation':'binding','kind':'formula','formula':'202701A1ABC12+DEF34'}
    ob,_=validate_clause_chunk(u,{'units':{'S1R2':{'disposition':'operative','reason':'实际字段','clauses':[c]}}})
    assert ob['S1R2:1']['formula'] is None


def test_phase_without_actual_business_rules_is_skipped_by_program():
    from bonus_agent.adaptive_compiler import AdaptiveCompiler,_empty_fragment
    def prohibited_request(*args,**kwargs):raise AssertionError('empty stage must not call model')
    compiler=AdaptiveCompiler(None,'2027-01',prohibited_request,lambda *a:None,{})
    for phase in ('project_policy','coefficient_policy','hold_policy'):
        assert compiler._compile_phase(phase,{}, {},{}, {},{},_empty_fragment())==_empty_fragment()


def test_explicit_direct_flow_can_bind_unique_terminal_formula_without_inventing_pool():
    from bonus_agent.adaptive_compiler import direct_funding_contract
    rules={'R1':{'phase':'project_policy','quote':'单笔服务奖金=ROUND(服务金额×费率,2)','formula':'ROUND(服务金额*费率,2)'},
           'R2':{'phase':'roster','operation':'assignment','quote':'每笔服务只归属于一名人员，不设奖金池，不进行权重分配。','formula':None}}
    metrics={'base':{'label':'服务金额','expr':100,'owner':'project_policy','type':'number','rules':['R1']},
             'rate':{'label':'费率','expr':{'num':'0.2'},'owner':'project_policy','type':'number','rules':['R1']},
             'award':{'label':'单笔服务奖金','expr':op('round',op('mul',ref('base'),ref('rate')),2),'owner':'project_policy','type':'number','rules':['R1']}}
    combined={'metrics':metrics,'formula_bindings':{'R1':{'target':'award','symbols':{'服务金额':ref('base'),'费率':ref('rate')}}}}
    funding,proof=direct_funding_contract({},rules,combined)
    assert funding['total']==funding['payable']==funding['alloc']=='award' and funding['destinations']=={} and funding['summary_digits']==2
    assert set(proof['dependencies'])=={'award','base','rate'}
    assert direct_funding_contract({'hold-rule':{}},rules,combined) is None
    broken=deepcopy(combined);broken['metrics']['other']={'label':'另外奖金','expr':7,'owner':'project_policy','type':'number','rules':['R1']}
    assert direct_funding_contract({},rules,broken) is None
    assert direct_funding_contract({}, {'R1':rules['R1']},combined) is None


def test_final_review_sees_full_small_population_and_real_identity_rule():
    from bonus_agent.adaptive_compiler import AdaptiveCompiler
    from bonus_agent.commission_chain import _settlement_counterexample_issue
    source={'file':'identity-policy.docx','where':'1'}
    rule={'quote':'人员姓名允许重名，必须按唯一员工编号区分。','source':source}
    basis={'projects':{'A':{'alloc':D(10)},'B':{'alloc':D(20)}},'funding':{},'rule_coverage':{'R1:1':{'clause':rule}}}
    rows=[{'project':p,'role_id':'direct','destination':'person','share':D(1),'person_id':i,'name':'同名人员','raw':a} for p,i,a in [('A','0001',D(10)),('B','0002',D(20))]]
    people=[{'id':r['person_id'],'name':r['name'],'destination':'person','attributed':r['raw'],'entitled':r['raw'],'amount':r['raw'],'destinations':{}} for r in rows]
    settlement={'rows':rows,'people':people,'rounding':{'when':'none','digits':2},'rule_coverage':{},'checks':{'projects':2},
                'person_basis':{'funding':{}},'person_plan':{'identity_key':'employee_id','program':{'population':{'dataset':'staff'}}},'funding_bridge':{'approved_basis':D(30)}}
    payload,prompt=AdaptiveCompiler(None,'2027-01',None,None,{}).settlement_review(basis,settlement)
    assert payload['review_scope']['full_project_population_in_payload'] and len(payload['complete_projects'])==2
    assert payload['review_scope']['full_person_population_in_payload'] and len(payload['personal_examples'])==2
    assert payload['identity_source_rules']['R1:1']['quote']==rule['quote']
    assert payload['identity_checks']['unique_ids']==2 and payload['identity_checks']['same_display_name_distinct_ids']=={'同名人员':['0001','0002']}
    assert _settlement_counterexample_issue(payload,{'status':'REVISE','reason':'重名即冲突'})
    bad={'counterexample':{'rule_id':'R1:1','quote':'姓名不得重名','object':'0001','target':'name','actual':'甲','expected':'乙'}}
    assert _settlement_counterexample_issue(payload,bad)


def test_explicit_global_layout_and_unit_are_not_optional_context():
    raw='各数据表第 3 行为表头，第 4 行起为数据；金额单位统一为万元。'
    units={'R1':{'file':'new-layout.md','where':'第2行','text':raw}}
    with pytest.raises(ProgramError,match='物理绑定'):
        validate_clause_chunk(units,{'units':{'R1':{'disposition':'context','reason':'文件说明','clauses':[]}}})
    proposal={'units':{'R1':{'disposition':'operative','reason':'实际布局','clauses':[{'quote':'各数据表第 3 行为表头，第 4 行起为数据','meaning':'实际行范围','phase':'scope_mapping','kind':'policy'}]}}}
    with pytest.raises(ProgramError,match='万元'):
        validate_clause_chunk(units,proposal)
    proposal['units']['R1']['clauses'].append({'quote':'金额单位统一为万元','meaning':'人民币万元','phase':'scope_mapping','kind':'policy'})
    ob,_=validate_clause_chunk(units,proposal)
    assert {a['kind'] for c in ob.values() for a in c.get('binding_assertions',[])}=={'header_row','data_start','money_unit'}


def test_report_display_unit_is_not_a_raw_amount_conversion():
    raw='输入数据金额单位统一为元；输出汇总表的金额单位统一为万元，以便展示。'
    units={'R1':{'file':'report-rule.md','where':'第2行','text':raw}}
    proposal={'units':{'R1':{'disposition':'operative','reason':'输入与报表各自单位','clauses':[
        {'quote':'输入数据金额单位统一为元','meaning':'原始元','phase':'scope_mapping','kind':'policy'},
        {'quote':'输出汇总表的金额单位统一为万元，以便展示','meaning':'报表万元展示','phase':'export','kind':'output'}]}}}
    ob,_=validate_clause_chunk(units,proposal)
    assert [a['value'] for c in ob.values() for a in c.get('binding_assertions',[])]==['raw']


def test_literal_formula_boundary_does_not_absorb_following_explanation():
    # Counterexample from the actual V5 independent model run, generalized
    # to any balanced function and trailing prose.
    from bonus_agent.adaptive_rules import explicit_formulas
    text='预发合计 = 所有预发记录之和；本次实发 = max(应得提成 − 预发合计, 0)。超预发登记 = max(预发合计 − 应得提成, 0)，只登记，不产生负数发放。'
    assert explicit_formulas(text)==['max(应得提成-预发合计,0)','max(预发合计-应得提成,0)']
    unit={'R1':{'file':'doc','where':'row','text':text}}
    clauses=[{'quote':'预发合计 = 所有预发记录之和','meaning':'实际预发汇总','phase':'roster','kind':'formula','formula':'sum(预发记录)'},
             {'quote':'本次实发 = max(应得提成 − 预发合计, 0)','meaning':'实发','phase':'roster','kind':'formula','formula':'MAX(应得提成-预发合计,0)'},
             {'quote':'超预发登记 = max(预发合计 − 应得提成, 0)','meaning':'超发','phase':'roster','kind':'formula','formula':'MAX(预发合计-应得提成,0)'}]
    obligations,_=validate_clause_chunk(unit,{'units':{'R1':{'disposition':'operative','reason':'实际规则','clauses':clauses}}})
    assert obligations['R1:1']['kind']=='policy' and obligations['R1:1']['formula'] is None
    assert obligations['R1:3']['formula']=='max(预发合计-应得提成,0)'


def test_category_footnotes_and_identifier_predicates_are_not_arithmetic():
    from bonus_agent.adaptive_rules import explicit_formulas,matrix_header
    assert explicit_formulas('| 新类别 | 旧类别*')==[]
    assert matrix_header(['','新类别','旧类别*'],[['负责人',25,15],['其他',35,40]])
    assert not matrix_header(['','金额×费率','另项'],[['负责人',25,15]])
    assert explicit_formulas('区域指定 | 当区域=GROUP-A,按另一个区域归类')==[]
    assert explicit_formulas('每个合同一行（合同=月份+客户编号+产品编码）')==[]
    assert explicit_formulas('项目编号=月份+客户编号+产品编码')==[]
    assert explicit_formulas('计算金额=销售额×(费率-0.05)')==['销售额*(费率-0.05)']
    assert explicit_formulas('净额 | 收入-成本')==['收入-成本']
    # A model-proposed formula cannot reintroduce the false conditional RHS.
    u={'R1':{'file':'rules.txt','where':'1','text':'区域指定 | 当区域=GROUP-A,按另一个区域归类'}}
    c={'quote':u['R1']['text'],'meaning':'实际区域映射','level':'primary','operation':'adjustment','kind':'formula','formula':'GROUP-A'}
    ob,_=validate_clause_chunk(u,{'units':{'R1':{'disposition':'operative','reason':'条件规则','clauses':[c]}}})
    assert ob['R1:1']['formula'] is None and ob['R1:1']['kind']=='policy'


def test_source_line_recovers_verbatim_mixed_script_without_fuzzy_inference():
    text='类别说明\n如負責人直接负责的客户，分配40%。\n其他条件使用60%。'
    u={'R1':{'file':'rules.pptx','where':'1','text':text}}
    c={'quote':'如负责人直接負責的客戶，分配40%。','meaning':'责任人归属','level':'person','operation':'assignment','kind':'policy'}
    proposal={'units':{'R1':{'disposition':'operative','reason':'原件规则','clauses':[c]}}}
    with pytest.raises(ProgramError,match='实际连续文字') as exc:validate_clause_chunk(u,proposal)
    assert exc.value.details['source_lines']['2']=='如負責人直接负责的客户，分配40%。'
    c.pop('quote');c['source_line']=2
    ob,_=validate_clause_chunk(u,proposal);assert ob['R1:1']['quote']==text.splitlines()[1]
    c['quote']='其他条件使用60%。'
    with pytest.raises(ProgramError,match='实际连续文字'):validate_clause_chunk(u,proposal)
    c.pop('quote');c['source_line']=4
    with pytest.raises(ProgramError,match='有效行号'):validate_clause_chunk(u,proposal)
    c['source_line']=2;c['kind']='parameter'
    with pytest.raises(ProgramError,match='kind=.*parameter'):validate_clause_chunk(u,proposal)


def test_merged_sources_explicitly_preserve_physically_missing_nullable_fields():
    cat=Catalog([])
    cat.sheets={'S1':Sheet('S1','early.xlsx','data',[('单号','金额'),('001',100)]),
                'S2':Sheet('S2','late.xlsx','data',[('单号','金额','回款状态'),('002',50,'未回款')])}
    ds={'sales':{'label':'多来源销售','fields':{'id':{'kind':'id','nullable':False},'amount':{'kind':'money','nullable':False},'paid':{'kind':'text','nullable':True,'label':'回款状态'}},
                  'sources':[{'sheet':'S1','header_row':1,'columns':{'id':'A','amount':'B'}},
                             {'sheet':'S2','header_row':1,'columns':{'id':'A','amount':'B','paid':'C'}}],
                  'grain':{'kind':'unique','keys':['id'],'duplicates':'error','rules':[]},'rules':[]}}
    with pytest.raises(ProgramError,match='未绑定.*paid'):read_datasets(cat,ds,period='2027Q1')
    absence={'reason':'早期原件实际没有回款状态列','rules':[]}
    ds['sales']['sources'][0]['absent_fields']={'paid':absence}
    actual,diag=read_datasets(cat,ds,period='2027Q1')
    assert [r.values['paid'] for r in actual['sales'].rows]==[None,'未回款'] and diag['sales']['null_counts']['paid']==1
    wrong=deepcopy(ds);wrong['sales']['sources'][1]['columns'].pop('paid');wrong['sales']['sources'][1]['absent_fields']={'paid':absence}
    with pytest.raises(ProgramError,match='实际存在已知表头'):read_datasets(cat,wrong,period='2027Q1')
    wrong=deepcopy(ds);wrong['sales']['fields']['paid']['nullable']=False
    with pytest.raises(ProgramError,match='必须可空'):read_datasets(cat,wrong,period='2027Q1')
    wrong=deepcopy(ds);wrong['sales']['fields']['computed_status']={'kind':'bool','nullable':True}
    for src in wrong['sales']['sources']:
        src.setdefault('absent_fields',{})['computed_status']={'reason':'由金额派生，无物理列','rules':[]}
    with pytest.raises(ProgramError,match='在所有来源均声明缺列'):read_datasets(cat,wrong,period='2027Q1')


def test_actual_financial_authority_routes_by_confirmed_owner_not_model_guess():
    raw='本批风险快照按相同来源，客户月平均出货累计至项目实际月份。'
    u={'Rnew':{'file':'财务正式规则修订','where':'v1','text':raw,'authority':{'owner':'coefficient_policy'}}}
    c={'quote':raw,'meaning':'实际项目月平均','kind':'policy','level':'source','operation':'parameter'}
    r={'units':{'Rnew':{'disposition':'operative','reason':'真实本批财务确认','clauses':[c]}}}
    out,_=validate_clause_chunk(u,r)
    assert out['Rnew:1']['phase']=='coefficient_policy' and out['Rnew:1']['level']=='primary'
    # Formal routing does not excuse unsupported monetary/source formulas.
    u['Rnew']['authority']['owner']='reference_mapping';u['Rnew']['text']='总额=数量×费率';c['quote']=u['Rnew']['text'];c['kind']='formula'
    with pytest.raises(ProgramError,match='不能仅绑定数据源'):validate_clause_chunk(u,r)


def test_secondary_roster_scope_is_not_primary_population_and_reference_closure_is_structural():
    from bonus_agent.adaptive_rules import primary_population_invariants
    u={'R1':{'file':'rules','where':'1','text':'分配名单只取实际核算月的记录。'},
       'R2':{'file':'rules','where':'2','text':'参考数据中有、但订单中不存在的记录不参与核算。'}}
    p={'units':{k:{'disposition':'operative','reason':'实际业务义务','clauses':[{'quote':v['text'],'meaning':v['text'],'level':'source','operation':'population','kind':'policy'}]} for k,v in u.items()}}
    ob,_=validate_clause_chunk(u,p)
    assert ob['R1:1']['phase']=='roster' and ob['R1:1']['operation']=='binding'
    assert primary_population_invariants(ob)==['R2:1']


def test_metric_parameter_and_role_share_policies_are_not_physical_bindings():
    examples=[
        ('合同质量系数使用质量对应的系数表。','primary','binding','coefficient_policy'),
        ('每个订单按规则去参考数据里取对应系数。','source','binding','coefficient_policy'),
        ('阶梯提成按合同的计算基数算。','primary','binding','project_policy'),
        ('奖励点数确认，以每笔交易的销售额为依据。','source','binding','project_policy'),
        ('角色份额未标单位时默认按百分数；表头明示单位时按明示值。','source','parameter','roster'),
        ('按原表字段名关联客户编码。','source','binding','reference_mapping')]
    for text,level,operation,phase in examples:
        u={'R1':{'file':'policy','where':'row','text':text}}
        c={'quote':text,'meaning':text,'kind':'policy','level':level,'operation':operation}
        ob,_=validate_clause_chunk(u,{'units':{'R1':{'disposition':'operative','reason':'实际原文','clauses':[c]}}})
        assert ob['R1:1']['phase']==phase,(text,ob)
    text='销售额阶梯(RMB/月) | 新合同 | 旧合同'
    u={'T1R1':{'file':'policy','where':'P1','text':text,'table':'T1','row':1}}
    c={'quote':text,'meaning':'区间单位','kind':'table','level':'source','operation':'parameter'}
    ob,_=validate_clause_chunk(u,{'units':{'T1R1':{'disposition':'operative','reason':'实际制度参数','clauses':[c]}}})
    assert ob['T1R1:1']['phase']=='project_policy'


def test_reference_only_records_are_not_required_reverse_foreign_keys():
    from bonus_agent.adaptive_compiler import normalize_reference_relationships
    from bonus_agent.adaptive_sources import check_relationships
    scope={'population':{'dataset':'orders'}}
    reference={'datasets':{'budget':{'purpose':'lookup','relationships':[{'fields':['region'],'target':'orders','target_fields':['region'],'cardinality':'at_least_one','rules':['R1']}]}}}
    ob={'R1':{'phase':'scope_mapping','operation':'population','quote':'参考表中有但订单中不存在的对象不参与核算。'}}
    changes=normalize_reference_relationships(scope,reference,ob)
    assert len(changes)==1 and reference['datasets']['budget']['relationships'][0]['cardinality']=='optional_many'
    datasets={'orders':Dataset({'region':{}},(Record({'region':'A'},'sale1'),Record({'region':'A'},'sale2'))),
              'budget':Dataset({'region':{}},(Record({'region':'A'},'budget1'),Record({'region':'B'},'budget2')))}
    proof=check_relationships(datasets,reference['datasets'])
    assert proof['budget:0']['matched']==1 and proof['budget:0']['optional_missing']==1
    with pytest.raises(ProgramError,match='缺少明确允许'):normalize_reference_relationships(scope,reference,{})


def test_redundant_structural_check_normalization_requires_exact_typed_evidence():
    from bonus_agent.adaptive_compiler import normalize_reference_relationships,normalize_reference_checks
    scope={'population':{'dataset':'orders'},'datasets':{'orders':{'fields':{'id':{}},'grain':{'kind':'detail','keys':[]}}}}
    lookup={'purpose':'lookup','fields':{'id':{},'quality':{'nullable':True,'rules':['R3']}},
         'sources':[{'columns':{'id':'A','quality':'B'}}],
         'grain':{'kind':'unique','keys':['id'],'duplicates':'error','rules':['R2']},'rules':[],
         'relationships':[{'fields':['id'],'target':'orders','target_fields':['id'],'cardinality':'at_least_one','rules':['R1']}]}
    missing=op('count',op('filter',{'dataset':'lookup'},op('is_missing',fld('quality'))))
    fk=op('eq',op('count',op('filter',{'dataset':'lookup'},op('not',op('exists',{'dataset':'orders','where':op('eq',fld('orders.id'),fld('lookup.id'))})))),0)
    unique=op('eq',op('count',op('distinct',op('tuple',fld('id')))),op('count'))
    checks=[{'id':'fk','expr':fk,'message':'原关联缺失','rules':['R1']},
            {'id':'unique','expr':unique,'message':'不重复','rules':['R2']},
            {'id':'null','expr':op('eq',missing,missing),'message':'空值保留','rules':['R3']},
            {'id':'real','expr':op('gt',ref('computed'),0),'message':'实际业务计算条件','rules':['R2']}]
    v={'datasets':{'lookup':lookup},'checks':checks}
    ob={'R1':{'phase':'scope_mapping','operation':'population','quote':'参考表中有但订单中不存在的对象不参与核算。'},'R2':{'quote':'唯一编号'},'R3':{'quote':'空值保留为空，不替换为零。'}}
    normalize_reference_relationships(scope,v,ob)
    proofs=normalize_reference_checks(scope,v,ob)
    assert {x['proof']['kind'] for x in proofs}=={'physical_relationship','physical_unique_grain','physical_null_preservation'}
    assert [x['id'] for x in v['checks']]==['real']
    frozen=deepcopy(v);normalize_reference_checks(scope,v,ob);assert frozen==v
    # A different field relation is not silently treated as the validated one.
    changed=deepcopy(frozen);changed['datasets']['lookup']['relationships'][0]['target_fields']=['other']
    normalize_reference_checks(scope,changed,ob)
    assert 'fk' in [x['id'] for x in changed['checks']]


def test_literal_join_pair_uses_real_duplicate_header_occurrence():
    from bonus_agent.adaptive_compiler import normalize_reference_relationships
    c=Catalog([]);c.sheets={
        'S1':Sheet('S1','orders.xlsx','orders',[('简称','名称','简称'),('old','legal','actual')]),
        'S2':Sheet('S2','budget.xlsx','budget',[('名称','预算'),('actual',100)])}
    main={'sources':[{'sheet':'S1','header_row':1,'columns':{'old_alias':'A','legal_name':'B','current_alias':'C'}}]}
    lookup={'purpose':'lookup','sources':[{'sheet':'S2','header_row':1,'columns':{'label':'A','budget':'B'}}],
            'relationships':[{'fields':['label'],'target':'orders','target_fields':['legal_name'],'cardinality':'at_least_one','rules':['R2']}]}
    scope={'population':{'dataset':'orders'},'datasets':{'orders':main}}
    v={'datasets':{'budget':lookup}}
    rules={'R1':{'phase':'scope_mapping','operation':'population','quote':'参考表中有但订单中不存在的对象不参与核算。'},
           'R2':{'quote':'对应月份，简称2与名称，按实际金额和预算计算。'}}
    changes=normalize_reference_relationships(scope,v,rules,catalog=c)
    assert lookup['relationships'][0]['target_fields']==['current_alias']
    assert lookup['relationships'][0]['cardinality']=='optional_many'
    assert any(ch.get('field')=='label' and ch['target_field']=='current_alias' for ch in changes)


def test_native_interval_schedules_are_compiled_by_actual_axis_not_copied_rates():
    from bonus_agent.commission_sources import ParameterTable
    from bonus_agent.adaptive_compiler import AdaptiveCompiler,native_interval_layouts,normalize_calculation_phases
    from bonus_agent.adaptive_sources import read_schedules
    c=Catalog([]);c.tables['T1']=ParameterTable('T1','rate.pptx',1,1,'交易额阶梯提成',
        [['金额区间','新合同','老合同'],['100元以下','1%','0.5%'],['100元以上','2%','1%']])
    c.tables['T2']=ParameterTable('T2','rate.pptx',2,1,'质量毛利率系数',
        [['毛利率范围','质量系数'],['10%以下','0.7'],['10%以上','1.2']])
    tables=native_interval_layouts(c)
    assert set(tables)=={'T1','T2'} and len(tables['T1']['forms'])==2
    units={'T1R2':{'table':'T1','text':'100元以下 | 1% | 0.5%'},
           'R1':{'text':'按超额累进方式计算阶梯提成，负基数按阶梯照算。'},
           'T2R2':{'table':'T2','text':'10%以下 | 0.7'},
           'R2':{'text':'奖金总额 | 阶梯金额×质量系数'}}
    ob={'T1R2:1':{'unit':'T1R2','quote':units['T1R2']['text'],'meaning':'真实费率','phase':'project_policy','level':'primary','operation':'parameter'},
        'R1:1':{'unit':'R1','quote':units['R1']['text'],'meaning':'累计规则','phase':'project_policy','level':'primary','operation':'calculation'},
        'T2R2:1':{'unit':'T2R2','quote':units['T2R2']['text'],'meaning':'系数','phase':'project_policy','level':'primary','operation':'parameter'},
        'R2:1':{'unit':'R2','quote':units['R2']['text'],'meaning':'总额','phase':'project_policy','level':'primary','operation':'calculation'}}
    for rid,r in ob.items():r['kind']='table' if rid.startswith('T') else 'policy'
    normalize_calculation_phases(c,units,ob)
    assert ob['T2R2:1']['phase']=='coefficient_policy' and ob['R2:1']['phase']=='hold_policy'
    def request(name,data,prompt,validate,**kw):
        raise AssertionError('已审核的明确累进口径与物理格无需再请求模型')
    compiler=AdaptiveCompiler(c,'2027-01',request,lambda *a:None,{})
    fragment,covered=compiler._compile_native_schedules('project_policy',{k:v for k,v in ob.items() if v['phase']=='project_policy'},units)
    assert covered=={'T1R2:1'} and len(fragment['schedules'])==2
    schedules,_=read_schedules(c,fragment['schedules'])
    assert all(s.method=='progressive' and s.negative=='signed' for s in schedules.values())
    assert schedules['interval_T1_c2'].steps==((D(0),D('.01'),True),(D(100),D('.02'),True))
    assert compiler.state['receipts']['adaptive/project_policy/schedule/T1']['reviewer']=='程序'


def test_actual_detail_attribute_uniqueness_is_not_amount_aggregation():
    from bonus_agent.adaptive_compiler import source_attributes,source_aggregates,_compile_prefix,_empty_fragment
    c=Catalog([]);c.sheets['S1']=Sheet('S1','detail.xlsx','detail',[('合同','币种','汇率','金额','记录'),('A','USD',7,100,'x'),('A','USD',7,120,'y'),('B','RMB',1,200,'z')])
    ds={'label':'原始明细','fields':{'id':{'kind':'id','nullable':False},'currency':{'kind':'text','nullable':False},'fx':{'kind':'number','nullable':False},'amount':{'kind':'money','nullable':False},'row_id':{'kind':'id','nullable':False}},
        'sources':[{'sheet':'S1','header_row':1,'columns':{'id':'A','currency':'B','fx':'C','amount':'D','row_id':'E'}}],
        'grain':{'kind':'detail','keys':[],'duplicates':'error','rules':[]},'rules':[]}
    scope={'datasets':{'detail':ds},'population':{'dataset':'detail','keys':['id'],'where':True,'rules':[]}}
    p=_compile_prefix(c,scope['datasets'],scope['population'],_empty_fragment(),'2027-01',calculate=False)
    attrs=source_attributes(scope,p)
    assert set(attrs)=={'source:detail:currency','source:detail:fx'}
    assert all(v['expr']['query']['duplicates']=='identical' for v in attrs.values())
    sums=source_aggregates(c,scope,{'R1':{'phase':'scope_mapping','quote':'先合计每个合同的金额。'}})
    assert set(sums)=={'source_sum:detail:amount'} and sums['source_sum:detail:amount']['expr']['query']['aggregate']=='sum'
    assert source_aggregates(c,scope,{'R1':{'phase':'scope_mapping','quote':'每个合同的金额从原件读取。'}})=={}


def test_progressive_policy_rejects_whole_rate_times_base():
    from bonus_agent.adaptive_compiler import verify_progressive_amounts
    rules={'R1':{'quote':'实际阶梯金额按超额累进计算。'}}
    schedules={'actual':{'method':'progressive','rules':['R1']}}
    bad={'amount':{'expr':op('mul',ref('base'),{'tier':{'schedule':'actual','value':ref('base'),'output':'rate'}}),'rules':['R1']},
         'base':{'expr':100,'rules':[]}}
    with pytest.raises(ProgramError,match='不能仅取整档费率'):verify_progressive_amounts(bad,schedules,rules,financial_ids={'amount','base'})
    good=deepcopy(bad);good['amount']['expr']={'tier':{'schedule':'actual','value':ref('base'),'output':'amount'}}
    verify_progressive_amounts(good,schedules,rules,financial_ids={'amount','base'})


def test_category_tiers_require_exact_observed_category_match():
    from bonus_core.engine.adaptive import Schedule
    c,ds,params,plan,_=fixture()
    inputs=compile_inputs(c,ds,params,{},period='202701')
    inputs.schedules={'north':Schedule(((D(0),D('.1'),True),),method='progressive'),
                      'south':Schedule(((D(0),D('.2'),True),),method='progressive')}
    selection={'tier':{'selectors':[key('office')],
                        'cases':[{'values':['N'],'schedule':'north'},{'values':['S'],'schedule':'south'}],
                        'value':ref('sales_net'),'output':'amount'}}
    plan['metrics']['gross']['expr']=selection
    p=CalculationProgram(inputs,deepcopy(plan),'2027-01');b=p.basis()
    amounts={v['key_values']['office']:v['total'] for v in b['projects'].values()}
    assert amounts=={'N':D('120'),'S':D('160')}
    broken=deepcopy(plan);broken['metrics']['gross']['expr']['tier']['cases'][0]['values']=['North office table']
    with pytest.raises(ProgramError,match='分类阶梯未覆盖实际取值'):
        CalculationProgram(inputs,broken,'2027-01').basis()
    broken=deepcopy(plan);broken['metrics']['gross']['expr']['tier']['cases'][1]['values']=['N']
    with pytest.raises(ProgramError,match='分类阶梯.*重复'):
        CalculationProgram(inputs,broken,'2027-01')


def test_regional_progressive_policy_requires_explicit_dispatch_not_fallback():
    from bonus_agent.adaptive_compiler import verify_progressive_amounts
    rules={'R1':{'quote':'按区域对应超额累进阶梯比例计算'}}
    schedules={s:{'method':'progressive','layout':{'table':s},'rules':['R1']} for s in ('A','B')}
    tier=lambda s:{'tier':{'schedule':s,'value':ref('base'),'output':'amount'}}
    bad={'base':{'expr':100,'rules':[]},'amount':{'expr':op('if',op('eq',key('office'),'North office table'),tier('A'),tier('B')),'rules':['R1']}}
    with pytest.raises(ProgramError,match='显式分类阶梯'):
        verify_progressive_amounts(bad,schedules,rules,financial_ids={'amount','base'})
    good=deepcopy(bad);good['amount']['expr']={'tier':{'selectors':[key('office')],'cases':[{'values':['N'],'schedule':'A'},{'values':['S'],'schedule':'B'}],'value':ref('base'),'output':'amount'}}
    verify_progressive_amounts(good,schedules,rules,financial_ids={'amount','base'})


def test_literal_category_reassignment_uses_physical_field_and_exact_literal():
    from bonus_agent.adaptive_compiler import literal_category_rules,_compile_prefix,_empty_fragment
    c,ds,params,plan,_=fixture()
    # Actual field names differ from the semantic IDs of the contract.
    for sheet in ('S1','S2'):
        src=c.sheets[sheet];i=0 if sheet=='S1' else 1
        src.rows[i]=tuple({'Office':'部门','Agreement':'客户','Contract':'客户'}.get(x,x) for x in src.rows[i])
    scope={'datasets':{'orders':ds['orders']},'population':plan['population']}
    rules={'R1':{'phase':'project_policy','quote':'当部门=N，客户不等于0008，归类部门为A'}}
    metrics,covered=literal_category_rules(c,scope,rules,{})
    assert covered=={'R1'}
    f=_empty_fragment();f['metrics'].update(metrics)
    p=_compile_prefix(c,scope['datasets'],scope['population'],f,'2027-01')
    assert {v['key_values']['agreement']:v['metrics']['source_category:orders:office'] for v in p.basis()['projects'].values()}=={'0007':'A','0008':'S'}
    rules['R2']={'phase':'project_policy','quote':'当部门=N，归类部门为B'}
    assert literal_category_rules(c,scope,rules,{})==({},set())


def test_compound_source_amount_name_and_literal_citation_are_not_new_operands():
    from bonus_agent.adaptive_compiler import normalize_calculation_phases
    units={'R1':{'text':'每个项目都先算「减值+返还」=基数×(1−比率)（补充规则说明第13行）'},
           'R2':{'text':'可分配=基数−减值+返还'}}
    ob={'R1:1':{'unit':'R1','quote':units['R1']['text'],'formula':'基数*(1-比率)(补充规则说明第13行)','phase':'hold_policy'},
        'R2:1':{'unit':'R2','quote':units['R2']['text'],'formula':'基数-减值+返还','phase':'hold_policy'}}
    normalize_calculation_phases(Catalog([]),units,ob)
    assert ob['R2:1']['defined_formula_symbols']=={'减值+返还':'R1:1'}
    assert parse_literal_formula(ob['R1:1']['formula'],{'基数':ref('base'),'比率':ref('rate')})==op('mul',ref('base'),op('sub',{'num':'1'},ref('rate')))
    metrics={k:{'label':k,'expr':v,'rules':r} for k,v,r in [('base',100,[]),('rate',{'num':'.5'},[]),('combined',op('mul',ref('base'),op('sub',1,ref('rate'))),['R1:1']),('pay',op('sub',ref('base'),ref('combined')),['R2:1'])]}
    bindings={'R1:1':{'target':'combined','symbols':{'基数':ref('base'),'比率':ref('rate')}},'R2:1':{'target':'pay','symbols':{'基数':ref('base'),'减值+返还':ref('combined')}}}
    coverage(ob,{'hold_policy':{'metrics':metrics}},phases=['hold_policy'],formula_bindings=bindings,metrics=metrics)
    bindings['R2:1']['symbols']={'基数':ref('base'),'减值':ref('combined'),'返还':ref('rate')}
    metrics['pay']['expr']=op('add',op('sub',ref('base'),ref('combined')),ref('rate'))
    with pytest.raises(ProgramError,match='定义为一个完整金额指标'):
        coverage(ob,{'hold_policy':{'metrics':metrics}},phases=['hold_policy'],formula_bindings=bindings,metrics=metrics)


def test_explicit_period_average_uses_activity_months_and_never_future_months():
    from bonus_agent.adaptive_compiler import source_period_averages,_compile_prefix,_empty_fragment
    c=Catalog([]);c.sheets['S1']=Sheet('S1','detail.xlsx','detail',[('月份','客户','金额'),('202701','A',100),('202702','A',-20),('202703','A',0),('202703','B',50)])
    ds={'label':'真实明细','fields':{'month':{'kind':'month','nullable':False},'customer':{'kind':'text','nullable':False},'amount':{'kind':'money','nullable':False}},
        'sources':[{'sheet':'S1','header_row':1,'columns':{'month':'A','customer':'B','amount':'C'}}],
        'grain':{'kind':'detail','keys':[],'duplicates':'error','rules':[]},'rules':[]}
    scope={'datasets':{'detail':ds},'population':{'dataset':'detail','keys':['month','customer'],'where':op('ne',fld('amount'),0),'rules':[]}}
    rules={'R1':{'phase':'scope_mapping','quote':'先合计每个项目的金额。'},
           'R2':{'phase':'coefficient_policy','quote':'月平均出货：该客户本年1月到本月出货金额合计除以实际有出货的月份数。'},
           'R3':{'phase':'coefficient_policy','quote':'对应客户，计算库存金额与客户月平均金额之比。'}}
    period='2027-01~2027-03'
    p=_compile_prefix(c,scope['datasets'],scope['population'],_empty_fragment(),period,calculate=False)
    metrics,covered=source_period_averages(c,scope,rules,period,p)
    assert covered=={'R2'} and len(metrics)==1
    mid=next(iter(metrics));fragment=_empty_fragment();fragment['metrics']=metrics
    b=_compile_prefix(c,scope['datasets'],scope['population'],fragment,period).basis()
    assert {tuple(x['key_values'].values()):x['metrics'][mid] for x in b['projects'].values()}=={('202701','A'):D(100),('202702','A'):D(40),('202703','B'):D(50)}
    assert source_period_averages(c,scope,rules,'2027-02~2027-03',p)==({},set())
    # A real typed month field accepts the aligned period-date anchor.
    fragment['metrics']['count']={'label':'活动月份数','expr':{'query':{'dataset':'$population','aggregate':'count_distinct','match':{'customer':key('customer')},'where':op('ge',fld('month'),{'period':'year_start'}),'value':fld('month'),'empty':'zero','nulls':'error'}},'rules':[]}
    _compile_prefix(c,scope['datasets'],scope['population'],fragment,period)


def test_query_cache_preserves_exact_arithmetic_and_actual_trace_per_context():
    from bonus_core.engine.adaptive import Context
    c,ds,parameters,plan,_=fixture()
    p=CalculationProgram(compile_inputs(c,ds,parameters,{},period='2027-01'),plan,'2027-01')
    p.prepare_population()
    q={'dataset':'$population','aggregate':'sum','match':{'office':{'key':'office'}},'value':{'field':'revenue'},'empty':'zero','nulls':'error'}
    a=Context(keys={'office':'N','irrelevant':'p1'},trace=[]);b=Context(keys={'office':'N','irrelevant':'p2'},trace=[])
    first=p.query(q,a);size=len(p._query_cache)
    assert p.query(q,b)==first and len(p._query_cache)==size and a.trace==b.trace
    p.query(q,Context(keys={'office':'N'},trace=None),exact=True)
    assert len(p._query_cache)>size
    assert p.query(q,Context(keys={'office':'S'}))!=first
    p.prepare_population();assert not p._query_cache


def test_literal_exclusion_and_nonzero_counterexample_on_real_source_coordinates():
    from bonus_agent.adaptive_compiler import apply_literal_source_exclusions,validate_explicit_zero_exclusion,_compile_prefix,_empty_fragment
    c=Catalog([]);c.sheets={
        'S1':Sheet('S1','early.xlsx','data',[('单号','结算额(元)','过滤状态'),('a',100,None),('b',-30,None),('c',70,'排除'),('d',0,None)]),
        'S2':Sheet('S2','late.xlsx','data',[('单号','结算额(元)','判断'),('e',20,20),('f',15,'排除')])}
    ds={'label':'明细','fields':{'id':{'kind':'id','nullable':False},'amount':{'kind':'money','nullable':False}},
        'sources':[{'sheet':s,'header_row':1,'columns':{'id':'A','amount':'B'}} for s in c.sheets],
        'grain':{'kind':'detail','keys':[],'duplicates':'error','rules':[]},'rules':[]}
    scope={'datasets':{'orders':ds},'population':{'dataset':'orders','keys':['id'],'where':op('ne',fld('amount'),0),'rules':['R0']}}
    rules={'R0':{'quote':'结算额为0，不计算。','phase':'scope_mapping'},
           'R1':{'quote':'「过滤状态」列值为「排除」的行不参与核算；「判断」列值为「排除」的行不参与核算。','phase':'scope_mapping'}}
    assert apply_literal_source_exclusions(c,scope,rules)==['R1','R1']
    before=deepcopy(scope);apply_literal_source_exclusions(c,scope,rules);assert scope==before
    p=_compile_prefix(c,scope['datasets'],scope['population'],_empty_fragment(),'2027-01')
    validate_explicit_zero_exclusion(c,scope,rules,p)
    assert {r.values['id'] for r in p._population_rows}=={'a','b','e'}
    scope['population']['where']=op('ne',fld('amount'),op('abs',fld('amount')))
    p=_compile_prefix(c,scope['datasets'],scope['population'],_empty_fragment(),'2027-01')
    with pytest.raises(ProgramError,match='不等价于非零'):validate_explicit_zero_exclusion(c,scope,rules,p)


def test_review_patch_is_atomic_and_bound_to_exact_reviewed_candidate():
    from bonus_agent.commission_chain import apply_review_patches,archived_candidates
    from bonus_agent.recovery import fingerprint
    base={'datasets':{'orders':{'sources':[{'sheet':'S1','columns':{'id':'A'}}]}},'population':{'rules':['R1']}}
    patches=[{'op':'set','path':['datasets','orders','sources',0,'columns','amount'],'value':'B'}]
    merged=apply_review_patches(base,patches,owner='scope_mapping')
    assert merged['datasets']['orders']['sources'][0]['sheet']=='S1' and 'amount' not in base['datasets']['orders']['sources'][0]['columns']
    with pytest.raises(ProgramError,match='无法定位'):apply_review_patches(base,patches+[{'op':'remove','path':['missing','field']}],owner='scope_mapping')
    assert 'amount' not in base['datasets']['orders']['sources'][0]['columns']
    actual={'status':'REVISE','patches':patches}
    pairs=archived_candidates([{'node':'x/解析','status':'PASS','result':{'older':True}},
        {'node':'x/独立复核','result':actual,'review_base':base,'review_base_digest':fingerprint(base)}],'x')
    assert pairs==[(base,actual)]
    nested={'schedules':{'rate':{'includes':[False,False]}}}
    assert apply_review_patches(nested,[{'op':'set','path':['rate','includes',0],'value':True}],owner='project_policy')['schedules']['rate']['includes']==[True,False]
    ambiguous={'a':{'rate':{'includes':[False]}},'b':{'rate':{'includes':[False]}}}
    with pytest.raises(ProgramError,match='无法定位'):apply_review_patches(ambiguous,[{'op':'set','path':['rate','includes',0],'value':True}],owner='project_policy')


def test_native_interval_rate_rules_cannot_be_only_data_binding():
    text='100-500万 | 0.2% | 0.3%'
    u={'T1R2':{'file':'policy.pptx','where':'P2','text':text,'table':'T1','row':2,'title':'阶梯费率'}}
    c={'quote':text,'meaning':'实际制度档费率','kind':'table','level':'source','operation':'parameter'}
    p={'units':{'T1R2':{'disposition':'operative','reason':'实际制度参数','clauses':[c]}}}
    with pytest.raises(ProgramError,match='不能仅归source'):validate_clause_chunk(u,p)
    c['level']='primary';ob,_=validate_clause_chunk(u,p);assert ob['T1R2:1']['phase']=='project_policy'


def test_actual_person_grain_cannot_route_to_primary_formula():
    text='人员应得：先按员工编号汇总。应得金额=ROUND(订单合计×人员表计奖系数,2)。'
    unit={'R1':{'file':'policy','where':'line','text':text}}
    c={'quote':text,'meaning':'人员汇总再计奖','kind':'formula','level':'primary','operation':'adjustment'}
    response={'units':{'R1':{'disposition':'operative','reason':'真实规则','clauses':[c]}}}
    with pytest.raises(ProgramError,match='人员归属/汇总层'):validate_clause_chunk(unit,response)
    c.update(level='person',operation='calculation')
    actual,_=validate_clause_chunk(unit,response);assert actual['R1:1']['phase']=='roster'


def test_source_parameter_usage_and_exact_percent_literal():
    from bonus_agent.adaptive_sources import read_literal
    from bonus_agent.adaptive_rules import verify_parameter_usage
    c=Catalog([]);c._block('x.md','line','提成比例为1%。')
    with pytest.raises(ProgramError,match='不在精确引用'):read_literal(c,{'block':'R1','quote':'提成比例为1%','value':'1'})
    assert read_literal(c,{'block':'R1','quote':'提成比例为1%','value':'1%'})[0]=='1%'
    obligations={'R1:1':{'phase':'project_policy'}};params={'rate':{'rules':['R1:1']}}
    with pytest.raises(ProgramError,match='没有进入实际计算'):verify_parameter_usage(params,{},ref('base'),obligations,owner='project_policy')
    verify_parameter_usage(params,{},op('mul',ref('base'),param('rate')),obligations,owner='project_policy')


def test_truncated_nonempty_source_range_cannot_silently_pass():
    c,ds,params,plan,_=fixture();ds['orders']['sources'][0]['end_row']=2
    with pytest.raises(ProgramError,match='未交代实际数据范围'):read_datasets(c,{'orders':ds['orders']},period='202701')
    ds['orders']['sources'][0]['excluded_ranges']=[{'start':3,'end':3,'reason':'明确排除的辅助记录','rules':['R1:1']}]
    _,proof=read_datasets(c,{'orders':ds['orders']},period='202701');assert any(x['where'].endswith('!3') for x in proof['orders']['excluded'])


def test_quarter_is_not_silently_replaced_by_its_last_month():
    from bonus_agent.commission_revision import select_versions,RevisionError
    ds=Dataset({'month':{},'key':{},'amount':{}},tuple(Record({'month':m,'key':'x','amount':D(v)},'data!'+str(i)) for i,(m,v) in enumerate([('202701',1),('202702',2),('202703',3),('202704',99)],2)))
    metrics={'sum':{'label':'期间合计','expr':query('$project','sum',fld('amount')),'rules':[]}}
    plan={'population':{'dataset':'source','keys':['key'],'where':op('in_period',fld('month')),'rules':[]},'metrics':metrics,
          'funding':{'total':'sum','payable':'sum','alloc':'sum','destinations':{},'labels':{'total':'总额','alloc':'归属'},'summary_digits':2,'rules':[]}}
    assert CalculationProgram(Inputs({'source':ds}),plan,'2027Q1').basis()['pool']==6
    first={'id':'first','field':'x','from':'202701','to':None};second={'id':'second','field':'x','from':'202703','to':None}
    assert select_versions([first],'2027Q1')==[first]
    with pytest.raises(RevisionError,match='跨越不同'):select_versions([first,second],'2027Q1')


def test_runtime_diagnostic_contains_rule_metric_object_and_source():
    c,ds,params,plan,_=fixture()
    plan['metrics']['collect_factor']['rules']=['R91:1']
    c.sheets['S3'].rows.pop(2)
    p=CalculationProgram(compile_inputs(c,ds,params,{},period='202701'),plan,'2027-01')
    with pytest.raises(ProgramError) as exc:p.basis()
    d=exc.value.diagnostic()
    assert d['owner']=='coefficient_policy' and d['metric']=='collect_factor' and d['dataset']=='collection'
    assert '0008' in d['key'] and d['rules']==['R91:1']


def test_person_layer_preserves_zero_employee_and_applies_offset_after_aggregation():
    from bonus_agent.adaptive_sources import check_relationships
    c,ds,params,plan,roster=fixture()
    inputs=compile_inputs(c,ds,params,{},period='202701')
    p=CalculationProgram(inputs,plan,'2027-01');basis=p.basis();allocation=p.settlement(roster,basis)
    def records(items):
        fields={k:{} for k in items[0]};return Dataset(fields,tuple(Record(r,f'person-data!{i+2}') for i,r in enumerate(items)))
    inputs.datasets['employees']=records([{'id':'A001','name':'同名甲','factor':D('.5')},{'id':'B001','name':'同名甲','factor':D(1)},{'id':'C000','name':'零奖金','factor':D(1)}])
    inputs.datasets['prepaid']=records([{'id':'A001','amount':D(40)},{'id':'B001','amount':D(10)},{'id':'C000','amount':D(5)}])
    def metric(expr):return {'label':'test','expr':expr,'rules':[],'owner':'roster'}
    metrics={
        'name':metric(query('$project','unique',fld('name'))),
        'factor':metric(query('$project','unique',fld('factor'))),
        'attributed':metric(query('approved_contributions','sum',fld('raw'),match={'person_id':key('id')},empty='zero')),
        'due':metric(op('round',op('mul',ref('attributed'),ref('factor')),2)),
        'prepaid':metric(query('prepaid','sum',fld('amount'),match={'id':key('id')},empty='zero')),
        'applied':metric(op('min',ref('due'),ref('prepaid'))),
        'over':metric(op('max',op('sub',ref('prepaid'),ref('due')),0)),
        'paid':metric(op('max',op('sub',ref('due'),ref('prepaid')),0)),
    }
    person={'program':{'population':{'dataset':'employees','keys':['id'],'where':True,'rules':[]},'metrics':metrics,
             'funding':{'total':'due','payable':'paid','alloc':'paid','destinations':{'applied':'已预发抵扣'},'labels':{'total':'应得','alloc':'实发'},'summary_digits':2,'rules':[]}},
            'identity_key':'id','name_metric':'name','attributed_metric':'attributed','contribution_amount':'raw','rules':[]}
    result=apply_person_program(inputs,basis,allocation,person,'2027-01')
    people={x['id']:x for x in result['people'] if x['destination']=='person'}
    assert people['A001']['entitled']==D('32.64') and people['A001']['amount']==0 and people['A001']['metrics']['over']==D('7.36')
    assert people['B001']['amount']==D('20.72')
    assert people['C000']['amount']==0 and people['C000']['attributed']==0 and people['C000']['metrics']['over']==5
    bridge=result['funding_bridge']
    assert basis['pool']==bridge['paid']+bridge['pending']+bridge['rule_adjustment']+sum(bridge['deductions'].values())+bridge['residual']
    # An identifier-only attribution is not a claim that the person is named
    # by that identifier; actual source names must still agree when present.
    identity_only=deepcopy(allocation)
    for row in identity_only['rows']:
        if row['destination']=='person' and row['person_id']=='A001':row.update(name='A001',name_is_identity=True)
    completed=apply_person_program(inputs,basis,identity_only,person,'2027-01')
    assert all(r['name']=='同名甲' and r['original_identity_display']=='A001' for r in completed['rows'] if r['person_id']=='A001')
    for row in identity_only['rows']:
        if row['person_id']=='A001':row.pop('name_is_identity',None)
    with pytest.raises(ProgramError,match='姓名.*不一致'):
        apply_person_program(inputs,basis,identity_only,person,'2027-01')
    # An extra/orphan source row can never simply disappear from joins.
    inputs.datasets['orphan']=records([{'id':'missing','amount':D(1)}])
    with pytest.raises(ProgramError,match='实际匹配 0 行'):
        check_relationships(inputs.datasets,{'orphan':{'relationships':[{'fields':['id'],'target':'employees','target_fields':['id'],'cardinality':'exactly_one','rules':[]}]}})


@settings(max_examples=60,deadline=None)
@given(cents=st.integers(min_value=-10000000,max_value=10000000),share=st.integers(min_value=0,max_value=100))
def test_parameterized_money_and_split_agree_with_independent_fraction(cents,share):
    d=D(cents)/100
    source=Dataset({'contract':{},'money':{}},(Record({'contract':'a','money':d},'synthetic!2'),))
    plan={'population':{'dataset':'x','keys':['contract'],'where':True,'rules':[]},
          'metrics':{'raw':{'label':'raw','expr':query('$project','sum',fld('money')),'rules':[]},
                     'paid':{'label':'paid','expr':op('round',ref('raw'),2),'rules':[]}},
          'funding':{'total':'raw','payable':'raw','alloc':'paid','destinations':{},'labels':{'total':'total','alloc':'alloc'},'summary_digits':2,'rules':[]}}
    p=CalculationProgram(Inputs({'x':source}),plan,'2027-01');b=p.basis()
    rate=D(share)/100
    r={'roles':{'customA':{'label':'A','rate':{'num':str(rate)},'rules':[],'recipients':{'kind':'pending','label':'A','rules':[]}},
                'customB':{'label':'B','rate':{'num':str(1-rate)},'rules':[],'recipients':{'kind':'pending','label':'B','rules':[]}}},
        'rounding':{'when':'after_sum','digits':2,'rules':[]},'rules':[]}
    s=p.settlement(r,b)
    assert F(s['rows'][0]['raw'])==F(cents,100)*F(share,100)
    assert F(s['rows'][1]['raw'])==F(cents,100)*F(100-share,100)
    assert s['pending']+s['residual']==D(cents)/100
