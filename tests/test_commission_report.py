"""Output-contract regressions, independent from today's actual finance data."""
import json
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from bonus_agent import case as cs
from bonus_agent.commission_report import read_report, reports_for_case
from bonus_agent.commission_report_sources import build_report_sources, inline_role_slots
from bonus_agent.commission_sources import Sheet


def test_inline_recipient_pairs_expand_to_actual_people_and_keep_pending():
    rows=[{'project':'p','role':'客户经理','name':f'人员{i}'} for i in range(4)]
    rows.append({'project':'p','role':'客户经理','name':'客户经理未分配'})
    rows.append({'project':'q','role':'销售执行人','name':'执行者'})
    slots=inline_role_slots({'rows':rows})
    managers=[s for s in slots if s[0]=='客户经理']
    assert len(managers)==5 and managers[-1][2:]==('客户经理5','客户经理5金额')
    assert len([s for s in slots if s[0]=='销售执行人'])==2
    assert any(s[2]=='NPD' and s[3]=='NPD金额' for s in slots)
    assert inline_role_slots(None)==[]


def report_fixture():
    tables={
        'S1':Sheet('S1','region.xlsx','region',[('区域','预算'),('R',200),('R',200)]),
        'S2':Sheet('S2','customer.xlsx','customer',[('客户','预算'),('a',None)]),
        'S3':Sheet('S3','stock.xlsx','stock',[('客户','余额'),('a',30),('a',-10),('a',None)]),
        'S4':Sheet('S4','overdue.xlsx','overdue',[('客户','余额'),('a',0)]),
        'S5':Sheet('S5','history.xlsx','history',[('月份','客户','出货额'),('202601','a',300),('202603','a',5),('202603','a',-5),('202604','a',0),('202609','a',100)]),
        'S6':Sheet('S6','gross.xlsx','gross',[('月份','编码','客户','型号','毛利'),('202609','1','a','p',0)]),
        'S7':Sheet('S7','debt.xlsx','debt',[('月份','编码','客户','型号','状态'),('202609','1','a','p','未到期')]),
    }
    catalog=SimpleNamespace(sheets=tables,sheet=lambda s:tables[s['sheet']])
    def mapping(sid,columns):return {'sheet':sid,'header_row':1,'columns':columns}
    ref={'region_budget':mapping('S1',{'region':'A','amount':'B'}),'customer_budget':mapping('S2',{'customer':'A','amount':'B'}),
         'stock':mapping('S3',{'customer':'A','amount':'B'}),'overdue':mapping('S4',{'customer':'A','amount':'B'}),
         'history':mapping('S5',{'month':'A','customer':'B','amount':'C'})}
    kcols={'month':'A','code':'B','customer':'C','product':'D'}
    scope={'gross':mapping('S6',{**kcols,'margin':'E'}),'unpaid':mapping('S7',{**kcols,'status':'E'})}
    specs={'reference_mapping':ref,'scope_mapping':scope,'coefficient_policy':{'budget_duplicates':'unique','history_months':'nonzero'},
           'hold_policy':{'freeze_source':'status','freeze_statuses':['已逾期']}}
    key='202609|1|a|p';p={'customer':'a','region':'R','route':'T1','sales':D(100),'sources':{'sales':['source.xlsx/sales!2']},
                         'region_budget':D(200),'customer_budget':None,'stock':D(20),'overdue':D(0),'gross':D(0),
                         'history_sum':D(400),'history_months':3,'frozen':False}
    return catalog,{'projects':{key:p}},specs,key


def test_report_preserves_actual_reference_rows_missing_vs_zero_and_net_zero_month():
    catalog,basis,specs,key=report_fixture()
    p=build_report_sources(catalog,basis,specs,'202609')
    assert len(p['groups'][('region_budget','R')])==2
    assert p['groups'][('customer_budget','a')][0]['amount'] is None
    assert p['groups'][('overdue','a')][0]['amount']==0
    assert p['groups'][('gross',key)][0]['amount']==0
    assert set(p['history']['a'])=={'202601','202603'} # nonzero source records in March net to zero; still an actual shipment month
    assert len(p['history']['a']['202603'])==2 and p['debt'][key][0]['status']=='未到期'
    specs['coefficient_policy']['history_months']='elapsed';basis['projects'][key]['history_months']=9
    assert build_report_sources(catalog,basis,specs,'202609')['current_sales']['a']==100


def test_report_rejects_a_conflicting_input_or_amount_not_in_frozen_basis():
    catalog,basis,specs,key=report_fixture();catalog.sheets['S1'].rows[2]=('R',201)
    with pytest.raises(ValueError,match='唯一值存在冲突'):build_report_sources(catalog,basis,specs,'202609')
    specs['coefficient_policy']['budget_duplicates']='sum'
    with pytest.raises(ValueError,match='核验结果.*不一致'):build_report_sources(catalog,basis,specs,'202609')


def test_report_manifest_detects_tampering_and_historical_financial_versions(tmp_path):
    out=tmp_path/'out';p=out/'report_versions'/'report-1'/'result.xlsx';p.parent.mkdir(parents=True);p.write_bytes(b'workbook')
    m={'id':'report-1','status':'PASS','workbook':'result.xlsx','workbook_sha256':cs.sha256(p),'source_round':2,
       'source_basis_digest':'basis','source_details_sha256':'details'}
    (p.parent/'manifest.json').write_text(json.dumps(m),encoding='utf8')
    c={'rounds':[{'n':2,'status':'OK','outputs':{'details':['d.json','details']}}],
       'workspace':{'workflow':{'fixed_commission':{'status':'OK','basis_digest':'basis'}}}}
    assert read_report(p)==m and reports_for_case(out,c)[0]['current']
    c['rounds'][-1]['n']=3
    assert not reports_for_case(out,c)[0]['current']
    p.write_bytes(b'tampered')
    with pytest.raises(cs.ConfirmError,match='指纹不符'):read_report(p)
    assert reports_for_case(out,c)==[]
