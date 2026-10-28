"""未发放台账：挂起金额在后续回款后，按「当时」的规则版本补发。"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from bonus_core.data.table import Datasets
from bonus_core.dsl.models import RuleSet
from bonus_core.engine.allocation import CENT
from bonus_core.engine.evaluator import Evaluator
from bonus_core.engine.issues import EngineError, Issue
from bonus_core.engine.run import Result
from bonus_core.engine.tree import build_tree


def ledger_entries(res: Result) -> list[dict]:
    return [
        {"node_id": nid, "condition": c, "due": p.final, "held": h, "released": "0.00",
         "rule_version": res.rule_version, "origin_hash": res.input_hash}
        for nid, p in res.payouts.items() for c, h in p.holds.items() if Decimal(h) > 0
    ]


def release(rs_then: RuleSet, entries: list[dict], updated: Datasets) -> list[dict]:
    """用当时的规则 rs_then 和最新数据（如最新回款）重算挂起比例，返回每条记录可释放的金额。

    应发金额沿用当时的结果，不随新数据变化；只有挂起部分会被释放。
    """
    root, by_level, issues = build_tree(rs_then, updated)
    blocking = [i for i in issues if i.severity == "blocking"]
    if blocking:
        raise EngineError(blocking)
    ev = Evaluator(rs_then, root, by_level)
    nodes = {n.id: n for n in root.walk()}
    conds = {c.id: c for c in rs_then.payout}
    out = []
    for e in entries:
        if e["rule_version"] != rs_then.version:
            raise EngineError([Issue.block("version_mismatch", f"台账记录属于规则 {e['rule_version']}，不能用 {rs_then.version} 补发")])
        n = nodes.get(e["node_id"])
        if n is None:
            out.append({**e, "release_now": "0.00", "note": "最新数据中找不到该人员"})
            continue
        cond = conds.get(e["condition"])
        if cond is None:
            raise EngineError([Issue.block("condition_missing", f"规则 {rs_then.version} 中没有发放条件 {e['condition']}", e["node_id"])])
        pay = bool(cond.pay_ratio.strip())
        r = ev.evaluate(cond.pay_ratio if pay else cond.hold_ratio, n,
                        f"{n.id}/补发{'可发' if pay else '挂起'}比例/{e['condition']}", f"payout.{e['condition']}")
        if r < 0 or r > 1:      # 与首次计算同样的范围校验，不能算出负的挂起
            raise EngineError([Issue.block("hold_ratio_range", f"{e['condition']} 的比例 {r} 不在 0~1 之间", n.id)])
        due, held, released = Decimal(e["due"]), Decimal(e["held"]), Decimal(e["released"])
        still = due - (due * r).quantize(CENT, ROUND_HALF_UP) if pay else (due * r).quantize(CENT, ROUND_HALF_UP)
        still = min(max(still, Decimal(0)), held)       # 挂起只会减少，不会超过当初挂起的金额
        now = max(Decimal(0), held - still - released)
        out.append({**e, "release_now": str(now), "still_held": str(max(held - released - now, Decimal(0)))})
    return out
