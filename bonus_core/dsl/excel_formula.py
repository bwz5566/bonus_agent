"""把规则表达式翻译成 Excel 公式，供结果文件里的「过程复核」表用：财务在 Excel 里用公式自己重算一遍，和引擎结果逐个核对。

只翻译逐个对象可以直接算的部分（四则运算、比较、if / min / max / round / abs / clamp / ratio、查找表、阶梯表）。
对明细的汇总（sum / count …）、日期、同级占比等不翻译，作为“取数”列直接写引擎算出的值。
"""
from __future__ import annotations

import re
from decimal import Decimal

from bonus_core.dsl.models import LookupTable, RuleSet, TierTable
from bonus_core.dsl.parser import BinOp, BoolOp, Call, Cmp, Const, ExprError, Name, Neg, Not, Num, Str, parse, walk

FUNCS = {"if", "min", "max", "round", "abs", "clamp", "ratio", "tier", "tier_rate", "lookup"}
_CMP = {"==": "=", "!=": "<>", ">=": ">=", "<=": "<=", ">": ">", "<": "<"}


class NotExcel(ValueError):
    pass


def translatable(src: str, rs: RuleSet) -> bool:
    try:
        ast = parse(src)
    except ExprError:
        return False
    for n in walk(ast):
        if isinstance(n, Call):
            if n.fn not in FUNCS or n.kwargs and not (n.fn == "ratio" and set(n.kw()) == {"zero"}):
                return False
            if n.fn in ("min", "max") and len(n.args) < 2:        # 单参数 min / max 是对明细的汇总
                return False
            if n.fn in ("tier", "tier_rate", "lookup") and not (isinstance(n.args[0], Name) and n.args[0].id in rs.tables):
                return False
    return True


def names(src: str, rs: RuleSet) -> list[str]:
    """表达式里引用的字段和变量（不含表名），按出现顺序。"""
    out = []
    for n in walk(parse(src)):
        if isinstance(n, Name) and n.id not in rs.tables and n.id not in out:
            out.append(n.id)
    return out


class Params:
    """「参数表」sheet 里每张表的位置：查找表每行 键 | 值；阶梯表每行 下限 | 比例。"""

    def __init__(self, sheet: str = "参数表"):
        self.sheet = sheet
        self.cells: dict[str, list[tuple[str, str]]] = {}     # 表名 -> [(键或下限的单元格, 值的单元格)]
        self.rows: list[list] = []

    def place(self, rs: RuleSet, used: set[str]) -> None:
        for name in sorted(used):
            t = rs.tables[name]
            desc = getattr(t, "desc", "") or ""
            if isinstance(t, LookupTable):
                self.rows.append([f"{name}（查找表）{desc}", "键", "值"])
                items = list(t.mapping.items())
            else:
                mode = "超额累进：各档分段累加" if t.mode == "excess" else "全额累进：整体按所在档"
                bound = "达到下限即适用" if t.lower_inclusive else "超过下限才适用"
                self.rows.append([f"{name}（阶梯表，{mode}，{bound}）{desc}", "下限", "比例 / 系数"])
                items = list(t.steps)
            refs = []
            for k, v in items:
                r = len(self.rows) + 1
                key = str(k) if isinstance(t, LookupTable) else float(k)     # 查找表的键保留文本（如 001），阶梯下限是数
                self.rows.append(["", key, float(v)])
                refs.append((f"'{self.sheet}'!$B${r}", f"'{self.sheet}'!$C${r}"))
            if isinstance(t, LookupTable) and t.default is not None:
                r = len(self.rows) + 1
                self.rows.append(["", "（其他）", float(t.default)])
                refs.append(("", f"'{self.sheet}'!$C${r}"))
            self.cells[name] = refs
            self.rows.append([])


class Formula:
    """把一个表达式翻译成某一行的 Excel 公式。cell(name) 给出该名字在这一行的单元格。"""

    def __init__(self, rs: RuleSet, params: Params, cell):
        self.rs, self.params, self.cell = rs, params, cell

    def __call__(self, src: str) -> str:
        return "=" + self.tr(parse(src))

    def tr(self, n) -> str:
        match n:
            case Num(value=v):
                return _num(v)
            case Str(value=s):
                return '"' + s.replace('"', '""') + '"'
            case Const(value=b):
                return "TRUE" if b else "FALSE"
            case Name(id=x):
                return self.cell(x)
            case Neg(operand=x):
                return f"-({self.tr(x)})"
            case Not(operand=x):
                return f"NOT({self.tr(x)})"
            case BinOp(op=op, left=l, right=r):
                return f"({self.tr(l)}{op}{self.tr(r)})"
            case Cmp(op=op, left=l, right=r):
                return f"({self.tr(l)}{_CMP[op]}{self.tr(r)})"
            case BoolOp(op=op, left=l, right=r):      # 用 IF 保留短路：左边已能定结果时不算右边（AND()/OR() 会两边都算）
                if op == "and":
                    return f"IF({self.tr(l)},IF({self.tr(r)},TRUE,FALSE),FALSE)"
                return f"IF({self.tr(l)},TRUE,IF({self.tr(r)},TRUE,FALSE))"
            case Call():
                return self._call(n)
        raise NotExcel(str(n))

    def _call(self, c: Call) -> str:
        a = [self.tr(x) for x in c.args] if c.fn not in ("tier", "tier_rate", "lookup") else []
        if c.fn == "if":
            return f"IF({a[0]},{a[1]},{a[2]})"
        if c.fn in ("min", "max", "abs"):
            return f"{c.fn.upper()}({','.join(a)})"
        if c.fn == "round":
            return f"ROUND({a[0]},{a[1] if len(a) > 1 else 2})"
        if c.fn == "clamp":
            return f"MIN(MAX({a[0]},{a[1]}),{a[2]})"
        if c.fn == "ratio":
            z = c.kw().get("zero")
            return f"IF({a[1]}=0,{self.tr(z)},{a[0]}/{a[1]})" if z is not None else f"({a[0]}/{a[1]})"
        name, x = c.args[0].id, self.tr(c.args[1])
        t, refs = self.rs.tables[name], self.params.cells[name]
        if c.fn == "lookup":
            out = refs[-1][1] if t.default is not None else "NA()"
            keys = list(t.mapping)
            for k, (kc, vc) in reversed(list(zip(keys, refs))):     # 和引擎一样按文本比较键（数字 1 与键 "1" 相同）
                out = f"IF(({x}&\"\")={kc},{vc},{out})"
            return out
        steps = list(zip(t.steps, refs))
        op = ">=" if t.lower_inclusive else ">"
        rate = "0"
        for (lo, _), (lc, rc) in steps:
            rate = f"IF({x}{op}{lc},{rc},{rate})"
        if c.fn == "tier_rate":
            return rate
        if t.mode == "whole":
            return f"({x}*{rate})"
        parts = []
        for i, ((lo, _), (lc, rc)) in enumerate(steps):
            hi = steps[i + 1][1][0] if i + 1 < len(steps) else None
            top = x if hi is None else f"MIN({x},{hi})"
            parts.append(f"MAX({top}-{lc},0)*{rc}")
        return "(" + "+".join(parts) + ")"


def _num(v: Decimal) -> str:
    s = format(v.normalize(), "f")
    return s if not re.match(r"^-", s) else f"({s})"


def plan(rs: RuleSet) -> dict[str, dict]:
    """每个分配层级的复核表怎么排：computed 用公式算的变量（按依赖排好），inputs 直接取引擎值的字段和变量，
    pool / weight 该层的应得金额或权重能否用公式复算。"""
    out = {}
    for lv in rs.allocation:
        own = [n for n, v in rs.variables.items() if v.scope == lv.level]
        ok = {n for n in own if translatable(rs.variables[n].expr, rs)}
        targets = {}
        if rs.pool.scope == lv.level and translatable(rs.pool.expr, rs):
            targets["pool"] = rs.pool.expr
        if lv.weight and translatable(lv.weight, rs):
            targets["weight"] = lv.weight
        if not own and not targets:
            continue
        computed: list[str] = []

        def visit(n: str, path=()):
            if n in computed or n in path:
                return
            for m in names(rs.variables[n].expr, rs):
                if m in ok:
                    visit(m, path + (n,))
            computed.append(n)
        for n in own:
            if n in ok:
                visit(n)
        inputs = []
        for src in [rs.variables[n].expr for n in computed] + list(targets.values()):
            for m in names(src, rs):
                if m not in computed and m not in inputs:
                    inputs.append(m)
        inputs += [n for n in own if n not in computed and n not in inputs]
        out[lv.level] = {"computed": computed, "inputs": inputs, **targets}
    return out
