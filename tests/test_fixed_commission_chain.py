from decimal import Decimal as D
from fractions import Fraction

import pytest

from bonus_agent.commission_calculation import _steps,_parse_shares,validated_maps
from bonus_agent.commission_chain import unresolved_questions,archived_candidate,_settlement_review_job,prior_approved_basis,basis_contract_digest
from bonus_agent.commission_sources import Sheet,ChainError
from bonus_core.engine.commission import tier_amount,tier_check,bracket,coefficient,project_money,close_money,settle_project


def test_progressive_exact_boundaries_and_negative_against_independent_identity():
    bands=[(D(0),D('.011'),True),(D('1500'),D('.013'),False),(D('4200'),D('.019'),False)]
    for value in map(D,['-30','0','1499.99','1500','1500.01','4199.99','4200','4200.01','8100']):
        assert Fraction(tier_amount(value,bands,'progressive','signed'))==tier_check(value,bands,'progressive','signed')
    assert tier_amount(D('-30'),bands,'progressive','signed')==D('-.33')


def test_coefficient_interval_explicit_inclusion_and_missing_not_zero():
    raw=[('<30% (含)',D('.7'),{}),('30-60% (含)',D('.9'),{}),('60%+',D('1.1'),{})]
    steps,_=_steps(raw,percent=True,includes=[True,False,False],where='synthetic')
    assert bracket(D('.3'),steps)[1]==D('.7')
    assert bracket(D('.30001'),steps)[1]==D('.9')
    assert coefficient(None,D(1),steps,D(1))==(None,D(1))
    assert coefficient(D(0),D(1),steps,D(1))==(D(0),D('.7'))
    with pytest.raises(ChainError,match='含上界'):_steps(raw,percent=True,includes=[True,True,False],where='synthetic')


def test_two_deduction_factors_and_signed_freeze_are_independent():
    args=(D('100'),D(1),D(1),D(1),D('.8'),D('.9'))
    a=project_money(*args,False,'additive',2)
    assert a['hold']==D('30') and a['alloc']==D('70')
    b=project_money(*args,True,'additive',2)
    assert b['hold']==D('30') and b['debt']==D('70') and b['alloc']==0
    c=project_money(D('-100'),*args[1:],False,'additive',2)
    assert c['hold']==D('-30') and c['alloc']==D('-70')
    for x in (a,b,c):close_money(x)


def test_roster_explicit_text_overrides_name_and_blank_share_uses_one_name():
    policy={'default_share':'1','share_unit':'ratio'};spec={'text_precedence':True}
    assert _parse_shares('甲80%，乙20%',['旧姓名'],policy,spec,'example')==[('甲',D('.8')),('乙',D('.2'))]
    assert _parse_shares(None,['现有姓名',''],policy,spec,'example')==[('现有姓名',D(1))]
    assert _parse_shares(None,['',''],policy,spec,'example')==[]
    with pytest.raises(ChainError,match='多名人员'):_parse_shares(None,['甲','乙'],policy,spec,'example')
    rows=settle_project(D('-100'),[('r','甲',D('.4'),'person','source'),('r','r未分配',D('.6'),'pending','source')],{'r':D(1)})
    assert [r['raw'] for r in rows]==[D('-40'),D('-60')]


def test_known_question_with_no_missing_evidence_does_not_stop_verified_candidate():
    a={'questions':[{'text':'重复确认','missing':'无（已确认）'},{'text':'确实缺少','missing':'尚缺两人分成比例'}]}
    assert [q['text'] for q in unresolved_questions(a)]==['确实缺少']
    events=[{'node':'a/解析','status':'PASS','result':{'x':1}}, {'node':'a/独立复核','status':'PASS','result':{'status':'PASS'}},
            {'node':'a/解析','status':'PASS','result':{'x':2}}]
    assert archived_candidate(events,'a')==({'x':2},None)


def test_optional_coordinate_shape_normalizes_before_real_column_validation():
    class C:
        table=Sheet('S','s.xlsx','sheet',[('编号','判断'),('x','剔除')])
        def sheet(self,spec):return self.table
    candidate={'sales':{'sheet':'S','columns':{'id':'A'},'exclude':'B'}}
    assert validated_maps(C(),candidate,{'sales':('id',)})['sales']['columns']=={'id':'A','exclude':'B'}
    candidate={'sales':{'sheet':'S','columns':{'id':'A','exclude':'A'},'exclude':'B'}}
    with pytest.raises(ChainError,match='矛盾'):validated_maps(C(),candidate,{'sales':('id',)})


def test_review_keeps_all_split_recipients_in_each_sampled_project():
    class C:blocks={}
    policies={'role_unit':'percent','share_unit':'ratio','person_rounding':'after_sum','person_round_digits':2,'default_share':'1','roles':{},'evidence':{}}
    basis={'projects':{k:{'route':'route-x','old':False,'alloc':D('-50'),'frozen':False} for k in ('p1','p2')}}
    rows=[]
    for key in basis['projects']:
        rows.extend([dict(project=key,role='客户经理',name='甲',share=D('.8'),role_rate=D(1),destination='person',raw=D('-40'),source='sheet!1'),
                     dict(project=key,role='客户经理',name='乙',share=D('.2'),role_rate=D(1),destination='person',raw=D('-10'),source='sheet!1')])
    data,_=_settlement_review_job(C(),policies,basis,dict(rows=rows,checks={},allocated=D('-100'),pending=D(0),residual=D(0),parameter_evidence=[]))
    assert data['complete_project_samples']
    for sample in data['complete_project_samples']:
        assert sample['complete_for_this_project'] and len(sample['role_rows'])==2
        assert sample['role_raw_sum']==sample['project_amount']==D('-50')


def test_same_case_approval_requires_original_human_record_and_review():
    approval={'status':'PASS','digest':'amount-digest','by':'finance','at':'time','identity':'old'}
    state={'files':{'file':'sha'},'period':'202610','specs':{},'basis_digest':'amount-digest','identity':'old','approval':approval,
           'receipts':{'basis_review':{'status':'PASS','inputs':'input-digest'}}}
    assert prior_approved_basis(state,{'total_confirmations':[]}) is None
    assert prior_approved_basis(state,{'total_confirmations':[dict(approval,by='other')]}) is None
    proof=prior_approved_basis(state,{'total_confirmations':[approval]})
    assert proof['contract']==basis_contract_digest(state) and proof['approval']==approval
    assert prior_approved_basis(dict(state,basis_digest='changed'),{'total_confirmations':[approval]}) is None
