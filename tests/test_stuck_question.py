"""卡住时给财务一个具体问题：问什么、选项、怎么回答；程序自身的问题不问财务。"""
import json
from types import SimpleNamespace

import openpyxl

from bonus_agent import stuck_question as sq
from bonus_agent.agent import run_agent
from bonus_agent.failure import to_text
from bonus_agent.llm import FakeLLM


def _sales(tmp_path):
    p = tmp_path / "sales.xlsx"
    wb = openpyxl.Workbook()
    s = wb.active
    s.title = "sales"
    s.append(["客户编码", "客户简称", "金额"])
    for row in [["1001", "甲(海外)", 1], ["1002", "甲(海外)", 2], ["2001", "乙", 3]]:
        s.append(row)
    wb.save(p)
    return p


def test_stuck_on_a_data_conflict_becomes_a_concrete_question(tmp_path):
    p = _sales(tmp_path)
    bad = {"name": "客户", "file": p.name, "sheet": "sales", "key": "short", "distinct": True,
           "fields": {"code": {"column": "客户编码", "type": "id"}, "short": {"column": "客户简称", "type": "text"}}}
    script = [{"tool": "define_dataset", "args": bad} for _ in range(4)] + [{"tool": "note", "args": {"topic": "t", "text": "x"}}]
    out = tmp_path / "out"
    ar = run_agent(FakeLLM(script), [p], out, max_steps=8, reflect=0)
    assert ar.status == "NEEDS_CONFIRMATION" and not ar.error           # 不再是“失败 + 补充材料、明确答复”
    q = SimpleNamespace(**ar.pending[0])
    assert "「客户简称」对应多个不同的「客户编码」" in q.text and "甲(海外)" in q.text
    assert q.options[0].startswith("合并") and q.options[-1].startswith("其他") and q.blocking
    assert any(s.tool == "（向财务提问）" for s in ar.steps)
    from bonus_agent.web import _round_status
    text = _round_status(ar)
    assert q.id in text and "{" not in text and "A. 合并" in text and "怎么回答" in text and "运行下一轮" in text and "补充材料" not in text


def test_model_writes_the_question_for_other_business_stalls():
    llm = FakeLLM([])
    llm.stuck = json.dumps({"program_issue": False, "question": "退货冲销的金额要从当月提成基数里扣掉吗？",
                            "options": ["扣：按负数计入当月", "不扣：退货另行处理"], "why": "规则没写退货"})
    ws = SimpleNamespace(datasets={}, mapping={})
    q = sq.build(ws, llm, "智能体卡住了：set_rules 连续 3 次失败，最后一次：错误：规则里退货怎么处理没有依据", [])
    assert q["text"].startswith("退货冲销") and q["options"][-1] == sq.OTHER
    llm.stuck = json.dumps({"program_issue": False, "question": "update_dataset 的 cust_code 怎么改？", "options": ["a", "b"]})
    assert "材料里这一点是怎么规定的" in sq.build(ws, llm, "智能体卡住了：set_rules 连续 3 次失败，最后一次：错误：某处不通过", [])["text"]   # 带程序术语：改用通用问法


def test_program_errors_do_not_ask_finance(tmp_path):
    assert sq.build(SimpleNamespace(datasets={}, mapping={}), FakeLLM([]),
                    "纠错预算耗尽（智能体卡住）：thought 同一问题累计失败 3 次：错误：没有工具 thought", []) is None
    text = to_text("纠错预算耗尽（智能体卡住），已保留工作区并停止自动接续：thought 同一问题累计失败 3 次：错误：没有工具 thought")
    assert "不需要你回答" in text and "需要补充材料" not in text
