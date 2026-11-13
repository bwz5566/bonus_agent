"""按需深度思考：平时不思考，出错、卡住的那一步临时打开思考，想清楚再改；各家模型的思考开关自动推出来。"""
from types import SimpleNamespace

import pytest

from bonus_agent import llm as L
from bonus_agent.llm import LLM, FakeLLM, load_config, think_switch

ROOT = L.Path(__file__).parent.parent
ANNUAL = ROOT / "evals" / "cases" / "annual_2026"


def test_think_switch_covers_each_dialect():
    assert think_switch({"enable_thinking": False}) == {"enable_thinking": True, "thinking_budget": L.THINK_BUDGET}
    assert think_switch({"chat_template_kwargs": {"enable_thinking": False}}) == {"chat_template_kwargs": {"enable_thinking": True}}
    assert think_switch({"chat_template_kwargs": {"thinking": False}}) == {"chat_template_kwargs": {"thinking": True}}
    assert think_switch({"thinking": {"type": "disabled"}}) == {"thinking": {"type": "enabled"}}
    assert think_switch({"enable_thinking": True}) is None and think_switch(None) is None and think_switch({"x": 1}) is None


def test_every_configured_model_is_adapted():
    """配置里每个能关掉思考的模型都能按需打开；本来不思考或一直思考的模型不切换。DeepSeek 思考内容要带回。"""
    cfg = load_config(L.DEFAULT_CONFIG)
    for name, m in cfg["models"].items():
        llm = LLM.from_config(name, cfg)
        body = {**(m.get("extra_body") or {}), **(m.get("tools_extra_body") or {})}
        off = "false" in str(body).lower() or "disabled" in str(body)
        assert llm.can_think == off, name
        assert llm.think_echo == (name.startswith("deepseek")), name


class _Client:
    def __init__(self, seen, reasoning="想清楚了"):
        self.seen, self.reasoning = seen, reasoning
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.seen.append(kw)
        msg = SimpleNamespace(content="ok", tool_calls=None, reasoning_content=self.reasoning)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)


def test_think_only_on_request_and_reasoning_echo(monkeypatch):
    seen, timeouts = [], []

    def fake_client(base_url, key, timeout):
        timeouts.append(timeout)
        return _Client(seen)
    monkeypatch.setattr(L, "openai_client", fake_client)
    llm = LLM("q", "q", "http://127.0.0.1:1/v1", "k", extra_body={"chat_template_kwargs": {"enable_thinking": False, "x": 1}},
              think_body={"chat_template_kwargs": {"enable_thinking": True}}, timeout=300, think_timeout=900)
    out = llm.chat_tools([{"role": "user", "content": "a"}], [])
    assert seen[-1]["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False, "x": 1}
    assert seen[-1]["max_tokens"] == 8192 and timeouts[-1] == 300 and out["reasoning"] == ""
    out = llm.chat_tools([{"role": "user", "content": "a"}], [], think=True)
    assert seen[-1]["extra_body"]["chat_template_kwargs"] == {"enable_thinking": True, "x": 1}     # 嵌套参数逐项合并
    assert seen[-1]["max_tokens"] == llm.think_max_tokens and timeouts[-1] == 900 and out["reasoning"] == "想清楚了"
    llm.chat([{"role": "user", "content": "a"}], json_out=True, think=True)
    assert "response_format" not in seen[-1] and llm.last_reasoning == "想清楚了"     # 思考时不强制 JSON 输出
    hist = [{"role": "assistant", "content": "", "reasoning_content": "想过"}]
    assert "reasoning_content" not in llm._prepare(hist)[0]
    llm.think_echo = True
    assert llm._prepare(hist)[0]["reasoning_content"] == "想过"
    plain = LLM("p", "p", "http://127.0.0.1:1/v1", "k")                 # 不能切换思考的模型：照常调用
    plain.chat_tools([{"role": "user", "content": "a"}], [], think=True)
    assert "extra_body" not in seen[-1] and seen[-1]["max_tokens"] == 8192


def test_think_model_handles_only_the_deep_steps(monkeypatch):
    """平时用便宜的模型，深度思考的那一步交给配置的 think_model（如 flash 平时干活、卡住时交给 pro），用量记在一处。"""
    seen = []
    monkeypatch.setattr(L, "openai_client", lambda base_url, key, timeout: _Client(seen))
    cfg = load_config(L.DEFAULT_CONFIG)
    llm = LLM.from_config("deepseek-flash-pro", cfg)
    assert llm.think_llm is not None and llm.think_llm.model == "deepseek-v4-pro" and llm.think_llm.usage is llm.usage
    llm.chat_tools([{"role": "user", "content": "a"}], [])
    assert seen[-1]["model"] == "deepseek-flash" and seen[-1]["extra_body"] == {"thinking": {"type": "disabled"}}
    out = llm.chat_tools([{"role": "user", "content": "a"}], [], think=True)
    assert seen[-1]["model"] == "deepseek-v4-pro" and seen[-1]["extra_body"] == {
        "thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    assert out["reasoning"] == "想清楚了"
    llm.chat([{"role": "user", "content": "a"}], json_out=True, think=True)
    assert seen[-1]["model"] == "deepseek-v4-pro" and llm.last_reasoning == "想清楚了"
    llm.chat([{"role": "user", "content": "a"}], json_out=True)
    assert seen[-1]["model"] == "deepseek-flash"
    cfg["models"]["deepseek-flash-pro"]["think_model"] = "nope"
    with pytest.raises(L.LLMError):
        LLM.from_config("deepseek-flash-pro", cfg)


class _Recorder(FakeLLM):
    def __init__(self, script):
        super().__init__(script)
        self.think_body = {"enable_thinking": True}
        self.flags, self.tails = [], []

    def chat_tools(self, messages, tools, **kw):
        self.flags.append(kw.get("think", False))
        self.tails.append(messages[-1]["content"])
        out = super().chat_tools(messages, tools, **kw)
        return {**out, "reasoning": "错在文件名" if kw.get("think") else ""}


def test_agent_thinks_after_error_then_switches_back():
    """出错的下一步深度思考（附分析要求、思考内容记进步骤），成功后切回普通模式；参数格式小错第一次不思考。"""
    from bonus_agent.agent import run_agent
    cols = {"a": {"column": "b", "type": "id"}}
    bad = {"tool": "define_dataset", "args": {"name": "x", "file": "无.xlsx", "sheet": "a", "fields": cols}}
    llm = _Recorder([bad, {"tool": "list_files"}, {"tool": "define_dataset", "args": {"nam": "x"}}, {"tool": "list_files", "args": {"a": 1}}])
    ar = run_agent(llm, [ANNUAL], max_steps=4)
    assert llm.flags == [False, True, False, False]
    assert "深度思考" in llm.tails[1] and "define_dataset 失败" in llm.tails[1] and "深度思考" not in llm.tails[2]
    assert ar.steps[1].deep and ar.steps[1].thought.startswith("【深度思考】错在文件名") and not ar.steps[0].deep
    assert ar.deep_steps == 1


def test_deep_budget_and_off():
    from bonus_agent.agent import run_agent
    cols = {"a": {"column": "b", "type": "id"}}
    bad = [{"tool": "define_dataset", "args": {"name": "x", "file": f"无{i}.xlsx", "sheet": "a", "fields": cols}} for i in range(5)]
    llm = _Recorder(bad)
    run_agent(llm, [ANNUAL], max_steps=5, deep=2)
    assert llm.flags == [False, True, True, False, False]
    llm = _Recorder(bad)
    run_agent(llm, [ANNUAL], max_steps=3, deep=0)
    assert llm.flags == [False, False, False]


def test_deep_step_with_only_analysis_hands_back():
    """深度思考那一步（如换成更强的模型）只回了分析、没调用工具：不算失败，分析留在历史里，下一步不思考、按分析动手"""
    from bonus_agent.agent import run_agent
    cols = {"a": {"column": "b", "type": "id"}}
    bad = {"tool": "define_dataset", "args": {"name": "x", "file": "无.xlsx", "sheet": "a", "fields": cols}}
    script = [bad] + ["错在文件名，应该先 list_files 看清文件"] * 1 + [{"tool": "list_files"}] * 6
    llm = _Recorder(script)
    ar = run_agent(llm, [ANNUAL], max_steps=6)
    assert llm.flags[:3] == [False, True, False]
    assert any("深度思考得出的分析" in str(c) and "错在文件名" in str(c) for c in llm.calls)
    assert "没有调用工具" not in (ar.error or "")
