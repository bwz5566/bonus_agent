"""Source-derived review evidence, without production formulas or monetary rows.

Counts scan all detected rows. Mapping counts precede population patches and
tree attachment; they do not claim that every mapped row earns a bonus.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from bonus_core.data.mapping import apply_mapping, rebind_dup_columns, row_filter
from bonus_core.data.table import to_date
from bonus_core.data.table_detect import table_at
from bonus_core.dsl.models import RuleSet

PROFILE_SCHEMA = 1
VALUE_LIMIT = 12
ROW_LIMIT = 2
_IDENTITY = re.compile(r'(^|_)(id|name|person|employee|staff)(_|$)|姓名|人名|人员|编码|编号|客户.*(?:名称|简称)', re.I)
_TIME_FIELD = re.compile(r'(^|_)(date|month|period|day)(_|$)|日期|年月|月份', re.I)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _zero(value):
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        return False
    try:
        return Decimal(str(value).strip().rstrip('%')) == 0
    except (InvalidOperation, ValueError):
        return False


def _stats(rows, *, values=False, limit=VALUE_LIMIT):
    """Every row contributes to counts; only displayed categories/references are bounded."""
    counts, refs = Counter(), {}
    nulls, blanks, zeros = [], [], []
    for row, value in rows:
        if value is None:
            nulls.append(row)
        elif isinstance(value, str) and not value.strip():
            blanks.append(row)
        else:
            token = (type(value).__name__, str(value))
            counts[token] += 1
            refs.setdefault(token, [])
            if len(refs[token]) < ROW_LIMIT:
                refs[token].append(row)
            if _zero(value):
                zeros.append(row)
    out = {'status': 'known', 'rows': len(rows), 'null': len(nulls), 'blank': len(blanks),
           'zero': len(zeros), 'distinct_nonblank': len(counts), 'scan_complete': True,
           'null_rows': nulls[:ROW_LIMIT], 'blank_rows': blanks[:ROW_LIMIT]}
    if values:
        ordered = sorted(counts, key=lambda k: (-counts[k], k))
        out.update(values=[{'value': k[1], 'type': k[0], 'count': counts[k], 'rows': refs[k]} for k in ordered[:limit]],
                   values_complete=len(ordered) <= limit, values_truncated=len(ordered) > limit,
                   omitted_values=max(0, len(ordered)-limit))
    return out


def _unknown(reason):
    return {'status': 'unknown', 'reason': reason, 'scan_complete': False}


def _categorical(field, definition, column):
    kind = getattr(definition, 'type', None)
    return kind in ('text', 'enum', 'bool') and not _IDENTITY.search(field + ' ' + column)


def _date_range(rows):
    dates, invalid = [], 0
    for _, value in rows:
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        try:
            dates.append(to_date(value))
        except ValueError:
            invalid += 1
    return {'status': 'known' if dates and not invalid else 'unknown',
            'min': min(dates).isoformat() if dates else None,
            'max': max(dates).isoformat() if dates else None, 'unparsed': invalid,
            'note': '只统计实际字段值；年月按首日呈现，不推断无日期表的业务时点'}


def _relations(datasets, normalized, allocations=()):
    """Exact mapped-key coverage is diagnostic evidence, not a population policy."""
    owners = {}
    for name, definition in datasets.items():
        if getattr(definition, 'key', None):
            owners.setdefault(definition.key, []).append(name)
    edges = {(child, definition.link, definition.link, tuple(owners.get(definition.link, [])), 'dataset.link')
             for child, definition in datasets.items() if getattr(definition, 'link', None)}
    for levels in allocations:
        for parent_level, child_level in zip(levels, levels[1:]):
            child_owners = owners.get(child_level.get('key'), [])
            if len(child_owners) == 1 and child_level.get('parent_key'):
                edges.add((child_owners[0], child_level['parent_key'], parent_level.get('key'),
                           tuple(owners.get(parent_level.get('key'), [])), 'allocation.parent_key'))
    results = []
    token = lambda v: (type(v).__name__, str(v))
    for child, field, parent_field, parents, declared_by in sorted(edges):
        item = {'child': child, 'field': field, 'parent_field': parent_field,
                'parent_candidates': list(parents), 'declared_by': declared_by,
                'comparison': 'exact_mapped_values；不做别名合并，不裁行，不决定核算对象范围'}
        results.append(item)
        if len(parents) != 1 or child not in normalized or parents[0] not in normalized:
            item.update(_unknown('未取得唯一关系两侧的映射数据'))
            continue
        parent = parents[0]
        p = {token(r.values[parent_field]) for r in normalized[parent].rows if r.values.get(parent_field) is not None}
        c = {token(r.values[field]) for r in normalized[child].rows if r.values.get(field) is not None}
        item.update(status='known', parent=parent, parent_rows=len(normalized[parent].rows),
                    child_rows=len(normalized[child].rows), parent_distinct_keys=len(p), child_distinct_keys=len(c),
                    matching_distinct_keys=len(p & c), child_unmatched_keys=len(c-p), parent_unreferenced_keys=len(p-c))
    return results


def _source_sheet(ws, mapping):
    hits = [s for s in getattr(ws.inv, 'sheets', []) if (not mapping.file or s.file == Path(mapping.file).name)
            and (not mapping.sheet or s.sheet == mapping.sheet)]
    if not mapping.file and not mapping.sheet:
        hits = [s for s in hits if all(c in s.headers for c in mapping.columns.values())]
    if len(hits) != 1:
        raise ValueError('源工作表缺失或不唯一')
    st = hits[0]
    if mapping.header_rows:
        paths = [Path(p) for p in getattr(ws.inv, 'files', []) if Path(p).name == st.file]
        if len(paths) != 1:
            raise ValueError('自定义表头对应文件不唯一')
        st = table_at(paths[0], st.sheet, mapping.header_rows)
    if st is None:
        raise ValueError('未识别源表')
    return st


def _selected_rows(st, mapping, definition, period):
    if mapping.unpivot:
        return None, 'unpivot展开会生成或拆分行，转换前有效原格统计未确定'
    keep = row_filter(st.headers, mapping.filter)
    if isinstance(keep, str):
        return None, keep
    pcol = mapping.columns.get(definition.period) if definition.period else None
    if period and definition.period and pcol not in st.headers:
        return None, '期间字段不是可直接读取的原列'
    win = period.window(definition.window) if period and pcol else None
    selected = []
    for row, vals in st.rows:
        if not keep(vals):
            continue
        if win:
            index = st.headers.index(pcol)
            inside = win.contains(vals[index] if index < len(vals) else None)
            if inside is None:
                return None, '存在无法确定核算期间的原始行'
            if not inside:
                continue
        selected.append((row, vals))
    return selected, '筛选及期间后，if_empty/类型转换/去重/population前'


def build_profile(ws, *, value_limit=VALUE_LIMIT):
    """Unsupported or invalid observations are explicitly unknown, never guessed."""
    value_limit = max(1, min(int(value_limit), 50))
    datasets, mapping = getattr(ws, 'datasets', {}), getattr(ws, 'mapping', {})
    period = getattr(ws, 'period', None)
    result = {'schema': PROFILE_SCHEMA, 'period': str(period),
              'scope': 'raw=识别源表数据行(不含表头/合计)；selected=筛选与期间后、默认值转换前；'
                       'mapped=实际映射/类型转换/if_empty/去重后，尚未做方案数据修订、population及关联挂载。'
                       '分类频数扫描全量；values_complete表示值域展示是否完整；不判断别名或缺失默认口径。',
              'datasets': {}}
    normalized, mapped_issues, file_hashes = {}, [], {}
    try:
        files = list(getattr(ws, 'data_files', []))
        if not files:
            files = [p for p in getattr(ws.inv, 'files', []) if Path(p).suffix.lower() in ('.xlsx', '.xlsm', '.csv')]
        if not files:
            raise ValueError('没有可读取的映射输入文件')
        file_hashes = {Path(p).name: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in files}
        rs = RuleSet(name='review-data-profile', version=str(PROFILE_SCHEMA), datasets=datasets,
                     pool={'expr': '0'}, allocation=[])
        normalized, mapped_issues = apply_mapping(rs, files, mapping, period)
        mapping_failure = ''
    except Exception as exc:
        mapping_failure = type(exc).__name__ + ': ' + str(exc)[:240]
    for name, definition in datasets.items():
        m = mapping.get(name)
        item = result['datasets'][name] = {'fields': {}}
        if m is None:
            item.update(_unknown('没有字段映射'))
            continue
        try:
            st = _source_sheet(ws, m)
            m, _ = rebind_dup_columns(m, st.headers)
            selected, selected_note = _selected_rows(st, m, definition, period)
            item.update(file=st.file, sheet=st.sheet, header_row=st.header_row,
                        source_fingerprint=_hash([st.headers, st.rows]),
                        source_file_fingerprint=file_hashes.get(st.file),
                        mapping_fingerprint=_hash(m.model_dump(mode='json')),
                        selected_scope=selected_note, declared_period_field=definition.period,
                        declared_window=definition.window, population_from=definition.population_from)
        except Exception as exc:
            item.update(_unknown(type(exc).__name__ + ': ' + str(exc)[:240]))
            continue
        blocked = [i.code for i in mapped_issues if i.severity == 'blocking' and
                   (i.where == name or str(i.where).startswith(f'{st.file}/{st.sheet}!') or not i.where)]
        table = normalized.get(name)
        for field, fd in getattr(definition, 'fields', {}).items():
            column = m.columns.get(field, '')
            info = item['fields'][field] = {'type': fd.type, 'source_column': column}
            direct = st.headers.count(column) == 1 and column not in (m.combine or {})
            uncertain = direct and any(st.headers.index(column) in indices for _, indices in st.unsure)
            if uncertain:
                direct = False
            if not direct:
                info['raw'] = _unknown('跨段原列对应不确定' if uncertain else '派生列或缺少唯一原列；不能把合成/默认值当原格')
                info['selected'] = _unknown('没有唯一原始列')
            else:
                index = st.headers.index(column)
                get = lambda rows: [(r, vals[index] if index < len(vals) else None) for r, vals in rows]
                categorical = _categorical(field, fd, column)
                info['raw'] = _stats(get(st.rows), values=categorical, limit=value_limit)
                info['selected'] = (_stats(get(selected), values=categorical, limit=value_limit)
                                    if selected is not None else _unknown(selected_note))
                info['source_position'] = {'column_1based': index+1 if not st.remaps and not st.unsure else None,
                                           'rows_refer_to': '源工作表行号；列按source_column核对'}
            if mapping_failure or table is None or blocked:
                reason = mapping_failure or ('映射存在阻断：' + ','.join(sorted(set(blocked))) if blocked else '未取得映射数据')
                info['mapped'] = _unknown(reason)
            else:
                rows = [(r.src.row if r.src else None, r.values.get(field)) for r in table.rows]
                info['mapped'] = _stats(rows, values=_categorical(field, fd, column), limit=value_limit)
                if fd.type == 'date' or field == definition.period or _TIME_FIELD.search(field + ' ' + column):
                    item.setdefault('mapped_date_ranges', {})[field] = _date_range(rows)
        ranges = item.get('mapped_date_ranges', {})
        item['date_range_status'] = ('known' if ranges and all(r['status']=='known' for r in ranges.values()) else 'unknown')
    allocations = [draft.get('allocation') or [] for draft in getattr(ws, 'drafts', {}).values() if isinstance(draft, dict)]
    result['relations'] = _relations(datasets, normalized, allocations)
    result['mapped_data_complete'] = not mapping_failure and not any(i.severity == 'blocking' for i in mapped_issues)
    if not result['mapped_data_complete']:
        # A converter can return a partial table; its key coverage is not complete evidence.
        result['relations'] = [{**r, 'status': 'unknown', 'reason': '映射未完整通过，覆盖数字仅作诊断'}
                               for r in result['relations']]
    result['fingerprint'] = _hash(result)
    return result


def review_context(ws, *, max_chars=40000):
    """Bound output by complete fields, never slice JSON or source values."""
    visible = copy.deepcopy(build_profile(ws))
    visible['fields_complete'] = True
    # Most reference tables have identical pre/post-filter counts. Preserve all
    # fields before spending the context budget on repeated copies of evidence.
    visible['same_as'] = '同一字段的统计完全相同时引用对应阶段；并非省略检查或抽样'
    for dataset in visible['datasets'].values():
        for info in dataset.get('fields', {}).values():
            raw, selected, mapped = (info.get(k) for k in ('raw', 'selected', 'mapped'))
            if selected == raw and raw is not None:
                info['selected'] = {'same_as': 'raw'}
            if mapped == selected and selected is not None:
                info['mapped'] = {'same_as': 'selected'}
            elif mapped == raw and raw is not None:
                info['mapped'] = {'same_as': 'raw'}
    encode = lambda: json.dumps(visible, ensure_ascii=False, separators=(',', ':'))
    while len(encode()) > max_chars:
        candidates = [(name, field, info) for name, ds in visible['datasets'].items()
                      for field, info in ds.get('fields', {}).items()]
        if not candidates:
            break
        def priority(candidate):
            name, field, info = candidate
            missing = sum(info.get('raw', {}).get(k, 0) for k in ('null', 'blank'))
            categorical = bool(info.get('mapped', {}).get('values') or info.get('raw', {}).get('values'))
            return (2 if missing else 1 if categorical else 0, name, field)
        name, field, _ = min(candidates, key=priority)
        del visible['datasets'][name]['fields'][field]
        visible['datasets'][name].setdefault('omitted_fields', []).append(field)
        visible['fields_complete'] = False
    return '\n## 数据概况（程序扫描；非生产公式或答案）\n' + encode()
