"""Review round 5: page scope for coordinate facts, cell references in compile_table, partial fact batches,
one-shot rejection feedback and a single chat entry for review fixes."""
from decimal import Decimal

import pytest

from bonus_agent.agent import Step, ToolError, _deep_reason, run_agent
from bonus_agent.llm import FakeLLM
from tests.test_t14_parameter_cells import workspace

FILE = "policy.pptx"


def test_coordinate_registration_rejects_a_scope_that_is_not_on_the_chosen_page(tmp_path):
    ws = workspace(tmp_path)          # pages 1..3 are captioned 北区 / 西区 / 东区 with identical tables
    with pytest.raises(ToolError, match="而在第 2 页"):
        ws.record_parameter_cells(FILE, 3, 1, [{"row": 2, "column": 2}], applies_to="西区")
    with pytest.raises(ToolError, match="通用词"):
        ws.record_parameter_cells(FILE, 2, 1, [{"row": 2, "column": 2}], applies_to="提成比例")
    with pytest.raises(ToolError, match="applies_to"):
        ws.record_parameter_cells(FILE, 2, 1, [{"row": 2, "column": 2}])
    assert not ws.facts
    ws.record_parameter_cells(FILE, 2, 1, [{"row": 2, "column": 2}], applies_to="西区")
    assert ws.facts[0].context == "西区" and ws.facts[0].value == "0.50%"


def test_compile_table_resolves_cell_coordinates_without_reading_the_ledger(tmp_path):
    ws = workspace(tmp_path)
    ws.record_parameter_cells(FILE, 2, 1, [{"row": 2, "column": 2}, {"row": 2, "column": 3}], applies_to="西区")
    where = {"file": FILE, "page": 2, "table": 1}
    ws.compile_table("R", [{"key": "新", "value": "R2C2"}, {"key": "老", "value": "r2c3"}], scheme="S",
                     kind="lookup", cells_from=where)
    table = ws.drafts["S"]["tables"]["R"]
    assert {k: Decimal(v) for k, v in table["mapping"].items()} == {"新": Decimal("0.005"), "老": Decimal("0.001")}
    assert all(f.targets for f in ws.facts)
    with pytest.raises(ToolError, match="还没有登记.*record_parameter_cells"):
        ws.compile_table("T", [{"key": "x", "value": "R3C3"}], scheme="S", kind="lookup", cells_from=where)
    with pytest.raises(ToolError, match="cells_from"):
        ws.compile_table("T", [{"key": "x", "value": "R2C2"}], scheme="S", kind="lookup", cells_from={"file": FILE})


def _batch(i):
    ok = {"name": f"ok{i}", "file": "policy.md", "quote": "甲组费率3%", "value": "3%"}
    bad = {"name": f"bad{i}", "file": "policy.md", "quote": "甲组费率3%", "value": "7%"}
    return {"tool": "record_facts", "args": {"facts": [ok, bad]}}


def test_partially_accepted_batches_are_progress_not_stuck(tmp_path):
    p = tmp_path / "policy.md"
    p.write_text("甲组费率3%", encoding="utf8")
    script = [_batch(i) for i in range(4)] + [{"tool": "note", "args": {"topic": "t", "text": "x"}}]
    ar = run_agent(FakeLLM(script), [p], tmp_path / "out", max_steps=5)
    steps = [s for s in ar.steps if s.tool == "record_facts"]
    assert len(steps) == 4 and not any(s.ok for s in steps)               # still reported as not fully accepted
    assert not any(s.tool in ("（反思）", "（局部诊断）") or s.deep for s in ar.steps)
    assert "卡住" not in (ar.error or "")
    assert ar.ws._fact_rejections == []                                    # shown once, not re-sent every step
    assert _deep_reason([steps[0]], 0, ar.ws, {}) == ""


def test_review_fix_is_recorded_once_in_chat(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from bonus_agent import web
    from tests.test_web import post
    app = web.create_app(tmp_path / "data", lambda: FakeLLM([]))
    store = app.state.store
    store.users.add("alice", "password-123", ["editor"], "张会计")
    from fastapi.testclient import TestClient
    c = TestClient(app)
    c.sids = {}
    cid = post(c, store, "/cases", {"name": "口径单"}).headers["location"].split("/")[-1].split("?")[0]
    (store.dir(cid) / "materials" / "a.md").write_text("规则", encoding="utf8")
    monkeypatch.setattr(web.rv, "instruction", lambda out: "改正：甲组费率应为 3%")
    monkeypatch.setattr(web.rv, "mark_sent", lambda out: None)
    app.state.runner._run = lambda *a, **k: None
    assert post(c, store, f"/cases/{cid}/run").status_code == 303
    app.state.runner.jobs[cid].thread.join(5)
    said = [m for m in store.chat(cid) if m["role"] == "user" and "改正" in m["text"]]
    assert len(said) == 1


def test_same_cell_with_a_different_quote_is_not_registered_twice(tmp_path):
    ws = workspace(tmp_path)
    fact = {"name": "西区首档", "file": FILE, "page": 2, "context": "西区", "value": "0.50%",
            "source_cell": {"table": 1, "row": 2, "column": 2}}
    ws.record_facts([{**fact, "quote": "首档\n新项目\n0.50%"}])
    with pytest.raises(ToolError, match="之前已登记过"):
        ws.record_facts([{**fact, "quote": "0.50%", "source_cell": {"table": 1, "row": 2, "column": 2, "line": 1}}])
    assert len(ws.facts) == 1


def test_update_dataset_can_drop_a_field_as_the_distinct_error_advises(tmp_path):
    """实测卡住：按客户简称合并客户表时客户编码不唯一，报错让去掉客户编码，但局部修改删不掉字段，只能原样重试到判为卡住。"""
    import openpyxl
    from bonus_agent.auto import build_inventory
    from bonus_agent.agent import Workspace
    p = tmp_path / "sales.xlsx"
    wb = openpyxl.Workbook()
    s = wb.active
    s.title = "sales"
    s.append(["code", "short", "amount"])
    for row in [["1001", "甲(海外)", 1], ["1002", "甲(海外)", 2], ["2001", "乙", 3]]:
        s.append(row)
    wb.save(p)
    ws = Workspace(FakeLLM([]), build_inventory([p]))
    ws.define_dataset("客户", p.name, "sales", {"code": {"column": "code", "type": "id"},
                                                "short": {"column": "short", "type": "text"}}, key="code", distinct=True)
    with pytest.raises(ToolError, match=r'"code": null'):        # 建议里给出能照做的写法
        ws.update_dataset("客户", {"key": "short"})
    with pytest.raises(ToolError, match="key/link/period"):
        ws.update_dataset("客户", {"fields": {"code": None}})     # 还是 key 时不能删
    ws.update_dataset("客户", {"key": "short", "fields": {"code": None}})
    assert ws.datasets["客户"].key == "short" and set(ws.datasets["客户"].fields) == {"short"}
    with pytest.raises(ToolError, match="没有字段"):
        ws.update_dataset("客户", {"fields": {"code": None}})


def test_misnamed_tool_runs_the_only_matching_read_tool_and_is_not_kept_across_rounds(tmp_path):
    """实测：反思清掉历史后模型把参数名 thought 当成工具名，连续 3 次被记为同一问题，之后每轮一开始就被拦停。"""
    import json as _json
    from bonus_agent.agent import TOOLS, _guess_tool
    handlers = {t["name"]: None for t in TOOLS}
    hit = _guess_tool("thought", _json.dumps({"file": "a.xlsx", "sheet": "Sheet1", "rows": 35}), handlers)
    assert hit and hit[0] == "read_sheet" and "thought 不是工具" in hit[1]
    assert _guess_tool("thought", "{}", handlers) is None                     # 没有参数：猜不出
    assert _guess_tool("read_sheet", '{"file": "a"}', handlers) is None        # 名字本来就对
    assert _guess_tool("thought", '{"name": "x", "changes": {}}', handlers) is None   # 会写入的工具不猜

    p = tmp_path / "policy.md"
    p.write_text("甲组费率3%", encoding="utf8")
    script = [{"tool": "thought", "args": {}} for _ in range(4)] + [{"tool": "note", "args": {"topic": "t", "text": "x"}}]
    out = tmp_path / "out"
    ar = run_agent(FakeLLM(script), [p], out, max_steps=6, reflect=0)
    bad = [s for s in ar.steps if s.tool == "thought"]
    assert bad and "thought 不是工具" in bad[0].result
    path = out / "recovery.json"          # 没有记下任何失败时不会生成
    journal = _json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"failures": {}}
    assert not journal.get("halted") and not any(v["tool"] == "thought" for v in journal["failures"].values())


def test_unnamed_rule_writes_go_to_the_planned_scheme_and_empty_extra_schemes_are_dropped(tmp_path):
    """实测：compile_table 省略 scheme 时建了计划外的“默认”方案，编译公式时“已声明方案与计划不一致”连续被拦。"""
    ws = workspace(tmp_path)
    ws.workflow.update(enabled=True, plan={"schemes": ["九月提成"]})
    ws.record_parameter_cells(FILE, 2, 1, [{"row": 2, "column": 2}], applies_to="西区")
    ws.compile_table("R", [{"key": "新", "value": "R2C2"}], kind="lookup", cells_from={"file": FILE, "page": 2, "table": 1})
    assert ws.schemes == ["九月提成"] and "R" in ws.drafts["九月提成"]["tables"]
    with pytest.raises(ToolError, match="不在计划里"):
        ws.set_rules(scheme="另一个", tables={"X": {"kind": "lookup", "mapping": {"a": "1"}}})
    ws.drafts["默认"] = {"tables": {"R": ws.drafts["九月提成"]["tables"]["R"]}, "variables": {}}   # 旧案卷里已经多出来的
    assert "已去掉计划外的空方案" in ws.set_rules(scheme="默认", tables={"R": None})
    assert ws.schemes == ["九月提成"]


def test_update_dataset_can_set_unmatched_but_still_needs_material_basis(tmp_path):
    """实测：模型 5 次用局部修改设 unmatched: ignore 都被拒（局部修改不支持），只能原地打转。"""
    import openpyxl
    from bonus_agent.auto import build_inventory
    from bonus_agent.agent import Workspace
    p = tmp_path / "ref.xlsx"
    wb = openpyxl.Workbook()
    s = wb.active
    s.title = "ref"
    s.append(["code", "amount"])
    s.append(["A", 1])
    wb.save(p)
    ws = Workspace(FakeLLM([]), build_inventory([p]))
    ws.define_dataset("参考", p.name, "ref", {"code": {"column": "code", "type": "id"},
                                              "amount": {"column": "amount", "type": "number"}}, link="code")
    with pytest.raises(ToolError) as e:
        ws.update_dataset("参考", {"unmatched": "ignore"})
    assert "dataset.patch" not in str(e.value) and "unmatched_basis" in str(e.value)   # 进到依据核对，不是“不允许修改”
