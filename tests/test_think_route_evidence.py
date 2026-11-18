"""Delegation must retain the failing route's evidence without changing its exception or allowance."""
import pytest

from bonus_agent.llm import LLMError, RunBudgetError
from bonus_agent.recovery import RunBudget
from tests.test_request_budget import Transport, model


def invoke(llm, kind, *, max_tokens=100):
    return getattr(llm, kind)([], **({'tools': []} if kind == 'chat_tools' else {}), think=True, max_tokens=max_tokens)


@pytest.mark.parametrize('kind', ['chat', 'chat_tools'])
def test_failed_think_route_preserves_transport_evidence_and_original_exception(monkeypatch, kind):
    original = TimeoutError('unknown transport outcome')
    transport = Transport([original])
    monkeypatch.setattr('bonus_agent.llm.openai_client', lambda *args: transport)
    primary, child = model(), model('thinking')
    primary.think_llm = child
    primary.last_call = {'status': 'stale-primary-success'}
    child.last_reasoning = 'retained child reasoning'
    budget = RunBudget(0, 10000, 0).bind(primary)
    with pytest.raises(LLMError) as error:
        invoke(primary, kind)
    assert error.value.__cause__ is original
    assert primary.last_call is child.last_call
    assert primary.last_reasoning == child.last_reasoning
    assert primary.last_call['model'] == 'thinking'
    assert primary.last_call['request_attempts'][0]['dispatch_status'] == 'unknown'
    assert primary.last_call['first_request_error']['type'] == 'TimeoutError'
    assert len(transport.requests) == 1 and budget.snapshot()['unconfirmed_reserved_tokens'] > 0
    assert budget.snapshot()['actual_used_tokens'] == 0 and budget.error


@pytest.mark.parametrize('kind', ['chat', 'chat_tools'])
def test_successful_think_route_keeps_response_and_records_usage_once(monkeypatch, kind):
    transport = Transport()
    monkeypatch.setattr('bonus_agent.llm.openai_client', lambda *args: transport)
    primary, child = model(), model('thinking')
    primary.think_llm = child
    budget = RunBudget(0, 10000, 0).bind(primary)
    value = invoke(primary, kind)
    assert value == '{}' if kind == 'chat' else value['content'] == '{}'
    assert primary.last_call is child.last_call and primary.last_call['status'] == 'completed'
    assert len(transport.requests) == 1 and child.usage.calls == 1
    assert budget.snapshot()['actual_used_tokens'] == 150
    assert budget.snapshot()['unconfirmed_reserved_tokens'] == 0


@pytest.mark.parametrize('kind', ['chat', 'chat_tools'])
def test_budget_blocked_route_is_visible_without_transport_or_retry(monkeypatch, kind):
    transport = Transport()
    monkeypatch.setattr('bonus_agent.llm.openai_client', lambda *args: transport)
    primary, child = model(), model('thinking')
    primary.think_llm = child
    RunBudget(0, 1, 0).bind(primary)
    with pytest.raises(RunBudgetError):
        invoke(primary, kind)
    assert not transport.requests
    assert primary.last_call is child.last_call
    assert primary.last_call['status'] == 'budget_blocked'
    assert primary.last_call['request_attempts'] == []


def test_nested_think_routes_propagate_same_leaf_failure_evidence(monkeypatch):
    transport = Transport([ConnectionResetError('leaf reset')])
    monkeypatch.setattr('bonus_agent.llm.openai_client', lambda *args: transport)
    primary, intermediate, leaf = model(), model('middle'), model('leaf')
    primary.think_llm, intermediate.think_llm = intermediate, leaf
    RunBudget(0, 10000, 0).bind(primary)
    with pytest.raises(LLMError):
        primary.chat([], think=True, max_tokens=100)
    assert primary.last_call is intermediate.last_call is leaf.last_call
    assert primary.last_call['model'] == 'leaf'
    assert len(transport.requests) == 1
