"""把智能体算出的结果和财务给的期望结果（Excel）逐个对象核对。

期望结果只用于事后核对，不会交给智能体。表格格式不固定，按内容自动识别：
- 编号列：取值和结果里的对象编号（D01、G01、E001……）重合最多的列；
- 方案列 / 修订列（可选）：取值是方案名（QUARTER……）或修订名（R0、R1……）的列；工作表名里含方案名、修订名也算；
- 金额列：按列名认指标（应得、本次可发、暂缓、已发抵扣、超发），同一指标有几列时取逐分一致最多的那一列；
  应得金额找不到带名字的列时，退回到所有数值列里一致最多的那一列。
部门、小组、个人分在几张表里也可以：每张表各找各的列，合起来核对。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from bonus_core.data.mapping import cell_text
from bonus_core.data.table_detect import detect
from bonus_core.engine.run import Result

TOL = Decimal("0.005")
# 指标 -> 列名关键字（小写比较）
METRICS = {
    "应得": ("应得", "奖金", "bonus", "amount", "final", "金额"),
    "本次可发": ("本次可发", "本次实发", "实发", "paid"),
    "暂缓": ("暂缓", "挂起", "held"),
    "已发抵扣": ("已发抵扣", "抵扣", "offset"),
    "超发待核对": ("超发", "overpaid"),
}
EXCLUDE = ("差异", "diff", "权重", "weight", "比例", "ratio", "快照", "上限", "目标", "target", "销售", "sales",
           "回款", "利润", "激励", "系数")


@dataclass
class Match:
    scheme: str
    revision: str
    metric: str = "应得"
    sources: list[tuple[str, str]] = field(default_factory=list)    # (工作表, 列名)
    matched: int = 0
    total: int = 0
    diffs: list[tuple[str, Decimal, Decimal]] = field(default_factory=list)   # (编号, 期望, 实际)

    @property
    def ok(self) -> bool:
        return self.total > 0 and self.matched == self.total

    @property
    def sheet(self) -> str:
        return "、".join(dict.fromkeys(s for s, _ in self.sources))

    @property
    def column(self) -> str:
        return "、".join(dict.fromkeys(c for _, c in self.sources))


def _num(v) -> Decimal | None:
    if isinstance(v, bool) or v in (None, ""):
        return None
    try:
        return Decimal(str(v).replace(",", ""))
    except InvalidOperation:
        return None


def _figures(res: Result) -> dict[str, dict[str, Decimal]]:
    """每个指标：对象编号 -> 金额。发放类指标在最末层，上级为下级合计。"""
    out = {"应得": {n.key: Decimal(n.final) for n in res.nodes.values() if n.key}}
    if not res.payouts:
        return out
    for m, attr in (("本次可发", "paid"), ("暂缓", "held"), ("已发抵扣", "offset"), ("超发待核对", "overpaid")):
        acc: dict[str, Decimal] = {}
        for nid, p in res.payouts.items():
            v = Decimal(getattr(p, attr) or 0)
            n = res.nodes.get(nid)
            while n is not None:
                if n.key:
                    acc[n.key] = acc.get(n.key, Decimal(0)) + v
                n = res.nodes.get(n.parent) if n.parent else None
        out[m] = acc
    return out


def compare_expected(results: dict[str, dict[str, Result]], path: str | Path) -> list[Match]:
    tables = detect(path)
    out = []
    for scheme, revs in results.items():
        for rev, res in revs.items():
            for metric, ours in _figures(res).items():
                total = Match(scheme, rev, metric)
                seen: set[str] = set()
                for st in tables:
                    m = _best_in(st, scheme, rev, metric, ours, list(results), list(revs))
                    fresh = [(k, e, a) for k, e, a in m.diffs if k not in seen]
                    keys = m.keys - seen
                    if not keys:
                        continue
                    seen |= keys
                    total.sources.append((st.sheet, m.column))
                    total.total += len(keys)
                    total.matched += len(keys) - len(fresh)
                    total.diffs += fresh
                if total.total or metric == "应得":
                    out.append(total)
    return out


def _mentions(name: str, own: str, others: list[str]) -> bool:
    """工作表名提到了别的方案（修订）而没提到自己的，说明不是这一张。"""
    up = name.upper()
    return own.upper() not in up and any(o.upper() in up for o in others if o != own)


@dataclass
class _SheetMatch:
    column: str = ""
    keys: set[str] = field(default_factory=set)
    diffs: list = field(default_factory=list)

    @property
    def matched(self) -> int:
        return len(self.keys) - len(self.diffs)


def _best_in(st, scheme, rev, metric, ours, schemes, revs) -> _SheetMatch:
    if _mentions(st.sheet, scheme, schemes) or _mentions(st.sheet, rev, revs):
        return _SheetMatch()
    cols = list(zip(*[vals for _, vals in st.rows])) if st.rows else []
    texts = [[cell_text(v) for v in c] for c in cols]
    keep = [True] * len(st.rows)
    for ts in texts:     # 方案列、修订列：取值里有本方案（修订）名的列，只留这些行
        vals = {t.upper() for t in ts if t}
        for own, names in ((scheme, schemes), (rev, revs)):
            if len(names) > 1 and own.upper() in vals and vals <= {n.upper() for n in names} | {"R0"}:
                keep = [k and t.upper() == own.upper() for k, t in zip(keep, ts)]

    def id_score(i):     # 编号列：留下的行里编号不重复（“部门”这种上级编号列会重复），与结果重合最多
        hit = [t for t, k in zip(texts[i], keep) if k and t in ours]
        return len(hit) if hit and len(set(hit)) == len(hit) else 0
    id_col = max(range(len(cols)), key=id_score, default=None)
    if id_col is None or not id_score(id_col):
        return _SheetMatch()
    heads = [str(h or "").lower() for h in st.headers]
    others = [kw for m, kws in METRICS.items() if m != metric for kw in kws if m != "应得"]

    def named(j):
        h = heads[j]
        return any(kw in h for kw in METRICS[metric]) and not any(x in h for x in EXCLUDE) and \
            (metric != "应得" or not any(kw in h for kw in others))

    cand = [j for j in range(len(cols)) if j != id_col and named(j)]
    if not cand and metric == "应得":
        cand = [j for j in range(len(cols)) if j != id_col]
    best = _SheetMatch()
    for j in cand:
        pairs = [(texts[id_col][r], _num(cols[j][r])) for r in range(len(cols[j])) if keep[r] and texts[id_col][r] in ours]
        pairs = [(k, v) for k, v in pairs if v is not None]
        if not pairs:
            continue
        m = _SheetMatch(st.headers[j], {k for k, _ in pairs}, [(k, v, ours[k]) for k, v in pairs if abs(v - ours[k]) > TOL])
        if (m.matched, -len(m.diffs)) > (best.matched, -len(best.diffs)):
            best = m
    return best


def report(matches: list[Match]) -> list[str]:
    """每个方案（修订）一行：全部一致时只列各指标的核对个数，有不一致的列出前 10 个。"""
    lines, groups = [], {}
    for m in matches:
        groups.setdefault((m.scheme, m.revision), []).append(m)
    for (scheme, rev), ms in groups.items():
        label = scheme + ("" if rev == "R0" else f" {rev}")
        found = [m for m in ms if m.total]
        if not found:
            lines.append(f"「{label}」：期望结果里没有找到对应的金额")
            continue
        head = "、".join(f"{m.metric} {m.matched}/{m.total}" for m in found)
        bad = [m for m in found if not m.ok]
        line = f"「{label}」：{head} 个对象" + ("逐分一致" if not bad else "核对")
        for m in bad:
            line += (f"；{m.metric}不一致（对照{'；'.join(f'「{s}」的「{c}」列' for s, c in m.sources)}）："
                     + "；".join(f"{k} 期望 {e:,} 实际 {a:,}" for k, e, a in m.diffs[:10])
                     + ("……" if len(m.diffs) > 10 else ""))
        lines.append(line)
    srcs = dict.fromkeys(f"「{s}」的「{c}」列" for m in matches for s, c in m.sources)
    if srcs:
        lines.append("对照的列：" + "；".join(srcs))
    return lines
