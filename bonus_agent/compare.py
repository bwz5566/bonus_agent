"""把智能体算出的结果和财务给的期望结果（Excel）逐个对象核对。

期望结果只用于事后核对，不会交给智能体。表格格式不固定，按内容自动识别：
- 编号列：取值和结果里的对象编号（D01、G01、E001……）重合最多的列；
- 方案列 / 修订列（可选）：取值是方案名（QUARTER……）或修订名（R0、R1……）的列；工作表名里含方案名、修订名也算；
- 金额列：只按列名认（应得、本次可发、暂缓、已发抵扣、超发），**不看哪一列和结果一致**，否则改错的答案列会被
  旁边一列“正好一致”的数字顶替，造成假通过。列名带“期望 / 标准 / expected”的优先；同一优先级有几列、
  或认不出列名时，判为“无法确定”（不通过），用 --expected-column 指定列名。
部门、小组、个人分在几张表里也可以：每张表各找各的列，合起来核对。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

from bonus_core.data.mapping import cell_text
from bonus_core.data.table_detect import detect
from bonus_core.engine.run import Result

TOL = Decimal("0.005")
# 指标 -> 列名关键字（小写比较）
METRICS = {
    "应得": ("应得", "应发", "奖金", "bonus", "amount", "final", "金额"),
    "本次可发": ("本次可发", "本次实发", "实发", "paid"),
    "暂缓": ("暂缓", "挂起", "held"),
    "已发抵扣": ("已发抵扣", "预发", "已发金额", "offset"),
    "实际抵扣": ("实际抵扣", "本次抵扣", "抵扣", "deducted"),
    "超发待核对": ("超发", "超预发", "overpaid"),
    "汇总核算": ("汇总金额", "核算金额", "总金额"),
}
PRIORITY = (("期望", "标准", "答案", "expected", "golden"), ("应得", "应发"))   # 列名优先级，靠前的优先
EXCLUDE = ("差异", "差额", "diff", "权重", "weight", "比例", "ratio", "快照", "上限", "目标", "target", "销售", "sales",
           "回款", "利润", "激励", "系数", "上轮", "上次", "剩余")      # 差额、上一轮的数、剩余待分的数都不是本次结果


@dataclass
class Match:
    scheme: str
    revision: str
    metric: str = "应得"
    sources: list[tuple[str, str]] = field(default_factory=list)    # (工作表, 列名)
    matched: int = 0
    total: int = 0
    diffs: list[tuple[str, Decimal, Decimal]] = field(default_factory=list)   # (编号, 期望, 实际)
    ambiguous: list[tuple[str, list[str]]] = field(default_factory=list)  # (工作表, 几列都可能是这个指标)
    missing: list[tuple[str, Decimal]] = field(default_factory=list)      # 期望结果里有、本次结果里没有的对象 (编号, 期望)
    blank: list[str] = field(default_factory=list)                         # 本次结果里有、期望结果里金额为空的对象
    extra: list[str] = field(default_factory=list)  # same entity scope, actual objects absent from acceptance
    partial: bool = False

    @property
    def ok(self) -> bool:
        return self.total > 0 and self.matched == self.total and not self.ambiguous and not self.missing and not self.blank and not self.extra

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
    # 分配人员金额（report.people 按人名合计）：期望结果按人列金额时，按人名核对
    for p in getattr(res, "people", None) or []:
        out["应得"].setdefault(str(p["name"]), Decimal(p["final"]))
    for group in getattr(res,'details',{}).values():
        for row in group.values():
            key,amount=str(row['key']),Decimal(row['amount'])
            if key in out['应得'] and out['应得'][key] != amount:
                raise ValueError(f'对象金额报告与分配金额使用相同编号但口径不同：{key}，请明确区分编号')
            out['应得'][key]=amount
    if getattr(res, "totals", None):        # 汇总核算：期望结果里有同名的汇总行时核对
        out["汇总核算"] = {str(n): Decimal(v) for n, v in res.totals}
    if not res.payouts:
        return _id_variants(out)
    for r in res.rollups.values():          # 按关联实体汇总后再抵扣：发放类指标以实体为准
        out["应得"].setdefault(r.key, Decimal(r.final))
    for m, attr in (("本次可发", "paid"), ("暂缓", "held"), ("已发抵扣", "offset"), ("实际抵扣", "deducted"),
                    ("超发待核对", "overpaid")):
        acc: dict[str, Decimal] = {}
        if attr in ("paid", "held"):
            for p in getattr(res, "people", None) or []:
                acc[str(p["name"])] = Decimal(p[attr])
        if res.rollups and attr != "held":
            out[m] = {r.key: Decimal(getattr(r, attr)) for r in res.rollups.values()}
            continue
        for r in res.rollups.values():
            acc[r.key] = Decimal(r.held)
        for nid, p in res.payouts.items():
            v = Decimal(getattr(p, attr) or 0)
            n = res.nodes.get(nid)
            while n is not None:
                if n.key:
                    acc[n.key] = acc.get(n.key, Decimal(0)) + v
                n = res.nodes.get(n.parent) if n.parent else None
        out[m] = acc
    return _id_variants(out)


def _id_variants(figures):
    """Composite IDs may differ only by the program's explicit separator; never choose by money."""
    for values in figures.values():
        aliases={}
        for key in list(values):
            if '|' in key:
                aliases.setdefault(key.replace('|',''),[]).append(key)
        for alias,keys in aliases.items():
            if len(keys)==1 and alias not in values:
                values[alias]=values[keys[0]]
    return figures


def compare_expected(results: dict[str, dict[str, Result]], path: str | Path, column: str | None = None,
                     where: dict[str, str] | None = None, *, complete: bool = True) -> list[Match]:
    """column：指定应得金额的列名（期望结果的列名认不出或有歧义时用）。
    where：列名 -> 取值，只核对这些行（如期望结果里有几个版本，本次只算了其中一个）；没有这一列的工作表不受影响。
    方案名对不上时（智能体起名「季度」，期望结果的方案列写 QUARTER），按一一对应试一遍，取对上金额最多的那种，
    方案名写成「季度（期望结果里是 QUARTER）」。金额不一致照样记不一致，对应关系只影响去哪几行找。"""
    labels = _scheme_labels(path)
    ours = [s for s in results if s.upper() not in {x.upper() for x in labels}]
    free = [x for x in labels if x.upper() not in {s.upper() for s in results}]
    best = _compare(results, path, column, where, complete)
    if not ours or not free or len(ours) > 6 or len(free) > 6:
        return best
    from itertools import permutations
    score = lambda ms: sum(m.matched for m in ms)
    top, pick = score(best), None
    for perm in permutations(free, min(len(ours), len(free))):
        names = dict(zip(ours, perm))
        ms = _compare({names.get(k, k): v for k, v in results.items()}, path, column, where, complete)
        if score(ms) > top:
            top, pick, best = score(ms), names, ms
    if pick:
        back = {v: f"{k}（期望结果里是 {v}）" for k, v in pick.items()}
        for m in best:
            m.scheme = back.get(m.scheme, m.scheme)
    return best


def _scheme_labels(path) -> list[str]:
    """期望结果里“方案”列的取值（列名含 方案 / scheme）。"""
    out: list[str] = []
    for st in detect(path):
        for j, h in enumerate(st.headers):
            if "方案" in str(h or "") or "scheme" in str(h or "").lower():
                out += [t for _, vals in st.rows if (t := cell_text(vals[j])) and t not in out]
    return out


def _coverage_groups(res, metric):
    """Compare within an entity level/view, never require parent IDs in a recipient sheet."""
    groups=[]
    if metric=='汇总核算':
        return [{str(n) for n,_ in res.totals}]
    if metric=='应得' or not res.rollups:
        levels={n.level for n in res.nodes.values() if n.key}
        groups.extend({n.key for n in res.nodes.values() if n.key and n.level==level} for level in levels)
    if res.rollups:
        groups.append({r.key for r in res.rollups.values()})
    if metric in ('应得','本次可发','暂缓'):
        groups.append({str(p['name']) for p in res.people or []})
    if metric=='应得':
        groups.extend({str(r['key']) for r in rows.values()} for rows in getattr(res,'details',{}).values())
    return groups


def _compare(results, path, column, where, complete=True) -> list[Match]:
    tables = detect(path)
    for st in tables:
        for col, val in (where or {}).items():
            if col in st.headers:
                i = st.headers.index(col)
                st.rows = [(n, vals) for n, vals in st.rows if cell_text(vals[i]) == str(val)]
                st.totals = [(n, vals) for n, vals in st.totals if cell_text(vals[i]) == str(val)]
    out = []
    for scheme, revs in results.items():
        for rev, res in revs.items():
            for metric, ours in _figures(res).items():
                total = Match(scheme, rev, metric, partial=not complete)
                seen: set[str] = set()
                for st in tables:
                    # 普通对象表排除合计；显式汇总指标必须核对合计行本身。
                    if metric == '汇总核算':
                        st = replace(st, rows=sorted(st.rows + st.totals, key=lambda row: row[0]))
                    m = _best_in(st, scheme, rev, metric, ours, list(results), list(revs),
                                 column if metric == "应得" else None)
                    if m.ambiguous:
                        total.ambiguous.append((st.sheet, m.ambiguous))
                    fresh = [(k, e, a) for k, e, a in m.diffs if k not in seen]
                    keys = m.keys - seen
                    lost = [(k, e) for k, e in m.missing if k not in seen and k not in {x for x, _ in total.missing}]
                    if not keys and not lost:
                        continue
                    seen |= keys
                    total.missing += lost
                    total.blank += [k for k in m.blank if k not in total.blank]
                    total.total += len(lost)
                    total.sources.append((st.sheet, m.column))
                    total.total += len(keys)
                    total.matched += len(keys) - len(fresh)
                    total.diffs += fresh
                if complete:
                    for group in _coverage_groups(res,metric):
                        covered={k for k in group if k in seen or ('|' in k and k.replace('|','') in seen)}
                        if covered:
                            total.extra += sorted(group-covered-set(total.extra))
                if total.total or total.ambiguous or metric == "应得":
                    out.append(total)
            current=[m for m in out if m.scheme==scheme and m.revision==rev]
            if complete and not any(m.total for m in current):
                current[0].ambiguous.append(('验收范围',['缺少本方案/修订的有效金额对照']))
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
    ambiguous: list[str] = field(default_factory=list)
    missing: list = field(default_factory=list)       # (编号, 期望)：期望里有、结果里没有
    blank: list = field(default_factory=list)         # 编号：结果里有、期望的金额为空

    @property
    def matched(self) -> int:
        return len(self.keys) - len(self.diffs)


def _best_in(st, scheme, rev, metric, ours, schemes, revs, column=None) -> _SheetMatch:
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
    unmatched = False
    if id_col is None or not id_score(id_col):
        strong = metric == '应得' and any(
            any(w in str(h).lower() for w in PRIORITY[0]) and
            any(w in str(h).lower() for w in METRICS['应得']) and
            not any(w in str(h).lower() for w in EXCLUDE)
            for h in st.headers)
        ids=[j for j,h in enumerate(st.headers) if any(w in str(h).lower() for w in ('编号','姓名','对象','_id','name'))]
        if not strong or len(ids)!=1:
            return _SheetMatch()
        id_col,unmatched=ids[0],True
    heads = [str(h or "").lower() for h in st.headers]
    others = [kw for m, kws in METRICS.items() if m != metric for kw in kws if m != "应得"]

    def best(h):         # 列名同时含几个指标的关键字时（如“已发抵扣”含“抵扣”），取匹配最长的那个
        return max((len(kw), m) for m, kws in METRICS.items() if m != "应得" for kw in kws if kw in h)[1]

    def named(j):
        h = heads[j]
        if metric=='汇总核算' and h.strip() in ('金额','值'):
            return True
        return any(kw in h for kw in METRICS[metric]) and not any(x in h for x in EXCLUDE) and \
            (not any(kw in h for kw in others) if metric == "应得" else best(h) == metric)

    if column is not None:
        cand = [j for j in range(len(cols)) if j != id_col and str(st.headers[j]).strip() == column.strip()]
    else:
        cand = [j for j in range(len(cols)) if j != id_col and named(j)]
        if not cand and metric == "应得":     # 认不出列名：只有一列数字才能用
            cand = [j for j in range(len(cols)) if j != id_col and
                    any(_num(cols[j][r]) is not None for r in range(len(cols[j])) if keep[r])]
        for kws in PRIORITY:                   # 按列名优先级收窄，不按数值是否一致来挑
            top = [j for j in cand if any(kw in heads[j] for kw in kws)]
            if top:
                cand = top
                break
    rows = [r for r in range(len(st.rows)) if keep[r] and texts[id_col][r] in ours]
    inspect_rows = [r for r in range(len(st.rows)) if keep[r]] if unmatched else rows
    cand = [j for j in cand if any(_num(cols[j][r]) is not None for r in inspect_rows)]
    if len(cand) > 1:
        return _SheetMatch(ambiguous=[str(st.headers[j]) for j in cand])
    if not cand:
        return _SheetMatch()
    j = cand[0]
    if len(revs)>1 and rev!='R0' and not any(r.upper() in st.sheet.upper() for r in revs) and not any(
            rev.upper() in {t.upper() for t in ts} and
            {t.upper() for t in ts if t} <= {r.upper() for r in revs} for ts in texts):
        return _SheetMatch(ambiguous=['多修订结果需要明确修订列或含修订名的工作表'])
    pairs = [(texts[id_col][r], _num(cols[j][r])) for r in rows]
    blank = sorted({k for k, v in pairs if v is None})
    pairs = [(k, v) for k, v in pairs if v is not None]
    # 期望结果里有金额、本次结果里却没有这个对象的（如漏算了某个人）：只比共有的编号会漏掉，要单独列出
    missing = {}
    for r in range(len(st.rows)):
        k, v = texts[id_col][r], _num(cols[j][r])
        if keep[r] and k and k not in ours and v is not None:
            missing.setdefault(k, v)
    return _SheetMatch(st.headers[j], {k for k, _ in pairs}, [(k, v, ours[k]) for k, v in pairs if abs(v - ours[k]) > TOL],
                       missing=sorted(missing.items()), blank=blank)


def report(matches: list[Match]) -> list[str]:
    """每个方案（修订）一行：全部一致时只列各指标的核对个数，有不一致的列出前 10 个。"""
    lines, groups = [], {}
    for m in matches:
        groups.setdefault((m.scheme, m.revision), []).append(m)
    for (scheme, rev), ms in groups.items():
        label = scheme + ("" if rev == "R0" else f" {rev}")
        found = [m for m in ms if m.total or m.ambiguous]
        if not found:
            lines.append(f"「{label}」：期望结果里没有找到对应的金额")
            continue
        head = "、".join(f"{m.metric} {m.matched}/{m.total}" for m in found if m.total)
        bad = [m for m in found if not m.ok]
        line = f"「{label}」：{head} 个对象" + ("逐分一致" if not bad else "核对")
        if any(m.partial for m in ms):
            line += '（部分抽样，不代表完整验收）'
        for m in bad:
            for sheet, cols in m.ambiguous:
                line += (f"；{m.metric}无法确定用哪一列（「{sheet}」里 {'、'.join(cols)} 都可能是），"
                         "未核对，请用 --expected-column 指定列名")
            if m.missing:
                line += (f"；期望结果里有、本次结果里没有的 {len(m.missing)} 个：" + "、".join(f"{k}（期望 {e:,}）" for k, e in m.missing[:10])
                         + ("……" if len(m.missing) > 10 else ""))
            if m.blank:
                line += f"；期望结果里{m.metric}为空的 {len(m.blank)} 个：" + "、".join(m.blank[:10]) + ("……" if len(m.blank) > 10 else "")
            if m.extra:
                line += f'；实际结果中多出、未验收的 {len(m.extra)} 个：'+'、'.join(m.extra[:10])
            if not m.diffs:
                continue
            line += (f"；{m.metric}不一致（对照{'；'.join(f'「{s}」的「{c}」列' for s, c in m.sources)}）："
                     + "；".join(f"{k} 期望 {e:,} 实际 {a:,}" for k, e, a in m.diffs[:10])
                     + ("……" if len(m.diffs) > 10 else ""))
        lines.append(line)
    srcs = dict.fromkeys(f"「{s}」的「{c}」列" for m in matches for s, c in m.sources)
    if srcs:
        lines.append("对照的列：" + "；".join(srcs))
    return lines
