"""造的提成样例：手写的参照规则要算出期望结果（不调用模型）。"""
import sys
from pathlib import Path

CASE = Path(__file__).resolve().parents[1] / "evals" / "cases" / "syn_commission"


def _case():
    sys.path.insert(0, str(CASE))
    try:
        import syn_ref as reference
        import syn_run
    finally:
        sys.path.remove(str(CASE))
    return reference, syn_run


def test_reference_rules_match_expected():
    reference, syn_run = _case()
    w = reference.build()
    assert w.result is not None
    ok, lines = syn_run.check(w.result)
    assert ok, "\n".join(lines)


def test_tier_below_first():
    from decimal import Decimal as D
    from bonus_core.dsl.models import TierTable
    from bonus_core.engine.evaluator import tier_amount, tier_rate
    ex = TierTable(kind="tier", mode="excess", steps=[[0, 0.01], [100000, 0.02]], below_first="first")
    assert tier_amount(ex, D("-5000")) == D("-50")
    assert tier_amount(ex, D("150000")) == D("2000")
    assert tier_amount(ex.model_copy(update={"below_first": "zero"}), D("-5000")) == 0
    wh = TierTable(kind="tier", mode="whole", steps=[[0, 0.8], [1, 1.0]], below_first="first")
    assert tier_rate(wh, D("-0.2")) == D("0.8")
    assert tier_rate(wh.model_copy(update={"below_first": "zero"}), D("-0.2")) == 0


def test_negative_target_allocates_by_weight():
    from decimal import Decimal as D
    from bonus_core.engine.allocation import allocate
    from bonus_core.engine.tree import Node
    kids = [Node(id=f"k{i}", level="人", key=None, weight=D(w)) for i, w in enumerate(["5", "3", "2", "1"])]
    top = Node(id="p", level="项目", key=None, children=kids)
    allocate(top, D("-155.40"))
    assert sum(k.final for k in kids) == D("-155.40")
    assert all(k.final < 0 for k in kids)
    assert kids[0].final == D("-70.64") or kids[0].final == D("-70.63")


def test_unmatched_ignore_needs_real_quote():
    import pytest
    reference, _ = _case()
    from bonus_agent.agent import ToolError
    w = reference.build()
    args = dict(name="逾期2", file=reference.X, sheet="逾期应收",
                fields={"cust": {"column": "客户简称", "type": "id"}, "o2": {"column": "逾期金额", "type": "money", "unit": "元"}},
                link="cust", unmatched="ignore")
    with pytest.raises(ToolError, match="依据"):
        w.define_dataset(**args, unmatched_basis={"file": "guide.md", "quote": "这句话材料里根本没有，不参与"})
    out = w.define_dataset(**args, unmatched_basis=reference.B)
    assert "已保存" in out


def test_unmatched_hint_points_to_missing_period():
    """明细表有日期字段却没写 period，其他期间的行挂不上按期间取数的上级：提示先查 period，不让模型去问财务或改编号"""
    from bonus_core.engine.issues import Issue
    ref, _ = _case()
    w = ref.build()
    w.datasets["名单"].period = None
    iss = [Issue.block("link_unmatched", "「名单」中 pid=202601|C001|甲|M1 在「项目」中找不到")]
    out = w._unmatched_text(iss)
    assert "没有写 period" in out and 'period: "n_month"' in out
    w2 = ref.build()
    assert "没有写 period" not in w2._unmatched_text(iss)


def test_unpivot_share_from_text_column():
    """几个人的比例集中写在另一列文字里（“甲60%，乙40%”），人名格子里只有名字：按名字取比例"""
    from decimal import Decimal as D
    from bonus_core.data.table_detect import SheetTable, parse_shares, unpivot_by
    assert parse_shares("吴晨晨80%，张荣20%") == {"吴晨晨": D("0.8"), "张荣": D("0.2")}
    st = SheetTable("f.xlsx", "s", 1, ["项目", "经理1", "经理2", "比例"],
                    [(2, ["P1", "吴晨晨", "张荣", "吴晨晨80%，张荣20%"]), (3, ["P2", "吴晨晨", None, None])], [], 1.0, [], [], [], [])
    out = unpivot_by(st, {"columns": ["经理1", "经理2"], "value": "人", "label": "角色", "share": "占比", "share_from": "比例"})
    h = out.headers
    got = [(v[0], v[h.index("人")], v[h.index("占比")]) for _, v in out.rows]
    assert got == [("P1", "吴晨晨", D("0.8")), ("P1", "张荣", D("0.2")), ("P2", "吴晨晨", D(1))]


def test_reference_rules_match_expected_v2():
    """第二套：规则在 PPT 里、销售单有标题行和合计行、名单跨月且夹着重复表头、技术支持比例写在另一列文字里、毛利率有空值"""
    c2 = CASE.parent / "syn_commission2"
    sys.path[:0] = [str(c2), str(CASE)]
    try:
        import syn2_ref
        import syn_run
    finally:
        del sys.path[:2]
    w = syn2_ref.build()
    ok, lines = syn_run.check(w.result, c2)
    assert ok, "\n".join(lines)


def test_dup_column_choice_checked_against_reference_tables():
    """两列同名都写明了列号也会选错：用的那一列和其他表对不上、另一列对得上更多时不保存，并列出例子"""
    import pytest
    from bonus_agent.agent import ToolError
    ref, _ = _case()
    w = ref.build()
    with pytest.raises(ToolError, match="安禾集团→安禾"):
        w.define_dataset("客户X", ref.X, "销售单", {"cust": {"column": "客户简称(D列)", "type": "id"}}, key="cust", distinct=True)
    assert "已保存" in w.define_dataset("客户Y", ref.X, "销售单", {"cust": {"column": "客户简称(K列)", "type": "id"}}, key="cust", distinct=True)


def test_fact_from_wrong_page_is_flagged_once():
    """参数名写了区域，引用的原文却在另一页（那一页没有这个区域）：先拦一次提示抄错页，原样再提交才登记"""
    import pytest
    from bonus_agent.agent import ToolError, Workspace
    from bonus_agent.auto import build_inventory
    from bonus_agent.llm import FakeLLM
    from bonus_agent.parsing.docs import Block, ParsedDoc
    from bonus_agent.auto import Inventory
    d = ParsedDoc("规则.pptx", [Block("规则.pptx", 1, "text", "欧洲区阶梯 200万以下 0.30%"),
                                 Block("规则.pptx", 2, "text", "南方国家区阶梯 200万以下 0.50%"),
                                 Block("规则.pptx", 3, "text", "美洲区阶梯同欧洲区")])
    w = Workspace(FakeLLM(["x"]), Inventory([Path("规则.pptx")], [d], [], {}))
    bad = {"name": "美洲区阶梯-第1档", "value": "0.50%", "file": "规则.pptx", "quote": "南方国家区阶梯 200万以下 0.50%"}
    with pytest.raises(ToolError, match="抄错了页"):
        w.record_facts([bad])
    assert "已登记 1 条" in w.record_facts([bad])            # 原样再提交：登记
    ok = {"name": "南方国家区阶梯-第1档", "value": "0.50%", "file": "规则.pptx", "quote": "南方国家区阶梯 200万以下 0.50%"}
    assert "已登记 1 条" in w.record_facts([ok])


def test_link_matches_when_only_separators_differ(tmp_path):
    """参考表里的编号是连写的（202609C1），项目编号是用 | 拼的（202609|C1）：去掉分隔符一致就对上，并提示"""
    import openpyxl
    from bonus_agent.agent import Workspace
    from bonus_agent.auto import build_inventory, collect_files
    from bonus_agent.llm import FakeLLM
    from bonus_core.engine.tree import _loose_get
    assert _loose_get({"202609|C1": 1, "202609|C2": 2}, "202609C2") == 2
    assert _loose_get({"a|bc": 1, "ab|c": 2}, "abc") is None            # 去掉分隔符后撞车的不算
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "销售"
    for r in [["月份", "客户", "金额"], ["202609", "C1", 100], ["202609", "C2", 200]]:
        ws.append(r)
    s = wb.create_sheet("系数")
    for r in [["编号", "系数"], ["202609C1", 0.5], ["202609C2", 1]]:
        s.append(r)
    wb.save(tmp_path / "d.xlsx")
    w = Workspace(FakeLLM(["x"]), build_inventory(collect_files([tmp_path])))
    w.define_dataset("项目", "d.xlsx", "销售", {"pid": {"column": "项目编号", "type": "id"}, "amt": {"column": "金额", "type": "money", "unit": "元"}},
                     key="pid", combine={"项目编号": ["月份", "客户"]})
    w.define_dataset("系数", "d.xlsx", "系数", {"pid": {"column": "编号", "type": "id"}, "c": {"column": "系数", "type": "ratio"}}, link="pid")
    w.set_rules(variables={"pay": {"scope": "项目", "expr": 'amt * sum(c, of: "系数")'}},
                pool={"scope": "项目", "expr": "pay"}, allocation=[{"level": "项目", "key": "pid"}])
    out = w.trial_run()
    assert "试算成功" in out, out
    assert w.result.pool == "250.00"


def test_if_empty_skip_and_key_autofill(tmp_path):
    """空格子按“查不到”处理（if_empty: skip）；key 没写进 fields、combine 合成了新列时自动补上"""
    import openpyxl
    from bonus_agent.agent import Workspace
    from bonus_agent.auto import build_inventory, collect_files
    from bonus_agent.llm import FakeLLM
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "销售"
    for r in [["月份", "客户", "金额"], ["202609", "C1", 100], ["202609", "C2", 200]]:
        ws.append(r)
    s = wb.create_sheet("毛利")
    for r in [["月份", "客户", "毛利率"], ["202609", "C1", 0.5], ["202609", "C2", None]]:
        s.append(r)
    wb.save(tmp_path / "d.xlsx")
    w = Workspace(FakeLLM(["x"]), build_inventory(collect_files([tmp_path])))
    out = w.define_dataset("项目", "d.xlsx", "销售", {"amt": {"column": "金额", "type": "money", "unit": "元"}},
                           key="pid", combine={"项目编号": ["月份", "客户"]})
    assert "已按列「项目编号」补上" in out
    w.define_dataset("毛利", "d.xlsx", "毛利", {"pid": {"column": "项目编号", "type": "id"},
                                                "m": {"column": "毛利率", "type": "ratio", "if_empty": "skip"}},
                     link="pid", combine={"项目编号": ["月份", "客户"]})
    w.set_rules(variables={"coef": {"scope": "项目", "expr": 'if(count(of: "毛利") > 0, sum(m, of: "毛利"), 1)'},
                           "pay": {"scope": "项目", "expr": "amt * coef"}},
                pool={"scope": "项目", "expr": "pay"}, allocation=[{"level": "项目", "key": "pid"}])
    assert "试算成功" in w.trial_run()
    assert w.result.pool == "250.00"          # C1：100×0.5；C2 毛利率空着按查不到，系数 1


def test_page_with_parameters_not_cited_blocks_finish():
    """PPT 某页有整张参数表（成组的百分数），台账和条款都没引用这一页：结束前拦下，指出哪一页；写进 ignored 后放行"""
    from bonus_agent.agent import Workspace
    from bonus_agent.auto import Inventory
    from bonus_agent.facts import Fact
    from bonus_agent.llm import FakeLLM
    from bonus_agent.parsing.docs import Block, ParsedDoc
    d = ParsedDoc("规则.pptx", [Block("规则.pptx", 1, "text", "2026年 提成方案"),
                                 Block("规则.pptx", 2, "text", "甲区阶梯 100万以下 0.20% 100-200万 0.25%"),
                                 Block("规则.pptx", 3, "text", "乙区阶梯 100万以下 0.30% 100-200万 0.33%")])
    w = Workspace(FakeLLM(["x"]), Inventory([Path("规则.pptx")], [d], [], {}))
    w.facts.append(Fact(name="甲区阶梯-第1档", value="0.20%", file="规则.pptx", quote="甲区阶梯 100万以下 0.20%"))
    gaps = w._page_gaps(set())
    assert len(gaps) == 1 and "第 3 页" in gaps[0] and "〔规则.pptx#3〕" in gaps[0]      # 第 1 页只有年份，不算参数页
    assert w._page_gaps({"规则.pptx#3"}) == [] and w._page_gaps({"规则.pptx"}) == []


def test_hint_when_same_table_with_more_months_exists(tmp_path):
    """用当月明细定义数据表时，另有表头相同、行数多得多的表（年内累计明细）：提示规则要累计/月平均时用那份并写 window"""
    import openpyxl
    from bonus_agent.agent import Workspace
    from bonus_agent.auto import build_inventory, collect_files
    from bonus_agent.llm import FakeLLM
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "本月"
    for r in [["月份", "客户", "产品", "金额"], ["2026-09", "C1", "P1", 100], ["2026-09", "C2", "P1", 200]]:
        ws.append(r)
    s = wb.create_sheet("累计")
    s.append(["月份", "客户", "产品", "金额"])
    for m in range(1, 10):
        s.append([f"2026-{m:02d}", "C1", "P1", 10 * m])
    wb.save(tmp_path / "d.xlsx")
    w = Workspace(FakeLLM(["x"]), build_inventory(collect_files([tmp_path])))
    fields = {"cust": {"column": "客户", "type": "id"}, "amt": {"column": "金额", "type": "money", "unit": "元"}}
    out = w.define_dataset("当月", "d.xlsx", "本月", fields, key="cust")
    assert "另有「d.xlsx/累计」" in out and "year_to_date" in out
    out = w.define_dataset("累计", "d.xlsx", "累计", {**fields, "m": {"column": "月份", "type": "text"}}, link="cust")
    assert "另有" not in out


def test_link_to_wrong_table_suggests_better_owner(tmp_path):
    """两张参考表互相挂（逾期挂到呆滞上），有客户对不上；明细表的客户字段能全部对上：提示挂错了表、该挂到哪"""
    import openpyxl
    from bonus_agent.agent import Workspace
    from bonus_agent.auto import build_inventory, collect_files
    from bonus_agent.llm import FakeLLM
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "销售"
    for r in [["单号", "客户", "金额"], ["S1", "A", 100], ["S2", "B", 200], ["S3", "C", 300], ["S4", "D", 50]]:
        ws.append(r)
    s = wb.create_sheet("呆滞")
    for r in [["客户", "呆滞金额"], ["A", 10], ["B", 20]]:
        s.append(r)
    s = wb.create_sheet("逾期")
    for r in [["客户", "逾期金额"], ["A", 5], ["C", 6], ["D", 7]]:
        s.append(r)
    wb.save(tmp_path / "d.xlsx")
    w = Workspace(FakeLLM(["x"]), build_inventory(collect_files([tmp_path])))
    w.define_dataset("销售单", "d.xlsx", "销售", {"no": {"column": "单号", "type": "id"}, "cust": {"column": "客户", "type": "id"},
                                                   "amt": {"column": "金额", "type": "money", "unit": "元"}}, key="no")
    w.define_dataset("呆滞", "d.xlsx", "呆滞", {"cust": {"column": "客户", "type": "id"},
                                               "dz": {"column": "呆滞金额", "type": "money", "unit": "元"}}, key="cust")
    out = w.define_dataset("逾期", "d.xlsx", "逾期", {"cust": {"column": "客户", "type": "id"},
                                                    "yq": {"column": "逾期金额", "type": "money", "unit": "元"}}, link="cust")
    assert "多半挂错了表" in out and "「销售单」的字段 cust 能对上 3/3" in out
