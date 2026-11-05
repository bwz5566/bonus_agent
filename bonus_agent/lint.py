"""程序检查：不靠模型记住提示词，按规则结构和真实数据找出常见的写错，必要时强制提问。

- 照抄：查找表的键和值和某张数据表的两列完全一样（如把品类费率表抄成常数），下一期数据变了规则不跟着变，要改成直接读字段；
- 抵扣：pool 里用 max(应得 − 预发 / 已发, 0) 直接扣了已发金额，发放表会把实发当成应得、已发抵扣和超发显示为 0，要改用 offsets；
- 出处：规则里的每个常数（系数、比例、阈值、单价）都要在材料、财务的话或答复里找得到；
- 取值：规则里和字段比较的文字（如 review == "通过"）要在数据或材料里出现，防止写错取值导致整批被筛掉；
- 未提及的取值：数据里出现了材料和规则都没提到的取值（如工单状态“挂起”），规则会把它悄悄当成不满足条件，要问财务；
- 漏用金额：规则读了某张数据表，却没用到表里的某个金额字段（如算利润只减了成本、漏了费用），要么接进计算，要么说明为什么不用；
- 漏筛状态：规则读了某张数据表，表里有审批 / 状态这类列（取值只有几种），规则却没按它筛选；
- 漏筛期间：带日期的明细表没按日期或期间筛选，数据又跨了几个月（只提示）；
- 分档量纲：分档表的实际传入值全部落在同一档，而且和分档点差一个数量级以上（如传入比值、分档点却按分钟写），其他档从来用不上；
- 覆盖报告：参数表（分档、查表）写了三种以上比例 / 取值，用了很多次（≥10）却只用上一种，等于常数，要对照材料判断；
  查表全部落到默认值（键和数据写法不一致）；条件分支始终只有一种结果（只提示）。程序只如实列出，不预设哪种分布算错；
- 悄悄归零：按比例取的分档表有传入值低于第一档下限（比例成了 0，第一档比例又不是 0）；查表有取值落到默认值、
  又和表里的键写法相近（如“华东区”和“华东”）。其余落到默认值的取值只提示；
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
from bonus_core.dsl.parser import SELF_COMPARE_HINT, BinOp, Call, Cmp, ExprError, Name, Num, Str, parse, self_compares, walk

# 不需要出处的常数：0、1、百分数换算、单位换算（月、天、小时、分钟、周）
FREE = {Decimal(x) for x in ("0", "1", "-1", "12", "24", "60", "7", "365", "366", "0.01")} | {Decimal(10) ** i for i in range(1, 9)}
_SCALE = {"%": Decimal("0.01"), "％": Decimal("0.01"), "‰": Decimal("0.001"), "万元": Decimal(10000), "万": Decimal(10000),
          "亿元": Decimal(10) ** 8, "亿": Decimal(10) ** 8}
_NUM = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(%|％|‰|万元|万|亿元|亿)?")
_DATE = re.compile(r"^\d{4}-\d{1,2}(-\d{1,2})?$")
MAX_CATEGORIES = 30          # 不同取值超过这么多的字段（编号、姓名）不查“未提及的取值”
STATUS_VALUES = 6            # 审批 / 状态列：不同取值不超过这么多
_STATUS = re.compile(r"审批|审核|复核|状态|有效|无效|作废|通过|驳回|撤销|撤回|生效|status|approv|valid|state|review|verif", re.I)
_PERIOD = re.compile(r"year|period|month|quarter|年度|年份|月份|季度|期间|周期|账期", re.I)


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


def percent_only(text: str) -> set[str]:
    """材料里只以百分数（或千分数）出现、从没单独出现过的数（如只写过“0.24%”的 0.24）。
    规则里写成 0.24 多半是漏了 %，金额会差 100 倍。"""
    t = unicodedata.normalize("NFKC", text)
    t = re.sub(r"(?<=\d),(?=\d{3})", "", t)
    pct, bare = set(), set()
    for m in _NUM.finditer(t):
        try:
            k = _key(Decimal(m.group(1)))
        except InvalidOperation:
            continue
        (pct if m.group(2) in ("%", "％", "‰") else bare).add(k)
    return pct - bare


_PCT_CTX = re.compile(r"%|％|比例|占比|百分|分成|分配|提成|系数")


def percent_sources(text: str) -> tuple[set[str], set[str]]:
    """材料里能作为小数比例出处的数：(写成 x% 换算成的 x/100, 不带 % 的数换算成的 x/100)。
    后一种是表格里的数（如“分配比例”一栏的 42.5），前后 300 字内有“%”“比例”这类字样才算。"""
    t = unicodedata.normalize("NFKC", text)
    t = re.sub(r"(?<=\d),(?=\d{3})", "", t)
    pct, ctx = set(), set()
    for m in _NUM.finditer(t):
        try:
            n = Decimal(m.group(1))
        except InvalidOperation:
            continue
        if m.group(2) in ("%", "％"):
            pct.add(_key(n / 100))
        elif not m.group(2) and 1 <= n <= 100 and _PCT_CTX.search(t[max(0, m.start() - 300):m.end() + 300]):
            ctx.add(_key(n / 100))
    return pct, ctx


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
    """规则里写死的数和位置。round 的位数不算；校验里 abs(…) < 0.0001 这类比较容差也不算（只影响校验，不影响金额）。"""
    out = []
    for where, ast in _parsed(rs):
        skip = {id(c.args[1]) for c in walk(ast) if isinstance(c, Call) and c.fn == "round" and len(c.args) > 1}
        if where.startswith("checks."):
            skip |= _tolerances(ast)
        out += [(n.value, where) for n in walk(ast) if isinstance(n, Num) and id(n) not in skip]
    for name, t in rs.tables.items():
        if isinstance(t, TierTable):
            out += [(x, f"tables.{name}") for step in t.steps for x in step]
        elif isinstance(t, LookupTable):
            out += [(x, f"tables.{name}") for x in list(t.mapping.values()) + ([t.default] if t.default is not None else [])]
    return out


def _tolerances(ast) -> set[int]:
    """abs(x) < c、abs(x) <= c（或反过来写）里很小的 c：浮点比较容差。"""
    out = set()
    for c in walk(ast):
        if not isinstance(c, Cmp):
            continue
        for a, b, ops in ((c.left, c.right, ("<", "<=")), (c.right, c.left, (">", ">="))):
            if c.op in ops and isinstance(a, Call) and a.fn == "abs" and isinstance(b, Num) and 0 < b.value <= Decimal("0.01"):
                out.add(id(b))
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


def check(rs: RuleSet, data: Datasets, text: str, traces: dict | None = None, sourced: str = "",
          headers: dict[str, str] | None = None, tiers: dict | None = None, used_elsewhere: set[str] | None = None,
          coverage: list | None = None, reached_elsewhere: set[str] | None = None, said: str = "") -> Report:
    """text：规则材料原文（规则文档、财务的话和答复）。sourced 非空时（如有图片没转写）常数出处无法核对，只提示。
    headers：字段 -> Excel 表头原文（含工作表名），判断字段含义时不只看智能体起的名字。
    tiers：引擎记录的分档表实际取值分布；coverage：查找表、条件分支的命中分布；used_elsewhere：其他方案用到的名字（一个金额字段只要有方案用到就不算漏用）。
    said：财务的话和答复（阶梯算法只要财务说过就算写明）。"""
    rep = Report()
    norm = unicodedata.normalize("NFKC", text)
    nums = material_numbers(text)
    # 0. 查找表照抄了数据表：换期后数据变了，规则还按旧数算，而且不报错
    copied = _copied(rs, data)
    for name, (ds, key, fld) in copied.items():
        rep.gates.append(f"查找表 {name} 照抄了数据表「{ds}」的 {fld}（按 {key} 对应）。下一期数据变了，规则还会按旧数算；"
                         f"删掉这张查找表，直接引用字段 {fld}（行上有 {key} 的表都能按编号读到）")
        rep.keys.append(f"查找表 {name}")
    # 0'. 已发金额扣在 pool 里：应得、已发抵扣、超发登记三列都会错（实发碰巧对）
    paid = _paid_in_pool(rs, headers or {})
    if paid:
        rep.gates.append(f"奖金池用 max(… − …, 0) 直接扣减了已发 / 预发金额（{'、'.join(paid)}）。这样「发放」表会把实发当成应得，"
                         "已发抵扣、超发待核对都显示为 0。改法：pool 写扣减前的应得；已发金额写进 offsets（scope 最末层或人员表，"
                         "expr 写已发合计），引擎算本次实发 = MAX(应得 − 已发, 0)，超发待核对 = MAX(已发 − 应得, 0)")
        rep.keys.append("抵扣 pool")
    # 0''. 字段和自己比较（id == id）：永远成立，多半是想按当前对象筛选却写错了
    for where, ast in _parsed(rs):
        same = self_compares(ast)
        if same:
            rep.gates.append(f"{where} 里的 {'、'.join(dict.fromkeys(same))} {SELF_COMPARE_HINT}")
            rep.keys.append(f"同字段比较 {where}")
    # 1. 常数要有出处；材料里只写成百分数的，规则里不能漏了 %
    miss: dict[str, set[str]] = {}
    unit: dict[str, set[str]] = {}
    scaled, in_table = percent_sources(text)
    pct_only = percent_only(text) - scaled      # 材料里另有 25% 的，0.25 不算漏了百分号
    nums = nums | in_table
    for n, where in constants(rs):
        if where.removeprefix("tables.") in copied:
            continue
        if _key(abs(n)) in pct_only and abs(n) not in FREE:
            unit.setdefault(_key(abs(n)), set()).add(where)
            continue
        if n in FREE or abs(n) in FREE or _key(abs(n)) in nums:
            continue
        miss.setdefault(_key(n), set()).add(where)
    for k, ws in unit.items():
        rep.gates.append(f"常数 {k}（{'、'.join(sorted(ws))}）在材料里只以 {k}% 出现：规则里应写 {_key(Decimal(k) / 100)}"
                         f"（或写成 \"{k}%\"），现在会大 100 倍。确实就是 {k} 才保留")
        rep.keys.append(f"百分号 {k}")
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
            if lit in have:
                continue
            if lit not in norm:
                rep.gates.append(f"「{fld}」写的取值「{lit}」（{'、'.join(sorted(ws))}）在数据和材料里都没有；数据里的取值是："
                                 f"{'、'.join(list(have)[:12])}。按数据的实际写法改，对应关系拿不准就 ask_question")
                rep.keys.append(f"取值 {lit}")
                continue
            # 材料里的叫法和数据的写法不一样（如材料写“客户经理”，数据是“客户经理1”“客户经理2”；“南方国家区”和“南方国家部”）：
            # 规则照材料写，一行都对不上，这部分悄悄成了 0
            near = _near_values(lit, have)
            if near:
                rep.gates.append(f"「{fld}」写的取值「{lit}」（{'、'.join(sorted(ws))}）是材料里的叫法，数据里没有这个取值，"
                                 f"规则按它比较一行都对不上。数据里相近的取值：{'、'.join(f'{v}（{have[v]} 行）' for v in near[:8])}。"
                                 "按数据的实际写法改（对应几个取值就都写上，用 or 连接）；材料的叫法和数据对应不上或拿不准，用 ask_question 问财务")
                rep.keys.append(f"取值 {lit}")
            elif len(have) <= MAX_CATEGORIES:
                rep.warns.append(f"「{fld}」写的取值「{lit}」（{'、'.join(sorted(ws))}）在数据里没有，按它比较一行都对不上；"
                                 f"数据里的取值是：{'、'.join(list(have)[:12])}。核对是否是数据的另一种写法")
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
    # 3'. 名称和百分比写在一格里（如“张三50%”）：会被当成一个新的对象、全额参与，比例丢了
    from bonus_agent.auto import _NAME_PCT
    # 只看规则用到的字段和对象编号（主键、关联、层级键）：备注这类文字列里的“回款比例50%”不算
    keyed = _names(rs) | {d.key for d in rs.datasets.values() if d.key} | {d.link for d in rs.datasets.values() if d.link} \
        | {lv.key for lv in rs.allocation} | {lv.parent_key for lv in rs.allocation if lv.parent_key}
    for fld, have in values.items():
        if fld not in keyed:
            continue
        mixed = sorted(v for v in have if _NAME_PCT.match(v.strip()))
        if mixed and len(mixed) < len(have):
            rep.gates.append(f"数据里「{fld}」有 {len(mixed)} 种取值把名称和百分比写在一格里（如 {'、'.join(mixed[:4])}），"
                             "现在会被当成一个单独的对象、按整行参与计算，百分比没起作用。用 ask_question 问财务这些格子怎么拆"
                             "（是谁、各占多少），问题里列出这些写法；答复后按答复拆分")
            rep.keys.append(f"名称百分比 {mixed[0]}")
    # 4. 读了数据表却漏用了表里的金额字段
    for ds, fld in _unused_money(rs, used_elsewhere or set()):
        head = (headers or {}).get(f"{ds}.{fld}", "").strip()
        rep.gates.append(f"规则读了数据表「{ds}」，但没有用到它的金额字段 {fld}{f'（表头：{head}）' if head else ''}。"
                         "核对材料：这个金额是否应该计入（如利润要扣的费用、要扣的成本或已发金额）；确实与本方案无关，写进 ignored 说明原因")
        rep.keys.append(f"金额 {fld}")
    # 4'. 读了数据表却没按审批 / 状态列筛选：未审批、作废的记录会被算进去
    for ds, fld, vals in _unused_status(rs, data, headers or {}):
        head = (headers or {}).get(f"{ds}.{fld}", "").strip()
        rep.gates.append(f"规则读了数据表「{ds}」，但没有用 {fld}{f'（表头：{head}）' if head else ''} 筛选，它的取值有："
                         f"{'、'.join(vals)}。审批、状态这类列通常决定哪些记录能计入（如只取审批通过、有效的）。"
                         "核对材料：有要求就在 where 里加上；材料说全部计入或这一列与本方案无关，写进 ignored 说明原因")
        rep.keys.append(f"状态 {fld}")
    # 4''. 带日期的明细表没按期间筛选：别的期间的记录也会算进来
    for ds, fld, span in _no_period(rs, data, headers or {}):
        rep.warns.append(f"规则汇总了数据表「{ds}」，但没有按日期（{fld}）或期间筛选，数据跨 {span}，会把这些月份的记录全部算进来。"
                         "核对材料规定的统计期间；本来就该全算的可以不管")
    # 5. 分档表的传入值和分档点量纲不一致
    for name, msg in _tier_scale(rs, tiers or {}):
        rep.gates.append(msg)
        rep.keys.append(f"分档 {name}")
    # 6. 覆盖报告：参数表写了几档 / 几项却只用上一种，条件始终一种结果
    scaled = {k.removeprefix("分档 ") for k in rep.keys}
    cov_gates, cov_warns = _coverage(rs, tiers or {}, coverage or [])
    for name, msg in cov_gates:
        if name not in scaled:
            rep.gates.append(msg)
            rep.keys.append(f"参数表 {name}")
    rep.warns += cov_warns
    # 6'. 阶梯直接算金额（tier）时，全额累进还是超额累进算出来差很多；材料和答复都没说是哪种，不能自己定
    # 只认财务的话 / 答复，或材料里这张表的分档点、比例附近写明的算法；材料别处出现“累进”“分段”不算
    if norm.strip() and not _TIER_HOW.search(unicodedata.normalize("NFKC", said)):   # 没有材料原文（只校验结构）时不查
        for t, how in sorted(_tier_amount_tables(rs).items()):
            tb = rs.tables[t]
            if _tier_stated(norm, tb):
                continue
            algo = ("超额部分分段累加" if tb.mode == "excess" else "整笔按所在档的比例") if how == "tier" else \
                "用 tier_rate 取所在档的比例再乘金额，等于整笔按所在档的比例（全额）"
            (rep.warns if sourced else rep.gates).append(
                f"阶梯表 {t} 现在是{algo}。材料里这张表附近和财务答复都没说是全额（整笔按所在档）还是超额累进（每段按各自比例），"
                "两种算法差很多，不能默认。用 ask_question 问财务，问题里各举一个金额按两种算法的结果")
            if not sourced:       # 没有规则文档可核对时只提示
                rep.keys.append(f"阶梯方式 {t}")
    # 7. 参数表、用参数表算的变量定义了，却没接进计算（如系数表建了、提成里没乘）：要么接上，要么说明为什么不用
    used = _reachable(rs)
    live = reached(rs) | (reached_elsewhere or set())
    in_vars = {x.id for w, ast in _parsed(rs) if w.startswith("variables.") for x in walk(ast) if isinstance(x, Name)}
    for t in rs.tables:
        if t not in live and t not in in_vars:      # 有变量用它时，下面提示那个变量
            rep.gates.append(f"参数表 {t} 定义了，但奖金池、变量、权重、发放、抵扣和校验都没有用到。"
                             "核对材料：它对应的规则（如系数、扣减、暂缓、冻结）是否漏接进计算；确实不用，删掉它或写进 ignored 说明原因")
            rep.keys.append(f"未用 {t}")
    for n, v in rs.variables.items():
        if n in live:
            continue
        try:
            tabs = {x.id for x in walk(parse(v.expr)) if isinstance(x, Name) and x.id in rs.tables}
        except ExprError:
            continue
        if tabs:
            rep.gates.append(f"变量 {n} 按参数表 {'、'.join(sorted(tabs))} 算出来，但没有接进奖金池、权重、发放或抵扣。"
                             "核对材料：这一项（如系数、扣减、暂缓）是否应该乘进或扣进金额；只是展示用的写进 ignored 说明原因")
            rep.keys.append(f"未用 {n}")
    # 结构提示
    flagged = {k.removeprefix("未用 ") for k in rep.keys}
    idle = [n for n in rs.variables if n not in used and n not in flagged]
    if idle:
        rep.warns.append(f"变量 {'、'.join(idle[:10])} 定义了，但奖金池、权重、发放、抵扣和校验都没有用到。"
                         "是不是漏接进计算了（如封顶、扣减）？只是展示用的可以不管")
    if traces:
        rep.warns += _all_zero(rs, traces)
    return rep


def _names(rs: RuleSet) -> set[str]:
    return {n.id for _, ast in _parsed(rs) for n in walk(ast) if isinstance(n, Name)}


def _read(rs: RuleSet, names: set[str]) -> list[str]:
    """规则读到的数据表：用到了表里键以外的字段，或按表名聚合。"""
    text = " ".join(e for _, e in _exprs(rs))
    return [ds for ds, d in rs.datasets.items()
            if any(f in names for f in d.fields or {} if f not in (d.key, d.link)) or f'"{ds}"' in text]


def _unused_money(rs: RuleSet, elsewhere: set[str]) -> list[tuple[str, str]]:
    """(数据表, 金额字段)：表里有字段被规则用到，金额字段本身却哪个方案都没用到。"""
    names = _names(rs) | elsewhere
    return [(ds, f) for ds in _read(rs, names) for f, fd in (rs.datasets[ds].fields or {}).items()
            if fd.type == "money" and f not in names]


def _table_names(rs: RuleSet) -> tuple[dict[str, set[str]], set[str]]:
    """按表区分用到的名字：({表名: 在 of: "表名" 的聚合里用到的名字}, 其他地方用到的名字)。
    同名字段（如几张表都有 approved）在一张表上筛选了，不等于另一张表也筛选了。"""
    by: dict[str, set[str]] = {}
    inside: set[int] = set()
    loose: set[str] = set()
    for _, ast in _parsed(rs):
        for c in walk(ast):
            if isinstance(c, Call) and isinstance(c.kw().get("of"), Str):
                ns = [n for n in walk(c) if isinstance(n, Name)]
                by.setdefault(c.kw()["of"].value, set()).update(n.id for n in ns)
                inside |= {id(n) for n in ns}
        loose |= {n.id for n in walk(ast) if isinstance(n, Name) and id(n) not in inside}
    return by, loose


def _read_by_table(rs: RuleSet, by: dict[str, set[str]], loose: set[str]) -> list[str]:
    """本方案确实读到的表：按 of: "表名" 聚合过，或用到了只有这张表才有的字段（同名字段不算，免得把没用的补充表也算进来）。"""
    owners: dict[str, int] = {}
    for d in rs.datasets.values():
        for f in d.fields or {}:
            owners[f] = owners.get(f, 0) + 1
    return [ds for ds, d in rs.datasets.items()
            if ds in by or any(f in loose and owners[f] == 1 for f in d.fields or {} if f not in (d.key, d.link))]


def _label(ds: str, f: str, fd, headers: dict[str, str]) -> str:
    return f"{f} {headers.get(f'{ds}.{f}', '')} {fd.desc or ''}"


def _unused_status(rs: RuleSet, data: Datasets, headers: dict[str, str]) -> list[tuple[str, str, list[str]]]:
    """(数据表, 字段, 取值)：规则读了这张表，表里有审批 / 状态这类列（取值只有几种），本方案却没按它筛选。"""
    by, loose = _table_names(rs)
    out = []
    for ds in _read_by_table(rs, by, loose):
        d, t = rs.datasets[ds], data.get(ds)
        if t is None:
            continue
        used = by.get(ds, set()) | loose
        for f, fd in (d.fields or {}).items():
            if f in used or f in (d.key, d.link) or fd.type not in ("text", "enum", "bool"):
                continue
            vals = list(dict.fromkeys(str(r.values.get(f)).strip() for r in t.rows if r.values.get(f) not in (None, "")))
            if 2 <= len(vals) <= STATUS_VALUES and (_STATUS.search(_label(ds, f, fd, headers))
                                                    or any(_STATUS.search(v) for v in vals)):
                out.append((ds, f, vals))
    return out


def _no_period(rs: RuleSet, data: Datasets, headers: dict[str, str]) -> list[tuple[str, str, str]]:
    """(数据表, 日期字段, 数据跨的月份)：本方案汇总了一张带日期的明细表，却没用任何日期或期间字段筛选，数据又跨了几个月。"""
    by, loose = _table_names(rs)
    out = []
    for ds in _read_by_table(rs, by, loose):
        d, t = rs.datasets[ds], data.get(ds)
        if t is None or d.key:                      # 实体表（人员、项目）的日期多是入职、签约日，不是统计期间
            continue
        fields = d.fields or {}
        used = by.get(ds, set()) | loose
        dates = [f for f, fd in fields.items() if fd.type == "date"]
        if not dates or any(f in used for f in dates) or \
                any(f in used and _PERIOD.search(_label(ds, f, fd, headers)) for f, fd in fields.items()):
            continue
        months = sorted({str(v)[:7] for r in t.rows for f in dates if (v := r.values.get(f)) not in (None, "")})
        if len(months) > 1:
            out.append((ds, dates[0], f"{months[0]}～{months[-1]}"))
    return out


def _fmt(x) -> str:
    return format(Decimal(str(x)).normalize(), "f")


SPREAD_N = 10                  # 参数表用了不少于这么多次、却只用上同一档 / 同一项（表里三种以上取值）：要处理


def _tier_scale(rs: RuleSet, tiers: dict) -> list[tuple[str, str]]:
    """(表名, 提示)。全部传入值落在同一档、而且和相邻分档点差 5 倍以上：量纲多半不一致，必须处理。"""
    out = []
    for name, st in tiers.items():
        t = rs.tables.get(name)
        if not isinstance(t, TierTable) or len(t.steps) < 2 or st.get("n", 0) < 3 or len(st.get("bands", {})) != 1:
            continue
        lo, hi = Decimal(str(st["min"])), Decimal(str(st["max"]))
        cuts = [Decimal(a) for a, _ in t.steps]
        band = next(iter(st["bands"]))
        nxt = cuts[band + 1] if band + 1 < len(cuts) else None
        cur = cuts[band] if band >= 0 else None
        if (nxt is not None and nxt > 0 and hi >= 0 and hi * 5 < nxt) or (cur is not None and cur > 0 and lo > cur * 5 and nxt is None):
            out.append((name, f"分档表 {name} 的分档点是 {'、'.join(_fmt(c) for c in cuts)}，实际传入的 {st['n']} 个值在 "
                              f"{_fmt(lo)}～{_fmt(hi)} 之间，全部落在同一档，而且和分档点差了一个数量级以上，量纲多半不一致"
                              "（如一个是比值、一个是分钟数或百分数，一个是元、一个是万元）。核对传入的表达式和分档点是否同一单位；"
                              "确实如此就写进 ignored 说明原因"))
    return out


def _coverage(rs: RuleSet, tiers: dict, coverage: list) -> tuple[list[tuple[str, str]], list[str]]:
    """覆盖报告：参数表（分档、查表）和条件分支在数据上实际用到了哪些档、哪些项、哪个分支。
    程序不预设哪种分布算错，只把“写了却从没用上”的如实列出来：参数表用了很多次却只用上一档 / 一项，等于一个常数，
    要智能体对照材料判断（改规则或写明原因）；查表全部落到默认值，说明键和数据对不上；条件始终只有一种结果只提示。
    返回 ([(表名, 必须处理的提示)], [提示])。"""
    hits: dict[str, dict] = {}
    for name, st in tiers.items():
        t = rs.tables.get(name)
        if isinstance(t, TierTable):
            kinds = len({r for _, r in t.steps})
            hits[name] = {"n": st.get("n", 0), "seen": {f"第 {b + 1} 档" if b >= 0 else "第一档以下": c
                                                        for b, c in st.get("bands", {}).items()}, "kinds": kinds, "unit": "档"}
    for c in coverage:
        t = rs.tables.get(c["what"]) if c["kind"] == "lookup" else None
        if isinstance(t, LookupTable):
            h = hits.setdefault(c["what"], {"n": 0, "seen": {}, "unit": "项",
                                            "kinds": len(set(t.mapping.values()) | ({t.default} if t.default is not None else set()))})
            for k, n in c["seen"].items():
                h["n"] += n
                h["seen"][k] = h["seen"].get(k, 0) + n
    gates = []
    for name, h in hits.items():
        if len(h["seen"]) != 1:
            continue
        only, n = next(iter(h["seen"].items()))
        if only == "默认值" and n >= 3:
            gates.append((name, f"查找表 {name} 查了 {n} 次，全部没有对上表里的键、落到了默认值：表里的键和数据的写法多半不一致"
                                f"（如“A类”和“A”、全角半角、多了空格），或者查的列不对。按数据的实际写法改键或改查的字段；"
                                "数据确实都不在表里就写进 ignored 说明原因"))
        elif n >= SPREAD_N and h["kinds"] >= 3:
            gates.append((name, f"参数表 {name} 写了 {h['kinds']} 种{'比例' if h['unit'] == '档' else '取值'}，实际用了 {n} 次，"
                                f"全部是{only}，其他{h['unit']}从来没用上，这张表在本次数据上等于一个常数。要么数据确实都在这一{h['unit']}"
                                f"（写进 ignored 说明原因），要么传进来的值不对：对照材料核对传入的表达式（取的哪一列、分子分母、"
                                "相减的项、比值还是百分数、先汇总还是逐个对象算）"))
    # 分档表按比例取（全额累进或 tier_rate），有传入值低于第一档的下限：比例悄悄成了 0。第一档比例本来就是 0 的不算
    for name, lo, below, low in tier_holes(rs, tiers):
        if name not in {g for g, _ in gates}:
            t = rs.tables[name]
            gates.append((name, f"分档表 {name} 的第一档从 {_fmt(lo)} 起（比例 {_fmt(t.steps[0][1])}"
                                f"{'，不含 ' + _fmt(lo) + ' 本身' if not t.lower_inclusive else ''}），实际有 {below} 个传入值"
                                f"不在任何一档里（最小 {_fmt(low)}），这些对象按规则拿到的比例是 0。核对材料对这些值的规定："
                                f"不发就在最前面加一档写明（下限写到比可能的最小值还小，比例 0），按第一档发就把第一档的下限往下放；"
                                "材料没说就问财务"))
    # 查找表部分取值落到默认值：没对上的键和表里某个键写法相近（全角半角、空格、多了后缀），多半是写法不一致
    misses: dict[str, dict[str, int]] = {}
    for c in coverage:
        if c["kind"] == "lookup" and c.get("misses"):
            misses.setdefault(c["what"], c["misses"])
    done = {g for g, _ in gates}
    for name, miss in misses.items():
        t = rs.tables.get(name)
        if not isinstance(t, LookupTable) or name in done:
            continue
        near = [(k, m) for k in miss for m in t.mapping if _near_key(k, m)]
        if near:
            gates.append((name, f"查找表 {name} 有取值没对上表里的键、落到了默认值，但和表里的键写法相近："
                                + "、".join(f"数据里的「{k}」（{miss[k]} 次）↔ 表里的「{m}」" for k, m in near[:5])
                                + "。多半是写法不一致：按数据的实际写法改键；确实是不同的取值、按默认值处理是材料规定的，就写进 ignored 说明原因"))
    one = [(c["item"], c["what"], *next(iter(c["seen"].items()))) for c in coverage
           if c["kind"] == "if" and len(c["seen"]) == 1 and sum(c["seen"].values()) >= 3]
    warns = []
    for name, miss in misses.items():
        if name not in {g for g, _ in gates}:
            warns.append(f"查找表 {name} 有这些取值不在表里、按默认值算："
                         + "、".join(f"「{k}」{n} 次" for k, n in list(miss.items())[:8])
                         + "。核对材料是否规定这些取值按默认值处理")
    if one:
        warns.append("这些条件在数据上始终只有一种结果：" + "；".join(
            f"{item} 里的 {what} {'始终成立' if r == '是' else '始终不成立'}（{n} 次）" for item, what, r, n in one[:6])
            + ("……" if len(one) > 6 else "") + "。防零、资格这类保护条件不触发很正常；筛选、分类条件始终一种结果的，核对取值和写法")
    return gates, warns


def tier_holes(rs: RuleSet, tiers: dict) -> list[tuple[str, Decimal, int, Decimal]]:
    """按比例取的分档表（全额累进，或用 tier_rate 取比例）有传入值低于第一档下限（不含下限时等于下限也算），
    第一档比例又不是 0：这些值的比例悄悄成了 0。返回 [(表名, 第一档下限, 个数, 最小传入值)]。"""
    out = []
    for name, st in tiers.items():
        t = rs.tables.get(name)
        below = st.get("bands", {}).get(-1, 0)
        if (isinstance(t, TierTable) and t.steps and below and Decimal(t.steps[0][1]) != 0
                and (t.mode == "whole" or st.get("rate"))):
            out.append((name, Decimal(t.steps[0][0]), below, Decimal(str(st["min"]))))
    return out


def _norm_key(k: str) -> str:
    return re.sub(r"[\s()（）\[\]【】「」\"'_\-·.,，、:：;；/]", "", unicodedata.normalize("NFKC", str(k))).casefold()


def _near_key(a: str, b: str) -> bool:
    """两个取值写法相近：去掉空格、括号、标点、全半角和大小写后相同，或一个是另一个加了前后缀（较短的至少 2 个字）。"""
    x, y = _norm_key(a), _norm_key(b)
    if not x or not y or a == b:
        return False
    if x == y:
        return True
    short, long_ = sorted((x, y), key=len)
    return len(short) >= 2 and (long_.startswith(short) or long_.endswith(short))


_PAID = re.compile(r"预发|预付|预支|已发|已付|已支付|已领|prepa|paid|advance", re.I)


def _paid_in_pool(rs: RuleSet, headers: dict[str, str]) -> list[str]:
    """奖金池（顺着变量展开）里 max(a − b, 0) 的 b 取自预发 / 已发类数据的字段。字段含义按表名、字段名、说明和
    Excel 表头原文判断（表头是数据里的原文，改字段名绕不过去）。"""
    paid_fields = {f for n, d in rs.datasets.items() for f, fd in d.fields.items()
                   if any(_PAID.search(x or "") for x in (n, f, fd.desc, headers.get(f"{n}.{f}")))}

    def names_of(node, seen=()) -> set[str]:
        out = set()
        for x in walk(node):
            if isinstance(x, Name):
                if x.id in rs.variables and x.id not in seen:
                    try:
                        out |= names_of(parse(rs.variables[x.id].expr), seen + (x.id,))
                    except ExprError:
                        pass
                out.add(x.id)
        return out

    def scan(src: str, seen=()) -> list[str]:
        try:
            ast = parse(src)
        except ExprError:
            return []
        hits = []
        for c in walk(ast):
            if isinstance(c, Call) and c.fn == "max" and len(c.args) == 2 and isinstance(c.args[1], Num) \
                    and c.args[1].value == 0 and isinstance(c.args[0], BinOp) and c.args[0].op == "-":
                hits += sorted(names_of(c.args[0].right) & paid_fields)
            if isinstance(c, Name) and c.id in rs.variables and c.id not in seen:
                hits += scan(rs.variables[c.id].expr, seen + (c.id,))
        return hits
    return list(dict.fromkeys(scan(rs.pool.expr)))


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


_TIER_HOW = re.compile(r"累进|超额|分段|全额|整笔|全部按|按所在档|按达到的档|按达成的档")


def _tier_amount_tables(rs: RuleSet) -> dict[str, str]:
    """按金额分档算提成的阶梯表 -> 用法（tier / tier_rate）：tier(...) 直接算金额的（至少 2 档）；
    tier_rate(...) 取比例的，各档比例都像提成比例（都小于 0.3、至少两档不同且不为 0），不是 0.8、1.1 这类系数。"""
    out: dict[str, str] = {}
    for t in _tier_called(rs):
        tb = rs.tables.get(t)
        if isinstance(tb, TierTable) and len(tb.steps) >= 2:
            out[t] = "tier"
    for _, ast in _parsed(rs):
        for c in walk(ast):
            if isinstance(c, Call) and c.fn == "tier_rate" and c.args and isinstance(c.args[0], Name):
                tb = rs.tables.get(c.args[0].id)
                if not isinstance(tb, TierTable) or c.args[0].id in out:
                    continue
                rates = [Decimal(str(r)) for _, r in tb.steps]
                if len({r for r in rates if r}) >= 2 and all(Decimal(0) <= r < Decimal("0.3") for r in rates):
                    out[c.args[0].id] = "tier_rate"
    return out


def _tier_stated(norm: str, tb: TierTable) -> bool:
    """材料里这张表的分档点或比例出现的地方，前后 300 字内写了全额 / 超额 / 累进这类算法。"""
    want = set()
    for lo, r in tb.steps:
        for x in (lo, r):
            try:
                d = Decimal(str(x))
            except InvalidOperation:
                continue
            if d not in (Decimal(0), Decimal(1)):
                want.add(_key(d))
    t = re.sub(r"(?<=\d),(?=\d{3})", "", norm)
    for m in _NUM.finditer(t):
        try:
            n = Decimal(m.group(1))
        except InvalidOperation:
            continue
        keys = {_key(n)} | ({_key(n * _SCALE[m.group(2)])} if m.group(2) else set())
        if keys & want and _TIER_HOW.search(t[max(0, m.start() - 300):m.end() + 300]):
            return True
    return False


def _tier_called(rs: RuleSet) -> set[str]:
    """用 tier(...) 直接算金额的阶梯表名。"""
    return {c.args[0].id for _, ast in _parsed(rs) for c in walk(ast)
            if isinstance(c, Call) and c.fn == "tier" and c.args and isinstance(c.args[0], Name)}


def reached(rs: RuleSet) -> set[str]:
    """计算真正用到的名字：从奖金池、权重、发放、抵扣、校验出发能到达的变量，以及它们引用的参数表名、字段名。"""
    seen = _reachable(rs)
    out = set(seen)
    for where, ast in _parsed(rs):
        if not where.startswith("variables.") or where.removeprefix("variables.") in seen:
            out |= {x.id for x in walk(ast) if isinstance(x, Name)}
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


def _near_values(lit: str, have: dict[str, int]) -> list[str]:
    """数据里和规则取值写法相近的取值：互相包含（去掉空格、大小写），或字面相似度 ≥ 0.75（至少 2 个字相同）。"""
    import difflib

    def plain(x: str) -> str:
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", x)).lower()
    a = plain(lit)
    if not a:
        return []
    out = []
    for v in have:
        b = plain(v)
        if not b or b == a:
            if b == a:
                out.append(v)
            continue
        if (len(a) >= 2 and a in b) or (len(b) >= 2 and b in a):
            out.append(v)
        elif min(len(a), len(b)) >= 2 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.75:
            out.append(v)
    return sorted(out, key=lambda v: (-difflib.SequenceMatcher(None, a, plain(v)).ratio(), -have[v]))


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
