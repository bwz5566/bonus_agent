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
from bonus_core.data.table_detect import SheetTable, detect
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
    columns: dict[str, str]            # 标准字段 -> 列名
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
    exprs = [v.expr for v in rs.variables.values()] + [rs.pool.expr] + [lv.weight for lv in rs.allocation]
    exprs += [p.hold_ratio for p in rs.payout]
    for e in exprs:
        used |= names(parse(e))
    for d in rs.datasets.values():
        used |= {k for k in (d.key, d.link) if k}
    for lv in rs.allocation:
        if lv.parent_key:
            used.add(lv.parent_key)
    return used & set(rs.field_owner())


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
        st = next((s for s in sheets if s.sheet == m.sheet and (m.file is None or s.file == m.file)), None) if m.sheet else None
        if st is None:
            st = next((s for s in sheets if all(c in s.headers for c in m.columns.values())), None)
        if st is None:
            issues.append(Issue.block("sheet_missing", f"找不到「{ds_name}」对应的 sheet（{m.sheet}）"))
            continue
        for f in ds.fields:
            if f not in m.columns and f in needed:
                issues.append(Issue.block("field_unmapped", f"规则用到的字段 {f}（{ds.fields[f].desc}）没有对应的列", ds_name))
            elif f in m.columns and m.columns[f] not in st.headers:
                issues.append(Issue.block("column_missing", f"列「{m.columns[f]}」不在 sheet「{st.sheet}」中", ds_name))
        idx = {f: st.headers.index(c) for f, c in m.columns.items() if c in st.headers and f in ds.fields}
        t = StdTable(ds_name)
        for excel_row, vals in st.rows:
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
