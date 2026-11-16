"""Read-only loops must stop: compact ledger listings, re-read and quiet-streak guards, no auto-continue without progress."""
import json

from bonus_agent.agent import QUIET_STUCK, Workspace, run_agent
from bonus_agent.auto import build_inventory
from bonus_agent.context import PAGE_CHARS, workspace_view
from bonus_agent.failure import retryable
from bonus_agent.llm import FakeLLM


class _Item:
    def __init__(self, d):
        self.d = d

    def model_dump(self, mode=None):
        return self.d


class _WS:
    def __init__(self, facts):
        self.facts, self.clauses, self.schemes = [_Item(f) for f in facts], [], []


def _fact(i):
    file = "policy.pptx"
    return {"name": f"{file}::P{i // 20 + 1}.T1.R{i % 20 + 1}.C2.L1", "value": str(i), "file": file,
            "quote": f"角色{i}\n新项目\n{i} 原文{i}号" * 4, "meaning": f"角色{i} | 新项目 | {i}", "override": False, "round": 1,
            "targets": [], "page": i // 20 + 1, "context": "某事业部 提成分成比例", "source_hash": "a" * 64,
            "source_cell": {"table": 1, "row": i % 20 + 1, "column": 2}, "component": "literal", "unit": None}


def _pages(ws, **kw):
    calls, args = 0, {"section": "facts", "offset": 0, "limit": 50, **kw}
    while True:
        calls += 1
        r = workspace_view(ws, args["section"], None, args.get("offset", 0), args.get("limit", 50), args.get("name"),
                           args.get("query"), args.get("file"), args.get("page"), args.get("detail_offset"),
                           bool(args.get("detail")))
        assert len(json.dumps(r, ensure_ascii=False)) <= PAGE_CHARS + 200
        if not r["next_call"]:
            return calls, r
        args = r["next_call"]["args"]


def test_ledger_listing_is_compact_and_full_record_on_demand():
    ws = _WS([_fact(i) for i in range(135)])
    brief_calls, last = _pages(ws)
    full_calls, _ = _pages(ws, detail=True)
    # A 135-row ledger used to take 15 pages; the listing must fit in a handful.
    assert brief_calls <= 6 and full_calls >= 3 * brief_calls
    item = last["items"][-1]
    assert {"name", "value", "meaning"} <= set(item) and "quote" not in item and "source_hash" not in item
    one = workspace_view(ws, "facts", name=_fact(7)["name"])
    assert one["items"][0]["quote"] == _fact(7)["quote"]            # name lookup returns the full record
    hit = workspace_view(ws, "facts", query="原文7号")   # filters still search the full record
    assert hit["total"] == 1 and "quote" not in hit["items"][0]


def _ws(tmp_path):
    p = tmp_path / "policy.md"
    p.write_text("甲组费率3%", encoding="utf8")
    return [p]


def test_rereading_same_page_between_writes_counts_as_stuck(tmp_path):
    # History compaction clears the exact-duplicate filter; only the limit changes between passes.
    script = [{"tool": "read_workspace", "args": {"section": "facts", "offset": (i % 3) * 9, "limit": 50 - i // 3}}
              for i in range(150)]
    ar = run_agent(FakeLLM(script), _ws(tmp_path), tmp_path / "out", max_steps=150)
    assert ar.error and "读了 3 次" in ar.error and not retryable(ar.error)
    assert any(s.tool == "（反思）" for s in ar.steps)
    assert len([s for s in ar.steps if s.tool == "read_workspace"]) < 30


def test_long_quiet_streak_stops_and_unproductive_round_is_not_auto_continued(tmp_path):
    script = [{"tool": "read_workspace", "args": {"section": "facts", "offset": i}} for i in range(200)]
    ar = run_agent(FakeLLM(script), _ws(tmp_path), tmp_path / "out", max_steps=QUIET_STUCK + 5)
    first = next(i for i, s in enumerate(ar.steps) if s.tool == "（反思）")
    assert first <= QUIET_STUCK + 1
    assert ar.error.startswith("本轮没有进展") and not retryable(ar.error)


def test_a_write_resets_the_read_guards(tmp_path):
    read = lambda o: {"tool": "read_workspace", "args": {"section": "facts", "offset": o}}
    note = {"tool": "record_facts", "args": {"facts": [{"name": "费率", "file": "policy.md", "quote": "甲组费率3%",
                                                        "value": "3%"}]}}
    script = [read(0), {**read(0), "args": {**read(0)["args"], "limit": 10}}, note,
              {**read(0), "args": {**read(0)["args"], "limit": 11}}, {**read(0), "args": {**read(0)["args"], "limit": 12}}]
    ar = run_agent(FakeLLM(script), _ws(tmp_path), tmp_path / "out", max_steps=5)
    assert not any(s.tool == "（反思）" for s in ar.steps)
    assert ar.error.startswith("达到最大步数") and retryable(ar.error)      # it wrote something, so it may continue


def test_ledger_listing_drops_what_the_row_already_says():
    """实测 156 条参数的台账简表要 5 页 2.6 万字：页码、表名、取值在每条里重复出现。"""
    ws = _WS([_fact(i) for i in range(40)])
    items = workspace_view(ws, "facts", limit=50)["items"]
    first, second = items[0], items[1]
    assert "page" not in first                               # 坐标名里已有 P1
    assert first["meaning"] == "角色0 | 新项目"              # 末尾的取值和 value 重复
    assert first["context"] == "某事业部 提成分成比例" and "context" not in second   # 同一张表只写一次
    page2 = workspace_view(ws, "facts", offset=1, limit=50)["items"][0]
    assert page2["context"] == "某事业部 提成分成比例"       # 每页第一条仍带表名


def test_mapping_view_drops_empty_settings():
    class _Def:
        def __init__(self, d):
            self.d = d

        def model_dump(self, mode=None):
            return self.d

    ws = _WS([])
    ws.datasets = {"销售": _Def({"key": None, "fields": {"amount": {"type": "money", "unit": None, "values": None}}})}
    ws.mapping = {"销售": _Def({"file": "a.xlsx", "confidence": {}, "confirmed_by": None})}
    item = workspace_view(ws, "mapping")["items"][0]
    assert item == {"dataset": "销售", "definition": {"fields": {"amount": {"type": "money"}}}, "mapping": {"file": "a.xlsx"}}


def test_scattered_duplicate_reads_between_successful_writes_do_not_stop_the_round(tmp_path):
    """实测第 240、241 步各被拦下一次重复读取，中间 6 次定义数据表都成功，第 249 步再重复一次就判成卡住、整轮停下。"""
    script = []
    for i in range(3):
        read = {"tool": "read_workspace", "args": {"section": "facts", "offset": i}}
        script += [read, read, {"tool": "note", "args": {"topic": f"要点{i}", "text": f"第 {i} 条记下了"}}]
    ar = run_agent(FakeLLM(script), _ws(tmp_path), tmp_path / "out", max_steps=len(script))
    assert not any(s.tool == "（反思）" for s in ar.steps), [(s.tool, s.result[:80]) for s in ar.steps]
    assert "反复调用" not in (ar.error or "")


def test_history_budget_comes_from_the_model_config(tmp_path):
    """网页入口不传 max_history_chars：按模型配置（DeepSeek 上下文 1M，配得比默认大）。"""
    p = tmp_path / "policy.md"
    p.write_text("甲组费率3%。" + "说明文字。" * 12000, encoding="utf8")   # 太长，不进材料包，要 read_document
    read = {"tool": "read_document", "args": {"file": "policy.md"}}

    def again(budget):
        llm = FakeLLM([read, read])
        llm.max_history_chars = budget
        ar = run_agent(llm, [p], tmp_path / f"out{budget}", max_steps=2)
        return [s for s in ar.steps if s.tool == "read_document"][-1].result

    assert "完全相同" in again(None)            # 默认上限放得下：原样重读被拦下
    assert "完全相同" not in again(1500)        # 上限很小：前一次的结果已被整理掉，重读是合理的
