"""带追踪的表达式求值器。

每次求值返回值和渲染后的算式；变量、权重、奖金池级别的结果记为 Trace，
所有 Trace 构成计算链，可以从任意金额一路追到明细行和规则原文。
"""
from __future__ import annotations

import operator
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal, localcontext

from bonus_core.data.table import Row
from bonus_core.dsl.models import LookupTable, RuleSet, TierTable
from bonus_core.dsl.parser import BinOp, BoolOp, Call, Cmp, Name, Neg, Not, Num, Str, names, parse
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


_ARITH = {"+": operator.add, "-": operator.sub, "*": operator.mul, "/": operator.truediv}
_CMP = {"==": operator.eq, "!=": operator.ne, ">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt}


def _merge(*vals: Val) -> tuple[frozenset, tuple]:
    deps = frozenset().union(*(v.deps for v in vals))
    rows = tuple(r for v in vals for r in v.rows)
    return deps, rows


class Evaluator:
    def __init__(self, rs: RuleSet, root: Node, by_level: dict[str, list[Node]]):
        self.rs = rs
        self.root = root
        self.by_level = by_level
        self.traces: dict[str, Trace] = {}
        self._values: dict[str, object] = {}
        self._cache: dict[tuple[str, str], Val] = {}
        self._busy: set[tuple[str, str]] = set()
        self.layout = Layout(rs)
        # 只能在聚合里使用的字段 -> 所在的明细表/公司级表（不含主键/关联键）
        self.detail_fields = self.layout.row_only

    # ---------------- 对外接口 ----------------
    def evaluate(self, expr: str, node: Node, trace_id: str, rule_item: str) -> Decimal:
        val = self._eval_traced(expr, node, trace_id, rule_item)
        return self._num(val, trace_id)

    def variable(self, name: str, node: Node) -> Val:
        var = self.rs.variables[name]
        target = self._scope_node(var.scope, node, name)
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

    def _scope_node(self, scope: str, node: Node, name: str) -> Node:
        if scope == "global":
            return self.root
        for n in node.chain():
            if n.level == scope:
                return n
        raise EvalError(f"变量 {name} 属于「{scope}」层级，不能在「{node.level}」层直接引用（需要聚合）", node.id)

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

    def _bool(self, val: Val, ctx: Ctx) -> bool:
        if not isinstance(val.v, bool):
            raise EvalError(f"需要条件（是/否），得到 {val.text}", ctx.where)
        return val.v

    # ---- 名字解析 ----
    def _name(self, n: str, ctx: Ctx) -> Val:
        if ctx.row is not None and n in ctx.row.values:
            v = ctx.row.values[n]
            if v is None:
                raise EvalError(f"字段 {n} 为空", str(ctx.row.src))
            return Val(v, n, rows=(ctx.row,))
        if n in self.rs.variables:
            return self.variable(n, ctx.node)
        for node in ctx.node.chain():
            if n in node.attrs:
                v = node.attrs[n]
                if v is None:
                    raise EvalError(f"{node.id} 的字段 {n} 为空", str(node.attr_src))
                return Val(v, fmt(v))
        if n in self.rs.tables:
            return Val(self.rs.tables[n], n)
        if n in self.detail_fields:
            raise EvalError(f"{n} 是明细字段，需要放在 sum/avg/count 等聚合函数里使用", ctx.where)
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
            branch = self.ev(c.args[1] if self._bool(cond, ctx) else c.args[2], ctx)
            deps, rows = _merge(cond, branch)
            return Val(branch.v, f"if({cond.text} → {branch.text})", deps, rows)
        if fn in ("tier", "tier_rate"):
            t, x = self.ev(c.args[0], ctx), self.ev(c.args[1], ctx)
            if not isinstance(t.v, TierTable):
                raise EvalError(f"{fn} 的第一个参数必须是阶梯表，得到 {t.text}", ctx.where)
            xv = self._num(x, ctx.where)
            r = tier_amount(t.v, xv) if fn == "tier" else tier_rate(t.v, xv)
            return Val(r, f"{fn}({t.text}, {x.text})", x.deps, x.rows)
        if fn == "lookup":
            t, k = self.ev(c.args[0], ctx), self.ev(c.args[1], ctx)
            if not isinstance(t.v, LookupTable):
                raise EvalError(f"lookup 的第一个参数必须是查找表，得到 {t.text}", ctx.where)
            key = k.v if isinstance(k.v, str) else fmt(k.v)
            if key in t.v.mapping:
                r = t.v.mapping[key]
            elif t.v.default is not None:
                r = t.v.default
            else:
                raise EvalError(f"查找表 {c.args[0].id} 中没有 {key}，且没有默认值", ctx.where)
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
        raise EvalError(f"不支持的函数 {fn}", ctx.where)

    def _aggregate(self, c: Call, ctx: Ctx) -> Val:
        kw = c.kw()
        ds = self._agg_dataset(c, ctx)
        rows = ctx.node.rows(ds)
        if "where" in kw:
            cond = kw["where"]
            rows = [r for r in rows if self._bool(self.ev(cond, Ctx(ctx.node, r, ctx.where)), ctx)]
        deps: set = set()
        if c.fn == "count":
            r = Decimal(len(rows))
        else:
            vals = []
            for row in rows:
                v = self.ev(c.args[0], Ctx(ctx.node, row, ctx.where))
                deps |= v.deps
                vals.append(v.v)
            if c.fn == "count_distinct":
                r = Decimal(len(set(vals)))
            else:
                nums = [self._num(Val(v, fmt(v)), ctx.where) for v in vals]
                if c.fn == "sum":
                    r = sum(nums, Decimal(0))
                elif not nums:
                    raise EvalError(f"{c.fn} 没有可计算的明细行（{ds}）", ctx.where)
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

    def _sibling(self, c: Call, ctx: Ctx) -> Val:
        node = ctx.node
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


_PREC = {"+": 1, "-": 1, "*": 2, "/": 2}


def _paren(ast, text: str, parent_op: str, right: bool) -> str:
    """只在优先级需要时加括号，让渲染出的算式易读。"""
    if not isinstance(ast, BinOp):
        return text
    p, c = _PREC[parent_op], _PREC[ast.op]
    if c < p or (right and c == p and parent_op in "-/"):
        return f"({text})"
    return text


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
