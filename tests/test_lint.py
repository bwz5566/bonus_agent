"""程序检查：不靠提示词，按规则结构和真实数据找出写错，必要时强制提问。"""
import openpyxl
import pytest

from bonus_agent.agent import ToolError, Workspace
from bonus_agent.auto import build_inventory
from bonus_agent.llm import FakeLLM


def case(d, rule="每人奖金 = 有效工单数 × 12.5，扣减后低于 0 的按 0 计。工单状态为已关闭、处理中或已作废。", status=("已关闭",) * 3):
    d.mkdir()
    (d / "01_制度.md").write_text(rule, encoding="utf-8")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "员工"
    ws.append(["工号"])
    for e in ("E1", "E2", "E3"):
        ws.append([e])
    t = wb.create_sheet("工单")
    t.append(["工单号", "工号", "复核", "状态"])
    for i, (e, ok) in enumerate([("E1", "有效"), ("E1", "无效"), ("E2", "有效"), ("E3", "有效")]):
        t.append([f"T{i}", e, ok, status[i % len(status)]])
    wb.save(d / "02_数据.xlsx")
    w = Workspace(FakeLLM([]), build_inventory(sorted(d.iterdir())))
    f = lambda c, t: {"column": c, "type": t}   # noqa: E731
    w.define_dataset("员工", "02_数据.xlsx", "员工", {"emp_id": f("工号", "id")}, header_rows=[1], key="emp_id")
    w.define_dataset("工单", "02_数据.xlsx", "工单", {"ticket_id": f("工单号", "id"), "emp_id": f("工号", "id"),
                                                    "review": f("复核", "text"), "status": f("状态", "text")},
                     header_rows=[1], link="emp_id")
    return w


def rules(w, n_expr, extra=None):
    w.set_rules(scheme="月度", variables={"n": {"scope": "个人", "expr": n_expr}, **(extra or {})},
                pool={"expr": "round(max(n * 12.5, 0), 2)", "scope": "个人"}, allocation=[{"level": "个人", "key": "emp_id"}])
    return w.trial_run()


def gates(w):
    return w._open_gates(w.lint["月度"])


def test_clean_rules_pass(tmp_path):
    w = case(tmp_path / "m")
    out = rules(w, 'count(of: "工单", where: review == "有效")')
    assert "程序检查发现" not in out and not gates(w)


def test_constant_without_source_must_be_asked(tmp_path):
    w = case(tmp_path / "m")
    w.set_rules(scheme="月度", variables={"n": {"scope": "个人", "expr": 'count(of: "工单", where: review == "有效") * 1.15'}},
                pool={"expr": "round(n * 12.5, 2)", "scope": "个人"}, allocation=[{"level": "个人", "key": "emp_id"}])
    out = w.trial_run()
    assert "常数 1.15（variables.n）在材料里找不到出处" in out
    w.record_facts([{"name": "每单金额", "value": "12.5", "file": "01_制度.md", "quote": "有效工单数 × 12.5"}])
    w.record_clauses([{"point": "按单计奖", "file": "01_制度.md", "quote": "每人奖金 = 有效工单数 × 12.5"}])
    w.cover_clauses({"C1": "pool"})
    with pytest.raises(ToolError, match="常数 1.15"):
        w.finish("x")
    w.ask_question("材料没写 1.15 的系数，是否按 1.15 计算？", ["是", "否"])   # 问过财务就不再拦（等答复）
    assert not gates(w)


def test_value_not_in_data_is_blocked(tmp_path):
    w = case(tmp_path / "m")
    out = rules(w, 'count(of: "工单", where: review == "通过")')
    assert "「review」写的取值「通过」" in out and "有效、无效" in out
    assert "变量 n 带筛选条件，在全部 3 个对象上都是 0" in out


def test_unmentioned_value_with_exclusion_must_be_asked(tmp_path):
    w = case(tmp_path / "m", status=("已关闭", "挂起"))
    out = rules(w, 'count(of: "工单", where: status != "已作废" and status != "处理中")')
    assert "「status」有取值「挂起」" in out and "算进去" in out
    w.ignored = [{"ref": "取值 挂起", "reason": "财务口头确认挂起算有效"}]
    assert not gates(w)


def test_unmentioned_value_with_inclusion_is_fine(tmp_path):
    w = case(tmp_path / "m", status=("已关闭", "挂起"))
    out = rules(w, 'count(of: "工单", where: status == "已关闭")')
    assert "挂起" not in out and not gates(w)


def test_structure_hints(tmp_path):
    w = case(tmp_path / "m")
    out = rules(w, 'count(of: "工单", where: review == "有效")', {"spare": {"scope": "个人", "expr": "n * 2"}})
    assert "变量 spare 定义了，但" in out
