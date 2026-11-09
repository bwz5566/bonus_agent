"""按场景的输入模板检查本期材料：表在不在、列齐不齐、数字列是不是数字。在计算（和智能体读材料）之前就把格式问题报出来。"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from bonus_agent.scenarios import Column, InputTable, Scenario

EXCEL = {".xlsx", ".xlsm"}
_PUNCT = re.compile(r"[\s_\-—–·:：/\\()（）\[\]【】{}<>《》'\"“”‘’,，.。、]+")
_DATE = re.compile(r"^\d{4}\s*[-/.年]\s*\d{1,2}(\s*[-/.月]\s*(\d{1,2}\s*日?)?)?\s*月?$")
EXAMPLES = 5


def norm(s) -> str:
    return _PUNCT.sub("", unicodedata.normalize("NFKC", str(s or "")).casefold())


@dataclass
class Finding:
    level: str          # 错误 / 提醒
    table: str
    text: str

    def line(self) -> str:
        return f"[{self.level}] {self.table}：{self.text}" if self.table else f"[{self.level}] {self.text}"


@dataclass
class InputReport:
    found: dict[str, str] = field(default_factory=dict)     # 模板里的表 -> 「文件 / 工作表」
    findings: list[Finding] = field(default_factory=list)
    checked: bool = True                                   # 场景没有输入模板时为 False

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "错误"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def text(self) -> str:
        if not self.checked:
            return "本场景还没有输入模板，没有做格式检查。"
        lines = [f"- {t}：{w}" for t, w in self.found.items()]
        lines += [f"- {f.line()}" for f in self.findings]
        head = "按输入模板检查：全部符合。" if not self.findings else (
            f"按输入模板检查：{len(self.errors)} 个错误，{len(self.findings) - len(self.errors)} 个提醒。")
        return head + ("\n" + "\n".join(lines) if lines else "")


def _sheets(files: list[Path]):
    from bonus_core.data.table_detect import detect
    out, bad = [], []
    for f in files:
        if f.suffix.lower() not in EXCEL:
            continue
        try:
            out += detect(f)
        except Exception as e:      # 文件坏了、加密了：照样报出来，不中断其他检查
            bad.append(Finding("错误", "", f"读不了 {f.name}：{type(e).__name__}: {e}"))
    return out, bad


def _names(x) -> list[str]:
    return [norm(n) for n in [x.name, *x.aliases] if norm(n)]


def _col_index(headers: list[str], col: Column) -> int | None:
    want = set(_names(col))
    hits = [i for i, h in enumerate(headers) if norm(h) in want]
    return hits[0] if hits else None


def _is_number(v) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float, Decimal)):
        return True
    s = unicodedata.normalize("NFKC", str(v)).strip().replace(",", "").replace("%", "")
    try:
        Decimal(s)
        return True
    except InvalidOperation:
        return False


def _is_date(v) -> bool:
    if isinstance(v, (date, datetime)):
        return True
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return 190001 <= v <= 210012 or 1 <= v <= 12     # 202609 这类年月、只写月份
    return bool(_DATE.match(unicodedata.normalize("NFKC", str(v)).strip()))


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _score(st, t: InputTable) -> int:
    heads = {norm(h) for h in st.headers}
    return sum(1 for c in t.columns if set(_names(c)) & heads)


def _pick(sheets, t: InputTable):
    names = _names(t)
    by_name = [st for st in sheets if any(n in norm(st.sheet) or n in norm(Path(st.file).stem) for n in names)]
    need = [c for c in t.columns if c.required]
    # 表名、文件名都对不上时按列名认：必需列对上大半（至少 2 列）才算，缺的列照常报出来
    least = max(2, -(-len(need) * 2 // 3))
    pool = by_name or [st for st in sheets if need and len(need) >= 2 and sum(
        1 for c in need if set(_names(c)) & {norm(h) for h in st.headers}) >= least]
    if not pool:
        return None, []
    pool = sorted(pool, key=lambda st: -_score(st, t))
    best = pool[0]
    ties = [st for st in pool[1:] if _score(st, t) == _score(best, t)]
    return best, ties


def _check_table(st, t: InputTable, out: list[Finding]) -> None:
    for c in t.columns:
        i = _col_index(st.headers, c)
        if i is None:
            if c.required:
                out.append(Finding("错误", t.name, f"缺少列「{c.name}」" + (f"（也认：{'、'.join(c.aliases)}）" if c.aliases else "")))
            continue
        bad, blank = [], []
        for row_no, values in st.rows:
            v = values[i] if i < len(values) else None
            if _blank(v):
                if c.nonblank:
                    blank.append(row_no)
                continue
            if (c.type == "number" and not _is_number(v)) or (c.type == "date" and not _is_date(v)):
                bad.append((row_no, v))
        if bad:
            kind = "数字" if c.type == "number" else "日期"
            eg = "、".join(f"第 {r} 行「{v}」" for r, v in bad[:EXAMPLES])
            out.append(Finding("错误", t.name, f"列「{c.name}」应为{kind}，有 {len(bad)} 行不是：{eg}" + ("……" if len(bad) > EXAMPLES else "")))
        if blank:
            eg = "、".join(str(r) for r in blank[:EXAMPLES])
            out.append(Finding("错误", t.name, f"列「{c.name}」必须填写，有 {len(blank)} 行为空（第 {eg} 行" + ("……" if len(blank) > EXAMPLES else "") + "）"))


def check_inputs(sc: Scenario, files: list[str | Path]) -> InputReport:
    if not sc.inputs:
        return InputReport(checked=False)
    files = [Path(f) for f in files]
    sheets, findings = _sheets(files)
    rep = InputReport(findings=findings)
    used = set()
    for t in sc.inputs:
        st, ties = _pick(sheets, t)
        if st is None:
            rep.findings.append(Finding("错误" if t.required else "提醒", t.name,
                                        "没找到这张表（按表名、文件名和列名都没对上）" + ("" if t.required else "，这张表可以不提供")))
            continue
        where = f"{st.file} / {st.sheet}（表头在第 {st.header_row} 行，{len(st.rows)} 行数据）"
        rep.found[t.name] = where
        used.add((st.file, st.sheet))
        if ties:
            rep.findings.append(Finding("提醒", t.name, f"有多张表都像，按 {st.file} / {st.sheet} 检查；其他：" +
                                        "、".join(f"{x.file} / {x.sheet}" for x in ties)))
        _check_table(st, t, rep.findings)
    extra = [f"{st.file} / {st.sheet}" for st in sheets if (st.file, st.sheet) not in used]
    if extra:
        rep.findings.append(Finding("提醒", "", "这些表不在输入模板里：" + "、".join(extra)))
    return rep


def template_text(sc: Scenario) -> str:
    """输入模板的文字说明（给智能体和财务看）。"""
    if not sc.inputs:
        return ""
    kinds = {"text": "文字", "number": "数字", "date": "日期"}
    lines = []
    for t in sc.inputs:
        cols = "、".join(f"{c.name}（{kinds[c.type]}{'' if c.required else '，可选'}{'，' + c.note if c.note else ''}）" for c in t.columns)
        lines.append(f"- {t.name}{'' if t.required else '（可选）'}" + (f"：{t.note}" if t.note else "") + (f"。列：{cols}" if cols else ""))
    return "\n".join(lines)
