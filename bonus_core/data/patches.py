"""数据修订：计算前把补充表并入原表、按编号更正字段值。只作用在本方案的数据副本上，原始数据不变。

每一处改动都记成提示（数据修订），出处指向补充表的行，便于核对；有歧义的情况（同号内容不同、旧值不符、
找不到对象、同一字段改成不同的值）直接报错，不自行取舍。
"""
from __future__ import annotations

from bonus_core.data.table import ConvertError, Datasets, Row, StdTable, convert
from bonus_core.dsl.models import AppendRows, CorrectRows, RuleSet
from bonus_core.engine.issues import EngineError, Issue


def apply_patches(rs: RuleSet, data: Datasets) -> tuple[Datasets, list[Issue]]:
    if not rs.patches:
        return data, []
    out = dict(data)
    notes: list[Issue] = []
    for p in rs.patches:
        if isinstance(p, AppendRows):
            notes += _append(rs, out, p)
        else:
            notes += _correct(rs, out, p)
    return out, notes


def _same(a, b) -> bool:
    return a == b or (a is not None and b is not None and str(a) == str(b))


def _append(rs: RuleSet, data: Datasets, p: AppendRows) -> list[Issue]:
    tgt = rs.datasets[p.into]
    uniq = p.unique or tgt.key
    rows = list(data[p.into].rows)
    seen = {r.values.get(uniq): r for r in rows} if uniq else {}
    added = skipped = 0
    errs = []
    for r in data[p.source].rows:
        vals = {}
        for f, fd in tgt.fields.items():
            v = r.values.get(f)
            m = p.values.get(f)
            if m and v is not None and str(v) in m:
                try:
                    v = convert(m[str(v)], fd)
                except ConvertError as e:
                    errs.append(f"{r.src}：{f} 对照后的取值 {m[str(v)]!r} 不对：{e}")
                    continue
            vals[f] = v
        k = vals.get(uniq) if uniq else None
        if uniq and k is None:
            errs.append(f"{r.src}：没有 {uniq}，无法去重")
            continue
        old = seen.get(k) if uniq else None
        if old is not None:
            diff = [f for f in tgt.fields if not _same(old.values.get(f), vals.get(f))]
            if diff:
                errs.append(f"{uniq}={k} 在「{p.into}」（{old.src}）和「{p.source}」（{r.src}）里内容不同：{'、'.join(diff)}")
            else:
                skipped += 1
            continue
        row = Row(vals, r.src)
        rows.append(row)
        if uniq:
            seen[k] = row
        added += 1
    if errs:
        raise EngineError([Issue.block("patch_append", f"「{p.source}」并入「{p.into}」：{e}", f"patches.{p.source}") for e in errs[:10]])
    data[p.into] = StdTable(p.into, rows)
    return [Issue.warn("data_patch", f"数据修订：「{p.source}」{added} 行并入「{p.into}」" + (f"，{skipped} 行重复已跳过" if skipped else ""),
                       f"patches.{p.source}")]


def _correct(rs: RuleSet, data: Datasets, p: CorrectRows) -> list[Issue]:
    tgt = rs.datasets[p.target]
    rows = [Row(dict(r.values), r.src) for r in data[p.target].rows]
    index: dict[str, list[Row]] = {}
    for r in rows:
        index.setdefault(str(r.values.get(p.key)), []).append(r)
    done: dict[tuple[str, str], tuple] = {}
    errs, notes = [], []
    for s in data[p.source].rows:
        k = s.values.get(p.key)
        hits = index.get(str(k), []) if k is not None else []
        if len(hits) != 1:
            errs.append(f"{s.src}：{p.key}={k} 在「{p.target}」里" + ("找不到" if not hits else f"有 {len(hits)} 行，不知道改哪一行"))
            continue
        row = hits[0]
        if p.field:
            label = s.values.get(p.field)
            f = p.labels.get(str(label), str(label))
            pairs = [(f, s.values.get(p.value or ""))]
        else:
            pairs = [(f, s.values.get(col)) for f, col in p.set.items()]
        for f, raw in pairs:
            fd = tgt.fields.get(f)
            if fd is None:
                errs.append(f"{s.src}：「{p.target}」没有字段 {f}（更正字段的写法要在 labels 里对照到字段名）")
                continue
            try:
                new = convert(raw, fd)
            except ConvertError as e:
                errs.append(f"{s.src}：{f} 的新值 {raw!r} 不对：{e}")
                continue
            if f in p.expect:
                try:
                    want = convert(s.values.get(p.expect[f]), fd)
                except ConvertError as e:
                    errs.append(f"{s.src}：{f} 的旧值不对：{e}")
                    continue
                if (k, f) not in done and not _same(row.values.get(f), want):
                    errs.append(f"{s.src}：{p.key}={k} 的 {f} 原值是 {row.values.get(f)}，更正表写的旧值是 {want}，对不上")
                    continue
            if (k, f) in done:
                if not _same(done[(k, f)][0], new):
                    errs.append(f"{p.key}={k} 的 {f} 被改了两次且新值不同：{done[(k, f)][0]}（{done[(k, f)][1]}）、{new}（{s.src}）")
                elif f in p.expect:
                    errs.append(f"{p.key}={k} 的 {f} 重复修订（{done[(k, f)][1]}、{s.src}）")
                continue
            before = row.values.get(f)
            done[(k, f)] = (new, s.src)
            if not _same(before, new):
                row.values[f] = new
                notes.append(f"{k} {f}：{before} → {new}（{s.src}）")
    if errs:
        raise EngineError([Issue.block("patch_correct", f"「{p.source}」更正「{p.target}」：{e}", f"patches.{p.source}") for e in errs[:10]])
    data[p.target] = StdTable(p.target, rows)
    return [Issue.warn("data_patch", f"数据修订：「{p.source}」更正「{p.target}」{len(notes)} 处" + ("：" + "；".join(notes[:12]) if notes else "（都与原值相同）"),
                       f"patches.{p.source}")]
