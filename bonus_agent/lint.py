"""程序检查：不靠模型记住提示词，按规则结构和真实数据找出常见的写错，必要时强制提问。

- 照抄：查找表的键和值和某张数据表的两列完全一样（如把品类费率表抄成常数），下一期数据变了规则不跟着变，要改成直接读字段；
- 出处：规则里的每个常数（系数、比例、阈值、单价）都要在材料、财务的话或答复里找得到；
- 取值：规则里和字段比较的文字（如 review == "通过"）要在数据或材料里出现，防止写错取值导致整批被筛掉；
- 未提及的取值：数据里出现了材料和规则都没提到的取值（如工单状态“挂起”），规则会把它悄悄当成不满足条件，要问财务；
- 结构提示：筛选后全部为 0、定义了却没用到的变量。

gates 在 finish 时必须处理（改规则、问财务，或写进 ignored 说明原因）；warns 只附在试算结果里。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from bonus_core.data.table import Datasets
from bonus_core.dsl.models import LookupTable, RuleSet, TierTable
from bonus_core.dsl.parser import Call, Cmp, ExprError, Name, Num, Str, parse, walk

# 不需要出处的常数：0、1、百分数换算、单位换算（月、天、小时、分钟、周）
FREE = {Decimal(x) for x in ("0", "1", "-1", "12", "24", "60", "7", "365", "366", "0.01")} | {Decimal(10) ** i for i in range(1, 9)}
_SCALE = {"%": Decimal("0.01"), "％": Decimal("0.01"), "‰": Decimal("0.001"), "万元": Decimal(10000), "万": Decimal(10000),
          "亿元": Decimal(10) ** 8, "亿": Decimal(10) ** 8}
_NUM = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(%|％|‰|万元|万|亿元|亿)?")
_DATE = re.compile(r"^\d{4}-\d{1,2}(-\d{1,2})?$")
MAX_CATEGORIES = 30          # 不同取值超过这么多的字段（编号、姓名）不查“未提及的取值”


def _key(d: Decimal) -> str:
    return format(d.normalize(), "f")


def material_numbers(text: str) -> set[str]:
    """材料里出现的数：原样一份，带 % / 万 的再按换算后一份。"""
    t = unicodedata.normalize("NFKC", text)
    t = re.sub(r"(?<=\d),(?=\d{3})", "", t)
    out = set()
    for m in _NUM.finditer(t):
        try:
            n = Decimal(m.group(1))
        except InvalidOperation:
            continue
        out.add(_key(n))
        if m.group(2):
            out.add(_key(n * _SCALE[m.group(2)]))
    return out


@dataclass
class Report:
    gates: list[str] = field(default_factory=list)       # finish 前必须处理
    warns: list[str] = field(default_factory=list)       # 只提示
    keys: list[str] = field(default_factory=list)        # 与 gates 一一对应，用于 ignored 豁免（如 常数 1.15、取值 挂起）


def _exprs(rs: RuleSet):
    """(位置, 表达式)"""
    for n, v in rs.variables.items():
        yield f"variables.{n}", v.expr
    yield "pool", rs.pool.expr
    for lv in rs.allocation:
        if lv.weight:
            yield f"allocation.{lv.level}", lv.weight
    for p in rs.payout:
        for e in (p.pay_ratio, p.hold_ratio):
            if e:
                yield f"payout.{p.id}", e
    for o in rs.offsets:
        yield f"offsets.{o.id}", o.expr
    for c in rs.checks:
        yield f"checks.{c.id}", c.expr


def _parsed(rs: RuleSet):
    for where, src in _exprs(rs):
        try:
            yield where, parse(src)
        except ExprError:
            continue


def constants(rs: RuleSet) -> list[tuple[Decimal, str]]:
    """规则里写死的数和位置。round 的位数不算。"""
    out = []
    for where, ast in _parsed(rs):
        skip = {id(c.args[1]) for c in walk(ast) if isinstance(c, Call) and c.fn == "round" and len(c.args) > 1}
        out += [(n.value, where) for n in walk(ast) if isinstance(n, Num) and id(n) not in skip]
    for name, t in rs.tables.items():
        if isinstance(t, TierTable):
            out += [(x, f"tables.{name}") for step in t.steps for x in step]
        elif isinstance(t, LookupTable):
            out += [(x, f"tables.{name}") for x in list(t.mapping.values()) + ([t.default] if t.default is not None else [])]
    return out


def _field_values(data: Datasets) -> dict[str, dict[str, int]]:
    """字段名 -> {文字取值: 行数}，只收全是文字的字段。"""
    out: dict[str, dict[str, int]] = {}
    bad: set[str] = set()
    for t in data.values():
        for r in t.rows:
            for k, v in r.values.items():
                if v is None or v == "":
                    continue
                if not isinstance(v, str):
                    bad.add(k)
                    continue
                c = out.setdefault(k, {})
                c[v] = c.get(v, 0) + 1
    return {k: v for k, v in out.items() if k not in bad}


def _compared(rs: RuleSet, negated: set[str] | None = None) -> dict[str, dict[str, set[str]]]:
    """字段 -> {比较用的文字: 位置}（== 和 != 都算，日期除外）。negated 收集用 != 排除取值的字段。"""
    out: dict[str, dict[str, set[str]]] = {}
    for where, ast in _parsed(rs):
        for c in walk(ast):
            if isinstance(c, Cmp) and c.op in ("==", "!="):
                for a, b in ((c.left, c.right), (c.right, c.left)):
                    if isinstance(a, Name) and isinstance(b, Str) and not _DATE.match(b.value):
                        out.setdefault(a.id, {}).setdefault(b.value, set()).add(where)
                        if c.op == "!=" and negated is not None:
                            negated.add(a.id)
    for name, t in rs.tables.items():      # 查找表的键：lookup(表, 字段)
        if not isinstance(t, LookupTable):
            continue
        for where, ast in _parsed(rs):
            for c in walk(ast):
                if isinstance(c, Call) and c.fn == "lookup" and len(c.args) == 2 and isinstance(c.args[0], Name) \
                        and c.args[0].id == name and isinstance(c.args[1], Name):
                    for k in t.mapping:
                        out.setdefault(c.args[1].id, {}).setdefault(k, set()).add(f"tables.{name}")
    return out


def check(rs: RuleSet, data: Datasets, text: str, traces: dict | None = None, sourced: str = "") -> Report:
    """text：规则材料原文（规则文档、财务的话和答复）。sourced 非空时（如有图片没转写）常数出处无法核对，只提示。"""
    rep = Report()
    norm = unicodedata.normalize("NFKC", text)
    nums = material_numbers(text)
    # 0. 查找表照抄了数据表：换期后数据变了，规则还按旧数算，而且不报错
    copied = _copied(rs, data)
    for name, (ds, key, fld) in copied.items():
        rep.gates.append(f"查找表 {name} 照抄了数据表「{ds}」的 {fld}（按 {key} 对应）。下一期数据变了，规则还会按旧数算；"
                         f"删掉这张查找表，直接引用字段 {fld}（行上有 {key} 的表都能按编号读到）")
        rep.keys.append(f"查找表 {name}")
    # 1. 常数要有出处
    miss: dict[str, set[str]] = {}
    for n, where in constants(rs):
        if where.removeprefix("tables.") in copied:
            continue
        if n in FREE or abs(n) in FREE or _key(abs(n)) in nums:
            continue
        miss.setdefault(_key(n), set()).add(where)
    for k, ws in miss.items():
        if sourced:
            rep.warns.append(f"常数 {k}（{'、'.join(sorted(ws))}）在材料文字里找不到出处（{sourced}），请自己核对")
            continue
        rep.gates.append(f"常数 {k}（{'、'.join(sorted(ws))}）在材料里找不到出处。核对是否抄错、换算错；"
                         "材料确实没写、是你推算的，用 ask_question 请财务确认")
        rep.keys.append(f"常数 {k}")
    # 2. 比较用的文字要在数据或材料里出现；3. 数据里材料和规则都没提到的取值要问财务
    values = _field_values(data)
    negated: set[str] = set()
    for fld, lits in _compared(rs, negated).items():
        have = values.get(fld)
        if not have:
            continue
        for lit, ws in lits.items():
            if lit not in have and lit not in norm:
                rep.gates.append(f"「{fld}」写的取值「{lit}」（{'、'.join(sorted(ws))}）在数据和材料里都没有；数据里的取值是："
                                 f"{'、'.join(list(have)[:12])}。按数据的实际写法改，对应关系拿不准就 ask_question")
                rep.keys.append(f"取值 {lit}")
        if len(have) > MAX_CATEGORIES:
            continue
        for v, cnt in have.items():
            if v in lits or v in norm or _opposite(v, lits):
                continue
            # 用 != 排除取值时，没提到的取值会被悄悄算进去；和规则里某个取值写法相近（如 已关闭（系统））时可能被悄悄漏掉
            near = [x for x in lits if x in v or v in x]
            if fld in negated or near:
                how = "算进去（规则用 != 排除取值）" if fld in negated else f"当作不是「{near[0]}」"
                rep.gates.append(f"数据里「{fld}」有取值「{v}」（{cnt} 行），材料和规则都没提到，现在的规则会把它{how}。"
                                 "用 ask_question 问财务这类记录怎么处理（问题里写上这个取值）")
                rep.keys.append(f"取值 {v}")
    # 结构提示
    used = _reachable(rs)
    idle = [n for n in rs.variables if n not in used]
    if idle:
        rep.warns.append(f"变量 {'、'.join(idle[:10])} 定义了，但奖金池、权重、发放、抵扣和校验都没有用到。"
                         "是不是漏接进计算了（如封顶、扣减）？只是展示用的可以不管")
    if traces:
        rep.warns += _all_zero(rs, traces)
    return rep


def _copied(rs: RuleSet, data: Datasets) -> dict[str, tuple[str, str, str]]:
    """查找表 -> (数据表, 键字段, 值字段)：查找表的每个键在数据表里都有，对应的值也完全一样。"""
    out = {}
    for name, t in rs.tables.items():
        if not isinstance(t, LookupTable) or len(t.mapping) < 2:
            continue
        for ds, tbl in data.items():
            rows = [r.values for r in tbl.rows]
            fields = list(rs.datasets[ds].fields) if ds in rs.datasets else []
            hit = None
            for key in fields:
                index = {str(r.get(key)): r for r in rows if r.get(key) is not None}
                if not all(k in index for k in t.mapping):
                    continue
                for fld in fields:
                    if fld != key and all(isinstance(index[k].get(fld), Decimal) and index[k][fld] == v
                                          for k, v in t.mapping.items()):
                        hit = (ds, key, fld)
                        break
                if hit:
                    break
            if hit:
                out[name] = hit
                break
    return out


def _reachable(rs: RuleSet) -> set[str]:
    todo = []
    for where, ast in _parsed(rs):
        if not where.startswith("variables."):
            todo += [n.id for n in walk(ast) if isinstance(n, Name)]
    seen: set[str] = set()
    while todo:
        n = todo.pop()
        if n in seen or n not in rs.variables:
            continue
        seen.add(n)
        try:
            todo += [x.id for x in walk(parse(rs.variables[n].expr)) if isinstance(x, Name)]
        except ExprError:
            pass
    return seen


def _all_zero(rs: RuleSet, traces: dict) -> list[str]:
    """按条件筛选明细的变量，在所有对象上都是 0：多半是筛选条件或取值写错了。"""
    out = []
    for n, v in rs.variables.items():
        try:
            ast = parse(v.expr)
        except ExprError:
            continue
        if not any(isinstance(c, Call) and c.kw().get("where") is not None for c in walk(ast)):
            continue
        vals = [t.value for k, t in traces.items() if k.endswith(f"/{n}")]
        if len(vals) >= 3 and all(_zero(x) for x in vals):
            out.append(f"变量 {n} 带筛选条件，在全部 {len(vals)} 个对象上都是 0。核对筛选条件和取值写法是否和数据一致")
    return out


def _zero(x) -> bool:
    try:
        return Decimal(str(x)) == 0
    except (InvalidOperation, ValueError):
        return str(x).strip().lower() in ("false", "否")


_NEG = ("不", "非", "未", "无", "没有")
_PAIRS = (("有", "无"), ("是", "否"), ("已", "未"), ("合格", "不合格"), ("通过", "不通过"))


def _opposite(v: str, lits) -> bool:
    """取值是规则里某个取值的反义写法（参与 / 不参与、有效 / 无效、已复核 / 未复核），规则按“不满足”处理就是对的。"""
    for lit in lits:
        if any(v == n + lit or lit == n + v for n in _NEG):
            return True
        for a, b in _PAIRS:
            for x, y in ((a, b), (b, a)):
                if v.startswith(x) and lit.startswith(y) and v[len(x):] == lit[len(y):]:
                    return True
    return False
