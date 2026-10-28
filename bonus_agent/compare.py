"""把智能体算出的结果和财务给的期望结果（Excel）逐个对象核对。

期望结果只用于事后核对，不会交给智能体。表格格式不固定，按内容自动识别：
- 编号列：取值和结果里的对象编号（D01、G01、E001……）重合最多的列；
- 方案列 / 修订列（可选）：取值是方案名（QUARTER……）或修订名（R0、R1……）的列；工作表名里含方案名、修订名也算；
- 金额列：每个方案（修订）和每个数值列配对，取逐分一致最多的那一列。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from bonus_core.data.mapping import cell_text
from bonus_core.data.table_detect import detect
from bonus_core.engine.run import Result

TOL = Decimal("0.005")


@dataclass
class Match:
    scheme: str
    revision: str
    sheet: str = ""
    column: str = ""
    matched: int = 0
    total: int = 0
    diffs: list[tuple[str, Decimal, Decimal]] = field(default_factory=list)   # (编号, 期望, 实际)

    @property
    def ok(self) -> bool:
        return self.total > 0 and self.matched == self.total


def _num(v) -> Decimal | None:
    if isinstance(v, bool) or v in (None, ""):
        return None
    try:
        return Decimal(str(v).replace(",", ""))
    except InvalidOperation:
        return None


def compare_expected(results: dict[str, dict[str, Result]], path: str | Path) -> list[Match]:
    tables = detect(path)
    out = []
    for scheme, revs in results.items():
        for rev, res in revs.items():
            ours = {n.key: Decimal(n.final) for n in res.nodes.values() if n.key}
            best = Match(scheme, rev)
            for st in tables:
                best = max(best, _best_in(st, scheme, rev, ours, list(results), list(revs)),
                           key=lambda m: (m.matched, -len(m.diffs)))
            out.append(best)
    return out


def _mentions(name: str, own: str, others: list[str]) -> bool:
    """工作表名提到了别的方案（修订）而没提到自己的，说明不是这一张。"""
    up = name.upper()
    return own.upper() not in up and any(o.upper() in up for o in others if o != own)


def _best_in(st, scheme: str, rev: str, ours: dict[str, Decimal], schemes: list[str], revs: list[str]) -> Match:
    if _mentions(st.sheet, scheme, schemes) or _mentions(st.sheet, rev, revs):
        return Match(scheme, rev)
    cols = list(zip(*[vals for _, vals in st.rows])) if st.rows else []
    texts = [[cell_text(v) for v in c] for c in cols]
    id_col = max(range(len(cols)), key=lambda i: sum(t in ours for t in texts[i]), default=None)
    if id_col is None or not any(t in ours for t in texts[id_col]):
        return Match(scheme, rev)
    keep = [True] * len(st.rows)
    for ts in texts:     # 方案列、修订列：取值里有本方案（修订）名的列，只留这些行
        vals = {t.upper() for t in ts if t}
        for own, names in ((scheme, schemes), (rev, revs)):
            if len(names) > 1 and own.upper() in vals and vals <= {n.upper() for n in names} | {"R0"}:
                keep = [k and t.upper() == own.upper() for k, t in zip(keep, ts)]
    best = Match(scheme, rev)
    for j, c in enumerate(cols):
        if j == id_col:
            continue
        pairs = [(texts[id_col][r], _num(c[r])) for r in range(len(c)) if keep[r] and texts[id_col][r] in ours]
        pairs = [(k, v) for k, v in pairs if v is not None]
        if not pairs:
            continue
        diffs = [(k, v, ours[k]) for k, v in pairs if abs(v - ours[k]) > TOL]
        m = Match(scheme, rev, st.sheet, st.headers[j], len(pairs) - len(diffs), len(pairs), diffs)
        if (m.matched, -len(m.diffs)) > (best.matched, -len(best.diffs)):
            best = m
    return best


def report(matches: list[Match]) -> list[str]:
    lines = []
    for m in matches:
        label = m.scheme + ("" if m.revision == "R0" else f" {m.revision}")
        if not m.total:
            lines.append(f"「{label}」：期望结果里没有找到对应的金额")
            continue
        lines.append(f"「{label}」：对照「{m.sheet}」的「{m.column}」列，{m.matched}/{m.total} 个对象逐分一致"
                     + ("" if m.ok else "，不一致的：" + "；".join(f"{k} 期望 {e:,} 实际 {a:,}" for k, e, a in m.diffs[:10])
                        + ("……" if len(m.diffs) > 10 else "")))
    return lines
