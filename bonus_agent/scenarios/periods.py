"""Approved per-table date contracts, applied before the amount engine."""
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from bonus_core.data.table import StdTable


class PeriodError(ValueError):
    code = "period_contract"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DateWindow(Strict):
    start: date
    end_exclusive: date

    @model_validator(mode="after")
    def bounds(self):
        if self.start >= self.end_exclusive:
            raise ValueError("期间起点必须早于终点；区间为左闭右开")
        return self


class DatePolicy(Strict):
    mode: Literal["window", "as_of", "none"]
    field: str | None = None
    action: Literal["block", "filter"] = "block"
    reason: str = ""
    windows: dict[str, DateWindow] | None = None

    @model_validator(mode="after")
    def policy(self):
        if self.mode == "none":
            if self.field or self.windows or self.action != "block" or not self.reason.strip():
                raise ValueError("不适用期间的表必须说明原因，不能同时配置日期字段或筛选")
        elif not self.field:
            raise ValueError("期间契约必须指定日期字段")
        if self.action == "filter" and not self.reason.strip():
            raise ValueError("自动排除跨期行必须提供财务批准的原因")
        return self


def validate_contracts(rs, periods, windows, policies):
    if set(periods) != set(windows):
        raise PeriodError("所有批准期间必须有明确的日期范围")
    if set(policies) != set(rs.datasets):
        raise PeriodError("每张数据表必须明确声明期间契约或不适用的原因")
    for name, policy in policies.items():
        if policy.windows is not None and set(policy.windows) != set(periods):
            raise PeriodError(f"{name} 的累计/特定日期范围必须覆盖全部批准期间")
        if policy.mode != "none":
            field = rs.datasets[name].fields.get(policy.field)
            if field is None or field.type != "date" or field.if_empty not in (None, ""):
                raise PeriodError(f"{name}.{policy.field} 必须是明确的 date 字段，禁止为空日期自动填默认值")


def apply_period(spec, period, data):
    if period not in spec.period_windows:
        raise PeriodError("期间未获批准或缺少日期范围")
    tables, report = {}, {"period": period, "tables": {}}
    for name, table in data.items():
        policy = spec.period_contracts[name]
        window = (policy.windows or spec.period_windows)[period]
        kept, excluded = [], []
        for row in table.rows:
            if policy.mode == "none":
                kept.append(row)
                continue
            value = row.values.get(policy.field)
            if not isinstance(value, date):
                raise PeriodError(f"{name}.{policy.field} 日期为空或无效：{row.src}")
            eligible = value < window.end_exclusive and (policy.mode == "as_of" or value >= window.start)
            if eligible:
                kept.append(row)
            elif policy.action == "block":
                raise PeriodError(f"{name}.{policy.field}={value} 不符合期间 {period} 的 {policy.mode} 范围 "
                                  f"[{window.start}, {window.end_exclusive})：{row.src}")
            else:
                excluded.append({"source": str(row.src), "date": value.isoformat(),
                    "reason": policy.reason, "values": {k: v.isoformat() if isinstance(v,date) else
                    str(v) if isinstance(v,Decimal) else v for k,v in row.values.items()}})
        tables[name] = StdTable(name, kept)
        report["tables"][name] = {"input_count": len(table.rows), "kept_count": len(kept),
            "excluded_count": len(excluded), "excluded": excluded, "mode": policy.mode,
            "reason": policy.reason, "window": window.model_dump(mode="json")}
    return tables, report
