"""Explicit repair-tool thinking keeps request evidence and one shared allowance."""
import pytest
import yaml

from bonus_agent.llm import DEFAULT_CONFIG, LLM, LLMError, RunBudgetError
from bonus_agent.recovery import RunBudget
from tests.test_request_budget import Transport, response


def config():
    cfg = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding='utf-8'))
    cfg['providers']['deepseek'] = {'base_url': 'https://example.invalid/v1', 'api_key': 'synthetic-test-key'}
    return cfg


def reply(model='deepseek-v4-pro'):
    result = response()
    result.model = model
    result.choices[0].message.reasoning_content = 'Synthetic reasoning for the next tool turn'
    return result


def test_repair_profile_is_optional_and_keeps_an_independent_high_effort_checker():
    cfg = config()
    llm = LLM.from_config('deepseek-pro-repair', cfg)
    assert cfg['default'] == 'deepseek-flash'
    assert not LLM.from_config('deepseek-flash', cfg).tools_thinking
    assert llm.model == 'deepseek-v4-pro' and llm.tools_thinking
    assert llm.think_body == {'thinking': {'type': 'enabled'}, 'reasoning_effort': 'low'}
    assert (llm.think_max_tokens, llm.think_timeout) == (16384, 300)
    assert llm.think_echo and llm.think_llm is None
    assert llm.checker_required and llm.checker_llm is not llm
    assert llm.checker_llm.model == 'deepseek-v4-pro'
    assert llm.checker_llm.think_body['reasoning_effort'] == 'high'
    assert llm.workflow_mode == 'staged'
    assert (llm.max_input_tokens, llm.max_history_chars) == (160000, 80000)
    assert llm.vision_llm.model == 'deepseek-flash'


def test_high_repair_profile_is_explicit_and_keeps_low_profile_unchanged():
    cfg = config()
    high = LLM.from_config('deepseek-pro-repair-high', cfg)
    low = LLM.from_config('deepseek-pro-repair', cfg)
    assert cfg['default'] == 'deepseek-flash'
    assert high.model == low.model == 'deepseek-v4-pro'
    assert high.tools_thinking and high.think_llm is None
    assert high.think_body['reasoning_effort'] == 'high'
    assert high.min_request_seconds == high.think_timeout == 300
    assert high.think_max_tokens == 16384
    assert high.checker_required and high.checker_llm is not high
    assert high.checker_llm.name == 'deepseek-v4-pro'
    assert high.checker_llm.think_body['reasoning_effort'] == 'high'
    assert (high.workflow_mode, high.max_input_tokens, high.max_history_chars) == ('staged', 160000, 80000)
    assert high.vision_llm.model == 'deepseek-flash'
    assert low.think_body['reasoning_effort'] == 'low' and low.min_request_seconds == 120


@pytest.mark.parametrize('value', ['true', 'false', 1, None])
def test_tools_thinking_is_a_boolean_config_option(value):
    cfg = config()
    cfg['models']['deepseek-pro-repair']['tools_thinking'] = value
    with pytest.raises(LLMError, match='tools_thinking'):
        LLM.from_config('deepseek-pro-repair', cfg)


def test_tools_thinking_rejects_a_model_without_thinking_support():
    cfg = config()
    cfg['models']['deepseek-pro-repair']['think'] = False
    with pytest.raises(LLMError, match='tools_thinking=true.*think_model'):
        LLM.from_config('deepseek-pro-repair', cfg)


@pytest.mark.parametrize('kind', ['native', 'json'])
def test_tool_request_enables_own_low_effort_thinking_with_truthful_metadata_and_echo(monkeypatch, kind):
    llm = LLM.from_config('deepseek-pro-repair', config())
    client = Transport([reply(), reply()])
    timeouts = []
    def create_client(base_url, key, timeout):
        timeouts.append(timeout)
        return client
    monkeypatch.setattr('bonus_agent.llm.openai_client', create_client)
    budget = RunBudget(0, 50000, 0).bind(llm)
    messages = [{'role': 'user', 'content': 'Use a tool to inspect the synthetic input'}]
    if kind == 'native':
        result = llm.chat_tools(messages, [], think=llm.tools_thinking)
        reasoning = result['reasoning']
    else:
        llm.chat(messages, json_out=True, think=llm.tools_thinking)
        reasoning = llm.last_reasoning
    packet = client.requests[0]
    assert packet['model'] == 'deepseek-v4-pro'
    assert packet['extra_body'] == {'thinking': {'type': 'enabled'}, 'reasoning_effort': 'low'}
    assert packet['max_tokens'] == 16384 and timeouts == [300]
    assert 'response_format' not in packet
    assert llm.last_call['thinking'] is True and llm.last_call['reasoning_effort'] == 'low'
    assert llm.last_call['requested_output_tokens'] == 16384
    assert llm.last_call['status'] == 'completed' and llm.last_call['model'] == 'deepseek-v4-pro'
    assert len(client.requests) == 1 and budget.snapshot()['actual_used_tokens'] == 150
    history = [*messages, {'role': 'assistant', 'content': '{}', 'reasoning_content': reasoning}]
    if kind == 'native':
        llm.chat_tools(history, [], think=llm.tools_thinking)
    else:
        llm.chat(history, json_out=True, think=llm.tools_thinking)
    assert client.requests[1]['messages'][-1]['reasoning_content'] == reasoning
    assert reasoning and budget.snapshot()['actual_used_tokens'] == 300


def test_plain_extraction_does_not_inherit_tools_thinking(monkeypatch):
    llm = LLM.from_config('deepseek-pro-repair', config())
    client = Transport([reply()])
    monkeypatch.setattr('bonus_agent.llm.openai_client', lambda *args: client)
    llm.chat([], json_out=True)
    assert client.requests[0]['extra_body'] == {'thinking': {'type': 'disabled'}}
    assert client.requests[0]['response_format'] == {'type': 'json_object'}
    assert llm.last_call['thinking'] is False


def test_repair_thinking_output_is_reserved_before_transport(monkeypatch):
    llm = LLM.from_config('deepseek-pro-repair', config())
    client = Transport()
    monkeypatch.setattr('bonus_agent.llm.openai_client', lambda *args: client)
    RunBudget(0, 16000, 0).bind(llm)
    with pytest.raises(RunBudgetError):
        llm.chat_tools([], [], think=llm.tools_thinking, max_tokens=1)
    assert not client.requests
    assert llm.last_call['thinking'] is True
    assert llm.last_call['requested_output_tokens'] == 16384


def test_executor_checker_and_both_vision_routes_share_the_job_budget(monkeypatch):
    llm = LLM.from_config('deepseek-pro-repair', config())
    client = Transport([reply(), reply(), reply('deepseek-flash'), reply('deepseek-flash')])
    monkeypatch.setattr('bonus_agent.llm.openai_client', lambda *args: client)
    budget = RunBudget(0, 100000, 0).bind(llm)
    llm.chat_tools([], [], think=llm.tools_thinking)
    llm.checker_llm.chat([], json_out=True, think=True)
    llm.vision_llm.chat([])
    llm.checker_llm.vision_llm.chat([])
    assert client.requests[0]['extra_body']['reasoning_effort'] == 'low'
    assert client.requests[1]['extra_body']['reasoning_effort'] == 'high'
    assert all(model.run_budget is budget for model in (llm, llm.checker_llm, llm.vision_llm, llm.checker_llm.vision_llm))
    assert len(client.requests) == 4 and budget.snapshot()['actual_used_tokens'] == 600
