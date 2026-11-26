"""Check physical input coverage before issuing a staged data receipt."""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from unicodedata import normalize

from bonus_agent.scenarios import InputTable
from bonus_agent.scenarios.inputs import _col_index, _pick, norm
from bonus_core.data.table_detect import ROW_ID
from openpyxl.utils import get_column_letter


_RAW_COLUMNS = re.compile(
    r'^\s*(?:[\d]+\s*[.．、)）]\s*)?(?:原始列|原始字段|输入列|source columns|input columns)'
    r'\s*[:：]\s*([^\r\n]+)', re.MULTILINE | re.IGNORECASE
)
_PIVOT_PREFIX = re.compile(r'^(?:求和项|平均值项|计数项|sum of|average of|count of)\s*[:：]?\s*', re.I)
_REPORT_QUALIFIER = re.compile(r'[,，]\s*[^,，()（）]*(?:合计|汇总|subtotal|total)\s*([)）])$', re.I)
_ZERO_ROWS = re.compile(
    r'^\s*(?:[-*]\s*)?(?P<column>[^,。.:;\r\n|]{1,44}?)\s*(?:为|等于)\s*0(?:\.0+)?\s*'
    r'(?:的)?(?:行|记录)\s*(?:\([^()\r\n]*\)\s*)?(?:不参与|不纳入|不计入)'
    r'(?:本次|本期|当期)?(?:核算|计算)(?=[,.。;\s]|$)',
)
_REFERENCE_SCOPE = re.compile(
    r'参考(?:数据|资料|表)里?有[、, ]*但(?:实际)?(?:销售单|销售明细|主表|核算明细)(?:里|内|中)没有'
    r'[^。;]{0,24}(?:金额|记录)[^。;]{0,12}不参与核算',
)


def _labels(line):
    """Split an explicit column list without splitting units or notes in parentheses."""
    start = depth = 0
    for index, char in enumerate(line):
        if char in '（([':
            depth += 1
        elif char in '）)]':
            depth = max(0, depth - 1)
        elif depth == 0 and char in '、，,；;':
            yield line[start:index].strip(' 。.「」“”')
            start = index + 1
    yield line[start:].strip(' 。.「」“”')


def scenario_contract(scenario):
    """Use the host's scenario declaration; model plans cannot edit this contract."""
    return {
        'tables': [t.model_dump(mode='json') for t in (getattr(scenario, 'inputs', None) or ()) if t.required],
        'columns': {
            label: line.strip() for line in _RAW_COLUMNS.findall(getattr(scenario, 'outputs', '') or '')
            for label in _labels(line) if label
        },
        **({'supplement_sources': sorted(p.name for p in scenario.supplements)}
           if getattr(scenario, 'supplements', None) else {}),
    }


def _column_name(value):
    value = _PIVOT_PREFIX.sub('', value or '')
    # Keep the currency qualifier (settlement, local, accounting). Only the
    # header's spelling of the currency suffix is interchangeable.
    value = re.sub(r'币(?:别|种)$', '币', value.strip())
    return norm(_REPORT_QUALIFIER.sub(r'\1', value))


def row_scope_clauses(ws):
    for clause in ws.clauses:
        if match := _ZERO_ROWS.match(normalize('NFKC', clause.quote)):
            yield match['column'].strip(' 「」“”"'), clause


def _matching_scope_columns(headers, label):
    target = _column_name(label)
    suffix = re.compile(r'\([^()]+\)$')
    qualified = bool(suffix.search(normalize('NFKC', label)))
    return [col for col in headers if col and (_column_name(col) == target or (
        not qualified and _column_name(suffix.sub('', normalize('NFKC', col))) == target))]


def _row_rule_sources(ws, label, candidates):
    """A linked reference's explicitly aggregated measure cannot identify raw zero rows."""
    if len(candidates) < 2 or _PIVOT_PREFIX.match(label):
        return candidates
    direct = [hit for hit in candidates if any(not _PIVOT_PREFIX.match(col) for col in hit[2])]
    if len(direct) != 1 or len(direct[0][2]) != 1:
        return candidates
    keys = {ws.datasets[n].key for n in direct[0][1] if ws.datasets[n].key}
    for _, family, columns, _ in candidates:
        if family == direct[0][1]:
            continue
        measures = [ws.datasets[n] for n in family if set(columns).intersection(ws.mapping[n].columns.values())]
        if not measures or any(ds.key or ds.link not in keys for ds in measures):
            return candidates
    return direct


def _zero_filter(columns, column):
    """Intersect the existing column condition with an explicit nonzero rule."""
    from bonus_core.data.mapping import row_filter

    remove_zero = row_filter([column], {column: {'not': ['0']}})
    current = columns.get(column)
    if current is None:
        return {'not': ['0']}
    if isinstance(current, dict):
        excluded = current['not']
        return current if any(not remove_zero([v]) for v in excluded) else {'not': [*excluded, '0']}
    allowed = current if isinstance(current, list) else [current]
    keep = [v for v in allowed if remove_zero([v])]
    return current if keep == allowed else keep


def _current_window_patch(ws, name, family, sheet):
    """Narrow a month-keyed detail only when the requested wider window added no rows."""
    from bonus_agent.data_profile import _selected_rows

    detail, mapping = ws.datasets[name], ws.mapping[name]
    if not ws.period or detail.window == 'period' or detail.key:
        return {}
    stamp = mapping.columns.get(detail.period)
    if stamp not in sheet.headers:
        return {}
    index = sheet.headers.index(stamp)
    parents = []
    for p in family:
        parent, origin = ws.datasets[p], ws.mapping[p]
        key = parent.key
        components = (mapping.combine or {}).get(key, ())
        if (not key or detail.link not in (None, key) or parent.window != 'period' or stamp not in components
                or (origin.combine or {}).get(key) != components):
            continue
        if parent.period and origin.columns.get(parent.period) != stamp:
            continue
        if not parent.period and any(ws.period.contains(values[index]) is not True for _, values in sheet.rows):
            continue
        if not detail.link and origin.filter not in ({}, mapping.filter):
            continue
        parents.append(key)
    rows, _ = _selected_rows(sheet, mapping, detail, ws.period)
    if len(parents) != 1 or not rows or any(ws.period.contains(values[index]) is not True for _, values in rows):
        return {}
    if detail.link:
        return {'window': 'period'}
    if any(draft.get('pool') or draft.get('variables') for draft in ws.drafts.values()):
        return {}  # The draft may deliberately use a company-level source.
    return {'window': 'period', 'link': parents[0]}


def _align_reference_scope(before, candidate, repairs, corpus):
    """An unrelated bad join cannot inherit the authority to ignore excluded rows."""
    from bonus_agent.clauses import check_quote

    scope_files = {row['rule_file'] for row in repairs}
    bases = {clause.file: {'file': clause.file, 'quote': clause.quote} for clause in before.clauses
             if clause.file in scope_files and _REFERENCE_SCOPE.search(normalize('NFKC', clause.quote))
             and not check_quote(clause.file, clause.quote, corpus)}
    if not bases:
        return
    for row in tuple(repairs):
        parent, file = row['dataset'], row['rule_file']
        key = candidate.datasets[parent].key
        if file not in bases or 'filter' not in row['changes'] or not key:
            continue
        origin = candidate.mapping[parent]
        prior = set(before._values(parent, key))
        active = set(candidate._values(parent, key))
        if not active or not prior - active:
            continue
        for name, definition in candidate.datasets.items():
            if definition.link != key or definition.unmatched != 'block' or definition.key:
                continue
            mapping = candidate.mapping[name]
            column = mapping.columns.get(key)
            if (mapping.file == origin.file or not column or _column_name(column) != _column_name(origin.columns.get(key))
                    or not any(f.type == 'money' for f in definition.fields.values())):
                continue
            values = set(candidate._values(name, key))
            missing = values - active
            if not missing or not values & active or not missing <= prior - active:
                continue
            changes = {'unmatched': 'ignore', 'unmatched_basis': bases[file]}
            candidate.update_dataset(name, changes)
            repairs.append({'dataset': name, 'parent': parent, 'excluded_reference_keys': len(missing), 'changes': changes})


def reconcile_row_scopes(ws):
    """Apply a verified row rule to all views of its uniquely identified raw source."""
    from copy import deepcopy

    from bonus_agent.agent import ToolError
    from bonus_agent.clauses import check_quote
    from bonus_agent.data_profile import _source_sheet
    from bonus_core.data.mapping import row_filter

    rules = list(row_scope_clauses(ws))
    if not rules:
        return [], []
    ws._check_source_version()
    corpus = ws._corpus()
    roles = ws.workflow['plan']['files']
    by_source = defaultdict(list)
    for name, mapping in ws.mapping.items():
        if mapping.file and mapping.sheet and roles.get(Path(mapping.file).name) in ('data', 'rule_and_data'):
            by_source[(mapping.file, mapping.sheet, tuple(mapping.header_rows or ()))].append(name)

    changes, repairs, issues = {}, [], []
    for label, clause in rules:
        evidence = f'《{clause.file}》{clause.id}「{clause.quote}」'
        if not label or check_quote(clause.file, clause.quote, corpus):
            issues.append(f'dataset.scope：{evidence}未能在当前材料中逐字复验。')
            continue
        candidates = []
        for family in by_source.values():
            measures = [(ws.mapping[name].columns.get(field), ws.datasets[name].window) for name in family
                        if not ws.mapping[name].unpivot for field, definition in ws.datasets[name].fields.items()
                        if definition.type in ('money', 'number')]
            if not measures:
                continue
            try:
                sheet = _source_sheet(ws, ws.mapping[family[0]])
            except (ValueError, KeyError, OSError):
                continue
            columns = _matching_scope_columns(sheet.headers, label)
            windows = {window for field, window in measures if field in columns}
            if windows:
                candidates.append((sheet, family, columns, windows))
        candidates = _row_rule_sources(ws, label, candidates)
        current = [hit for hit in candidates if 'period' in hit[3]]
        if current:
            candidates = current  # Wider-window inputs are checked at their consuming formula.
        if len(candidates) != 1 or len(candidates[0][2]) != 1:
            found = '、'.join(f'{sheet.file}/{sheet.sheet} 的 {"、".join(columns)}' for sheet, _, columns, _ in candidates)
            issues.append(f'dataset.scope：{evidence}要求排除零值，尚未唯一绑定原始数值列。'
                          f'已识别 {found or "无已映射的同名数值列"}。请核对币种、列名和数据源。')
            continue
        sheet, family, (column,), _ = candidates[0]
        keep = row_filter(sheet.headers, {column: {'not': ['0']}})
        removed = sum(not keep(values) for _, values in sheet.rows)
        for name in family:
            mapping = ws.mapping[name]
            if mapping.unpivot and column in mapping.unpivot.get('columns', ()):
                issues.append(f'dataset.scope：{evidence}的原始筛选列「{column}」在「{name}」中已展开，'
                              '需核对展开前的有效行和筛选映射。')
                continue
            before = changes.get(name, {}).get('filter', mapping.filter)
            patch = {}
            if removed:
                condition = _zero_filter(before, column)
                if condition == []:
                    issues.append(f'dataset.scope：{evidence}与「{name}」仅允许零值的筛选冲突。')
                    continue
                if before.get(column) != condition:
                    patch['filter'] = {**before, column: condition}
            patch.update(_current_window_patch(ws, name, family, sheet))
            if patch:
                changes.setdefault(name, {}).update(patch)
                repairs.append({'dataset': name, 'file': sheet.file, 'sheet': sheet.sheet, 'column': column,
                                'clause': clause.id, 'rule_file': clause.file, 'evidence': evidence,
                                'raw_zero_rows': removed, 'changes': patch})
    if issues or not changes:
        return [], issues
    candidate = deepcopy(ws, {id(ws.llm): ws.llm})
    try:
        for name, patch in changes.items():
            candidate.update_dataset(name, patch)
        _align_reference_scope(ws, candidate, repairs, corpus)
    except (ToolError, ValueError) as exc:
        return [], [f'dataset.scope：按原文调整「{name}」后未通过数据定义复验。{exc}']
    ws.datasets, ws.mapping = candidate.datasets, candidate.mapping
    ws._prev_fields, ws.untested, ws.changed = candidate._prev_fields, candidate.untested, True
    return repairs, []


def _physical_columns(mapping, fields):
    columns = set()
    for field, column in mapping.columns.items():
        if field not in fields or column == ROW_ID:
            continue
        columns.update((mapping.combine or {}).get(column, [column]))
    unpivot = mapping.unpivot or {}
    if {unpivot.get('label'), unpivot.get('value')} & columns:
        columns.update(unpivot.get('columns') or ())
        columns.update(unpivot.get('share_from_columns') or ())
        if unpivot.get('share_from'):
            columns.add(unpivot['share_from'])
    if unpivot.get('share') in columns:
        columns.update((unpivot.get('share_columns') or {}).values())
    return columns


def _mapped_origins(ws, data):
    """Use sources returned by apply_mapping, including explicitly mapped empty tables."""
    origins = defaultdict(set)
    for name, table in data.items():
        mapping = ws.mapping[name]
        columns = _physical_columns(mapping, ws.datasets[name].fields)
        if not columns:
            continue
        locations = {(row.src.file, row.src.sheet) for row in table.rows if row.src}
        if not locations:
            if mapping.file and mapping.sheet:
                locations = {(mapping.file, mapping.sheet)}
            else:
                candidates = {
                    (s.file, s.sheet) for s in ws.inv.sheets if (not mapping.file or s.file == mapping.file)
                    and (not mapping.sheet or s.sheet == mapping.sheet)
                    and (mapping.file or mapping.sheet or columns <= set(s.headers))
                }
                if len(candidates) == 1:
                    locations = candidates
        for location in locations:
            origins[location].update(columns)
    return origins


def _entity_scopes(ws, data):
    for name, definition in ws.datasets.items():
        if not definition.key or name not in data:
            continue
        rows = {str(row.values[definition.key]): str(row.src) for row in data[name].rows
                if row.values.get(definition.key) not in (None, '')}
        key_columns = _physical_columns(ws.mapping[name], {definition.key})
        yield {
            'dataset': name, 'keys': len(rows), 'examples': [
                {'key': key, 'source': rows[key]} for key in sorted(rows)[:3]
            ], 'key_columns': sorted(key_columns),
        }


def check_sources(ws, data):
    """Return material and physical-column requirements with a bounded next action."""
    contract = ws.workflow.get('source_contract', {})
    files = ws.workflow.get('plan', {}).get('files', {})
    from bonus_agent.auto import DATA_SUFFIXES

    origins = _mapped_origins(ws, data)
    scopes = list(_entity_scopes(ws, data))
    sheets = ws.inv.sheets
    reports = []

    def add(kind, name, status, candidates, evidence, matched=()):
        locations = []
        for sheet in candidates:
            location = {'file': sheet.file, 'sheet': sheet.sheet, 'header_row': sheet.header_row}
            if kind == 'column':
                location['cells'] = [
                    get_column_letter(index) + str(sheet.header_row)
                    for index, header in enumerate(sheet.headers, 1) if _column_name(header) == _column_name(name)
                ][:8]
            locations.append(location)
        result = {'kind': kind, 'name': name, 'status': status,
                  'evidence': evidence, 'sources': locations[:6], 'matched': list(matched)}
        if status != 'MAPPED':
            headers = {norm(h) for sheet in candidates for h in sheet.headers if h}
            ranked = [(len({norm(c) for c in s['key_columns']} & headers), s) for s in scopes]
            result['scopes'] = [s for hits, s in sorted(ranked, key=lambda pair: -pair[0]) if hits][:4]
            if result['scopes']:
                result['scope_note'] = '列出当前业务对象及原始行作为待关联范围。实际命中按后续的精确关联校验计算。'
            if candidates:
                sheet = candidates[0]
                result['next_call'] = {
                    'tool': 'read_sheet',
                    'args': {'file': sheet.file, 'sheet': sheet.sheet, 'start_row': sheet.header_row, 'rows': 4},
                }
        reports.append(result)

    for label, evidence in contract.get('columns', {}).items():
        normalized = _column_name(label)
        candidates = sorted(
            (s for s in sheets if any(_column_name(h) == normalized for h in s.headers)),
            key=lambda s: (norm(label) not in norm(Path(s.file).stem), s.file, s.sheet),
        )
        matched = [f'{s.file}/{s.sheet}' for s in candidates
                   if any(_column_name(c) == normalized for c in origins.get((s.file, s.sheet), ()))]
        # Free-form output labels can name an alias or a custom header. A
        # structured template, where present, is authoritative for missing tables.
        status = 'MAPPED' if matched else 'UNMAPPED' if candidates else 'UNRESOLVED_LABEL'
        add('column', label, status, candidates, evidence, matched)

    for raw in contract.get('tables', ()):
        template = InputTable.model_validate(raw)
        best, ties = _pick(sheets, template)
        candidates = [best, *ties] if best else []
        valid = [s for s in candidates if all(_col_index(s.headers, c) is not None
                                             for c in template.columns if c.required)]
        matched = [f'{s.file}/{s.sheet}' for s in valid if (s.file, s.sheet) in origins]
        status = 'MAPPED' if matched else 'UNMAPPED' if valid else 'MISSING_SOURCE'
        add('table', template.name, status, candidates, '本批场景输入模板', matched)

    for file, role in sorted(files.items()):
        if role not in ('data', 'rule_and_data') or Path(file).suffix.lower() not in DATA_SUFFIXES:
            continue
        candidates = [s for s in sheets if s.file == file]
        matched = sorted(f'{f}/{s}' for f, s in origins if f == file)
        add('file', file, 'MAPPED' if matched else 'UNMAPPED', candidates, '本批计划中的计算数据', matched)

    return reports


def join_coverage(ws, data):
    """Identify gaps on the parent side even when every supplied child row can join."""
    from bonus_core.engine.tree import _loose_get, _norm_id

    parents = {d.key: name for name, d in ws.datasets.items() if d.key and name in data}
    for name, definition in ws.datasets.items():
        parent = parents.get(definition.link)
        if parent is None or name not in data:
            continue
        key = definition.link
        objects = {str(r.values[key]): r for r in data[parent].rows if r.values.get(key) not in (None, '')}
        normalized = defaultdict(list)
        for identity in objects:
            normalized[_norm_id(identity)].append(identity)
        matched = defaultdict(list)
        orphan_rows = 0
        for row in data[name].rows:
            value = row.values.get(key)
            identity = str(value) if value is not None else ''
            if identity not in objects and identity:
                alternatives = normalized[_norm_id(identity)]
                if len(alternatives) == 1:
                    identity = alternatives[0]
                elif synonym := _loose_get(objects, identity):
                    identity = str(synonym.values[key])
            if identity in objects:
                matched[identity].append(row)
            else:
                orphan_rows += 1
        missing = sorted(objects.keys() - matched.keys())
        multiple = sorted(identity for identity, rows in matched.items() if len(rows) > 1)
        yield {
            'dataset': name, 'parent': parent, 'key': key, 'parent_keys': len(objects),
            'matched_keys': len(matched), 'single_key_matches': len(matched) - len(multiple),
            'missing_keys': len(missing), 'multiple_keys': len(multiple), 'orphan_rows': orphan_rows,
            'missing_examples': [{'key': identity, 'source': str(objects[identity].src)} for identity in missing[:3]],
            'multiple_examples': [
                {'key': identity, 'sources': [str(row.src) for row in matched[identity][:4]]}
                for identity in multiple[:3]
            ],
        }


def coverage_issue(item):
    if item['status'] not in ('UNMAPPED', 'MISSING_SOURCE'):
        return None
    name = item['name']
    sources = '、'.join(f"{s['file']}/{s['sheet']}" for s in item['sources'][:3])
    if item['status'] == 'MISSING_SOURCE':
        return f'data.source_missing：本批必需来源「{name}」或其必需列缺失。'
    if sources:
        return f'data.source_unmapped：「{name}」尚未取数。可核对 {sources} 后补字段或数据集。'
    return f'data.source_unmapped：「{name}」已列为计算数据，尚无有效映射。请检查工作表和表头。'
