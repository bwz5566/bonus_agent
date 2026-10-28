"""白名单函数的签名，供静态校验和提示词使用。实现在 engine/evaluator.py。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FnSpec:
    min_args: int
    max_args: int
    kwargs: frozenset = frozenset()
    aggregate: bool = False   # 参数在明细行上逐行求值
    doc: str = ""


AGG_KW = frozenset({"where", "of"})

FUNCTIONS: dict[str, FnSpec] = {
    "sum": FnSpec(1, 1, AGG_KW, True, "对当前节点下的明细行求和，where 为筛选条件，of 指定明细表名"),
    "avg": FnSpec(1, 1, AGG_KW, True, "明细行平均值"),
    "count": FnSpec(0, 0, AGG_KW, True, "明细行计数"),
    "count_distinct": FnSpec(1, 1, AGG_KW, True, "明细行中不同取值的个数，如项目数"),
    "min": FnSpec(1, 9, AGG_KW, True, "一个参数时为明细行最小值，多个参数时为取最小"),
    "max": FnSpec(1, 9, AGG_KW, True, "一个参数时为明细行最大值，多个参数时为取最大"),
    "tier": FnSpec(2, 2, doc="tier(阶梯表, x) 直接返回奖金金额：whole=x×所在档比例；excess=各档分段累加"),
    "tier_rate": FnSpec(2, 2, doc="tier_rate(阶梯表, x) 只返回所在档的比例或系数"),
    "lookup": FnSpec(2, 2, doc="lookup(查找表, 键) 查表，无匹配且无 default 时报错"),
    "if": FnSpec(3, 3, doc="if(条件, 真值, 假值)"),
    "ratio": FnSpec(2, 2, frozenset({"zero"}), doc="ratio(a, b, zero: v) = a/b，b 为 0 时返回 v，未给 v 则报错"),
    "share": FnSpec(1, 1, doc="share(x) = 本节点 x / 同级所有节点 x 之和"),
    "minmax": FnSpec(1, 1, doc="minmax(x) = (x - 同级最小) / (同级最大 - 同级最小)"),
    "round": FnSpec(1, 2, doc="round(x, 位数=2) 四舍五入"),
    "clamp": FnSpec(3, 3, doc="clamp(x, 下限, 上限)"),
    "abs": FnSpec(1, 1, doc="绝对值"),
}


def describe() -> str:
    return "\n".join(f"- {n}: {s.doc}" for n, s in FUNCTIONS.items())
