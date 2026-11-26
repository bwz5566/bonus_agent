"""Review independently in bounded calls and persist only complete, blind evidence."""

import json

import pytest

from bonus_agent import case, reread
from bonus_agent.agent import ToolError, Workspace, run_agent
from bonus_agent.basis_review_parts import BasisReviewParts, compile_source_tier, source_catalog, source_tier_series
from bonus_agent.llm import FakeLLM, LLMError, RunBudgetError
from bonus_core.dsl.phases import basis_rules
from tests.test_staged_workflow import make_workspace, population_spec, prepared, save_staged_case


def review_workspace(tmp_path):
    def change_sales(workbook):
        workbook['projects']['B2'] = 654321.91

    ws = make_workspace(tmp_path, mutate_workbook=change_sales)
    ws.said = '财务确认：本批先按项目计算。'
    ws.notes['参考答案'] = 'PRIVATE_ANSWER_SENTINEL'
    ws.drafts['S']['pool']['expr'] = '((amount) * (0.1))'
    ws.drafts['S']['report']['details'][0]['desc'] = 'PRIVATE_REPORT_DESCRIPTION'
    prepared(ws)
    ws.calculate_basis('S')
    return ws


def responses():
    scopes = population_spec()
    return [
        {'populations': {key: scopes[key] for key in ('项目', 'rows:projects')}, 'unclear': []},
        {'pool': 'amount / 10', 'helpers': {}, 'tables': {}, 'unclear': []},
        {'details': {'项目奖': 'amount / 10'},
         'totals': {'项目奖合计': 'sum(amount, of: "projects") / 10'}, 'unclear': []},
    ]


def attach_checker(ws, declarations):
    checker = FakeLLM([json.dumps(item, ensure_ascii=False) for item in declarations], name='independent')
    checker.basis_review_split = True
    ws.llm.checker_llm = checker
    return checker


@pytest.mark.parametrize('selected,accepted', [('policy_net', True), ('amount', False)])
def test_independent_pool_reviewer_may_keep_or_connect_its_own_unused_measure(tmp_path, selected, accepted):
    ws = review_workspace(tmp_path)
    population, _, reports = responses()
    original = {'pool': 'amount', 'helpers': {'policy_net': {'scope': '项目', 'expr': 'amount / 10'}},
                'tables': {}, 'unclear': []}
    decision = {'pool': selected, 'sources': [{'doc': 'policy.md', 'quote': '每个项目按销售金额的10%计算奖金'}],
                'unclear': []}
    checker = attach_checker(ws, [population, original, decision, reports])
    result = ws.review_basis('S')
    assert result['status'] == ('PASS' if accepted else 'FAIL')
    assert len(checker.calls) == (4 if accepted else 3)
    evidence = checker.calls[2][-1]['content']
    assert 'unused_helpers' in evidence and 'policy_net' in evidence
    assert ws.drafts['S']['pool']['expr'] not in evidence and '"actual":' not in evidence
    assert 'PRIVATE_ANSWER_SENTINEL' not in evidence and 'PRIVATE_REPORT_DESCRIPTION' not in evidence
    assert ws._flow().current('review:S') is accepted and not ws._flow().current('approved:S')


def test_independent_measure_with_invented_policy_stays_blocked(tmp_path):
    ws = review_workspace(tmp_path)
    population, _, _ = responses()
    gross = {'pool': 'amount', 'helpers': {'after_policy': {'scope': '项目', 'expr': 'amount / 10'}},
             'tables': {}, 'unclear': []}
    attach_checker(ws, [population, gross,
                        {'pool': 'after_policy', 'sources': [{'doc': 'policy.md', 'quote': '虚构的口径'}]}])
    with pytest.raises(ToolError, match='逐字核对'):
        ws.review_basis('S')
    assert not ws._flow().current('review:S') and not ws._flow().current('approved:S')


@pytest.mark.parametrize('unresolved', [True, False])
def test_report_with_wrong_labels_can_rebuild_from_scope_evidence(tmp_path, unresolved):
    ws = review_workspace(tmp_path)
    population, pool, reports = responses()
    wrong = {**reports, 'details': {'销售额': 'amount'}, 'unclear': ['同一范围可能有多个预算记录。']}
    fixed = {'unclear': ['原文仍缺少两份预算的优先级']} if unresolved else reports
    checker = attach_checker(ws, [population, pool, wrong, fixed])
    if unresolved:
        with pytest.raises(ToolError, match='两份预算的优先级'):
            ws.review_basis('S')
    else:
        assert ws.review_basis('S')['status'] == 'PASS'
    assert len(checker.calls) == 4
    packet = checker.calls[3][-1]['content']
    assert 'engine_row_coverage' in packet and 'max_rows_per_object' in packet and '项目奖' in packet
    assert 'PRIVATE_REPORT_DESCRIPTION' not in packet and ws.drafts['S']['pool']['expr'] not in packet
    assert not ws._flow().current('approved:S')


def test_segmented_money_dispute_keeps_the_structured_source_question():
    question = {'text': '请确认适用名单', 'options': [],
                'sources': [{'doc': 'policy.md', 'quote': '有部分人员按确认名单分配'}]}
    row = {}
    parsed = BasisReviewParts._accept_response(
        json.dumps({'unclear': ['缺确认名单'], 'settlement_question': question}),
        row, 'pool', ['pool'], {}, ['pool'], lambda _: pytest.fail('an unresolved formula cannot pass'),
    )
    assert row['state'] == 'unresolved' and parsed['settlement_question'] == question
    with pytest.raises(ToolError, match='尚未解决的 unclear'):
        BasisReviewParts._accept_response(json.dumps({'unclear': [], 'settlement_question': question}),
                                         {}, 'pool', ['pool'], {}, ['pool'], lambda _: None)


def test_restore_retries_only_incomplete_report_without_production_values(tmp_path):
    ws = review_workspace(tmp_path)
    population, pool, reports = responses()
    bad_report = {**reports, 'totals': {}}
    checker = attach_checker(ws, [population, pool, bad_report])
    with pytest.raises(ToolError):
        ws.review_basis('S')
    assert len(checker.calls) == 3 and not ws._flow().current('review:S')

    clone = Workspace(FakeLLM([]), ws.inv)
    case.restore(clone, json.loads(json.dumps(case.snapshot(ws), ensure_ascii=False)))
    clone.llm.checker_required = True
    resumed_checker = attach_checker(clone, [reports])
    result = clone.review_basis('S')
    assert result['status'] == 'PASS' and clone._flow().current('review:S')
    assert not clone._flow().current('approved:S')
    assert clone.review_basis('S')['cached'] and len(resumed_checker.calls) == 1

    submitted = checker.calls + resumed_checker.calls
    total = ws.workflow['basis']['S']['pool']
    assert total == '65452.19'
    for request in submitted:
        text = '\n'.join(msg['content'] for msg in request)
        assert '((amount) * (0.1))' not in text
        assert 'PRIVATE_ANSWER_SENTINEL' not in text and 'PRIVATE_REPORT_DESCRIPTION' not in text
        assert total not in text


@pytest.mark.parametrize('other_reviewer', [False, True])
def test_resume_rechecks_saved_approved_basis_under_current_code(tmp_path, monkeypatch, other_reviewer):
    from bonus_agent import workflow

    ws = review_workspace(tmp_path)
    attach_checker(ws, responses())
    assert ws.review_basis('S')['status'] == 'PASS'
    digest = ws.workflow['receipts']['review:S']['payload']['basis_digest']
    out = tmp_path / 'case'
    save_staged_case(ws, out)
    case.archive(out, 1, {'case': out / case.CASE_FILE})
    case.confirm_totals(out, 'S', digest, '财务')
    legacy = case.load_case(out)
    legacy['workspace']['workflow'].pop('confirmed_basis')
    case.save_case(out, legacy)

    primary = FakeLLM([])
    primary.workflow_mode = 'staged'
    primary.checker_required = True
    checker = FakeLLM([], name='different reviewer' if other_reviewer else 'independent')
    checker.basis_review_split = True
    primary.checker_llm = checker
    old_profile = workflow._EXECUTION_PROFILE
    monkeypatch.setattr(workflow, '_EXECUTION_PROFILE', 'different-engine-version')
    run = run_agent(primary, ws.inv.files, out, max_steps=0)
    flow = run.ws._flow()
    assert flow.state['receipts']['basis:S']['payload']['digest'] == digest
    assert not primary.calls and not checker.calls and not run.results
    assert len(case.load_case(out)['total_confirmations']) == 1
    if other_reviewer:
        assert flow.stage() == (5, 'S')
        assert not flow.current('approved:S')
    else:
        assert flow.current('review:S') and flow.current('approved:S')
        assert flow.state['receipts']['approved:S']['payload']['by'] == '财务'
        review_step = next(step for step in run.steps if step.args.get('tool') == 'review_basis')
        assert json.loads(review_step.result)['revalidated_from_profile'] == old_profile


def test_restart_reuses_scope_finished_before_round_was_saved(tmp_path, monkeypatch):
    ws = review_workspace(tmp_path)
    out = tmp_path / 'case'
    save_staged_case(ws, out)
    population, pool, reports = responses()

    def agent_with(declarations):
        primary = FakeLLM([{'tool': 'review_basis', 'args': {'scheme': 'S'}}])
        primary.workflow_mode = 'staged'
        primary.checker_required = True
        primary.checker_llm = FakeLLM([json.dumps(item, ensure_ascii=False) for item in declarations],
                                      name='independent')
        primary.checker_llm.basis_review_split = True
        return primary

    first = agent_with([population])
    run_part = BasisReviewParts._part

    def interrupted(self, section, *args, **kwargs):
        if section == 'pool':
            raise KeyboardInterrupt('模拟已完成范围、未开始金额请求的进程中断')
        return run_part(self, section, *args, **kwargs)

    with monkeypatch.context() as interruption:
        interruption.setattr(BasisReviewParts, '_part', interrupted)
        with pytest.raises(KeyboardInterrupt):
            run_agent(first, ws.inv.files, out, max_steps=1)
    assert len(case.load_case(out)['rounds']) == 1 and len(first.checker_llm.calls) == 1

    next_process = agent_with([pool, reports])
    resumed = run_agent(next_process, ws.inv.files, out, max_steps=1)
    assert resumed.status == 'AWAITING_TOTAL_CONFIRMATION', [(s.tool, s.result) for s in resumed.steps]
    assert len(next_process.checker_llm.calls) == 2
    assert resumed.ws._flow().current('review:S') and not resumed.ws._flow().current('approved:S')


def test_scoped_review_replays_with_added_downstream_mapping(tmp_path, monkeypatch):
    from bonus_agent import workflow

    ws = review_workspace(tmp_path)
    ws.set_scheme_datasets('S', list(ws.datasets))
    prepared(ws)
    digest = ws.calculate_basis('S')['digest']
    checker = attach_checker(ws, responses())
    assert ws.review_basis('S')['status'] == 'PASS'
    ws._flow().approve('S', '财务', digest)

    ws.define_dataset('extra_recipient_scope', 'data.xlsx', 'people',
                      {'extra_id': {'column': 'rid', 'type': 'id'}}, key='extra_id')
    monkeypatch.setattr(workflow, '_EXECUTION_PROFILE', 'new-version-after-scope-change')
    prepared(ws)
    assert ws.calculate_basis('S')['digest'] == digest
    report = ws._flow().review_basis('S', cached_only=True)

    assert report['status'] == 'PASS' and report['confirmation_reused']
    assert ws._flow().current('approved:S')
    assert len(checker.calls) == 3


@pytest.mark.parametrize('usage_reported', [True, False])
def test_known_length_only_retries_interrupted_section_with_more_output(tmp_path, usage_reported):
    ws = review_workspace(tmp_path)
    population, pool, reports = responses()
    checker = attach_checker(ws, [population, pool, reports])
    chat, ceilings = checker.chat, []

    def observed(messages, **options):
        ceilings.append(options['max_tokens'])
        if len(ceilings) == 2:
            checker.last_call = {'finish_reason': 'length', 'response_received': True,
                                 'usage_reported': usage_reported, 'requested_output_tokens': 8192}
            raise LLMError('finish_reason=length')
        checker.last_call = {'finish_reason': 'stop', 'response_received': True}
        return chat(messages, **options)

    checker.chat = observed
    result = ws.review_basis('S')
    if usage_reported:
        assert result['status'] == 'PASS' and ceilings == [8192, 8192, 16384, 8192]
        assert ws._flow().current('review:S') and not ws._flow().current('approved:S')
    else:
        assert result['status'] == 'ERROR'
        with pytest.raises(ToolError, match='缺少已确认的用量'):
            ws.review_basis('S')
        assert ceilings == [8192, 8192] and not ws._flow().current('review:S')


def test_budget_stop_before_length_retry_preserves_the_interrupted_request(tmp_path, monkeypatch):
    ws = review_workspace(tmp_path)
    population, pool, reports = responses()
    checker = attach_checker(ws, [population])
    ask = reread._ask
    length = {'finish_reason': 'length', 'response_received': True,
              'usage_reported': True, 'requested_output_tokens': 8192}
    invoked = []

    def stop_before_retry(workspace, messages, **options):
        invoked.append(options.get('max_tokens', 8192))
        if len(invoked) == 1:
            return ask(workspace, messages, **options)
        if len(invoked) == 2:
            checker.last_call = length
            raise LLMError('finish_reason=length')
        checker.last_call = {'status': 'budget_blocked'}
        stop = RunBudgetError('新请求未发出')
        stop.request_not_sent = True
        raise stop

    with monkeypatch.context() as stopped:
        stopped.setattr(reread, '_ask', stop_before_retry)
        with pytest.raises(RunBudgetError, match='新请求未发出'):
            ws._flow().review_basis('S')
    assert invoked == [8192, 8192, 16384]
    (sig, part), = [(sig, row) for sig, row in ws.workflow['basis_review_parts']['S'].items() if row['section'] == 'pool']
    assert part['request'] == length and ws.workflow['attempts'][f'S:part:{sig}'] == 1

    clone = Workspace(FakeLLM([]), ws.inv)
    case.restore(clone, json.loads(json.dumps(case.snapshot(ws), ensure_ascii=False)))
    clone.llm.checker_required = ws.llm.checker_required
    resumed = attach_checker(clone, [pool, reports])
    assert clone.review_basis('S')['status'] == 'PASS' and len(resumed.calls) == 2


def test_malformed_scope_response_gets_one_precise_retry(tmp_path):
    ws = review_workspace(tmp_path)
    population, pool, reports = responses()
    bad = {'scope_results': population['populations'], 'unclear': []}
    checker = attach_checker(ws, [bad, population, pool, reports])

    assert ws.review_basis('S')['status'] == 'PASS'
    assert len(checker.calls) == 4
    feedback = checker.calls[1][-1]['content']
    assert '缺少字段 ["populations"]' in feedback
    assert '多出字段 ["scope_results"]' in feedback
    assert 'scope_results' not in str(checker.calls[2:])
    sig, record = next((sig, part) for sig, part in ws.workflow['basis_review_parts']['S'].items()
                       if part['section'] == 'populations')
    assert record['protocol_failures'][0]['received_fields'] == ['scope_results', 'unclear']
    assert ws.workflow['attempts'][f'S:part:{sig}'] == 2
    assert not ws._flow().current('approved:S')


def test_exact_scope_fields_at_top_level_can_be_checked_without_resampling(tmp_path):
    ws = review_workspace(tmp_path)
    population, pool, reports = responses()
    checker = attach_checker(ws, [{**population['populations'], 'unclear': []}, pool, reports])
    assert ws.review_basis('S')['status'] == 'PASS'
    assert len(checker.calls) == 3 and not ws._flow().current('approved:S')


def test_same_invalid_scope_request_keeps_attempts_after_upgrade(tmp_path, monkeypatch):
    from bonus_agent import workflow

    ws = review_workspace(tmp_path)
    bad = {'scope_results': {}, 'unclear': []}
    first = attach_checker(ws, [bad])
    guard = reread.guard_request

    def block_retry(*args, **kwargs):
        if first.calls:
            raise RunBudgetError('未发出第二次请求')
        return guard(*args, **kwargs)

    with monkeypatch.context() as deadline:
        deadline.setattr(reread, 'guard_request', block_retry)
        with pytest.raises((ToolError, RunBudgetError)):
            ws._flow().review_basis('S')
    assert len(first.calls) == 1
    saved = json.loads(json.dumps(case.snapshot(ws), ensure_ascii=False))
    clone = Workspace(FakeLLM([]), ws.inv)
    case.restore(clone, saved)
    clone.llm.checker_required = ws.llm.checker_required
    checker = attach_checker(clone, [bad])
    monkeypatch.setattr(workflow, '_EXECUTION_PROFILE', 'review-shape-upgrade')
    prepared(clone)
    clone.calculate_basis('S')
    with pytest.raises(ToolError, match='只允许并须完整填写本段字段'):
        clone.review_basis('S')
    assert len(checker.calls) == 1
    with pytest.raises(ToolError, match='已达两次尝试'):
        clone.review_basis('S')
    assert len(checker.calls) == 1


def test_partially_invalid_populations_resume_only_remaining_label(tmp_path, monkeypatch):
    from bonus_agent.llm import RunBudgetError
    ws = review_workspace(tmp_path)
    population, pool, reports = responses()
    rows = population['populations']['rows:projects']
    bad = {**population, 'populations': {**population['populations'], 'rows:projects': {**rows, 'period': 'PRIVATE_BAD_VALUE'}}}
    first = attach_checker(ws, [bad])
    guard = reread.guard_request
    def stop_after_first(*args, **kwargs):
        if first.calls:
            raise RunBudgetError('test deadline before repair')
        return guard(*args, **kwargs)
    with monkeypatch.context() as deadline:
        deadline.setattr(reread, 'guard_request', stop_after_first)
        with pytest.raises(RunBudgetError, match='test deadline'):
            ws._flow().review_basis('S')
    assert len(first.calls) == 1 and not ws._flow().current('review:S')
    clone = Workspace(FakeLLM([]), ws.inv)
    case.restore(clone, json.loads(json.dumps(case.snapshot(ws), ensure_ascii=False)))
    clone.llm.checker_required = True
    resumed = attach_checker(clone, [{'populations': {'rows:projects': rows}, 'unclear': []}, pool, reports])
    assert clone.review_basis('S')['status'] == 'PASS'
    request = resumed.calls[0]
    contract = json.loads(request[1]['content'].split('\n本段输入：')[-1])
    assert set(contract['output_template']) == set(contract['population_requirements']) == {'rows:projects'}
    assert 'period_field' in request[-1]['content'] and 'PRIVATE_BAD_VALUE' not in str(request)
    assert '项目' in clone.workflow['references']['S']['spec']['populations']


def test_saved_scope_reply_is_revalidated_after_filter_parser_upgrade(tmp_path, monkeypatch):
    from bonus_agent import basis_review_parts, workflow

    ws = review_workspace(tmp_path)
    population, pool, reports = responses()
    population['populations']['rows:projects']['filter'] = {'amount': {'not': [0]}}
    first = attach_checker(ws, [population])
    with monkeypatch.context() as previous:
        previous.setattr(basis_review_parts, 'check_populations', lambda *args, **kw: {
            'status': 'FAIL', 'contract_valid': False, 'checks': [],
            'contract_errors': [{'scope': 'rows:projects', 'code': 'filter_shape'}], 'issues': ['数字筛选未解析'],
        })
        with pytest.raises(ToolError, match='filter_shape'):
            ws.review_basis('S')
    assert len(first.calls) == 1

    clone = Workspace(FakeLLM([]), ws.inv)
    case.restore(clone, json.loads(json.dumps(case.snapshot(ws), ensure_ascii=False)))
    clone.llm.checker_required = True
    reviewer = attach_checker(clone, [pool, reports])
    monkeypatch.setattr(workflow, '_EXECUTION_PROFILE', 'review-filter-parser-upgrade')
    prepared(clone)
    clone.calculate_basis('S')

    assert clone.review_basis('S')['status'] == 'PASS'
    assert len(reviewer.calls) == 2
    domains = clone.workflow['basis_review_parts']['S']
    sig, record = next((h, v) for h, v in domains.items() if v.get('replayed_from') and v['section'] == 'populations')
    assert record['state'] == 'valid' and clone.workflow['attempts'][f'S:part:{sig}'] == 1
    assert not clone._flow().current('approved:S')


def test_report_cannot_override_independent_pool_or_exceed_retry_limit(tmp_path):
    ws = review_workspace(tmp_path)
    population, pool, reports = responses()
    bad = {**reports, 'pool': '0'}
    checker = attach_checker(ws, [population, pool, bad, bad])
    for _ in range(3):
        with pytest.raises(ToolError):
            ws.review_basis('S')
    assert len(checker.calls) == 4 and not ws._flow().current('review:S')
    assert not ws.results


def test_basis_review_evaluates_each_detail_on_the_same_object_independently(tmp_path):
    ws = review_workspace(tmp_path)
    report = ws.drafts['S']['report']
    report['details'].append({**report['details'][0], 'name': '项目原金额', 'expr': 'amount'})
    prepared(ws)
    ws.calculate_basis('S')
    population, pool, reports = responses()
    reports['details']['项目原金额'] = 'amount'
    checker = attach_checker(ws, [population, pool, reports])

    assert ws.review_basis('S')['status'] == 'PASS'
    assert len(checker.calls) == 3 and not ws._flow().current('approved:S')


def test_source_id_is_expanded_and_rechecked_without_exposing_ledger_annotations(tmp_path):
    ws = review_workspace(tmp_path)
    population, pool, reports = responses()
    literal = population['populations']['项目']['sources'][0]
    ws.clauses[0].point = 'PRIVATE_LEDGER_INTERPRETATION'
    ws.compile_formulas()
    ws.calculate_basis('S')
    ident = next(k for k, ref in source_catalog(ws).items() if ref == literal)
    population['populations']['项目']['sources'] = [{'source_id': ident}]
    checker = attach_checker(ws, [population, pool, reports])
    assert ws.review_basis('S')['status'] == 'PASS'
    assert ws.workflow['references']['S']['spec']['populations']['项目']['sources'] == [literal]
    text = checker.calls[0][1]['content']
    assert '允许的标准数据集名称：' + json.dumps(sorted(ws.datasets), ensure_ascii=False) in text
    assert ident in text and 'PRIVATE_LEDGER_INTERPRETATION' not in text
    assert any(ref['doc'] == '财务对话' and ref['quote'] == ws.said for ref in source_catalog(ws).values())
    options = json.loads(text.split('\n本段输入：')[-1])['source_options']['项目']
    assert any(row['dataset'] == 'projects' and row['key_columns'] == ['pid'] for row in options)
    assert all('filter' not in row for row in options)


def test_population_packet_keeps_table_headers_with_their_physical_source(tmp_path):
    ws = review_workspace(tmp_path)
    population, *_ = responses()
    catalog = source_catalog(ws)
    project_header = next(k for k, ref in catalog.items() if ref['quote'] == 'pid | amount')
    people_header = next(k for k, ref in catalog.items() if ref['quote'] == 'rid | pid | person | share')
    population['populations']['项目']['sources'].append({'source_id': people_header})

    checker = attach_checker(ws, [population])
    with pytest.raises(ToolError, match='scope_header_other_table'):
        ws.review_basis('S')
    text = checker.calls[0][1]['content']
    catalog_text, contract_text = text.split('\n业务原文目录 source_catalog：')[-1].split('\n本段输入：')
    assert not {project_header, people_header} & json.loads(catalog_text).keys()
    contract = json.loads(contract_text)
    options = contract['source_options']['项目']
    by_table = {option['dataset']: option for option in options}
    assert project_header in by_table['projects']['header_sources']
    assert people_header not in by_table['projects']['header_sources']
    assert people_header in by_table['people']['header_sources']
    record = next(iter(ws.workflow['basis_review_parts']['S'].values()))
    error, = record['contract_errors']
    assert error['source_index'] == 2
    assert error['declared_source'] == {'dataset': 'projects', 'file': 'data.xlsx', 'sheet': 'projects'}
    assert error['quoted_header_locations'] == [{'file': 'data.xlsx', 'sheet': 'people'}]
    assert set(record['accepted']) == {'rows:projects'}


@pytest.mark.parametrize(('source', 'code'), [
    (None, 'missing_scope_source'),
    ({'doc': 'policy.md', 'quote': '每个已批准的项目参与核算'}, 'scope_quote_not_found'),
    ({'doc': 'other.md', 'quote': '每个项目按销售金额的10%计算奖金'}, 'unknown_scope_source_document'),
    ({'doc': 'policy.md', 'quote': '每个项目按销售金额的10%计算奖金', 'page': 9}, 'scope_quote_wrong_page'),
    ({'doc': 'data.xlsx', 'quote': 'person'}, 'scope_header_other_table'),
    ({'doc': 'data.xlsx', 'quote': 'pid'}, 'scope_rule_needed'),
])
def test_scope_evidence_contract_distinguishes_failures(tmp_path, source, code):
    ws = review_workspace(tmp_path)
    population, _, _ = responses()
    population['populations']['项目']['sources'] = [source] if source else []
    attach_checker(ws, [population])
    with pytest.raises(ToolError, match=code):
        ws.review_basis('S')
    rec = next(iter(ws.workflow['basis_review_parts']['S'].values()))
    assert rec['contract_errors'][0]['code'] == code
    assert set(rec['accepted']) == {'rows:projects'}
    assert not ws._flow().current('review:S')


def test_catalog_id_cannot_override_its_quote(tmp_path):
    ws = review_workspace(tmp_path)
    population, _, _ = responses()
    ident = next(iter(source_catalog(ws)))
    population['populations']['项目']['sources'] = [{'source_id': ident, 'quote': 'invented'}]
    attach_checker(ws, [population])
    with pytest.raises(ToolError, match='invalid_source_catalog_id'):
        ws.review_basis('S')
    assert not ws._flow().current('review:S')


def test_valid_segmented_disagreement_stays_frozen(tmp_path):
    ws = review_workspace(tmp_path)
    population, pool, reports = responses()
    checker = attach_checker(ws, [population, {**pool, 'pool': 'amount / 5'}, reports])
    assert ws.review_basis('S')['status'] == 'FAIL'
    assert len(checker.calls) == 2
    # This text is intentionally excluded from independent evidence. It cannot
    # invalidate the reviewer response or buy a chance at a friendlier one.
    ws.drafts['S']['report']['details'][0]['desc'] = '只修改内部展示说明'
    prepared(ws)
    ws.calculate_basis('S')
    repeated = ws.review_basis('S')
    assert repeated['status'] == 'FAIL' and repeated['cached']
    assert len(checker.calls) == 2 and not ws._flow().current('review:S')


def test_independent_table_source_series_preserves_each_axis_and_boundary():
    raw = [{'file': '规则.pptx', 'page': 3, 'table': 1, 'caption': ['预算达成'],
            'rows': [[[], ['<25%(含)'], ['25%-50%(含)'], ['超过50%']],
                     [['地区'], ['0.8'], ['0.9'], ['1.0']],
                     [['客户'], ['0.5'], ['0.7'], ['1.05']]]},
           {'file': '规则.pptx', 'page': 4, 'table': 1, 'caption': ['超额累进'],
            'rows': [[['销售额'], ['新项目'], ['老项目']],
                     [['100万元以上'], ['3%'], ['2%']],
                     [['100万元以下'], ['2%'], ['1%']]]}]
    series = source_tier_series(raw)
    from bonus_core.dsl.models import TierTable

    customer = {'source_series': '规则.pptx::P3.T1.R3', 'mode': 'whole', 'purpose': 'coefficient'}
    record = compile_source_tier(customer, series)
    assert record['steps'] == [['0', '0.5'], ['0.25', '0.7'], ['0.50', '1.05']]
    assert record['step_inclusive'] == [True, False, False]
    assert TierTable.model_validate(record).below_first == 'first'
    with pytest.raises(ValueError, match='不得另填档位'):
        compile_source_tier({**customer, 'steps': [[0, 3]]}, series)
    old_projects = {'source_series': '规则.pptx::P4.T1.C3', 'mode': 'excess', 'purpose': 'amount', 'below_first': 'first'}
    record = compile_source_tier(old_projects, series)
    assert record['steps'] == [['0', '0.01'], ['1000000', '0.02']]
    assert all(source['page'] == 4 for source in record['sources'])


@pytest.mark.parametrize('keep', [True, False])
def test_executable_tier_draft_requires_independent_applicability_findings(tmp_path, monkeypatch, keep):
    ws = review_workspace(tmp_path)
    source = [{'file': 'policy.md', 'page': 1, 'table': 1, 'caption': ['预算达成'],
               'rows': [[['金额'], ['本期']], [['100以上'], ['10%']], [['100以下'], ['10%']]]}]
    population, _, reports = responses()
    draft = {'pool': 'tier(rate, amount)', 'helpers': {},
             'tables': {'rate': {'source_series': 'policy.md::P1.T1.C2', 'mode': 'excess', 'purpose': 'amount',
                                 'below_first': 'first'}}, 'unclear': ['分档点归属是否影响本批？']}
    observations = BasisReviewParts._formula_source_observations.func
    monkeypatch.setattr(BasisReviewParts, '_formula_source_observations', property(lambda self: {
        **observations(self), 'verified_source_tier_series': source_tier_series(source)}))
    decision = ({'unclear': ['仍需书面的边界口径']} if keep else
                {'resolutions': {'1': {'source_ids': ['S1'], 'reason': '已核实本批没有命中分档端点'}}, 'unclear': []})
    checker = attach_checker(ws, [population, draft, decision, reports])
    if keep:
        with pytest.raises(ToolError, match='仍需书面的边界口径'):
            ws.review_basis('S')
        assert [q.text for q in ws.pending] == ['仍需书面的边界口径']
        assert not ws.pending[0].answer
    else:
        assert ws.review_basis('S')['status'] == 'PASS'
    packet = checker.calls[2][-1]['content']
    assert 'boundary_hits' in packet and 'sign_counts' in packet and 'PRIVATE_REPORT_DESCRIPTION' not in packet
    assert ws.workflow['basis']['S']['pool'] not in packet
    assert not ws._flow().current('approved:S')


@pytest.mark.parametrize('problem', ['syntax', 'name_clash', 'saved_amendment', 'partial_amendments'])
def test_invalid_helper_in_saved_pool_draft_reaches_local_amendment(tmp_path, monkeypatch, problem):
    ws = review_workspace(tmp_path)
    raw = [{'file': '规则.pptx', 'page': 1, 'table': 1, 'caption': ['预算达成'],
            'rows': [[[], ['≤25%'], ['超过25%']], [['地区'], ['0.8'], ['1.0']]]}]
    observations = {
        'verified_source_tier_series': source_tier_series(raw), 'rule_source_excerpts': [],
        'source_period_coverage': {}, 'engine_scope_observations': {}, 'applied_field_empty_policy': {},
    }
    monkeypatch.setattr(BasisReviewParts, '_formula_source_observations', property(lambda _: observations))
    population, pool, reports = responses()
    draft_helper = {'project_bonus': {'scope': '项目', 'expr': '(amount / 10)'}}
    amendment = {'helpers': {'project_bonus': {'scope': '项目', 'expr': '(amount / 10)'}}, 'unclear': []}
    if problem == 'syntax':
        draft_helper['project_bonus']['expr'] = '(amount / 10'
    else:
        draft_helper['amount'] = {'scope': '项目', 'expr': 'amount'}
        amendment['helpers']['amount'] = None
    if problem == 'saved_amendment':
        amendment['helpers']['project_bonus']['expr'] = 'per_item'
        amendment['helpers']['per_item'] = {'scope': '项目', 'expr': 'amount / 10'}
        amendment['unclear'] = ['待核对分组字段出处']
    draft = {**pool, 'pool': 'project_bonus', 'helpers': draft_helper,
             'tables': {'budget_coef': {'source_series': '规则.pptx::P1.T1.R2',
                                        'mode': 'whole', 'purpose': 'coefficient'}},
             'unclear': ['25% 的达成率档位待核对。']}
    confirmations = [{'confirmed': True, 'unclear': []}] if problem == 'saved_amendment' else []
    next_edits = []
    if problem == 'partial_amendments':
        amendment['helpers']['project_bonus']['expr'] = 'per_item'
        next_edits.append({'helpers': {'per_item': {'scope': '项目', 'expr': 'amount / 10'}}, 'unclear': []})
    checker = attach_checker(ws, [population, draft, amendment, *next_edits, *confirmations, reports])

    if next_edits:
        with pytest.raises(ToolError):
            ws.review_basis('S')
    assert ws.review_basis('S')['status'] == 'PASS'
    assert len(checker.calls) == 4 + len(confirmations) + len(next_edits)
    packet = checker.calls[2][1]['content'].rsplit('\n本段输入：', 1)[1]
    correction = json.loads(packet)
    target = 'variables.project_bonus' if problem == 'syntax' else 'variables.amount'
    code = 'syntax' if problem == 'syntax' else 'name_clash'
    assert any(d['code'] == code and d['target'] == target for d in correction['engine_static_issues'])
    assert correction['removable_name_clashes'] == ([] if problem == 'syntax' else ['amount'])
    assert correction['source_verified_bands']['budget_coef']['step_inclusive'] == [True, False]
    assert correction['field_contract']['projects']['fields']['pid'] == 'id'
    assert correction['field_contract']['projects']['source_columns']['pid'] == 'pid'
    assert correction['direct_fields_by_scope']['项目']['pid'] == 2
    assert correction['observed_direct_values_by_scope']['项目']['pid'] == {'A': 1, 'B': 1}
    if confirmations:
        evidence = json.loads(checker.calls[3][1]['content'].split('\n本段输入：')[-1])
        assert evidence['verified_populations'] == population['populations']
        assert evidence['independent_declaration']['helpers']['per_item']['expr'] == 'amount / 10'
        assert ws.workflow['basis']['S']['pool'] not in json.dumps(evidence, ensure_ascii=False)
    assert ws.workflow['basis']['S']['pool'] not in packet
    assert not ws._flow().current('approved:S')


@pytest.mark.parametrize(('source', 'expected'), [
    ('if(region == "A(北)", max(1, 2), if(active, 3, 4)',
     'if(region == "A(北)", max(1, 2), if(active, 3, 4))'),
    ('max(a, b + c', None),
    ('unknown_function(amount', None),
    ('(amount + 1)', None),
])
def test_syntax_repair_requires_one_unambiguous_valid_formula(source, expected):
    from bonus_core.dsl.parser import repair_unclosed_parenthesis

    assert repair_unclosed_parenthesis(source) == expected


@pytest.mark.parametrize('recovered', [True, False])
def test_formula_protocol_failure_retries_inside_the_independent_stage(tmp_path, recovered):
    ws = review_workspace(tmp_path)
    population, pool, reports = responses()
    malformed = '公式改写说明，未附结构化声明'
    replies = [pool, reports] if recovered else [malformed]
    checker = attach_checker(ws, [population, malformed, *replies])
    result = ws.review_basis('S')
    if recovered:
        assert result['status'] == 'PASS' and len(checker.calls) == 4
    else:
        assert result['status'] == 'ERROR' and len(checker.calls) == 3 and ws.done
    assert not ws.llm.calls and not ws._flow().current('approved:S')


def test_failed_formula_keeps_independent_draft_and_private_engine_feedback_separate(tmp_path):
    ws = review_workspace(tmp_path)
    population, pool, reports = responses()
    bad_pool = {**pool, 'pool': 'amount / 0'}
    checker = attach_checker(ws, [population, bad_pool])
    with pytest.raises(ToolError, match='求值校验'):
        ws.review_basis('S')

    row = next(row for row in ws.workflow['basis_review_parts']['S'].values() if row['section'] == 'pool')
    failure, = row['formula_failures']
    assert failure['declaration'] == bad_pool
    private = failure['local_error']
    assert private and private != failure['diagnostic']

    clone = Workspace(FakeLLM([]), ws.inv)
    case.restore(clone, json.loads(json.dumps(case.snapshot(ws), ensure_ascii=False)))
    clone.llm.checker_required = True
    resumed = attach_checker(clone, [pool, reports])
    assert clone.review_basis('S')['status'] == 'PASS'
    assert len(checker.calls) == 2 and len(resumed.calls) == 2
    feedback = resumed.calls[0][-1]['content']
    assert bad_pool['pool'] in feedback
    assert private not in feedback
    assert not clone._flow().current('approved:S')


def test_independent_reviewer_receives_field_and_helper_for_missing_row_context(tmp_path):
    ws = review_workspace(tmp_path)
    ws.define_dataset('lines', 'data.xlsx', 'projects', {
        'pid': {'column': 'pid', 'type': 'id'}, 'line_amount': {'column': 'amount', 'type': 'money'},
    }, link='pid')
    ws.drafts['S']['pool']['expr'] = 'sum(line_amount, of: "lines") * 0.1'
    prepared(ws)
    ws.calculate_basis('S')
    population, pool, reports = responses()
    population['populations']['rows:lines'] = {**population['populations']['rows:projects'], 'dataset': 'lines'}
    wrong = {**pool, 'pool': 'base_try', 'helpers': {'base_try': {'scope': '项目', 'expr': 'line_amount'}}}
    reviewer = attach_checker(ws, [population, wrong, pool, reports])
    assert ws.review_basis('S')['status'] == 'PASS'
    feedback = reviewer.calls[2][-1]['content']
    assert 'detail_field_requires_aggregation' in feedback and 'base_try' in feedback and 'lines' in feedback
    assert '项目:A' not in feedback and '项目:B' not in feedback
    assert not ws._flow().current('approved:S') and not ws.llm.calls


@pytest.mark.parametrize('decision', ['keep', 'rebuild', 'changed_materials', 'wrong_amount'])
def test_finance_reply_requires_fresh_independent_opinion_on_archived_pool(tmp_path, decision):
    from bonus_agent.extract_rules import Question

    ws = review_workspace(tmp_path)
    question = Question(id='q2', text='本批是否沿用制度表？', answer='按所附制度表',
                        answered_by='财务', related='S / 独立金额适用性复核')
    ws.questions.append(question)
    ws.workflow['applicability_questions'] = {'q2': {'scheme': 'S', 'question': ws._flow()._question_identity(question),
                                                      'files': dict(ws._source_version), 'period': str(ws.period)}}
    ws._review_archive_unchanged = decision != 'changed_materials'
    population, pool, reports = responses()
    original = {**pool, 'pool': 'amount / 5' if decision in ('rebuild', 'wrong_amount') else pool['pool'],
                'unclear': ['待核对所用制度表']}
    request = {'model': 'independent', 'finish_reason': 'stop', 'response_received': True,
               'usage_reported': True, 'response_id': 'saved-response'}
    ws.workflow.setdefault('basis_review_parts', {}).setdefault('S', {})['previous'] = {
        'section': 'pool', 'state': 'unresolved', 'request': request, 'independent_draft': original}
    resolutions = {'1': {'source_ids': ['q2'], 'reason': '财务核实制度适用'},
                   'q2': {'source_ids': ['q2'], 'reason': '核对了本批答复'}}
    replies = ([{'decision': 'rebuild', 'reason': '原公式的比例不符合本批制度', 'unclear': []}, pool]
               if decision == 'rebuild' else [pool] if decision == 'changed_materials' else
               [{'decision': 'keep', 'resolutions': resolutions, 'unclear': []}])
    checker = attach_checker(ws, [population, *replies, reports])
    checker.model = 'independent'
    prepared(ws)
    ws.calculate_basis('S')
    result = ws.review_basis('S')
    assert result['status'] == ('FAIL' if decision == 'wrong_amount' else 'PASS')
    offer = checker.calls[1][0]['content']
    assert ('整份草稿仍适用' in offer) == (decision != 'changed_materials')
    assert not ws._flow().current('approved:S')


@pytest.mark.parametrize('claim,valid', [('unused', True), ('clear_boundary', True), ('active', False), ('ended', False), ('wrong_metric', False),
                                        ('duplicate', False), ('unverified', False), ('conflict', False)])
def test_current_batch_effect_uses_actual_calls_and_expires_on_period_change(tmp_path, monkeypatch, claim, valid):
    from bonus_agent.extract_rules import Question
    from bonus_core.data.period import Period

    ws = review_workspace(tmp_path)
    table = 'settled_positive' if claim in ('active', 'ended', 'wrong_metric') else 'unseen_negative'
    issue = '实际 boundary_hits 需要检查' if claim in ('ended', 'clear_boundary') else f'{table}.calls 的规则仍待明确'
    q = Question(id='q4', text=issue, related='S / 独立金额适用性复核')
    ws.questions.append(q)
    flow = ws._flow()
    ws.workflow['applicability_questions'] = {q.id: {'scheme': 'S', 'files': dict(ws._source_version),
                                                    'period': str(ws.period), 'question': flow._question_identity(q)}}
    ws.workflow['references'] = {'S': {'spec_state': 'unresolved', 'spec': {'unclear': [issue]}}}
    parts = BasisReviewParts(flow, 'S', basis_rules(ws._ruleset('S')), '', {'base': {}, 'reports': []}, 'current')
    parts._pool_record = {}
    parts.records['previous'] = {'section': 'pool_finance_recheck', 'state': 'unresolved', 'value': {'unclear': [issue]}}
    objection = 'q2 已指定采用的制度，是否需再次确认？'
    if claim == 'unused':
        ws.workflow['references']['S']['spec']['unclear'] = [objection]
        parts.records['previous_effect'] = {'section': 'pool_batch_effects', 'state': 'unresolved',
                                            'value': {'unclear': [objection]}}
    threshold = '201' if claim in ('wrong_metric', 'clear_boundary') else '200'
    raw = [{'file': 'policy.md', 'page': 1, 'table': 1, 'caption': ['本期'],
            'rows': [[['金额'], ['本期']], [[threshold + '以上'], ['10%']], [[threshold + '以下'], ['10%']]]}]
    source_table = {'source_series': 'policy.md::P1.T1.C2', 'mode': 'excess', 'purpose': 'amount', 'below_first': 'first'}
    monkeypatch.setattr(BasisReviewParts, '_formula_source_observations', property(lambda _: {
        'rule_source_excerpts': [{'doc': 'policy.md', 'quote': '每个项目按销售金额的10%计算奖金'}],
        'pool_source_excerpts': [], 'verified_source_tier_series': source_tier_series(raw)}))
    draft = {'pool': 'if(amount >= 0, tier(settled_positive, amount), tier(unseen_negative, amount))', 'helpers': {},
             'tables': {n: dict(source_table) for n in ('settled_positive', 'unseen_negative')}, 'unclear': [issue]}
    def review(section, targets, instruction, contract, check, **kwargs):
        assert claim not in ('active', 'ended', 'wrong_metric'), '触发条件已由引擎证实，不能请求放行'
        assert section == 'pool_batch_effects' and targets == ['q4']
        assert contract['open_issues'] == {'q4': issue} and set(contract['finance_answers']) == {'q2'}
        witness = contract['observed_effects']['q4']
        if claim == 'clear_boundary':
            assert witness['metric'] == 'boundary_hits' and set(witness['tables']) == set(draft['tables'])
            assert all(not row['boundary_hits'] for row in witness['observations'].values())
        else:
            assert witness['metric'] == 'calls' and witness['observations'][table]['calls'] == 0
        assert witness['observations'][table]['source_series'] == 'policy.md::P1.T1.C2'
        if claim == 'unused':
            assert contract['previous_objections'] == [objection]
        if claim in ('duplicate', 'unverified', 'conflict'):
            blocker = {'subject': 'q2', 'kind': 'active_condition' if claim == 'duplicate' else 'formula_mismatch',
                       'evidence_id': 'independent_pool', 'quote': 'invented' if claim == 'unverified' else draft['pool'],
                       'impact': '本批实际使用该表达式，须核对所选制度与现有公式'}
            answer = {'unclear': ['草稿与指定制度的公式有冲突'], 'blockers': [blocker]}
            record = {}
            return BasisReviewParts._accept_response(json.dumps(answer), record, section, targets, {}, targets, check)
        answer = {'decision': 'keep', 'reason': '已按财务选择核对公式；疑义所指表没有被调用', 'unclear': []}
        check(answer)
        return answer
    monkeypatch.setattr(parts, '_part', review)
    finance = {'q2': {'doc': '本批财务答复 q2', 'question': '本批适用哪份制度？', 'quote': '使用本批制度'}}
    if valid:
        result = parts._confirm_pool_applicability(draft, draft, lambda _: None, {'项目': {}}, finance=finance)
        assert not result['unclear'] and q not in ws.pending and q.answer is None
        assert ws.workflow['batch_question_effects']['q4']['evidence']['observations'][table]['calls'] == 0
        ws.period = Period.parse('2026-10')
    elif claim in ('active', 'ended', 'wrong_metric'):
        result = parts._confirm_pool_applicability(draft, draft, lambda _: None, {'项目': {}}, finance=finance)
        assert result['unclear'] == [issue] and '实际触发' in q.diagnosis['本批影响']
    elif claim == 'conflict':
        result = parts._confirm_pool_applicability(draft, draft, lambda _: None, {'项目': {}}, finance=finance)
        assert result['unclear'] and result['blockers'][0]['subject'] == 'q2'
    else:
        with pytest.raises((ValueError, ToolError)):
            parts._confirm_pool_applicability(draft, draft, lambda _: None, {'项目': {}}, finance=finance)
    assert q in ws.pending and q.answer is None and not flow.current('approved:S')


@pytest.mark.parametrize('values,expected', [
    (('1.011', '2.011', '3.023'), (False, True, True)),
    (('0.005', '0.005', '0.010'), (True, True, False)),
    (('-0.005', '-0.005', '-0.010'), (True, True, False)),
    (('1', '2', '4'), (False, False, False)),
])
def test_independent_report_reconciliation_distinguishes_raw_and_displayed(values, expected):
    from decimal import Decimal
    from bonus_agent.basis_review_parts import _report_sum_observations

    values = dict(zip(('A', 'B', 'total'), map(Decimal, values)))
    result = next(row for row in _report_sum_observations(values) if row['target'] == 'total')
    assert result['other_items'] == ['A', 'B']
    assert (result['raw_equal'], result['rounded_sum_equal'], result['displayed_sum_equal']) == expected
    assert _report_sum_observations({'A': values['A']}) == []


def test_unclear_population_blocks_future_slices_and_repeat_queries(tmp_path):
    ws = review_workspace(tmp_path)
    checker = attach_checker(ws, [{'unclear': ['原文未指定销售退货的业务截止期。']}])
    for _ in range(2):
        with pytest.raises(ToolError, match='截止期'):
            ws.review_basis('S')
    assert len(checker.calls) == 1 and not ws._flow().current('review:S')
    assert not ws.questions and not ws.results


def test_switching_from_monolithic_to_split_cannot_discard_independent_disagreement(tmp_path):
    ws = review_workspace(tmp_path)
    population, pool, reports = responses()
    different = {**population, **pool, **reports, 'pool': 'amount / 5'}
    checker = attach_checker(ws, [different])
    checker.basis_review_split = False
    assert ws.review_basis('S')['status'] == 'FAIL'
    checker.basis_review_split = True
    review = ws.review_basis('S')
    assert review['status'] == 'FAIL' and review['cached']
    assert len(checker.calls) == 1 and not ws._flow().current('review:S')


@pytest.mark.parametrize('accepted', [False, True])
@pytest.mark.parametrize('two_filters', [False, True])
def test_same_source_row_scope_requires_the_independent_reviewers_decision(tmp_path, monkeypatch, accepted, two_filters):
    rule = '本表只核算 A 项目。'
    ws = make_workspace(tmp_path, policy_tail=rule)
    ws.mapping['projects'].filter = {'pid': ['A']}
    ws.mapping['people'].filter = {'pid': ['A']}
    ws.define_dataset('sales_peer', 'data.xlsx', 'projects',
                      {'peer_pid': {'column': 'pid', 'type': 'id'}}, key='peer_pid')
    labels = ['rows:projects']
    original = population_spec()['rows:projects']
    peer = {**original, 'dataset': 'sales_peer', 'filter': {'pid': ['A']},
            'sources': [{'doc': 'policy.md', 'quote': rule}]}
    verified = {'rows:sales_peer': peer}
    if two_filters:
        ws.define_dataset('second_source', 'data.xlsx', 'projects',
                          {'source_id': {'column': 'pid', 'type': 'id'}}, key='source_id')
        verified['rows:second_source'] = {**peer, 'dataset': 'second_source', 'filter': {'pid': {'not': ['B']}}}
    part = {'populations': {'rows:projects': original}, 'unclear': []}
    parts = BasisReviewParts(ws._flow(), 'S', basis_rules(ws._ruleset('S')), '', {'base': {}}, 'probe')
    parts._validate_populations(part, labels)
    assert parts.population_report['status'] == 'FAIL'

    def review(section, targets, instruction, contract, validate):
        assert section == 'population_scope_choice' and targets == labels
        option = contract['choices']['rows:projects']
        if two_filters:
            assert {p['peer_scope'] for p in option['alternatives']} == set(verified)
        else:
            assert option['peer_declaration']['sources'] == peer['sources']
        if not accepted:
            return {'unclear': ['本指标的资格仍需核对']}
        with pytest.raises(ValueError):
            validate({'use_peer': {'rows:projects': '未被独立核验的范围'}, 'unclear': []})
        decision = {'use_peer': {'rows:projects': 'rows:second_source' if two_filters else 'rows:sales_peer'}, 'unclear': []}
        validate(decision)
        return decision

    monkeypatch.setattr(parts, '_part', review)
    checked = parts._clarify_shared_rows(part, labels, verified)
    assert parts.population_report['status'] == ('PASS' if accepted else 'FAIL')
    chosen = verified['rows:second_source' if two_filters else 'rows:sales_peer']
    assert checked['populations']['rows:projects']['filter'] == (chosen['filter'] if accepted else {})
    assert original['filter'] == {} and ws.mapping['projects'].filter == {'pid': ['A']}


@pytest.mark.parametrize('supported', [False, True])
def test_first_scope_can_reconsider_a_prior_independent_filter_only_with_current_evidence(tmp_path, supported):
    rule = '本表只核算 A 项目。'
    ws = make_workspace(tmp_path, policy_tail=rule)
    ws.mapping['projects'].filter = ws.mapping['people'].filter = {'pid': ['A']}
    label = 'rows:projects'
    current = population_spec()[label]
    prior = {**current, 'filter': {'pid': ['A']},
             'sources': [{'doc': 'policy.md', 'quote': rule if supported else '不存在的历史规则'}]}
    parts = BasisReviewParts(ws._flow(), 'S', basis_rules(ws._ruleset('S')), '', {'base': {}}, 'current')
    parts.records['previous'] = {'section': 'populations', 'state': 'valid', 'value': {'populations': {label: prior}}}
    parts._validate_populations({'populations': {label: current}}, [label])
    choices = parts._shared_row_options({label: current}, parts.population_report, {})
    assert bool(choices) == supported and current['filter'] == {}
    if supported:
        option, = choices[label].values()
        assert option['candidate_declaration']['filter'] == {'pid': ['A']} and option['witness_rows']
    assert parts.population_report['status'] == 'FAIL' and not ws._flow().current('review:S')


@pytest.mark.parametrize('other_invalid', [False, True])
def test_population_counterexample_stops_before_paid_formula_slices(tmp_path, other_invalid):
    ws = review_workspace(tmp_path)
    population, _, _ = responses()
    population['populations']['项目']['filter'] = {'pid': ['A']}
    if other_invalid:
        population['populations']['rows:projects']['window'] = 'invalid'
    checker = attach_checker(ws, [population])
    for _ in range(2):
        result = ws.review_basis('S')
        assert result['status'] == 'FAIL'
        rows = result['population']['checks']
        assert next(r['extra'] for r in rows if r['scope'] == '项目') == ['B']
    if other_invalid:
        saved = next(iter(ws.workflow['basis_review_parts']['S'].values()))
        assert saved['accepted'] == {'项目': population['populations']['项目']}
    assert len(checker.calls) == 1 and not ws._flow().current('review:S')


def blank_reference_review(tmp_path, policy_tail=''):
    def add_reference(workbook):
        table = workbook.create_sheet('ratings')
        table.append(['pid', 'metric'])
        table.append(['A', None])
        table.append(['B', 0])

    ws = make_workspace(tmp_path, add_reference, policy_tail=policy_tail)
    ws.said = '财务确认：metric 单元格为空视为缺失，取数时排除空白。数值 0 正常取用。'
    ws.define_dataset('ratings', 'data.xlsx', 'ratings', {
        'pid': {'column': 'pid', 'type': 'id'},
        'metric': {'column': 'metric', 'type': 'ratio', 'if_empty': 'skip'},
    }, link='pid')
    ws.drafts['S']['pool']['expr'] = 'amount * 0.1 + count(of: "ratings") * 0'
    prepared(ws)
    ws.calculate_basis('S')
    population, pool, reports = responses()
    before = {'dataset': 'ratings', 'filter': {}, 'period': None, 'window': 'period',
              'sources': [{'doc': '财务对话', 'quote': ws.said}]}
    population['populations']['rows:ratings'] = before
    resolved = {'populations': {'rows:ratings': {**before, 'filter': {'metric': {'not': ['']}}}}, 'unclear': []}
    return ws, population, resolved, pool, reports


@pytest.mark.parametrize(('malformed', 'filter_only'), [
    ('该空单元格没有提供有效指标，应按原文排除。', False),
    ({'decision': '按空白排除'}, True),
])
def test_malformed_blank_scope_retries_with_verified_sources(tmp_path, malformed, filter_only):
    ws, population, resolved, pool, reports = blank_reference_review(tmp_path)
    label = 'rows:ratings'
    invalid = {'populations': {label: malformed}, 'unclear': []}
    fields = {'filter': {'metric': {'not': ['']}}} if filter_only else {'exclude_blank_columns': ['metric']}
    amendment = {'populations': {label: {**fields, 'sources': resolved['populations'][label]['sources']}},
                 'unclear': []}
    checker = attach_checker(ws, [population, invalid, amendment, pool, reports])

    assert ws.review_basis('S')['status'] == 'PASS'
    assert len(checker.calls) == 5
    assert all(token in checker.calls[2][-1]['content'] for token in ('exclude_blank_columns', 'sources', label))
    declaration = ws.workflow['references']['S']['spec']['populations'][label]
    assert declaration == resolved['populations'][label]
    assert not ws._flow().current('approved:S')


def test_two_malformed_blank_scope_responses_stop_without_a_review_receipt(tmp_path):
    ws, population, _, _, _ = blank_reference_review(tmp_path)
    invalid = {'populations': {'rows:ratings': {'decision': '排除空白'}}, 'unclear': []}
    checker = attach_checker(ws, [population, invalid, invalid])
    result = ws.review_basis('S')
    assert result['status'] == 'ERROR' and '两次' in result['error'] and ws.done
    assert len(checker.calls) == 3 and not ws.llm.calls
    assert not ws._flow().current('review:S') and not ws._flow().current('approved:S')


def test_blank_scope_normalizes_an_unambiguous_raw_dataset_name(tmp_path):
    ws, population, resolved, pool, reports = blank_reference_review(tmp_path)
    amended = {'populations': {'ratings': {'exclude_blank_columns': ['metric'],
                                          'sources': resolved['populations']['rows:ratings']['sources']}}}
    checker = attach_checker(ws, [population, amended, pool, reports])
    assert ws.review_basis('S')['status'] == 'PASS'
    assert len(checker.calls) == 4
    assert ws.workflow['references']['S']['spec']['populations']['rows:ratings'] == resolved['populations']['rows:ratings']


def test_unresolved_blank_scope_is_answerable_in_the_saved_case(tmp_path, monkeypatch):
    from bonus_agent import workflow

    ws, population, _, _, _ = blank_reference_review(tmp_path)
    ws.said = '财务确认：metric 数值 0 按原值参与核算。'
    population['populations']['rows:ratings']['sources'] = [{'doc': '财务对话', 'quote': ws.said}]
    reviewer = attach_checker(ws, [population, {'unclear': ['ratings 第 2 行的 metric 为空，原文未明确取数口径。']}])
    prepared(ws)
    ws.calculate_basis('S')
    with pytest.raises(ToolError, match='第 2 行'):
        ws.review_basis('S')
    question, = ws.pending
    assert 'ratings' in question.text and '第 2 行' in question.text and 'metric' in question.text
    assert question.answer is None and question.blocking
    out = tmp_path / 'out'
    save_staged_case(ws, out)

    primary = FakeLLM(lambda *args: pytest.fail('no new model request before source confirmation'))
    primary.workflow_mode = 'staged'
    primary.checker_required = True
    primary.checker_llm = reviewer
    result = run_agent(primary, ws.inv.files, out, max_steps=1)
    assert result.status == 'NEEDS_CONFIRMATION' and {r['id'] for r in result.pending} == {question.id}
    assert len(reviewer.calls) == 2 and not result.results
    with monkeypatch.context() as upgrade:
        upgrade.setattr(workflow, '_EXECUTION_PROFILE', 'newer-code-same-source')
        upgrade.setattr(primary, 'context_window', lambda: pytest.fail('no discovery while the same question is pending'))
        same_wait = run_agent(primary, ws.inv.files, out, max_steps=1)
        assert same_wait.status == 'NEEDS_CONFIRMATION'
        assert [r['id'] for r in same_wait.pending] == [question.id]

    following = FakeLLM([{'tool': 'note', 'args': {'topic': '续跑答复', 'text': '已进入下一步核对。'}}])
    following.workflow_mode = 'staged'
    following.checker_required = True
    following.checker_llm = reviewer
    resumed = run_agent(following, ws.inv.files, out, max_steps=1,
                        answers=[{'id': question.id, 'answer': 0, 'answered_by': '测试财务'}])
    assert len(following.calls) == 1 and resumed.ws.notes.get('续跑答复') == '已进入下一步核对。'
    assert not resumed.pending and not resumed.results
    assert not case.load_case(out).get('total_confirmations')


@pytest.mark.parametrize('depends_on_ratings', [False, True])
def test_blank_scope_answer_keeps_only_unaffected_question_withdrawals(tmp_path, depends_on_ratings):
    from bonus_agent.extract_rules import Question

    ws, population, _, _, _ = blank_reference_review(tmp_path)
    rule = {'file': 'policy.md', 'quote': '每个项目按销售金额的10%计算奖金'}
    scope, expression = ('项目', 'amount + count(of: "ratings")') if depends_on_ratings else ('参与', 'share')
    ws.drafts['S'].setdefault('variables', {})['base_amount'] = {'scope': scope, 'expr': expression}
    ws.questions.append(Question(id='q1', text='是否只核算单个项目？', related='S::/variables/base_amount'))
    reviewed_question = {'decision': 'withdraw', 'kind': 'false_premise',
                         'reason': '原文按每个项目核算。', 'evidence': [rule]}
    clarification = {'unclear': ['ratings 第 2 行的 metric 为空，原文未明确取数口径。']}
    checker = attach_checker(ws, [reviewed_question, population, clarification])
    assert ws.review_question('q1', '核对项目范围', [rule])['status'] == 'WITHDRAWN'
    prepared(ws)
    ws.calculate_basis('S')
    with pytest.raises(ToolError, match='第 2 行'):
        ws.review_basis('S')
    question, = ws.pending
    out = tmp_path / 'out'
    save_staged_case(ws, out)

    primary = FakeLLM([{'tool': 'note', 'args': {'topic': '续跑答复', 'text': '已载入新答复。'}}])
    primary.workflow_mode = 'staged'
    primary.checker_required = True
    primary.checker_llm = checker
    result = run_agent(primary, ws.inv.files, out, max_steps=1,
                       answers=[{'id': question.id, 'answer': 0, 'answered_by': '测试财务'}])
    assert [q['id'] for q in result.pending] == (['q1'] if depends_on_ratings else [])
    assert not result.results and not case.load_case(out).get('total_confirmations')
    assert len(checker.calls) == 3


@pytest.mark.evidence
@pytest.mark.parametrize('source_review', [False, True])
def test_blank_scope_selection_reuses_the_saved_population(tmp_path, monkeypatch, source_review):
    from bonus_agent import workflow

    scope_rule = '评分单元格为空时不参与取数，真实数值零参与。'
    ws, population, supported, pool, reports = blank_reference_review(tmp_path, '\n地区费率25。\n' + scope_rule)
    finance = '财务确认：地区费率按百分数。'
    ws.said = finance + '\n\n财务确认：metric 数值 0 按原值参与核算。'
    ws.record_facts([{'name': '地区费率', 'file': 'policy.md', 'quote': '地区费率25。', 'value': '25'}])
    ws.drafts['S']['pool']['expr'] += ' + 0.25 * 0'
    ws.bind_facts([{'name': '地区费率', 'targets': ['S::/pool/expr'], 'unit': '%',
                    'unit_basis': {'file': '财务对话', 'quote': finance}}])
    population['populations']['rows:ratings']['sources'] = [{'doc': '财务对话', 'quote': ws.said}]
    prior = attach_checker(ws, [population, {'unclear': ['需要确认空单元格是否为占位。']}])
    prepared(ws)
    ws.calculate_basis('S')
    with pytest.raises(ToolError, match='是否为占位'):
        ws.review_basis('S')
    question, = ws.pending
    out = tmp_path / 'out'
    answers = [{'id': question.id, 'answer': 0, 'answered_by': '测试财务'}]
    if source_review:
        declaration = supported['populations']['rows:ratings']
        declaration['sources'] = [{'doc': 'policy.md', 'quote': scope_rule}]
        wrong = {**declaration, 'filter': {'metric': {'not': ['', '0']}}}
        with pytest.raises(ToolError, match='材料声明与本期取数范围不一致'):
            ws._flow().record_material_scope_review('S', 'rows:ratings', question.id, wrong, 'review-test')
        assert question.answer is None
        ws._flow().record_material_scope_review('S', 'rows:ratings', question.id, declaration, 'review-test')
        assert question.answered_by == '材料《policy.md》'
        answers = None
    save_staged_case(ws, out)

    primary = FakeLLM([{'tool': 'calculate_basis', 'args': {'scheme': 'S'}},
                       {'tool': 'review_basis', 'args': {'scheme': 'S'}}])
    primary.workflow_mode = 'staged'
    primary.checker_required = True
    primary.checker_llm = attach_checker(ws, [pool, reports])
    monkeypatch.setattr(workflow, '_EXECUTION_PROFILE', 'new-code-same-scope')
    result = run_agent(primary, ws.inv.files, out, max_steps=2, answers=answers)

    assert result.status == 'AWAITING_TOTAL_CONFIRMATION', [(s.tool, s.result) for s in result.steps if not s.ok]
    assert len(prior.calls) == 2 and len(primary.checker_llm.calls) == 2
    reference = result.ws.workflow['references']['S']
    applied = reference['spec']['populations']['rows:ratings']['filter']
    checked = next(r for r in reference['report']['population']['checks'] if r['scope'] == 'rows:ratings')
    assert applied == {'metric': {'not': ['']}} and checked['actual_count'] == checked['expected_count'] == 1
    assert reference['scope_confirmations']['rows:ratings']['question'] == question.id
    if source_review:
        assert reference['scope_confirmations']['rows:ratings']['source_review'] == 'review-test'
        reference['spec_state'] = 'population_difference'
        reference['scope_confirmations'] = {}
    assert not result.results and not case.load_case(out).get('total_confirmations')
    assert not result.ws._binding_gaps()
    result.ws.said += '\n\n财务更正：地区费率按千分数。'
    assert any('地区费率：parameter.unit' in gap for gap in result.ws._binding_gaps())
    if source_review:
        from bonus_agent.review_population import withdraw_stale_material_scope_answers
        from bonus_core.data.period import Period

        old = result.ws.questions[0].model_dump()
        result.ws.period = Period.parse('2026-10')
        assert withdraw_stale_material_scope_answers(result.ws, [old]) == []
        assert result.ws.questions[0].answer is None and result.ws.questions[0].withdrawn
        assert not result.ws.pending


def test_population_clarification_reuses_saved_declarations_after_upgrade(tmp_path, monkeypatch):
    import bonus_agent.basis_review_parts as parts
    from bonus_agent import workflow

    ws, population, resolved, pool, reports = blank_reference_review(tmp_path)
    with monkeypatch.context() as prior:
        prior.setattr(parts, 'blank_scope_witnesses', lambda report: {})
        prior.setattr(parts, '_BLANK_SCOPE_GUIDANCE', '')
        original_checker = attach_checker(ws, [population])
        assert ws.review_basis('S')['status'] == 'FAIL'
    assert len(original_checker.calls) == 1
    record = ws.workflow['references']['S']
    assert record['spec_state'] == 'population_difference'
    frozen = json.dumps(case.snapshot(ws), ensure_ascii=False)
    clone = Workspace(FakeLLM([]), ws.inv)
    clone.llm.checker_required = True
    case.restore(clone, json.loads(frozen))
    checker = attach_checker(clone, [resolved, pool, reports])
    monkeypatch.setattr(workflow, '_EXECUTION_PROFILE', 'review-source-upgrade')
    prepared(clone)
    clone.calculate_basis('S')
    outcome = clone.review_basis('S')
    assert outcome['status'] == 'PASS', outcome
    assert not clone._flow().current('approved:S')
    first = json.loads(checker.calls[0][1]['content'].split('\n本段输入：')[-1])
    assert set(first['prior_declaration']) == {'rows:ratings'}
    assert first['observed_empty_cells']['rows:ratings'] == [
        {'file': 'data.xlsx', 'sheet': 'ratings', 'row': 2, 'empty_columns': ['metric']}]
    assert len(checker.calls) == 3 and not clone.llm.calls
    formula_input = json.loads(checker.calls[1][1]['content'].split('\n本段输入：')[-1])
    scope = formula_input['engine_scope_observations']['项目']
    assert scope['objects'] == 2
    assert scope['readable_tables']['ratings'] == {'with_rows': 1, 'max_rows_per_object': 1}
    assert 'matching_distinct_keys' not in checker.calls[1][1]['content']
    records = clone.workflow['basis_review_parts']['S'].values()
    assert any(v.get('replayed_from') for v in records)
    assert next(v for v in records if v['section'] == 'populations')['value'] == population


def test_population_clarification_cannot_exclude_zero_or_repeat_a_valid_opinion(tmp_path):
    ws, population, resolved, _, _ = blank_reference_review(tmp_path)
    wrong = json.loads(json.dumps(resolved, ensure_ascii=False))
    wrong['populations']['rows:ratings']['filter']['metric']['not'].append('0')
    unchanged = {'populations': {'rows:ratings': population['populations']['rows:ratings']}}
    checker = attach_checker(ws, [population, wrong, unchanged])
    with pytest.raises(ToolError, match='只允许在证据所指的列'):
        ws.review_basis('S')
    assert ws.review_basis('S')['status'] == 'FAIL'
    assert ws.review_basis('S')['status'] == 'FAIL'
    assert len(checker.calls) == 3
    assert not ws._flow().current('review:S')


def test_segment_failure_does_not_fall_back_to_primary(tmp_path):
    ws = review_workspace(tmp_path)
    ws.llm.checker_required = False
    checker = attach_checker(ws, [])

    def unavailable(messages):
        raise LLMError('independent review unavailable')

    checker._responder = unavailable
    ws.llm._responder = lambda messages: pytest.fail('unexpected primary fallback')
    assert ws.review_basis('S')['status'] == 'ERROR'
    assert len(checker.calls) == 1 and not ws.llm.calls and not ws._flow().current('review:S')
