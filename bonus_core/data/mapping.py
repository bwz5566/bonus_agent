"""字段映射：把 Excel 的列对应到规则里的标准字段，并做类型转换和数据校验。

映射文件格式（YAML）::

    人员:
      sheet: 人员名单          # 可省略，省略时按表头自动找
      columns: {employee_id: 工号, name: 姓名, group_id: 所属小组}
"""
from __future__ import annotations

import difflib
import re
import statistics
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from bonus_core.data.table import ConvertError, Datasets, Row, RowRef, StdTable, convert
from bonus_core.data.table_detect import ROW_ID, SheetTable, add_row_ids, combine_columns, detect, table_at, unpivot_by
from bonus_core.dsl.models import RuleSet
from bonus_core.dsl.parser import names, parse
from bonus_core.engine.issues import Issue

# 常见同义词，可按业务持续补充
SYNONYMS: dict[str, list[str]] = {
    "employee_id": ["工号", "员工编号", "员工号", "人员编号", "负责人工号"],
    "name": ["姓名", "名称", "员工姓名", "小组名称"],
    "group_id": ["小组", "小组编号", "所属小组", "组别", "团队"],
    "project_id": ["项目编号", "合同编号", "项目号", "合同号"],
    "sales_amount": ["销售额", "合同金额", "签约金额", "合同额", "销售金额"],
    "net_profit": ["净利润", "利润"],
    "gross_margin": ["毛利率"],
    "collected": ["回款", "已回款", "回款金额", "收款金额", "已收款"],
    "region": ["区域", "地区", "大区"],
    "project_type": ["项目类型", "新老项目", "项目性质"],
    "seniority": ["工龄", "司龄", "工作年限"],
}


class DatasetMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str | None = None            # 多个文件里有同名 sheet 时用文件名区分
    sheet: str | None = None
    header_rows: list[int] | None = None  # 自动识别的表头不对时，指定 Excel 表头行号
    columns: dict[str, str]            # 标准字段 -> 列名
    filter: dict[str, list[str] | str | dict[str, list[str]]] = {}  # 只取这些行：列名 -> 允许的取值；{"not": [...]} 为去掉这些取值的行
    unpivot: dict | None = None        # 几列同一类的值展开成每个值一行：{"columns": [...], "value": 列名, "label": 列名}（先展开再筛选）
    combine: dict[str, list[str]] | None = None   # 几列合成一个编号列：{"新列名": ["列1", "列2"]}（如项目 = 月份+客户+型号），展开之后再合成
    confidence: dict[str, float] = {}  # 推荐时填写
    confirmed_by: str | None = None


Mapping = dict[str, DatasetMapping]


def load_mapping(path: str | Path) -> Mapping:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return {k: DatasetMapping.model_validate(v) for k, v in raw.items()}


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _score(field: str, desc: str, header: str) -> float:
    h = _norm(header)
    if not h:
        return 0.0
    cands = [field, desc] + SYNONYMS.get(field, [])
    best = 0.0
    for c in cands:
        c = _norm(c)
        if not c:
            continue
        if c == h:
            return 1.0
        if c in h or h in c:
            best = max(best, 0.85)
        best = max(best, difflib.SequenceMatcher(None, c, h).ratio() * 0.8)
    return best


def suggest_heuristic(rs: RuleSet, sheets: list[SheetTable], history: Mapping | None = None) -> Mapping:
    """规则启发式推荐：历史映射 > 同义词与字符串相似度。低置信度的交给大模型或财务。"""
    out: Mapping = {}
    for ds_name, ds in rs.datasets.items():
        hist = (history or {}).get(ds_name)
        best_sheet, best_cols, best_conf, best_total = None, {}, {}, -1.0
        for st in sheets:
            cols, conf = {}, {}
            for f, fd in ds.fields.items():
                if hist and hist.columns.get(f) in st.headers:
                    cols[f], conf[f] = hist.columns[f], 1.0
                    continue
                scored = sorted(((_score(f, fd.desc, h), h) for h in st.headers if h), reverse=True)
                if scored and scored[0][0] >= 0.5:
                    cols[f], conf[f] = scored[0][1], round(scored[0][0], 2)
            total = sum(conf.values()) + (2 if st.sheet == ds_name or ds_name in st.sheet else 0)
            if total > best_total:
                best_sheet, best_cols, best_conf, best_total = st.sheet, cols, conf, total
        out[ds_name] = DatasetMapping(sheet=best_sheet, columns=best_cols, confidence=best_conf)
    return out


def used_fields(rs: RuleSet) -> set[str]:
    """规则表达式里实际用到的字段，加上主键和关联键。"""
    used = set()
    exprs = [v.expr for v in rs.variables.values()] + [rs.pool.expr] + [lv.weight for lv in rs.allocation if lv.weight.strip()]
    exprs += [e for p in rs.payout for e in (p.hold_ratio, p.pay_ratio) if e.strip()]
    exprs += [c.expr for c in rs.checks] + [o.expr for o in rs.offsets]
    if rs.report:
        exprs += [t.expr for t in [*rs.report.totals, *rs.report.pending]]
        exprs += [e for b in rs.report.balances for e in [b.total, *b.parts]]
    for e in exprs:
        used |= names(parse(e))
    for d in rs.datasets.values():
        used |= {k for k in (d.key, d.link) if k}
    for lv in rs.allocation:
        if lv.parent_key:
            used.add(lv.parent_key)
    return used & set(rs.field_owner())


def _sheet_names(path) -> list[str]:
    import openpyxl
    try:
        wb = openpyxl.load_workbook(path, read_only=True)
    except Exception:
        return []
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def rebind_files(mapping: Mapping, files: list[str | Path]) -> Mapping:
    """模板按期复用时，下一期的文件名通常会变（如 2026-10 → 2026-11）。映射里记的文件名不在本批时，
    找出包含它对应全部工作表的那个新文件，换成新文件名；只在能唯一对上时才换，否则原样返回，照常报“文件不在本批”。"""
    paths = {Path(f).name: Path(f) for f in files}
    stale = sorted({m.file for m in mapping.values() if m.file is not None and m.file not in paths})
    if not stale:
        return mapping
    fresh = [n for n in paths if n not in {m.file for m in mapping.values()}]
    sheets = {n: set(_sheet_names(paths[n])) for n in fresh}
    to: dict[str, str] = {}
    for old in stale:
        want = {m.sheet for m in mapping.values() if m.file == old and m.sheet}
        if want:
            hits = [n for n in sheets if want <= sheets[n]]
        else:          # 没记工作表时，只有一个旧文件、一个新文件才能对上
            hits = list(sheets) if len(stale) == 1 else []
        if len(hits) == 1:
            to[old] = hits[0]
    if len(set(to.values())) != len(to):        # 两个旧文件对到同一个新文件，拿不准，不换
        return mapping
    return {k: m.model_copy(update={"file": to[m.file]}) if m.file in to else m for k, m in mapping.items()}


def apply_mapping(rs: RuleSet, files: list[str | Path], mapping: Mapping, period=None) -> tuple[Datasets, list[Issue]]:
    """period：本次核算期间（bonus_core.data.period.Period）。写了 period 字段的数据表只取落在期间里的行。"""
    sheets = [s for f in files for s in detect(f)]
    data: Datasets = {}
    issues: list[Issue] = []
    needed = used_fields(rs)
    for ds_name, ds in rs.datasets.items():
        m = mapping.get(ds_name)
        if m is None:
            issues.append(Issue.block("mapping_missing", f"数据表「{ds_name}」没有映射"))
            continue
        st = None
        # 指定了文件或工作表就只认指定的，找不到时报错；不能静默改读别的表（如“旧数据”）
        if m.file is not None and not any(Path(x).name == m.file for x in files):
            issues.append(Issue.block("file_missing", f"「{ds_name}」指定的文件「{m.file}」不在本批输入中", ds_name))
            continue
        if m.header_rows and m.sheet:
            cand = [Path(x) for x in files if Path(x).name == m.file] if m.file is not None else \
                [Path(x) for x in files if m.sheet in _sheet_names(x)]
            if len(cand) > 1:
                issues.append(Issue.block("sheet_ambiguous", f"「{ds_name}」的工作表「{m.sheet}」在多个文件中都有："
                                          + "、".join(x.name for x in cand) + "，请指定文件", ds_name))
                continue
            f = cand[0] if cand else None
            try:
                st = table_at(f, m.sheet, m.header_rows) if f else None
            except (KeyError, ValueError):
                st = None
        elif m.sheet:
            hits = [s for s in sheets if s.sheet == m.sheet and (m.file is None or s.file == m.file)]
            if len(hits) > 1:
                issues.append(Issue.block("sheet_ambiguous", f"「{ds_name}」的工作表「{m.sheet}」在多个文件中都有："
                                          + "、".join(s.file for s in hits) + "，请指定文件", ds_name))
                continue
            st = hits[0] if hits else None
        elif m.file is None:      # 没指定位置时才按表头找，且只能有一张表匹配
            hits = [s for s in sheets if all(c in s.headers for c in m.columns.values())]
            if len(hits) > 1:
                issues.append(Issue.block("sheet_ambiguous", f"「{ds_name}」按表头能匹配多张表："
                                          + "、".join(f"{s.file}/{s.sheet}" for s in hits) + "，请指定文件和工作表", ds_name))
                continue
            st = hits[0] if hits else None
        else:
            hits = [s for s in sheets if s.file == m.file]
            st = hits[0] if len(hits) == 1 else None
        if st is None:
            where = "/".join(x for x in (m.file, m.sheet) if x) or "按表头查找"
            issues.append(Issue.block("sheet_missing", f"找不到「{ds_name}」对应的工作表（{where}）", ds_name))
            continue
        m, moved = rebind_dup_columns(m, st.headers)
        for note in moved:
            issues.append(Issue.warn("dup_column_moved", f"「{ds_name}」{note}，请核对这一列的取值", ds_name))
        used_cols = set(m.columns.values()) | set((m.unpivot or {}).get("columns") or []) | ({m.unpivot["share_from"]} if (m.unpivot or {}).get("share_from") else set()) | {
            c for cols in (m.combine or {}).values() for c in cols}
        unsure = sorted({st.headers[i] for _, cols in getattr(st, "unsure", []) for i in cols
                         if i < len(st.headers) and st.headers[i] in used_cols})
        if unsure:     # 另一段表头的列名对不上、又没法按列名对回去：用到的列读到的可能是别的数，不能算
            issues.append(Issue.block("segment_unsure", f"「{ds_name}」的 sheet「{st.sheet}」中间另一段表头的列对不上第一段，"
                                      f"用到的列「{'、'.join(unsure)}」在那一段里不知道该读哪一列", ds_name))
            continue
        if m.unpivot:
            bad = [c for c in m.unpivot.get("columns") or [] if c not in st.headers]
            if bad:
                issues.append(Issue.block("column_missing", f"unpivot 的列「{'、'.join(bad)}」不在 sheet「{st.sheet}」中", ds_name))
                continue
            st = unpivot_by(st, m.unpivot)
        if m.combine:
            bad = [c for cols in m.combine.values() for c in cols if c not in st.headers]
            if bad:
                issues.append(Issue.block("column_missing", f"combine 的列「{'、'.join(bad)}」不在 sheet「{st.sheet}」中", ds_name))
                continue
            st = combine_columns(st, m.combine)
        if ROW_ID in m.columns.values():
            st = add_row_ids(st)
        for f in ds.fields:
            if f not in m.columns and f in needed:
                issues.append(Issue.block("field_unmapped", f"规则用到的字段 {f}（{ds.fields[f].desc}）没有对应的列", ds_name))
            elif f in m.columns and m.columns[f] not in st.headers:
                issues.append(Issue.block("column_missing", f"列「{m.columns[f]}」不在 sheet「{st.sheet}」中", ds_name))
        idx = {f: st.headers.index(c) for f, c in m.columns.items() if c in st.headers and f in ds.fields}
        t = StdTable(ds_name)
        keep = row_filter(st.headers, m.filter)
        if isinstance(keep, str):
            issues.append(Issue.block("filter_column", keep, ds_name))
            continue
        pcol = idx.get(ds.period) if ds.period else None
        if ds.period and period is not None and pcol is None:
            issues.append(Issue.block("period_unmapped", f"「{ds_name}」按期间筛选的字段 {ds.period} 没有对应的列", ds_name))
            continue
        win = period.window(ds.window) if period is not None else None
        win_text = "" if win is None else {"year_to_date": f"（年初累计 {win.start}~{win.end}）", "to_date": f"（截至 {win.end}）"}.get(ds.window, "")
        out_of, unclear, skipped = 0, [], 0
        for excel_row, vals in st.rows:
            if not keep(vals):
                continue
            src = RowRef(st.file, st.sheet, excel_row)
            if pcol is not None and period is not None:
                inside = win.contains(vals[pcol] if pcol < len(vals) else None)
                if inside is False:
                    out_of += 1
                    continue
                if inside is None:
                    unclear.append((str(src), vals[pcol] if pcol < len(vals) else None))
                    continue
            if any(getattr(fd, "if_empty", None) == SKIP and f in idx and _blank(vals[idx[f]] if idx[f] < len(vals) else None)
                   for f, fd in ds.fields.items()):
                skipped += 1           # 这一格空着的行当作没有（如毛利率空着按“查不到”处理）
                continue
            rv = {}
            for f, fd in ds.fields.items():
                raw = vals[idx[f]] if f in idx else None
                try:
                    rv[f] = convert(raw, fd)
                except ConvertError as e:
                    issues.append(Issue.block("convert", f"{f}: {e}", str(src)))
                    rv[f] = None
            t.rows.append(Row(rv, src))
        if unclear:
            issues.append(Issue.block("period_unclear", f"「{ds_name}」有 {len(unclear)} 行的 {ds.period} 看不出是否属于核算期间 {period}{win_text}"
                                      f"（如 {unclear[0][1]!r}）：空值、写法认不出，或只有一部分落在期间里",
                                      unclear[0][0]))
        if skipped:
            issues.append(Issue.warn("empty_skipped", f"「{ds_name}」有 {skipped} 行按 if_empty: skip 当作没有（空格子所在的行不参与，"
                                     f"按对象取值时和“查不到”一样）", ds_name))
        if ds.period and period is not None:
            issues.append(Issue.warn("period_filtered", f"「{ds_name}」按核算期间 {period}{win_text} 筛选（{ds.period}）：取 {len(t.rows)} 行，"
                                     f"期间外 {out_of} 行不参与计算", ds_name))
        if ds.distinct and ds.key:
            t.rows, clash = _distinct(t.rows, ds.key)
            if clash:
                issues.append(Issue.block("distinct_conflict", f"「{ds_name}」按 {ds.key} 合并时，同一编号的 {clash[0]} 取值不同"
                                          f"（如 {clash[1]}：{clash[2]}）：这个字段不属于“每个 {ds.key} 一个”的对象，从 fields 去掉，"
                                          f"或改在原表上按 link 汇总", ds_name))
        data[ds_name] = t
        issues += _reconcile(st, idx, ds, t)
        issues += _sanity(ds_name, ds, t, needed)
    return data, issues


def _distinct(rows: list[Row], key: str) -> tuple[list[Row], tuple | None]:
    """同一编号只留第一行；其余字段在同一编号下不一样时返回 (字段, 编号, 两个取值)。空编号的行不算对象。"""
    first: dict = {}
    for r in rows:
        k = r.values.get(key)
        if k is None:
            continue
        if k not in first:
            first[k] = r
            continue
        for f, v in r.values.items():
            if v != first[k].values.get(f):
                return list(first.values()), (f, k, f"{first[k].values.get(f)} / {v}")
    return list(first.values()), None


def cell_text(v) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return "" if v is None else str(v).strip()


_PLAIN_NUM = re.compile(r"-?(0|[1-9]\d*)(\.\d+)?\Z")


def _num(t: str):
    """写法规范的数（不带前导 0）才按数值比：编号 0012 和 12 不是一回事。"""
    t = t.replace(",", "")
    if not _PLAIN_NUM.match(t):
        return None
    try:
        return Decimal(t)
    except (InvalidOperation, ValueError):
        return None


_DUP_COL = re.compile(r"(.+)\(([A-Z]{1,3})列\)\Z")


def rebind_dup_columns(m: "DatasetMapping", headers: list[str]) -> tuple["DatasetMapping", list[str]]:
    """映射里按位置写的同名列（如「客户简称(AE列)」）在本期表里换了位置（前面插了列、删了列）：
    同名的后几列里只有一列时，改认那一列并提示；有几列分不清的不改，照常报列不存在。返回 (映射, 提示)。"""
    notes: dict[str, str] = {}

    def fix(col):
        c = str(col)
        mm = _DUP_COL.match(c)
        if c in headers or not mm or mm.group(1) not in headers:
            return col
        cands = [h for h in headers if (x := _DUP_COL.match(h)) and x.group(1) == mm.group(1)]
        if len(cands) != 1:
            return col
        notes[c] = cands[0]
        return cands[0]
    cols = {f: fix(c) for f, c in m.columns.items()}
    unp = {**m.unpivot, "columns": [fix(c) for c in m.unpivot.get("columns") or []],
            **({"share_from": fix(m.unpivot["share_from"])} if m.unpivot.get("share_from") else {})} if m.unpivot else m.unpivot
    comb = {k: [fix(c) for c in v] for k, v in m.combine.items()} if m.combine else m.combine
    flt = {fix(k): v for k, v in m.filter.items()}
    if not notes:
        return m, []
    return (m.model_copy(update={"columns": cols, "unpivot": unp, "combine": comb, "filter": flt}),
            [f"「{a}」这一列在本期表里不在原来的位置，改用同名的「{b}」" for a, b in notes.items()])


def _in(text: str, vals: set[str], nums: set) -> bool:
    """取值在列表里：按文字比，数字再按数值比（0、0.0、"0.00" 都算 0）。"""
    if text in vals:
        return True
    if nums:
        n = _num(text)
        return n is not None and n in nums
    return False


def row_filter(headers: list[str], flt: dict[str, list[str]]):
    """返回行筛选函数；列名不存在时返回错误信息。
    取值写列表（或一个值）：只取这些行；写 {"not": [...]}：去掉这些行（如 {"not": ["剔除"]}、金额列 {"not": ["0"]}）。"""
    if not flt:
        return lambda vals: True
    idx = []
    for col, allowed in flt.items():
        if col not in headers:
            return f"筛选条件的列「{col}」不在表头中：{[h for h in headers if h]}"
        neg = isinstance(allowed, dict)
        if neg:
            if set(allowed) != {"not"}:
                return f"筛选条件「{col}」写列表（只取这些值）或 {{\"not\": [...]}}（去掉这些值），不认识 {sorted(allowed)}"
            allowed = allowed["not"]
        vals = {cell_text(a) for a in ([allowed] if isinstance(allowed, (str, int, float)) else allowed)}
        nums = {n for n in (_num(v) for v in vals) if n is not None}
        idx.append((headers.index(col), vals, nums, neg))
    return lambda row: all(_in(cell_text(row[i] if i < len(row) else None), ok, nums) != neg for i, ok, nums, neg in idx)


def _reconcile(st: SheetTable, idx, ds, t: StdTable) -> list[Issue]:
    """与 Excel 自带的合计行对账。"""
    out = []
    for excel_row, vals in st.totals:
        for f, i in idx.items():
            fd = ds.fields[f]
            if fd.type != "money" or vals[i] in (None, ""):
                continue
            try:
                expect = convert(vals[i], fd)
            except ConvertError:
                continue
            got = sum((r.values[f] or Decimal(0) for r in t.rows), Decimal(0))
            if abs(got - expect) > Decimal("0.01"):
                out.append(Issue.warn("total_mismatch", f"{f} 明细合计 {got} 与表内合计行 {expect} 不一致",
                                      f"{st.file}/{st.sheet}!{excel_row}"))
    return out


SKIP = "skip"


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _sanity(ds_name, ds, t: StdTable, needed) -> list[Issue]:
    out = []
    for f, fd in ds.fields.items():
        if f not in needed:
            continue
        vals = [(r.values[f], r.src) for r in t.rows]
        empty = [str(s) for v, s in vals if v is None]
        if empty:
            out.append(Issue.block("value_empty", f"「{ds_name}」字段 {f} 有 {len(empty)} 个空值。空值怎么算要有依据："
                                   f"材料写明了就照写，没写就 ask_question 问财务（列出这几行和备注）；"
                                   f"确定后在 define_dataset 的这个字段里写 if_empty（如 0）；材料说空着按“查不到 / 没有”处理时写 if_empty: \"skip\""
                                   f"（这些行不参与，按对象取值时和查不到一样）", ", ".join(empty[:5])))
        if fd.type == "money":
            nums = [v for v, _ in vals if v is not None]
            neg = [str(s) for v, s in vals if v is not None and v < 0]
            if neg:
                out.append(Issue.warn("negative", f"「{ds_name}」字段 {f} 有负数", ", ".join(neg[:5])))
            if len(nums) >= 5:
                med = statistics.median(nums)
                big = [str(s) for v, s in vals if v is not None and med > 0 and v > med * 20]
                if big:
                    out.append(Issue.warn("outlier", f"「{ds_name}」字段 {f} 有超过中位数 20 倍的值", ", ".join(big[:5])))
    return out
