"""层级与数据表的对应关系，求值器和静态校验共用，保证两边的判断一致。

四类数据表：
- 层级实体表（key 是某个分配层级的主键）：每行一个该层级的对象，字段在该层及以下可直接使用，也可在上层聚合；
- 关联实体表（key 不是层级主键，如项目、销售单）：每行一个对象，挂在上级对象下面（层级对象或另一个关联实体）。
  它的字段和变量（scope 写表名）可以在三个地方使用：该表的聚合里、该实体作用域的变量里、
  以及带有它的编号字段的其他表的行里（如项目参与表的行通过 project_id 取到项目的 region 和项目层变量）；
- 明细表（只有 link）：每行挂到某层级对象或某个关联实体上，字段只能在聚合里使用；
- 公司级表（key、link 都没有）：行挂在“全部”上，字段只能在 global 作用域聚合。
"""
from __future__ import annotations

from bonus_core.dsl.models import RuleSet
from bonus_core.dsl.parser import Call, Name, names, walk

AGGREGATES = {"sum", "avg", "count", "count_distinct", "min", "max"}


def is_aggregate(c: Call) -> bool:
    """min / max 带多个参数时是普通函数（取较小/较大值），只有一个参数时是聚合。"""
    return c.fn in AGGREGATES and not (c.fn in ("min", "max") and len(c.args) > 1)


class Layout:
    def __init__(self, rs: RuleSet):
        self.rs = rs
        self.index = {lv.level: i for i, lv in enumerate(rs.allocation)}
        self.key_level = {lv.key: i for i, lv in enumerate(rs.allocation)}
        ds = rs.datasets
        # 关联实体：有 key，但 key 不是层级主键
        self.aux = {n: d for n, d in ds.items() if d.key and d.key not in self.key_level}
        self.aux_by_key = {d.key: n for n, d in self.aux.items()}
        # 每张表通过编号字段直接引用的关联实体（不含自己的主键）
        self.refs = {n: [self.aux_by_key[f] for f in d.fields if f in self.aux_by_key and f != d.key]
                     for n, d in ds.items()}
        self.parent: dict[str, tuple[str, object, str | None]] = {}   # 表 -> (level|aux|root|bad, 层级下标或表名, 连接字段)
        for n, d in ds.items():
            if d.key and d.key in self.key_level:
                self.parent[n] = ("level", self.key_level[d.key], d.key)
            else:
                self.parent[n] = self._parent_of(n, d)
        self.table_level = {n: self._home(n, set()) for n in ds}
        keys = {d.link for d in ds.values() if d.link} | {d.key for d in ds.values() if d.key}
        self.row_only: dict[str, set[str]] = {}     # 只能在聚合或行里用的字段 -> 所在的表（不含层级实体表）
        self.entity_fields: dict[str, set[str]] = {}
        self.entity_level: dict[str, int] = {}      # 层级实体字段 -> 最上层的所属层级
        for n, d in ds.items():
            for f in d.fields:
                if d.key and d.key in self.key_level:
                    lvl = self.key_level[d.key]
                    self.entity_fields.setdefault(f, set()).add(n)
                    self.entity_level[f] = min(self.entity_level.get(f, lvl), lvl)
                elif f not in keys:
                    self.row_only.setdefault(f, set()).add(n)

    def _parent_of(self, n: str, d) -> tuple[str, object, str | None]:
        if d.link:
            if d.link in self.key_level:
                return ("level", self.key_level[d.link], d.link)
            if d.link in self.aux_by_key and self.aux_by_key[d.link] != n:
                return ("aux", self.aux_by_key[d.link], d.link)
            return ("bad", None, d.link)
        if not d.key:
            return ("root", None, None)
        lv = [(self.key_level[f], f) for f in d.fields if f in self.key_level]
        if lv:
            i, f = max(lv)
            return ("level", i, f)
        for f in d.fields:
            if f in self.aux_by_key and f != d.key:
                return ("aux", self.aux_by_key[f], f)
        return ("root", None, None)

    def _home(self, n: str, busy: set) -> int:
        kind, target, _ = self.parent[n]
        if kind == "level":
            return target
        if kind == "aux" and target not in busy:
            return self._home(target, busy | {n})
        return -1

    def reach(self, n: str) -> set[str]:
        """从表 n 的一行出发，经编号字段能取到的关联实体（传递）。"""
        out, todo = set(), list(self.refs.get(n, []))
        while todo:
            a = todo.pop()
            if a not in out:
                out.add(a)
                todo += self.refs.get(a, [])
        return out

    def level_label(self, i: int) -> str:
        return "global（全部）" if i < 0 else self.rs.allocation[i].level

    def aux_var_scope(self, name: str) -> str | None:
        v = self.rs.variables.get(name)
        return v.scope if v is not None and v.scope in self.aux else None

    def visible(self, n: str, table: str) -> bool:
        """在表 table 的一行里能否用到名字 n（本表字段、经编号取到的实体字段或实体变量）。"""
        d = self.rs.datasets[table]
        if n in d.fields:
            return True
        reach = self.reach(table) | ({table} if table in self.aux else set())
        sc = self.aux_var_scope(n)
        if sc is not None:
            return sc in reach
        return any(n in self.rs.datasets[a].fields for a in reach)

    def agg_dataset(self, c: Call) -> tuple[str | None, str]:
        """推断聚合函数作用在哪张表上，返回 (表名, 错误信息)。"""
        kw = c.kw()
        if "of" in kw:
            of = kw["of"]
            name = getattr(of, "value", None)
            if name not in self.rs.datasets:
                return None, (f"{c.fn} 的 of 参数必须是数据表名字符串（写的是 {name if name is not None else '非字符串'}；"
                              f"可用：{'、'.join(self.rs.datasets)}）")
            return name, ""
        refs = set()
        for a in list(c.args) + [v for k, v in c.kwargs if k == "where"]:
            refs |= names(a) - _inner_agg_names(a)
        fields = {n for n in refs if n in self.rs.field_owner()}
        auxv = {n for n in refs if self.aux_var_scope(n)}
        if not fields and not auxv:
            detail = [n for n, d in self.rs.datasets.items() if not d.key]
            if len(detail) == 1:
                return detail[0], ""
            return None, f"{c.fn}(...) 无法确定聚合哪张表，请用 of: \"表名\" 指定"
        cands = [t for t in self.rs.datasets if all(self.visible(n, t) for n in fields | auxv)]
        if not cands:
            return None, (f"{c.fn}(...) 引用的 {sorted(fields | auxv)} 不在同一张表里，也不能通过编号字段关联到一起，"
                          f"无法一起聚合")

        def score(t):   # 直接包含的字段 / 本表变量越多越优先；同分时明细表、关联实体优先于层级实体表
            d = self.rs.datasets[t]
            direct = sum(1 for n in fields if n in d.fields) + sum(1 for n in auxv if self.aux_var_scope(n) == t)
            return direct, 0 if (d.key and d.key in self.key_level) else 1
        best = max(score(t) for t in cands)
        top = [t for t in cands if score(t) == best]
        if len(top) == 1:
            return top[0], ""
        return None, f"{c.fn}(...) 可以作用在 {sorted(top)} 上，请用 of: \"表名\" 指定"


def _inner_agg_names(node) -> set[str]:
    """嵌套聚合里面的名字属于内层聚合，不参与外层推断。"""
    out = set()
    for n in walk(node):
        if n is not node and isinstance(n, Call) and is_aggregate(n):
            out |= {x.id for x in walk(n) if isinstance(x, Name)}
    return out
