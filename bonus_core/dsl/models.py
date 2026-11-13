"""规则 DSL 的数据模型。

规则集是一份 YAML/JSON 文档，由这里的 pydantic 模型定义和校验。
同一份模型导出的 JSON Schema 也用于约束大模型的输出。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


def to_decimal(v) -> Decimal:
    """把 YAML/JSON 里的数字安全地转成 Decimal（不经过 float 误差）。"""
    if isinstance(v, Decimal):
        if not v.is_finite():
            raise ValueError("规则参数必须是有限数字")
        return v
    if isinstance(v, bool):
        raise ValueError("布尔值不能作为数字")
    if isinstance(v, (int, float)):
        return to_decimal(Decimal(str(v)))
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("，", "")
        scale = Decimal(1)
        for suffix, k in (("%", Decimal("0.01")), ("万", Decimal(10000)), ("亿", Decimal(10) ** 8)):
            if s.endswith(suffix):
                s, scale = s[:-len(suffix)].strip(), k
                break
        try:
            return to_decimal(Decimal(s) * scale if scale != 1 else Decimal(s))
        except InvalidOperation:    # 不是数字：报成普通的校验错误（原来会让整次运行崩掉，只留下 ConversionSyntax）
            raise ValueError(f"无法转换为数字: {v!r}（写数字，可带 %、万、亿）") from None
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
    applies_when: str = ""  # material-backed applicability guard, checked on table use


FieldType = Literal["money", "number", "int", "ratio", "text", "enum", "date", "id", "bool"]


class FieldDef(_Base):
    type: FieldType
    desc: str = ""
    unit: Literal["元", "万元"] | None = None
    values: list[str] | None = None
    if_empty: str | None = None       # 空单元格按这个值处理（材料写明或财务答复过的口径）；不写时空值是数据问题，会被拦下


class DatasetDef(_Base):
    """标准数据表。

    - 有 key：实体表，每行一个实体（如人员表 key=employee_id）。
      key 不是分配层级主键的实体表是“关联实体”（如项目表 key=project_id、销售单 key=sales_id）：
      它挂在上级对象下面（link 指定，不写时按表中出现的层级主键或其他实体主键自动推断），
      可以有自己作用域的变量（scope 写表名），其他表的行通过同名编号字段引用它的字段和变量。
    - 只有 link：明细表，每行通过 link 字段挂到某一层级或某个关联实体上（如销售明细 link=employee_id、回款 link=sales_id）。
    - 都没有：公司级表，行属于“全部”，只能在 global 作用域聚合（如各月净利润）。
    """
    desc: str = ""
    key: str | None = None
    link: str | None = None
    fields: dict[str, FieldDef]
    # 按 key 的不同取值各建一个对象（如明细表里每个客户一个“客户”对象）：同一编号的多行合成一个，
    # 其余字段在同一编号下必须一样。没有单独的主表、又要按这个编号汇总时用
    distinct: bool = False
    # 明细表 / 关联实体的 link 编号在上级里找不到时：block（默认，阻断并要求问财务）；ignore（财务确认过“查不到的不参与”，
    # 这些行不挂到任何对象上，试算结果里提示有几行）
    unmatched: Literal["block", "ignore"] = "block"
    # 记业务日期或月份的字段：计算时只取落在这次核算期间里的行（期间在计算时选，规则里不写死月份）。
    # 名单、预算这类跨期主数据不写，按需要在表达式里用 period_start() / period_end() 取当期
    period: str | None = None
    # 按期间取哪些行（写了 period 才有用）：period（默认，只取本期）；year_to_date（本期所在年份年初到本期末，如按年内累计出货算月均）；
    # to_date（本期末及以前的全部行）。同一份明细既要当期又要累计时，定义成两张表
    window: Literal["period", "year_to_date", "to_date"] = "period"
    population_from: str | None = None


class TierTable(RuleItem):
    kind: Literal["tier"] = "tier"
    mode: Literal["whole", "excess"]          # 全额累进 / 超额累进
    steps: list[tuple[Decimal, Decimal]]       # (下限, 比例)
    lower_inclusive: bool = True               # 达到下限即适用
    # 低于第一档下限的值怎么算：zero（默认，不计：比例 0、金额 0）；first（按第一档比例照算，如退货冲销的负数：
    # 比例取第一档；超额累进的金额 =（值 − 第一档下限）× 第一档比例，为负）。不要用“把第一档下限写成很小的负数”代替：
    # 超额累进会从那个负数起逐段累加，每个对象都凭空多出一大笔
    below_first: Literal["zero", "first"] = "zero"

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
    ratio_default: Literal["rate", "coefficient"] | None = None


class Pool(RuleItem):
    expr: str
    scope: str = "global"      # 写分配层级名（如 部门）时，奖金池在该层每个对象上分别计算（四舍五入到分），上层为合计
    # 奖金池（按层计算时是某个对象的金额）算出负数时：block（默认，阻断，要按材料处理或问财务）；
    # allow（材料或财务明确负数照算，如退货冲销的项目提成为负、照样按比例分到人）
    negative: Literal["block", "allow"] = "block"


class Check(RuleItem):
    """数据校验：在 scope 的每个对象上求值，结果必须为“是”，否则阻断计算（或提示）。"""
    id: str
    scope: str = "global"      # global、分配层级名或关联实体表名
    expr: str                  # 条件表达式，如 abs(sum(share) - 1) < 0.000001
    message: str = ""
    severity: Literal["blocking", "warning"] = "blocking"


class AllocationLevel(RuleItem):
    level: str                 # 层级名：部门 / 小组 / 个人 ...
    key: str                   # 实体主键字段
    parent_key: str | None = None
    weight: str = ""           # 权重表达式，在本层级求值；奖金池所在层及以上不需要


class PayoutCondition(RuleItem):
    """发放条件，二选一：hold_ratio 挂起比例（挂起 = ROUND(应得 × 比例, 2)），
    或 pay_ratio 可发比例（可发上限 = ROUND(应得 × 比例, 2)，挂起 = 应得 − 可发上限，如按回款比例发放）。"""
    id: str
    scope: str                 # 目前只支持最末一层（个人）
    hold_ratio: str = ""
    pay_ratio: str = ""


class Offset(RuleItem):
    """已发金额抵扣（如年度奖扣减预付和已发的季度奖）：本次实发 = MAX(可发上限 − 已发, 0)，
    超发待核对 = MAX(已发 − 可发上限, 0)，只登记不追回。"""
    id: str
    scope: str                 # 最末一层；或最末层数据表里有编号字段的关联实体表（按该实体汇总后再抵扣）
    expr: str                  # 已发金额


class AppendRows(RuleItem):
    """数据修订：把补充表的行并入原表（如补充收款并入收款流水）。补充表要先按原表的字段名定义（列名、单位可以不同）。
    按 unique 字段去重：原表已有同号且内容相同的跳过（同一文件重传不重复累加），内容不同的报错。"""
    kind: Literal["append"] = "append"
    source: str                # 补充表（数据表名）
    into: str                  # 原表
    unique: str | None = None  # 去重字段（单号）；不写时用原表的 key
    values: dict[str, dict[str, str]] = {}   # 取值对照：字段 -> {补充表写法: 原表写法}，如 approved: {通过: 是, 驳回: 否}


class CorrectRows(RuleItem):
    """数据修订：按编号更正原表的字段值。两种写法二选一：
    - 长表（每行改一个字段）：field 写“更正字段”列、value 写“新值”列，labels 把更正字段的写法对照到原表字段名；
    - 宽表：set 写 {原表字段: 补充表里新值的字段}。
    expect 写 {原表字段: 补充表里旧值的字段}：先核对旧值，不符就报错。找不到编号、同一对象同一字段改成不同的值也报错；
    新值与原值相同不算变化（可重复执行）。"""
    kind: Literal["correct"] = "correct"
    source: str
    target: str
    key: str                   # 编号字段（两张表同名）
    field: str | None = None
    value: str | None = None
    labels: dict[str, str] = {}
    set: dict[str, str] = {}
    expect: dict[str, str] = {}


DataPatch = Annotated[Union[AppendRows, CorrectRows], Field(discriminator="kind")]


class GoldenCase(_Base):
    """财务手算的金标样例。datasets 直接给标准表的行。"""
    name: str
    datasets: dict[str, list[dict]]
    expect: dict[str, str]     # 节点 id（或 "pool"）-> 期望金额（保留到分）


class ReportTotal(RuleItem):
    """汇总核算里的一行：在某一层每个对象上算 expr 再合计（如 正常分配 = 各项目可分配之和）。level 写层级名，或 global（只算一次）。"""
    name: str
    level: str
    expr: str


class ReportBalance(RuleItem):
    """A material-backed decomposition checked for every object before totals are rounded."""
    name: str
    level: str
    total: str
    parts: list[str] = Field(min_length=1)
    rounding: Literal["exact", "cent"] = "exact"


class ReportPending(ReportTotal):
    """An explicitly named unallocated component; not a recipient of the allocated pool."""


class ReportDistribution(RuleItem):
    """Reconcile a material entitlement against actual allocated money and named pending items."""
    name: str
    level: str
    total: str
    pending: list[str] = []
    rounding: Literal["per_pool", "after_sum"] = "per_pool"


class Report(RuleItem):
    """结果文件的固定输出：汇总核算（按核算类型的总金额）和分配人员金额。
    totals：按顺序列出的各类总金额；people：最末层上表示“人”的字段或变量，按它把每人参与的各项合计成一行。"""
    totals: list[ReportTotal] = []
    people: str | None = None
    balances: list[ReportBalance] = []
    pending: list[ReportPending] = []
    distributions: list[ReportDistribution] = []
    details: list[ReportTotal] = []  # explicit per-object money reports, including auxiliary entities


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
    offsets: list[Offset] = []
    checks: list[Check] = []
    patches: list[DataPatch] = []        # 数据修订：计算前按顺序作用在数据上（只影响本方案）
    tests: list[GoldenCase] = []
    report: Report | None = None         # 结果文件的汇总核算和分配人员金额

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
