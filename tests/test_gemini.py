import copy
import json

import pytest
import yaml
from openai.types.chat import ChatCompletionMessage

from bonus_agent.agent import _shrink, run_agent
from bonus_agent.llm import DEFAULT_CONFIG, LLM, LLMError
from tests.test_request_budget import model, response, transport


def test_gemini_key_and_review_route(monkeypatch):
    cfg = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(LLMError, match=r"GEMINI_API_KEY.*providers\.gemini\.api_key"):
        LLM.from_config("gemini-4-hydrogen", cfg)
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini")
    primary = LLM.from_config("gemini-4-hydrogen", cfg)
    reviewer = primary.checker_llm
    assert reviewer is not primary and primary.checker_required
    assert primary.model == reviewer.model == "gemini-4-hydrogen"
    assert primary.api_key == reviewer.api_key == "test-gemini"
    assert primary.extra_body["reasoning_effort"] == "low"
    assert reviewer.think_body["reasoning_effort"] == "high"


def test_agent_replays_signed_parallel_gemini_tools_without_rewriting(tmp_path, monkeypatch):
    rule = tmp_path / "policy.md"
    rule.write_text("虚构样例。合计两个数值。", encoding="utf-8")
    raw_arguments = json.dumps({"file": rule.name, "thought": "long test input " * 80}, indent=2)
    calls = [
        {"id": "call-a", "type": "function", "function": {"name": "read_document", "arguments": raw_arguments},
         "extra_content": {"google": {"thought_signature": "mock-signature"}}},
        {"id": "call-b", "type": "function", "function": {"name": "list_files", "arguments": "{}"}},
    ]
    original = {"role": "assistant", "tool_calls": calls}
    reply = response()
    reply.choices[0].message = ChatCompletionMessage.model_validate(copy.deepcopy(original))
    reply.choices[0].finish_reason = "tool_calls"
    client = transport(monkeypatch, [reply, response()])
    primary = model()
    primary.workflow_mode = "legacy"
    run = run_agent(primary, [rule], max_steps=2)

    assert len(client.requests) == 2
    history = client.requests[1]["messages"]
    index = next(i for i, msg in enumerate(history) if msg.get("tool_calls"))
    assert history[index] == original
    results = history[index + 1:index + 3]
    assert [(r["role"], r["tool_call_id"], r["name"]) for r in results] == [
        ("tool", "call-a", "read_document"), ("tool", "call-b", "list_files")]
    assert run.steps[0].tool == "read_document" and run.steps[0].ok
    assert rule.name in results[0]["content"]

    # When the size limit is hit, the most recent complete turn must still be valid for replay.
    history = copy.deepcopy(history[index:index + 3])
    history[1]["content"] *= 100
    before = copy.deepcopy(history)
    assert _shrink(history, max_chars=300, keep=1)
    assert history == before
    history.append({"role": "assistant", "content": "done"})
    assert _shrink(history, max_chars=300, keep=1)
    assert history == [{"role": "assistant", "content": "done"}]
