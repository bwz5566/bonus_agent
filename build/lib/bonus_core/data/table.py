"""标准数据表：每行带有来源指针，便于追溯到原始 Excel 的某一行。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from bonus_core.dsl.models import DatasetDef, FieldDef, RuleSet


@dataclass(frozen=True)
class RowRef:
    file: str
    sheet: str
    row: int  # Excel 行号（从 1 开始）

    def __str__(self) -> str:
        return f"{self.file}/{self.sheet}!{self.row}"


@dataclass
class Row:
    values: dict
    src: RowRef | None = None


@dataclass
class StdTable:
    name: str
    rows: list[Row] = field(default_factory=list)


Datasets = dict[str, StdTable]


class ConvertError(ValueError):
    pass


_NUM_CLEAN = re.compile(r"[,\s¥￥$元]")


def convert(value, fd: FieldDef):
    """把单元格的值转换为字段类型。空值返回 None。"""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    t = fd.type
    if t in ("money", "number", "int", "ratio"):
        if isinstance(value, bool):
            raise ConvertError(f"布尔值不能作为数字: {value!r}")
        if isinstance(value, (int, float)):
            d = Decimal(str(value))
        else:
            s = _NUM_CLEAN.sub("", str(value))
            pct = s.endswith("%")
            try:
                d = Decimal(s[:-1] if pct else s)
            except InvalidOperation:
                raise ConvertError(f"无法解析为数字: {value!r}") from None
            if pct:
                d = d / Decimal(100)
        if t == "money" and fd.unit == "万元":
            d = d * Decimal(10000)
        if t == "int" and d != d.to_integral_value():
            raise ConvertError(f"应为整数: {value!r}")
        return d
    if t == "date":
        return _to_date(value)
    if t == "bool":
        return to_bool(value)
    s = str(value).strip()
    if isinstance(value, float) and value.is_integer():
        s = str(int(value))  # Excel 里的编号常被存成 1001.0
    if t == "enum" and fd.values and s not in fd.values:
        raise ConvertError(f"取值 {s!r} 不在枚举 {fd.values} 中")
    return s


_TRUE = {"true", "1", "是", "y", "yes", "√", "已审批", "已确认"}
_FALSE = {"false", "0", "否", "n", "no", "×", "未审批", "未确认"}


def to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    s = str(int(value) if isinstance(value, float) and value.is_integer() else value).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ConvertError(f"无法解析为是/否: {value!r}")


_DATE = re.compile(r"^(\d{4})\s*[-/.年]\s*(\d{1,2})(?:\s*[-/.月]\s*(\d{1,2})\s*日?)?\s*月?")


def to_date(value) -> date:
    return _to_date(value)


def _to_date(value) -> date:
    """日期：Excel 日期、2026-01-15、2026/1/5、2026年1月5日；只有年月（2026-01、2026年1月）取当月 1 日。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 20000 < value < 80000:  # Excel 序列号
            return date(1899, 12, 30) + timedelta(days=int(value))
        value = str(int(value))
    s = str(value).strip()
    if re.fullmatch(r"\d{6}|\d{8}", s):  # 202601 / 20260115
        s = f"{s[:4]}-{s[4:6]}" + (f"-{s[6:]}" if len(s) == 8 else "")
    m = _DATE.match(s)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3] or 1))
        except ValueError:
            pass
    raise ConvertError(f"无法解析为日期: {value!r}")


def from_records(rs: RuleSet, records: dict[str, list[dict]], file: str = "inline") -> Datasets:
    """把普通 dict 行（已是标准字段名）转为标准表，用于金标样例和测试。"""
    out: Datasets = {}
    for name, rows in records.items():
        ds: DatasetDef = rs.datasets[name]
        t = StdTable(name)
        for i, r in enumerate(rows, start=1):
            vals = {}
            for f, fd in ds.fields.items():
                vals[f] = convert(r.get(f), fd)
            t.rows.append(Row(vals, RowRef(file, name, i)))
        out[name] = t
    return out
