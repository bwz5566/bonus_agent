"""评测打分：结构检查 + 用标准数据实算金额，与标准答案逐人对比。"""
from __future__ import annotations

from decimal import Decimal

from bonus_agent.extract_rules import ExtractResult
from bonus_core.dsl.models import LookupTable, RuleSet, TierTable, to_decimal
from bonus_core.engine.issues import EngineError
from bonus_core.engine.allocation import AdjustConflict
from bonus_core.engine.evaluator import EvalError
from bonus_core.engine.run import Result, compute


def _steps(v) -> list[tuple[Decimal, Decimal]]:
    return [(to_decimal(a), to_decimal(b)) for a, b in v]


def run_check(chk: dict, rs: RuleSet, ex: ExtractResult) -> bool:
    t = chk["type"]
    if t == "tier":
        want = _steps(chk["steps"])
        for tb in rs.tables.values():
            if isinstance(tb, TierTable) and list(tb.steps) == want:
                return "mode" not in chk or tb.mode == chk["mode"]
        return False
    if t == "lookup":
        want = {k: to_decimal(v) for k, v in chk["mapping"].items()}
        for tb in rs.tables.values():
            if isinstance(tb, LookupTable) and all(tb.mapping.get(k) == v for k, v in want.items()):
                if "default" in chk:
                    return tb.default == to_decimal(chk["default"])
                return True
        return False
    if t == "levels":
        return [lv.key for lv in rs.allocation] == chk["keys"]
    if t == "level_count":
        return len(rs.allocation) == chk["count"]
    if t == "payout_exists":
        return bool(rs.payout)
    if t == "question":
        kws = [str(k) for k in chk["keywords"]]
        # 至少命中两个关键词，避免泛泛的提问被算作命中
        return any(sum(k in q.text + " ".join(q.options) for k in kws) >= 2 for q in ex.questions)
    raise ValueError(f"未知检查类型 {t}")


def leaf_amounts(res: Result) -> dict[str, Decimal]:
    leaf = res.levels[-1]
    return {n.key: Decimal(n.final) for n in res.nodes.values() if n.level == leaf}


def numeric_score(rs: RuleSet, data, reference: Result) -> dict:
    """用抽取出的规则在标准数据上实算，与参考规则的结果逐人对比。"""
    try:
        got = compute(rs, data)
    except (EngineError, AdjustConflict, EvalError, ValueError) as e:
        return {"computed": False, "error": str(e)[:300], "pool_match": False, "leaf_match_rate": 0.0}
    return compare_results(got, reference)


def compare_results(got: Result, reference: Result) -> dict:
    """按最末层主键逐个比对金额（auto 模式下层级名、字段名可能和标准答案不同，主键取值相同即可）。"""
    want, have = leaf_amounts(reference), leaf_amounts(got)
    hits = sum(1 for k, v in want.items() if have.get(k) == v)
    def held(res: Result) -> dict:
        return {res.nodes[nid].key: p.held for nid, p in res.payouts.items()}
    held_ok = held(got) == held(reference) if reference.rule_version and reference.payouts else True
    return {
        "computed": True, "pool": got.pool, "pool_expected": reference.pool,
        "pool_match": got.pool == reference.pool,
        "leaf_match_rate": hits / len(want) if want else 0.0,
        "held_match": held_ok,
    }
