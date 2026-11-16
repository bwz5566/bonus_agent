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


def _tier_ppt(tmp_path):
    """实测的档位表：档位格是“5000万以上 / 1000-5000万 / 200万以下”，表头有两行字。"""
    from pptx import Presentation
    from pptx.util import Inches
    from bonus_agent.auto import build_inventory
    from bonus_agent.agent import Workspace
    path = tmp_path / "tier.pptx"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    t = slide.shapes.add_table(4, 3, Inches(0), Inches(0), Inches(7), Inches(3)).table
    for r, row in enumerate([["销售额阶梯", "新项目", "老客户\n（旧项目）"], ["5000万以上", "0.24%", "0.12%"],
                             ["1000-5000万", "0.23%", "0.115%"], ["1000万以下", "0.20%", "0.10%"]]):
        for c, text in enumerate(row):
            t.cell(r, c).text = text
    slide.shapes.add_textbox(Inches(0), Inches(4), Inches(7), Inches(1)).text_frame.text = "北区 提成阶梯"
    prs.save(path)
    return Workspace(FakeLLM([]), build_inventory([path])), path.name


def test_tier_band_cells_register_and_compile_by_coordinates(tmp_path):
    """实测约 30 步：区间格整批登记失败且不说哪一格 → 改 record_facts → “不存在、非数值或冲突尚未确认”。"""
    ws, f = _tier_ppt(tmp_path)
    cells = [{"row": r, "column": c} for c in (1, 2, 3) for r in (1, 2, 3, 4)]
    out = ws.record_parameter_cells(f, 1, 1, cells, applies_to="北区")
    assert out["status"] == "PARTIAL" and len(out["facts"]) == 9
    assert any("R1C3" in x and "line 1「老客户」" in x for x in out["failed"])
    assert {x["value"] for x in out["facts"] if x["source_cell"]["column"] == 1} == {"5000万", "1000-5000万", "1000万"}
    cf = {"file": f, "page": 1, "table": 1}
    msg = ws.compile_table("old", [{"value": "R4C3"}, {"lower": "R3C1", "value": "R3C3"}, {"lower": "R2C1", "value": "R2C3"}],
                           kind="tier", mode="excess", cells_from=cf)
    assert "10,000,000 至 50,000,000的部分按 0.115%" in msg
    with pytest.raises(ToolError, match="R4C1"):
        ws.compile_table("x", [{"value": "R4C2"}, {"lower": "10000000", "value": "R3C2"}], cells_from=cf)
    with pytest.raises(ToolError, match="没有登记。名字相近"):
        ws.compile_table("y", [{"key": "a", "value": "tier.pptx::P1.T1.R4.C2.L2"}], kind="lookup")
    with pytest.raises(ToolError, match=r"去掉 component，value 写「5000万」"):
        ws.record_facts([{"file": f, "page": 1, "name": "上界", "quote": "5000万以上", "value": "5000万以上", "component": "lower"}])
    with pytest.raises(ToolError, match=r'"row": 2, "column": 3'):
        ws.record_facts([{"file": f, "page": 1, "name": "老5000", "quote": "| 5000万以上 | 0.24% | 0.12% |", "value": "0.12%"}])


def _customer_tables():
    return {"项目": {"key": "project_id", "fields": {"project_id": {"type": "id"}, "cust_short": {"type": "text"}}},
            "客户": {"key": "cust_code", "fields": {"cust_code": {"type": "id"}, "cust_short": {"type": "text"}}},
            "呆滞库存": {"key": "cust_short", "fields": {"cust_short": {"type": "text"}, "stock": {"type": "money"}}},
            "客户预算": {"link": "cust_short", "fields": {"cust_short": {"type": "text"}, "bud": {"type": "money"}}}}


def test_scope_error_names_the_entity_both_tables_can_reach():
    """实测：客户预算按客户简称挂在呆滞库存下，在客户（按编码）层汇总被拦，提示只说“聚合不到”，模型反复改 link。"""
    from bonus_core.dsl.models import RuleSet
    from bonus_core.dsl.validator import validate
    rs = RuleSet.model_validate({"name": "t", "version": "1", "pool": {"expr": "0"}, "allocation": [{"level": "项目", "key": "project_id", "weight": "1"}],
                                 "datasets": _customer_tables(),
                                 "variables": {"rate": {"scope": "客户", "expr": 'sum(bud, of: "客户预算")'}}})
    msg = next(i.message for i in validate(rs) if i.code == "agg_unrelated")
    assert "scope 改成「呆滞库存」" in msg and "不要再改 link" in msg


def test_unchanged_dataset_definition_says_where_the_table_hangs(tmp_path):
    """实测同一个 update_dataset（link=cust_short）交了 7 次，每次只回“no-op：数据定义和映射未变化”，被判卡住。"""
    import openpyxl
    from bonus_agent.auto import build_inventory
    from bonus_agent.agent import Workspace
    p = tmp_path / "ref.xlsx"
    wb = openpyxl.Workbook()
    for title, head, row in (("stock", ["简称", "金额"], ["A", 1]), ("budget", ["简称", "预算"], ["A", 2])):
        s = wb.create_sheet(title)
        s.append(head)
        s.append(row)
    wb.save(p)
    ws = Workspace(FakeLLM([]), build_inventory([p]))
    ws.define_dataset("呆滞库存", p.name, "stock", {"cust_short": {"column": "简称", "type": "text"},
                                                    "stock": {"column": "金额", "type": "money"}}, key="cust_short")
    ws.define_dataset("客户预算", p.name, "budget", {"cust_short": {"column": "简称", "type": "text"},
                                                    "bud": {"column": "预算", "type": "money"}}, link="cust_short")
    out = ws.update_dataset("客户预算", {"link": "cust_short"})
    assert out.startswith("no-op") and "挂在「呆滞库存」下" in out and "scope=「呆滞库存」" in out


def test_identical_resubmission_loop_is_not_turned_into_a_question_for_finance():
    from bonus_agent.failure import explain
    from bonus_agent.stuck_question import program_issue
    err = ("智能体卡住了，已停止以免继续消耗 tokens：反复提交相同的 update_dataset，结果：no-op：数据定义和映射未变化，未触发重新试算。。"
           "已用 65 步，中途停下来反思、换做法 2 次仍没过去。请把这条信息反馈给开发，或在对话里告诉智能体怎么处理")
    assert program_issue(err)
    got = explain(err)
    assert "不需要你回答" in got["title"] and not got["ask"] and "update_dataset" in got["where"]


def test_recompiling_a_table_drops_the_bindings_of_its_previous_version(tmp_path):
    """实测首档 lower 先写档位格、再改成 0：旧绑定留在 steps/0/0 上，编译公式一直报“与原文不一致”。"""
    ws, f = _tier_ppt(tmp_path)
    ws.record_parameter_cells(f, 1, 1, [{"row": r, "column": c} for c in (1, 3) for r in (2, 3, 4)], applies_to="北区")
    cf = {"file": f, "page": 1, "table": 1}
    ws.compile_table("old", [{"lower": "R4C1", "value": "R4C3"}, {"lower": "R3C1", "value": "R3C3"},
                             {"lower": "R2C1", "value": "R2C3"}], kind="tier", mode="excess", cells_from=cf)
    first = next(x for x in ws.facts if x.name.endswith("R4.C1.L1"))
    assert any(t.endswith("/tables/old/steps/0/0") for t in first.targets)
    ws.compile_table("old", [{"value": "R4C3"}, {"lower": "R3C1", "value": "R3C3"}, {"lower": "R2C1", "value": "R2C3"}],
                     kind="tier", mode="excess", cells_from=cf)
    assert not first.targets
    from bonus_agent import facts as fx
    err = next(e for e in fx.binding_errors(ws.facts, ws.drafts) if e.startswith(first.name))
    assert "缺少精确规则位置" in err


def test_unbound_fact_message_points_at_the_rule_positions_using_its_number():
    """实测“查不到预算系数为 1”只报“缺少精确规则位置”，模型不知道能绑到变量的表达式上。"""
    from bonus_agent import facts as fx
    from bonus_agent.facts import Fact
    f = Fact(name="达成率无预算默认系数", value="1", file="场景说明.md", quote="该项系数指定为 1")
    drafts = {"提成奖": {"tables": {}, "variables": {"cust_rate": {"expr": 'ratio(a, b, zero: 1)'}}}}
    err = fx.binding_errors([f], drafts)[0]
    assert "提成奖::/variables/cust_rate/expr" in err


def test_keys_that_differ_only_in_case_count_as_matched_like_the_calculation(tmp_path):
    """计算时 BELVAPE ↔ Belvape 按上级写法对上（同 Excel VLOOKUP）；保存和诊断时却报“对不上”，误导模型去改结构。"""
    import openpyxl
    from bonus_agent.auto import build_inventory
    from bonus_agent.agent import Workspace
    p = tmp_path / "ref.xlsx"
    wb = openpyxl.Workbook()
    for title, head, rows in (("stock", ["简称", "金额"], [["Belvape", 1], ["MURO", 2]]),
                              ("budget", ["简称", "预算"], [["BELVAPE", 5], ["MURO", 6]])):
        s = wb.create_sheet(title)
        s.append(head)
        for r in rows:
            s.append(r)
    wb.save(p)
    ws = Workspace(FakeLLM([]), build_inventory([p]))
    ws.define_dataset("呆滞库存", p.name, "stock", {"cust_short": {"column": "简称", "type": "text"},
                                                    "stock": {"column": "金额", "type": "money"}}, key="cust_short")
    out = ws.define_dataset("客户预算", p.name, "budget", {"cust_short": {"column": "简称", "type": "text"},
                                                          "bud": {"column": "预算", "type": "money"}}, link="cust_short")
    assert "只差大小写" in out and "BELVAPE" in out and "只有" not in out


def test_check_that_keeps_shrinking_is_progress_not_the_same_failure():
    from bonus_agent.agent import _issue_set
    a = _issue_set('{"status": "FAIL", "issues": ["x 缺少精确规则位置", "y 不一致"]}\n注意：……')
    b = _issue_set('{"status": "FAIL", "issues": ["y 不一致"]}')
    assert a == frozenset({"x 缺少精确规则位置", "y 不一致"}) and not b >= a and a >= b
    assert _issue_set("错误：别的") is None


def test_stop_on_own_binding_gaps_does_not_ask_finance_how_the_rule_works():
    from bonus_agent.failure import explain
    err = ('智能体卡住了，已停止以免继续消耗 tokens：compile_formulas 连续 3 次失败，最后一次：{"status": "FAIL", "issues": '
           '["达成率无预算默认系数：缺少精确规则位置，用 bind_facts 绑定参数", "方案「提成奖」有多项汇总但未声明 report.balances 对账关系"]}。'
           '已用 74 步，中途停下来反思、换做法 2 次仍没过去。')
    got = explain(err)
    assert "不需要你回答" in got["title"] and not got["ask"]
