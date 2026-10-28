from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bonus_core.data.table import from_records
from bonus_core.dsl.models import RuleSet, TierTable
from bonus_core.dsl.parser import ExprError, parse
from bonus_core.dsl.validator import validate
from bonus_core.engine.allocation import AdjustConflict
from bonus_core.engine.evaluator import tier_amount
from bonus_core.engine.issues import EngineError
from bonus_core.engine.run import Adjustment, Engine, compute
from bonus_core.golden import run_golden

EXAMPLE = Path(__file__).parent.parent / "examples" / "sales_q3" / "rules.yaml"
D = Decimal


def three_groups(weights=(2, 3, 1), people_per_group=3, pool="6000000"):
    """方案文档里的例子：总包 600 万，小组权重 2:3:1。"""
    rs = RuleSet.model_validate({
        "name": "t", "version": "v1",
        "datasets": {
            "人员": {"key": "eid", "fields": {"eid": {"type": "id"}, "gid": {"type": "id"}, "score": {"type": "number"}}},
            "小组": {"key": "gid", "fields": {"gid": {"type": "id"}, "w": {"type": "number"}}},
        },
        "pool": {"expr": pool},
        "allocation": [
            {"level": "小组", "key": "gid", "weight": "w"},
            {"level": "个人", "key": "eid", "parent_key": "gid", "weight": "score"},
        ],
    })
    people = [{"eid": f"E{g}{i}", "gid": f"G{g}", "score": i + 1}
              for g in range(1, len(weights) + 1) for i in range(people_per_group)]
    groups = [{"gid": f"G{g}", "w": w} for g, w in enumerate(weights, start=1)]
    return rs, from_records(rs, {"人员": people, "小组": groups})


def test_parser_rejects_code():
    for bad in ["a.b", "x[0]", "a = 1", "lambda: 1", "a; b"]:
        with pytest.raises(ExprError):
            parse(bad)
    # 函数调用语法合法，但不在白名单里的函数会被校验拦下
    rs, _ = three_groups(pool="__import__('os')")
    assert any(i.code == "fn_unknown" for i in validate(rs))


def test_example_rules_valid_and_golden():
    rs = RuleSet.load(EXAMPLE)
    assert not [i for i in validate(rs) if i.severity == "blocking"]
    results = run_golden(rs)
    assert results and all(r.passed for r in results), [r.diffs for r in results]


def test_tier_modes():
    t = TierTable(mode="whole", steps=[(0, 0), (10_000_000, "0.10"), (20_000_000, "0.12")])
    assert tier_amount(t, D(15_000_000)) == D("1500000.00")
    assert tier_amount(t, D(25_000_000)) == D("3000000.00")
    assert tier_amount(t, D(9_999_999)) == 0
    e = t.model_copy(update={"mode": "excess"})
    # 前 1000 万 0%，1000~2000 万 10%，超出 2000 万部分 12%
    assert tier_amount(e, D(15_000_000)) == D("500000.00")
    assert tier_amount(e, D(25_000_000)) == D("1600000.00")
    strict = t.model_copy(update={"lower_inclusive": False})
    assert tier_amount(strict, D(10_000_000)) == 0


def test_adjustment_example_from_plan():
    rs, data = three_groups()
    eng = Engine(rs, data)
    r0 = eng.compute()
    assert [r0.amount(f"小组:G{i}") for i in (1, 2, 3)] == [D("2000000.00"), D("3000000.00"), D("1000000.00")]
    a1 = Adjustment("小组:G1", "1800000", created_at="2026-01-01T00:00:01")
    r1 = eng.compute([a1])
    assert [r1.amount(f"小组:G{i}") for i in (1, 2, 3)] == [D("1800000.00"), D("3150000.00"), D("1050000.00")]
    a2 = Adjustment("小组:G3", "1200000", created_at="2026-01-01T00:00:02")
    r2 = eng.compute([a1, a2])
    assert [r2.amount(f"小组:G{i}") for i in (1, 2, 3)] == [D("1800000.00"), D("3000000.00"), D("1200000.00")]
    # 组长把 G2 的一名成员调高 2 万：小组总额不变，组内其他人按原权重分摊
    before = r2.amount("个人:E20")
    a3 = Adjustment("个人:E20", str(before + 20000), created_at="2026-01-01T00:00:03")
    r3 = eng.compute([a1, a2, a3])
    assert r3.amount("小组:G2") == D("3000000.00")
    assert sum(r3.amount(f"个人:E2{i}") for i in range(3)) == D("3000000.00")
    assert r3.amount("个人:E10") == r2.amount("个人:E10")  # 其他小组不受影响
    # 撤销 a1 后回到只有 a2 的效果
    a1.revoked = True
    r4 = eng.compute([a1, a2])
    assert r4.amount("小组:G1") == D("1920000.00")  # (600-120) × 2/(2+3)


def test_adjust_conflicts_and_permissions():
    rs, data = three_groups()
    eng = Engine(rs, data)
    with pytest.raises(AdjustConflict):
        eng.compute([Adjustment("小组:G1", "7000000")])
    with pytest.raises(AdjustConflict):  # 同级全部锁定且合计不等于总额
        eng.compute([Adjustment("小组:G1", "1"), Adjustment("小组:G2", "1"), Adjustment("小组:G3", "1")])
    with pytest.raises(AdjustConflict):
        eng.compute([Adjustment("小组:G1", "100.001")])
    with pytest.raises(AdjustConflict):
        eng.compute([Adjustment("全部", "1")])
    scopes = {"组长1": {"小组:G1"}}
    eng.compute([Adjustment("个人:E10", "1000", actor="组长1")], actor_scopes=scopes)
    with pytest.raises(PermissionError):
        eng.compute([Adjustment("个人:E20", "1000", actor="组长1")], actor_scopes=scopes)


def test_preview_reports_max_allowed():
    rs, data = three_groups()
    eng = Engine(rs, data)
    existing = [Adjustment("小组:G1", "1800000", created_at="1")]
    with pytest.raises(AdjustConflict) as e:
        eng.preview(existing, Adjustment("小组:G3", "4500000", created_at="2"))
    assert e.value.max_allowed == D("4200000.00")
    p = eng.preview(existing, Adjustment("小组:G3", "1200000", created_at="2"))
    assert {c["node_id"] for c in p["changes"]} >= {"小组:G2", "小组:G3"}


def _check_invariants(res):
    pool = D(res.pool)
    kids = {}
    for n in res.nodes.values():
        if n.parent:
            kids.setdefault(n.parent, []).append(D(n.final))
        assert D(n.final) == D(n.final).quantize(D("0.01"))
    for pid, vals in kids.items():
        assert sum(vals) == D(res.nodes[pid].final), pid
    assert D(res.nodes["全部"].final) == pool


@settings(max_examples=60, deadline=None)
@given(
    weights=st.lists(st.integers(0, 50), min_size=2, max_size=5).filter(lambda w: sum(w) > 0),
    pool=st.decimals("1000", "9999999.99", places=2),
    adj=st.lists(st.tuples(st.integers(0, 4), st.integers(0, 4), st.floats(0, 0.6)), max_size=4),
)
def test_allocation_properties(weights, pool, adj):
    rs, data = three_groups(tuple(weights), people_per_group=4, pool=str(pool))
    eng = Engine(rs, data)
    base = eng.compute()
    _check_invariants(base)
    events = []
    for k, (g, p, frac) in enumerate(adj):
        g = g % len(weights) + 1
        node = f"个人:E{g}{p % 4}" if k % 2 else f"小组:G{g}"
        amt = (D(base.nodes[node].final) * D(str(round(frac, 3)))).quantize(D("0.01"))
        trial = events + [Adjustment(node, str(amt), created_at=f"{k:03d}")]
        try:
            res = eng.compute(trial)
        except AdjustConflict:
            continue
        events = trial
        _check_invariants(res)
        for e in events:  # 锁定值保持
            last = [x for x in events if x.node_id == e.node_id][-1]
            assert res.amount(e.node_id) == last.amount
    # 事件顺序打乱后结果一致（同一节点只保留最后一次，避免顺序语义影响）
    uniq = {e.node_id: e for e in events}
    shuffled = list(reversed(list(uniq.values())))
    for i, e in enumerate(shuffled):
        e.created_at = f"z{i:03d}"
    if uniq:
        a = eng.compute(list(uniq.values()))
        b = eng.compute(shuffled)
        assert {k: v.final for k, v in a.nodes.items()} == {k: v.final for k, v in b.nodes.items()}


def test_errors_are_explicit():
    rs, data = three_groups()
    bad = rs.model_copy(update={"pool": rs.pool.model_copy(update={"expr": "ratio(1, 0)"})})
    with pytest.raises(EngineError, match="分母为 0"):
        compute(bad, data)
    bad = rs.model_copy(update={"pool": rs.pool.model_copy(update={"expr": "unknown_x + 1"})})
    with pytest.raises(EngineError, match="unknown_x"):
        compute(bad, data)


def test_trace_links_to_rows():
    rs = RuleSet.load(EXAMPLE)
    res = compute(rs, from_records(rs, rs.tests[0].datasets))
    t = res.traces["个人:E1/eff_sales"]
    assert t.rows and "销售明细" in t.rows[0]
    assert res.traces["全部/奖金池"].inputs == ("全部/total_profit", "全部/total_sales")


def _variant(**changes):
    rs = RuleSet.load(Path(__file__).parent.parent / "examples" / "sales_q3" / "rules.yaml")
    data = rs.model_dump(mode="json")
    for path, value in changes.items():
        obj = data
        *head, last = path.split("__")
        for k in head:
            obj = obj[int(k)] if k.isdigit() else obj[k]
        obj[last] = value
    return RuleSet.model_validate(data)


@pytest.mark.parametrize("changes, code", [
    # 模型真实犯过的错：小组权重引用个人层变量
    ({"allocation__0__weight": "group_score * eff_sales"}, "scope_var"),
    # 奖金池（global）引用个人层变量
    ({"pool__expr": "tier(sales_tier, eff_sales)"}, "scope_var"),
    # 逐行系数写在聚合外面
    ({"variables__eff_sales__expr": "sum(sales_amount) * lookup(region_coef, region)"}, "detail_outside_agg"),
    ({"variables__eff_sales__expr": "sales_amount * 1.2"}, "detail_outside_agg"),
    # 上层直接用下层实体字段
    ({"allocation__0__weight": "seniority"}, "scope_field"),
])
def test_scope_errors_caught_statically(changes, code):
    issues = validate(_variant(**changes))
    assert any(i.code == code and i.severity == "blocking" for i in issues), issues


def test_scope_check_allows_valid_patterns():
    rs = _variant(allocation__0__weight="group_score * sum(sales_amount * lookup(region_coef, region))",
                  payout__0__hold_ratio="1 - min(collected_ratio, 1) + 0 * seniority * group_score")
    assert not [i for i in validate(rs) if i.severity == "blocking"]


def test_company_table_and_entity_aggregate_scope():
    rs = RuleSet.load(Path(__file__).parent.parent / "evals" / "cases" / "annual_2026" / "rules.yaml")
    data = rs.model_dump(mode="json")
    # 部门层对员工实体表聚合：合法
    data["allocation"][0]["weight"] = "kpi_score * sum(monthly_salary)"
    assert not [i for i in validate(RuleSet.model_validate(data)) if i.severity == "blocking"]
    # 个人层聚合公司级表：聚合不到行
    data["allocation"][1]["weight"] = "sum(net_profit)"
    assert any(i.code == "agg_above" for i in validate(RuleSet.model_validate(data)))
    # 公司级表字段不聚合直接用
    data["allocation"][1]["weight"] = "net_profit"
    assert any(i.code == "detail_outside_agg" for i in validate(RuleSet.model_validate(data)))
