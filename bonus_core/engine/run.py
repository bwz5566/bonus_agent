"""引擎入口：compute(规则, 数据, 调整) -> Result。

同样的输入必然得到同样的结果；结果里记录输入哈希、规则版本和引擎版本用于自证。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from bonus_core.data.population import prepare as apply_patches
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
    totals: list[tuple[str, str]] = field(default_factory=list)          # 汇总核算：(名称, 金额)，按 report.totals 的顺序
    people: list[dict] = field(default_factory=list)                     # 分配人员金额：{name, final, held, paid, holds{条件: 金额}}
    tier_stats: dict[str, dict] = field(default_factory=dict)            # 分档表实际取值分布（程序检查用，不进输出）
    coverage: list[dict] = field(default_factory=list)                   # 条件分支、查找表的命中分布（程序检查用，不进输出）
    rounding: list[dict] = field(default_factory=list)
    auxiliary: dict[str, dict] = field(default_factory=dict)
    details: dict[str, dict] = field(default_factory=dict)
    rules_hash: str = ""

    def payout_rows(self) -> list:
        """发放的最终口径：有按实体汇总的抵扣时用汇总行，否则用最末层。"""
        return list(self.rollups.values()) if self.rollups else list(self.payouts.values())

    def node(self, node_id: str) -> NodeResult:
        return self.nodes[node_id]

    def amount(self, node_id: str) -> Decimal:
        return Decimal(self.nodes[node_id].final)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("tier_stats", None)
        d.pop("coverage", None)
        return json.loads(json.dumps(d, default=str, ensure_ascii=False))


def rule_fingerprint(rs: RuleSet) -> str:
    from pydantic import BaseModel
    def clean(value):
        if isinstance(value,BaseModel):
            return {k:clean(getattr(value,k)) for k in type(value).model_fields
                    if k not in {'sources','desc','confidence','confirmed_by'} and not (isinstance(value,RuleSet) and k=='name')}
        if isinstance(value,dict):return {k:clean(v) for k,v in value.items()}
        if isinstance(value,(list,tuple)):return [clean(v) for v in value]
        return value
    return hashlib.sha256(json.dumps(clean(rs),sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


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

    def __init__(self, rs: RuleSet, data: Datasets, refs: dict | None = None, period=None):
        issues = [i for i in validate(rs) if i.severity == "blocking"]
        if issues:
            raise EngineError(issues)
        self.rs = rs
        try:
            data, patched = apply_patches(rs, data)
        except ValueError as e:
            raise EngineError([Issue.block("population", str(e))]) from None
        self.data = data
        root, by_level, issues = build_tree(rs, data)
        issues = patched + issues
        blocking = [i for i in issues if i.severity == "blocking"]
        if blocking:
            raise EngineError(blocking)
        self.root, self.by_level, self.issues = root, by_level, issues
        self.refs = refs or {}
        self.period = period
        self.ev = Evaluator(rs, root, by_level, refs, period)
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
        if self.pool_exact < 0 and rs.pool.negative != "allow":
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
            if v < 0 and rs.pool.negative != "allow":
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
        # Reports also evaluate financial expressions: capture their traces before taking a snapshot.
        totals, people = self._report_totals(), self._report_people(payouts)
        details = self._report_details()
        inputs = self._sheet_inputs()
        auxiliary = self._auxiliary_inputs()
        return Result(
            rule_set=self.rs.name, rule_version=self.rs.version, engine_version=ENGINE_VERSION,
            input_hash=run_hash(self.hash, active, self.refs), data_hash=self.hash, pool_exact=str(self.pool_exact), pool=str(self.root.final),
            levels=[lv.level for lv in self.rs.allocation], nodes=nodes, payouts=payouts,
            traces=dict(self.ev.traces), adjustments=adjustments, issues=list(self.issues), rollups=rollups,
            inputs=inputs, tier_stats=dict(self.ev.tier_stats),
            totals=totals, people=people,
            coverage=self.ev.coverage(),
            rounding=list(getattr(self,'rounding_checks',[])), auxiliary=auxiliary,
            details=details, rules_hash=rule_fingerprint(self.rs),
        )

    def _report_nodes(self, level):
        if level == 'global':
            return [self.root]
        return self.by_level.get(level) or list((self.root.index or {}).get(level,{}).values())

    def _report_details(self):
        out={}
        for item in (self.rs.report.details if self.rs.report else []):
            nodes=self._report_nodes(item.level)
            if not nodes or not item.sources:
                raise EngineError([Issue.block('report_detail_scope','对象金额报告缺少可验证对象或原文依据',item.name)])
            out[item.name]={n.id:{'level':item.level,'key':str(n.key or n.id),'name':n.label,
                'amount':str(round_pool(self.ev.evaluate(item.expr,n,f'{n.id}/明细核算/{item.name}','report.details')))} for n in nodes}
        return out

    def _report_totals(self) -> list[tuple[str, str]]:
        """汇总核算：每一行在指定层的每个对象上算一遍再合计，保留到分。"""
        rep = self.rs.report
        if rep:
            self._check_distributions(rep)
        out = []
        for balance in (rep.balances if rep else []):
            nodes = self._report_nodes(balance.level)
            if not nodes:
                raise EngineError([Issue.block("report_balance_scope", f"对账「{balance.name}」没有可验证对象", balance.level)])
            if not balance.sources:
                raise EngineError([Issue.block("report_balance_source", f"对账「{balance.name}」缺少原文依据", balance.name)])
            for n in nodes:
                try:
                    values = [self.ev.evaluate(e, n, f"{n.id}/对账/{balance.name}/{i}", "report.balances")
                              for i, e in enumerate([balance.total, *balance.parts])]
                    if balance.rounding == "cent":
                        values = [round_pool(v) for v in values]
                    if values[0] != sum(values[1:], Decimal(0)):
                        raise EngineError([Issue.block("report_unbalanced", f"对账「{balance.name}」不闭合：{values[0]} != {values[1:]}", n.id)])
                except EvalError as e:
                    raise EngineError([Issue.block("report_balance_eval", str(e), n.id)]) from e
        for t in (rep.totals if rep else []):
            nodes = self._report_nodes(t.level)
            total = Decimal(0)
            for n in nodes:
                try:
                    total += self.ev.evaluate(t.expr, n, f"{n.id}/汇总/{t.name}", "report.totals")
                except EvalError as e:
                    raise EngineError([Issue.block("eval_error", f"汇总核算「{t.name}」：{e}", e.where)]) from None
            out.append((t.name, str(total.quantize(CENT, ROUND_HALF_UP))))
        return out

    def _check_distributions(self, rep):
        self.rounding_checks = []
        pending = {p.name: p for p in rep.pending}
        if len(pending) != len(rep.pending):
            raise EngineError([Issue.block("distribution_pending_duplicate", "待分配项目名称重复", "report.pending")])
        claimed = set()
        for contract in rep.distributions:
            if contract.rounding == 'after_sum':
                self._check_recipient_distribution(contract, pending, claimed, rep)
                continue
            if contract.level != self.rs.pool.scope or contract.level not in self.by_level or not contract.sources:
                raise EngineError([Issue.block("distribution_scope", "金额去向对账须在奖金池层并提供原文依据", contract.name)])
            if len(set(contract.pending)) != len(contract.pending) or claimed & set(contract.pending):
                raise EngineError([Issue.block("distribution_pending_duplicate", "待分配项目被重复计入去向对账", contract.name)])
            claimed.update(contract.pending)
            for name in contract.pending:
                if name not in pending or pending[name].level != contract.level:
                    raise EngineError([Issue.block("distribution_pending_missing", f"待分配项 {name} 不存在或层级不同", contract.name)])
            for node in self.by_level[contract.level]:
                total = self.ev.evaluate(contract.total, node, f"{node.id}/去向/{contract.name}/总额", "report.distributions")
                waiting = sum((self.ev.evaluate(pending[name].expr, node, f"{node.id}/待分配/{name}", "report.pending")
                               for name in contract.pending), Decimal(0))
                # Compare exact rule entitlement, before largest-remainder allocation/adjustments.
                allocated = self.ev.evaluate(self.rs.pool.expr, node, f"{node.id}/去向/{contract.name}/分配池", "pool")
                if round_pool(total) != round_pool(allocated + waiting):
                    raise EngineError([Issue.block("distribution_unbalanced",
                        f"金额去向不闭合：应分配 {total} != 分配池 {allocated} + 已列示待分配 {waiting}；"
                        "不能把无名单对象的分配池归零，须修复关联或按原文列示待分配", node.id)])
        if rep.distributions and set(pending) - claimed:
            raise EngineError([Issue.block("distribution_pending_unclaimed", "待分配项未纳入金额去向对账：" +
                                           '、'.join(sorted(set(pending) - claimed)), 'report.distributions')])

    def _check_recipient_distribution(self, contract, pending, claimed, rep):
        """A declared recipient settlement keeps rounding effects visible, never reallocates them."""
        fail = lambda code,message: EngineError([Issue.block(code,message,contract.name)])
        if contract.level != 'global' or self.rs.pool.scope != self.rs.leaf.level or not rep.people or not contract.sources:
            raise fail('recipient_rounding_scope','汇总后舍入须以人员为奖金池及最末层，在 global 对账并提供原文依据')
        names=[]
        raw=Decimal(0)
        for node in self.by_level[self.rs.pool.scope]:
            name=self.ev._eval_traced(rep.people,node,f'{node.id}/舍入人员','report.people').v
            if name in (None,''):
                raise fail('recipient_rounding_person','汇总舍入的人员标识不能为空')
            names.append(str(name))
            raw += Decimal(self.ev.traces[f'{node.id}/奖金池'].value)
        if len(set(names)) != len(names):
            raise fail('recipient_rounding_duplicate','同一人员有多个奖金池；须先按人员聚合后再舍入，不能逐条记录舍入')
        if len(set(contract.pending)) != len(contract.pending) or claimed & set(contract.pending):
            raise fail('distribution_pending_duplicate','待分配项被重复计入去向对账')
        claimed.update(contract.pending)
        waiting=Decimal(0); rounded_waiting=Decimal(0)
        for name in contract.pending:
            if name not in pending:
                raise fail('distribution_pending_missing',f'待分配项 {name} 不存在')
            item=pending[name]; nodes=self._report_nodes(item.level)
            if not nodes or not item.sources:
                raise fail('distribution_pending_missing',f'待分配项 {name} 缺少对象或原文依据')
            value=sum((self.ev.evaluate(item.expr,n,f'{n.id}/待分配/{name}','report.pending') for n in nodes),Decimal(0))
            waiting += value; rounded_waiting += round_pool(value)
        total=self.ev.evaluate(contract.total,self.root,f'{self.root.id}/去向/{contract.name}/总额','report.distributions')
        if abs(total-raw-waiting) > Decimal('0.000000000001'):
            raise fail('distribution_unbalanced',f'舍入前金额去向不闭合：{total} != 人员原值 {raw} + 待分配原值 {waiting}')
        allocated=sum((n.final for n in self.by_level[self.rs.pool.scope]),Decimal(0))
        residual=round_pool(total)-allocated-rounded_waiting
        self.rounding_checks.append({'name':contract.name,'mode':'after_sum','raw_total':str(total),
            'raw_allocated':str(raw),'raw_pending':str(waiting),'total':str(round_pool(total)),
            'allocated':str(allocated),'pending':str(rounded_waiting),'residual':str(residual)})

    def _report_people(self, payouts: dict[str, PayoutResult]) -> list[dict]:
        """分配人员金额：最末层每个对象按 report.people 的取值（人名）合计应得、暂缓、本次实发。"""
        rep = self.rs.report
        if not rep or not rep.people:
            return []
        out: dict[str, dict] = {}
        for n in self.by_level[self.rs.leaf.level]:
            try:
                who = self.ev._name(rep.people, Ctx(n, where=f"{n.id}/{rep.people}")).v
            except EvalError as e:
                raise EngineError([Issue.block("eval_error", f"分配人员金额按 {rep.people} 汇总：{e}", e.where)]) from None
            who = "（空）" if who in (None, "") else str(who)
            p = payouts.get(n.id)
            o = out.setdefault(who, {"name": who, "final": Decimal(0), "held": Decimal(0), "paid": Decimal(0), "holds": {}, "count": 0})
            o["final"] += n.final
            o["count"] += 1
            if p is not None:
                o["held"] += Decimal(p.held)
                o["paid"] += Decimal(p.paid)
                for c, h in p.holds.items():
                    o["holds"][c] = o["holds"].get(c, Decimal(0)) + Decimal(h)
        rows = [{**o, "final": str(o["final"]), "held": str(o["held"]), "paid": str(o["paid"]),
                 "holds": {c: str(v) for c, v in o["holds"].items()}} for o in out.values()]
        for pending in rep.pending:
            if pending.name in out:
                raise EngineError([Issue.block("report_pending_name", f"待分配名称「{pending.name}」与人员重复", pending.name)])
            nodes = self._report_nodes(pending.level)
            if not nodes or not pending.sources:
                raise EngineError([Issue.block("report_pending_source", f"待分配「{pending.name}」缺少对象或原文依据", pending.name)])
            amount = sum((self.ev.evaluate(pending.expr, n, f"{n.id}/待分配/{pending.name}", "report.pending") for n in nodes), Decimal(0))
            rows.append({"name": pending.name, "final": str(round_pool(amount)), "held": "0.00", "paid": "0.00",
                         "holds": {}, "count": 0, "pending": True})
        return rows

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

    def _auxiliary_inputs(self):
        out={}
        for scope,index in (self.root.index or {}).items():
            variables=[name for name,v in self.rs.variables.items() if v.scope==scope]
            for node in index.values():
                computed={name:self.ev.traces[f'{node.id}/{name}'].value for name in variables
                          if f'{node.id}/{name}' in self.ev.traces}
                if not computed:
                    continue
                values=dict(node.attrs)
                for name,value in computed.items():
                    try:
                        number=Decimal(value)
                        values[name]=number if number.is_finite() else value
                    except (ArithmeticError,ValueError,TypeError):
                        values[name]=value
                out[node.id]={'level':scope,'key':node.key,'name':node.label,'source':str(node.attr_src),'values':values}
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
            ded = min(max(remaining, Decimal(0)), offset)   # 应得为负（规则允许负数照算）时没有可抵扣的，负数原样带到本次实发
            paid, over = (remaining - ded).quantize(CENT), (offset - ded).quantize(CENT)
            out[n.id] = PayoutResult(n.id, str(n.final), str(held), str(paid), holds, ratios,
                                     cap=str(remaining), offset=str(offset), overpaid=str(over),
                                     deducted=str(ded))
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
            # 最末层自己也有抵扣时（如参与记录扣一次、人员再扣一次），汇总表要把两层都列上，账才对得上：
            # 可发上限 = 最末层可发上限之和；已发抵扣、实际抵扣、超发 = 最末层的 + 本层的；本次实发 = 可发上限 − 实际抵扣
            cap = sum((Decimal(p.cap or p.paid) for p in ps), Decimal(0))
            left = sum((Decimal(p.paid) for p in ps), Decimal(0))     # 最末层抵扣后剩下的
            ded = min(max(left, Decimal(0)), offset)
            leaf_off = sum((Decimal(p.offset or 0) for p in ps), Decimal(0))
            leaf_ded = sum((Decimal(p.deducted or 0) for p in ps), Decimal(0))
            leaf_over = sum((Decimal(p.overpaid or 0) for p in ps), Decimal(0))
            out[e.id] = RollupResult(e.id, ent, str(k), e.label, [m.id for m in ms], str(final), str(held), str(cap),
                                     str(leaf_off + offset), str(leaf_ded + ded), str(left - ded),
                                      str((leaf_over + offset - ded).quantize(CENT)))
        return out


def compute(rs: RuleSet, data: Datasets, adjustments: list[Adjustment] = (), actor_scopes=None, refs=None, period=None) -> Result:
    return Engine(rs, data, refs, period).compute(adjustments, actor_scopes)
