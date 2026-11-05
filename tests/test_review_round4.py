"""代码审核（第四轮）：限制后用很小的 limit 逐段读仍能把全文再读一遍、财务答复里一句“全额冻结”关掉所有阶梯检查、
整张阶梯表漏了百分号（0.50% 写成 0.5）不报、辅助表多出来的编号没有“财务说不参与”的写法、带括号的「名字(50%)」认不出。"""
from decimal import Decimal

import openpyxl
import pytest
import yaml

from bonus_agent import lint
from bonus_agent.agent import REREAD_EXTRA, ToolError, Workspace
from bonus_agent.auto import _NAME_PCT, build_inventory, collect_files
from bonus_agent.llm import FakeLLM
from bonus_core.dsl.models import RuleSet


def test_small_reads_after_the_cap_are_limited_too():
    from test_d2_fixes import _doc_ws
    ws = _doc_ws()
    full = len(ws._render_doc(ws.inv.docs[0]))
    off = 0
    while ws.doc_reads.get("制度.pptx", 0) < 3 * full:
        ws.read_document("制度.pptx", off % full)
        off += 5800
    for i in range(REREAD_EXTRA):
        assert "条款内容" in ws.read_document("制度.pptx", i * 300, limit=300)
    out = ws.read_document("制度.pptx", 0, limit=200)
    assert "不再给原文" in out and "条款内容" not in out
    assert "条款内容" in ws.read_document("制度.pptx", 0, limit=10 ** 9)


def _tier_rs():
    return RuleSet.model_validate({"name": "t", "version": "1", "datasets": {}, "pool": {"expr": "tier(rate, 1000)"},
                                   "allocation": [], "tables": {"rate": {"kind": "tier", "mode": "whole",
                                                                         "steps": [[0, "0.01"], [5000000, "0.02"]]}}})


def test_unrelated_quanE_in_finance_answer_does_not_settle_tier_method():
    text = "提成比例：500 万以下 1%，500 万以上 2%。"
    assert "阶梯方式 rate" in lint.check(_tier_rs(), {}, text, said="欠款客户全额冻结，回款后再发").keys
    assert "阶梯方式 rate" not in lint.check(_tier_rs(), {}, text, said="按超额累进").keys
    assert "阶梯方式 rate" not in lint.check(_tier_rs(), {}, text, said="提成档按全额计算").keys


def test_whole_tier_table_missing_percent_is_gated_even_if_whole_number_elsewhere():
    rs = RuleSet.model_validate({"name": "t", "version": "1", "datasets": {}, "allocation": [],
                                 "pool": {"expr": "1000 * tier_rate(odm, 1000)"},
                                 "tables": {"odm": {"kind": "tier", "mode": "whole",
                                                    "steps": [[0, "0.5"], [1000000, "0.8"]]}}})
    text = "ODM 提成：100 万以下 0.50%，100 万以上 0.80%，超额累进。另：销售负责人分 50%，执行人 80% 封顶。"
    keys = lint.check(rs, {}, text).keys
    assert any(k.startswith("百分号") for k in keys), keys


def test_bracketed_name_percent_is_recognised():
    for v in ("谢娜(50%)", "谢娜（50%）", "谢娜50%", "谢娜 50 %"):
        assert _NAME_PCT.match(v), v
    for v in ("50%", "回款比例达到50%以上", "A2"):
        assert not _NAME_PCT.match(v), v


def _book(p, rows, title):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title
    for r in rows:
        ws.append(r)
    wb.save(p)


def test_unmatched_ignore_needs_finance_answer_and_drops_rows(tmp_path):
    from bonus_agent.extract_rules import Question
    _book(tmp_path / "客户.xlsx", [["客户", "毛利"], ["C1", 100], ["C2", 200]], "客户")
    _book(tmp_path / "库存.xlsx", [["客户", "库存"], ["C1", 5], ["C9", 7]], "Sheet1")
    w = Workspace(FakeLLM(["x"]), build_inventory(collect_files([tmp_path])))
    w.define_dataset("客户", "客户.xlsx", "客户", {"cust": {"column": "客户", "type": "id"},
                                               "gp": {"column": "毛利", "type": "money"}}, key="cust")
    spec = {"cust": {"column": "客户", "type": "id"}, "stock": {"column": "库存", "type": "money"}}
    with pytest.raises(ToolError, match="ask_question"):
        w.define_dataset("库存", "库存.xlsx", "Sheet1", spec, link="cust", unmatched="ignore")
    w.questions = [Question(id="q1", text="Sheet1 里有客户找不到", options=[], answer="不参与")]
    with pytest.raises(ToolError, match="ask_question"):      # 只提到 Sheet1 不算
        w.define_dataset("库存", "库存.xlsx", "Sheet1", spec, link="cust", unmatched="ignore")
    w.questions = [Question(id="q1", text="「库存」里 C9 在客户表找不到，怎么处理？", options=[], answer="找不到的不参与，系数按 1")]
    assert "已保存" in w.define_dataset("库存", "库存.xlsx", "Sheet1", spec, link="cust", unmatched="ignore")
    assert w.datasets["库存"].unmatched == "ignore"

    from bonus_core.data.mapping import apply_mapping
    from bonus_core.engine.run import compute
    rs = RuleSet.model_validate({"name": "t", "version": "1",
                                 "datasets": {n: d.model_dump(mode="json") for n, d in w.datasets.items()},
                                 "variables": {"st": {"scope": "客户", "expr": "sum(stock)"}},
                                 "pool": {"expr": "100"},
                                 "allocation": [{"level": "客户", "key": "cust", "weight": "gp + st"}]})
    data, issues = apply_mapping(rs, sorted(tmp_path.glob("*.xlsx")), w.mapping)
    assert not [i for i in issues if i.level == "block"], issues
    res = compute(rs, data)
    assert any(i.code == "link_ignored" and "C9" in i.message for i in res.issues)
    assert not [i for i in res.issues if i.severity == "blocking"]


def test_unmatched_ignore_rejected_on_entity_table(tmp_path):
    _book(tmp_path / "客户.xlsx", [["客户", "毛利"], ["C1", 100]], "客户")
    w = Workspace(FakeLLM(["x"]), build_inventory(collect_files([tmp_path])))
    with pytest.raises(ToolError, match="只用于"):
        w.define_dataset("客户", "客户.xlsx", "客户", {"cust": {"column": "客户", "type": "id"}}, key="cust",
                         unmatched="ignore")
