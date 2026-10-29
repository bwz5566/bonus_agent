"""规则集的静态校验：提交草稿和确认版本时都会运行。"""
from __future__ import annotations

from graphlib import CycleError, TopologicalSorter

from bonus_core.dsl.functions import FUNCTIONS
from bonus_core.dsl.models import RuleSet, TierTable
from bonus_core.dsl.parser import BinOp, BoolOp, Call, Cmp, ExprError, Name, Neg, Not, Str, names, parse, walk
from bonus_core.dsl.scope import Layout, is_aggregate
from bonus_core.engine.issues import Issue


def _exprs(rs: RuleSet):
    """(位置, 表达式, 求值层级或关联实体表名) 列表。None 表示全局。"""
    for n, v in rs.variables.items():
        yield f"variables.{n}", v.expr, None if v.scope == "global" else v.scope
    yield "pool", rs.pool.expr, None if rs.pool.scope == "global" else rs.pool.scope
    for lv in _weighted(rs):
        yield f"allocation.{lv.level}.weight", lv.weight, lv.level
    for p in rs.payout:
        for k in ("hold_ratio", "pay_ratio"):
            if getattr(p, k).strip():
                yield f"payout.{p.id}.{k}", getattr(p, k), p.scope
    for o in rs.offsets:
        yield f"offsets.{o.id}", o.expr, o.scope
    for c in rs.checks:
        yield f"checks.{c.id}", c.expr, None if c.scope == "global" else c.scope


def _weighted(rs: RuleSet):
    """需要权重的层级：奖金池按层计算时，该层及以上的金额由规则直接算出，不需要权重。"""
    start = next((i + 1 for i, lv in enumerate(rs.allocation) if lv.level == rs.pool.scope), 0)
    return rs.allocation[start:]


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
                issues.append(Issue.block("parent_key_missing", f"实体表「{ent[0]}」没有字段 {lv.parent_key}"
                                          + ("。如果一个对象对应上一层的多条记录（如一人参与多个项目），不要把它放在下一层："
                                             "最末层用那张记录表，需要按人汇总抵扣时把 offsets 的 scope 写成人员表"
                                             if i == len(rs.allocation) - 1 else ""), f"allocation.{lv.level}"))
            elif lv.parent_key != rs.allocation[i - 1].key:
                issues.append(Issue.warn("parent_key_name", f"parent_key {lv.parent_key} 与上一层主键 {rs.allocation[i - 1].key} 名字不同，请确认", f"allocation.{lv.level}"))
    if rs.allocation and rs.entity_dataset(rs.leaf.key) is None:
        issues.append(Issue.block("leaf_no_entity", f"最末层「{rs.leaf.level}」必须有实体表"))
    lay = Layout(rs)
    level_keys = {lv.key for lv in rs.allocation}
    for n, d in rs.datasets.items():
        if d.key and d.link and d.key in level_keys:
            issues.append(Issue.block("dataset_kind", f"层级实体表「{n}」不用写 link，上级用分配层级的 parent_key 表示", f"datasets.{n}"))
        for k in (d.key, d.link):
            if k and k not in d.fields:
                issues.append(Issue.block("dataset_key_field", f"数据表「{n}」的字段列表里没有 {k}", f"datasets.{n}"))
        if lay.parent[n][0] == "bad":
            issues.append(Issue.block("link_bad", f"「{n}」的 link={d.link} 既不是分配层级的主键，也不是关联实体表的主键", f"datasets.{n}"))
        if n in lay.aux and n in lay.index:
            issues.append(Issue.block("name_clash", f"关联实体表名「{n}」与分配层级重名", f"datasets.{n}"))
        dup = [o for o, od in rs.datasets.items() if o < n and od.key and od.key == d.key]
        if dup:
            issues.append(Issue.block("key_shared", f"「{n}」和「{dup[0]}」的主键都是 {d.key}；一个编号只能有一张实体表，"
                                      f"其他表作为明细表用 link 关联", f"datasets.{n}"))

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
        if v.scope != "global" and v.scope not in levels and v.scope not in lay.aux:
            issues.append(Issue.block("scope_bad", f"变量 {n} 的 scope「{v.scope}」不是 global、分配层级或关联实体表名", f"variables.{n}"))
        if n in all_fields:
            issues.append(Issue.block("name_clash", f"变量 {n} 与数据字段重名", f"variables.{n}"))
    if rs.pool.scope != "global" and rs.pool.scope not in levels:
        issues.append(Issue.block("scope_bad", f"pool 的 scope「{rs.pool.scope}」不是 global 或分配层级", "pool"))
    for lv in _weighted(rs):
        if not lv.weight.strip():
            issues.append(Issue.block("weight_missing", f"层级「{lv.level}」需要权重表达式 weight", f"allocation.{lv.level}"))
    for p in rs.patches:                         # 数据修订：表和字段都要存在
        p_w = f"patches.{p.source}"
        p_names = [p.source, p.into] if p.kind == "append" else [p.source, p.target]
        p_miss = [n for n in p_names if n not in rs.datasets]
        if p_miss:
            issues.append(Issue.block("patch_dataset", f"数据修订引用的数据表 {p_miss} 不存在", p_w))
            continue
        p_src, p_dst = rs.datasets[p_names[0]], rs.datasets[p_names[1]]
        if p.kind == "append":
            p_u = p.unique or p_dst.key
            if not p_u:
                issues.append(Issue.block("patch_field", f"并入「{p.into}」要写 unique（去重用的单号字段）", p_w))
            elif p_u not in p_src.fields or p_u not in p_dst.fields:
                issues.append(Issue.block("patch_field", f"去重字段 {p_u} 要在「{p.source}」和「{p.into}」里都有（补充表按原表的字段名定义）", p_w))
            p_lost = [f for f in p_dst.fields if f not in p_src.fields]
            if p_lost:
                issues.append(Issue.warn("patch_field", f"「{p.source}」没有「{p.into}」的字段 {p_lost}，并入的行这些字段为空", p_w))
            p_bad = [f for f in p.values if f not in p_dst.fields]
            if p_bad:
                issues.append(Issue.block("patch_field", f"values 里的字段 {p_bad} 不在「{p.into}」里", p_w))
        else:
            if p.key not in p_src.fields or p.key not in p_dst.fields:
                issues.append(Issue.block("patch_field", f"编号字段 {p.key} 要在「{p.source}」和「{p.target}」里都有", p_w))
            if bool(p.field) == bool(p.set):
                issues.append(Issue.block("patch_field", "更正要在 field + value（长表：更正字段列、新值列）和 set（宽表）里写且只写一种", p_w))
            p_cols = [c for c in [p.field, p.value] if c] + list(p.set.values()) + list(p.expect.values())
            if p.field and not p.value:
                issues.append(Issue.block("patch_field", "长表更正要同时写 value（新值所在的字段）", p_w))
            p_bad = [c for c in p_cols if c not in p_src.fields]
            if p_bad:
                issues.append(Issue.block("patch_field", f"字段 {p_bad} 不在「{p.source}」里", p_w))
            p_bad = [f for f in list(p.set) + list(p.expect) + list(p.labels.values()) if f not in p_dst.fields]
            if p_bad:
                issues.append(Issue.block("patch_field", f"字段 {p_bad} 不在「{p.target}」里", p_w))
    for c in rs.checks:
        if c.scope != "global" and c.scope not in levels and c.scope not in lay.aux:
            issues.append(Issue.block("scope_bad", f"校验 {c.id} 的 scope「{c.scope}」不是 global、分配层级或关联实体表名", f"checks.{c.id}"))
    # 编号重复：发放条件按编号记台账，重复会互相覆盖、漏记
    for what, items in (("发放条件", rs.payout), ("抵扣", rs.offsets), ("校验", rs.checks),
                        ("分配层级", [type("L", (), {"id": lv.level}) for lv in rs.allocation])):
        seen: set[str] = set()
        for it in items:
            if it.id in seen:
                issues.append(Issue.block("id_duplicate", f"{what}编号「{it.id}」重复，每条要用不同的编号", it.id))
            seen.add(it.id)
    for p in rs.payout:
        if rs.allocation and p.scope != rs.leaf.level:
            issues.append(Issue.block("payout_scope", f"发放条件 {p.id} 目前只支持最末层「{rs.leaf.level}」", f"payout.{p.id}"))
        if bool(p.hold_ratio.strip()) == bool(p.pay_ratio.strip()):
            issues.append(Issue.block("payout_ratio", f"发放条件 {p.id} 要在 hold_ratio（挂起比例）和 pay_ratio（可发比例）中写且只写一个", f"payout.{p.id}"))
    leaf_ds = rs.entity_dataset(rs.leaf.key) if rs.allocation else None
    rolled = set()
    for o in rs.offsets:
        if not rs.allocation or o.scope == rs.leaf.level:
            continue
        ent = rs.datasets.get(o.scope)
        if o.scope in lay.aux and ent is not None and leaf_ds is not None and ent.key in leaf_ds[1].fields:
            rolled.add(o.scope)       # 最末层按该实体编号汇总后再抵扣
            continue
        issues.append(Issue.block("payout_scope", f"抵扣 {o.id} 的 scope 要写最末层「{rs.leaf.level}」，或者最末层数据表"
                                  f"（{leaf_ds[0] if leaf_ds else '?'}）里有编号字段的关联实体表（按该实体汇总后再抵扣，如一人参与多个项目）",
                                  f"offsets.{o.id}"))
    if len(rolled) > 1:
        issues.append(Issue.block("payout_scope", f"抵扣只能汇总到一个关联实体，现在有 {sorted(rolled)}", "offsets"))

    # ---- 表达式 ----
    deps: dict[str, set[str]] = {}
    known = set(rs.variables) | set(rs.tables) | all_fields
    for where, src, scope in _exprs(rs):
        if scope is not None and scope not in levels and scope not in lay.aux:
            continue   # scope 不对已在上面报过
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
                if node.fn == "scheme_paid" and not all(isinstance(a, Str) for a in node.args):
                    issues.append(Issue.block("fn_args", 'scheme_paid 的参数是方案名和修订名字符串，如 scheme_paid("QUARTER", "R2")', where))
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
    """静态检查表达式在所处位置能否求值，规则与求值器一致（见 bonus_core.dsl.scope）：

    - 层级变量只能在本层或更下层引用（上层要用下层的数，只能对数据表聚合）；
    - 层级实体表字段只能在其所属层级或更下层直接使用，上层要用就聚合；
    - 明细表、公司级表的字段只能出现在聚合函数里，且表的行必须挂在当前层或更下层（否则聚合不到行）；
    - 关联实体（如项目）的字段和变量，只能在该表的聚合里、该实体作用域里、或带有它编号的表的行里使用。
    """

    def __init__(self, rs: RuleSet, where: str):
        self.rs, self.where = rs, where
        self.lay = Layout(rs)
        self.issues: list[Issue] = []
        self.seen: set[str] = set()

    def run(self, ast, scope: str | None) -> list[Issue]:
        if scope in self.lay.aux:
            self._visit(ast, self.lay.table_level[scope], scope, None)
        else:
            self._visit(ast, -1 if scope is None else self.lay.index.get(scope, -1), None, None)
        return self.issues

    def _block(self, code: str, msg: str):
        if msg not in self.seen:
            self.seen.add(msg)
            self.issues.append(Issue.block(code, msg, self.where))

    def _global_total(self, n: str, ctx: int):
        """全局变量在某层引用时是全公司一个数；它聚合的数据表在这一层每个对象都有自己的行，多半是漏写了 scope。"""
        lab = self.lay.level_label
        own = sorted(ds for ds in _global_aggs(self.rs.variables[n].expr, self.lay)
                     if ds not in self.lay.aux and self.lay.table_level.get(ds, -1) >= ctx)
        if own:
            msg = (f"变量 {n} 没写 scope（全局，算的是全公司所有行的合计），却在「{lab(ctx)}」层引用；"
                   f"它聚合的「{'、'.join(own)}」在「{lab(ctx)}」层每个对象都有自己的行。"
                   f"如果要每个对象自己的值，给 {n} 写 scope: {lab(ctx)}；确实要全公司合计才保留")
            if msg not in self.seen:
                self.seen.add(msg)
                self.issues.append(Issue.warn("global_in_level", msg, self.where))

    def _visit(self, node, ctx: int, ent: str | None, agg: str | None):
        lab = self.lay.level_label
        match node:
            case Name(id=n):
                self._name(n, ctx, ent, agg)
            case Call(args=args, kwargs=kws) if is_aggregate(node):
                ds, err = self.lay.agg_dataset(node)
                if ds is None:
                    self._block("agg_dataset", err)
                    return
                if ent is not None:
                    if ds != ent and ent not in self.lay.reach(ds):
                        self._block("agg_unrelated", f"在「{ent}」作用域聚合「{ds}」：这张表的行没有{ent}的编号，"
                                                     f"聚合不到属于本{ent}的行")
                else:
                    tl = self.lay.table_level[ds]
                    if tl < ctx:
                        d = self.rs.datasets[ds]
                        key = self.rs.allocation[ctx].key if 0 <= ctx < len(self.rs.allocation) else None
                        if key and key in d.fields and d.key != key:
                            fix = (f"「{ds}」有 {key} 字段：多半是数据表挂错了层，用 define_dataset 重新定义「{ds}」并写 link={key}，"
                                   f"挂到「{lab(ctx)}」层（行里的其他编号仍能取到对应实体的字段和变量）；不要把计算移到上层")
                        else:
                            ents = self.lay.reach(ds) | ({ds} if ds in self.lay.aux else set())
                            bridge = [(n, e) for n, dd in self.rs.datasets.items() if key and dd.link == key
                                      for e in sorted(self.lay.reach(n) & ents)]
                            if bridge:
                                n, e = bridge[0]
                                fix = (f"「{ds}」的行能关联到「{e}」，而「{n}」的每行属于「{lab(ctx)}」层又引用「{e}」："
                                       f"先定义 scope={e} 的变量（如 x = sum(..., of: \"{ds}\", where: ...)），"
                                       f"再在「{lab(ctx)}」层写 sum(x * 比例字段, of: \"{n}\")；不要把计算移到上层")
                            else:
                                fix = (f"如果「{ds}」的行应属于「{lab(ctx)}」层的对象，给它加上该层的编号字段并写 link；"
                                       f"确实是上层的数据，才把这个计算放到「{lab(tl)}」层或更上层")
                        self._block("agg_above", f"{node.fn}(...) 聚合的数据表「{ds}」挂在「{lab(tl)}」层，在「{lab(ctx)}」层"
                                                 f"聚合不到任何行。{fix}")
                inner = ds if ds in self.lay.aux else ent
                for a in list(args) + [v for k, v in kws if k == "where"]:
                    self._visit(a, ctx, inner, ds)
            case Call(args=args, kwargs=kws):
                for a in list(args) + [v for k, v in kws]:
                    self._visit(a, ctx, ent, agg)
            case BinOp(left=l, right=r) | Cmp(left=l, right=r) | BoolOp(left=l, right=r):
                self._visit(l, ctx, ent, agg)
                self._visit(r, ctx, ent, agg)
            case Not(operand=o) | Neg(operand=o):
                self._visit(o, ctx, ent, agg)

    def _name(self, n: str, ctx: int, ent: str | None, agg: str | None):
        rs, lay, lab = self.rs, self.lay, self.lay.level_label
        if agg is not None and lay.visible(n, agg):
            return
        if ent is not None and lay.visible(n, ent):
            return
        if agg is None and ent is None:          # 层级对象自己行上的编号引用到的关联实体（如参与记录的 emp_id → 人员）
            for lv in rs.allocation[:ctx + 1] if ctx >= 0 else []:
                ed = rs.entity_dataset(lv.key)
                if ed and lay.visible(n, ed[0]):
                    return
        sc = lay.aux_var_scope(n)
        owners = lay.row_only.get(n, set())
        if sc is not None or (owners and owners <= set(lay.aux)):    # 关联实体的字段或变量
            e = sc or sorted(owners)[0]
            key = rs.datasets[e].key
            what = f"变量 {n} 属于关联实体「{e}」" if sc else f"{n} 是关联实体「{e}」的字段"
            self._block("scope_aux", f"{what}，只能在「{e}」作用域的变量里、对「{e}」的聚合里（如 sum({n}, of: \"{e}\")），"
                                     f"或带有 {key} 的表的聚合里使用")
            return
        if n in rs.variables:
            vs = rs.variables[n].scope
            si = -1 if vs == "global" else lay.index.get(vs, -1)
            if si > ctx:
                self._block("scope_var", f"变量 {n} 属于「{vs}」层级，不能在「{lab(ctx)}」层引用。"
                                         f"上层需要下层的数时，请在上层直接对数据表聚合（如 sum(...)），不要引用下层变量"
                                         + (f"；如果奖金池是每个「{vs}」各自计提的，把 pool 的 scope 写成「{vs}」"
                                            if ctx < 0 and vs in lay.index else ""))
            elif vs == "global" and ctx >= 0:
                self._global_total(n, ctx)
            return
        if n in rs.tables:
            return
        if n in lay.entity_level and lay.entity_level[n] <= ctx:
            return
        if n in lay.row_only and agg is not None:
            own = "、".join(sorted(lay.row_only[n]))
            self._block("detail_outside_agg", f"正在对「{agg}」聚合，但 {n} 是「{own}」的字段，「{agg}」的行里取不到它。"
                                              f"先在被引用的实体上定义变量（如 scope=项目 的 x = sum({n}, of: \"{own}\", where: ...)），"
                                              f"再在「{agg}」的聚合里用这个变量（如 sum(x * share, of: \"{agg}\")）")
        elif n in lay.row_only:
            self._block("detail_outside_agg", f"{n} 是明细表字段（每行一个值），必须放在 sum/avg/count 等聚合函数里使用；"
                                              f"逐行乘系数要写在聚合里面，如 sum({n} * lookup(表, 字段))")
        elif n in lay.entity_level:
            self._block("scope_field", f"字段 {n} 属于「{lab(lay.entity_level[n])}」层的实体表，不能在「{lab(ctx)}」层直接使用，"
                                       f"需要时用聚合，如 sum({n})")


def _global_aggs(src: str, lay: Layout) -> set[str]:
    try:
        ast = parse(src)
    except ExprError:
        return set()
    return {ds for node in walk(ast) if isinstance(node, Call) and is_aggregate(node)
            for ds in [lay.agg_dataset(node)[0]] if ds is not None}


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
    for o in rs.offsets:
        yield f"offsets.{o.id}", o
    for c in rs.checks:
        yield f"checks.{c.id}", c


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
