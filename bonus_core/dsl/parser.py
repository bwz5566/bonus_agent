"""受限表达式语法：只有算术、比较、逻辑和白名单函数调用。

没有属性访问、下标、赋值，因此规则里写不出任意代码。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

from lark import Lark, Transformer, v_args
from lark.exceptions import LarkError, VisitError

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
        if isinstance(l, Name) and l == r:      # 常见于想按“当前对象”筛选：where: id == id 对每一行都成立
            raise ExprError(f"{l.id} {op} {r.id} 两边是同一个字段，对每一行结果都一样，起不到筛选作用。"
                            f"想只取当前对象自己的行时不用写这个条件：在某一层（或关联实体）上汇总，"
                            f"引擎只取这个对象关联的行；要按别的对象筛选，右边写具体的值")
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
    except VisitError as e:          # 转换语法树时发现的写法问题（如位置参数写在命名参数后面）
        if isinstance(e.orig_exc, ExprError):
            raise ExprError(f"表达式写法不对：{e.orig_exc}") from None
        raise ExprError(f"表达式无法解析：{e.orig_exc}") from None
    except LarkError as e:
        raise ExprError(f"表达式无法解析：{_why(src, e)}") from None


def _parens(src: str, upto: int) -> tuple[list[tuple[int, bool]], int, int]:
    """扫描到 upto 为止（跳过字符串）：(还没闭合的左括号 [(位置, 是不是函数调用)], 第一处多出右括号的位置或 -1, 整式左右括号差)。"""
    stack: list[tuple[int, bool]] = []
    extra, depth, quote = -1, 0, ""
    for i, ch in enumerate(src):
        if quote:
            quote = "" if ch == quote else quote
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
            if i < upto:
                j = i - 1
                while j >= 0 and src[j] == " ":
                    j -= 1
                stack.append((i, j >= 0 and (src[j].isalnum() or src[j] == "_")))
        elif ch == ")":
            depth -= 1
            if i < upto:
                if stack:
                    stack.pop()
                elif extra < 0:
                    extra = i
    return stack, extra, depth


def _why(src: str, e: Exception) -> str:
    """把语法分析器的报错换成能照着改的话：出错位置（▲）、括号配没配对、常见的不支持写法。
    长表达式不再整段回显，只给出错位置前后一段。"""
    pos = getattr(e, "pos_in_stream", None)
    if pos is None and getattr(e, "column", None):
        pos = sum(len(l) + 1 for l in src.split("\n")[:max(getattr(e, "line", 1), 1) - 1]) + e.column - 1
    pos = min(max(pos or 0, 0), len(src))
    tok = getattr(e, "token", None)
    tok = str(tok) if tok is not None else src[pos:pos + 1]
    near = f"…{src[max(0, pos - 60):pos]}▲{src[pos:pos + 30]}…"
    stack, extra, depth = _parens(src, pos)
    total = f"整个式子左括号比右括号{'多' if depth > 0 else '少'} {abs(depth)} 个。" if depth else ""
    if tok == "in" or src[pos:pos + 3] == "in ":
        why = "不支持 in (…)，写成 x == \"a\" or x == \"b\""
    elif tok.startswith("{") or src.lstrip().startswith("{"):
        why = "表达式只能是一个算式，不能写成 {名字: 算式, …} 这样的对象；几个数分几次算"
    elif tok == "," and not stack:
        why = "到▲处括号已经全部闭合、式子已经结束，后面却还有逗号：前面多写了右括号，或少写了左括号"
    elif tok == "," and not stack[-1][1]:
        k = stack[-1][0]
        why = (f"▲处的逗号在一个普通括号里（从「{src[max(0, k - 20):k + 1]}」这个左括号开始，它不是函数调用）："
               "这个括号多半少写了右括号，没在逗号前闭合")
    elif tok == ")" or extra >= 0:
        why = "右括号多了，没有对应的左括号"
    elif pos >= len(src) or tok in ("", "$END"):
        why = "式子没写完" + ("：少了右括号" if depth > 0 else "")
    else:
        why = f"▲处的「{tok}」写法不对"
    return f"{why}。{total}出错位置（▲处）：{near}"


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


_PREC = {"or": 1, "and": 2, "not": 3, "cmp": 4, "+": 5, "-": 5, "*": 6, "/": 6, "neg": 7, "atom": 8}


def unparse(node) -> str:
    """AST 写回表达式文字（报告里引用条件用）。"""
    return _unparse(node)[0]


def _unparse(n) -> tuple[str, int]:
    def wrap(x, p):
        t, q = _unparse(x)
        return f"({t})" if q < p else t
    match n:
        case Num(value=v):
            return format(v.normalize(), "f"), _PREC["atom"]
        case Str(value=v):
            return f'"{v}"', _PREC["atom"]
        case Const(value=v):
            return ("true" if v else "false"), _PREC["atom"]
        case Name(id=i):
            return i, _PREC["atom"]
        case Call(fn=fn, args=args, kwargs=kws):
            parts = [unparse(a) for a in args] + [f"{k}: {unparse(v)}" for k, v in kws]
            return f"{fn}({', '.join(parts)})", _PREC["atom"]
        case BinOp(op=op, left=l, right=r):
            p = _PREC[op]
            return f"{wrap(l, p)} {op} {wrap(r, p + 1)}", p
        case Cmp(op=op, left=l, right=r):
            p = _PREC["cmp"]
            return f"{wrap(l, p + 1)} {op} {wrap(r, p + 1)}", p
        case BoolOp(op=op, left=l, right=r):
            p = _PREC[op]
            return f"{wrap(l, p)} {op} {wrap(r, p)}", p
        case Not(operand=x):
            return f"not {wrap(x, _PREC['not'])}", _PREC["not"]
        case Neg(operand=x):
            return f"-{wrap(x, _PREC['neg'])}", _PREC["neg"]
    return str(n), _PREC["atom"]
