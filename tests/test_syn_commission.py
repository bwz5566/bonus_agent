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
