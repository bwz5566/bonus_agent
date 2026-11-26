"""Regression tests for the actual financial-feedback/approval boundary.

These tests exercise persistence, version gates, scope and routing. The real
source/engine replay is tools/verify_commission_revision.py; no provider is
called by either suite.
"""
import copy
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bonus_agent import case as cs, commission_revision as cr
from bonus_agent.commission_chain import MODE, SCHEME, basis_contract_digest, execution_profile
from bonus_agent.commission_sources import Sheet
from bonus_agent.recovery import fingerprint


def make_case(out):
    out.mkdir(parents=True,exist_ok=True)
    basis={'projects':{'202609|1|A|P':{'key':'202609|1|A|P','alloc':'60.00','total':'60.00','product':'P','customer':'A','sources':{}},
                       '202609|2|B|Q':{'key':'202609|2|B|Q','alloc':'40.00','total':'40.00','product':'Q','customer':'B','sources':{}}},
           'totals':{'合计':'100.00'},'pool':'100.00','objects':{'A':{'amount':'60.00'},'B':{'amount':'40.00'}}}
    catalog=SimpleNamespace(sheets={'S1':Sheet('S1','sales.xlsx','sales',[('id',),('a',)]),
                                    'S2':Sheet('S2','history.xlsx','history',[('id',),('a',)])},tables={},blocks={})
    specs={k:{} for k in cr.BINDINGS}
    specs['scope_mapping']={'sales':{'sheet':'S1'}};specs['reference_mapping']={'history':{'sheet':'S2'}}
    specs['project_policy']={'age':{'months':18,'old_if':'gt'}};specs['hold_policy']={'hold_method':'additive'}
    specs['roster']={'person_rounding':'after_sum','person_round_digits':2}
    state={'mode':MODE,'identity':'identity','profile':execution_profile(),'period':'202609','status':'OK',
           'files':{'sales.xlsx':'sha-sales','history.xlsx':'sha-history'},'source_order':['sales.xlsx','history.xlsx'],
           'source_manifest':cr.source_manifest(catalog),'specs':specs,'basis_view':basis,'basis_digest':fingerprint(basis),
           'receipts':{k:{'status':'PASS','output':fingerprint(v),'inputs':'old'} for k,v in specs.items()},
           'node_receipts':{k:{'status':'PASS'} for k in ('basis','confirm_basis','settlement','export')},'questions':[],'events':[],'revisions':{}}
    state['receipts']['basis_review']={'status':'PASS','inputs':'review'}
    approval={'status':'PASS','scheme':SCHEME,'digest':state['basis_digest'],'identity':state['identity'],
              'by':'实际示例审批人','at':'2026-11-26T10:00:00','contract':basis_contract_digest(state),'round':1}
    state['approval']=approval
    result=out/'result.xlsx';result.write_bytes(b'old-result-for-boundary-test')
    detail=out/'commission_details.json';detail.write_text(json.dumps({'basis':basis,'settlement':{'people':[],'rows':[]}},ensure_ascii=False),encoding='utf8')
    rnd={'n':1,'status':'OK','period':'2026-09','source_inputs':[],'outputs':{'excel':[result.name,cs.sha256(result)],'details':[detail.name,cs.sha256(detail)]}}
    record={'rounds':[rnd],'workspace':{'workflow':{'fixed_commission':state}},'total_confirmations':[copy.deepcopy(approval)]}
    cs.save_case(out,record)
    cs.archive(out,1,{'result':result,'details':detail})
    return record,catalog


def test_total_rejection_retires_current_files_and_rejects_old_approval(tmp_path):
    out=tmp_path/'out';case,_=make_case(out);old=cr.state_of(case);token=cr.receipt_token(old)
    revision=cr.submit(out,by='经办',stage='basis',owner='coefficient_policy',description='客户达成系数有疑义，核对原始预算')
    saved=cs.load_case(out);state=cr.state_of(saved)
    assert state['status']=='REVISION_PENDING' and state['revision_id']==revision['id'] and 'approval' not in state
    assert 'coefficient_policy' not in state['receipts'] and 'scope_mapping' in state['receipts']
    assert 'basis_review' not in state['receipts'] and 'confirm_basis' not in state['node_receipts']
    assert saved['total_confirmations']==case['total_confirmations']
    assert not (out/'result.xlsx').exists()
    assert (out/'history'/'第1轮'/'result.xlsx').read_bytes()==b'old-result-for-boundary-test'
    assert cr.baseline(out,revision)['basis_digest']==token
    with pytest.raises(cs.ConfirmError,match='财务修订'):cs.confirm_totals(out,SCHEME,token,'审批')
    with pytest.raises(cs.ConfirmError):cs._checked_round(out)


def test_same_money_needs_new_version_token_and_per_object_diff(tmp_path):
    old,_=make_case(tmp_path/'out');state=cr.state_of(old);old_token=cr.receipt_token(state)
    state['basis_revision']='new-generation'
    assert cr.receipt_token(state)!=old_token
    assert not cr.approval_matches(state)
    same=copy.deepcopy(state['basis_view']);keys=list(same['projects'])
    same['projects'][keys[0]]['alloc']='61.00';same['projects'][keys[1]]['alloc']='39.00'
    revision={'id':'r','basis_revision':'r','requests':[],'owners':['basis_review'],'affected':['basis_review'],'basis_changed':True}
    previous={'basis':state['basis_view'],'basis_digest':state['basis_digest'],'specs':state['specs'],'files':state['files']}
    diff=cr.diff_report(previous,same,None,state,revision)
    assert diff['totals']==[] and diff['projects']['changed']==2
    assert sorted(Decimal(x['delta']) for x in diff['projects']['indicators'])==[Decimal(-1),Decimal(1)]
    assert not diff['settlement_compared']


def test_personnel_revision_cannot_change_project_contract(tmp_path):
    out=tmp_path/'out';case,_=make_case(out);key=next(iter(cr.state_of(case)['basis_view']['projects']))
    revision=cr.submit(out,by='审批',approver=True,kind='recipient_change',project_keys=[key],role='客户经理',shares='甲70%，乙30%',description='实际名单调整')
    saved=cr.state_of(cs.load_case(out));assert not revision['basis_changed'] and cr.approval_matches(saved)
    assert cr.recipient_overrides(cr.load(out),'202609')[0]['shares']==[{'name':'甲','share':'0.7'},{'name':'乙','share':'0.3'}]
    cr.assert_unchanged_basis(cr.baseline(out,revision),saved,revision)
    saved['specs']['hold_policy']['hold_method']='multiplicative'
    with pytest.raises(cr.RevisionError,match='原总额确认不能复用'):cr.assert_unchanged_basis(cr.baseline(out,revision),saved,revision)


@pytest.mark.parametrize('text',['甲120%','甲60%，乙60%','甲50%，甲50%','甲-10%','甲0.7',''])
def test_recipient_correction_rejects_invalid_named_shares(text):
    with pytest.raises(cr.RevisionError):cr.parse_recipients(text)


def test_future_policy_and_case_override_scope(tmp_path):
    out=tmp_path/'out';case,_=make_case(out);store=tmp_path/'rule_store'/'versions.json'
    with pytest.raises(cr.RevisionError,match='财务确认权限'):
        cr.submit(out,by='经办',kind='rule_change',field='age_months',value='24',description='正式要求')
    r=cr.submit(out,by='审批',approver=True,kind='rule_change',field='age_months',value='24',scope='future_periods',effective_from='2026-10',rule_store=store,description='10月起有效')
    assert r['status']=='SCHEDULED' and not cr.active(cr.load(out))
    assert cr.effective_rules(cr.load(out),store,'202609')==[]
    assert cr.effective_rules(cr.load(out),store,'202610')[0]['value']==24
    current=cs.load_case(out);assert cr.approval_matches(cr.state_of(current))
    cr.submit(out,by='审批',approver=True,kind='rule_change',field='age_months',value='20',description='仅本案',rule_store=store)
    assert cr.effective_rules(cr.load(out),store,'202609')[0]['value']==20
    assert cr.effective_rules({'case_rules':[]},store,'202609')==[]
    assert cr.effective_rules(cr.load(out),store,'202611')[0]['value']==24


def test_pending_amendments_do_not_lose_or_reapply_superseded_feedback(tmp_path):
    out=tmp_path/'out';make_case(out)
    cr.submit(out,by='经办',owner='coefficient_policy',description='检查达成系数')
    cr.submit(out,by='审批',approver=True,kind='rule_change',field='age_months',value='24',description='调整阈值')
    r=cr.submit(out,by='审批',approver=True,kind='rule_change',field='age_months',value='30',description='更正新阈值')
    assert len(r['requests'])==3 and r['requests'][1]['superseded_by']==r['requests'][2]['id']
    assert r['requests'][2]['rule']['old_value']==24
    assert len(cr.requests_for_node(r,'project_policy'))==1
    assert len(cr.requests_for_node(r,'basis_review'))==2
    assert len(cr.load(out)['revisions'])==3


def test_specific_feedback_must_have_actual_findings_and_literal_evidence():
    catalog=SimpleNamespace(blocks={'R1':{'text':'毛利缺值系数为1'},'R2':{'text':'不相关原文'}})
    requests=[{'id':'r1'}]
    with pytest.raises(cr.RevisionError,match='不能只提交 PASS'):cr.relevant_findings(catalog,'basis_review',{'status':'PASS'},requests)
    row={'id':'r1','identified':'毛利缺值口径异议','checked':'逐条读取缺值系数原文','disposition':'retained','conclusion':'实际原文为1，保持该系数','evidence':[{'id':'R1','quote':'毛利缺值系数为1'}]}
    assert cr.relevant_findings(catalog,'basis_review',{'feedback_findings':[row]},requests,{'R1'})==[row]
    row['evidence'][0]['quote']='虚构的系数';
    with pytest.raises(cr.RevisionError,match='不在原文'):cr.relevant_findings(catalog,'basis_review',{'feedback_findings':[row]},requests,{'R1'})


def test_material_scope_requires_actual_unchanged_source_namespace(tmp_path):
    out=tmp_path/'out';_,catalog=make_case(out)
    r=cr.submit(out,by='经办',kind='material_correction',description='更正历史数据')
    state=cr.state_of(cs.load_case(out));state['files']['history.xlsx']='new-content'
    ledger=cr.load(out);r=cr.active(ledger)
    cr.narrow_material_revision(out,ledger,r,state,catalog)
    assert set(r['owners'])=={'inventory','reference_mapping','basis_review'}
    assert 'scope_mapping' in state['prior_accepted_specs']
    assert [x['owner'] for x in cr.requests_for_node(r,'reference_mapping')]==['inventory']
    assert not cr.requests_for_node(r,'scope_mapping')
    # Altering the namespace makes the prior physical coordinates unsafe.
    catalog.sheets['S3']=Sheet('S3','history.xlsx','added',[('col',),('v',)])
    cr.narrow_material_revision(out,ledger,r,state,catalog)
    assert set(cr.BINDINGS)<=set(r['owners'])


def test_web_revision_entry_permission_and_stale_download(tmp_path):
    from fastapi.testclient import TestClient
    from bonus_agent.web import create_app
    app=create_app(tmp_path/'data',lambda:pytest.fail('No provider should be called in this test'))
    store=app.state.store;store.users.add('editor','test-password',['editor'],'经办');store.users.add('approver','test-password',['approver'],'审批')
    cid=store.create('固定提成修订','editor');out=store.dir(cid)/'out';case,_=make_case(out)
    client=TestClient(app)
    def login(user):
        client.cookies.clear();assert client.post('/login',data={'username':user,'password':'test-password'},follow_redirects=False).status_code==303
        return store.csrf(client.cookies.get('bonus_session'))
    csrf=login('editor');base=f'/cases/{cid}';form=base+'/commission-feedback'
    assert '财务反馈与版本修订' in client.get(base).text
    reply=client.post(form,data={'csrf':csrf,'kind':'rule_change','field':'age_months','value':'24','description':'修改'},follow_redirects=False)
    assert reply.status_code==303 and not cr.active(cr.load(out))
    old_version=cr.edit_token(case,cr.load(out))
    reply=client.post(form,data={'csrf':csrf,'kind':'discrepancy','stage':'basis','description':'总额有误','base_version':old_version},follow_redirects=False)
    assert reply.status_code==303 and cr.active(cr.load(out))
    assert client.get(base+'/dl/out/result.xlsx').status_code in (404,409)
    assert client.get(base+'/dl/out/history/第1轮/result.xlsx').status_code==200
    page=client.get(base).text;assert '旧版交付停止' in page and '确认第 1 轮核算结果' not in page
    assert client.post(base+'/chat',data={'csrf':csrf,'text':'总额怎么算？'},follow_redirects=False).status_code==303
    csrf=login('approver');previous=cr.active_id(cr.load(out))
    client.post(form,data={'csrf':csrf,'kind':'rule_change','field':'age_months','value':'24','description':'修改','base_version':old_version},follow_redirects=False)
    assert cr.active_id(cr.load(out))==previous


def test_pending_fixed_confirmation_is_prominent_for_both_finance_roles(tmp_path):
    from fastapi.testclient import TestClient
    from bonus_agent.web import create_app
    app=create_app(tmp_path/'data',lambda:pytest.fail('No provider should be called'))
    store=app.state.store;store.users.add('editor','test-password',['editor'],'经办');store.users.add('approver','test-password',['approver'],'审批')
    cid=store.create('等待确认','editor');out=store.dir(cid)/'out';case,_=make_case(out)
    state=cr.state_of(case);state.pop('approval');state['status']='AWAITING_TOTAL_CONFIRMATION';state['confirmation_version']=2
    case['rounds'][-1]['status']='AWAITING_TOTAL_CONFIRMATION';cs.save_case(out,case)
    client=TestClient(app);base=f'/cases/{cid}'
    def login(user):
        client.cookies.clear();assert client.post('/login',data={'username':user,'password':'test-password'},follow_redirects=False).status_code==303
        return store.csrf(client.cookies.get('bonus_session'))
    csrf=login('editor');page=client.get(base).text
    assert page.index('总额预核算确认：下一步操作')<page.index('财务反馈与版本修订')
    assert '<button type="button" disabled>确认本版本总额并继续人员分配</button>' in page
    assert '当前登录账号没有审批权限' in page
    assert client.post(base+'/confirm-totals',data={'csrf':csrf,'scheme':SCHEME,'digest':cr.receipt_token(state)},follow_redirects=False).status_code==403
    login('approver');page=client.get(base).text
    assert '<button>确认本版本总额并继续人员分配</button>' in page and '当前登录账号没有审批权限' not in page
    state['profile']='old-program';cs.save_case(out,case)
    page=client.get(base).text
    assert '核算程序已更新' in page and '<button type="button" disabled>确认本版本总额并继续人员分配</button>' in page


def test_single_account_keeps_password_session_and_enables_approval(tmp_path):
    from fastapi.testclient import TestClient
    from bonus_agent.web import create_app
    app=create_app(tmp_path/'data',lambda:pytest.fail('No provider call expected'))
    store=app.state.store;store.users.add('keep','test-password',['editor'],'财务');store.users.add('old','other-password',['approver'],'旧审批')
    old_hash=store.users.load()['keep']['password']
    cid=store.create('单账号等待确认','keep');out=store.dir(cid)/'out';obj,_=make_case(out)
    state=cr.state_of(obj);state.pop('approval');state['status']='AWAITING_TOTAL_CONFIRMATION';state['confirmation_version']=2
    obj['rounds'][-1]['status']='AWAITING_TOTAL_CONFIRMATION';cs.save_case(out,obj)
    keep=TestClient(app);old=TestClient(app)
    for client,name,pw in ((keep,'keep','test-password'),(old,'old','other-password')):
        assert client.post('/login',data={'username':name,'password':pw},follow_redirects=False).status_code==303
    assert '当前登录账号没有审批权限' in keep.get(f'/cases/{cid}').text
    backup=store.users.single('keep');assert backup.is_file()
    assert set(store.users.load())=={'keep'} and store.users.load()['keep']['password']==old_hash
    assert set(store.users.check('keep','test-password')['roles'])=={'editor','approver'}
    assert store.users.check('old','other-password') is None
    # Existing browser session has the new full permissions without a restart.
    assert '<button>确认本版本总额并继续人员分配</button>' in keep.get(f'/cases/{cid}').text
    assert old.get(f'/cases/{cid}',follow_redirects=False).status_code==303
    assert store.users.single('keep') is None


def test_verified_report_version_is_primary_download_without_replacing_original(tmp_path):
    from fastapi.testclient import TestClient
    from bonus_agent.web import create_app
    app=create_app(tmp_path/'data',lambda:pytest.fail('No provider should be called'))
    store=app.state.store;store.users.add('finance','test-password',['editor','approver'],'财务')
    cid=store.create('新版报表','finance');out=store.dir(cid)/'out';obj,_=make_case(out);state=cr.state_of(obj)
    p=out/'report_versions'/'report-test'/'result.xlsx';p.parent.mkdir(parents=True);p.write_bytes(b'new-report-version')
    record={'id':'report-test','status':'PASS','workbook':'result.xlsx','workbook_sha256':cs.sha256(p),'source_round':1,
            'source_basis_digest':state['basis_digest'],'source_details_sha256':obj['rounds'][-1]['outputs']['details'][1],
            'format':'commission-workpaper-v2','created_at':'2026-11-26T15:00:00'}
    (p.parent/'manifest.json').write_text(json.dumps(record),encoding='utf8')
    client=TestClient(app);assert client.post('/login',data={'username':'finance','password':'test-password'},follow_redirects=False).status_code==303
    base=f'/cases/{cid}';page=client.get(base).text;href=base+'/dl/out/report_versions/report-test/result.xlsx'
    assert '新版核算底稿和汇总.xlsx' in page and '原已核验报表（留档）' in page and href in page
    assert client.get(href).content==b'new-report-version'
    assert client.get(base+'/dl/out/result.xlsx').content==b'old-result-for-boundary-test'
    p.write_bytes(b'tampered-report');assert client.get(href).status_code==409
