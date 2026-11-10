"""T3（提成 9 月）之后补的通用检查：提问要附依据原文、对话里更正的答复要落实、结束前清理没用上的表和变量、
材料里“某类行不参与”的条款要在数据里落实、式子解析不了时不保存并指出是哪一条。数据都是造的。"""
from decimal import Decimal

import openpyxl
import pytest

from bonus_agent import lint
from bonus_agent.agent import ToolError, Workspace
from bonus_agent.auto import build_inventory, collect_files
from bonus_agent.llm import FakeLLM
from bonus_core.data.mapping import row_filter
from bonus_core.dsl.models import RuleSet


def _ws(tmp_path, rule="提成按销售额的 10% 计提。"):
    p = tmp_path / "数据.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "销售"
    ws.append(["行", "客户", "金额", "判断"])
    for r in ([1, "C1", 100, "正常"], [2, "C1", 0, "剔除"], [3, "C2", 70, "正常"], [4, "C3", 0.0, "正常"]):
        ws.append(r)
    wb.save(p)
    (tmp_path / "规则.txt").write_text(rule, encoding="utf-8")
    return Workspace(FakeLLM(["x"]), build_inventory(collect_files([tmp_path])))


def _sales(w, **kw):
    return w.define_dataset("销售", "数据.xlsx", "销售", {"rid": {"column": "行", "type": "id"},
                                                     "cust": {"column": "客户", "type": "text"},
                                                     "amt": {"column": "金额", "type": "money", "unit": "元"}},
                            key="rid", **kw)


def test_suggestion_needs_evidence_and_quotes_are_checked(tmp_path):
    w = _ws(tmp_path)
    with pytest.raises(ToolError, match="evidence"):
        w.ask_question("提成比例按多少？", ["10%", "8%"], 0)
    out = w.ask_question("提成比例按多少？", ["10%", "8%"], 0,
                         evidence=[{"file": "规则.txt", "quote": "提成按销售额的 10% 计提"},
                                   {"file": "规则.txt", "quote": "提成按毛利的 8% 计提"}])
    assert "1 条在原文里找不到" in out
    ev = w.questions[-1].evidence
    assert [e["ok"] for e in ev] == [True, False] and ev[0]["file"] == "规则.txt"
    w.ask_question("不带建议的问题可以不写依据？", ["是", "否"])        # 没有 suggested 不强制


def test_answer_page_shows_evidence(tmp_path):
    import yaml
    from bonus_agent import web
    out = tmp_path / "out"
    out.mkdir()
    (out / "questions.yaml").write_text(yaml.safe_dump([{"id": "q1", "text": "按哪个？", "options": ["a", "b"], "suggested": 0,
                                                         "evidence": [{"file": "规则.txt", "quote": "原文一句", "ok": False}]}],
                                                       allow_unicode=True), encoding="utf-8")
    assert web.load_questions(out)[0]["evidence"][0]["quote"] == "原文一句"


def test_chat_correction_must_change_rules(tmp_path):
    w = _ws(tmp_path)
    _sales(w)
    w.set_rules(pool={"expr": "sum(amt) * 0.1"}, allocation=[{"level": "行", "key": "rid", "weight": "amt"}])
    w.trial_run()
    w.ask_question("提成比例？", ["10%", "8%"])
    w.chat_by = "财务"
    w.answer_question("q1", "10%")
    w.answer_question("q1", "更正：按 8%")
    with pytest.raises(ToolError, match="(?s)更正.*q1"):
        w.reply("已按 8% 改好")
    w.set_rules(pool={"expr": "sum(amt) * 0.08"})
    assert w.reply("已按 8% 改好") == "已回复"


def test_correction_without_rule_change_can_be_explained(tmp_path):
    w = _ws(tmp_path)
    _sales(w)
    w.set_rules(pool={"expr": "sum(amt) * 0.1"}, allocation=[{"level": "行", "key": "rid", "weight": "amt"}])
    w.trial_run()
    w.ask_question("提成比例？", ["10%", "8%"])
    w.chat_by = "财务"
    w.answer_question("q1", "10%")
    w.answer_question("q1", "10%，按销售额")
    assert w.reply("口径没变", ignored=[{"ref": "更正 q1", "reason": "只是补充说明"}]) == "已回复"


def test_idle_datasets_and_variables():
    rs = RuleSet.model_validate({
        "name": "t", "version": "1",
        "datasets": {"销售": {"key": "rid", "fields": {"rid": {"type": "id"}, "amt": {"type": "money"}}},
                     "临时": {"link": "rid", "fields": {"rid": {"type": "id"}, "x": {"type": "money"}}}},
        "variables": {"a": {"scope": "行", "expr": "sum(amt)"}, "old": {"scope": "行", "expr": "sum(x)"},
                      "shown": {"scope": "行", "expr": "a * 2"}},
        "pool": {"expr": "sum(amt)"}, "allocation": [{"level": "行", "key": "rid", "weight": "a"}],
        "report": {"totals": [{"name": "展示", "level": "行", "expr": "shown"}]}})
    ds, vs = lint.idle_parts(rs)
    assert ds == ["临时"] and vs == ["old"]


def test_filter_can_exclude_values_and_zero_numbers():
    keep = row_filter(["金额", "判断"], {"金额": {"not": ["0"]}, "判断": {"not": ["剔除"]}})
    assert keep([100, "正常"]) and not keep([0.0, "正常"]) and not keep(["0.00", "正常"]) and not keep([5, "剔除"])
    assert isinstance(row_filter(["金额"], {"金额": {"bad": ["0"]}}), str)


def test_exclusion_clause_in_material_must_be_applied(tmp_path):
    w = _ws(tmp_path, "提成按销售额的 10% 计提。销售金额为 0 的行（包括判断为“剔除”的行）不参与核算。")
    _sales(w)
    w.set_rules(pool={"expr": "sum(amt) * 0.1"}, allocation=[{"level": "行", "key": "rid", "weight": "amt"}])
    out = w.trial_run()
    assert "排除条款 销售.金额" in out and "排除条款 销售.判断" in out
    _sales(w, filter={"金额": {"not": ["0"]}})
    out = w.trial_run()
    assert "排除条款" not in out
    assert len(w.results[w.schemes[0]]["R0"].nodes) == 3      # 全部 + 两行


def test_broken_expression_is_not_saved_and_named(tmp_path):
    w = _ws(tmp_path)
    _sales(w)
    with pytest.raises(ToolError, match=r"variables\.big.*少了右括号"):
        w.set_rules(pool={"expr": "sum(amt) * 0.1"}, allocation=[{"level": "行", "key": "rid", "weight": "amt"}],
                    variables={"big": {"scope": "行", "expr": "max(min(amt, round(amt * (1 + 2), 2), 0"}})
    assert not w.drafts
