"""口径单：把智能体写的规则逐条翻译成财务看得懂的话，财务逐条确认后才能确认结果、冻结模板。

翻译完全由程序按规则结构机械完成，不调用大模型，所以口径单和实际计算一字不差：
字段换成 Excel 表头，变量和参数表换成中文说明，筛选条件写成句子，并附上一个对象的代入算式。
每行带一个按内容算出的 key：规则改动后，内容变了的行要重新确认，没变的行保留原来的确认。

模型写规则时理解错了（如净利润漏减费用、分档点单位写错），自我验算也会跟着错；
这类错误只能靠懂业务的人看口径发现，口径单就是把这一步变得具体、可逐条落实。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from bonus_core.dsl.parser import BinOp, BoolOp, Call, Cmp, Const, ExprError, Name, Neg, Not, Num, Str, parse, walk

FILE = "口径单.json"
MARKS = "口径确认.json"
OK, WRONG = "确认", "有误"
SHARED = "共用"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PREC = {"or": 1, "and": 2, "not": 3, "cmp": 4, "+": 5, "-": 5, "*": 6, "/": 6, "neg": 7, "atom": 8}
_OPS = {"*": "×", "/": "÷", "+": "+", "-": "−"}
_CMP = {"==": "等于", "!=": "不等于", ">=": "≥", "<=": "≤", ">": ">", "<": "<"}


def _num(v) -> str:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return str(v)
    s = format(d.normalize(), "f") if d == d.to_integral() or abs(d) < 1 else format(d.normalize(), ",f")
    return s


def short(desc: str, fallback: str, limit: int = 24) -> str:
    """变量、参数表的中文名：取说明里括号、冒号之前的部分；太长或没有说明时用原名。"""
    t = re.split(r"[（(：:，,；;。]", (desc or "").strip(), maxsplit=1)[0].strip()
    return t if t and len(t) <= limit else fallback


class Names:
    """规则里的名字 -> 财务看得懂的叫法。"""

    def __init__(self, columns: dict[str, str], variables: dict[str, str], tables: dict[str, str]):
        self.columns, self.variables, self.tables = columns, variables, tables

    def name(self, n: str) -> str:
        if n in self.variables:
            return f"〔{self.variables[n]}〕"
        if n in self.columns:
            return f"「{self.columns[n]}」"
        if n in self.tables:
            return f"《{self.tables[n]}》"
        return n


def plain(expr: str, nm: Names) -> str:
    """表达式翻译成中文；解析不了时原样返回。"""
    try:
        ast = parse(expr)
    except ExprError:
        return expr
    return _render(ast, nm)[0]


def _render(n, nm: Names) -> tuple[str, int]:
    """(文字, 优先级)"""
    if isinstance(n, Num):
        return _num(n.value), _PREC["atom"]
    if isinstance(n, Str):
        return (n.value if _DATE.match(n.value) else f"“{n.value}”"), _PREC["atom"]
    if isinstance(n, Const):
        return ("是" if n.value else "否"), _PREC["atom"]
    if isinstance(n, Name):
        return nm.name(n.id), _PREC["atom"]
    if isinstance(n, Neg):
        t, p = _render(n.operand, nm)
        return f"−{_wrap(t, p, _PREC['neg'])}", _PREC["neg"]
    if isinstance(n, Not):
        t, _ = _render(n.operand, nm)
        return f"不满足（{t}）", _PREC["not"]
    if isinstance(n, BoolOp):
        p = _PREC[n.op]
        a, pa = _render(n.left, nm)
        b, pb = _render(n.right, nm)
        return f"{_wrap(a, pa, p)} {'且' if n.op == 'and' else '或'} {_wrap(b, pb, p)}", p
    if isinstance(n, Cmp):
        p = _PREC["cmp"]
        a, pa = _render(n.left, nm)
        b, pb = _render(n.right, nm)
        op = _CMP[n.op]
        if n.op in ("==", "!=") and isinstance(n.right, (Str, Const)):
            op = "为" if n.op == "==" else "不为"
        return f"{_wrap(a, pa, p + 1)} {op} {_wrap(b, pb, p + 1)}", p
    if isinstance(n, BinOp):
        p = _PREC[n.op]
        a, pa = _render(n.left, nm)
        b, pb = _render(n.right, nm)
        right_min = p + 1 if n.op in "-/" else p
        return f"{_wrap(a, pa, p)} {_OPS[n.op]} {_wrap(b, pb, right_min)}", p
    if isinstance(n, Call):
        return _call(n, nm), _PREC["atom"]
    return str(n), _PREC["atom"]


def _wrap(t: str, p: int, need: int) -> str:
    return f"({t})" if p < need else t


def _call(c: Call, nm: Names) -> str:
    # 参数本身是算式时加括号，免得和外面的话连在一起读错；参数之间已有“，；”分隔的（if、round、取较大等）不加
    raw = [_render(x, nm) for x in c.args]
    a = [t if p >= _PREC["atom"] else f"（{t}）" for t, p in raw]
    r = [t for t, _ in raw]
    kw = c.kw()
    fn = c.fn
    if fn in ("sum", "avg", "count", "count_distinct") or (fn in ("min", "max") and len(c.args) == 1):
        src = kw.get("of")
        table = f"「{src.value}」" if isinstance(src, Str) else "明细"
        cond = f"中满足〔{_render(kw['where'], nm)[0]}〕的行" if "where" in kw else "各行"
        what = {"sum": f"{a[0] if a else ''} 合计", "avg": f"{a[0] if a else ''} 平均值", "count": "行数",
                "count_distinct": f"不同 {a[0] if a else ''} 的个数", "min": f"{a[0] if a else ''} 最小值",
                "max": f"{a[0] if a else ''} 最大值"}[fn]
        return f"{table}{cond}的{what}" if "where" in kw else f"{table}{what}"
    if fn in ("min", "max"):
        return f"取{'较小' if fn == 'min' else '较大'}（{'；'.join(r)}）"
    if fn == "if":
        return f"（如果 {r[0]}，则 {r[1]}，否则 {r[2]}）"
    if fn == "tier":
        return f"{a[1]} 按分档表{a[0]}计算的金额"
    if fn == "tier_rate":
        return f"{a[1]} 在分档表{a[0]}中所在档的系数"
    if fn == "lookup":
        return f"按{a[0]}查 {a[1]} 对应的值"
    if fn == "ratio":
        z = f"（分母为 0 时取 {_render(kw['zero'], nm)[0]}）" if "zero" in kw else ""
        return f"({r[0]}) ÷ ({r[1]}){z}"
    if fn == "round":
        return f"四舍五入（{r[0]}，保留 {r[1] if len(r) > 1 else '2'} 位小数）"
    if fn == "between":
        return f"{a[0]} 在 {a[1]} 至 {a[2]} 之间（含两端）"
    if fn == "years_between":
        return f"从 {a[0]} 到 {a[1]} 已满整年数"
    if fn == "days_between":
        return f"从 {a[0]} 到 {a[1]} 相差天数"
    if fn == "share":
        return f"{a[0]} 占同级合计的比例"
    if fn == "minmax":
        return f"{a[0]} 在同级中的相对位置（最小为 0、最大为 1）"
    if fn == "clamp":
        return f"限定在 {a[1]} 至 {a[2]} 之间（{a[0]}）"
    if fn == "abs":
        return f"{a[0]} 的绝对值"
    if fn == "date":
        return a[0].strip("“”")
    if fn in ("year", "month", "quarter"):
        return f"{a[0]} 的{ {'year': '年份', 'month': '月份', 'quarter': '季度'}[fn] }"
    if fn == "scheme_paid":
        return f"方案 {a[0]}（{a[1] if len(a) > 1 else 'R0'}）给本人的本次实发"
    return f"{fn}({', '.join(a)})"


def tier_text(t: dict) -> str:
    steps = [(Decimal(str(lo)), Decimal(str(r))) for lo, r in t.get("steps") or []]
    inc = t.get("lower_inclusive", True)
    parts = []
    if steps and steps[0][0] > 0:
        parts.append(f"{'不到' if inc else '不超过'} {_num(steps[0][0])}：0")
    for i, (lo, r) in enumerate(steps):
        hi = steps[i + 1][0] if i + 1 < len(steps) else None
        if hi is None:
            rng = f"{'达到' if inc else '超过'} {_num(lo)}"
        else:
            rng = f"{_num(lo)}（{'含' if inc else '不含'}）至 {_num(hi)}（{'不含' if inc else '含'}）"
        parts.append(f"{rng}：{_num(r)}")
    how = "超额累进：各段分别按本段比例计算后相加" if t.get("mode") == "excess" else "全额：整个数按所在档计算"
    return f"{how}。" + "；".join(parts)


def lookup_text(t: dict) -> str:
    items = "；".join(f"{k} → {_num(v)}" for k, v in (t.get("mapping") or {}).items())
    d = t.get("default")
    return items + (f"；其他取值 → {_num(d)}" if d is not None else "；表里没有的取值会报错停下")


def _key(*parts) -> str:
    return hashlib.sha1("␟".join(str(p) for p in parts).encode()).hexdigest()[:16]


def _example(res, item: str) -> str:
    """该规则项在引擎里数值绝对值最大的一个对象的代入算式。"""
    best, best_v = None, None
    for tr in (res.traces.values() if res else []):
        if tr.rule_item != item:
            continue
        try:
            v = abs(Decimal(str(tr.value)))
        except (InvalidOperation, ValueError):
            continue
        if best is None or v > best_v:
            best, best_v = tr, v
    if best is None:
        return ""
    node = best.id.rsplit("/", 1)[0] if best.id.endswith(f"/{item}") else best.id.split("/", 1)[0]
    text = f"{node}：{best.rendered} = {_num(best.value)}"
    return text if len(text) <= 400 else text[:397] + "……"


def _sources(clauses, refs: list[str]) -> str:
    out = []
    for c in clauses:
        w = c.where or ""
        if any(re.search(rf"(?<![A-Za-z0-9_]){re.escape(r)}(?![A-Za-z0-9_])", w) for r in refs):
            out.append(f"{c.quote}（{c.file}）")
    return "\n".join(dict.fromkeys(out))


def _flags(texts: list[str], needles: list[str]) -> str:
    return "\n".join(t for t in texts if any(n in t for n in needles))


def build(ws, results: dict | None = None) -> list[dict]:
    """口径单的全部行。results：方案 -> 修订 -> Result，取代入示例和分档表的实际取值。"""
    results = results or getattr(ws, "results", {}) or {}
    columns: dict[str, str] = {}
    for ds, m in ws.mapping.items():
        for f, col in m.columns.items():
            columns.setdefault(f, col)
    variables, tables = {}, {}
    for d in ws.drafts.values():
        for k, v in (d.get("variables") or {}).items():
            variables.setdefault(k, short((v or {}).get("desc", ""), k))
        for k, v in (d.get("tables") or {}).items():
            tables.setdefault(k, short((v or {}).get("desc", ""), k))
    nm = Names(columns, variables, tables)
    lint_texts = {s: list(r.gates) + list(r.warns) for s, r in (getattr(ws, "lint", {}) or {}).items()}
    all_lint = [t for ts in lint_texts.values() for t in ts]
    rows: list[dict] = []

    def add(scheme, section, item, reading, refs, rule_item="", res=None, flag="", extra=""):
        rows.append({"scheme": scheme, "section": section, "item": item, "reading": reading + (f"\n{extra}" if extra else ""),
                     "source": _sources(ws.clauses, refs), "example": _example(res, rule_item) if rule_item else "",
                     "flag": flag, "key": _key(scheme, section, item, reading)})

    for scheme, d in ws.drafts.items():
        own = [s for s in results if s != SHARED]
        res_name = scheme if scheme in results else (own[0] if own else None)
        res = results.get(res_name, {}).get("R0") if res_name else None
        texts = all_lint if scheme == SHARED else lint_texts.get(scheme, [])
        for t, spec in (d.get("tables") or {}).items():
            spec = spec or {}
            body = tier_text(spec) if spec.get("kind") == "tier" else lookup_text(spec)
            stats = ""
            for r in ([x.get("R0") for x in results.values()] if scheme == SHARED else [res]):
                st = (getattr(r, "tier_stats", {}) or {}).get(t) if r else None
                if st:
                    bands = "、".join(f"第 {b + 1} 档 {n} 次" if b >= 0 else f"低于第一档 {n} 次"
                                     for b, n in sorted(st["bands"].items()))
                    stats = f"实际传入 {st['n']} 个值，范围 {_num(st['min'])}～{_num(st['max'])}；{bands}"
                    break
            add(scheme, "参数表", f"{tables[t]}（{t}）", body, [t, f"tables.{t}"], flag=_flags(texts, [f"分档表 {t} ", f"{t}（"]),
                extra=stats)
        for v, spec in (d.get("variables") or {}).items():
            spec = spec or {}
            add(scheme, "计算项", f"{variables[v]}（{v}）", plain(spec.get("expr", ""), nm), [v, f"variables.{v}"],
                rule_item=v, res=res, flag=_flags(texts, [f"变量 {v} ", f"变量 {v}、", f"、{v} "]),
                extra=f"计算对象：{spec.get('scope')}" if spec.get("scope") else "")
        pool = d.get("pool") or {}
        if pool.get("expr"):
            add(scheme, "奖金池", f"奖金池（按{pool.get('scope') or '全部'}计算）", plain(pool["expr"], nm), ["pool"],
                rule_item="pool", res=res)
        for lv in d.get("allocation") or []:
            w = str(lv.get("weight") or "")
            add(scheme, "分配", f"{lv.get('level')}层分配",
                (f"按 {plain(w, nm)} 的比例分配" if w and w != "1" else "不按权重分（只有一个对象或直接计算）")
                + (f"；上一层编号 {nm.name(lv['parent_key'])}" if lv.get("parent_key") else ""),
                ["allocation", f"allocation.{lv.get('level')}"], rule_item=f"allocation.{lv.get('level')}", res=res)
        for p in d.get("payout") or []:
            if p.get("pay_ratio"):
                body = f"可发比例 = {plain(p['pay_ratio'], nm)}；可发上限 = 应得 × 可发比例，其余暂缓"
            else:
                body = f"暂缓比例 = {plain(p.get('hold_ratio', ''), nm)}；暂缓 = 应得 × 暂缓比例"
            add(scheme, "发放", f"发放条件 {p.get('id')}（按{p.get('scope') or '最末层'}）", body,
                ["payout", f"payout.{p.get('id')}"], rule_item=f"payout.{p.get('id')}", res=res)
        for o in d.get("offsets") or []:
            add(scheme, "已发抵扣", f"已发抵扣 {o.get('id')}（按{o.get('scope')}）",
                f"已发金额 = {plain(o.get('expr', ''), nm)}；本次实发 = max(可发上限 − 已发, 0)，超出部分登记为超发待核对、不追回",
                ["offsets", f"offsets.{o.get('id')}"], rule_item=f"offsets.{o.get('id')}", res=res)
        for r in d.get("revisions") or []:
            adj = "；".join(f"{a.get('node')} 指定 {_num(a.get('amount'))} 元" + (f"（{a['reason']}）" if a.get("reason") else "")
                           for a in r.get("adjust") or [])
            add(scheme, "人工调整", f"修订 {r.get('name')}", adj + "（被指定对象之外的金额在同级里按权重重新分配）",
                ["revisions", f"revisions.{r.get('name')}"])
        for p in d.get("patches") or []:
            add(scheme, "数据修订", f"数据修订 {p.get('kind')}", json.dumps({k: v for k, v in p.items() if k != "kind"},
                                                                      ensure_ascii=False), ["patches"])
        for c in d.get("checks") or []:
            add(scheme, "数据校验", f"校验 {c.get('id')}", f"{plain(c.get('expr', ''), nm)}；不满足时：{c.get('message', '')}",
                ["checks", f"checks.{c.get('id')}"])

    # 数据范围：每张表用到了哪些列，哪些金额列没用到
    used: set[str] = set()
    for d in ws.drafts.values():
        for s in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", json.dumps(d, ensure_ascii=False)):
            used.add(s)
    for ds, dd in ws.datasets.items():
        m = ws.mapping.get(ds)
        if m is None:
            continue
        cols = [(f, m.columns.get(f, f)) for f in dd.fields]
        on = [c for f, c in cols if f in used]
        idle_money = [c for f, c in cols if f not in used and dd.fields[f].type == "money"]
        body = f"来自 {m.file} / {m.sheet}" + (f"，筛选 {m.filter}" if m.filter else "") + f"。用到的列：{'、'.join(on) or '无'}"
        if idle_money:
            body += f"。没用到的金额列：{'、'.join(idle_money)}"
        add("数据", "数据范围", ds, body, [ds, f"datasets.{ds}"], flag=_flags(all_lint, [f"「{ds}」"]))
    # 待定事项：智能体决定不处理的地方、财务的答复
    for i in getattr(ws, "ignored", []) or []:
        ref = str(i.get("ref", ""))
        add("待定事项", "智能体说明不处理", ref, str(i.get("reason", "")), [ref],
            flag="独立复读和规则在这里不一致，智能体认为规则是对的，请核对材料原文" if ref.startswith("复读 ")
            else "智能体决定不处理这一项，请确认理由成立")
    rr = getattr(ws, "reread", None) or {}
    if rr.get("error"):
        add("待定事项", "独立复读", "独立复读没有完成", f"复读调用失败：{rr['error']}。这一轮没有第二份独立理解做对照，请逐行仔细核对",
            [], flag="独立复读没有完成")
    elif rr.get("unclear"):
        add("待定事项", "独立复读", "复读认为材料没写清的地方", "\n".join(f"- {u}" for u in rr["unclear"]), [])
    for q in getattr(ws, "questions", []) or []:
        if q.answer not in (None, "") and not getattr(q, "withdrawn", ""):
            by_doc = str(getattr(q, "answered_by", "") or "").startswith("材料")
            add("待定事项", "财务答复", f"问题 {q.id}", f"{q.text}\n答复：{q.answer}", [q.id],
                flag="智能体按材料原文关闭了这个问题，请确认材料确实答复了它" if by_doc else "")
    seen: set[str] = set()
    out = []
    for r in rows:                    # 同名同内容的行只留一次
        if r["key"] in seen:
            continue
        seen.add(r["key"])
        r["id"] = f"K{len(out) + 1:03d}"
        out.append(r)
    return out


def write(ws, out: Path, results: dict | None = None) -> Path:
    p = Path(out) / FILE
    p.write_text(json.dumps(build(ws, results), ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def load(out: Path) -> list[dict]:
    p = Path(out) / FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def load_marks(out: Path) -> dict[str, dict]:
    p = Path(out) / MARKS
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_marks(out: Path, marks: dict[str, dict]) -> None:
    (Path(out) / MARKS).write_text(json.dumps(marks, ensure_ascii=False, indent=1), encoding="utf-8")


def mark(out: Path, decisions: dict[str, tuple[str, str]], by: str) -> list[str]:
    """decisions：行 key -> (确认 / 有误, 说明)。有误必须写说明。返回出错提示。"""
    rows = {r["key"]: r for r in load(out)}
    marks, errs = load_marks(out), []
    at = datetime.now().isoformat(timespec="seconds")
    for k, (st, note) in decisions.items():
        if k not in rows or st not in (OK, WRONG):
            continue
        if st == WRONG and not note.strip():
            errs.append(f"{rows[k]['id']} 标了有误，请写明哪里不对")
            continue
        marks[k] = {"status": st, "note": note.strip()[:1000], "by": by, "at": at, "item": rows[k]["item"]}
    save_marks(out, marks)
    return errs


def status(out: Path) -> dict:
    """当前口径单的确认进度：total、confirmed、wrong（有误的行）、pending（还没确认的行）。"""
    rows, marks = load(out), load_marks(out)
    wrong = [r for r in rows if marks.get(r["key"], {}).get("status") == WRONG]
    pending = [r for r in rows if marks.get(r["key"], {}).get("status") != OK]
    return {"total": len(rows), "confirmed": len(rows) - len(pending), "wrong": wrong, "pending": pending}


def instruction(out: Path) -> str:
    """有误的行汇成给智能体的修改说明（只含还没交给智能体的）。"""
    rows, marks = {r["key"]: r for r in load(out)}, load_marks(out)
    lines = []
    for k, mk in marks.items():
        if mk.get("status") == WRONG and not mk.get("sent") and k in rows:
            r = rows[k]
            lines.append(f"- {r['scheme']} / {r['item']}：财务认为有误：{mk['note']}（现在的口径：{r['reading'][:300]}）")
    if not lines:
        return ""
    return "财务在口径单上标了有误的地方，逐条改正（改不了或理解不了的用 ask_question 问清楚）：\n" + "\n".join(lines)


def mark_sent(out: Path) -> None:
    marks = load_marks(out)
    for mk in marks.values():
        if mk.get("status") == WRONG:
            mk["sent"] = True
    save_marks(out, marks)


def sheet(out: Path | None = None, rows: list[dict] | None = None):
    """结果 Excel 里的「口径单」工作表：(标题, 表头, 数据, 列宽)。"""
    rows = rows if rows is not None else load(out)
    data = [[r["id"], r["scheme"], r["section"], r["item"], r["reading"], r["source"], r["example"], r["flag"], ""]
            for r in rows]
    return ("口径单", ["序号", "方案", "类别", "计算项", "程序的理解（按规则翻译）", "材料原文", "代入示例", "需要重点看", "财务意见"],
            data, {"计算项": 28, "程序的理解（按规则翻译）": 70, "材料原文": 50, "代入示例": 50, "需要重点看": 40})
