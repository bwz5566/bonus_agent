"""Explicit entity populations derived from effective detail records."""
from __future__ import annotations

from bonus_core.data.table import StdTable


def prepare(rs, data):
    """Patches must precede population derivation, including in independent probes."""
    from bonus_core.data.patches import apply_patches
    patched, issues = apply_patches(rs, data)
    return materialize(rs, patched), issues


def materialize(rs, data):
    out, done, path = dict(data), set(), set()

    def visit(name):
        if name in done:
            return
        if name in path:
            raise ValueError("对象范围 population_from 循环引用")
        path.add(name)
        spec = rs.datasets[name]
        source = spec.population_from
        if source:
            if source not in rs.datasets or not spec.key or rs.datasets[source].link != spec.key:
                raise ValueError(f"{name} 的 population_from 必须引用按 {spec.key} 关联的明细表")
            visit(source)
            if source not in out or name not in out:
                raise ValueError(f"{name} 的对象范围缺少输入表 {source}")
            keys = {r.values.get(spec.key) for r in out[source].rows}
            out[name] = StdTable(name, [r for r in out[name].rows if r.values.get(spec.key) in keys])
        path.remove(name)
        done.add(name)

    for name in rs.datasets:
        visit(name)
    return out


def gaps(rs, data, mapping):
    """Same-source entities and their filtered detail must not silently disagree."""
    out = []
    for name, spec in rs.datasets.items():
        own = mapping.get(name)
        if not spec.key or spec.population_from or own is None or name not in data:
            continue
        for source, detail in rs.datasets.items():
            peer = mapping.get(source)
            if source == name or detail.link != spec.key or peer is None or source not in data:
                continue
            own_sources = {(r.src.file, r.src.sheet) for r in data[name].rows if r.src}
            peer_sources = {(r.src.file, r.src.sheet) for r in data[source].rows if r.src}
            if not own_sources or own_sources != peer_sources or not peer.filter:
                continue
            own_key, peer_key = (own.combine or {}).get(spec.key), (peer.combine or {}).get(spec.key)
            if own_key != peer_key or (not own_key and own.columns.get(spec.key) != peer.columns.get(spec.key)):
                continue
            extra = {r.values.get(spec.key) for r in data[name].rows} - {r.values.get(spec.key) for r in data[source].rows}
            if extra:
                out.append((name, f"同源实体「{name}」比过滤后的明细「{source}」多 {len(extra)} 个对象；"
                            f"按有效明细建对象时设置 population_from: {source}，或统一筛选；"
                            "材料确实要求保留无有效明细的对象时，需写明依据并单独核对"))
    return out
