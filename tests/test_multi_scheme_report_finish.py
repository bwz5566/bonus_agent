"""Basis schemes keep their own evidence without masquerading as recipient reports."""
import copy
import json
from dataclasses import replace
from pathlib import Path

import openpyxl
import pytest

from bonus_agent.agent import AgentResult, ToolError, write_agent_outputs
from test_settlement_rounding import recipient_model

pytestmark = [pytest.mark.verify, pytest.mark.reread, pytest.mark.evidence]


def funded_model(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    ws = recipient_model(tmp_path)
    source = [{'doc': 'data.xlsx', 'quote': 'amount'}]
    ws.set_rules(scheme='Basis', pool={'scope': 'orders', 'expr': 'amount'},
                 allocation=[{'level': 'orders', 'key': 'pid'}],
                 report={'totals': [{'name': 'Funding', 'level': 'orders', 'expr': 'amount'}],
                         'details': [{'name': 'orders', 'level': 'orders', 'expr': 'amount', 'sources': source}]})
    variables = copy.deepcopy(ws.drafts['S']['variables'])
    variables['project_funding'] = {'scope': 'orders', 'expr': 'scheme_paid("Basis")'}
    variables['recipient_total']['expr'] = 'sum(project_funding / order_count, of: "shares")'
    ws.set_rules(scheme='S', variables=variables)
    ws.trial_run()
    ws.report_required = True
    ws.llm.calc = json.dumps({'schemes': {
        'Basis': {'pool': 'amount', 'details': {'orders': 'amount'}},
        'S': {'pool': 'sum(amount / independently_counted, of: "shares")',
              'helpers': {'independently_counted': {'scope': 'orders', 'expr': 'count(of: "shares")'}},
              'people': {'mode': 'after_sum', 'key': 'person',
                         'expr': 'sum(amount / independently_counted, of: "shares")'},
              'details': {'orders': 'amount'}},
    }})
    return ws


def verify_all(ws):
    for scheme in ws.schemes:
        for node, kind, _ in ws._verify_targets(scheme):
            expr = ('amount' if scheme == 'Basis' else
                    'sum(amount / independently_counted, of: "shares")')
            ws.verify(node, expr, 'amount', scheme=scheme, amount=kind,
                      helpers={'independently_counted': {'scope': 'orders', 'expr': 'count(of: "shares")'}})


def finished_report(tmp_path):
    ws = funded_model(tmp_path)
    verify_all(ws)
    ws.finish('Verified', review=False)
    rulesets = {name: ws._ruleset(name) for name in ws.schemes}
    return AgentResult(ws, [], rulesets['S'], ws.results['S']['R0'], rulesets=rulesets, results=ws.results)


def test_funding_workbook_does_not_list_projects_as_people(tmp_path):
    report = finished_report(tmp_path)
    files = write_agent_outputs(report, tmp_path / 'out')
    book = openpyxl.load_workbook(files['excel:Basis:R0'], data_only=True, read_only=True)
    try:
        assert all(row[0] != '二、分配人员金额' for row in book['汇总核算'].values)
    finally:
        book.close()


def test_funding_only_scheme_can_be_recomputed_by_public_trial_tools(tmp_path):
    ws = funded_model(tmp_path)
    ws.set_rules(scheme='Basis', allocation=[])

    ws.trial_run('Basis')
    funding = ws.results['Basis']['R0']
    assert funding.funding and not funding.people
    assert ws.evaluate('amount', node='orders:P', scheme='Basis').startswith('amount = ')

    ws.trial_run('S')
    assert ws.results['S']['R0'].pool == funding.pool
    assert not ws._report_output_gaps()


@pytest.mark.parametrize(('literal_source', 'scope_mode'), [
    (True, 'monolithic'), (False, 'monolithic'), (True, 'reused'), (True, 'changed'), (True, 'invalid_saved'),
    (True, 'native'), (True, 'terminal'),
])
def test_material_question_resumes_only_the_funded_settlement(tmp_path, literal_source, scope_mode, monkeypatch):
    from bonus_agent import case, reread
    from bonus_agent.agent import Workspace
    from bonus_agent.auto import build_inventory
    from bonus_agent.llm import FakeLLM
    from bonus_core.dsl.phases import basis_rules

    draft = funded_model(tmp_path)
    scope_rule = '每个项目按销售金额确认可分配资金'
    exception = '销售负责人直接负责的客户，执行人份额转给销售负责人'
    policy = tmp_path / 'policy.md'
    policy.write_text(scope_rule + '\n' + exception, encoding='utf-8')
    ws = Workspace(FakeLLM([]), build_inventory([*draft.inv.files, policy]))
    case.restore(ws, case.snapshot(draft))
    ws.set_rules(scheme='Basis', allocation=[])
    ws.drafts['S']['report']['distributions'][0]['source_funding'] = {
        'scheme': 'Basis', 'level': 'orders', 'allocated': 'sum(project_funding / order_count, of: "shares")',
    }
    ws.workflow['enabled'] = True
    ws.plan_calculation('先核准资金再分配', ['项目资金', '人员分配'], {'data.xlsx': 'data', 'policy.md': 'rule'}, ['Basis', 'S'],
                        output_targets={'项目资金': ['Basis::/report/details', 'Basis::/report/totals'],
                                        '人员分配': ['S::/report/people', 'S::/report/distributions']})
    ws.set_scheme_datasets('Basis', ['orders'])
    if scope_mode == 'terminal':
        ws.set_scheme_datasets('S', ['orders', 'shares', 'people'])
    ws.record_clauses([{'file': policy.name, 'quote': scope_rule, 'point': '项目资金来源'}])
    ws.record_facts([{'name': '项目金额来源', 'file': policy.name, 'quote': scope_rule, 'value': '销售金额'}])
    ws.cover_clauses({'C1': 'pool'})
    selection = {'dataset': 'orders', 'key': 'pid', 'filter': {}, 'period': None, 'window': 'period',
                 'sources': [{'doc': policy.name, 'quote': scope_rule}]}
    text = '请确认本期是否按名单中的人员归属发放'
    choices = ['按名单发放', '按例外清单发放']
    evidence = [{'file': policy.name, 'quote': exception}]
    disagreement = {'unclear': ['执行人份额是否改由直接负责客户的负责人领取'],
                    'settlement_question': {'text': text, 'options': choices, 'sources': [
                        {'doc': policy.name, 'quote': exception if literal_source else '材料没有这句原文'},
                    ]}}
    ws.llm.checker_llm = FakeLLM([json.dumps({
        'pool': 'amount', 'details': {'orders': 'amount'}, 'totals': {'Funding': 'amount'},
        'populations': {'orders': selection, 'rows:orders': {k: v for k, v in selection.items() if k != 'key'}},
    }), json.dumps(disagreement)])
    ws.llm.checker_required = scope_mode in ('native', 'terminal')
    flow = ws._flow()
    assert ws.prepare_data()['status'] == 'PASS'
    assert flow.compile('basis')['status'] == 'PASS'
    digest = ws.calculate_basis('Basis')['digest']
    assert ws.review_basis('Basis')['status'] == 'PASS'
    assert flow.approve('Basis', '财务', digest)['status'] == 'PASS'
    ws.done = False
    assert flow.compile('all')['status'] == 'PASS'
    ws.trial_run('Basis')
    assert reread.calc(ws, {})['reused_funding'] == ['Basis']
    assert len(ws.llm.checker_llm.calls) == 1
    funding = ws.results['Basis']['R0'].funding
    node = next(iter(funding))
    before = funding[node]
    funding[node] = '9000'
    assert not reread._reviewed_funding(ws, ['Basis'])[0]
    funding[node] = before
    if literal_source:
        sent = []

        def sample(_workspace, messages, names):
            sent.append(messages[1]['content'])
            assert names == ['S']
            return {'schemes': {'S': {'pool': 'amount'}}, 'error': '', 'unclear': []}

        with monkeypatch.context() as review_request:
            review_request.setattr(reread, '_sample', sample)
            ws.results['S'] = draft.results['S']
            detail = ws.drafts['S']['report']['details'][0]
            detail['desc'] = 'PRIVATE_AUTHOR_RESULT_DESCRIPTION'
            packet = reread.calc(ws, {})
            detail.pop('desc')
            ws.results.pop('S')
        assert set(packet['schemes']) == {'Basis', 'S'} and packet['reused_funding'] == ['Basis']
        assert 'PRIVATE_AUTHOR_RESULT_DESCRIPTION' not in sent[0]
        assert '方案「Basis」：' not in sent[0] and 'scheme_paid(资金方案名' in sent[0]
    basis_spec = _settlement_spec(scope_rule)
    if scope_mode != 'monolithic':
        checker = ws.llm.checker_llm
        checker.basis_review_split = True
        checker._responder.insert(1, json.dumps({'populations': basis_spec['populations']}, ensure_ascii=False))
    ws.calculate_basis('S')
    with pytest.raises(ToolError, match='未明确口径'):
        ws.review_basis('S')

    if literal_source:
        question, = ws.pending
        assert question.text == text and question.options == choices and question.evidence[0]['ok']
        qid = question.id
    else:
        assert flow.evidence_wait() and not ws.pending and not flow.state.get('settlement_questions')
        with pytest.raises(ToolError, match='逐字核对'):
            flow.record_settlement_question('S', text, choices, [{'file': 'data.xlsx', 'quote': 'invented'}])
        qid = flow.record_settlement_question('S', text, choices, evidence)
    assert flow.record_settlement_question('S', text, choices, evidence) == qid
    assert flow.evidence_wait()['question'] == qid
    assert flow.current('approved:Basis') and not flow.current('approved:S')

    restored = Workspace(ws.llm, ws.inv)
    case.restore(restored, json.loads(json.dumps(case.snapshot(ws), ensure_ascii=False)))
    flow = restored._flow()
    assert restored._pause_unresolved_review()
    case.apply_answers(restored.questions, [{'id': qid, 'answer': choices[0], 'answered_by': '财务'}])
    assert flow.evidence_wait() is None
    assert flow.current('approved:Basis') and not flow.current('approved:S')
    for name in ['Basis', 'S']:
        view = flow._review_workspace(name, basis_rules(restored._ruleset(name)))
        assert (choices[0] in reread.material(view)) == (name == 'S')
    assert len(ws.llm.checker_llm.calls) == (2 if scope_mode == 'monolithic' else 3)

    if literal_source:
        _finish_funded_answer(ws, tmp_path / 'formal-output', digest, qid, choices[0], scope_rule, basis_spec, scope_mode)

    # Changes in funding or the actual scheme relationship are still binding.
    restored.drafts['S']['variables']['project_funding']['expr'] = 'amount'
    assert not flow.current('approved:Basis')

    # A change to the supporting files must require a new interpretation.
    policy.write_text(scope_rule + '\n' + exception + '\n已提供本批例外清单', encoding='utf-8')
    changed = Workspace(ws.llm, build_inventory([*draft.inv.files, policy]))
    case.restore(changed, case.snapshot(ws))
    assert changed._flow().withdraw_stale_settlement_answers([{'id': qid, 'answer': choices[0]}]) == []
    assert not changed.pending and changed.questions[0].withdrawn


def _settlement_spec(rule):
    # The workbook has one cent of funding and three equal roster lines, two for B.
    # These declarations independently count the source lines; the expected cents below
    # are calculated from the fixture, independently of the engine and report.
    formula = 'sum(amount / roster_count, of: "shares")'
    helpers = {'roster_count': {'scope': 'orders', 'expr': 'count(of: "shares")'}}
    scope = {'filter': {}, 'period': None, 'window': 'period', 'sources': [{'doc': 'policy.md', 'quote': rule}]}
    return {
        'pool': formula, 'helpers': helpers, 'details': {'orders': 'amount'},
        'totals': {'理论金额': 'sum(amount, of: "orders")'},
        'populations': {
            'people': {**scope, 'dataset': 'people', 'key': 'person'},
            'orders': {**scope, 'dataset': 'orders', 'key': 'pid'},
            'rows:orders': {**scope, 'dataset': 'orders'},
            'rows:shares': {**scope, 'dataset': 'shares'},
        },
    }


def _finish_funded_answer(ws, out, funding_digest, qid, answer, rule, basis_spec, scope_mode):
    from decimal import Decimal

    from bonus_agent import case
    from bonus_agent.agent import run_agent
    from bonus_agent.llm import FakeLLM

    formula, helpers = basis_spec['pool'], basis_spec['helpers']
    pool = {k: basis_spec[k] for k in ('pool', 'helpers')} | {'tables': {}}
    reports = {k: basis_spec[k] for k in ('details', 'totals')}
    populations = {'populations': basis_spec['populations']}
    expected_parts = []
    if scope_mode == 'monolithic':
        replies = [basis_spec]
    elif scope_mode in ('reused', 'native', 'terminal'):
        replies = [{**pool, 'keep_reviewed_population': True}, reports]
        expected_parts = ['combined', 'reports']
        if scope_mode == 'terminal':
            replies[0].update(unclear=None, terminal_review={
                'people': {'mode': 'after_sum', 'key': 'person', 'expr': formula},
                'details': {'orders': 'amount'}, 'pending': {},
                'items': [{'name': '项目资金', 'columns': ['orders/amount']}],
            })
    elif scope_mode == 'changed':
        replies = [{'keep_reviewed_population': False}, populations, pool, reports]
        expected_parts = ['combined', 'population', 'pool', 'reports']
    else:
        replies = [populations, pool, reports]
        expected_parts = ['population', 'pool', 'reports']

    checker = FakeLLM([json.dumps(item, ensure_ascii=False) for item in replies])
    checker.basis_review_split = scope_mode != 'monolithic'
    checker.reread = json.dumps({'items': [{'name': '项目资金', 'columns': ['orders/amount']}]}, ensure_ascii=False)
    checker.calc = json.dumps({'schemes': {'S': {
        'pool': formula, 'helpers': helpers, 'details': {'orders': 'amount'},
        'people': {'mode': 'after_sum', 'key': 'person', 'expr': formula},
    }}})
    primary_steps = [
        {'tool': 'calculate_basis', 'args': {'scheme': 'S'}},
        {'tool': 'review_basis', 'args': {'scheme': 'S'}},
        {'tool': 'trial_run'},
        {'tool': 'review_calculation'},
        [
            {'tool': 'verify', 'args': {'node': 'orders:P', 'expr': 'amount', 'basis': rule, 'scheme': 'Basis'}},
            *({'tool': 'verify', 'args': {'node': f'people:{person}', 'expr': formula,
                                          'basis': rule, 'scheme': 'S', 'helpers': helpers}} for person in ('A', 'B')),
        ],
        {'tool': 'finish', 'args': {'summary': 'Synthetic funded settlement'}},
    ]
    llm = FakeLLM(primary_steps[3:] if scope_mode in ('native', 'terminal') else primary_steps)
    llm.checker_required = scope_mode in ('native', 'terminal')
    llm.workflow_mode = 'staged'
    llm.checker_llm = checker
    files_before = case.fingerprint(ws.inv.files)
    snapshot = case.snapshot(ws)
    if scope_mode == 'invalid_saved':
        saved, = (r for r in snapshot['workflow']['basis_review_parts']['S'].values() if r['section'] == 'populations')
        # A stale cached filter excludes A's only row; matching source files alone are insufficient.
        saved['value']['populations']['rows:shares']['filter'] = {'rid': {'not': ['1']}}
    case.save_case(out, {'rounds': [{'n': 1, 'at': '2026-11-16', 'files': files_before,
        'source_inputs': [{'name': p.name, 'path': str(p), 'hash': files_before[p.name]} for p in ws.inv.files],
        'status': 'NEEDS_CONFIRMATION', 'pending': [qid]}], 'workspace': snapshot})
    report = run_agent(llm, ws.inv.files, out, answers=[{'id': qid, 'answer': answer, 'answered_by': '离线测试'}],
                       max_steps=0 if scope_mode == 'terminal' else 8, reflect=0)
    assert report.status == 'OK', (report.error, [(step.tool, step.result[:400]) for step in report.steps])
    first_calc = next(i for i, step in enumerate(report.steps)
                      if step.tool == 'calculate_basis' or step.args.get('tool') == 'calculate_basis')
    compiled = [step for step in report.steps[:first_calc] if step.args.get('tool') == 'compile_formulas']
    assert [(step.args['phase'], step.ok) for step in compiled] == [('basis', True), ('all', True)]
    if scope_mode == 'terminal':
        assert [st.args.get('tool') for st in report.steps if st.args.get('phase') == 'terminal'] == ['review_calculation', 'finish']
        assert sum(1 for v in report.ws.verified.values() if v.get('terminal_signature')) == 3
        assert not any(st.tool in ('trial_run', 'review_calculation', 'verify', 'finish') for st in report.steps)
    if scope_mode == 'native':
        before_first_main = report.steps[:next(i for i, step in enumerate(report.steps) if step.tool == 'review_calculation')]
        verify_step, = (step for step in before_first_main if step.tool == '（自动独立复核）')
        assert verify_step.ok and verify_step.args['scheme'] == 'S'
        assert len([step for step in before_first_main if step.args.get('tool') == 'trial_run' and step.ok]) == 2
    resumed = case.load_case(out)
    assert resumed['rounds'][-1]['answered'] == [qid]
    receipts = resumed['workspace']['workflow']['receipts']
    approval = receipts['approved:S']['payload']
    assert approval['origin'] == 'confirmed_funding' and approval['funding_basis_digest'] == funding_digest
    assert report.ws.reread['calc']['reused_funding'] == ['Basis']
    assert answer in str(checker.calls[0])
    if expected_parts:
        observed = []
        for part in checker.calls:
            if not part[0]['content'].startswith('【独立总额分段复核】'):
                continue
            contract = json.loads(part[1]['content'].rsplit('本段输入：', 1)[1])
            observed.append('combined' if 'previous_independent_populations' in contract else
                            'population' if 'population_requirements' in contract else
                            'reports' if 'independent_pool' in contract else 'pool')
        assert observed == expected_parts
        used = resumed['workspace']['workflow']['references']['S']['report'].get('reused_settlement_scopes', [])
        assert used == (sorted(populations['populations']) if scope_mode in ('reused', 'native', 'terminal') else [])
    files = report.outputs
    assert {'excel', 'engine_results', 'excel:S:R0', 'excel:Basis:R0'} <= files.keys()
    assert json.loads(files['validation'].read_text(encoding='utf-8'))['status'] == 'PASS'
    wb = openpyxl.load_workbook(files['excel'], read_only=True, data_only=True)
    try:
        rows = list(wb['汇总核算'].values)
        people = {(r[0], r[2]): Decimal(str(r[5])) for r in rows if r[3] == '人员'}
        assert people == {('S', 'A'): Decimal('0.00'), ('S', 'B'): Decimal('0.01')}
        row, = (row for row in rows if row[2] == 'funding')
        assert tuple(Decimal(str(n)) for n in row[3:7]) == (Decimal('.01'), Decimal('.01'), Decimal(0), Decimal(0))
        project, = (row for row in wb['明细原值'].values if row[:4] == ('Basis', 'R0', 'orders', 'orders:P'))
        assert Decimal(project[6]) == Decimal('.01')
    finally:
        wb.close()


@pytest.mark.parametrize('workbook', ['result_S.xlsx', 'result.xlsx'])
def test_swapped_person_amounts_block_publication_of_all_formal_results(tmp_path, monkeypatch, workbook):
    report = finished_report(tmp_path)
    original_save = openpyxl.Workbook.save

    def swap_saved_amounts(book, path):
        if Path(path).name == workbook:
            sheet = book['汇总核算']
            if workbook == 'result_S.xlsx':
                names, paid = 1, 4
            else:
                names, paid = 3, 8
            alice, bob = [row[paid - 1] for row in sheet.iter_rows() if row[names - 1].value in ('A', 'B')]
            assert alice.value != bob.value
            alice.value, bob.value = bob.value, alice.value
        return original_save(book, path)

    monkeypatch.setattr(openpyxl.Workbook, 'save', swap_saved_amounts)
    out = tmp_path / 'out'
    with pytest.raises((AssertionError, ToolError), match='汇总核算'):
        write_agent_outputs(report, out)
    assert not list(out.glob('result*.xlsx'))
    assert not (out / 'results.json').exists()
    assert not (out / 'engine_results.json').exists()


@pytest.mark.parametrize('workbook, sheet, cell', [
    ('result_S.xlsx', 'orders核算结果', 'E2'),
    ('result.xlsx', '明细原值', 'G2'),
])
def test_readback_blocks_changed_raw_detail_with_same_cent_amount(tmp_path, monkeypatch, workbook, sheet, cell):
    report = finished_report(tmp_path)
    original_save = openpyxl.Workbook.save

    def change_raw(book, path):
        if Path(path).name == workbook:
            assert book[sheet][cell].value == '0.01'
            book[sheet][cell] = '0.0101'
        return original_save(book, path)

    monkeypatch.setattr(openpyxl.Workbook, 'save', change_raw)
    out = tmp_path / 'out'
    with pytest.raises(AssertionError, match=f'回读失败.*{sheet}'):
        write_agent_outputs(report, out)
    assert not list(out.glob('result*.xlsx'))
    assert not (out / 'engine_results.json').exists()


def test_recomputation_sees_changes_to_the_raw_detail_before_publishing(tmp_path):
    report = finished_report(tmp_path)
    traces = report.results['S']['R0'].traces
    key = 'orders:P/明细核算/orders'
    traces[key] = replace(traces[key], value='0.0101')
    out = tmp_path / 'out'
    with pytest.raises(ToolError, match='明细原值'):
        write_agent_outputs(report, out)
    assert not list(out.glob('result*.xlsx'))
    assert not (out / 'engine_results.json').exists()


def test_finish_accepts_project_basis_and_final_recipients_with_all_validation(tmp_path):
    ws = funded_model(tmp_path)
    assert not ws._report_output_gaps()
    assert not ws.results['Basis']['R0'].people
    assert {p['name'] for p in ws.results['S']['R0'].people} == {'A', 'B'}
    verify_all(ws)
    assert ws.finish('Project amounts and recipient settlement verified', review=False) == '已完成'
    assert ws.validation_report()['status'] == 'PASS'


def test_basis_exemption_does_not_skip_independent_recipient_or_sample_validation(tmp_path):
    ws = funded_model(tmp_path)
    with pytest.raises(ToolError, match='独立验算还没全部通过'):
        ws.finish('Missing sample proof', review=False)
    ws = funded_model(tmp_path / 'missing_people')
    spec = json.loads(ws.llm.calc)
    del spec['schemes']['S']['people']
    ws.llm.calc = json.dumps(spec)
    with pytest.raises(ToolError, match='缺少人员汇总独立验算'):
        ws.finish('Missing recipient proof', review=False)


def test_single_basis_and_disconnected_extra_basis_still_need_people(tmp_path):
    ws = funded_model(tmp_path)
    ws.drafts.pop('S')
    ws.results.pop('S')
    assert any('Basis' in gap and 'report.people' in gap for gap in ws._report_output_gaps())
    ws = funded_model(tmp_path / 'other')
    ws.drafts['Disconnected'] = copy.deepcopy(ws.drafts['Basis'])
    ws.results['Disconnected'] = {'R0': copy.deepcopy(ws.results['Basis']['R0'])}
    assert any('Disconnected' in gap and 'report.people' in gap for gap in ws._report_output_gaps())


def test_description_or_unexecuted_reference_cannot_exempt_a_basis(tmp_path):
    ws = funded_model(tmp_path)
    ws.drafts['S']['variables']['project_funding']['expr'] = 'amount'
    ws.drafts['S']['variables']['project_funding']['desc'] = 'scheme_paid("Basis")'
    ws.trial_run()
    assert ('Basis', 'R0') in ws._deps('S')
    assert any('Basis' in gap and 'report.people' in gap for gap in ws._report_output_gaps())


def test_transitive_executed_basis_chain_still_requires_a_recipient_endpoint(tmp_path):
    ws = funded_model(tmp_path)
    ws.drafts['Bridge'] = copy.deepcopy(ws.drafts['Basis'])
    ws.drafts['Bridge']['pool']['expr'] = 'scheme_paid("Basis")'
    ws.drafts['S']['variables']['project_funding']['expr'] = 'scheme_paid("Bridge")'
    ws.trial_run()
    assert not ws._report_output_gaps()
    ws.drafts['S']['report']['people'] = None
    assert any('S' in gap and 'report.people' in gap for gap in ws._report_output_gaps())


def test_report_only_reference_is_not_a_recipient_funding_dependency(tmp_path):
    ws = funded_model(tmp_path)
    ws.drafts['S']['variables']['recipient_total']['expr'] = 'sum(amount / order_count, of: "shares")'
    ws.drafts['S']['report']['details'][0]['expr'] = 'project_funding'
    ws.trial_run()
    assert any('Basis/R0:orders:P' in trace.inputs for trace in ws.results['S']['R0'].traces.values())
    assert any('Basis' in gap and 'report.people' in gap for gap in ws._report_output_gaps())


def test_contract_pending_is_a_real_funding_destination_but_standalone_display_is_not(tmp_path):
    ws = funded_model(tmp_path)
    ws.drafts['S']['variables']['recipient_total']['expr'] = '0'
    ws.drafts['S']['report']['pending'] = [{'name': 'Waiting', 'level': 'global',
        'expr': 'sum(project_funding, of: "orders")', 'sources': [{'doc': 'data.xlsx', 'quote': 'amount'}]}]
    ws.drafts['S']['report']['distributions'][0]['pending'] = ['Waiting']
    ws.trial_run()
    assert not ws._report_output_gaps()
    ws.drafts['S']['report']['distributions'] = []
    ws.trial_run()
    assert any('Basis' in gap and 'report.people' in gap for gap in ws._report_output_gaps())


@pytest.mark.parametrize('missing', ['declaration', 'result'])
def test_exempt_basis_must_preserve_referenced_object_details(tmp_path, missing):
    ws = funded_model(tmp_path)
    if missing == 'declaration':
        ws.drafts['Basis']['report']['details'] = []
    else:
        ws.results['Basis']['R0'].details['orders'] = {}
    assert any('Basis' in gap and 'report.details' in gap for gap in ws._report_output_gaps())


def test_dependency_identity_preserves_separators_in_scheme_and_report_names(tmp_path):
    ws = funded_model(tmp_path)
    ws.drafts['Basis/R:1'] = ws.drafts.pop('Basis')
    ws.results.pop('Basis')
    ws.drafts['Basis/R:1']['report']['details'][0]['name'] = 'objects/:detail'
    ws.drafts['S']['variables']['project_funding']['expr'] = 'scheme_paid("Basis/R:1")'
    ws.trial_run()
    assert not ws._report_output_gaps()


@pytest.mark.parametrize('people', [[], [{'name': 'Waiting', 'pending': True, 'count': 0}]])
def test_report_structure_does_not_invent_a_nonempty_recipient_business_requirement(tmp_path, people):
    ws = funded_model(tmp_path)
    ws.results['S']['R0'].people = people
    assert not ws._report_output_gaps()


def test_fabricated_recipient_identity_still_fails_independent_validation(tmp_path):
    ws = funded_model(tmp_path)
    ws.results['S']['R0'].people[0]['name'] = 'Fabricated'
    with pytest.raises(ToolError, match='人员.*集合'):
        ws.finish('Wrong recipient identity', review=False)


def test_each_scheme_keeps_declared_and_calculated_totals(tmp_path):
    ws = funded_model(tmp_path)
    ws.drafts['Basis']['report']['totals'] = []
    assert any('Basis' in gap and 'report.totals' in gap for gap in ws._report_output_gaps())
    ws = funded_model(tmp_path / 'other')
    ws.results['S']['R0'].totals = []
    assert any('S' in gap and 'totals' in gap for gap in ws._report_output_gaps())
