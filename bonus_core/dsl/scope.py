"""层级与数据表的对应关系，求值器和静态校验共用，保证两边的判断一致。

三类数据表：
- 实体表（有 key）：每行一个某层级的实体，字段在该层及以下可直接使用，也可在上层聚合（如 sum(base_salary)）；
- 明细表（有 link）：每行挂到某层级的一个实体上，字段只能在聚合里使用；
- 公司级表（key、link 都没有）：行挂在“全部”上，字段只能在 global 作用域聚合（如全年各月净利润）。
"""
from __future__ import annotations

from bonus_core.dsl.models import RuleSet
from bonus_core.dsl.parser import Call, names

AGGREGATES = {"sum", "avg", "count", "count_distinct", "min", "max"}


def is_aggregate(c: Call) -> bool:
    """min / max 带多个参数时是普通函数（取较小/较大值），只有一个参数时是聚合。"""
    return c.fn in AGGREGATES and not (c.fn in ("min", "max") and len(c.args) > 1)


class Layout:
    def __init__(self, rs: RuleSet):
        self.rs = rs
        self.index = {lv.level: i for i, lv in enumerate(rs.allocation)}
        key_level = {lv.key: i for i, lv in enumerate(rs.allocation)}
        self.table_level: dict[str, int] = {}     # 数据表 -> 行挂靠的层级下标（-1 为“全部”）
        self.row_only: dict[str, set[str]] = {}   # 只能在聚合里用的字段 -> 所在的明细表/公司级表
        self.entity_fields: dict[str, set[str]] = {}
        self.entity_level: dict[str, int] = {}    # 实体字段 -> 最上层的所属层级
        keys = {d.link for d in rs.datasets.values() if d.link} | {d.key for d in rs.datasets.values() if d.key}
        for n, d in rs.datasets.items():
            if d.key:
                lvl = key_level.get(d.key)
                if lvl is None:
                    continue
                self.table_level[n] = lvl
                for f in d.fields:
                    self.entity_fields.setdefault(f, set()).add(n)
                    self.entity_level[f] = min(self.entity_level.get(f, lvl), lvl)
            else:
                self.table_level[n] = key_level.get(d.link, len(rs.allocation) - 1) if d.link else -1
                for f in d.fields:
                    if f not in keys:
                        self.row_only.setdefault(f, set()).add(n)

    def level_label(self, i: int) -> str:
        return "global（全部）" if i < 0 else self.rs.allocation[i].level

    def agg_dataset(self, c: Call) -> tuple[str | None, str]:
        """推断聚合函数作用在哪张表上，返回 (表名, 错误信息)。"""
        kw = c.kw()
        if "of" in kw:
            of = kw["of"]
            name = getattr(of, "value", None)
            if name not in self.table_level:
                return None, f"{c.fn} 的 of 参数必须是数据表名字符串"
            return name, ""
        refs = set()
        for a in list(c.args) + [v for k, v in c.kwargs if k == "where"]:
            refs |= names(a)
        for pool in (self.row_only, self.entity_fields):
            cands = None
            for n in refs:
                if n in pool:
                    cands = set(pool[n]) if cands is None else cands & pool[n]
            if cands is not None:
                if len(cands) == 1:
                    return next(iter(cands)), ""
                if not cands:
                    return None, f"{c.fn}(...) 引用的字段不在同一张表里，无法一起聚合"
                return None, f"{c.fn}(...) 引用的字段同时存在于 {sorted(cands)}，请用 of: \"表名\" 指定"
        detail = [n for n, d in self.rs.datasets.items() if not d.key]
        if len(detail) == 1:
            return detail[0], ""
        return None, f"{c.fn}(...) 无法确定聚合哪张表，请用 of: \"表名\" 指定"
