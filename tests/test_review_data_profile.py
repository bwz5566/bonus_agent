"""Independent reviewers receive actual domains and pre-default missingness."""
import json
from types import SimpleNamespace as NS

import openpyxl
import pytest

from bonus_agent.auto import build_inventory
from bonus_agent import reread
from bonus_agent.data_profile import build_profile, review_context
from bonus_core.data.mapping import DatasetMapping
from bonus_core.data.period import Period
from bonus_core.dsl.models import DatasetDef

pytestmark = pytest.mark.evidence


def fixture_ws(tmp_path, rows=None, *, unpivot=None):
    file = tmp_path/'data.xlsx'
    wb = openpyxl.Workbook(); sheet = wb.active; sheet.title = 'sales'
    sheet.append(['pid','区域','毛利率','月份','金额','人名'])
    for row in rows or [
        ['A','海外ODM-欧洲一组',None,'202609',123456.78,'PRIVATE_PERSON'],
        ['B','海外ODM-美加课',0,'202609',1,'PRIVATE_PERSON'],
        ['C','PI-IVG',' ','202608',2,'PRIVATE_PERSON'],
        ['D','PI-IVG',.2,'202609',3,'PRIVATE_PERSON'],
    ]:
        sheet.append(row)
    wb.save(file);wb.close()
    ds = DatasetDef.model_validate({'key':'pid','period':'month','fields':{
        'pid':{'type':'id'},'region':{'type':'text'},'gross':{'type':'ratio','if_empty':'0'},
        'month':{'type':'text'},'amount':{'type':'money'},'person':{'type':'text'},
    }})
    mapping = DatasetMapping(file=file.name,sheet='sales',columns={
        'pid':'pid','region':'区域','gross':'毛利率','month':'月份','amount':'金额','person':'人名'}, unpivot=unpivot)
    return NS(inv=build_inventory([file]), data_files=[file], datasets={'sales':ds}, mapping={'sales':mapping},
              period=Period.parse('2026-09'), questions=[], said='', drafts={'SECRET_PRODUCTION_FORMULA':'777777 * 888888'})


def test_actual_domains_and_pre_default_missingness(tmp_path):
    ws = fixture_ws(tmp_path)
    p = build_profile(ws)['datasets']['sales']['fields']
    assert p['gross']['raw']['null'] == 1 and p['gross']['raw']['blank'] == 1
    assert p['gross']['selected']['rows'] == 3 and p['gross']['selected']['null'] == 1
    assert p['gross']['selected']['blank'] == 0
    assert p['gross']['mapped']['null'] == 0 and p['gross']['mapped']['zero'] == 2
    assert p['gross']['raw']['null_rows'] == [2]
    assert p['gross']['source_position']['column_1based'] == 3
    domain = p['region']['mapped']
    assert domain['values_complete'] and not domain['values_truncated']
    assert {v['value']:v['count'] for v in domain['values']} == {'海外ODM-欧洲一组':1,'海外ODM-美加课':1,'PI-IVG':1}
    text = review_context(ws)
    assert 'PRIVATE_PERSON' not in text and '123456.78' not in text and 'SECRET_PRODUCTION_FORMULA' not in text


def test_value_limit_never_claims_complete_domain(tmp_path):
    ws = fixture_ws(tmp_path)
    values = build_profile(ws,value_limit=2)['datasets']['sales']['fields']['region']['mapped']
    assert values['rows'] == 3 and values['distinct_nonblank'] == 3
    assert values['scan_complete'] and values['values_truncated'] and not values['values_complete']
    assert len(values['values']) == 2 and values['omitted_values'] == 1


def test_raw_cells_remain_visible_when_unpivot_scope_is_unknown(tmp_path):
    ws = fixture_ws(tmp_path,unpivot={'columns':['人名'],'value':'负责人','label':'角色'})
    p = build_profile(ws)['datasets']['sales']['fields']
    assert p['gross']['raw']['null'] == 1
    assert p['gross']['selected']['status'] == 'unknown'
    assert not p['gross']['selected']['scan_complete']


def test_changed_values_same_rows_and_headers_change_review_signature(tmp_path):
    ws = fixture_ws(tmp_path)
    ws.llm = NS(checker_llm=None)
    before = reread.material(ws)
    changed = fixture_ws(tmp_path,rows=[
        ['A','海外ODM-欧洲三组',None,'202609',123456.78,'PRIVATE_PERSON'],
        ['B','海外ODM-美加课',0,'202609',1,'PRIVATE_PERSON'],
        ['C','PI-IVG',' ','202608',2,'PRIVATE_PERSON'],
        ['D','PI-IVG',.2,'202609',3,'PRIVATE_PERSON'],
    ])
    changed.llm = ws.llm
    after = reread.material(changed)
    assert reread.review_signature(ws,before) != reread.review_signature(changed,after)
    assert '海外ODM-欧洲三组' in after and '海外ODM-欧洲一组' not in after


def test_context_limit_reports_omitted_fields_as_partial(tmp_path):
    ws=fixture_ws(tmp_path)
    text=review_context(ws,max_chars=1800)
    obj=json.loads(text.split('\n',2)[2])
    assert not obj['fields_complete']
    assert obj['datasets']['sales']['omitted_fields']
    assert 'fingerprint' in obj


def test_invalid_declared_enum_still_exposes_actual_source_values(tmp_path):
    ws=fixture_ws(tmp_path)
    ws.datasets['sales'].fields['region'].type='enum'
    ws.datasets['sales'].fields['region'].values=['imagined region']
    profile=build_profile(ws)
    field=profile['datasets']['sales']['fields']['region']
    assert {v['value'] for v in field['raw']['values']} == {'海外ODM-欧洲一组','海外ODM-美加课','PI-IVG'}
    assert field['mapped']['status']=='unknown'
    assert not profile['mapped_data_complete']


def test_cross_period_parent_coverage_and_dates_do_not_trim_objects(tmp_path):
    ws=fixture_ws(tmp_path)
    source=ws.datasets.pop('sales');mapping=ws.mapping.pop('sales')
    ws.datasets={'projects':source.model_copy(update={'period':None}),
                 'sales':source.model_copy(update={'key':None,'link':'pid'})}
    ws.mapping={'projects':mapping,'sales':mapping}
    profile=build_profile(ws)
    relation=profile['relations'][0]
    assert (relation['parent_distinct_keys'],relation['child_distinct_keys'],relation['parent_unreferenced_keys']) == (4,3,1)
    assert relation['child_unmatched_keys']==0
    ranges=profile['datasets']['projects']['mapped_date_ranges']['month']
    assert ranges['min']=='2026-08-01' and ranges['max']=='2026-09-01'
    assert ws.datasets['projects'].period is None and ws.datasets['projects'].population_from is None


def test_basis_review_receives_profile_without_production_expression(tmp_path):
    from tests.test_staged_workflow import make_workspace, prepared
    ws = prepared(make_workspace(tmp_path))
    ws.calculate_basis('S')
    seen=[]
    original = ws.llm.checker_llm.chat
    def capture(messages, **kwargs):
        seen.append(messages)
        return original(messages, **kwargs)
    ws.llm.checker_llm.chat=capture
    assert ws.review_basis('S')['status']=='PASS'
    user=seen[0][1]['content']
    assert '数据概况（程序扫描；非生产公式或答案）' in user
    assert 'source_fingerprint' in user and 'scan_complete' in user
    assert 'amount * 0.1' not in user
