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
from unicodedata import normalize

from bonus_core.data.mapping import apply_mapping, rebind_dup_columns, row_filter
from bonus_core.data.table import ConvertError, convert, to_date
from bonus_core.data.table_detect import scope_unpivot_source, table_at
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


def _stats(rows, *, values=False, limit=VALUE_LIMIT, offset=0):
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
        page = ordered[offset:offset + limit]
        out.update(values=[{'value': k[1], 'type': k[0], 'count': counts[k], 'rows': refs[k]} for k in page],
                   values_complete=len(page) == len(ordered), values_truncated=len(page) < len(ordered),
                   omitted_values=len(ordered) - len(page))
    return out


def _unknown(reason):
    return {'status': 'unknown', 'reason': reason, 'scan_complete': False}


def unpivot_column_shapes(sheet, spec: dict) -> str:
    """Summarize the selected physical columns and competing ratio columns in one pass."""
    from openpyxl.utils import get_column_letter
    from bonus_core.data.table_detect import unpivot_cell_shape

    selected = set(spec['columns'])
    share_columns = spec.get('share_columns')
    ratios = {column for column in share_columns.values() if isinstance(column, str)} if isinstance(share_columns, dict) else set()
    if isinstance(spec.get('share_from'), str) and spec['share_from']:
        ratios.add(spec['share_from'])
    # Consider the whole repeated header group, even if the proposed mapping
    # chose a different member of the group.
    bases = {re.sub(r'\([A-Z]+列\)$', '', column) for column in ratios | selected}
    siblings = [i for i, h in enumerate(sheet.headers)
                if h and (re.sub(r'\([A-Z]+列\)$', '', h) in bases
                          or re.search(r'比例|占比|份额|share|ratio', h, re.I))]
    indexes = sorted(siblings, key=lambda i: (sheet.headers[i] not in ratios,
                                              sheet.headers[i] not in selected, i))[:12]
    profiles = []
    for idx in sorted(indexes):
        counts = {}
        for _, row in sheet.rows:
            key = unpivot_cell_shape(row[idx] if idx < len(row) else None)
            counts[key] = counts.get(key, 0) + 1
        profiles.append(f'{get_column_letter(idx + 1)}列「{sheet.headers[idx]}」'
                        + '、'.join(f'{kind}{count}格' for kind, count in sorted(counts.items())))
    return '本期原表列内容统计（不代表业务取列决定）：' + '，'.join(profiles)


def _categorical(field, definition, column):
    kind = getattr(definition, 'type', None)
    return kind in ('text', 'enum', 'bool') and not _IDENTITY.search(field + ' ' + column)


def query_category_fields(expression, definition, mapping):
    """Find the actual columns being counted or compared to text, across nested DSL calls."""
    from bonus_core.dsl.parser import Call, Cmp, Name, Str, parse, walk

    fields = []
    for node in walk(parse(expression)):
        if isinstance(node, Call) and node.fn == 'count_distinct' and node.args and isinstance(node.args[0], Name):
            field = node.args[0].id
        elif isinstance(node, Cmp) and isinstance(node.left, Name) and isinstance(node.right, Str):
            field = node.left.id
        elif isinstance(node, Cmp) and isinstance(node.right, Name) and isinstance(node.left, Str):
            field = node.right.id
        else:
            continue
        if field not in fields and field in definition.fields and _categorical(
                field, definition.fields[field], mapping.columns.get(field, '')):
            fields.append(field)
    return fields


def category_probe(args):
    """Recognize only distinct membership tests answered by one field's frequency table."""
    from bonus_core.dsl.parser import BinOp, Call, Cmp, ExprError, Name, Str, parse

    expr = args.get('expr')
    if not isinstance(expr, str) or not isinstance(args.get('dataset'), str):
        return None
    try:
        stack = [parse(expr)]
    except ExprError:
        return None
    fields = set()
    while stack:
        term = stack.pop()
        if isinstance(term, BinOp) and term.op == '+':
            stack.extend((term.left, term.right))
            continue
        if not (isinstance(term, Call) and term.fn == 'count_distinct'
                and len(term.args) == 1 and isinstance(term.args[0], Name)):
            return None
        field = term.args[0]
        kw = term.kw()
        if set(kw) - {'of', 'where'} or ('of' in kw and kw['of'] != Str(args['dataset'])):
            return None
        if 'where' in kw:
            test = kw['where']
            if not (isinstance(test, Cmp) and test.op == '==' and (
                    test.left == field and isinstance(test.right, Str)
                    or test.right == field and isinstance(test.left, Str))):
                return None
        fields.add(field.id)
    return (args['dataset'], fields.pop()) if len(fields) == 1 else None


def repeated_category_probe(calls):
    if len(calls) != 2 or any(not isinstance(args, dict) for args in calls):
        return None
    first = category_probe(calls[0])
    return first if first and first == category_probe(calls[1]) else None


def archived_category_probe(out, last):
    """Read only the identity of a repeated query from the exact failed round."""
    if not out or not last or last.get('status') != 'FAILED' or type(last.get('n')) is not int:
        return None
    error = last.get('error') or ''
    if 'construction:' not in error:
        return None
    path = out / 'history' / f'第{last["n"]}轮' / 'agent_log.json'
    try:
        log = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(log, dict) or log.get('error') != error or not isinstance(log.get('steps'), list):
        return None
    return repeated_category_probe([s.get('args') if isinstance(s, dict) and s.get('ok') is True
                                    and s.get('tool') == 'query_dataset' else None for s in log['steps'][-2:]])


def archived_unpivot_failure(ws, out, last):
    """Keep a failed, unsaved allocation mapping in focus on a same-source resume."""
    if (not out or not last or last.get('status') != 'FAILED' or type(last.get('n')) is not int
            or last.get('files') != ws._source_version or last.get('period') != (ws.period.label if ws.period else None)):
        return None
    path = out / 'history' / f'第{last["n"]}轮' / 'agent_log.json'
    try:
        log = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(log, dict) or log.get('error') != last.get('error') or not isinstance(log.get('steps'), list):
        return None
    for step in reversed(log['steps']):
        if not isinstance(step, dict) or step.get('tool') != 'define_dataset' or step.get('ok') is not False:
            continue
        args, result = step.get('args'), step.get('result')
        if (not isinstance(args, dict) or not isinstance(result, str)
                or not result.startswith(('错误：unpivot.share_parse：', '错误：unpivot.columns：'))):
            continue
        name, file, sheet = (args.get(key) for key in ('name', 'file', 'sheet'))
        if (not all(isinstance(value, str) and value for value in (name, file, sheet))
                or name in ws.datasets or file not in ws._source_version
                or any(mapping.file == file and mapping.sheet == sheet for mapping in ws.mapping.values())):
            continue
        first, separator, rest = result.partition('\n')
        spec, fields = args.get('unpivot'), args.get('fields')
        if ('列内容统计' not in first and isinstance(spec, dict) and isinstance(spec.get('columns'), list)
                and all(isinstance(column, str) for column in spec['columns']) and isinstance(fields, dict)):
            try:
                raw = (table_at(ws._file(file), sheet, args['header_rows']) if args.get('header_rows')
                       else ws.inv.sheet(f'{file}/{sheet}'))
                period_field = fields.get(args.get('period')) or {}
                if ws.period and raw is not None and isinstance(period_field, dict):
                    raw = scope_unpivot_source(raw, spec, period_field.get('column'),
                                               ws.period.window(args.get('window') or 'period'))
                if raw is not None:
                    result = first + '。' + unpivot_column_shapes(raw, spec) + separator + rest
            except (KeyError, TypeError, ValueError):
                pass  # the archived diagnostic still provides its original cell location
        return 'define_dataset', args, result
    return None


def category_handoff(ws, probe, *, prior_error=None):
    """Give the formula writer fresh evidence, never a business classification or a passing receipt."""
    from bonus_agent.agent import ToolError
    from bonus_agent.construction import TaskBoard

    if not probe or ws.pending or ws._flow().stage()[0] != 3:
        return None
    dataset, field = probe
    definition, mapping = ws.datasets.get(dataset), ws.mapping.get(dataset)
    if (not definition or not mapping or field not in definition.fields or not _categorical(
            field, definition.fields[field], mapping.columns.get(field, ''))):
        return None
    active = TaskBoard(ws).summary().get('active')
    if not active or active['target'] == '/structure':
        return None
    blocker = f'construction:{active["id"]}:{active["target"]}'
    if prior_error is not None and blocker not in re.split('[。；]', prior_error):
        return None
    try:
        result = ws.query_dataset(dataset, values_of=field)
    except ToolError:
        return None
    evidence = result['field_evidence'][0]
    if not evidence['values_complete'] or len(json.dumps(evidence, ensure_ascii=False)) > 4300:
        return None
    return {'task': active['id'], 'scheme': active['scheme'], 'target': active['target'],
            'dataset': dataset, 'field_evidence': evidence,
            'next': '该字段的完整取值已核实。按相关条款区分原表值和派生类别，使用已有任务的依赖，'
                    '证据充分时提交该任务；缺少独立条件时读取对应原文或核对其他字段的交叉条件。'}


def query_field_evidence(ws, dataset, field, rows, offset=0):
    """Use the exact same mapped source rows as the scalar query, before its where condition."""
    mapping = ws.mapping[dataset]
    stats = _stats([(row.src.row if row.src else None, row.values.get(field)) for row in rows],
                   values=True, offset=offset)
    source = next((row.src for row in rows if row.src), None)
    column = mapping.columns.get(field, '')
    values = {v['value'] for v in stats['values']}
    matches = [c for c in ws.clauses if column and column in c.quote and any(v in c.quote for v in values)]
    end = offset + len(stats['values'])
    return {'field': field, 'source': {'file': source.file if source else mapping.file,
                                      'sheet': source.sheet if source else mapping.sheet, 'column': column},
            'offset': offset, **stats,
            'related_clauses': [{'id': c.id, 'file': c.file, 'quote': c.quote} for c in matches[:3]],
            'related_clause_count': len(matches),
            'next_call': ({'tool': 'query_dataset', 'args': {'dataset': dataset, 'values_of': field, 'offset': end}}
                          if end < stats['distinct_nonblank'] else None)}


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


def _covered_month(window, value):
    span = window.span_of(value)
    if span is None:
        return None
    start, end = span
    if window.start <= start <= end <= window.end and (start.year, start.month) == (end.year, end.month):
        return f'{start.year:04d}-{start.month:02d}'
    return None


def _source_column_name(header):
    text = normalize('NFKC', str(header)).strip().casefold()
    return re.sub(r'^(?:求和项\s*:\s*|sum\s+of\s+)', '', text)


def _historical_alternatives(ws, definition, mapping, rows, window, observed, allowed_files, ignored_sheets):
    """Find readable dated inputs sharing actual keys and measure headers."""
    period_field = definition.period
    fields = {name: _source_column_name(column) for name, column in mapping.columns.items()
              if name in definition.fields and column not in (mapping.combine or {})}
    declared_keys = {spec.key for spec in ws.datasets.values() if spec.key}
    identities = {name: {row.values.get(name) for row in rows if row.values.get(name) not in (None, '')}
                  for name in fields if name != period_field
                  and (definition.fields[name].type == 'id' or name in declared_keys)}
    measures = {name: definition.fields[name].model_copy(update={'if_empty': None})
                for name in fields if definition.fields[name].type in ('money', 'number', 'int', 'ratio')}
    if period_field not in fields or not measures:
        return []
    alternatives = []
    for sheet in ws.inv.sheets:
        if (sheet.file not in allowed_files or f'{sheet.file}/{sheet.sheet}' in ignored_sheets
                or (sheet.file, sheet.sheet) == (Path(mapping.file).name, mapping.sheet)):
            continue
        headers = [_source_column_name(header) for header in sheet.headers]
        positions = {name: headers.index(header) for name, header in fields.items() if headers.count(header) == 1}
        amount_columns = [name for name in measures if name in positions]
        key_columns = [name for name, values in identities.items() if values and name in positions]
        if period_field not in positions or not amount_columns or (identities and not key_columns):
            continue
        matched_months, first_rows = Counter(), {}
        for row, values in sheet.rows:
            if len(values) < len(headers):
                continue
            month = _covered_month(window, values[positions[period_field]])
            if not month or month in observed:
                continue
            if key_columns and not any(convert(values[positions[name]], definition.fields[name]) in identities[name]
                                       for name in key_columns):
                continue
            try:
                has_amount = any(convert(values[positions[name]], measures[name]) is not None for name in amount_columns)
            except ConvertError:
                continue
            if has_amount:
                matched_months[month] += 1
                first_rows.setdefault(month, row)
        if not matched_months:
            continue
        months = sorted(matched_months)
        matched_fields = [period_field, *key_columns[:3], *amount_columns[:3]]
        alternatives.append({'file': sheet.file, 'sheet': sheet.sheet,
                             'columns': {name: sheet.headers[positions[name]] for name in matched_fields},
                             'additional_months': months[:12], 'additional_month_count': len(months),
                             'read_call': {'tool': 'read_sheet', 'args': {
                                 'file': sheet.file, 'sheet': sheet.sheet,
                                 'start_row': first_rows[months[0]], 'rows': 3}}})
    alternatives.sort(key=lambda item: (-item['additional_month_count'], item['file'], item['sheet']))
    return alternatives[:2]


def historical_source_evidence(ws, data, allowed_files, ignored):
    if ws.period is None:
        return []
    evidence = []
    for name, definition in ws.datasets.items():
        if not definition.period or definition.window == 'period' or name not in data:
            continue
        mapping = ws.mapping.get(name)
        if mapping is None or not mapping.file:
            continue
        window = ws.period.window(definition.window)
        rows = data[name].rows
        months = Counter(_covered_month(window, row.values.get(definition.period)) for row in rows)
        unclassified = months.pop(None, 0)
        alternatives = (_historical_alternatives(ws, definition, mapping, rows, window, months,
                                                allowed_files - ignored, ignored)
                        if len(months) < window.months else [])
        evidence.append({'dataset': name, 'file': Path(mapping.file).name, 'sheet': mapping.sheet,
                         'window': definition.window, 'requested_period': window.label,
                         'mapped_months': dict(sorted(months.items())[-12:]), 'mapped_month_count': len(months),
                         'rows_without_month': unclassified, 'alternatives': alternatives,
                         'note': '空月份可能没有业务。候选按日期、已见编号和金额列识别。按原文核实口径和记录粒度后，'
                                 '若需更换物理源表，用 define_dataset 保留相应期间、身份和空值规则。'})
    return evidence


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
