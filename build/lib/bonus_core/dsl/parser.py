"""受限表达式语法：只有算术、比较、逻辑和白名单函数调用。

没有属性访问、下标、赋值，因此规则里写不出任意代码。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

from lark import Lark, Transformer, v_args
from lark.exceptions import LarkError

GRAMMAR = r"""
?start: expr
?expr: or_
?or_: and_ | or_ "or" and_          -> or_op
?and_: not_ | and_ "and" not_       -> and_op
?not_: cmp | "not" not_             -> not_op
?cmp: sum | sum CMP sum             -> cmp_op
?sum: term | sum ADD term           -> bin_op
?term: factor | term MUL factor     -> bin_op
?factor: atom | "-" factor          -> neg_op
?atom: NUMBER                       -> number
     | STRING                       -> string
     | NAME "(" [args] ")"          -> call
     | NAME                         -> name
     | "(" expr ")"
args: arg ("," arg)*
?arg: expr | NAME ":" expr          -> kwarg

ADD: "+" | "-"
MUL: "*" | "/"
CMP: "==" | "!=" | ">=" | "<=" | ">" | "<"
NAME: /(?!(and|or|not)\b)[^\W\d]\w*/
NUMBER: /\d+(\.\d+)?%?/
STRING: /"[^"]*"/ | /'[^']*'/
%ignore /\s+/
"""


class ExprError(ValueError):
    pass


# ---------------- AST ----------------
@dataclass(frozen=True)
class Num:
    value: Decimal


@dataclass(frozen=True)
class Str:
    value: str


@dataclass(frozen=True)
class Const:
    value: bool


@dataclass(frozen=True)
class Name:
    id: str


@dataclass(frozen=True)
class Call:
    fn: str
    args: tuple
    kwargs: tuple  # ((name, node), ...)

    def kw(self) -> dict:
        return dict(self.kwargs)


@dataclass(frozen=True)
class BinOp:
    op: str
    left: object
    right: object


@dataclass(frozen=True)
class Cmp:
    op: str
    left: object
    right: object


@dataclass(frozen=True)
class BoolOp:
    op: str  # and / or
    left: object
    right: object


@dataclass(frozen=True)
class Not:
    operand: object


@dataclass(frozen=True)
class Neg:
    operand: object


@dataclass(frozen=True)
class _Kw:
    name: str
    value: object


@v_args(inline=True)
class _ToAst(Transformer):
    def number(self, tok):
        s = str(tok)
        if s.endswith("%"):
            return Num(Decimal(s[:-1]) / Decimal(100))
        return Num(Decimal(s))

    def string(self, tok):
        return Str(str(tok)[1:-1])

    def name(self, tok):
        if str(tok).lower() in ("true", "false"):
            return Const(str(tok).lower() == "true")
        return Name(str(tok))

    def call(self, fn, args=None):
        pos, kws = [], []
        for a in (args or []):
            if isinstance(a, _Kw):
                kws.append((a.name, a.value))
            else:
                if kws:
                    raise ExprError(f"{fn}(): 位置参数不能写在命名参数之后")
                pos.append(a)
        return Call(str(fn), tuple(pos), tuple(kws))

    def args(self, *items):
        return list(items)

    def kwarg(self, name, value):
        return _Kw(str(name), value)

    def bin_op(self, l, op, r):
        return BinOp(str(op), l, r)

    def cmp_op(self, l, op, r):
        return Cmp(str(op), l, r)

    def and_op(self, l, r):
        return BoolOp("and", l, r)

    def or_op(self, l, r):
        return BoolOp("or", l, r)

    def not_op(self, x):
        return Not(x)

    def neg_op(self, x):
        return Neg(x)


_parser = Lark(GRAMMAR, parser="lalr", maybe_placeholders=False)


@lru_cache(maxsize=4096)
def parse(src: str):
    try:
        return _ToAst().transform(_parser.parse(src))
    except LarkError as e:
        raise ExprError(f"表达式无法解析: {src!r}\n{e}") from None


def walk(node):
    """深度优先遍历 AST。"""
    yield node
    match node:
        case Call(args=args, kwargs=kws):
            for a in args:
                yield from walk(a)
            for _, v in kws:
                yield from walk(v)
        case BinOp(left=l, right=r) | Cmp(left=l, right=r) | BoolOp(left=l, right=r):
            yield from walk(l)
            yield from walk(r)
        case Not(operand=x) | Neg(operand=x):
            yield from walk(x)


def names(node) -> set[str]:
    return {n.id for n in walk(node) if isinstance(n, Name)}
