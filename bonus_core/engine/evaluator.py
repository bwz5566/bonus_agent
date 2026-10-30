"""带追踪的表达式求值器。

每次求值返回值和渲染后的算式；变量、权重、奖金池级别的结果记为 Trace，
所有 Trace 构成计算链，可以从任意金额一路追到明细行和规则原文。
"""
from __future__ import annotations

import json
import operator
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext

from bonus_core.data.table import ConvertError, Row, to_bool, to_date
from bonus_core.dsl.models import LookupTable, RuleSet, TierTable
from bonus_core.dsl.parser import BinOp, BoolOp, Call, Cmp, Const, Name, Neg, Not, Num, Str, names, parse, unparse
from bonus_core.dsl.scope import Layout, is_aggregate
from bonus_core.engine.tree import Node

PREC = 28


class EvalError(ValueError):
    def __init__(self, message: str, where: str = ""):
        self.where = where
        super().__init__(message + (f"（位置：{where}）" if where else ""))


@dataclass(frozen=True)
class Trace:
    id: str
    rule_item: str
    expr: str
    rendered: str
    value: str
    inputs: tuple[str, ...] = ()
    rows: tuple[str, ...] = ()

    def to_dict(self):
        return asdict(self)


@dataclass
class Val:
    v: object
    text: str
    deps: frozenset = frozenset()
    rows: tuple = ()


@dataclass
class Ctx:
    node: Node
    row: Row | None = None
    where: str = ""


def fmt(v) -> str:
    if isinstance(v, Decimal):
        if v == v.to_integral_value():
            return f"{v.quantize(Decimal(1)):,}"
        q = v.quantize(Decimal("0.000001"), ROUND_HALF_UP).normalize()
        return f"{q:,f}"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, str):
        return f'"{v}"'
    return str(v)


def lookup_key(v) -> str:
    """查找表的键按文本比较：数字不带千分位、不带多余的 0（1000 → "1000"，1.50 → "1.5"），和 Excel 里 x&"" 的结果一致。"""
    if isinstance(v, Decimal) and not isinstance(v, bool):
        return str(v.quantize(Decimal(1))) if v == v.to_integral_value() else format(v.normalize(), "f")
    return v if isinstance(v, str) else fmt(v)


_ARITH = {"+": operator.add, "-": operator.sub, "*": operator.mul, "/": operator.truediv}
_CMP = {"==": operator.eq, "!=": operator.ne, ">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt}


def _merge(*vals: Val) -> tuple[frozenset, tuple]:
    deps = frozenset().union(*(v.deps for v in vals))
    rows = tuple(r for v in vals for r in v.rows)
    return deps, rows


class Evaluator:
    def __init__(self, rs: RuleSet, root: Node, by_level: dict[str, list[Node]], refs: dict | None = None):
        self.rs = rs
        self.refs = refs or {}          # "方案/修订" -> 同一批里已算出的其他方案结果（scheme_paid 用）
        self.root = root
        self.by_level = by_level
        self.traces: dict[str, Trace] = {}
        self._values: dict[str, object] = {}
        self._cache: dict[tuple[str, str], Val] = {}
        self._busy: set[tuple[str, str]] = set()
        # 独立验算用：对本层或上层的实体表聚合（如在个人层对「员工」表、在小组层对「小组」表求和）时，按上一层的范围取同级，
        # 配合 where 就能算“同部门各小组合计”；share(上层变量) 按变量所在的层取同级。正式计算不开，规则语义不变
        self.peer_scope = False
        # 分档表的实际取值分布：表名 -> {n, min, max, bands: {档位序号: 次数}}，程序检查用来发现传入值和分档点量纲不一致
        self.tier_stats: dict[str, dict] = {}
        # 条件分支、查找表实际走到哪个结果：(所在计算项, "if"/"lookup", 条件或表名) -> {结果: 次数}，覆盖报告用
        self.outcomes: dict[tuple, dict[str, int]] = {}
        self.layout = Layout(rs)
        # 只能在聚合里使用的字段 -> 所在的明细表/公司级表（不含主键/关联键）
        self.detail_fields = self.layout.row_only
        self.index: dict[str, dict[str, Node]] = root.index or {}
        self._row_node = {id(n.row): n for tbl in self.index.values() for n in tbl.values()}
        # 每一行所属的对象（层级对象、关联实体）：对行聚合时，行里再嵌套的聚合按这一行自己的对象算，
        # 不能沿用外层对象（否则项目层对参与记录求和时，每条记录都会读到整个项目的工时）
        self._home: dict[int, Node] = {}
        for n in [*root.walk(), *(m for tbl in self.index.values() for m in tbl.values())]:
            for rows in n.own_rows.values():
                for r in rows:
                    self._home.setdefault(id(r), n)

    # ---------------- 对外接口 ----------------
    def evaluate(self, expr: str, node: Node, trace_id: str, rule_item: str) -> Decimal:
        val = self._eval_traced(expr, node, trace_id, rule_item)
        return self._num(val, trace_id)

    def variable(self, name: str, node: Node, row: Row | None = None) -> Val:
        var = self.rs.variables[name]
        target = self._scope_node(var.scope, node, name, row)
        key = (name, target.id)
        if key in self._cache:
            return self._cache[key]
        if key in self._busy:
            raise EvalError(f"变量 {name} 存在循环引用", target.id)
        self._busy.add(key)
        try:
            val = self._eval_traced(var.expr, target, f"{target.id}/{name}", name)
        finally:
            self._busy.discard(key)
        self._cache[key] = val
        return val

    # ---------------- 内部 ----------------
    def _eval_traced(self, expr: str, node: Node, trace_id: str, rule_item: str) -> Val:
        if trace_id in self._values:
            v = self._values[trace_id]
            return Val(v, fmt(v), frozenset({trace_id}))
        with localcontext() as c:
            c.prec = PREC
            val = self.ev(parse(expr), Ctx(node, where=trace_id))
        self.traces[trace_id] = Trace(
            id=trace_id, rule_item=rule_item, expr=expr, rendered=val.text,
            value=str(val.v), inputs=tuple(sorted(val.deps)),
            rows=tuple(sorted({str(r.src) for r in val.rows if r.src})),
        )
        self._values[trace_id] = val.v
        return Val(val.v, fmt(val.v), frozenset({trace_id}))

    def _scope_node(self, scope: str, node: Node, name: str, row: Row | None = None) -> Node:
        if scope == "global":
            return self.root
        for n in node.chain():
            if n.level == scope:
                return n
        for ref in self._refs(row) if row is not None else self._node_refs(node):
            for n in ref.chain():
                if n.level == scope:
                    return n
        if scope in self.layout.aux:
            raise EvalError(f"变量 {name} 属于关联实体「{scope}」，这里取不到对应的{scope}（当前行没有它的编号 "
                            f"{self.rs.datasets[scope].key}）", node.id)
        raise EvalError(f"变量 {name} 属于「{scope}」层级，不能在「{node.level}」层直接引用（需要聚合）", node.id)

    def _node_refs(self, node: Node):
        """对象经自己（及上级）行上的编号字段引用到的关联实体（如参与记录 → 人员、订单 → 品类费率）。"""
        for n in node.chain():
            if n.attrs:
                yield from self._refs(Row(n.attrs, n.attr_src))

    def _refs(self, row: Row):
        """当前行经编号字段引用到的关联实体，近的在前（如回款 → 销售单 → 项目）。"""
        seen, todo = set(), [row]
        while todo:
            r = todo.pop(0)
            for f, v in r.values.items():
                tbl = self.index.get(self.layout.aux_by_key.get(f, ""))
                n = tbl.get(str(v)) if tbl is not None and v is not None else None
                if n is None or n.row is r or id(n) in seen:
                    continue
                seen.add(id(n))
                yield n
                todo.append(n.row)

    def _num(self, val: Val, where: str) -> Decimal:
        if isinstance(val.v, bool) or not isinstance(val.v, Decimal):
            raise EvalError(f"需要数字，得到 {fmt(val.v)}", where)
        return val.v

    def ev(self, ast, ctx: Ctx) -> Val:
        match ast:
            case Num(value=v):
                return Val(v, fmt(v))
            case Str(value=s):
                return Val(s, fmt(s))
            case Const(value=b):
                return Val(b, fmt(b))
            case Name(id=n):
                return self._name(n, ctx)
            case Neg(operand=x):
                a = self.ev(x, ctx)
                return Val(-self._num(a, ctx.where), f"-{a.text}", a.deps, a.rows)
            case Not(operand=x):
                a = self.ev(x, ctx)
                return Val(not self._bool(a, ctx), f"not {a.text}", a.deps, a.rows)
            case BinOp(op=op, left=l, right=r):
                a, b = self.ev(l, ctx), self.ev(r, ctx)
                x, y = self._num(a, ctx.where), self._num(b, ctx.where)
                if op == "/" and y == 0:
                    raise EvalError(f"除数为 0：{a.text} / {b.text}", ctx.where)
                deps, rows = _merge(a, b)
                text = f"{_paren(l, a.text, op, False)} {op} {_paren(r, b.text, op, True)}"
                return Val(_ARITH[op](x, y), text, deps, rows)
            case Cmp(op=op, left=l, right=r):
                a, b = self.ev(l, ctx), self.ev(r, ctx)
                a, b = _coerce(a, b, ctx.where), _coerce(b, a, ctx.where)
                if type(a.v) is not type(b.v) and not (a.v is None or b.v is None):
                    raise EvalError(f"无法比较 {a.text} 和 {b.text}（类型不同）", ctx.where)
                if (a.v is None or b.v is None) and op not in ("==", "!="):
                    raise EvalError(f"空值不能比较大小：{a.text} {op} {b.text}", ctx.where)
                deps, rows = _merge(a, b)
                return Val(_CMP[op](a.v, b.v), f"{a.text} {op} {b.text}", deps, rows)
            case BoolOp(op=op, left=l, right=r):
                a = self.ev(l, ctx)
                av = self._bool(a, ctx)
                if (op == "and" and not av) or (op == "or" and av):
                    return Val(av, a.text, a.deps, a.rows)
                b = self.ev(r, ctx)
                deps, rows = _merge(a, b)
                return Val(self._bool(b, ctx), f"{a.text} {op} {b.text}", deps, rows)
            case Call():
                return self._call(ast, ctx)
        raise EvalError(f"无法求值的节点 {ast!r}", ctx.where)

    def _date(self, val: Val, ctx: Ctx) -> date:
        if isinstance(val.v, date):
            return val.v
        if isinstance(val.v, str):
            try:
                return to_date(val.v)
            except ConvertError as e:
                raise EvalError(str(e), ctx.where) from None
        raise EvalError(f"需要日期，得到 {val.text}", ctx.where)

    def _bool(self, val: Val, ctx: Ctx) -> bool:
        if not isinstance(val.v, bool):
            hint = (f"；{val.text} 的值是文字 {val.v!r}，要写成比较，如 {val.text} == {json.dumps(val.v, ensure_ascii=False)}"
                    if isinstance(val.v, str) else "")
            raise EvalError(f"需要条件（是/否），得到 {val.text}{hint}", ctx.where)
        return val.v

    # ---- 名字解析 ----
    def _name(self, n: str, ctx: Ctx) -> Val:
        if ctx.row is not None and n in ctx.row.values:
            v = ctx.row.values[n]
            if v is None:
                raise EvalError(f"字段 {n} 为空", str(ctx.row.src))
            return Val(v, n, rows=(ctx.row,))
        if n in self.rs.variables:
            return self.variable(n, ctx.node, ctx.row)
        if ctx.row is not None:
            for ref in self._refs(ctx.row):
                if n in ref.attrs:
                    v = ref.attrs[n]
                    if v is None:
                        raise EvalError(f"{ref.id} 的字段 {n} 为空", str(ref.attr_src))
                    return Val(v, f"{n}[{ref.key}]", rows=(ref.row,))
        for node in ctx.node.chain():
            if n in node.attrs:
                v = node.attrs[n]
                if v is None:
                    raise EvalError(f"{node.id} 的字段 {n} 为空", str(node.attr_src))
                return Val(v, fmt(v))
        if ctx.row is None:
            for ref in self._node_refs(ctx.node):
                if n in ref.attrs:
                    v = ref.attrs[n]
                    if v is None:
                        raise EvalError(f"{ref.id} 的字段 {n} 为空", str(ref.attr_src))
                    return Val(v, f"{n}[{ref.key}]", rows=(ref.row,))
        if n in self.rs.tables:
            return Val(self.rs.tables[n], n)
        if n in self.detail_fields:
            raise EvalError(f"{n} 是明细字段，需要放在 sum/avg/count 等聚合函数里使用", ctx.where)
        if n in self.rs.datasets:
            raise EvalError(f"{n} 是数据表名，不能直接当数用：对它的字段聚合，表名写在 of 里，如 sum(字段, of: \"{n}\")", ctx.where)
        raise EvalError(f"无法识别的名字 {n}", ctx.where)

    # ---- 函数 ----
    def _call(self, c: Call, ctx: Ctx) -> Val:
        fn = c.fn
        kw = c.kw()
        if fn in ("min", "max") and len(c.args) > 1:
            vals = [self.ev(a, ctx) for a in c.args]
            nums = [self._num(v, ctx.where) for v in vals]
            deps, rows = _merge(*vals)
            r = min(nums) if fn == "min" else max(nums)
            return Val(r, f"{fn}({', '.join(v.text for v in vals)})", deps, rows)
        if is_aggregate(c):
            return self._aggregate(c, ctx)
        if fn == "if":
            cond = self.ev(c.args[0], ctx)
            yes = self._bool(cond, ctx)
            self._seen(ctx, "if", c.args[0], "是" if yes else "否")
            branch = self.ev(c.args[1] if yes else c.args[2], ctx)
            deps, rows = _merge(cond, branch)
            return Val(branch.v, f"if({cond.text} → {branch.text})", deps, rows)
        if fn in ("tier", "tier_rate"):
            t, x = self.ev(c.args[0], ctx), self.ev(c.args[1], ctx)
            if not isinstance(t.v, TierTable):
                raise EvalError(f"{fn} 的第一个参数必须是阶梯表，得到 {t.text}", ctx.where)
            xv = self._num(x, ctx.where)
            r = tier_amount(t.v, xv) if fn == "tier" else tier_rate(t.v, xv)
            self._tier_seen(c.args[0], t, xv)
            return Val(r, f"{fn}({t.text}, {x.text})", x.deps, x.rows)
        if fn == "lookup":
            t, k = self.ev(c.args[0], ctx), self.ev(c.args[1], ctx)
            if not isinstance(t.v, LookupTable):
                raise EvalError(f"lookup 的第一个参数必须是查找表，得到 {t.text}", ctx.where)
            key = lookup_key(k.v)
            if key in t.v.mapping:
                r = t.v.mapping[key]
            elif t.v.default is not None:
                r = t.v.default
            else:
                raise EvalError(f"查找表 {c.args[0].id} 中没有 {key}，且没有默认值", ctx.where)
            self._seen(ctx, "lookup", getattr(c.args[0], "id", t.text), key if key in t.v.mapping else "默认值")
            return Val(r, fmt(r), k.deps, k.rows)
        if fn == "ratio":
            a, b = self.ev(c.args[0], ctx), self.ev(c.args[1], ctx)
            x, y = self._num(a, ctx.where), self._num(b, ctx.where)
            deps, rows = _merge(a, b)
            if y == 0:
                if "zero" not in kw:
                    raise EvalError(f"ratio 分母为 0：{a.text} / {b.text}，且没有给 zero 默认值", ctx.where)
                z = self.ev(kw["zero"], ctx)
                return Val(self._num(z, ctx.where), f"{a.text} / {b.text}(=0 → {z.text})", deps, rows)
            return Val(x / y, f"{a.text} / {b.text}", deps, rows)
        if fn in ("share", "minmax"):
            return self._sibling(c, ctx)
        if fn == "round":
            a = self.ev(c.args[0], ctx)
            places = int(self._num(self.ev(c.args[1], ctx), ctx.where)) if len(c.args) > 1 else 2
            r = self._num(a, ctx.where).quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP)
            return Val(r, f"round({a.text})", a.deps, a.rows)
        if fn == "clamp":
            vals = [self.ev(a, ctx) for a in c.args]
            x, lo, hi = (self._num(v, ctx.where) for v in vals)
            deps, rows = _merge(*vals)
            return Val(min(max(x, lo), hi), f"clamp({', '.join(v.text for v in vals)})", deps, rows)
        if fn == "abs":
            a = self.ev(c.args[0], ctx)
            return Val(abs(self._num(a, ctx.where)), f"|{a.text}|", a.deps, a.rows)
        if fn == "date":
            a = self.ev(c.args[0], ctx)
            d = self._date(a, ctx)
            return Val(d, fmt(d), a.deps, a.rows)
        if fn == "between":
            x, lo, hi = (self.ev(a, ctx) for a in c.args)
            lo, hi = _coerce(lo, x, ctx.where), _coerce(hi, x, ctx.where)
            if not (type(x.v) is type(lo.v) is type(hi.v)) or isinstance(x.v, (bool, str)):
                raise EvalError(f"between 的三个参数类型要一致（数字或日期）：{x.text}, {lo.text}, {hi.text}", ctx.where)
            deps, rows = _merge(x, lo, hi)
            return Val(lo.v <= x.v <= hi.v, f"{lo.text} ≤ {x.text} ≤ {hi.text}", deps, rows)
        if fn in ("years_between", "days_between"):
            a, b = self.ev(c.args[0], ctx), self.ev(c.args[1], ctx)
            d1, d2 = self._date(a, ctx), self._date(b, ctx)
            if fn == "days_between":
                r = Decimal((d2 - d1).days)
            else:
                r = Decimal(d2.year - d1.year - ((d2.month, d2.day) < (d1.month, d1.day)))
            deps, rows = _merge(a, b)
            return Val(r, f"{fn}({a.text}, {b.text})", deps, rows)
        if fn == "scheme_paid":
            key = "/".join([c.args[0].value, c.args[1].value if len(c.args) > 1 else "R0"])
            ref = self.refs.get(key)
            if ref is None:
                raise EvalError(f"scheme_paid 需要方案 {key} 的结果，请先计算该方案", ctx.where)
            p = ref.payouts.get(ctx.node.id) or getattr(ref, "rollups", {}).get(ctx.node.id)
            if p is None:
                raise EvalError(f"方案 {key} 的结果里没有 {ctx.node.id} 的发放记录（只能在最末层或抵扣汇总到的实体上使用）", ctx.where)
            r = Decimal(p.paid)
            return Val(r, f"{key} 实发 {fmt(r)}", frozenset({f"{key}:{ctx.node.id}"}))
        if fn in ("year", "month", "quarter"):
            a = self.ev(c.args[0], ctx)
            d = self._date(a, ctx)
            r = Decimal(d.year if fn == "year" else d.month if fn == "month" else (d.month - 1) // 3 + 1)
            return Val(r, f"{fn}({a.text})", a.deps, a.rows)
        raise EvalError(f"不支持的函数 {fn}", ctx.where)

    def _seen(self, ctx: Ctx, kind: str, what, outcome: str) -> None:
        item = ctx.where.split("/", 1)[-1]
        seen = self.outcomes.setdefault((item, kind, what), {})
        seen[outcome] = seen.get(outcome, 0) + 1

    def coverage(self) -> list[dict]:
        """条件分支、查找表的命中分布：[{item, kind, what, seen}]，what 是条件原文或表名。"""
        return [{"item": item, "kind": kind, "what": what if isinstance(what, str) and kind == "lookup" else unparse(what),
                 "seen": dict(seen)} for (item, kind, what), seen in self.outcomes.items()]

    def _tier_seen(self, arg, t: Val, x: Decimal) -> None:
        name = getattr(arg, "id", None) or t.text
        st = self.tier_stats.setdefault(name, {"n": 0, "min": x, "max": x, "bands": {}})
        st["n"] += 1
        st["min"], st["max"] = min(st["min"], x), max(st["max"], x)
        b = tier_band(t.v, x)
        st["bands"][b] = st["bands"].get(b, 0) + 1

    def _aggregate(self, c: Call, ctx: Ctx) -> Val:
        kw = c.kw()
        ds = self._agg_dataset(c, ctx)
        node = ctx.node
        if self.peer_scope and not node.aux:
            d = self.rs.datasets.get(ds)
            lvl = self.layout.key_level.get(d.key) if d is not None and d.key else None
            if lvl is not None and lvl <= self.layout.index.get(node.level, -1):
                node = self._above(node, lvl)
        rows = node.rows(ds)
        aux = ds in self.layout.aux

        def rctx(r):   # 关联实体的行在它自己的节点上求值，才能用到该实体的变量；其他行在它所属的下级对象上求值
            if aux:
                return Ctx(self._row_node[id(r)], r, ctx.where)
            home = self._home.get(id(r))
            # 在关联实体上（如人员）汇总分配层级的明细（如它参与的各项目分配记录）：行也在所属的层级对象上求值，才能用到层级变量
            return Ctx(home if home is not None and (node.aux or node in home.chain()) else node, r, ctx.where)
        if "where" in kw:
            cond = kw["where"]
            rows = [r for r in rows if self._bool(self.ev(cond, rctx(r)), ctx)]
        deps: set = set()
        if c.fn == "count":
            r = Decimal(len(rows))
        else:
            vals = []
            for row in rows:
                v = self.ev(c.args[0], rctx(row))
                deps |= v.deps
                vals.append(v.v)
            if c.fn == "count_distinct":
                r = Decimal(len(set(vals)))
            else:
                text = next((v for v in vals if isinstance(v, str)), None)
                if text is not None:
                    raise EvalError(f"{c.fn} 要对数字求值，这里取到的是 {fmt(text)}（{ds} 的行）。数行数用 count(of: \"{ds}\", where: 条件)，"
                                    f"数不同取值的个数用 count_distinct(字段, of: \"{ds}\")", ctx.where)
                nums = [self._num(Val(v, fmt(v)), ctx.where) for v in vals]
                if c.fn == "sum":
                    r = sum(nums, Decimal(0))
                elif not nums:
                    raise EvalError(f"{c.fn} 没有可计算的明细行（{ds}）：这个对象在「{ds}」里没有行，{c.fn} 求不出值。"
                                    f"没有行时应当怎样，按材料写明，如 count(of: \"{ds}\") == 0 or {c.fn}(...) >= 0，"
                                    f"或 if(count(of: \"{ds}\") > 0, {c.fn}(...), 0)；合计用 sum（没有行时为 0）", ctx.where)
                elif c.fn == "avg":
                    r = sum(nums, Decimal(0)) / len(nums)
                else:
                    r = min(nums) if c.fn == "min" else max(nums)
        return Val(r, fmt(r), frozenset(deps), tuple(rows))

    def _agg_dataset(self, c: Call, ctx: Ctx) -> str:
        ds, err = self.layout.agg_dataset(c)
        if ds is None:
            raise EvalError(err, ctx.where)
        return ds

    def _above(self, node: Node, lvl: int) -> Node:
        """node 的祖先里第一个层级在 lvl 之上的（没有就是根）。"""
        for a in node.chain():
            if self.layout.index.get(a.level, -1) < lvl:
                return a
        return self.root

    def _sibling(self, c: Call, ctx: Ctx) -> Val:
        node = ctx.node
        if self.peer_scope and not node.aux:     # share(小组变量) 在个人层求值：取小组在同部门各小组里的份额
            scopes = {self.rs.variables[n].scope for n in names(c.args[0]) if n in self.rs.variables}
            lv = self.layout.index.get(next(iter(scopes)), None) if len(scopes) == 1 else None
            if lv is not None and lv < self.layout.index.get(node.level, -1):
                node = next(a for a in node.chain() if a.level == self.rs.allocation[lv].level)
        sibs = node.parent.children if node.parent else [node]
        vals = {}
        for s in sibs:
            vals[s.id] = self.ev(c.args[0], Ctx(s, None, ctx.where))
        own = vals[node.id]
        nums = {k: self._num(v, ctx.where) for k, v in vals.items()}
        deps = frozenset().union(*(v.deps for v in vals.values()))
        if c.fn == "share":
            total = sum(nums.values(), Decimal(0))
            if total == 0:
                return Val(Decimal(0), f"share({own.text}/0)", deps)
            return Val(nums[node.id] / total, f"({own.text}/{fmt(total)})", deps)
        lo, hi = min(nums.values()), max(nums.values())
        if hi == lo:
            return Val(Decimal(1), f"minmax({own.text})=1", deps)
        return Val((nums[node.id] - lo) / (hi - lo), f"minmax({own.text})", deps)


def _coerce(a: Val, other: Val, where: str) -> Val:
    """字符串和日期、是/否比较时，把字符串按对方的类型转换（如 recognition_date >= "2026-07-01"）。"""
    if isinstance(a.v, str) and isinstance(other.v, (date, bool)):
        try:
            v = to_bool(a.v) if isinstance(other.v, bool) else to_date(a.v)
        except ConvertError as e:
            raise EvalError(str(e), where) from None
        return Val(v, a.text, a.deps, a.rows)
    return a


_PREC = {"+": 1, "-": 1, "*": 2, "/": 2}


def _paren(ast, text: str, parent_op: str, right: bool) -> str:
    """只在优先级需要时加括号，让渲染出的算式易读。"""
    if not isinstance(ast, BinOp):
        return text
    p, c = _PREC[parent_op], _PREC[ast.op]
    if c < p or (right and c == p and parent_op in "-/"):
        return f"({text})"
    return text


def tier_band(t: TierTable, x: Decimal) -> int:
    """x 落在第几档（从 0 起），低于第一档的下限为 -1。"""
    band = -1
    for i, (lo, _) in enumerate(t.steps):
        if x > lo or (t.lower_inclusive and x == lo):
            band = i
    return band


def tier_rate(t: TierTable, x: Decimal) -> Decimal:
    rate = Decimal(0)
    for lo, r in t.steps:
        if x > lo or (t.lower_inclusive and x == lo):
            rate = r
    return rate


def tier_amount(t: TierTable, x: Decimal) -> Decimal:
    if t.mode == "whole":
        return x * tier_rate(t, x)
    total = Decimal(0)
    steps = list(t.steps)
    for i, (lo, r) in enumerate(steps):
        hi = steps[i + 1][0] if i + 1 < len(steps) else None
        top = x if hi is None else min(x, hi)
        if top > lo:
            total += (top - lo) * r
    return total
