"""补充材料里有了答复：智能体引用原文关闭之前的问题（程序核对原文），口径单上请财务确认。"""
from types import SimpleNamespace as NS

import pytest

from bonus_agent import review as rv
from bonus_agent.agent import ToolError, Workspace
from bonus_agent.auto import build_inventory
from bonus_agent.llm import FakeLLM
from tests.test_conflicts import _doc, _materials


def test_close_question_by_quoting_material(tmp_path):
    m = _materials(tmp_path / "m")
    _doc(m / "财务答复.docx", "财务答复", "跨期抵扣：年度奖只抵扣本年度已发的预付款。")
    ws = Workspace(FakeLLM([]), build_inventory(sorted(m.iterdir())))
    ws.ask_question("年度奖抵扣哪些已发金额？", ["待财务确认"], blocking=True)
    q = ws.questions[0]
    assert ws.pending == [q]
    with pytest.raises(ToolError, match="不能替财务回答"):              # 不在对话时，没有原文不能关闭
        ws.answer_question(q.id, "只抵扣预付")
    with pytest.raises(ToolError, match="引用核对没通过"):              # 原文要真的在材料里
        ws.answer_question(q.id, "只抵扣预付", file="财务答复.docx", quote="年度奖抵扣全部已发金额")
    assert ws.pending == [q]
    ws.answer_question(q.id, "只抵扣预付", file="财务答复.docx", quote="年度奖只抵扣本年度已发的预付款")
    assert ws.pending == [] and q.answered_by == "材料《财务答复.docx》" and "依据《财务答复.docx》" in q.answer
    rows = rv.build(NS(mapping={}, datasets={}, drafts={}, clauses=[], lint={}, ignored=[], questions=ws.questions))
    assert rows[0]["flag"].startswith("智能体按材料原文关闭了这个问题")
