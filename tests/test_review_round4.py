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


# ---- D4：财务的答复没落实到规则里，检查没拦住 ----
def _rs_yaml(text):
    return RuleSet.model_validate(yaml.safe_load(text))


_FREEZE = '''
name: t
version: v1
datasets:
  项目: {key: pid, fields: {pid: {type: id}, amt: {type: money}}}
  冻结: {link: pid, fields: {pid: {type: id}, amount: {type: money}}}
pool: {scope: 项目, expr: "%s"}
allocation:
  - {level: 项目, key: pid, weight: "1"}
variables:
  frozen: {scope: 项目, expr: 'sum(amount, of: "冻结")'}
  payable: {scope: 项目, expr: "amt - frozen"}
'''


def test_variable_that_reads_data_but_is_not_wired_into_the_pool_is_a_gate():
    rs = _rs_yaml(_FREEZE % "amt * 0.1")           # payable / frozen 都没接进奖金池
    rep = lint.check(rs, {}, "")
    assert "未用 frozen" in rep.keys and "未用 payable" in rep.keys
    assert any("没有接进" in g for g in rep.gates)
    ok = lint.check(_rs_yaml(_FREEZE % "payable * 0.1"), {}, "")
    assert not [k for k in ok.keys if k.startswith("未用")], ok.keys


def test_money_field_read_only_by_an_unwired_variable_counts_as_unused():
    rs = _rs_yaml(_FREEZE % "amt * 0.1")
    assert ("冻结", "amount") in lint._unused_money(rs, set())
    ok = _rs_yaml(_FREEZE % "payable * 0.1")
    assert ("冻结", "amount") not in lint._unused_money(ok, set())


def test_negative_amounts_fall_below_a_zero_threshold_excess_tier():
    from decimal import Decimal as D
    rs = RuleSet.model_validate({"name": "t", "version": "1", "datasets": {}, "allocation": [], "pool": {"expr": "tier(r, 1)"},
                                 "tables": {"r": {"kind": "tier", "mode": "excess", "steps": [[0, "0.003"], [1000000, "0.0033"]]}}})
    st = {"r": {"n": 10, "min": D("-4163.73"), "max": D("900000"), "bands": {-1: 2, 0: 8}}}
    assert lint.tier_holes(rs, st) and any("第一档从" in g for g in lint.check(rs, {}, "", tiers=st).gates)
    pos = RuleSet.model_validate({"name": "t", "version": "1", "datasets": {}, "allocation": [], "pool": {"expr": "tier(r, 1)"},
                                  "tables": {"r": {"kind": "tier", "mode": "excess", "steps": [[100, "0.1"], [200, "0.2"]]}}})
    assert not lint.tier_holes(pos, {"r": {"n": 5, "min": D("50"), "max": D("300"), "bands": {-1: 2, 0: 1, 1: 2}}})


def test_unpivot_splits_name_and_share_and_records_the_remainder(tmp_path):
    from bonus_core.data.table_detect import detect, split_name_share, unpivot_by
    assert split_name_share("张三50%") == ("张三", Decimal("0.5"))
    assert split_name_share("张三（30.5%）") == ("张三", Decimal("0.305"))
    assert split_name_share("50%")[1] is None and split_name_share("B2")[1] is None and split_name_share("李四")[1] is None
    p = tmp_path / "名单.xlsx"
    _book(p, [["项目", "负责人", "执行人"], ["P1", "甲", "乙50%"], ["P2", "甲", None]], "名单")
    st = next(s for s in detect(p) if s.sheet == "名单")
    out = unpivot_by(st, {"columns": ["负责人", "执行人"], "value": "人员", "label": "角色", "share": "比例",
                          "remainder": "{label}未分配"})
    assert out.headers == ["项目", "人员", "角色", "比例"]
    got = [tuple(v) for _, v in out.rows]
    assert ("P1", "乙", "执行人", Decimal("0.5")) in got and ("P1", "执行人未分配", "执行人", Decimal("0.5")) in got
    assert ("P1", "甲", "负责人", Decimal(1)) in got and len(got) == 4
    plain = unpivot_by(st, {"columns": ["负责人", "执行人"], "value": "人员", "label": "角色", "share": "比例"})
    assert ("P1", "乙", "执行人", Decimal("0.5")) in [tuple(v) for _, v in plain.rows] and len(plain.rows) == 3


def test_define_dataset_unpivot_share_end_to_end(tmp_path):
    p = tmp_path / "名单.xlsx"
    _book(p, [["项目", "负责人", "执行人"], ["P1", "甲", "乙50%"], ["P2", "甲", None]], "名单")
    w = Workspace(FakeLLM(["x"]), build_inventory(collect_files([tmp_path])))
    out = w.define_dataset("参与", "名单.xlsx", "名单", {"part": {"column": "（行号）", "type": "id"},
                                                    "pid": {"column": "项目", "type": "id"},
                                                    "emp": {"column": "人员", "type": "text"},
                                                    "share": {"column": "比例", "type": "ratio"}},
                           key="part", unpivot={"columns": ["负责人", "执行人"], "value": "人员", "label": "角色",
                                                "share": "比例", "remainder": "{label}未分配"})
    assert "已保存" in out and "4 行" in out
    assert sorted(w._values("参与", "emp")) == ["乙", "执行人未分配", "甲", "甲"]
    with pytest.raises(ToolError, match="remainder"):
        w.define_dataset("参与", "名单.xlsx", "名单", {"emp": {"column": "人员", "type": "text"}}, key="emp",
                         unpivot={"columns": ["负责人", "执行人"], "value": "人员", "remainder": "x"})


def test_name_percent_gate_stays_until_data_is_really_split():
    from bonus_core.data.table import Row, StdTable
    rs = RuleSet.model_validate({"name": "t", "version": "1", "pool": {"expr": "1"}, "allocation": [],
                                 "datasets": {"名单": {"key": "emp", "fields": {"emp": {"type": "id"}}}}})
    data = {"名单": StdTable("名单", [Row({"emp": e}) for e in ("甲", "乙", "丙50%")])}
    from bonus_agent.extract_rules import Question
    w = Workspace(FakeLLM(["x"]), build_inventory([]))
    w.questions = [Question(id="q1", text="丙50%怎么拆？", options=[], answer="拆成丙，另一半未分配")]
    rep = lint.check(rs, data, "")
    assert w._open_gates(rep) and any("名称百分比" in g for g in w._open_gates(rep))
    fixed = {"名单": StdTable("名单", [Row({"emp": e}) for e in ("甲", "乙", "丙", "未分配")])}
    assert not lint.check(rs, fixed, "").keys


def test_currency_or_rate_column_not_used_by_rules_is_asked(tmp_path):
    from bonus_agent.extract_rules import Question
    p = tmp_path / "销售.xlsx"
    _book(p, [["项目", "币种", "汇率", "金额"], ["P1", "USD", 7.1, 100], ["P2", "CNY", 1, 200]], "销售")
    w = Workspace(FakeLLM(["x"]), build_inventory(collect_files([tmp_path])))
    w.define_dataset("销售", "销售.xlsx", "销售", {"pid": {"column": "项目", "type": "id"},
                                              "amt": {"column": "金额", "type": "money"}}, key="pid")
    rs = RuleSet.model_validate({"name": "t", "version": "1", "pool": {"expr": "sum(amt, of: \"销售\")"}, "allocation": [],
                                 "datasets": {n: d.model_dump(mode="json") for n, d in w.datasets.items()}})
    keys = [k for _, k in w._currency_gates(rs)]
    assert keys == ["币种 币种", "币种 汇率"]
    w.questions = [Question(id="q1", text="销售表里有「币种」「汇率」列，美元怎么折算？", options=[], answer="美元按汇率折算")]
    assert w._currency_gates(rs) == []


def test_ids_differing_only_in_case_or_spaces_are_matched_with_a_warning():
    from bonus_core.data.table import from_records
    from bonus_core.engine.run import compute
    rs = _rs_yaml('''
name: t
version: v1
datasets:
  客户: {key: cust, fields: {cust: {type: id}, budget: {type: money}}}
  销售: {link: cust, fields: {cust: {type: id}, amt: {type: money}}}
pool: {scope: global, expr: "100"}
allocation:
  - {level: 客户, key: cust, weight: "sum(amt, of: \\"销售\\")"}
''')
    data = from_records(rs, {"客户": [{"cust": "Belvape", "budget": 10}, {"cust": "ACME", "budget": 5}],
                             "销售": [{"cust": "BELVAPE", "amt": 30}, {"cust": " acme ", "amt": 10}, {"cust": "ｂelvape", "amt": 60}]})
    res = compute(rs, data)
    notes = [i for i in res.issues if i.code == "link_normalized"]
    assert notes and "BELVAPE→Belvape" in notes[0].message
    assert not [i for i in res.issues if i.severity == "blocking"], res.issues
    assert {k: v.final for k, v in res.nodes.items() if k.startswith("客户:")} == {"客户:Belvape": "90.00", "客户:ACME": "10.00"}
    assert data["销售"].rows[0].values["cust"] == "BELVAPE"          # 原数据不改
