"""独立复读：同一个模型另起一次调用，只看材料（规则文档、财务答复、参数表）和数据表的表头，独立列出每个计算项；
程序把它和智能体写的规则逐项比对，对不上的地方交还智能体逐条处理（改规则，或写明材料依据说明复读错了）。

为什么要有这一步：写规则和写验算出自同一个理解，理解错了（漏减费用、分档点单位写错）验算也跟着错。
复读不看规则、不看对话，是另一份独立的理解；两份理解不一致的地方，正是最可能错的地方。

输入边界由程序控制，不靠提示词：
- 给：规则文档原文（含图片转写）、财务的答复和对话里的说明、不是数据表的小工作表（参数表、字段说明）、每张数据表的表头和列类型；
- 不给：数据行（姓名、金额、名单）、智能体写的规则和对话。所以就算以后改用外部模型复读，也不涉及员工数据。

比对完全由程序做（不让模型当裁判），只比能机械判断的几类：用到的列、常数（允许百分数、万元换算）、分档点、
筛选取值、日期范围。复读本身对不对不重要：它只用来把可疑的地方挑出来。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from bonus_agent.extract_rules import prompt
from bonus_agent.llm import LLMError, parse_json

MARK = "【独立复读】"            # 系统提示的开头；测试用的 FakeLLM 靠它认出复读调用
PARAM_ROWS = 80                  # 没定义成数据表、不超过这么多行的工作表当作参数表，整表给复读
DOC_CHARS = 60000                # 单个文档最多给这么多字
DATA_JSON_CHARS = 20000          # 超过这么长的 JSON 数组文件当作数据，不给（财务答复这类短 JSON 照给）
SCALES = (1, 100, Decimal("0.01"), 10000, Decimal("0.0001"))     # 百分数、万元换算视为同一个数
YES_NO = {"是", "否", "y", "n", "yes", "no", "true", "false", "1", "0", "有", "无"}
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NUM = re.compile(r"-?\d+(?:\.\d+)?%?")


def _col_type(values) -> str:
    vals = [v for v in values if v not in (None, "")]
    if not vals:
        return "空"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
        return "数字"
    if all(hasattr(v, "year") or (isinstance(v, str) and _DATE.fullmatch(v.strip()[:10] or "")) for v in vals):
        return "日期"
    return "文本"


def _data_sheets(ws) -> set[tuple[str | None, str]]:
    return {(m.file, m.sheet) for m in ws.mapping.values()}


def _is_data(st, data: set) -> bool:
    return (st.file, st.sheet) in data or (None, st.sheet) in data


def material(ws) -> str:
    """交给复读的材料：规则文档、财务答复、参数表整表、数据表只给表头和列类型。"""
    from bonus_agent.agent import CHAT_SOURCE, _json_data_summary
    parts = []
    for d in ws.inv.docs:
        text = ws.read_document(d.name, 0, limit=10 ** 9)
        if len(text) > DATA_JSON_CHARS and _json_data_summary(ws._file(d.name)):    # 大的结构化数据文件（数据行），不给
            continue
        if text.strip():
            parts.append(f"## 文件 {d.name}\n{text[:DOC_CHARS]}")
    answered = [q for q in ws.questions if q.answer not in (None, "") and not getattr(q, "withdrawn", "")]
    if answered:
        parts.append("## 财务答复\n" + "\n".join(f"问：{q.text}\n答：{q.answer}" for q in answered))
    if getattr(ws, "said", ""):
        parts.append(f"## {CHAT_SOURCE}\n{ws.said}")
    data = _data_sheets(ws)
    heads = []
    for st in ws.inv.sheets:
        ref = f"{st.file}/{st.sheet}"
        if not _is_data(st, data) and len(st.rows) <= PARAM_ROWS and ref in ws.inv.sheet_text:
            parts.append(f"## 工作表 {ref}（整表）\n{ws.inv.sheet_text[ref]}")
            continue
        cols = list(zip(*[vals for _, vals in st.rows])) if st.rows else [()] * len(st.headers)
        heads.append(f"- {st.sheet}（{st.file}，{len(st.rows)} 行）：" + "、".join(
            f"{h}（{_col_type(c)}）" for h, c in zip(st.headers, cols) if h))
    if heads:
        parts.append("## 数据表（只有表头和列类型，没有数据行）\n" + "\n".join(heads))
    return "\n\n".join(parts)


def signature(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def run(ws) -> dict:
    """调用模型复读；材料没变时用上次的结果。返回 {"sig", "items", "unclear", "error"}。"""
    text = material(ws)
    sig = signature(text)
    old = getattr(ws, "reread", None) or {}
    if old.get("sig") == sig and not old.get("error"):
        return old
    msgs = [{"role": "system", "content": prompt("reread")}, {"role": "user", "content": text}]
    out = {"sig": sig, "items": [], "unclear": [], "error": ""}
    for attempt in range(2):
        try:
            raw = ws.llm.chat(msgs, json_out=True, temperature=0, max_tokens=8192)
            data = parse_json(raw)
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise LLMError("没有 items 列表")
            out["items"] = [i for i in items if isinstance(i, dict)]
            out["unclear"] = [str(u) for u in (data.get("unclear") or []) if u] if isinstance(data, dict) else []
            out["error"] = ""
            break
        except (LLMError, ValueError, AttributeError) as e:
            out["error"] = str(e)[:300]
            if attempt == 0:
                msgs = msgs + [{"role": "user", "content": "上次的输出不是合法的 JSON，请只输出 JSON 对象。"}]
    ws.reread = out
    return out


# ---- 程序比对 ----

def _dec(v) -> Decimal | None:
    if isinstance(v, bool) or v is None:
        return None
    s = str(v).strip().replace(",", "")
    pct = s.endswith("%")
    try:
        d = Decimal(s.rstrip("%"))
    except InvalidOperation:
        return None
    return d / 100 if pct else d


def _walk(x):
    if isinstance(x, dict):
        for k, v in x.items():
            yield k
            yield from _walk(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk(v)
    else:
        yield x


class Rules:
    """智能体规则里出现的数、字符串、日期和用到的列（按“工作表/表头”）。"""

    def __init__(self, ws):
        self.nums: set[Decimal] = set()
        self.strs: set[str] = set()
        self.dates: set[date] = set()
        text_ids: set[str] = set()
        for x in _walk(ws.drafts):
            if isinstance(x, (int, float)) and not isinstance(x, bool):
                self.nums.add(Decimal(str(x)))
            elif isinstance(x, str):
                for m in _NUM.findall(x):
                    d = _dec(m)
                    if d is not None:
                        self.nums.add(d)
                for a, b in re.findall(r'"([^"]*)"|\'([^\']*)\'', x):
                    self.strs.add((a or b).strip())
                self.strs.add(x.strip())
                for m in _DATE.findall(x):
                    try:
                        self.dates.add(date.fromisoformat(m))
                    except ValueError:
                        pass
                text_ids |= set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", x))
        self.used: set[tuple[str, str]] = set()
        data = _data_sheets(ws)
        for ds, m in ws.mapping.items():
            for v in m.filter.values():
                for s in (v if isinstance(v, list) else [v]):
                    self.strs.add(str(s).strip())
                    for d in _DATE.findall(str(s)):
                        self.dates.add(date.fromisoformat(d))
            for f, col in m.columns.items():
                if f in text_ids:
                    self.used.add((m.sheet or "", col))
            for col in m.filter:
                self.used.add((m.sheet or "", col))
            dd = ws.datasets.get(ds)
            for k in ([dd.key, dd.link] if dd else []):       # 主键、关联键：建表时就用上了
                if k:
                    self.used.add((m.sheet or "", m.columns.get(k, k)))
        # 规则读取的小数据表（如按表查的系数）里的数，也算规则里有
        for st in ws.inv.sheets:
            if _is_data(st, data) and len(st.rows) <= PARAM_ROWS:
                for _, vals in st.rows:
                    for v in vals:
                        d = _dec(v) if isinstance(v, (int, float)) else None
                        if d is not None:
                            self.nums.add(d)
        self.data_cols: dict[tuple[str, str], str] = {}        # (工作表, 表头) -> 列类型
        for st in ws.inv.sheets:
            if _is_data(st, data):
                cols = list(zip(*[vals for _, vals in st.rows])) if st.rows else [()] * len(st.headers)
                for h, c in zip(st.headers, cols):
                    if h:
                        self.data_cols[(st.sheet, h)] = _col_type(c)
        self.tiers = {k: v for d in ws.drafts.values() for k, v in ((d or {}).get("tables") or {}).items()
                      if (v or {}).get("kind") == "tier"}

    def has_num(self, v: Decimal) -> bool:
        return any(abs(v * k - n) <= Decimal("1e-9") * max(1, abs(n)) for n in self.nums for k in SCALES)

    def has_date(self, d: date) -> bool:
        return any(abs((d - x).days) <= 1 for x in self.dates)


def _near(x: Decimal, pool: list[Decimal], step: bool = False) -> bool:
    """x 和 pool 里某个数相同（允许百分数、万元换算；step 时整数分界点允许差 1）。"""
    for y in pool:
        if any(abs(x * k - y) <= Decimal("1e-9") * max(1, abs(y)) for k in SCALES):
            return True
        if step and x == x.to_integral() and y == y.to_integral() and abs(x - y) == 1:
            return True
    return False


def _col(ref: str) -> tuple[str, str]:
    parts = [p.strip() for p in str(ref).replace("／", "/").split("/") if p.strip()]
    return (parts[-2] if len(parts) > 1 else "", parts[-1] if parts else "")


def compare(ws, rr: dict) -> list[tuple[str, str]]:
    """复读和规则对不上的地方：[(编号, 说明)]。编号写进 finish 的 ignored 即表示核对过、规则是对的。"""
    if not rr or rr.get("error") or not rr.get("items"):
        return []
    rules = Rules(ws)
    out: dict[str, str] = {}
    for it in rr["items"]:
        name = str(it.get("name") or "未命名")[:40]
        how = str(it.get("how") or "")[:120]
        tag = f"复读「{name}」" + (f"（{how}）" if how else "")
        # 参与计算的列只比数字、日期列（编号、名称这类文字列复读常顺手写上，比了只是误报）；筛选用的列都比
        cols = [(c, False) for c in map(_col, it.get("columns") or [])]
        cols += [(_col(f.get("column", "")), True) for f in it.get("filters") or [] if isinstance(f, dict)]
        for (sheet, head), is_filter in cols:
            kind = rules.data_cols.get((sheet, head))
            if kind and (is_filter or kind in ("数字", "日期")) and (sheet, head) not in rules.used:
                out.setdefault(f"复读 列 {sheet}/{head}", f"{tag}{'按' if is_filter else '用到了'}「{sheet}」的「{head}」列"
                               f"{'筛选' if is_filter else ''}，规则没用到这一列")
        for c in it.get("constants") or []:
            v = _dec(c.get("value") if isinstance(c, dict) else c)
            if v is None or v in (0, 1) or (v == v.to_integral() and 1900 <= v <= 2100):
                continue
            if not rules.has_num(v):
                what = f"（{c.get('meaning') or ''}{'，单位 ' + str(c['unit']) if c.get('unit') else ''}）" \
                    if isinstance(c, dict) else ""
                out.setdefault(f"复读 常数 {_fmt(v)}", f"{tag}有常数 {_fmt(v)}{what}，规则里没有这个数（也不是它的百分数、万元换算）")
        for f in it.get("filters") or []:
            if not isinstance(f, dict):
                continue
            for s in f.get("values") or []:
                s = str(s).strip()
                if s and s.lower() not in YES_NO and _dec(s) is None and s not in rules.strs:
                    out.setdefault(f"复读 取值 {s}", f"{tag}按「{_col(f.get('column', ''))[1]}」筛选取值「{s}」，规则里没有这个取值")
            for k in ("from", "to"):
                try:
                    d = date.fromisoformat(str(f.get(k) or "")[:10])
                except ValueError:
                    continue
                if not rules.has_date(d):
                    out.setdefault(f"复读 日期 {d}", f"{tag}的日期范围{'从' if k == 'from' else '到'} {d}，规则里没有这个日期（前后一天也没有）")
        for t in it.get("tiers") or []:
            if not isinstance(t, dict):
                continue
            rates = [x for x in (_dec(r) for r in t.get("rates") or []) if x is not None]
            cuts = [x for x in (_dec(r) for r in t.get("cutoffs") or []) if x is not None and x != 0]
            if not rates or not cuts:
                continue
            mine = [r for r in rates if r != 0]
            cand = {}
            for tname, spec in rules.tiers.items():           # 各档比例对得上，才是同一张分档表
                theirs = [r for r in (_dec(r) for _, r in spec.get("steps") or []) if r not in (None, 0)]
                if len(mine) >= 2 and all(_near(r, theirs) for r in mine):
                    cand[tname] = len(theirs) == len(mine)
            exact = [n for n, same in cand.items() if same]
            pick = exact or (list(cand) if len(cand) == 1 else [])      # 对得上好几张又都不完全一样：认不准是哪张，不比
            for tname in pick:
                steps = rules.tiers[tname].get("steps") or []
                lows = sorted(x for x in (_dec(lo) for lo, _ in steps) if x is not None)
                # 只比档与档之间的分界点：第一档的起点有人写 0、有人写 1；按单数分档时“超过 5”和“第 6 单起”是同一个分界
                inner = sorted(cuts)[1:] if len(cuts) >= len(rates) else sorted(cuts)
                if not all(_near(c, lows[1:], step=True) for c in inner):
                    out.setdefault(f"复读 分档 {tname}",
                                   f"{tag}的分档（传入 {t.get('input') or '?'}）分档点是 {'、'.join(map(_fmt, cuts))}，"
                                   f"规则的分档表 {tname} 是 {'、'.join(map(_fmt, lows))}：各档比例对得上、分档点不同，"
                                   "核对传入的数和分档点是不是同一个单位")
    return list(out.items())


def _fmt(d: Decimal) -> str:
    return format(d.normalize(), "f")
