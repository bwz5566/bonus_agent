"""Real staged receipts, independent amounts and human-only confirmation boundaries."""
import copy
import json
from decimal import Decimal
from types import SimpleNamespace

import openpyxl
import pytest

from bonus_agent import case
from bonus_agent.agent import Workspace, ToolError, run_agent
from bonus_agent.auto import build_inventory
from bonus_agent.context import guard_messages, task_card
from bonus_agent.llm import FakeLLM, LLM, LLMError
from bonus_agent.recovery import fingerprint
from bonus_core.engine.run import Engine

pytestmark=pytest.mark.evidence


def population_spec():
    entry = {'dataset': 'projects', 'key': 'pid', 'filter': {}, 'period': None, 'window': 'period',
             'sources': [{'doc': 'policy.md', 'quote': '每个项目按销售金额的10%计算奖金'}]}
    return {'项目': copy.deepcopy(entry), 'projects': copy.deepcopy(entry),
            'rows:projects': {k: copy.deepcopy(v) for k, v in entry.items() if k != 'key'}}


def make_workspace(tmp_path, mutate_workbook=None, project_distinct=False, policy_tail=''):
    policy=tmp_path/'policy.md';policy.write_text('每个项目按销售金额的10%计算奖金，按名单比例分配。' + policy_tail,encoding='utf8')
    p=tmp_path/'data.xlsx';wb=openpyxl.Workbook();s=wb.active;s.title='projects'
    s.append(['pid','amount']);s.append(['A',100]);s.append(['B',200])
    r=wb.create_sheet('people');r.append(['rid','pid','person','share'])
    r.append(['1','A','甲',1]);r.append(['2','B','甲',.5]);r.append(['3','B','乙',.5])
    if mutate_workbook:
        mutate_workbook(wb)
    wb.save(p);wb.close()
    llm=FakeLLM([])
    llm.checker_required=True;llm.checker_llm=FakeLLM([json.dumps({
        'pool':'amount * 0.1','helpers':{},'tables':{},'details':{'项目奖':'amount * 0.1'},
        'totals':{'项目奖合计':'sum(amount, of: "projects") * 0.1'},'populations':population_spec(),'unclear':[]})])
    ws=Workspace(llm,build_inventory([policy,p]));ws.workflow['enabled']=True
    ws.plan_calculation('按项目核算，再按名单分配',['项目底稿','总额','人员金额'],{policy.name:'rule',p.name:'data'},['S'])
    ws.record_facts([{'name':'提成率','file':policy.name,'quote':'销售金额的10%','value':'10%'}])
    ws.record_clauses([{'file':policy.name,'quote':'每个项目按销售金额的10%计算奖金','point':'项目销售金额乘提成率'}])
    f=lambda c,t:{'column':c,'type':t}
    ws.define_dataset('projects',p.name,'projects',{'pid':f('pid','id'),'amount':f('amount','money')},key='pid',distinct=project_distinct)
    ws.define_dataset('people',p.name,'people',{'rid':f('rid','id'),'pid':f('pid','id'),
        'person':f('person','text'),'share':f('share','ratio')},key='rid')
    source=[{'doc':policy.name,'quote':'每个项目按销售金额的10%计算奖金'}]
    ws.set_rules(scheme='S',pool={'scope':'项目','expr':'amount * 0.1','sources':source},
        allocation=[{'level':'项目','key':'pid'},{'level':'参与','key':'rid','parent_key':'pid','weight':'share'}],
        report={'people':'person','details':[{'name':'项目奖','level':'项目','expr':'amount * 0.1','sources':source}],
                'distributions':[{'name':'项目分配去向','level':'项目','total':'amount * 0.1','sources':source}],
                'totals':[{'name':'项目奖合计','level':'global','expr':'sum(amount, of: "projects") * 0.1','sources':source}]})
    ws.cover_clauses({'C1':'pool'})
    ws.bind_facts([{'name':'提成率','targets':['S::/pool/expr','S::/report/details/0/expr','S::/report/totals/0/expr','S::/report/distributions/0/total']}])
    return ws


def prepared(ws):
    assert ws.prepare_data()['status']=='PASS'
    assert ws.compile_formulas()['status']=='PASS'
    return ws


def reviewed(ws):
    prepared(ws);ws.calculate_basis('S')
    result=ws.review_basis('S')
    assert result['status']=='PASS',result
    return ws


def test_looking_at_a_changed_clause_does_not_expire_formula_receipts(tmp_path):
    ws = make_workspace(tmp_path)
    flow = ws._flow()
    version = flow.version()
    clause, = ws.clauses
    ws._side_by_side([clause])
    assert clause.checked and flow.version() == version
    clause.where = 'report.details.项目奖'
    assert flow.version() != version


def test_pool_preview_never_allocates_and_requires_human_approval(tmp_path,monkeypatch):
    ws=prepared(make_workspace(tmp_path))
    monkeypatch.setattr(Engine,'_apply',lambda *a:pytest.fail('preview must not allocate'))
    ws.calculate_basis('S')
    assert not ws.results
    basis=ws.workflow['basis']['S']
    assert Decimal(basis['pool'])==30
    assert {k:Decimal(v['amount']) for k,v in basis['objects'].items()}=={'项目:A':Decimal(10),'项目:B':Decimal(20)}
    with pytest.raises(ToolError,match='review:S'):ws.trial_run('S')
    assert ws.review_basis('S')['status']=='PASS'
    with pytest.raises(ToolError,match='approved:S'):ws.trial_run('S')


def test_confirmed_basis_allocation_matches_exact_people_and_reports(tmp_path):
    ws=reviewed(make_workspace(tmp_path));basis=ws.workflow['basis']['S']
    ws._flow().approve('S','财务',fingerprint(basis))
    ws.trial_run('S')
    got={p['name']:Decimal(p['final']) for p in ws.results['S']['R0'].people}
    assert got=={'甲':Decimal(20),'乙':Decimal(10)}
    assert dict(ws.results['S']['R0'].totals)==basis['totals']


@pytest.mark.parametrize('with_source', [False, True])
def test_matching_pool_rebinds_only_required_project_detail(tmp_path, with_source):
    quote = '保留逐项目正常分配金额的核算明细。'
    ws = make_workspace(tmp_path, policy_tail='\n' + quote)
    if with_source:
        ws.record_clauses([{'file': 'policy.md', 'quote': quote, 'point': '输出逐项目分配金额'}])
    report = ws.drafts['S']['report']
    report['details'][0]['expr'] = 'amount'
    totals = copy.deepcopy(report['totals'])
    basis = {'objects': {'项目:A': {'amount': '10.00'}, '项目:B': {'amount': '20.00'}}}
    row_a = json.dumps(['项目奖', '项目:A'], ensure_ascii=False)
    row_b = json.dumps(['项目奖', '项目:B'], ensure_ascii=False)
    production = {row_a: Decimal(100), row_b: Decimal(200)}
    independent = {row_a: Decimal(10), row_b: Decimal(20)}
    spec = {'pool': 'amount / 10', 'details': {'项目奖': 'amount / 10'}}
    repair = ws._flow()._repair_pool_detail('S', ws._ruleset('S'), basis, spec, production, independent)
    assert bool(repair) == with_source
    row = ws.drafts['S']['report']['details'][0]
    if with_source:
        assert row['expr'] == ws._ruleset('S').pool.expr
        assert row['sources'] == [{'doc': 'policy.md', 'quote': quote}]
        assert repair['matched_objects'] == 2
    else:
        assert row['expr'] == 'amount'
    assert ws.drafts['S']['report']['totals'] == totals
    assert not ws._flow().current('review:S') and not ws._flow().current('approved:S')


@pytest.mark.parametrize('write_before_data_check', [False, True])
def test_distribution_field_mapping_is_checked_only_for_distribution(tmp_path, write_before_data_check):
    ws = make_workspace(tmp_path)
    # Saved drafts can still declare a field whose workbook mapping is unfinished.
    ws.datasets['people'].fields['adjusted_share'] = ws.datasets['people'].fields['share']
    levels = copy.deepcopy(ws.drafts['S']['allocation'])
    levels[1]['weight'] = 'adjusted_share'
    if write_before_data_check:
        ws.set_rules(scheme='S', allocation=levels)
    prepared(ws)
    if not write_before_data_check:
        ws.set_rules(scheme='S', allocation=levels)

    basis = ws.calculate_basis('S')
    assert basis['pool'] == '30.00'
    assert ws.review_basis('S')['status'] == 'PASS'
    assert ws._flow().approve('S', '测试确认人', basis['digest'])['status'] == 'PASS'

    allocation = ws.compile_formulas()
    assert allocation['status'] == 'FAIL'
    assert any('people' in problem and 'adjusted_share' in problem for problem in allocation['issues'])
    assert all('value_empty' not in problem for problem in allocation['issues'])
    assert ws._flow().current('approved:S')
    with pytest.raises(ToolError, match='adjusted_share'):
        ws.trial_run('S')
    assert not ws.results

    # The correct source is already known. Repairing only the weight preserves
    # the approval of the unchanged project amounts and clears the failed check.
    levels[1]['weight'] = 'share'
    ws.set_rules(scheme='S', allocation=levels)
    assert ws._flow().current('approved:S')
    assert ws.compile_formulas()['status'] == 'PASS'
    ws.trial_run('S')
    assert {row['name']: Decimal(row['final']) for row in ws.results['S']['R0'].people} == {
        '甲': Decimal(20), '乙': Decimal(10),
    }


def test_distribution_checks_person_report_column_before_calculation(tmp_path):
    ws = make_workspace(tmp_path)
    ws.datasets['people'].fields['recipient_code'] = ws.datasets['people'].fields['person'].model_copy(
        update={'if_empty': '未标识'},
    )
    ws.set_rules(scheme='S', report={**ws.drafts['S']['report'], 'people': 'recipient_code'})
    reviewed(ws)
    digest = fingerprint(ws.workflow['basis']['S'])
    ws._flow().approve('S', '测试确认人', digest)

    result = ws.compile_formulas()
    assert result['status'] == 'FAIL'
    assert any('field_unmapped' in problem and 'recipient_code' in problem and '(people)' in problem
               for problem in result['issues'])
    assert not ws.results


def test_equal_total_changed_object_invalidates_confirmation_after_restore(tmp_path):
    ws=reviewed(make_workspace(tmp_path));ws._flow().approve('S','财务',fingerprint(ws.workflow['basis']['S']))
    snap=case.snapshot(ws)
    clone=Workspace(FakeLLM([]),ws.inv);case.restore(clone,snap)
    assert clone._flow().current('approved:S')
    # Reversing entitlement across objects keeps the same total but changes financial semantics.
    clone.drafts['S']['pool']['expr']='30 - amount * 0.1'
    assert not clone._flow().current('approved:S')
    with pytest.raises(ToolError):clone.trial_run('S')


@pytest.mark.parametrize('changed_answer', [False, True])
def test_reuse_confirmation_after_fresh_review_only_with_identical_scope(tmp_path, changed_answer):
    from bonus_agent.extract_rules import Question

    ws = make_workspace(tmp_path)
    ws.llm.checker_llm._responder *= 2
    question = Question(id='q1', text='本批角色如何分配？', answer='按名单')
    ws.questions = [question]
    reviewed(ws)
    flow = ws._flow()
    digest = fingerprint(ws.workflow['basis']['S'])
    flow.approve('S', '财务', digest)
    ws.done = False
    ws.workflow['plan']['summary'] += '。补齐报表说明。'
    if changed_answer:
        question.answer = '按本期名单，单列待分配'
    assert not flow.current('approved:S')
    assert ws.compile_formulas()['status'] == 'PASS'
    assert ws.calculate_basis('S')['digest'] == digest
    assert not flow.current('approved:S')
    report = ws.review_basis('S')
    assert report['status'] == 'PASS'
    assert bool(report.get('confirmation_reused')) is not changed_answer
    assert flow.current('approved:S') is not changed_answer
    assert ws.done is changed_answer
    if not changed_answer:
        assert flow.state['receipts']['approved:S']['payload']['by'] == '财务'
        assert 'waiting' not in flow.state


def test_raw_query_survives_broken_unrelated_parent_and_uses_decimal(tmp_path):
    ws=make_workspace(tmp_path)
    # Simulate a saved, unresolved mapping. Raw source statistics must stay usable.
    ws.datasets['people'].link='missing_parent'
    result=ws.query_dataset('projects','sum(amount, of: "projects")')
    assert Decimal(result['value'])==300
    assert result['rows']==2
    assert ws.prepare_data()['status']=='FAIL'


def test_bad_independent_formula_never_gets_review_or_approval_receipt(tmp_path):
    ws=prepared(make_workspace(tmp_path));ws.calculate_basis('S')
    ws.llm.checker_llm=FakeLLM([json.dumps({'pool':'amount * 0.2','helpers':{},'tables':{},
        'details':{'项目奖':'amount * 0.2'},'totals':{'项目奖合计':'sum(amount, of: "projects") * 0.2'},'populations':population_spec(),'unclear':[]})])
    assert ws.review_basis('S')['status']=='FAIL'
    assert not ws._flow().current('review:S')
    with pytest.raises(ToolError):ws._flow().approve('S','财务',fingerprint(ws.workflow['basis']['S']))
    assert ws.review_basis('S')['status']=='FAIL'
    assert ws.review_basis('S')['cached']
    assert len(ws.llm.checker_llm.calls)==1
    assert ws.workflow['last_review']['examples'][0]['object'].startswith('项目:')


def test_independent_reviewer_failure_is_blocking(tmp_path):
    ws=prepared(make_workspace(tmp_path));ws.calculate_basis('S')
    def unavailable(*a,**k):raise LLMError('temporary outage')
    ws.llm.checker_llm.chat=unavailable
    assert ws.review_basis('S')['status']=='ERROR'
    assert ws.done and 'temporary outage' in ws.workflow['review_error']['error']
    assert not ws._flow().current('review:S')


def test_same_version_resume_waits_without_calling_primary_or_exporting_people(tmp_path):
    ws=reviewed(make_workspace(tmp_path));out=tmp_path/'out'
    files=case.fingerprint(ws.inv.files)
    case.save_case(out,{'rounds':[{'n':1,'at':'2026-11-16','files':files,'status':'AWAITING_TOTAL_CONFIRMATION'}],
                        'workspace':case.snapshot(ws)})
    llm=FakeLLM(lambda *a:pytest.fail('waiting for human must not call the model'));llm.workflow_mode='staged'
    ar=run_agent(llm,ws.inv.files,out,max_steps=3)
    assert ar.status=='AWAITING_TOTAL_CONFIRMATION',ar.error
    assert not ar.results and not list(out.glob('result*.xlsx'))
    assert (out/'总额预核算.xlsx').is_file()


@pytest.mark.parametrize('change_upstream', [False, True])
def test_restart_preserves_only_the_current_dependent_basis(tmp_path, monkeypatch, change_upstream):
    payout_quote = '暂缓发放比例为50%。'
    ws = make_workspace(tmp_path, policy_tail='\n' + payout_quote if change_upstream else '')
    plan = ws.workflow['plan']
    ws.plan_calculation(plan['summary'], plan['outputs'], plan['files'], ['S', '人员'])
    allocation = copy.deepcopy(ws.drafts['S']['allocation'])
    ws.set_rules(scheme='S', allocation=allocation[:1], report={**ws.drafts['S']['report'], 'people': None})
    ws.set_rules(scheme='人员', pool={'scope': '项目', 'expr': 'scheme_paid("S", "R0")',
                                   'sources': ws.drafts['S']['pool']['sources']},
                 allocation=allocation, report={'people': 'person'})
    prepared(ws)
    funding = ws.calculate_basis('S')
    assert ws.review_basis('S')['status'] == 'PASS'
    out = tmp_path/'out'
    save_staged_case(ws, out)
    case.confirm_totals(out, 'S', funding['digest'], '隔离测试确认人')

    response = {'pool': 'scheme_paid("S", "R0")', 'populations': {'项目': population_spec()['项目']}}
    primary = FakeLLM([{'tool': 'trial_run', 'args': {'scheme': 'S'}},
                       {'tool': 'calculate_basis', 'args': {'scheme': '人员'}},
                       {'tool': 'review_basis', 'args': {'scheme': '人员'}}])
    primary.workflow_mode = 'staged'
    primary.checker_required = True
    primary.checker_llm = FakeLLM([json.dumps(response, ensure_ascii=False)])
    pending = run_agent(primary, ws.inv.files, out, max_steps=3)
    assert pending.status == 'AWAITING_TOTAL_CONFIRMATION', [(s.tool, s.result) for s in pending.steps if not s.ok]
    assert pending.ws._flow().stage() == (6, '人员')
    assert not pending.results and len(primary.checker_llm.calls) == 1
    if change_upstream:
        old_digest = fingerprint(pending.ws.workflow['basis']['人员'])
        pending.ws.set_rules(scheme='S', payout=[{'id': '保留一半', 'scope': '项目', 'hold_ratio': '0.5',
                                                'sources': [{'doc': 'policy.md', 'quote': payout_quote}]}])
        assert pending.ws._flow().current('approved:S')
        assert not pending.ws._flow().current('review:人员')
        assert not pending.ws._flow().current('basis:人员')
        assert pending.ws._flow().stage() != (6, '人员')
        pending.ws.record_facts([{'name': '暂缓发放比例', 'file': 'policy.md', 'quote': payout_quote, 'value': '50%'}])
        pending.ws.bind_facts([{'name': '暂缓发放比例', 'targets': ['S::/payout/0/hold_ratio']}])
        assert pending.ws.compile_formulas()['phase'] == 'all'
        payout_task, = (task['id'] for task in pending.ws.workflow['construction']['tasks'].values()
                        if task['scheme'] == 'S' and task['target'] == '/payout')
        assert pending.ws.check_formula_task(payout_task)['status'] == 'PASS'
        basis = pending.ws.calculate_basis('人员')
        assert basis['pool'] == '15.00' and basis['digest'] != old_digest
        assert not pending.ws._flow().current('review:人员')
        return

    monkeypatch.setattr(primary, 'context_window', lambda: pytest.fail('restoring the same wait must not call the service'))
    resumed = run_agent(primary, ws.inv.files, out, max_steps=1)
    assert resumed.status == 'AWAITING_TOTAL_CONFIRMATION' and resumed.ws._flow().stage() == (6, '人员')
    assert not resumed.results and len(primary.checker_llm.calls) == 1
    assert not list(out.glob('result*.xlsx'))
    digest = fingerprint(resumed.ws.workflow['basis']['人员'])
    case.confirm_totals(out, '人员', digest, '隔离测试确认人')
    saved = case.load_case(out)
    assert [r['scheme'] for r in saved['total_confirmations']] == ['S', '人员']
    settle = FakeLLM([{'tool': 'trial_run', 'args': {'scheme': '人员'}}])
    settle.workflow_mode = 'staged'
    settle.checker_required = True
    settle.checker_llm = primary.checker_llm
    calculated = run_agent(settle, ws.inv.files, out, max_steps=1)
    assert not calculated.results and not list(out.glob('result*.xlsx'))
    recipients = calculated.ws.results['人员']['R0'].people
    assert {r['name']: r['final'] for r in recipients} == {'甲': '20.00', '乙': '10.00'}


def test_existing_unresolved_basis_review_waits_without_new_requests(tmp_path, monkeypatch):
    ws = prepared(make_workspace(tmp_path))
    ws.calculate_basis('S')
    ws.llm.checker_llm = FakeLLM([json.dumps({'unclear': ['区域预算缺少空白行口径。']}, ensure_ascii=False)])
    with pytest.raises(ToolError, match='区域预算'):
        ws.review_basis('S')
    out = tmp_path / 'out'
    save_staged_case(ws, out)

    llm = _staged_llm(ws, lambda *args: pytest.fail('same unresolved evidence must stop before a new model call'))
    monkeypatch.setattr(llm, 'context_window', lambda: pytest.fail('no service discovery while waiting'))
    result = run_agent(llm, ws.inv.files, out, max_steps=1)

    assert result.status == 'AWAITING_REVIEW'
    assert not result.results and not (out / 'result.xlsx').exists()
    assert '区域预算' in result.ws.workflow['review_error']['error']
    assert not case.load_case(out).get('total_confirmations')


def test_bounded_context_has_indices_not_full_original_documents(tmp_path):
    ws=make_workspace(tmp_path)
    text=task_card(ws)
    assert '当前任务' not in text and 'read_workspace' in text
    assert '每个项目按销售金额的10%' not in text
    llm=FakeLLM([]);llm.max_input_tokens=100
    with pytest.raises(LLMError,match='预算'):guard_messages([{'role':'user','content':'数据'*200}],llm)


def test_official_context_metadata_is_recognized(monkeypatch):
    llm=LLM('test','deepseek-flash','http://localhost','')
    client=SimpleNamespace(models=SimpleNamespace(list=lambda:[SimpleNamespace(id='deepseek-flash',
        context_window=1048576,max_output_tokens=393216,model_extra={})]))
    monkeypatch.setattr('bonus_agent.llm.openai_client',lambda *a:client)
    assert llm.context_window()==1048576
    assert llm._max_output_tokens==393216


def save_staged_case(ws, out):
    case.save_case(out,{'rounds':[{'n':1,'at':'2026-11-16','files':case.fingerprint(ws.inv.files),
        'source_inputs':[{'name':p.name,'path':str(p),'hash':case.sha256(p)} for p in ws.inv.files],
        'status':'AWAITING_TOTAL_CONFIRMATION'}],'workspace':case.snapshot(ws)})


def test_primary_stops_after_review_even_with_later_writes_in_same_reply(tmp_path):
    ws=prepared(make_workspace(tmp_path));out=tmp_path/'out';save_staged_case(ws,out)
    llm=FakeLLM([{'tool':'calculate_basis','args':{'scheme':'S'}},[
        {'tool':'review_basis','args':{'scheme':'S'}},
        {'tool':'note','args':{'topic':'late','text':'must not execute after stopping'}}]])
    llm.workflow_mode='staged';llm.checker_required=True;llm.checker_llm=ws.llm.checker_llm
    ar=run_agent(llm,ws.inv.files,out,max_steps=5)
    assert ar.status=='AWAITING_TOTAL_CONFIRMATION',[(s.tool,s.result[:300]) for s in ar.steps if not s.ok]
    assert not ar.results and 'late' not in ar.ws.notes
    assert (out/'总额预核算.xlsx').exists() and not (out/'result.xlsx').exists()
    assert not any(s.tool=='trial_run' for s in ar.steps)


def test_case_confirmation_recomputes_and_rejects_changed_input(tmp_path):
    ws=reviewed(make_workspace(tmp_path));out=tmp_path/'out';save_staged_case(ws,out)
    basis=ws.workflow['basis']['S']
    rec=case.confirm_totals(out,'S',fingerprint(basis),'财务')
    assert rec['status']=='PASS'
    same=case.confirm_totals(out,'S',fingerprint(basis),'第二次提交')
    assert same['already_confirmed'] and same['by']=='财务'
    assert len(case.load_case(out)['total_confirmations'])==1
    clone=Workspace(FakeLLM([]),ws.inv);case.restore(clone,case.load_case(out)['workspace'])
    assert clone._flow().current('approved:S')
    (tmp_path/'policy.md').write_text('原始规则已修改',encoding='utf8')
    with pytest.raises(case.ConfirmError,match='变化'):case.confirm_totals(out,'S',fingerprint(basis),'财务')


def test_offline_confirmation_preserves_source_reviewed_withdrawal(tmp_path):
    from bonus_agent.extract_rules import Question

    ws = make_workspace(tmp_path)
    evidence = [{'file': 'policy.md', 'quote': '每个项目按销售金额的10%计算奖金'}]
    ws.questions = [Question(id='q1', text='材料没有定义项目奖金比例，是否需要补充比例？')]
    ws.llm.checker_llm._responder.insert(0, json.dumps({
        'decision': 'withdraw', 'kind': 'false_premise', 'reason': '制度原文明确规定了适用比例。',
        'evidence': evidence,
    }, ensure_ascii=False))
    assert ws.review_question('q1', '请核对比例是否已经写在制度中。', evidence)['status'] == 'WITHDRAWN'
    reviewed(ws)
    out = tmp_path / 'out'
    save_staged_case(ws, out)
    prior = case.sha256(out / 'case.json')
    with pytest.raises(case.ConfirmError, match='总额版本已变化'):
        case.confirm_totals(out, 'S', 'wrong-digest', '测试案卷财务')
    assert case.sha256(out / 'case.json') == prior
    rec = case.confirm_totals(out, 'S', fingerprint(ws.workflow['basis']['S']), '测试案卷财务')
    assert rec['status'] == 'PASS'
    saved = case.load_case(out)
    assert saved['workspace']['questions'][0]['withdrawn'] == ws.questions[0].withdrawn
    assert saved['workspace']['questions'][0]['answer'] is None
    assert len(saved['total_confirmations']) == 1


def test_case_writer_lock_blocks_run_and_confirmation_before_side_effects(tmp_path):
    out=tmp_path/'out'
    llm=FakeLLM([])
    with case.case_lock(out):
        with pytest.raises(ToolError,match='案卷正在计算'):
            run_agent(llm, [], out)
        with pytest.raises(case.ConfirmError,match='案卷正在计算'):
            case.confirm_totals(out, 'S', 'digest', '财务')
    assert not llm.calls and not (out/case.CASE_FILE).exists()
    with case.case_lock(out):
        assert not (out/case.PROCESSING_NOTICE).exists()


def test_modified_basis_cannot_be_approved_even_if_browser_digest_matches(tmp_path):
    ws=reviewed(make_workspace(tmp_path));ws.workflow['basis']['S']['pool']='999.00'
    out=tmp_path/'out';save_staged_case(ws,out)
    with pytest.raises(case.ConfirmError,match='不一致'):
        case.confirm_totals(out,'S',fingerprint(ws.workflow['basis']['S']),'财务')
    assert 'approved:S' not in case.load_case(out)['workspace']['workflow']['receipts']


def test_approved_resume_runs_allocation_but_never_skips_final_validation(tmp_path):
    ws=make_workspace(tmp_path)
    ws.said='示例案卷已有的书面核算说明。\n同一版继续使用这份说明。'
    ws.report_required=True
    reviewed(ws)
    out=tmp_path/'out';save_staged_case(ws,out)
    case.confirm_totals(out,'S',fingerprint(ws.workflow['basis']['S']),'财务')
    llm=FakeLLM([{'tool':'trial_run','args':{'scheme':'S'}}]);llm.workflow_mode='staged'
    ar=run_agent(llm,ws.inv.files,out,max_steps=1)
    assert not ar.ok and not ar.results and not (out/'result.xlsx').exists()
    assert ar.ws.said==ws.said and ar.ws._flow().current('approved:S')
    assert ar.ws.report_required
    assert 'S' in ar.ws.results, [(s.tool, s.result[:1200]) for s in ar.steps]
    assert ar.ws.results['S']['R0'].pool=='30.00'
    assert ar.status=='FAILED' and '最大步数' in ar.error


def test_export_rechecks_person_view_before_publishing_any_results(tmp_path, monkeypatch):
    from bonus_agent.agent import AgentResult, write_agent_outputs

    ws = reviewed(make_workspace(tmp_path))
    ws._flow().approve('S', '测试案卷财务', fingerprint(ws.workflow['basis']['S']))
    ws.trial_run('S')
    rs = ws._ruleset('S')
    result = ws.results['S']['R0']
    replay = copy.deepcopy(result)
    replay.people[0]['final'] = '99.00'
    monkeypatch.setattr('bonus_agent.agent.compute', lambda *args, **kwargs: replay)
    ws.done = True
    out = tmp_path / 'out'
    ar = AgentResult(ws, [], rs, result, rulesets={'S': rs}, results=ws.results)

    with pytest.raises(ToolError, match='复算.*人员'):
        write_agent_outputs(ar, out)
    assert not any((out / name).exists() for name in ('result.xlsx', 'results.json', 'engine_results.json'))


def test_recipient_sum_rounding_is_preserved_in_basis_and_final_engine():
    from bonus_core.dsl.models import RuleSet
    from bonus_core.data.table import from_records
    rs=RuleSet.model_validate({'name':'按人汇总后舍入','version':'v1','datasets':{
        'staff':{'key':'emp','fields':{'emp':{'type':'id'}}},
        'work':{'link':'emp','fields':{'emp':{'type':'id'},'amount':{'type':'money'}}}},
        'pool':{'scope':'人员','expr':'sum(amount, of: "work")'},
        'allocation':[{'level':'人员','key':'emp'}]})
    data=from_records(rs,{'staff':[{'emp':'A'}],'work':[{'emp':'A','amount':'.015'},{'emp':'A','amount':'.015'}]})
    basis=Engine.prepare(rs,data).calculate_basis()
    final=Engine(rs,data).compute()
    assert basis['objects']['人员:A']['amount']==final.nodes['人员:A'].final=='0.03'


@pytest.mark.reread
@pytest.mark.verify
def test_full_staged_financial_validation_without_skipping_checker_or_samples(tmp_path):
    ws=reviewed(make_workspace(tmp_path))
    ws._flow().approve('S','财务',fingerprint(ws.workflow['basis']['S']))
    ws.trial_run('S')
    ws.llm.checker_llm.reread=json.dumps({'items':[{'name':'项目奖金','columns':['projects/amount'],
        'constants':[{'value':10,'unit':'%'}]}]})
    ws.llm.checker_llm.calc=json.dumps({'schemes':{'S':{'pool':'amount * 0.1',
        'helpers':{},'tables':{},'details':{'项目奖':'amount * 0.1'},
        'people':{'key':'person','mode':'allocation','weights':{'参与':'share'}}}}})
    assert ws.review_calculation()['status']=='PASS'
    targets=ws._verify_targets('S')
    assert targets
    for node,kind,_ in targets:
        expr=('sum(amount, of: "projects") * 0.1' if node=='全部' else
              'amount * 0.1' if node.startswith('项目:') else 'amount * 0.1 * share')
        ws.verify(node,expr,'每个项目按销售金额的10%计算奖金，按名单比例分配。',scheme='S',amount=kind)
    assert ws.validation_report()['status']=='PASS'
    assert ws.finish('项目按10%核算，按名单比例分配',review=False)=='已完成'


@pytest.mark.reread
@pytest.mark.verify
@pytest.mark.parametrize(
    'mode_text, threshold, rates, amounts, share, amount_column, expected_projects, expected_people',
    [
        ('整额分档', 100, (10, 20), (100, 140), '0.25', '到账金额', ('20.00', '28.00'), ('27.00', '21.00')),
        ('超额累进', 150, (8, 15), (120, 190), '0.60', '本期业绩', ('9.60', '18.00'), ('20.40', '7.20')),
    ],
)
def test_changed_rules_and_inputs_reach_verified_people_export(
    tmp_path, mode_text, threshold, rates, amounts, share, amount_column, expected_projects, expected_people,
):
    from bonus_agent.agent import AgentResult, write_agent_outputs

    low, high = rates
    quote = f'项目奖励按{mode_text}计算'
    policy = tmp_path / '新制度.md'
    cutoff = f'档位为{threshold}元，等于档位时归高档'
    policy.write_text(f'{quote}。低档费率为{low}%，高档费率为{high}%，{cutoff}。按名单份额分配。', encoding='utf-8')
    workbook = tmp_path / '新台账.xlsx'
    wb = openpyxl.Workbook()
    projects = wb.active
    projects.title = '业绩'
    projects.append(['立项号', amount_column])
    projects.append(['A', amounts[0]])
    projects.append(['B', amounts[1]])
    people = wb.create_sheet('名单')
    people.append(['行号', '立项号', '工号', '份额'])
    people.append(['A1', 'A', 'E01', '1'])
    people.append(['B1', 'B', 'E01', share])
    people.append(['B2', 'B', 'E02', str(1 - Decimal(share))])
    wb.save(workbook)
    wb.close()

    low_ratio, high_ratio = (Decimal(r) / 100 for r in rates)
    independent = (f'if(amount < {threshold}, amount * {low_ratio}, amount * {high_ratio})'
                   if mode_text == '整额分档' else
                   f'min(amount, {threshold}) * {low_ratio} + max(amount - {threshold}, 0) * {high_ratio}')
    source = [{'doc': policy.name, 'quote': quote}]
    populations = {k: {**v, 'sources': source} for k, v in population_spec().items()}
    llm = FakeLLM([])
    llm.checker_required = True
    llm.checker_llm = FakeLLM([json.dumps({
        'pool': independent, 'helpers': {}, 'tables': {}, 'details': {'项目奖': independent},
        'totals': {'项目奖合计': f'sum({independent}, of: "projects")'},
        'populations': populations, 'unclear': [],
    })])
    ws = Workspace(llm, build_inventory([policy, workbook]))
    ws.workflow['enabled'] = True
    ws.plan_calculation('按新制度核算并按名单分配', ['项目底稿', '总额', '人员金额'],
                        {policy.name: 'rule', workbook.name: 'data'}, ['S'])
    ws.record_clauses([{'file': policy.name, 'quote': quote, 'point': quote}])
    ws.record_facts([{'name': name, 'file': policy.name, 'quote': text, 'value': value}
                     for name, text, value in [('rate_low', f'低档费率为{low}%', f'{low}%'),
                                               ('rate_high', f'高档费率为{high}%', f'{high}%'),
                                               ('cutoff', cutoff, f'{threshold}元')]])
    ws.define_dataset('projects', workbook.name, '业绩', {
        'pid': {'column': '立项号', 'type': 'id'}, 'amount': {'column': amount_column, 'type': 'money'},
    }, key='pid')
    ws.define_dataset('people', workbook.name, '名单', {
        'rid': {'column': '行号', 'type': 'id'}, 'pid': {'column': '立项号', 'type': 'id'},
        'person': {'column': '工号', 'type': 'text'}, 'share': {'column': '份额', 'type': 'ratio'},
    }, key='rid')
    ws.compile_table('rate', [{'value': 'rate_low'}, {'lower': 'cutoff', 'value': 'rate_high'}],
                     scheme='S', algorithm_basis={'file': policy.name, 'quote': quote}, lower_inclusive=True)
    formula = 'tier(rate, amount)'
    ws.set_rules('S', pool={'scope': '项目', 'expr': formula, 'sources': source},
                 allocation=[{'level': '项目', 'key': 'pid'},
                             {'level': '参与', 'key': 'rid', 'parent_key': 'pid', 'weight': 'share'}],
                 report={'people': 'person', 'details': [{'name': '项目奖', 'level': '项目', 'expr': formula, 'sources': source}],
                         'distributions': [{'name': '项目分配去向', 'level': '项目', 'total': formula, 'sources': source}],
                         'totals': [{'name': '项目奖合计', 'level': 'global', 'expr': f'sum({formula}, of: "projects")',
                                     'sources': source}]})
    ws.cover_clauses({'C1': 'pool'})
    reviewed(ws)
    basis = ws.workflow['basis']['S']
    assert [basis['objects'][f'项目:{key}']['amount'] for key in 'AB'] == list(expected_projects)
    assert not ws.results
    ws._flow().approve('S', '测试确认人', fingerprint(basis))
    ws.trial_run('S')
    llm.checker_llm.reread = json.dumps({'items': [{'name': '项目奖', 'columns': [f'projects/{amount_column}'],
        'constants': [{'value': r, 'unit': '%'} for r in rates] + [{'value': threshold, 'unit': '元'}]}]})
    llm.checker_llm.calc = json.dumps({'schemes': {'S': {'pool': independent, 'helpers': {}, 'tables': {},
        'details': {'项目奖': independent}, 'people': {'key': 'person', 'mode': 'allocation', 'weights': {'参与': 'share'}}}}})
    assert ws.review_calculation()['status'] == 'PASS'
    for node, kind, _ in ws._verify_targets('S'):
        expr = (f'sum({independent}, of: "projects")' if node == '全部' else
                independent if node.startswith('项目:') else f'({independent}) * share')
        ws.verify(node, expr, quote, scheme='S', amount=kind)
    assert ws.validation_report()['status'] == 'PASS'
    result = ws.results['S']['R0']
    assert {p['name']: p['final'] for p in result.people} == dict(zip(('E01', 'E02'), expected_people))
    assert ws.finish('已按当期制度与名单核算', review=False) == '已完成'
    rules = ws._ruleset('S')
    paths = write_agent_outputs(AgentResult(ws, [], rules, result, rulesets={'S': rules}, results=ws.results), tmp_path / 'out')
    output = openpyxl.load_workbook(paths['excel'], read_only=True, data_only=True)
    try:
        actual = {row[0]: Decimal(str(row[2])).quantize(Decimal('0.01')) for row in output['汇总核算'].values
                  if row[0] in ('E01', 'E02')}
        assert actual == dict(zip(('E01', 'E02'), map(Decimal, expected_people)))
    finally:
        output.close()


def test_rewording_production_formula_cannot_resample_a_disagreeing_reference(tmp_path):
    ws=prepared(make_workspace(tmp_path));ws.calculate_basis('S')
    ws.llm.checker_llm=FakeLLM([json.dumps({'pool':'amount * 0.2','helpers':{},'tables':{},
        'details':{'项目奖':'amount * 0.2'},'totals':{'项目奖合计':'sum(amount, of: "projects") * 0.2'},'populations':population_spec(),'unclear':[]})])
    assert ws.review_basis('S')['status']=='FAIL'
    ws.set_rules(scheme='S',pool={'scope':'项目','expr':'amount * 0.1 + 0'})
    assert ws.compile_formulas()['status']=='PASS'
    ws.calculate_basis('S')
    report=ws.review_basis('S')
    assert report['status']=='FAIL' and report['cached']
    assert len(ws.llm.checker_llm.calls)==1


def test_new_material_version_requires_a_new_inventory_plan(tmp_path):
    ws=make_workspace(tmp_path);saved=case.snapshot(ws)
    extra=tmp_path/'补充说明.md';extra.write_text('不改变金额的补充规则也须重新核查。',encoding='utf8')
    clone=Workspace(FakeLLM([]),build_inventory([*ws.inv.files,extra]));case.restore(clone,saved)
    assert clone._flow().stage()[0]==0
    with pytest.raises(ToolError,match='材料清单'):clone.prepare_data()


def _staged_llm(ws, script):
    llm=FakeLLM(script)
    llm.workflow_mode='staged';llm.checker_required=True;llm.checker_llm=ws.llm.checker_llm
    return llm


def test_edit_that_invalidates_passed_checks_is_rechecked_without_going_back(tmp_path):
    """实测：编公式时补一条字段说明，数据回执失效，退回“准备数据”、清空对话，模型把检查和读取整套重做。"""
    ws=prepared(make_workspace(tmp_path));out=tmp_path/'out';save_staged_case(ws,out)
    edit={'tool':'update_dataset','args':{'name':'projects','changes':{'fields':{'amount':{'column':'amount','type':'money','desc':'销售金额'}}}}}
    ar=run_agent(_staged_llm(ws,[edit,{'tool':'read_workspace','args':{'section':'facts'}}]),ws.inv.files,out,max_steps=2)
    tools=[s.tool for s in ar.steps]
    assert '（自动复核）' in tools and '（进入新阶段）' not in tools, [(s.tool,s.result[:200]) for s in ar.steps]
    assert 'prepare_data 通过' in ar.steps[tools.index('（自动复核）')].result


def test_unchanged_reads_are_carried_across_a_context_reset(tmp_path):
    """实测每次清空对话后都把台账、条款原样再读一遍：没变的内容随说明带过去，再读只回一句。"""
    ws=prepared(make_workspace(tmp_path));out=tmp_path/'out';save_staged_case(ws,out)
    read={'tool':'read_workspace','args':{'section':'facts'}}
    ar=run_agent(_staged_llm(ws,[read,{'tool':'calculate_basis','args':{'scheme':'S'}},read]),ws.inv.files,out,max_steps=3)
    stage=next(s for s in ar.steps if s.tool=='（进入新阶段）')
    assert '之前读过、内容没变的工作区内容' in stage.result and '提成率' in stage.result
    again=[s for s in ar.steps if s.tool=='read_workspace'][-1]
    assert again.ok and '不用再读' in again.result and '提成率' not in again.result
