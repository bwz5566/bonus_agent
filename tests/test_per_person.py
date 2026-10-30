"""每人按公式直接算金额（没有奖金池）：先写共用变量、后写分配层级时不误报；pool 按最末层计算，金额与公式逐分一致。"""
import openpyxl
import pytest

from bonus_agent.agent import run_agent
from bonus_agent.clauses import check_where, refs
from bonus_agent.llm import FakeLLM


def make(d):
    d.mkdir()
    (d / "01_制度.md").write_text("每人奖金 = 保障奖 + 有效工单数 × 12.5 − 差错扣款，扣减后低于 0 的按 0 计。", encoding="utf-8")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "员工"
    ws.append(["工号", "保障奖", "差错扣款"])
    for r in (["E1", 300, 20], ["E2", 100, 400], ["E3", 0, 0]):
        ws.append(r)
    t = wb.create_sheet("工单")
    t.append(["工单号", "工号", "复核"])
    for i, (e, ok) in enumerate([("E1", "有效"), ("E1", "有效"), ("E1", "无效"), ("E2", "有效"), ("E3", "有效")]):
        t.append([f"T{i}", e, ok])
    wb.save(d / "02_数据.xlsx")
    return d


def script():
    f = lambda c, t: {"column": c, "type": t}   # noqa: E731
    return [
        {"tool": "record_facts", "args": {"facts": [{"name": "每单金额", "value": "12.5", "file": "01_制度.md",
                                                     "quote": "有效工单数 × 12.5"}]}},
        {"tool": "record_clauses", "args": {"clauses": [{"point": "每人独立按公式计算，扣减后归零", "file": "01_制度.md",
                                                          "quote": "扣减后低于 0 的按 0 计。"}]}},
        {"tool": "define_dataset", "args": {"name": "员工", "file": "02_数据.xlsx", "sheet": "员工", "header_rows": [1], "key": "emp_id",
                                            "fields": {"emp_id": f("工号", "id"), "base": f("保障奖", "money"), "ded": f("差错扣款", "money")}}},
        {"tool": "define_dataset", "args": {"name": "工单", "file": "02_数据.xlsx", "sheet": "工单", "header_rows": [1], "link": "emp_id",
                                            "fields": {"ticket_id": f("工单号", "id"), "emp_id": f("工号", "id"), "review": f("复核", "text")}}},
        # 先写共用变量，层级「个人」还没定义：不能当成阻断错误反复报
        {"tool": "set_rules", "args": {"scheme": "共用", "variables": {
            "n": {"scope": "个人", "expr": 'count(of: "工单", where: review == "有效")'}}}},
        {"tool": "set_rules", "args": {"scheme": "共用", "variables": {
            "amount": {"scope": "个人", "expr": "round(max(base + n * 12.5 - ded, 0), 2)"}}}},
        {"tool": "set_rules", "args": {"scheme": "月度", "pool": {"expr": "amount", "scope": "个人"},
                                       "allocation": [{"level": "个人", "key": "emp_id"}]}},
        {"tool": "cover_clauses", "args": {"items": {"C1": "variables.amount（含归零）、pool"}}},
        {"tool": "trial_run"},
        {"tool": "finish", "args": {"summary": "每人独立计算"}}]


def test_per_person_amounts(tmp_path):
    ar = run_agent(FakeLLM(script()), [make(tmp_path / "m")], tmp_path / "out")
    sets = [s.result for s in ar.steps if s.tool == "set_rules"]
    assert "[阻断]" not in sets[0] and "还没有分配层级" in sets[0] and "个人" in sets[0]
    assert ar.status == "OK", (ar.error, [(s.tool, s.result[:300]) for s in ar.steps if not s.ok])
    res = ar.results["月度"]["R0"]
    got = {k: n.final for k, n in res.nodes.items() if k.startswith("个人:")}
    assert got == {"个人:E1": "305.00", "个人:E2": "0.00", "个人:E3": "12.50"}


def test_where_ignores_notes_in_brackets():
    assert refs("tables.error_deduction（允许值外报错）、pool(仅求和)") == ["tables.error_deduction", "pool"]
    assert check_where("pool（仅求和、无 payout/offsets）", {"pool"}, set()) is None


@pytest.mark.verify
def test_per_person_end_to_end_with_independent_verification(tmp_path):
    """完整流程：finish 先被拦下要求验算；按材料原文从字段独立重算 E1（最高）、E3（最低非零）、E2（为 0）后才出结果。"""
    v = 'round(max(base + count(of: "工单", where: review == "有效") * 12.5 - ded, 0), 2)'
    basis = "每人奖金 = 保障奖 + 有效工单数 × 12.5 − 差错扣款，扣减后低于 0 的按 0 计"
    steps = script()
    fin = steps.pop()
    steps += [fin] + [{"tool": "verify", "args": {"thought": f"按原文独立重算 {n}", "node": n, "expr": v, "basis": basis}}
                      for n in ("个人:E1", "个人:E3", "个人:E2")] + [fin]
    ar = run_agent(FakeLLM(steps), [make(tmp_path / "m")], tmp_path / "out")
    assert ar.status == "OK", (ar.error, [(s.tool, s.result[:300]) for s in ar.steps if not s.ok])
    first = next(s for s in ar.steps if s.tool == "finish")
    assert not first.ok and "个人:E1（应得金额最高" in first.result and "个人:E2（应得金额为 0" in first.result
    assert [s.ok for s in ar.steps if s.tool == "verify"] == [True, True, True]
    assert ar.ws.verified["月度|个人:E3"]["basis"] == basis


@pytest.mark.verify
def test_failed_round_writes_no_result_files(tmp_path):
    """没做完的轮次（这里是没做独立验算、finish 连续被拦）不出 result*.xlsx，免得被当成结果用。"""
    ar = run_agent(FakeLLM(script() + [script()[-1]] * 3), [make(tmp_path / "m")], tmp_path / "out")
    assert ar.status == "FAILED" and "独立验算" in ar.error
    assert not list((tmp_path / "out").glob("result*.xlsx")) and (tmp_path / "out" / "agent_log.json").exists()
