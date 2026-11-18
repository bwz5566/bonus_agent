"""A request needs enough remaining job time before reservation and before transport."""
import json

import pytest

from bonus_agent.llm import LLM, LLMError, RunBudgetError
from bonus_agent.recovery import RunBudget, local_advice
from tests.test_request_budget import Transport, model, response
from tests.test_tools_thinking_config import config


@pytest.mark.parametrize('value', [-1, float('nan'), float('inf'), -float('inf'), True, '120', None, 10**400])
def test_minimum_request_time_requires_a_finite_nonnegative_number(value):
    cfg = config()
    cfg['models']['deepseek-pro-repair']['min_request_seconds'] = value
    with pytest.raises(LLMError, match='min_request_seconds'):
        LLM.from_config('deepseek-pro-repair', cfg)


def test_model_profiles_keep_separate_execution_and_critical_review_windows():
    llm = LLM.from_config('deepseek-pro-repair', config())
    assert llm.min_request_seconds == 120
    assert llm.checker_llm.min_request_seconds == llm.checker_llm.think_timeout == 300
    assert llm.checker_llm.think_max_tokens == 16384
    assert llm.vision_llm.min_request_seconds == 0


@pytest.mark.parametrize('kind', ['chat', 'chat_tools'])
def test_insufficient_time_stops_before_reservation_or_client_creation(monkeypatch, kind):
    clock = [1000.0]
    monkeypatch.setattr('bonus_agent.recovery.time.monotonic', lambda: clock[0])
    llm = model()
    llm.min_request_seconds = 120
    llm.usage.prompt_tokens = 250
    budget = RunBudget(0, 20000, 900, started=1000).bind(llm)
    clock[0] += 864.2
    monkeypatch.setattr('bonus_agent.llm.openai_client', lambda *args: pytest.fail('client must not be created'))
    with pytest.raises(RunBudgetError, match='剩余 35.8 秒.*至少 120 秒.*未发出请求') as error:
        getattr(llm, kind)([], **({'tools': []} if kind == 'chat_tools' else {}), max_tokens=100)
    assert error.value.request_not_sent is True
    assert llm.last_call['status'] == 'budget_blocked'
    assert llm.last_call['request_attempts'] == []
    state = budget.snapshot()
    assert state['actual_used_tokens'] == 250
    assert state['unconfirmed_reserved_tokens'] == state['inflight_reserved_tokens'] == 0
    assert budget._sequence == 0


def test_time_is_rechecked_after_client_setup_without_creating_unknown_usage(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('bonus_agent.recovery.time.monotonic', lambda: clock[0])
    llm = model()
    llm.min_request_seconds = 120
    budget = RunBudget(0, 20000, 900, started=1000).bind(llm)
    client = Transport()
    def create_client(*args):
        clock[0] += 800
        return client
    monkeypatch.setattr('bonus_agent.llm.openai_client', create_client)
    with pytest.raises(RunBudgetError, match='作业请求时间事前拦截') as error:
        llm.chat([], max_tokens=100)
    assert error.value.request_not_sent is True
    assert not client.requests and budget._sequence == 1
    assert llm.last_call['request_attempts'][0]['transport_invoked'] is False
    assert llm.last_call['request_attempts'][0]['dispatch_status'] == 'not_sent'
    state = budget.snapshot()
    assert state['actual_used_tokens'] == state['unconfirmed_reserved_tokens'] == state['inflight_reserved_tokens'] == 0


@pytest.mark.parametrize('minimum,duration,elapsed', [(0, 900, 899), (120, 900, 780), (300, 0, 900)])
def test_default_exact_boundary_and_unlimited_time_remain_valid(monkeypatch, minimum, duration, elapsed):
    clock = [1000.0 + elapsed]
    monkeypatch.setattr('bonus_agent.recovery.time.monotonic', lambda: clock[0])
    llm = model()
    llm.min_request_seconds = minimum
    budget = RunBudget(0, 20000, duration, started=1000).bind(llm)
    client = Transport()
    monkeypatch.setattr('bonus_agent.llm.openai_client', lambda *args: client)
    llm.chat([], max_tokens=100)
    assert len(client.requests) == 1 and budget.snapshot()['actual_used_tokens'] == 150


def test_existing_unknown_allowance_is_never_refunded_by_time_stop(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('bonus_agent.recovery.time.monotonic', lambda: clock[0])
    llm = model()
    llm.min_request_seconds = 120
    budget = RunBudget(0, 20000, 900, started=1000).bind(llm)
    request = budget.reserve(llm, 500, 200)
    budget.settle(request, sent=True, reported=False)
    clock[0] += 880
    monkeypatch.setattr('bonus_agent.llm.openai_client', lambda *args: pytest.fail('unknown request cannot be retried'))
    with pytest.raises(RunBudgetError, match='用量未确认') as error:
        llm.chat([], max_tokens=100)
    assert not getattr(error.value, 'request_not_sent', False)
    assert budget.snapshot()['unconfirmed_reserved_tokens'] == 700
    assert budget._sequence == 1


def test_a_late_sent_response_never_claims_request_not_sent(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('bonus_agent.recovery.time.monotonic', lambda: clock[0])
    llm = model()
    llm.min_request_seconds = 120
    budget = RunBudget(0, 20000, 900, started=1000).bind(llm)
    def late_response():
        clock[0] += 901
        return response()
    client = Transport([late_response])
    monkeypatch.setattr('bonus_agent.llm.openai_client', lambda *args: client)
    with pytest.raises(RunBudgetError, match='已记录响应实际用量') as error:
        llm.chat([], max_tokens=100)
    assert not getattr(error.value, 'request_not_sent', False)
    assert len(client.requests) == 1 and budget.snapshot()['actual_used_tokens'] == 150


def test_local_advice_caps_its_own_minimum_without_weakening_critical_review(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('bonus_agent.recovery.time.monotonic', lambda: clock[0])
    llm = LLM.from_config('deepseek-v4-pro', config())
    budget = RunBudget(0, 50000, 200, started=1000).bind(llm)
    answer = response()
    answer.choices[0].message.content = json.dumps({
        'diagnosis': 'source gap', 'next_action': 'read source', 'evidence_needed': 'policy', 'acceptance': 'exact binding'})
    client, timeouts = Transport([answer]), []
    def create_client(url, key, timeout):
        timeouts.append(timeout)
        return client
    monkeypatch.setattr('bonus_agent.llm.openai_client', create_client)
    local_advice(llm, 'synthetic failure', [], [], 'inspect source')
    assert len(client.requests) == 1 and timeouts == [180]
    assert client.requests[0]['max_tokens'] == 4096
    assert client.requests[0]['extra_body']['reasoning_effort'] == 'low'
    assert llm.min_request_seconds == llm.think_timeout == 300
    assert llm.think_max_tokens == 16384 and llm.think_body['reasoning_effort'] == 'high'
    with pytest.raises(RunBudgetError, match='至少 300 秒'):
        llm.chat([], json_out=True, think=True)
    assert len(client.requests) == 1 and budget.snapshot()['actual_used_tokens'] == 150
