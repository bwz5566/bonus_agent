"""按分配层级把标准表组织成实体树：全部 -> 部门 -> 小组 -> 个人 ...

关联实体（项目、销售单等 key 不是层级主键的表）也建成节点，挂在上级对象下面（aux_children），
不参与分配；它们按编号被其他表的行引用（fk_rows），见 bonus_core.dsl.scope。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace
from decimal import Decimal

from bonus_core.data.table import Datasets, Row
from bonus_core.dsl.models import RuleSet
from bonus_core.dsl.scope import Layout
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
    fixed: Decimal | None = None        # 奖金池按层计算时，该层及以上对象的金额由规则直接算出
    # 关联实体
    aux: bool = False
    row: Row | None = None              # 关联实体自己的那一行
    aux_children: list["Node"] = field(default_factory=list)
    fk_rows: dict[str, list[Row]] = field(default_factory=dict)   # 其他表中引用本实体编号的行
    index: dict | None = None           # 只在根节点上：{关联实体表: {编号: 节点}}
    _rows_cache: dict = field(default_factory=dict, repr=False)

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
        """当前节点下可聚合的行。层级对象：挂在自己及下级（含下面的关联实体）的行；
        关联实体：引用了它的编号的所有行（含它下面的关联实体的行）。"""
        if dataset not in self._rows_cache:
            if self.aux:
                seen, out = set(), []
                for r in self._aux_rows(dataset):
                    if id(r) not in seen:
                        seen.add(id(r))
                        out.append(r)
            else:
                out = self._home_rows(dataset)
            self._rows_cache[dataset] = out
        return self._rows_cache[dataset]

    def _home_rows(self, dataset: str) -> list[Row]:
        out = list(self.own_rows.get(dataset, []))
        for c in self.children:
            out.extend(c._home_rows(dataset))
        for a in self.aux_children:
            out.extend(a._home_rows(dataset))
        return out

    def _aux_rows(self, dataset: str):
        yield from self.own_rows.get(dataset, [])
        yield from self.fk_rows.get(dataset, [])
        for a in self.aux_children:
            yield from a._aux_rows(dataset)

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    @property
    def label(self) -> str:
        name = self.attrs.get("name") or next((v for k, v in self.attrs.items() if k.endswith("_name") and v), None)
        return str(name or self.key or self.id)


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
    listed: set[str] = set()      # 有实体表的层级：上级编号必须在实体表里，不能由下级推导
    for lv in levels:
        ent = rs.entity_dataset(lv.key)
        if ent is None:
            continue
        listed.add(lv.level)
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
            n.own_rows.setdefault(name, []).append(row)   # 上层可以对实体表聚合

    # 2. 自下而上连父节点；没有实体表的上层由下层的 parent_key 推导出来
    for i in range(len(levels) - 1, 0, -1):
        lv, up = levels[i], levels[i - 1]
        ent = rs.entity_dataset(lv.key)
        skip = bool(ent and ent[1].unmatched == "ignore")       # 财务确认过“上级里找不到的不参与”
        unknown: list[tuple[str, str, str]] = []
        for k, n in list(nodes[lv.level].items()):
            pk = n.attrs.get(lv.parent_key) if lv.parent_key else None
            if pk is None:
                issues.append(Issue.block(
                    "parent_missing", f"{n.id} 缺少上级字段 {lv.parent_key}，无法归入「{up.level}」",
                    where=str(n.attr_src)))
                continue
            if up.level in listed and str(pk) not in nodes[up.level]:
                unknown.append((k, str(pk), str(n.attr_src)))
                if skip:
                    del nodes[lv.level][k]
                continue
            p = get(up.level, str(pk))
            n.parent = p
            p.children.append(n)
        if unknown:
            ex = "；".join(f"{lv.level}:{k} 的 {lv.parent_key}={pk}（{src}）" for k, pk, src in unknown[:5])
            have = list(nodes[up.level])[:3]
            if skip:
                issues.append(Issue.warn("parent_unknown_skipped", f"「{lv.level}」有 {len(unknown)} 个对象在「{up.level}」里找不到上级，"
                                         f"按 unmatched: ignore 不参与分配：{ex}", where=unknown[0][2]))
            else:
                issues.append(Issue.block(
                    "parent_unknown", f"「{lv.level}」有 {len(unknown)} 个对象的上级在「{up.level}」的实体表里不存在，如 {ex}。"
                    f"「{up.level}」的编号如 {have}。先查两边编号的拼法是否一致（合成编号的列、同名列用的是哪一列）、"
                    "期间是否一致（这张表要不要也写 period 只取本期）；确实是材料或数据里就对不上的，把这几行列出来 ask_question 问财务，"
                    "财务答复不参与后在这张实体表写 unmatched: ignore", where=unknown[0][2]))
    for n in nodes[levels[0].level].values():
        n.parent = root
        root.children.append(n)

    # 3. 关联实体（项目、销售单……）：建节点，挂到上级对象下面
    lay = Layout(rs)
    index: dict[str, dict[str, Node]] = {}
    for name in _aux_order(lay):
        if name not in data:
            issues.append(Issue.block("dataset_missing", f"缺少数据表「{name}」", where=name))
            continue
        key = rs.datasets[name].key
        tbl = index.setdefault(name, {})
        for row in data[name].rows:
            k = row.values.get(key)
            if k is None:
                issues.append(Issue.block("key_empty", f"「{name}」主键 {key} 为空", where=str(row.src)))
                continue
            if str(k) in tbl:
                issues.append(Issue.block("key_dup", f"「{name}」主键 {key}={k} 重复", where=str(row.src)))
                continue
            n = Node(node_id(name, k), name, str(k), aux=True, row=row, attrs=dict(row.values), attr_src=row.src)
            n.own_rows[name] = [row]
            tbl[str(k)] = n
            parent = _parent_node(lay, name, row, root, nodes, index, issues)
            if parent is not None:
                n.parent = parent
                parent.aux_children.append(n)
    root.index = index
    data = _canonical_refs(rs, lay, data, nodes, index, issues)

    # 4. 明细表：按 link 字段挂到对应层级的节点或关联实体上
    for name, ds in rs.datasets.items():
        if ds.key is not None:
            continue
        if name not in data:
            issues.append(Issue.block("dataset_missing", f"缺少数据表「{name}」", where=name))
            continue
        if lay.parent[name][0] == "bad":
            issues.append(Issue.block("link_bad", f"「{name}」的 link 字段 {ds.link} 不是层级主键，也不是关联实体的主键",
                                      where=name))
            continue
        dropped: list[str] = []
        for row in data[name].rows:
            n = _parent_node(lay, name, row, root, nodes, index, issues, dropped if ds.unmatched == "ignore" else None)
            if n is not None:
                n.own_rows.setdefault(name, []).append(row)
        if dropped:
            keys = list(dict.fromkeys(dropped))
            issues.append(Issue.warn("link_ignored", f"「{name}」有 {len(dropped)} 行的 {ds.link} 在上级里找不到，按 unmatched: ignore "
                                     f"不参与计算（{len(keys)} 个编号，如 {'、'.join(keys[:6])}）", where=name))

    # 5. 编号引用：每张表中带有关联实体编号的行，记到被引用的实体上
    for name, ds in rs.datasets.items():
        if name not in data:
            continue
        for f in ds.fields:
            target = lay.aux_by_key.get(f)
            if target is None or f == ds.key or target not in index:
                continue
            missing = []
            for row in data[name].rows:
                v = row.values.get(f)
                n = index[target].get(str(v)) if v is not None else None
                if n is None:
                    if v is not None:
                        missing.append(f"{v}（{row.src}）")
                    continue
                n.fk_rows.setdefault(name, []).append(row)
            if missing and lay.parent[name][2] != f:      # 挂靠字段找不到的已在上面报过
                issues.append(Issue.warn("ref_unmatched", f"「{name}」中 {f} 有 {len(missing)} 个编号在「{target}」中找不到："
                                         + "、".join(missing[:5]), where=name))

    # 稳定排序，保证结果可复现
    for n in root.walk():
        n.children.sort(key=lambda c: c.key)
    for tbl in index.values():
        for n in tbl.values():
            n.aux_children.sort(key=lambda c: (c.level, c.key))
    for n in root.walk():
        n.aux_children.sort(key=lambda c: (c.level, c.key))
    by_level = {lv.level: sorted(nodes[lv.level].values(), key=lambda n: n.key) for lv in levels}
    return root, by_level, issues


def _norm_id(v) -> str:
    """编号的宽松写法：全角半角、大小写、前后和中间多余的空格不算不同（Excel 的 VLOOKUP 也是这样对的）。"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(v))).strip().casefold()


def _canonical_refs(rs: RuleSet, lay: Layout, data: Datasets, nodes, index, issues: list[Issue]) -> Datasets:
    """明细表的 link 编号、其他表引用关联实体的编号，和上级的编号只差大小写 / 全半角 / 空格、而且宽松写法下只对上唯一一个时，
    按上级的写法对上，并提示改了哪些（不改的话会被当成找不到，客户系数之类悄悄取了默认值）。不改原数据，返回换过行的副本。"""
    jobs: list[tuple[str, str, dict]] = []
    for name, ds in rs.datasets.items():
        if name not in data:
            continue
        if ds.key is None and lay.parent.get(name, ("bad",))[0] in ("level", "aux"):
            kind, target, f = lay.parent[name]
            keys = nodes[lay.rs.allocation[target].level] if kind == "level" else index.get(target, {})
            jobs.append((name, f, keys))
        for f in ds.fields:
            target = lay.aux_by_key.get(f)
            if target is not None and f != ds.key and target in index:
                jobs.append((name, f, index[target]))
    out = dict(data)
    for name, f, keys in jobs:
        wide: dict[str, list[str]] = {}
        for k in keys:
            wide.setdefault(_norm_id(k), []).append(k)
        fixed: dict[str, str] = {}
        rows = []
        for r in out[name].rows:
            v = r.values.get(f)
            if v is not None and str(v) not in keys:
                hit = wide.get(_norm_id(v), [])
                if len(hit) == 1:
                    fixed[str(v)] = hit[0]
                    r = replace(r, values={**r.values, f: hit[0]})
            rows.append(r)
        if fixed:
            out[name] = replace(out[name], rows=rows)
            issues.append(Issue.warn("link_normalized", f"「{name}」中 {f} 有 {len(fixed)} 种写法只差大小写 / 全半角 / 空格，"
                                     "已按上级的写法对上：" + "、".join(f"{a}→{b}" for a, b in list(fixed.items())[:6]), where=name))
    return out


def _aux_order(lay: Layout) -> list[str]:
    """关联实体按依赖顺序建：上级是另一个关联实体的，排在它后面。"""
    out, busy = [], set()

    def visit(n):
        if n in out or n in busy:
            return
        busy.add(n)
        kind, target, _ = lay.parent[n]
        if kind == "aux":
            visit(target)
        out.append(n)
    for n in lay.aux:
        visit(n)
    return out


_LOOSE_CACHE: dict = {}
_SEP = re.compile(r"[|\s_\-/]+")


def _loose_get(pool: dict, k: str):
    """编号只差分隔符（一边是 202609|C001|甲，一边是 202609C001甲）时按去掉分隔符后的写法找；去掉后撞车的不算。"""
    if not pool:
        return None
    key = id(pool)
    hit = _LOOSE_CACHE.get(key)
    if hit is None or hit[0] is not pool:
        m, dup = {}, set()
        for kk, n in pool.items():
            x = _SEP.sub("", str(kk))
            if x in m:
                dup.add(x)
            m[x] = n
        for x in dup:
            m.pop(x, None)
        hit = (pool, m)
        if len(_LOOSE_CACHE) > 200:
            _LOOSE_CACHE.clear()
        _LOOSE_CACHE[key] = hit
    return hit[1].get(_SEP.sub("", k))


def _parent_node(lay: Layout, name: str, row: Row, root: Node, nodes, index, issues, dropped: list | None = None) -> Node | None:
    kind, target, f = lay.parent[name]
    if kind == "root":
        return root
    k = row.values.get(f)
    if kind == "level":
        level = lay.rs.allocation[target].level
        pool = nodes[level]
    else:
        level = target
        pool = index.get(target, {})
    n = pool.get(str(k)) if k is not None else None
    if n is None and k is not None:
        n = _loose_get(pool, str(k))
        if n is not None and not any(i.code == "key_separator" and i.message.startswith(f"「{name}」") for i in issues):
            issues.append(Issue.warn("key_separator", f"「{name}」的 {f} 和「{level}」的编号只差分隔符（如 {k} ↔ {n.key or n.id}），"
                                     "已按去掉分隔符后的写法对上", where=str(row.src)))
    if n is None:
        if dropped is not None:        # 财务确认过查不到的不参与：不阻断，汇总成一条提示
            dropped.append(str(k))
            return None
        issues.append(Issue.block("link_unmatched", f"「{name}」中 {f}={k} 在「{level}」中找不到", where=str(row.src)))
    return n
