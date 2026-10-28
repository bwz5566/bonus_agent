"""按分配层级把标准表组织成实体树：全部 -> 部门 -> 小组 -> 个人 ..."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from bonus_core.data.table import Datasets, Row
from bonus_core.dsl.models import RuleSet
from bonus_core.engine.issues import Issue

ROOT_LEVEL = "全部"


@dataclass(eq=False)
class Node:
    id: str
    level: str
    key: str | None
    parent: "Node | None" = None
    children: list["Node"] = field(default_factory=list)
    attrs: dict = field(default_factory=dict)
    attr_src: object = None
    own_rows: dict[str, list[Row]] = field(default_factory=dict)
    # 分配过程中的状态
    weight: Decimal = Decimal(0)
    exact: Decimal = Decimal(0)
    final: Decimal = Decimal(0)
    lock: Decimal | None = None

    @property
    def locked(self) -> bool:
        return self.lock is not None

    def chain(self):
        """自己及所有祖先，由近到远。"""
        n = self
        while n is not None:
            yield n
            n = n.parent

    def rows(self, dataset: str) -> list[Row]:
        out = list(self.own_rows.get(dataset, []))
        for c in self.children:
            out.extend(c.rows(dataset))
        return out

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    @property
    def label(self) -> str:
        return str(self.attrs.get("name") or self.key or self.id)


def node_id(level: str, key) -> str:
    return f"{level}:{key}"


def build_tree(rs: RuleSet, data: Datasets) -> tuple[Node, dict[str, list[Node]], list[Issue]]:
    issues: list[Issue] = []
    root = Node(ROOT_LEVEL, ROOT_LEVEL, None)
    levels = rs.allocation
    nodes: dict[str, dict[str, Node]] = {lv.level: {} for lv in levels}

    def get(level, key) -> Node:
        tbl = nodes[level]
        if key not in tbl:
            tbl[key] = Node(node_id(level, key), level, key)
        return tbl[key]

    # 1. 实体表：建立节点并带上属性
    for lv in levels:
        ent = rs.entity_dataset(lv.key)
        if ent is None:
            continue
        name, _ = ent
        if name not in data:
            issues.append(Issue.block("dataset_missing", f"缺少数据表「{name}」", where=name))
            continue
        seen = set()
        for row in data[name].rows:
            k = row.values.get(lv.key)
            if k is None:
                issues.append(Issue.block("key_empty", f"「{name}」主键 {lv.key} 为空", where=str(row.src)))
                continue
            k = str(k)
            if k in seen:
                issues.append(Issue.block("key_dup", f"「{name}」主键 {lv.key}={k} 重复", where=str(row.src)))
                continue
            seen.add(k)
            n = get(lv.level, k)
            n.attrs, n.attr_src = dict(row.values), row.src

    # 2. 自下而上连父节点；没有实体表的上层由下层的 parent_key 推导出来
    for i in range(len(levels) - 1, 0, -1):
        lv, up = levels[i], levels[i - 1]
        for k, n in nodes[lv.level].items():
            pk = n.attrs.get(lv.parent_key) if lv.parent_key else None
            if pk is None:
                issues.append(Issue.block(
                    "parent_missing", f"{n.id} 缺少上级字段 {lv.parent_key}，无法归入「{up.level}」",
                    where=str(n.attr_src)))
                continue
            p = get(up.level, str(pk))
            n.parent = p
            p.children.append(n)
    for n in nodes[levels[0].level].values():
        n.parent = root
        root.children.append(n)

    # 3. 明细表：按 link 字段挂到对应层级的节点上
    level_by_key = {lv.key: lv.level for lv in levels}
    for name, ds in rs.datasets.items():
        if ds.link is None:
            continue
        if name not in data:
            issues.append(Issue.block("dataset_missing", f"缺少数据表「{name}」", where=name))
            continue
        level = level_by_key.get(ds.link)
        if level is None:
            issues.append(Issue.block("link_bad", f"「{name}」的 link 字段 {ds.link} 不是任何层级的主键", where=name))
            continue
        for row in data[name].rows:
            k = row.values.get(ds.link)
            n = nodes[level].get(str(k)) if k is not None else None
            if n is None:
                issues.append(Issue.block(
                    "link_unmatched", f"「{name}」中 {ds.link}={k} 在「{level}」中找不到", where=str(row.src)))
                continue
            n.own_rows.setdefault(name, []).append(row)

    # 稳定排序，保证结果可复现
    for n in root.walk():
        n.children.sort(key=lambda c: c.key)
    by_level = {lv.level: sorted(nodes[lv.level].values(), key=lambda n: n.key) for lv in levels}
    return root, by_level, issues
