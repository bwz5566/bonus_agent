"""引擎入口：compute(规则, 数据, 调整) -> Result。

同样的输入必然得到同样的结果；结果里记录输入哈希、规则版本和引擎版本用于自证。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from bonus_core.data.table import Datasets
from bonus_core.dsl.models import RuleSet
from bonus_core.dsl.validator import validate
from bonus_core.engine.allocation import CENT, AdjustConflict, allocate, max_allowed, round_pool, round_tree
from bonus_core.engine.evaluator import EvalError, Evaluator, Trace
from bonus_core.engine.issues import EngineError, Issue
from bonus_core.engine.tree import ROOT_LEVEL, Node, build_tree

ENGINE_VERSION = "0.1.0"


@dataclass
class Adjustment:
    node_id: str
    amount: Decimal
    actor: str = ""
    reason: str = ""
    id: str = ""
    created_at: str = ""
    revoked: bool = False

    def __post_init__(self):
        self.amount = Decimal(str(self.amount))
        if not self.created_at:
            self.created_at = datetime.now().isoformat(timespec="seconds")


@dataclass
class NodeResult:
    id: str
    level: str
    key: str | None
    label: str
    parent: str | None
    weight: str
    exact: str
    final: str
    locked: bool


@dataclass
class PayoutResult:
    node_id: str
    final: str
    held: str
    paid: str
    holds: dict[str, str]       # 条件 id -> 挂起金额
    hold_ratios: dict[str, str]


@dataclass
class Result:
    rule_set: str
    rule_version: str
    engine_version: str
    input_hash: str
    pool_exact: str
    pool: str
    levels: list[str]
    nodes: dict[str, NodeResult]
    payouts: dict[str, PayoutResult]
    traces: dict[str, Trace]
    adjustments: list[Adjustment]
    issues: list[Issue] = field(default_factory=list)

    def node(self, node_id: str) -> NodeResult:
        return self.nodes[node_id]

    def amount(self, node_id: str) -> Decimal:
        return Decimal(self.nodes[node_id].final)

    def to_dict(self) -> dict:
        d = asdict(self)
        return json.loads(json.dumps(d, default=str, ensure_ascii=False))


def input_hash(rs: RuleSet, data: Datasets) -> str:
    h = hashlib.sha256()
    h.update(rs.model_dump_json().encode())
    for name in sorted(data):
        for r in data[name].rows:
            h.update(name.encode())
            h.update(json.dumps(r.values, default=str, sort_keys=True, ensure_ascii=False).encode())
    h.update(ENGINE_VERSION.encode())
    return h.hexdigest()


class Engine:
    """一次计算的上下文：建树、求权重和奖金池，然后按调整事件分配。"""

    def __init__(self, rs: RuleSet, data: Datasets):
        issues = [i for i in validate(rs) if i.severity == "blocking"]
        if issues:
            raise EngineError(issues)
        self.rs = rs
        self.data = data
        root, by_level, issues = build_tree(rs, data)
        blocking = [i for i in issues if i.severity == "blocking"]
        if blocking:
            raise EngineError(blocking)
        self.root, self.by_level, self.issues = root, by_level, issues
        self.ev = Evaluator(rs, root, by_level)
        self.nodes: dict[str, Node] = {n.id: n for n in root.walk()}
        try:
            self.pool_exact = self.ev.evaluate(rs.pool.expr, root, f"{ROOT_LEVEL}/奖金池", "pool")
            for lv in rs.allocation:
                for n in by_level[lv.level]:
                    w = self.ev.evaluate(lv.weight, n, f"{n.id}/权重", f"allocation.{lv.level}")
                    if w < 0:
                        raise EvalError(f"权重不能为负：{w}", n.id)
                    n.weight = w
        except EvalError as e:
            raise EngineError([Issue.block("eval_error", str(e), e.where)]) from None
        if self.pool_exact < 0:
            raise EngineError([Issue.block("pool_negative", f"奖金池为负：{self.pool_exact}")])
        self.hash = input_hash(rs, data)

    # ---- 分配 ----
    def _apply(self, adjustments: list[Adjustment], actor_scopes: dict[str, set[str]] | None) -> list[Adjustment]:
        active = [a for a in sorted(adjustments, key=lambda a: a.created_at) if not a.revoked]
        for n in self.nodes.values():
            n.lock = None
        for a in active:
            n = self.nodes.get(a.node_id)
            if n is None:
                raise AdjustConflict(a.node_id, "节点不存在")
            if n is self.root:
                raise AdjustConflict(a.node_id, "奖金池总额不能调整")
            if a.amount < 0 or a.amount != a.amount.quantize(CENT):
                raise AdjustConflict(a.node_id, f"调整金额必须是非负、精确到分的数：{a.amount}")
            if actor_scopes is not None and n.parent.id not in actor_scopes.get(a.actor, set()):
                raise PermissionError(f"{a.actor} 无权调整 {a.node_id}（只能调整自己管辖节点的下级）")
            n.lock = a.amount  # 同一节点多次调整，以最后一次为准
        allocate(self.root, self.pool_exact)
        round_tree(self.root, round_pool(self.pool_exact))
        return active

    def compute(self, adjustments: list[Adjustment] = (), actor_scopes=None) -> Result:
        adjustments = list(adjustments)
        self._apply(adjustments, actor_scopes)
        payouts = self._payouts()
        nodes = {
            n.id: NodeResult(
                id=n.id, level=n.level, key=n.key, label=n.label,
                parent=n.parent.id if n.parent else None,
                weight=str(n.weight), exact=str(n.exact), final=str(n.final), locked=n.locked,
            )
            for n in self.root.walk()
        }
        return Result(
            rule_set=self.rs.name, rule_version=self.rs.version, engine_version=ENGINE_VERSION,
            input_hash=self.hash, pool_exact=str(self.pool_exact), pool=str(self.root.final),
            levels=[lv.level for lv in self.rs.allocation], nodes=nodes, payouts=payouts,
            traces=dict(self.ev.traces), adjustments=adjustments, issues=list(self.issues),
        )

    def preview(self, existing: list[Adjustment], new: Adjustment, actor_scopes=None) -> dict:
        """试算一次调整：返回每个节点的变化；冲突时附带 max_allowed。"""
        before = self.compute(existing, actor_scopes)
        self._apply(existing, actor_scopes)
        node = self.nodes.get(new.node_id)
        limit = max_allowed(node) if node is not None and node.parent is not None else None
        try:
            after = self.compute(list(existing) + [new], actor_scopes)
        except AdjustConflict as e:
            e.max_allowed = limit
            raise
        changes = [
            {"node_id": k, "before": before.nodes[k].final, "after": v.final, "locked": v.locked}
            for k, v in after.nodes.items() if v.final != before.nodes[k].final
        ]
        return {"changes": changes, "max_allowed": str(limit) if limit is not None else None, "result": after}

    # ---- 发放条件 ----
    def _payouts(self) -> dict[str, PayoutResult]:
        out = {}
        leaf = self.rs.leaf.level
        for n in self.by_level[leaf]:
            holds, ratios = {}, {}
            remaining = n.final
            for cond in self.rs.payout:
                try:
                    r = self.ev.evaluate(cond.hold_ratio, n, f"{n.id}/挂起比例/{cond.id}", f"payout.{cond.id}")
                except EvalError as e:
                    raise EngineError([Issue.block("eval_error", str(e), e.where)]) from None
                if r < 0 or r > 1:
                    raise EngineError([Issue.block("hold_ratio_range", f"{cond.id} 的挂起比例 {r} 不在 0~1 之间", n.id)])
                h = min((n.final * r).quantize(CENT, ROUND_HALF_UP), remaining)
                remaining -= h
                holds[cond.id], ratios[cond.id] = str(h), str(r)
            held = n.final - remaining
            out[n.id] = PayoutResult(n.id, str(n.final), str(held), str(remaining), holds, ratios)
        return out


def compute(rs: RuleSet, data: Datasets, adjustments: list[Adjustment] = (), actor_scopes=None) -> Result:
    return Engine(rs, data).compute(adjustments, actor_scopes)
