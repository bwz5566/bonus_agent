"""规则集的静态校验：提交草稿和确认版本时都会运行。"""
from __future__ import annotations

from graphlib import CycleError, TopologicalSorter

from bonus_core.dsl.functions import FUNCTIONS
from bonus_core.dsl.models import RuleSet, TierTable
from bonus_core.dsl.parser import BinOp, BoolOp, Call, Cmp, ExprError, Name, Neg, Not, Str, names, parse, walk
from bonus_core.dsl.scope import Layout, is_aggregate
from bonus_core.engine.issues import Issue


def _exprs(rs: RuleSet):
    """(位置, 表达式, 求值层级) 列表。层级 None 表示全局。"""
    for n, v in rs.variables.items():
        yield f"variables.{n}", v.expr, None if v.scope == "global" else v.scope
    yield "pool", rs.pool.expr, None
    for lv in rs.allocation:
        yield f"allocation.{lv.level}.weight", lv.weight, lv.level
    for p in rs.payout:
        yield f"payout.{p.id}.hold_ratio", p.hold_ratio, p.scope


def validate(rs: RuleSet) -> list[Issue]:
    issues: list[Issue] = []
    levels = [lv.level for lv in rs.allocation]
    all_fields = set(rs.field_owner())
    kw_names = set()

    # ---- 数据集与分配层级 ----
    if not rs.allocation:
        issues.append(Issue.block("no_levels", "至少需要一个分配层级"))
    for i, lv in enumerate(rs.allocation):
        ent = rs.entity_dataset(lv.key)
        if i > 0:
            if ent is None:
                issues.append(Issue.block("level_no_entity", f"层级「{lv.level}」需要一张 key={lv.key} 的实体表", f"allocation.{lv.level}"))
            if not lv.parent_key:
                issues.append(Issue.block("level_no_parent", f"层级「{lv.level}」需要 parent_key 指向上一层", f"allocation.{lv.level}"))
            elif ent and lv.parent_key not in ent[1].fields:
                issues.append(Issue.block("parent_key_missing", f"实体表「{ent[0]}」没有字段 {lv.parent_key}", f"allocation.{lv.level}"))
            elif lv.parent_key != rs.allocation[i - 1].key:
                issues.append(Issue.warn("parent_key_name", f"parent_key {lv.parent_key} 与上一层主键 {rs.allocation[i - 1].key} 名字不同，请确认", f"allocation.{lv.level}"))
    if rs.allocation and rs.entity_dataset(rs.leaf.key) is None:
        issues.append(Issue.block("leaf_no_entity", f"最末层「{rs.leaf.level}」必须有实体表"))
    for n, d in rs.datasets.items():
        if d.key and d.link:
            issues.append(Issue.block("dataset_kind", f"数据表「{n}」不能同时设置 key 和 link", f"datasets.{n}"))
        if d.key and d.key not in {lv.key for lv in rs.allocation}:
            issues.append(Issue.warn("entity_unused", f"实体表「{n}」的主键 {d.key} 不是任何分配层级的主键，表中数据不会被使用", f"datasets.{n}"))
        for k in (d.key, d.link):
            if k and k not in d.fields:
                issues.append(Issue.block("dataset_key_field", f"数据表「{n}」的字段列表里没有 {k}", f"datasets.{n}"))
        if d.link and d.link not in {lv.key for lv in rs.allocation}:
            issues.append(Issue.block("link_bad", f"明细表「{n}」的 link={d.link} 不是任何层级的主键", f"datasets.{n}"))

    # ---- 参数表 ----
    for n, t in rs.tables.items():
        if isinstance(t, TierTable):
            lows = [lo for lo, _ in t.steps]
            if not lows:
                issues.append(Issue.block("tier_empty", f"阶梯表 {n} 没有档位", f"tables.{n}"))
            elif any(b <= a for a, b in zip(lows, lows[1:])):
                issues.append(Issue.block("tier_order", f"阶梯表 {n} 的下限必须严格递增", f"tables.{n}"))
        if n in all_fields or n in rs.variables:
            issues.append(Issue.block("name_clash", f"参数表名 {n} 与字段或变量重名", f"tables.{n}"))

    # ---- 变量作用域 ----
    for n, v in rs.variables.items():
        if v.scope != "global" and v.scope not in levels:
            issues.append(Issue.block("scope_bad", f"变量 {n} 的 scope「{v.scope}」不是 global 或已定义层级", f"variables.{n}"))
        if n in all_fields:
            issues.append(Issue.block("name_clash", f"变量 {n} 与数据字段重名", f"variables.{n}"))
    for p in rs.payout:
        if rs.allocation and p.scope != rs.leaf.level:
            issues.append(Issue.block("payout_scope", f"发放条件 {p.id} 目前只支持最末层「{rs.leaf.level}」", f"payout.{p.id}"))

    # ---- 表达式 ----
    deps: dict[str, set[str]] = {}
    known = set(rs.variables) | set(rs.tables) | all_fields
    for where, src, scope in _exprs(rs):
        try:
            ast = parse(src)
        except ExprError as e:
            issues.append(Issue.block("syntax", str(e), where))
            continue
        for node in walk(ast):
            if isinstance(node, Call):
                spec = FUNCTIONS.get(node.fn)
                if spec is None:
                    issues.append(Issue.block("fn_unknown", f"不支持的函数 {node.fn}", where))
                    continue
                if not spec.min_args <= len(node.args) <= spec.max_args:
                    issues.append(Issue.block("fn_args", f"{node.fn} 需要 {spec.min_args}~{spec.max_args} 个参数，实际 {len(node.args)} 个", where))
                for k, val in node.kwargs:
                    kw_names.add(k)
                    if k not in spec.kwargs:
                        issues.append(Issue.block("fn_kwarg", f"{node.fn} 不支持参数 {k}", where))
                    if k == "of" and not (isinstance(val, Str) and val.value in rs.datasets):
                        issues.append(Issue.block("fn_of", f"{node.fn} 的 of 必须是数据表名字符串", where))
                if node.fn in ("tier", "tier_rate", "lookup") and node.args:
                    a0 = node.args[0]
                    want = "tier" if node.fn != "lookup" else "lookup"
                    t = rs.tables.get(a0.id) if isinstance(a0, Name) else None
                    if t is None or t.kind != want:
                        issues.append(Issue.block("fn_table", f"{node.fn} 的第一个参数必须是{'阶梯表' if want == 'tier' else '查找表'}名", where))
        unknown = names(ast) - known
        for n in unknown:
            issues.append(Issue.block("name_unknown", f"无法识别的名字 {n}", where))
        if not unknown and levels:
            issues += _ScopeCheck(rs, where).run(ast, scope)
        if where.startswith("variables."):
            deps[where.split(".", 1)[1]] = names(ast) & set(rs.variables)

    try:
        tuple(TopologicalSorter(deps).static_order())
    except CycleError as e:
        issues.append(Issue.block("cycle", f"变量循环引用：{' -> '.join(e.args[1])}"))

    # ---- 出处 ----
    unconfirmed = [
        where for where, item in _items(rs) if item.sources == [] and item.confidence is not None
    ]
    for where in unconfirmed:
        issues.append(Issue.warn("no_source", "智能体生成的规则元素缺少原文出处", where))
    return issues


class _ScopeCheck:
    """静态检查表达式在所处层级能否求值，规则与求值器一致（见 bonus_core.dsl.scope）：

    - 层级变量只能在本层或更下层引用（上层要用下层的数，只能对数据表聚合）；
    - 实体表字段只能在其所属层级或更下层直接使用，上层要用就聚合；
    - 明细表、公司级表的字段只能出现在聚合函数里，且表的行必须挂在当前层或更下层（否则聚合不到行）。
    """

    def __init__(self, rs: RuleSet, where: str):
        self.rs, self.where = rs, where
        self.lay = Layout(rs)
        self.issues: list[Issue] = []
        self.seen: set[str] = set()

    def run(self, ast, scope: str | None) -> list[Issue]:
        self._visit(ast, -1 if scope is None else self.lay.index.get(scope, -1), None)
        return self.issues

    def _block(self, code: str, msg: str):
        if msg not in self.seen:
            self.seen.add(msg)
            self.issues.append(Issue.block(code, msg, self.where))

    def _visit(self, node, ctx: int, agg: str | None):
        lab = self.lay.level_label
        match node:
            case Name(id=n):
                self._name(n, ctx, agg)
            case Call(args=args, kwargs=kws) if is_aggregate(node):
                ds, err = self.lay.agg_dataset(node)
                if ds is None:
                    self._block("agg_dataset", err)
                    return
                tl = self.lay.table_level[ds]
                if tl < ctx:
                    self._block("agg_above", f"{node.fn}(...) 聚合的数据表「{ds}」挂在「{lab(tl)}」层，在「{lab(ctx)}」层"
                                             f"聚合不到任何行；请把这个计算放到「{lab(tl)}」层或更上层")
                for a in list(args) + [v for k, v in kws if k == "where"]:
                    self._visit(a, ctx, ds)
            case Call(args=args, kwargs=kws):
                for a in list(args) + [v for k, v in kws]:
                    self._visit(a, ctx, agg)
            case BinOp(left=l, right=r) | Cmp(left=l, right=r) | BoolOp(left=l, right=r):
                self._visit(l, ctx, agg)
                self._visit(r, ctx, agg)
            case Not(operand=o) | Neg(operand=o):
                self._visit(o, ctx, agg)

    def _name(self, n: str, ctx: int, agg: str | None):
        rs, lab = self.rs, self.lay.level_label
        if agg is not None and n in rs.datasets[agg].fields:
            return
        if n in rs.variables:
            sc = rs.variables[n].scope
            si = -1 if sc == "global" else self.lay.index.get(sc, -1)
            if si > ctx:
                self._block("scope_var", f"变量 {n} 属于「{sc}」层级，不能在「{lab(ctx)}」层引用。"
                                         f"上层需要下层的数时，请在上层直接对数据表聚合（如 sum(...)），不要引用下层变量")
            return
        if n in rs.tables:
            return
        if n in self.lay.entity_level and self.lay.entity_level[n] <= ctx:
            return
        if n in self.lay.row_only:
            self._block("detail_outside_agg", f"{n} 是明细表字段（每行一个值），必须放在 sum/avg/count 等聚合函数里使用；"
                                              f"逐行乘系数要写在聚合里面，如 sum({n} * lookup(表, 字段))")
        elif n in self.lay.entity_level:
            self._block("scope_field", f"字段 {n} 属于「{lab(self.lay.entity_level[n])}」层的实体表，不能在「{lab(ctx)}」层直接使用，"
                                       f"需要时用聚合，如 sum({n})")


def _items(rs: RuleSet):
    for n, t in rs.tables.items():
        yield f"tables.{n}", t
    for n, v in rs.variables.items():
        yield f"variables.{n}", v
    yield "pool", rs.pool
    for lv in rs.allocation:
        yield f"allocation.{lv.level}", lv
    for p in rs.payout:
        yield f"payout.{p.id}", p


def unconfirmed_items(rs: RuleSet) -> list[str]:
    return [where for where, item in _items(rs) if not item.confirmed_by]


def diff(old: RuleSet, new: RuleSet) -> list[dict]:
    """两个版本之间的结构化差异。"""
    a, b = dict(_items(old)), dict(_items(new))
    out = []
    for k in sorted(set(a) | set(b)):
        if k not in a:
            out.append({"path": k, "change": "added"})
        elif k not in b:
            out.append({"path": k, "change": "removed"})
        else:
            da = a[k].model_dump(exclude={"sources", "confidence", "confirmed_by", "desc"}, mode="json")
            db = b[k].model_dump(exclude={"sources", "confidence", "confirmed_by", "desc"}, mode="json")
            if da != db:
                out.append({"path": k, "change": "modified", "old": da, "new": db})
    return out
