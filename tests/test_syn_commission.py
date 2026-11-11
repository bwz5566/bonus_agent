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
