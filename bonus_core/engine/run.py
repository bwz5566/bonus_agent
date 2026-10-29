"""引擎入口：compute(规则, 数据, 调整) -> Result。

同样的输入必然得到同样的结果；结果里记录输入哈希、规则版本和引擎版本用于自证。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from bonus_core.data.patches import apply_patches
from bonus_core.data.table import Datasets
from bonus_core.dsl.models import RuleSet
from bonus_core.dsl.validator import _weighted, validate
from bonus_core.engine.allocation import CENT, AdjustConflict, allocate, max_allowed, round_pool
from bonus_core.dsl import excel_formula as xf
from bonus_core.engine.evaluator import Ctx, EvalError, Evaluator, Trace
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
    final: str                  # 应得
    held: str                   # 暂缓（挂起）
    paid: str                   # 本次实发 = MAX(可发上限 − 已发抵扣, 0)
    holds: dict[str, str]       # 条件 id -> 挂起金额
    hold_ratios: dict[str, str]
    cap: str = ""               # 可发上限 = 应得 − 暂缓
    offset: str = "0"           # 已发抵扣
    overpaid: str = "0"         # 超发待核对 = MAX(已发抵扣 − 可发上限, 0)，只登记不追回
    deducted: str = "0"         # 实际抵扣 = MIN(可发上限, 已发抵扣)


@dataclass
class RollupResult:
    """抵扣的 scope 是关联实体（如 人员）时：最末层按该实体编号汇总后再抵扣（一个人参与多个项目）。"""
    node_id: str
    entity: str                 # 关联实体表名
    key: str
    label: str
    members: list[str]          # 汇总进来的最末层节点
    final: str
    held: str
    cap: str
    offset: str
    deducted: str
    paid: str
    overpaid: str


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
    data_hash: str = ""         # 只含规则和数据；input_hash 另含人工调整和引用的其他方案结果
    rollups: dict[str, RollupResult] = field(default_factory=dict)
    inputs: dict[str, dict[str, object]] = field(default_factory=dict)   # 过程复核表的取数：节点 -> {字段或变量: 引擎值}

    def payout_rows(self) -> list:
        """发放的最终口径：有按实体汇总的抵扣时用汇总行，否则用最末层。"""
        return list(self.rollups.values()) if self.rollups else list(self.payouts.values())

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


def run_hash(base: str, adjustments: list[Adjustment], refs: dict | None) -> str:
    """一次结果的输入哈希：规则 + 数据 + 生效的人工调整（节点、金额、顺序）+ 引用的其他方案结果。"""
    h = hashlib.sha256(base.encode())
    for a in adjustments:
        h.update(json.dumps([a.node_id, str(a.amount), a.created_at, a.id], ensure_ascii=False).encode())
    for k in sorted(refs or {}):
        h.update(f"{k}={refs[k].input_hash}".encode())
    return h.hexdigest()


class Engine:
    """一次计算的上下文：建树、求权重和奖金池，然后按调整事件分配。"""

    def __init__(self, rs: RuleSet, data: Datasets, refs: dict | None = None):
        issues = [i for i in validate(rs) if i.severity == "blocking"]
        if issues:
            raise EngineError(issues)
        self.rs = rs
        data, patched = apply_patches(rs, data)
        self.data = data
        root, by_level, issues = build_tree(rs, data)
        issues = patched + issues
        blocking = [i for i in issues if i.severity == "blocking"]
        if blocking:
            raise EngineError(blocking)
        self.root, self.by_level, self.issues = root, by_level, issues
        self.refs = refs or {}
        self.ev = Evaluator(rs, root, by_level, refs)
        self.nodes: dict[str, Node] = {n.id: n for n in root.walk()}
        try:
            self.issues += self._checks()
            self.pool_exact = self._pool()
            for lv in _weighted(rs):
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

    def _pool(self) -> Decimal:
        """奖金池。scope 为某层级时，在该层每个对象上分别计算并四舍五入到分，上层为下级合计，这些对象的金额固定。"""
        rs = self.rs
        if rs.pool.scope == "global":
            return self.ev.evaluate(rs.pool.expr, self.root, f"{ROOT_LEVEL}/奖金池", "pool")
        levels = [lv.level for lv in rs.allocation]
        at = levels.index(rs.pool.scope)
        for n in self.by_level[rs.pool.scope]:
            v = self.ev.evaluate(rs.pool.expr, n, f"{n.id}/奖金池", "pool")
            if v < 0:
                raise EngineError([Issue.block("pool_negative", f"{n.id} 的奖金池为负：{v}", n.id)])
            n.fixed = round_pool(v)
        for lvl in reversed(levels[:at]):
            for n in self.by_level[lvl]:
                n.fixed = sum((c.fixed for c in n.children), Decimal(0))
        return sum((c.fixed for c in self.root.children), Decimal(0))

    def _checks(self) -> list[Issue]:
        """数据校验规则：每个对象上条件必须成立。阻断类不通过时直接报错，提示类记入结果。"""
        out = []
        for c in self.rs.checks:
            if c.scope == "global":
                targets = [self.root]
            elif c.scope in self.by_level:
                targets = self.by_level[c.scope]
            else:
                targets = sorted((self.root.index or {}).get(c.scope, {}).values(), key=lambda n: n.key)
            failed = []
            for n in targets:
                tid = f"{n.id}/校验/{c.id}"
                v = self.ev._eval_traced(c.expr, n, tid, f"checks.{c.id}").v
                if not isinstance(v, bool):
                    raise EvalError(f"校验 {c.id} 的结果应为是/否，得到 {v}", n.id)
                if not v:
                    failed.append(f"{n.id}：{self.ev.traces[tid].rendered}")
            if failed:
                msg = (f"校验 {c.id} 未通过" + (f"（{c.message}）" if c.message else "") + f"，{len(failed)} 个对象："
                       + "；".join(failed[:10]) + ("……" if len(failed) > 10 else ""))
                out.append(Issue(c.severity, "check_failed", msg, f"checks.{c.id}"))
        blocking = [i for i in out if i.severity == "blocking"]
        if blocking:
            raise EngineError(blocking)
        return out

    # ---- 分配 ----
    def _apply(self, adjustments: list[Adjustment], actor_scopes: dict[str, set[str]] | None) -> list[Adjustment]:
        active = [a for a in sorted(adjustments, key=lambda a: a.created_at) if not a.revoked]
        for n in self.nodes.values():
            n.lock = n.fixed
        for a in active:
            n = self.nodes.get(a.node_id)
            if n is None:
                raise AdjustConflict(a.node_id, "节点不存在")
            if n is self.root:
                raise AdjustConflict(a.node_id, "奖金池总额不能调整")
            if n.fixed is not None:
                raise AdjustConflict(a.node_id, f"{n.level}的奖金包由规则直接算出，不能调整，只能调整它下面的对象")
            if a.amount < 0 or a.amount != a.amount.quantize(CENT):
                raise AdjustConflict(a.node_id, f"调整金额必须是非负、精确到分的数：{a.amount}")
            if actor_scopes is not None and n.parent.id not in actor_scopes.get(a.actor, set()):
                raise PermissionError(f"{a.actor} 无权调整 {a.node_id}（只能调整自己管辖节点的下级）")
            n.lock = a.amount.quantize(CENT)  # 同一节点多次调整，以最后一次为准
        allocate(self.root, round_pool(self.pool_exact), self.pool_exact)
        self._check_stranded()
        return active

    def _check_stranded(self) -> None:
        """不在最末层、下面又没有对象的节点分到了钱：这笔钱发不到任何人，必须拦下（不能悄悄留在中间层）。"""
        leaf = self.rs.allocation[-1].level if self.rs.allocation else None
        stuck = [n for n in self.root.walk() if n.level != leaf and not n.children and n.final != 0]
        if stuck:
            nxt = {lv.level: self.rs.allocation[i + 1].level for i, lv in enumerate(self.rs.allocation[:-1])}
            nxt[ROOT_LEVEL] = self.rs.allocation[0].level if self.rs.allocation else ""
            raise EngineError([Issue.block("branch_empty", f"{n.id}（{n.label}）分到 {n.final} 元，但下面没有任何「{nxt.get(n.level, '')}」，这笔钱发不到人。"
                f"请核对数据是否漏了下级，或规则是否应把这类对象排除（如权重为 0）", n.id) for n in stuck[:8]])

    def compute(self, adjustments: list[Adjustment] = (), actor_scopes=None) -> Result:
        adjustments = list(adjustments)
        active = self._apply(adjustments, actor_scopes)
        payouts = self._payouts()
        rollups = self._rollups(payouts)
        nodes = {
            n.id: NodeResult(
                id=n.id, level=n.level, key=n.key, label=n.label,
                parent=n.parent.id if n.parent else None,
                weight=str(n.weight), exact=str(n.exact), final=str(n.final), locked=n.locked and n.fixed is None,
            )
            for n in self.root.walk()
        }
        return Result(
            rule_set=self.rs.name, rule_version=self.rs.version, engine_version=ENGINE_VERSION,
            input_hash=run_hash(self.hash, active, self.refs), data_hash=self.hash, pool_exact=str(self.pool_exact), pool=str(self.root.final),
            levels=[lv.level for lv in self.rs.allocation], nodes=nodes, payouts=payouts,
            traces=dict(self.ev.traces), adjustments=adjustments, issues=list(self.issues), rollups=rollups,
            inputs=self._sheet_inputs(),
        )

    def _sheet_inputs(self) -> dict[str, dict[str, object]]:
        """「过程复核」表里直接取数的列：每个对象的字段值和不能用公式复算的变量值。取不到的留空。"""
        out: dict[str, dict[str, object]] = {}
        for lv, p in xf.plan(self.rs).items():
            for n in self.by_level.get(lv, []):
                row = out.setdefault(n.id, {})
                for name in p["inputs"]:
                    try:
                        row[name] = self.ev._name(name, Ctx(n, where=f"{n.id}/{name}")).v
                    except (EvalError, ArithmeticError):
                        row[name] = None
        return out

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
                pay = bool(cond.pay_ratio.strip())
                try:
                    r = self.ev.evaluate(cond.pay_ratio if pay else cond.hold_ratio, n,
                                         f"{n.id}/{'可发比例' if pay else '挂起比例'}/{cond.id}", f"payout.{cond.id}")
                except EvalError as e:
                    raise EngineError([Issue.block("eval_error", str(e), e.where)]) from None
                if r < 0 or r > 1:
                    raise EngineError([Issue.block("hold_ratio_range", f"{cond.id} 的比例 {r} 不在 0~1 之间", n.id)])
                if pay:   # 可发上限 = ROUND(应得 × 比例, 2)，挂起 = 应得 − 上限
                    h = min(n.final - (n.final * r).quantize(CENT, ROUND_HALF_UP), remaining)
                else:
                    h = min((n.final * r).quantize(CENT, ROUND_HALF_UP), remaining)
                remaining -= h
                holds[cond.id], ratios[cond.id] = str(h), str(r)
            offset = Decimal(0)
            for o in self.rs.offsets:
                if o.scope != leaf:
                    continue
                try:
                    v = self.ev.evaluate(o.expr, n, f"{n.id}/已发抵扣/{o.id}", f"offsets.{o.id}")
                except EvalError as e:
                    raise EngineError([Issue.block("eval_error", str(e), e.where)]) from None
                if v < 0:
                    raise EngineError([Issue.block("offset_negative", f"{o.id} 的已发金额为负：{v}", n.id)])
                offset += v.quantize(CENT, ROUND_HALF_UP)
            held = n.final - remaining
            paid, over = max(remaining - offset, Decimal(0)), max(offset - remaining, Decimal(0))
            out[n.id] = PayoutResult(n.id, str(n.final), str(held), str(paid), holds, ratios,
                                     cap=str(remaining), offset=str(offset), overpaid=str(over),
                                     deducted=str(min(remaining, offset)))
        return out

    def _rollups(self, payouts: dict[str, PayoutResult]) -> dict[str, RollupResult]:
        """抵扣写在关联实体上：最末层的应得、暂缓、可发按实体编号（最末层行上的字段）汇总，再在实体上算抵扣。"""
        leaf = self.rs.leaf.level
        scopes = list(dict.fromkeys(o.scope for o in self.rs.offsets if o.scope != leaf))
        if not scopes:
            return {}
        ent = scopes[0]                                  # 校验器保证只有一个
        key = self.rs.datasets[ent].key
        index = (self.root.index or {}).get(ent, {})
        groups: dict[str, list[Node]] = {}
        for n in self.by_level[leaf]:
            k = n.attrs.get(key)
            if k in (None, ""):
                raise EngineError([Issue.block("rollup_key_missing", f"{n.id} 没有「{key}」，无法汇总到「{ent}」", n.id)])
            if str(k) not in index:
                raise EngineError([Issue.block("rollup_key_missing", f"{n.id} 的 {key}={k} 在「{ent}」表里找不到", n.id)])
            groups.setdefault(str(k), []).append(n)
        out = {}
        for k in sorted(index):                          # 没有最末层记录的实体也列出（只有预发时记超发）
            e, ms = index[k], groups.get(k, [])
            ps = [payouts[m.id] for m in ms]
            offset = Decimal(0)
            for o in self.rs.offsets:
                if o.scope != ent:
                    continue
                try:
                    v = self.ev.evaluate(o.expr, e, f"{e.id}/已发抵扣/{o.id}", f"offsets.{o.id}")
                except EvalError as err:
                    raise EngineError([Issue.block("eval_error", str(err), err.where)]) from None
                if v < 0:
                    raise EngineError([Issue.block("offset_negative", f"{o.id} 的已发金额为负：{v}", e.id)])
                offset += v.quantize(CENT, ROUND_HALF_UP)
            final = sum((Decimal(p.final) for p in ps), Decimal(0))
            held = sum((Decimal(p.held) for p in ps), Decimal(0))
            cap = sum((Decimal(p.paid) for p in ps), Decimal(0))     # 最末层自己的抵扣（如有）已先扣
            ded = min(cap, offset)
            out[e.id] = RollupResult(e.id, ent, str(k), e.label, [m.id for m in ms], str(final), str(held), str(cap),
                                     str(offset), str(ded), str(cap - ded), str(max(offset - cap, Decimal(0))))
        return out


def compute(rs: RuleSet, data: Datasets, adjustments: list[Adjustment] = (), actor_scopes=None, refs=None) -> Result:
    return Engine(rs, data, refs).compute(adjustments, actor_scopes)
