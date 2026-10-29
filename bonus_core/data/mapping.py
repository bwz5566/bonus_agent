"""字段映射：把 Excel 的列对应到规则里的标准字段，并做类型转换和数据校验。

映射文件格式（YAML）::

    人员:
      sheet: 人员名单          # 可省略，省略时按表头自动找
      columns: {employee_id: 工号, name: 姓名, group_id: 所属小组}
"""
from __future__ import annotations

import difflib
import statistics
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from bonus_core.data.table import ConvertError, Datasets, Row, RowRef, StdTable, convert
from bonus_core.data.table_detect import SheetTable, detect, table_at
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
    filter: dict[str, list[str] | str] = {}  # 只取这些行：列名 -> 允许的取值（如 目标值表里 entity_type 为 group 的行）
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
    for e in exprs:
        used |= names(parse(e))
    for d in rs.datasets.values():
        used |= {k for k in (d.key, d.link) if k}
    for lv in rs.allocation:
        if lv.parent_key:
            used.add(lv.parent_key)
    return used & set(rs.field_owner())


def rebind_files(mapping: Mapping, files: list[str | Path]) -> Mapping:
    """模板按期复用时，下一期的文件名通常会变（如 2026-10 → 2026-11）。映射里记的文件名不在本批时，
    找出包含它对应全部工作表的那个新文件，换成新文件名；只在能唯一对上时才换，否则原样返回，照常报“文件不在本批”。"""
    import openpyxl

    paths = {Path(f).name: Path(f) for f in files}
    stale = sorted({m.file for m in mapping.values() if m.file is not None and m.file not in paths})
    if not stale:
        return mapping
    fresh = [n for n in paths if n not in {m.file for m in mapping.values()}]
    sheets: dict[str, set[str]] = {}
    for n in fresh:
        try:
            wb = openpyxl.load_workbook(paths[n], read_only=True)
            sheets[n] = set(wb.sheetnames)
            wb.close()
        except Exception:
            continue
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


def apply_mapping(rs: RuleSet, files: list[str | Path], mapping: Mapping) -> tuple[Datasets, list[Issue]]:
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
            f = next((Path(x) for x in files if m.file is None or Path(x).name == m.file), None)
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
        for excel_row, vals in st.rows:
            if not keep(vals):
                continue
            src = RowRef(st.file, st.sheet, excel_row)
            rv = {}
            for f, fd in ds.fields.items():
                raw = vals[idx[f]] if f in idx else None
                try:
                    rv[f] = convert(raw, fd)
                except ConvertError as e:
                    issues.append(Issue.block("convert", f"{f}: {e}", str(src)))
                    rv[f] = None
            t.rows.append(Row(rv, src))
        data[ds_name] = t
        issues += _reconcile(st, idx, ds, t)
        issues += _sanity(ds_name, ds, t, needed)
    return data, issues


def cell_text(v) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return "" if v is None else str(v).strip()


def row_filter(headers: list[str], flt: dict[str, list[str]]):
    """返回行筛选函数；列名不存在时返回错误信息。"""
    if not flt:
        return lambda vals: True
    idx = {}
    for col, allowed in flt.items():
        if col not in headers:
            return f"筛选条件的列「{col}」不在表头中：{[h for h in headers if h]}"
        idx[headers.index(col)] = {cell_text(a) for a in ([allowed] if isinstance(allowed, str) else allowed)}
    return lambda vals: all(cell_text(vals[i] if i < len(vals) else None) in ok for i, ok in idx.items())


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


def _sanity(ds_name, ds, t: StdTable, needed) -> list[Issue]:
    out = []
    for f, fd in ds.fields.items():
        if f not in needed:
            continue
        vals = [(r.values[f], r.src) for r in t.rows]
        empty = [str(s) for v, s in vals if v is None]
        if empty:
            out.append(Issue.block("value_empty", f"「{ds_name}」字段 {f} 有 {len(empty)} 个空值", ", ".join(empty[:5])))
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
