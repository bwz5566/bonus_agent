"""规则 DSL 的数据模型。

规则集是一份 YAML/JSON 文档，由这里的 pydantic 模型定义和校验。
同一份模型导出的 JSON Schema 也用于约束大模型的输出。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


def to_decimal(v) -> Decimal:
    """把 YAML/JSON 里的数字安全地转成 Decimal（不经过 float 误差）。"""
    if isinstance(v, Decimal):
        return v
    if isinstance(v, bool):
        raise ValueError("布尔值不能作为数字")
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if s.endswith("%"):
            return Decimal(s[:-1]) / Decimal(100)
        return Decimal(s)
    raise ValueError(f"无法转换为数字: {v!r}")


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRef(_Base):
    """规则元素在原始文档中的出处。"""
    doc: str
    page: int | None = None
    quote: str = ""


class RuleItem(_Base):
    """所有可被财务确认的规则元素的公共字段。"""
    desc: str = ""
    sources: list[SourceRef] = []
    confidence: float | None = None
    confirmed_by: str | None = None


FieldType = Literal["money", "number", "int", "ratio", "text", "enum", "date", "id"]


class FieldDef(_Base):
    type: FieldType
    desc: str = ""
    unit: Literal["元", "万元"] | None = None
    values: list[str] | None = None


class DatasetDef(_Base):
    """标准数据表。

    - 有 key：实体表，每行一个实体（如人员表 key=employee_id）。
    - 有 link：明细表，每行通过 link 字段挂到某一层级的实体上（如销售明细 link=employee_id）。
    - 都没有：公司级表，行属于“全部”，只能在 global 作用域聚合（如各月净利润）。
    """
    desc: str = ""
    key: str | None = None
    link: str | None = None
    fields: dict[str, FieldDef]


class TierTable(RuleItem):
    kind: Literal["tier"] = "tier"
    mode: Literal["whole", "excess"]          # 全额累进 / 超额累进
    steps: list[tuple[Decimal, Decimal]]       # (下限, 比例)
    lower_inclusive: bool = True               # 达到下限即适用

    @field_validator("steps", mode="before")
    @classmethod
    def _steps(cls, v):
        return [(to_decimal(a), to_decimal(b)) for a, b in v]


class LookupTable(RuleItem):
    kind: Literal["lookup"] = "lookup"
    mapping: dict[str, Decimal]
    default: Decimal | None = None

    @field_validator("mapping", mode="before")
    @classmethod
    def _mapping(cls, v):
        return {str(k): to_decimal(x) for k, x in v.items()}

    @field_validator("default", mode="before")
    @classmethod
    def _default(cls, v):
        return None if v is None else to_decimal(v)


Table = Annotated[Union[TierTable, LookupTable], Field(discriminator="kind")]


class Variable(RuleItem):
    scope: str = "global"      # "global" 或分配层级名，如 "个人"
    expr: str


class Pool(RuleItem):
    expr: str


class AllocationLevel(RuleItem):
    level: str                 # 层级名：部门 / 小组 / 个人 ...
    key: str                   # 实体主键字段
    parent_key: str | None = None
    weight: str                # 权重表达式，在本层级求值


class PayoutCondition(RuleItem):
    id: str
    scope: str                 # 目前只支持最末一层（个人）
    hold_ratio: str            # 挂起比例，0~1


class GoldenCase(_Base):
    """财务手算的金标样例。datasets 直接给标准表的行。"""
    name: str
    datasets: dict[str, list[dict]]
    expect: dict[str, str]     # 节点 id（或 "pool"）-> 期望金额（保留到分）


class RuleSet(_Base):
    name: str
    version: str
    effective_from: date | None = None
    effective_to: date | None = None
    datasets: dict[str, DatasetDef]
    tables: dict[str, Table] = {}
    variables: dict[str, Variable] = {}
    pool: Pool
    allocation: list[AllocationLevel]
    payout: list[PayoutCondition] = []
    tests: list[GoldenCase] = []

    # ---- 便捷方法 ----
    @classmethod
    def load(cls, path: str | Path) -> "RuleSet":
        with open(path, encoding="utf-8") as f:
            return cls.model_validate(yaml.safe_load(f))

    def dump_yaml(self) -> str:
        data = self.model_dump(mode="json", exclude_defaults=False)
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)

    @property
    def leaf(self) -> AllocationLevel:
        return self.allocation[-1]

    def level(self, name: str) -> AllocationLevel | None:
        return next((lv for lv in self.allocation if lv.level == name), None)

    def entity_dataset(self, key: str) -> tuple[str, DatasetDef] | None:
        for n, d in self.datasets.items():
            if d.key == key:
                return n, d
        return None

    def field_owner(self) -> dict[str, list[str]]:
        """字段名 -> 包含该字段的数据集列表。"""
        out: dict[str, list[str]] = {}
        for n, d in self.datasets.items():
            for f in d.fields:
                out.setdefault(f, []).append(n)
        return out
