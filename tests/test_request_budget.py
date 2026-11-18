"""Job limits are checked before transport, shared across roles and never reset by retries."""
import copy
from types import SimpleNamespace as NS

import pytest

from bonus_agent.agent import Workspace, Step, _deep_reason, run_agent
from bonus_agent.llm import LLM, LLMError, RunBudgetError, Usage
from bonus_agent.recovery import RunBudget


class Transport:
    def __init__(self, responses=None):
        self.requests = []
        self.options = []
        self.responses = list(responses or [])
        self.chat = NS(completions=NS(create=self.create))

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        item = self.responses.pop(0) if self.responses else response()
        if isinstance(item, Exception):
            raise item
        return item() if callable(item) else item


def response(prompt=100, completion=50, *, reported=True):
    return NS(id='mock-response', model='mock',
              usage=NS(prompt_tokens=prompt, completion_tokens=completion) if reported else None,
              choices=[NS(finish_reason='stop', message=NS(content='{}', tool_calls=[]))])


def model(name='primary'):
    llm = LLM(name, name, 'https://example.invalid/v1', 'not-a-key')
    llm._ctx = None
    return llm


def transport(monkeypatch, responses=None):
    client = Transport(responses)
    monkeypatch.setattr('bonus_agent.llm.openai_client', lambda *args: client)
    return client


@pytest.mark.parametrize('kind', ['chat', 'chat_tools'])
def test_last_request_cannot_overshoot_two_million_allowance(monkeypatch, kind):
    client = transport(monkeypatch)
    llm = model()
    llm.usage.prompt_tokens = 1_970_000
    budget = RunBudget(0, 2_000_000, 900).bind(llm)
    messages = [{'role': 'user', 'content': '核算规则' * 8000}]
    with pytest.raises(RunBudgetError, match='未发出请求'):
        if kind == 'chat':
            llm.chat(messages, max_tokens=8192)
        else:
            llm.chat_tools(messages, [{'type': 'function', 'function': {'name': 'calculate'}}], max_tokens=8192)
    assert client.requests == []
    assert llm.last_call['budget']['actual_used_tokens'] == 1_970_000
    assert llm.last_call['budget']['remaining_tokens'] == 30_000
    assert llm.last_call['budget']['input_estimate_is_exact'] is False
    assert llm.last_call['status'] == 'budget_blocked'


@pytest.mark.parametrize('shared_usage', [False, True])
def test_primary_thinking_checker_and_advisor_copy_share_one_allowance(monkeypatch, shared_usage):
    client = transport(monkeypatch)
    primary, thinking, checker = model(), model('thinking'), model('checker')
    primary.think_llm, primary.checker_llm = thinking, checker
    if shared_usage:
        thinking.usage = checker.usage = primary.usage
    budget = RunBudget(0, 2000, 0).bind(primary)
    primary.chat([], max_tokens=100)
    primary.chat([], max_tokens=100, think=True)
    checker.chat([], max_tokens=100)
    advisor = copy.copy(thinking)
    advisor.chat([], max_tokens=100)
    assert budget.snapshot()['actual_used_tokens'] == 600
    assert advisor.run_budget is checker.run_budget is primary.run_budget is budget
    budget.bind(primary)  # Another continuation does not refund preceding calls.
    with pytest.raises(RunBudgetError):
        checker.chat([], max_tokens=1500)
    assert len(client.requests) == 4
    assert budget.snapshot()['remaining_tokens'] == 1400


def test_thinking_output_ceiling_is_reserved_before_sending(monkeypatch):
    client = transport(monkeypatch)
    llm = model()
    llm.think_body = {'thinking': {'type': 'enabled'}}
    llm.think_max_tokens = 1200
    RunBudget(0, 1000, 0).bind(llm)
    with pytest.raises(RunBudgetError):
        llm.chat([], max_tokens=1, think=True)
    assert llm.last_call['requested_output_tokens'] == 1200
    assert not client.requests


def test_deadline_caps_transport_and_rejects_late_actions(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('bonus_agent.recovery.time.monotonic', lambda: clock[0])
    def late():
        clock[0] += 4
        return response()
    client = transport(monkeypatch, [late])
    llm = model()
    budget = RunBudget(0, 10000, 5, started=1000).bind(llm)
    clock[0] = 1002
    with pytest.raises(RunBudgetError, match='未执行返回动作'):
        llm.chat_tools([], [], max_tokens=100)
    assert client.options == [{'max_retries': 0, 'timeout': 3.0}]
    assert budget.snapshot()['actual_used_tokens'] == 150
    assert budget.snapshot()['remaining_seconds'] == 0
    with pytest.raises(RunBudgetError):
        llm.chat([], max_tokens=1)
    assert len(client.requests) == 1


@pytest.mark.parametrize('reported', [False, True])
def test_unknown_usage_or_transport_failure_is_not_refunded(monkeypatch, reported):
    item = TimeoutError('remote outcome unknown') if reported else response(reported=False)
    client = transport(monkeypatch, [item])
    llm = model()
    budget = RunBudget(0, 1000, 0).bind(llm)
    if reported:
        with pytest.raises(LLMError, match='outcome unknown'):
            llm.chat([], max_tokens=700)
    else:
        with pytest.raises(RunBudgetError, match='用量未确认'):
            llm.chat([], max_tokens=700)
    state = budget.snapshot()
    assert state['actual_used_tokens'] == 0
    assert state['unconfirmed_reserved_tokens'] >= 956
    assert state['inflight_reserved_tokens'] == 0
    with pytest.raises(RunBudgetError):
        llm.chat([], max_tokens=1)
    assert len(client.requests) == 1


@pytest.mark.parametrize('prompt,completion', [(None, 50), (100, None), (-1000, 50), (100, -50), ('100', 50)])
def test_invalid_usage_cannot_refund_budget_or_execute_response(monkeypatch, prompt, completion):
    client = transport(monkeypatch, [response(prompt=prompt, completion=completion)])
    llm = model()
    budget = RunBudget(0, 10000, 0).bind(llm)
    with pytest.raises(RunBudgetError, match='用量未确认'):
        llm.chat_tools([], [], max_tokens=100)
    assert budget.snapshot()['actual_used_tokens'] == 0
    assert budget.snapshot()['unconfirmed_reserved_tokens'] > 100
    assert llm.usage.calls == 1
    with pytest.raises(RunBudgetError):
        llm.chat([], max_tokens=1)
    assert len(client.requests) == 1


def test_auxiliary_inputs_calibrate_following_copies_without_context_guard(monkeypatch):
    client = transport(monkeypatch, [response(prompt=600)])
    llm = model('checker')
    budget = RunBudget(0, 1500, 0).bind(llm)
    advisor = copy.copy(llm)  # Copied before calibration; the shared budget retains it.
    llm.chat([{'role': 'user', 'content': '检查'}], max_tokens=100)
    assert not hasattr(llm, '_pending_context_estimate')
    assert budget.snapshot()['remaining_tokens'] == 850
    with pytest.raises(RunBudgetError):
        advisor.chat([{'role': 'user', 'content': '检查'}], max_tokens=100)
    assert advisor.last_call['reserved_input_estimate'] >= 976
    assert len(client.requests) == 1


@pytest.mark.parametrize('required', [False, True])
def test_checker_budget_error_cannot_send_fallback_or_retry(monkeypatch, required):
    from bonus_agent.reread import _ask
    client = transport(monkeypatch)
    primary, checker = model(), model('checker')
    primary.checker_llm, primary.checker_required = checker, required
    budget = RunBudget(0, 1000, 0).bind(primary)
    ws = NS(llm=primary, checker_down='')
    for _ in range(2):
        with pytest.raises(LLMError):
            _ask(ws, [{'role': 'user', 'content': '独立检查'}])
    assert budget.error and not client.requests


def test_unknown_advisor_usage_cannot_allow_primary_to_continue(monkeypatch):
    from bonus_agent.recovery import local_advice
    client = transport(monkeypatch, [response(reported=False)])
    primary, advisor = model(), model('advisor')
    primary.think_llm = advisor
    budget = RunBudget(0, 100000, 0).bind(primary)
    with pytest.raises(RunBudgetError):
        local_advice(advisor, '检查来源', [], [], '核对原文')
    with pytest.raises(RunBudgetError):
        primary.chat([], max_tokens=1)
    assert len(client.requests) == 1
    assert budget.snapshot()['remaining_tokens'] > 0  # Sticky stop prevents repeated smaller attempts.


def test_provider_output_cap_prevents_unnecessary_full_output_reservation(monkeypatch):
    client = transport(monkeypatch)
    llm = model()
    llm._max_output_tokens = 100
    RunBudget(0, 500, 0).bind(llm)
    llm.chat([], max_tokens=8192)
    assert client.requests[0]['max_tokens'] == 100
    assert llm.last_call['requested_output_tokens'] == 100


def test_continuation_tightens_shared_cap_without_resetting_usage_or_deadline(tmp_path, monkeypatch):
    client = transport(monkeypatch)
    llm = model()
    budget = RunBudget(0, 100000, 900).bind(llm)
    llm.usage.prompt_tokens = 600
    started = budget.started
    policy = tmp_path / 'policy.md'
    policy.write_text('按原文计算。', encoding='utf8')
    result = run_agent(llm, [policy], max_steps=1, max_tokens=1000, run_budget=budget)
    assert result.error.startswith('作业预算事前拦截')
    assert budget.max_tokens == 1000
    assert budget.snapshot()['actual_used_tokens'] == 600
    assert budget.started == started and not client.requests
    budget.tighten(2000)
    assert budget.max_tokens == 1000


def test_metadata_uses_remaining_deadline_without_hidden_retries(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('bonus_agent.recovery.time.monotonic', lambda: clock[0])
    client = transport(monkeypatch)
    calls = []
    def metadata():
        calls.append(True)
        clock[0] += 4
        return [NS(id='primary', max_model_len=100000)]
    client.models = NS(list=metadata)
    llm = model()
    del llm._ctx
    budget = RunBudget(0, 10000, 3, started=1000).bind(llm)
    with pytest.raises(RunBudgetError):
        llm.context_window()
    assert client.options == [{'max_retries': 0, 'timeout': 3.0}]
    assert budget.snapshot()['remaining_seconds'] == 0
    with pytest.raises(RunBudgetError):
        llm.chat([], max_tokens=1)
    assert len(calls) == 1 and not client.requests


def test_deadline_also_prevents_model_metadata_request(monkeypatch):
    client = transport(monkeypatch)
    llm = model()
    del llm._ctx
    RunBudget(0, 10000, 1, started=0).bind(llm)
    with pytest.raises(RunBudgetError):
        llm.context_window()
    assert not client.options and not client.requests


def test_service_estimate_miss_records_actual_and_stops_returned_actions(monkeypatch):
    client = transport(monkeypatch, [response(prompt=1100, completion=50)])
    llm = model()
    budget = RunBudget(0, 1000, 0).bind(llm)
    with pytest.raises(RunBudgetError, match='估算低于服务实际'):
        llm.chat_tools([], [], max_tokens=100)
    assert budget.snapshot()['actual_used_tokens'] == 1150
    assert len(client.requests) == 1
    assert llm.last_call['status'] == 'budget_blocked'


def test_run_agent_registers_budget_and_does_not_fallback_after_preflight_stop(tmp_path, monkeypatch):
    client = transport(monkeypatch)
    llm = model()
    monkeypatch.setattr(Workspace, 'triage_all', lambda self: None)
    policy = tmp_path / 'policy.md'
    policy.write_text('按原文计算。', encoding='utf8')
    budget = RunBudget(0, 1000, 0)
    result = run_agent(llm, [policy], max_steps=3, run_budget=budget)
    assert result.error.startswith('作业预算事前拦截')
    assert result.usage['budget']['actual_used_tokens'] == 0
    assert llm.run_budget is budget and not client.requests


@pytest.mark.parametrize('tool,error', [
    ('plan_formula_tasks', '错误：construction.plan：同一目标只允许一个任务，修改时复用id'),
    ('update_dataset', '错误：unmatched_basis 缺少依据，请先登记来源'),
])
def test_explicit_contract_feedback_gets_primary_repair_before_optional_advisor(tool, error):
    step = Step(tool, {}, error, False)
    assert _deep_reason([step], 0, NS(), {tool: 1}) == ''
    assert _deep_reason([step], 0, NS(), {tool: 2})
