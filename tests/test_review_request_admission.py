"""Review context and attempt limits follow the real request, not an 8k placeholder."""
from types import SimpleNamespace

import pytest

from bonus_agent import reread
from bonus_agent.context import ContextBudgetError
from bonus_agent.llm import FakeLLM, LLMError, RunBudgetError
from bonus_agent.recovery import RunBudget
from tests.test_staged_workflow import make_workspace, prepared

pytestmark = pytest.mark.evidence


def test_review_context_follows_effective_route_and_thinking_output(monkeypatch):
    primary, checker, routed = FakeLLM([]), FakeLLM([]), FakeLLM([])
    primary.checker_llm = checker
    primary.checker_required = True
    checker.think_llm = routed
    routed.think_body = {'thinking': {'type': 'enabled'}}
    routed.think_max_tokens = 16384
    seen = []
    monkeypatch.setattr('bonus_agent.context.guard_messages',
                        lambda messages, model, **kw: seen.append((model, kw['output_tokens'])))
    ws = SimpleNamespace(llm=primary, checker_down='')
    assert reread.guard_request(ws, [{'role': 'user', 'content': 'original rules'}]) is routed
    assert seen == [(routed, 16384)]
    routed._max_output_tokens = 12000
    reread.guard_request(ws, [])
    assert seen[-1] == (routed, 12000)


def test_minimum_time_stop_is_not_a_checker_outage_or_a_spent_attempt(tmp_path):
    ws = prepared(make_workspace(tmp_path))
    ws.calculate_basis('S')
    checker = ws.llm.checker_llm
    checker.min_request_seconds = 300
    budget = RunBudget(0, 100000, 120).bind(ws.llm)
    before = dict(ws.workflow['attempts'])
    with pytest.raises(RunBudgetError, match='请求时间事前拦截'):
        ws._flow().review_basis('S')
    assert ws.workflow['attempts'] == before and not checker.calls
    assert not getattr(ws, 'checker_down', '')
    assert budget.snapshot()['actual_used_tokens'] == budget.snapshot()['unconfirmed_reserved_tokens'] == 0


@pytest.mark.parametrize('not_sent', [True, False])
def test_only_proven_unsent_deadline_race_preserves_review_attempt(tmp_path, monkeypatch, not_sent):
    ws = prepared(make_workspace(tmp_path))
    ws.calculate_basis('S')

    def stopped(workspace, messages):
        exc = RunBudgetError('simulated deadline boundary')
        if not_sent:
            exc.request_not_sent = True
        else:
            workspace.llm.checker_llm.last_call = {
                'status': 'budget_blocked', 'budget_request_sequence': 1,
                'request_attempts': [{'dispatch_status': 'unknown'}], 'response_received': False,
            }
        raise exc

    monkeypatch.setattr(reread, '_ask', stopped)
    with pytest.raises(RunBudgetError):
        ws._flow().review_basis('S')
    assert sum(ws.workflow['attempts'].values()) == (0 if not_sent else 1)
    assert not ws.llm.checker_llm.calls


def test_ask_budget_stop_does_not_mark_independent_model_unavailable(monkeypatch):
    primary, checker = FakeLLM([]), FakeLLM([])
    primary.checker_llm, primary.checker_required = checker, True
    ws = SimpleNamespace(llm=primary, checker_down='')
    monkeypatch.setattr(reread, 'guard_request', lambda *args: checker)
    def no_allowance(*args, **kwargs):
        raise RunBudgetError('new request not funded')
    monkeypatch.setattr(checker, 'chat', no_allowance)
    with pytest.raises(RunBudgetError):
        reread._ask(ws, [])
    assert ws.checker_down == ''


def test_optional_fallback_checks_its_own_context_before_primary_call():
    primary, checker = FakeLLM(['must not run']), FakeLLM([])
    primary.checker_llm = checker
    primary.ctx, checker.ctx = 8192, 128000
    def unavailable(messages, **kwargs):
        checker.calls.append(messages)
        raise LLMError('checker service unavailable')
    checker.chat = unavailable
    ws = SimpleNamespace(llm=primary, checker_down='')
    with pytest.raises(ContextBudgetError, match='本地上下文预算拦截'):
        reread._ask(ws, [{'role': 'user', 'content': 'original rules'}])
    assert len(checker.calls) == 1 and not primary.calls
    assert 'checker service unavailable' in ws.checker_down


def test_optional_fallback_time_gate_preserves_attempted_review(tmp_path):
    ws = prepared(make_workspace(tmp_path))
    ws.calculate_basis('S')
    ws.llm.checker_required = False
    ws.llm.min_request_seconds = 300
    checker = ws.llm.checker_llm
    checker.min_request_seconds = 0
    def unavailable(messages, **kwargs):
        checker.calls.append(messages)
        raise LLMError('checker service unavailable')
    checker.chat = unavailable
    budget = RunBudget(0, 100000, 120).bind(ws.llm)
    with pytest.raises(RunBudgetError, match='请求时间事前拦截') as caught:
        ws._flow().review_basis('S')
    assert not getattr(caught.value, 'request_not_sent', False)
    assert sum(ws.workflow['attempts'].values()) == 1
    assert len(checker.calls) == 1 and not ws.llm.calls
    assert budget.snapshot()['unconfirmed_reserved_tokens'] == 0


def test_optional_fallback_guard_does_not_route_back_to_failed_thinker(monkeypatch):
    primary, checker = FakeLLM(['fallback result']), FakeLLM([])
    primary.checker_llm = primary.think_llm = checker
    checker.think_body = {'thinking': {'type': 'enabled'}}
    checker.think_max_tokens = 16384
    def unavailable(messages, **kwargs):
        checker.calls.append(messages)
        raise LLMError('checker service unavailable')
    checker.chat = unavailable
    seen, call_options = [], []
    monkeypatch.setattr('bonus_agent.context.guard_messages',
                        lambda messages, model, **kw: seen.append((model, kw['output_tokens'])))
    chat = primary.chat
    def fallback(messages, **kwargs):
        call_options.append(kwargs)
        return chat(messages, **kwargs)
    primary.chat = fallback
    ws = SimpleNamespace(llm=primary, checker_down='')
    assert reread._ask(ws, [{'role': 'user', 'content': 'original rules'}]) == 'fallback result'
    assert seen == [(checker, 16384), (primary, 8192)]
    assert len(checker.calls) == 1 and len(primary.calls) == 1
    assert call_options[0]['think'] is False


def test_required_checker_failure_never_admits_a_primary_fallback(monkeypatch):
    primary, checker = FakeLLM(['must not run']), FakeLLM([])
    primary.checker_llm, primary.checker_required = checker, True
    def unavailable(*args, **kwargs):
        raise LLMError('checker service unavailable')
    checker.chat = unavailable
    seen = []
    monkeypatch.setattr('bonus_agent.context.guard_messages',
                        lambda messages, model, **kw: seen.append(model))
    with pytest.raises(LLMError, match='不降级'):
        reread._ask(SimpleNamespace(llm=primary, checker_down=''), [])
    assert seen == [checker] and not primary.calls
