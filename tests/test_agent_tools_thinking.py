"""Tool-turn reasoning is independent of the bounded recovery/advisor budget."""
import copy
import json

import pytest

from bonus_agent.agent import JSON_PROTOCOL, run_agent
from bonus_agent.context import ContextBudgetError
from bonus_agent.llm import FakeLLM


class Recorder(FakeLLM):
    def __init__(self, script, mode='native'):
        encoded = [json.dumps(item) if isinstance(item, dict) else item for item in script]
        super().__init__(encoded if mode == 'json' else script)
        self.tool_mode = mode
        self.think_body = {'enable_thinking': True}
        self.think_echo = True
        self.requests = []

    def _capture(self, messages, kw):
        self.requests.append((copy.deepcopy(messages), kw.get('think', False)))
        return f'tool reasoning {len(self.requests)}'

    def chat_tools(self, messages, tools, **kw):
        reasoning = self._capture(messages, kw)
        reply = super().chat_tools(messages, tools, **kw)
        # Deliberately return reasoning even when not requested: the loop must
        # not expose stale/unrequested provider reasoning in the default mode.
        return {**reply, 'reasoning': reasoning}

    def chat(self, messages, **kw):
        if JSON_PROTOCOL in str(messages[0].get('content', '')):
            self.last_reasoning = self._capture(messages, kw)
        return super().chat(messages, **kw)


def material(tmp_path):
    path = tmp_path / 'input.csv'
    path.write_text('pid,amount\nA,100\n', encoding='utf-8')
    return path


@pytest.mark.parametrize('mode', ['native', 'json'])
@pytest.mark.parametrize('enabled', [False, True])
def test_configured_thinking_with_zero_recovery_budget_preserves_reasoning(mode, enabled, tmp_path):
    script = [
        {'tool': 'read_document', 'args': {'file': 'missing.md'}},
        {'tool': 'list_files'},
        {'tool': 'read_workspace', 'args': {'section': 'summary'}},
    ]
    llm = Recorder(script, mode)
    assert llm.tools_thinking is False  # no opt-in preserves the prior default
    if enabled:
        llm.tools_thinking = True
    llm.recovery_mode = 'advisor'
    llm.think_llm = FakeLLM(lambda *args: pytest.fail('tool thinking must not invoke the recovery advisor'))
    result = run_agent(llm, [material(tmp_path)], max_steps=3, deep=0, reflect=0)

    assert [flag for _, flag in llm.requests] == [enabled] * 3
    assert result.deep_steps == result.usage['deep_steps'] == 0
    assert not llm.think_llm.calls
    steps = [step for step in result.steps if step.tool in {'read_document', 'list_files', 'read_workspace'}]
    assert len(steps) == 3 and not steps[0].ok
    assert all(step.deep is enabled for step in steps)
    for index, (messages, _) in enumerate(llm.requests[1:], 1):
        echoed = [m.get('reasoning_content') for m in messages if m['role'] == 'assistant']
        if enabled:
            assert f'tool reasoning {index}' in echoed
        else:
            assert not any(echoed)
    assert ('tool reasoning 1' in steps[0].thought) is enabled


def test_always_thinking_analysis_without_tools_still_hits_nudge_limit(tmp_path):
    llm = Recorder(['Still considering the evidence.'])
    llm.tools_thinking = True
    result = run_agent(llm, [material(tmp_path)], max_steps=20, deep=0, reflect=0)
    assert '模型多次没有调用工具' in result.error
    assert len(llm.requests) == 4
    assert all(flag for _, flag in llm.requests)
    assert result.deep_steps == 0
    assert not result.steps


@pytest.mark.parametrize('mode', ['native', 'json'])
def test_context_guard_and_compaction_reserve_configured_thinking_output(mode, tmp_path, monkeypatch):
    llm = Recorder([{'tool': 'list_files'}, {'tool': 'read_workspace', 'args': {'section': 'summary'}}], mode)
    llm.tools_thinking = True
    llm.think_max_tokens = 24576
    llm.workflow_mode = 'staged'
    checks = []
    rejected_history = False

    def guard(messages, model, *, output_tokens=8192, tools=None):
        nonlocal rejected_history
        assert model is llm
        checks.append((len(messages), output_tokens, bool(tools)))
        if len(messages) > 3 and not rejected_history:
            rejected_history = True
            raise ContextBudgetError('simulated history requires compaction')

    monkeypatch.setattr('bonus_agent.context.guard_messages', guard)
    result = run_agent(llm, [material(tmp_path)], max_steps=3, deep=0, reflect=0)
    assert rejected_history and len(llm.requests) == 2
    assert len(checks) >= 4  # initial request, rejection, immutable floor, retry
    assert all(output == 24576 for _, output, _ in checks)
    assert all(has_tools is (mode == 'native') for _, _, has_tools in checks)
    assert result.deep_steps == 0
