"""Incomplete reviewer protocols get one audited repair, never another business vote."""
import copy

import pytest

from bonus_agent.agent import ToolError
from bonus_agent.question_review import review_disputed_question, review_question
from bonus_agent.recovery import RunBudget
from tests.test_pending_question_review import EVIDENCE, RULE, opinion, setup
from tests.test_request_budget import model, response, transport

pytestmark = pytest.mark.evidence


def reply(content=None, *, finish='stop', reported=True, completion=100):
    value = response(prompt=200, completion=completion, reported=reported)
    value.choices[0].finish_reason = finish
    value.choices[0].message.content = opinion() if content is None else content
    return value


def prepared(tmp_path, monkeypatch, responses, *, tokens=200000):
    ws = setup(tmp_path, opinion())
    checker = ws.llm.checker_llm = model('independent')
    checker.think_body = {'thinking': {'type': 'enabled'}, 'reasoning_effort': 'high'}
    checker.think_max_tokens = 8192
    client = transport(monkeypatch, responses)
    budget = RunBudget(0, tokens, 900).bind(ws.llm)
    return ws, checker, client, budget


def review(ws, *, retry=False, reason='核对原问题的材料依据'):
    return review_question(ws, 'q1', reason, EVIDENCE, retry_technical=retry)


def test_16k_high_review_preserves_full_sources_and_records_actual_independent_request(tmp_path, monkeypatch):
    ws, checker, client, budget = prepared(tmp_path, monkeypatch, [reply()])
    source = tmp_path / '规则.txt'
    tail = '最终限定：旧项目不能因为资料缺失就自动当作新项目。'
    source.write_text(RULE + '\n' + '完整原文说明。' * 1500 + '\n' + tail, encoding='utf8')
    from bonus_agent.auto import build_inventory, collect_files
    from bonus_agent import case
    ws.inv = build_inventory(collect_files([tmp_path]))
    ws._source_version = case.fingerprint(ws.inv.files)
    result = review(ws)
    request = client.requests[0]
    assert request['max_tokens'] == 16384
    assert request['extra_body']['reasoning_effort'] == 'high'
    assert tail in request['messages'][1]['content']
    assert result['status'] == 'WITHDRAWN' and result['outcome'] == 'COMPLETED'
    attempt = ws.workflow['question_reviews']['q1']['attempts'][0]
    assert attempt['request']['usage_reported'] is True
    assert attempt['request']['response_received'] is True
    assert attempt['request']['budget_request_sequence'] == 1
    assert attempt['request']['requested_output_tokens'] == 16384
    assert attempt['budget_after']['actual_used_tokens'] == 300
    assert not ws.questions[0].answer and not ws.questions[0].answered_by
    assert ws.workflow['receipts'] == {'approved:S': {'unchanged': True}}


def test_confirmed_length_needs_explicit_once_only_larger_retry_with_same_prompt(tmp_path, monkeypatch):
    # Even valid-looking partial JSON must not become a business opinion at length.
    first = reply(opinion(decision='keep', reason='incomplete draft'), finish='length', completion=16384)
    ws, checker, client, budget = prepared(tmp_path, monkeypatch, [first, reply(finish='length', completion=32768)])
    out = review(ws)
    assert out['status'] == 'KEEP' and out['outcome'] == 'TECHNICAL'
    assert out['technical_retry_available'] and 'review' not in ws.workflow['question_reviews']['q1']
    assert review(ws, reason='改写理由不能自动续试')['cached']
    assert len(client.requests) == 1
    second = review(ws, retry=True, reason='不能把新理由注入同证据续试')
    assert second['status'] == 'KEEP' and not second['technical_retry_available']
    assert [r['max_tokens'] for r in client.requests] == [16384, 32768]
    assert client.requests[0]['messages'] == client.requests[1]['messages']
    assert budget.snapshot()['actual_used_tokens'] == 200 + 16384 + 200 + 32768
    audit = ws.workflow['question_reviews']['q1']
    assert audit['technical_retries'] == 1 and len(audit['attempts']) == 2
    assert [a['request']['budget_request_sequence'] for a in audit['attempts']] == [1, 2]
    with pytest.raises(ToolError, match='用完一次'):
        review(ws, retry=True)
    assert len(client.requests) == 2 and ws.pending


def test_larger_output_or_explicit_retry_cannot_resample_completed_business_keep(tmp_path, monkeypatch):
    ws, checker, client, budget = prepared(tmp_path, monkeypatch, [
        reply(opinion(decision='keep', kind='unresolved', reason='原文存在两种解释'))])
    assert review(ws)['outcome'] == 'COMPLETED'
    checker.think_max_tokens = 32768  # capacity is not new business evidence
    assert review(ws)['cached']
    with pytest.raises(ToolError, match='业务判断'):
        review(ws, retry=True)
    assert len(client.requests) == 1 and ws.pending


@pytest.mark.parametrize('bad', [
    reply(reported=False, finish='length'),
    reply(finish='content_filter'),
    reply(content='not json'),
    reply(opinion(evidence=[{'file': '规则.txt', 'quote': '没有这条原文，不能据此撤回问题。'}])),
])
def test_unknown_usage_or_rejected_opinion_never_qualifies_for_retry(tmp_path, monkeypatch, bad):
    ws, checker, client, budget = prepared(tmp_path, monkeypatch, [bad])
    assert review(ws)['status'] == 'KEEP'
    assert review(ws)['cached']
    with pytest.raises(ToolError):
        review(ws, retry=True)
    assert len(client.requests) == 1 and ws.pending and not ws.questions[0].withdrawn


def test_retry_uses_existing_budget_and_cannot_send_without_full_reservation(tmp_path, monkeypatch):
    ws, checker, client, budget = prepared(tmp_path, monkeypatch, [
        reply(finish='length', completion=16384),
        reply(opinion(decision='keep', kind='unresolved', reason='尚需财务明确'))])
    review(ws)
    budget.tighten(budget.snapshot()['actual_used_tokens'] + 1000)
    out = review(ws, retry=True)
    assert out['status'] == 'KEEP' and '预算事前拦截' in out['reason']
    assert len(client.requests) == 1 and budget.error
    audit = ws.workflow['question_reviews']['q1']
    assert out['technical_retry_not_sent'] and audit['technical_retries'] == 0
    assert len(audit['attempts']) == 1 and len(audit['technical_budget_blocks']) == 1
    RunBudget(0, 200000, 900).bind(ws.llm)
    assert review(ws, retry=True)['outcome'] == 'COMPLETED'
    assert len(client.requests) == 2
    assert ws.questions[0].withdrawn == ''


def test_provider_cap_cannot_turn_technical_retry_into_same_size_resampling(tmp_path, monkeypatch):
    ws, checker, client, budget = prepared(tmp_path, monkeypatch, [reply(finish='length', completion=16384)])
    review(ws)
    checker._max_output_tokens = 16384
    with pytest.raises(ToolError, match='输出上限无法提高'):
        review(ws, retry=True)
    assert len(client.requests) == 1


def test_unsent_initial_budget_block_keeps_first_request_pending_and_audited(tmp_path, monkeypatch):
    ws, checker, client, budget = prepared(tmp_path, monkeypatch, [reply()], tokens=1000)
    out = review(ws)
    assert out['initial_request_not_sent'] and not client.requests
    audit = ws.workflow['question_reviews']['q1']
    assert audit['attempts'] == [] and audit['initial_request_pending']
    assert len(audit['budget_events']) == 1 and audit['technical_retries'] == 0
    original_reason = audit['proposed_reason']
    assert review(ws)['cached'] and not client.requests
    with pytest.raises(ToolError, match='初次独立复核尚未发送'):
        review(ws, retry=True)
    RunBudget(0, 200000, 900).bind(ws.llm)
    out = review(ws, reason='改写理由不应覆盖尚未发送的审核包')
    assert out['outcome'] == 'COMPLETED' and len(client.requests) == 1
    audit = ws.workflow['question_reviews']['q1']
    assert len(audit['attempts']) == 1 and len(audit['budget_events']) == 1
    assert audit['proposed_reason'] == original_reason and not audit.get('initial_request_pending')
    assert ws.workflow['question_review_history']['q1'][-1]['initial_request_pending']


@pytest.mark.parametrize('next_allowance', [200000, None])
def test_automatic_resume_sends_only_the_still_unsent_opinion(tmp_path, monkeypatch, next_allowance):
    ws, _, client, _ = prepared(tmp_path, monkeypatch, [reply()], tokens=1000)
    ws.workflow['enabled'] = True
    assert review(ws)['initial_request_not_sent']
    q = ws.pending[0]
    assert not client.requests and q.answer is None
    RunBudget(0, next_allowance, 900).bind(ws.llm)
    result = review_disputed_question(ws, q)
    assert result['outcome'] == 'COMPLETED' and len(client.requests) == 1 and q.answer is None
    assert review_disputed_question(ws, q) is None and len(client.requests) == 1


def legacy_length(ws):
    record = ws.workflow['question_reviews']['q1']
    for field in ('attempts', 'outcome', 'technical_retries'):
        record.pop(field, None)
    for field in ('outcome', 'technical_retry_available', 'next'):
        record['result'].pop(field, None)
    return copy.deepcopy(record)


def test_legacy_length_requires_real_new_round_and_spends_single_protocol_repair(tmp_path, monkeypatch):
    ws, checker, client, old_budget = prepared(tmp_path, monkeypatch, [
        reply(finish='length'), reply(finish='length')])
    review(ws)
    old = legacy_length(ws)
    with pytest.raises(ToolError, match='真实恢复到新轮'):
        review(ws, retry=True)
    ws.round = 17
    ws._resumed_from_round = 16  # set only by run_agent restoring a saved case
    budget = RunBudget(0, 200000, 900).bind(ws.llm)
    assert review(ws)['cached'] and len(client.requests) == 1
    out = review(ws, retry=True)
    assert out['status'] == 'KEEP' and len(client.requests) == 2
    audit = ws.workflow['question_reviews']['q1']
    assert audit['legacy_usage_status'] == 'UNKNOWN_NOT_REFUNDED'
    assert audit['legacy_resumed_from_round'] == 16 and audit['technical_retries'] == 1
    assert audit['attempts'][0]['round'] == 17 and audit['attempts'][0]['output_tokens'] == 16384
    assert ws.workflow['question_review_history']['q1'][-1] == old
    assert budget.snapshot()['actual_used_tokens'] == 300
    with pytest.raises(ToolError, match='用完一次'):
        review(ws, retry=True)
    assert len(client.requests) == 2


def test_legacy_unknown_transport_is_not_migrated_as_length(tmp_path, monkeypatch):
    ws, checker, client, budget = prepared(tmp_path, monkeypatch, [reply(finish='length')])
    review(ws)
    legacy_length(ws)
    ws.workflow['question_reviews']['q1']['result']['reason'] = '独立问题复核未通过，保持等待：transport result unknown'
    ws.round, ws._resumed_from_round = 17, 16
    with pytest.raises(ToolError, match='旧复核没有'):
        review(ws, retry=True)
    assert len(client.requests) == 1


@pytest.mark.parametrize('thinking_ceiling', [16384, 32768])
def test_legacy_unknown_effective_limit_cannot_claim_16k_is_an_upgrade(tmp_path, monkeypatch, thinking_ceiling):
    ws, checker, client, budget = prepared(tmp_path, monkeypatch, [reply(finish='length')])
    review(ws)
    legacy_length(ws)
    ws.round, ws._resumed_from_round = 17, 16
    checker.think_max_tokens = thinking_ceiling
    with pytest.raises(ToolError, match='输出上限无法提高'):
        review(ws, retry=True)
    assert len(client.requests) == 1 and ws.pending
