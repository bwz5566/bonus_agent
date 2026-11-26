"""Independent population contracts, evaluated from source rows before production filters.

Column identities are shared and remain subject to source review. Eligibility is
declared independently; production filters, periods, defaults and patches are
never silently copied into the reference population.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import re

from bonus_agent.clauses import check_quote
from bonus_agent.facts import norm
from bonus_core.data.mapping import DatasetMapping, apply_mapping
from bonus_core.dsl.models import DatasetDef, FieldDef, RuleSet, SourceRef
from bonus_core.dsl.parser import Call, Str, names, parse, walk


BLANK_SCOPE_OPTIONS = ('按空白占位排除所列行', '作为有效记录，补充原值或取数依据')


def blank_scope_question(rows):
    points = '、'.join(f"《{row['file']}》{row['sheet']} 第 {row['row']} 行"
                      f"（{'、'.join(row['empty_columns'])}）" for row in rows)
    return f'{points}的所列字段为空。请确认这些行按空白占位排除，还是作为有效记录并补充原值或取数依据？'


def scope_material_inputs(ws, dataset):
    from bonus_agent.recovery import fingerprint

    if dataset in ws.datasets and dataset in ws.mapping:
        return fingerprint((ws.datasets[dataset].model_dump(mode='json'), ws.mapping[dataset].model_dump(mode='json')))
    return None


def material_scope_review(ws, question):
    row = ws.workflow.get('material_scope_reviews', {}).get(question.id, {})
    if (row.get('files') == ws._source_version and row.get('period') == str(ws.period)
            and row.get('answer') == question.answer
            and row.get('related') == question.related and row.get('text') == question.text
            and row.get('decision_id') and question.answered_by == row.get('by')):
        if row.get('dataset_input') and row['dataset_input'] == scope_material_inputs(ws, row['declaration']['dataset']):
            return row
    return None


def withdraw_stale_material_scope_answers(ws, answers):
    records = ws.workflow.get('material_scope_reviews', {})
    stale = {}
    for question in ws.questions:
        row = records.get(question.id)
        if (row and question.answer == row['answer'] and question.answered_by == row['by']
                and material_scope_review(ws, question) is None):
            stale[question.id] = row
            question.answer = None
            question.answered_at = question.answered_by = ''
            question.withdrawn = '材料、取数配置或核算期间已变化，原范围问题按当前批次重新核对'
    return [entry for entry in answers or []
            if not ((old := stale.get(entry.get('id'))) and entry.get('answer') == old['answer']
                    and entry.get('answered_by') == old['by'])]


def verified_blank_scope_rows(ws, question, rows):
    if not rows or question.text != blank_scope_question(rows):
        return False
    coordinates = [{'file': r['file'], 'sheet': r['sheet'], 'row': r['row'], 'columns': r['empty_columns']}
                   for r in rows]
    evidence, errors = ws._evidence(coordinates)
    return (not errors and evidence == question.evidence and len(evidence) == len(rows)
            and all(ref['quote'] == ' | '.join(f'{c}=' for c in ref['columns']) for ref in evidence))


def confirmed_blank_scope_rows(ws, question, *, scheme=None):
    """Recognize the program's exact choice on the previously witnessed cells."""
    if question.withdrawn or tuple(question.options) != BLANK_SCOPE_OPTIONS:
        return []
    material = material_scope_review(ws, question)
    if material and (scheme is None or question.related.startswith(f'{scheme} / ')):
        rows = [{'file': row['file'], 'sheet': row['sheet'], 'row': row['row'], 'empty_columns': row['columns']}
                for row in question.evidence if {'file', 'sheet', 'row', 'columns'} <= row.keys()]
        if verified_blank_scope_rows(ws, question, rows):
            return rows
    if question.answer != BLANK_SCOPE_OPTIONS[0]:
        return []
    for name, review in ws.workflow.get('references', {}).items():
        if scheme is not None and name != scheme:
            continue
        scopes = dict(review.get('scope_origin', {}).get('scope_questions', {}))
        if review.get('spec_state') == 'unresolved':
            scopes.update(review.get('scope_questions', {}))
        scopes.update({label: decision['cells'] for label, decision in review.get('scope_confirmations', {}).items()
                       if decision['question'] == question.id and decision['answer'] == question.answer})
        for label, rows in scopes.items():
            if question.related == f'{name} / {label}' and verified_blank_scope_rows(ws, question, rows):
                return rows
    return []


def requirements(rs):
    levels = {rs.pool.scope}
    if rs.report:
        levels.update(item.level for item in [*rs.report.totals, *rs.report.details, *rs.report.balances])
    result = {level: {'kind': 'objects', 'level': level} for level in sorted(levels - {'global'})}
    # Correct object IDs do not prove the completeness of their contributing
    # details. Check all amount inputs, including historical/global aggregates.
    expressions = [rs.pool.expr]
    if rs.report:
        expressions += [item.expr for item in [*rs.report.totals, *rs.report.details]]
        expressions += [expr for item in rs.report.balances for expr in [item.total, *item.parts]]
    seen, datasets = set(), set()
    while expressions:
        ast = parse(expressions.pop())
        refs = names(ast)
        datasets.update(n for n, d in rs.datasets.items() if refs & set(d.fields))
        for node in walk(ast):
            if isinstance(node, Call) and isinstance(node.kw().get('of'), Str):
                datasets.add(node.kw()['of'].value)
        for name in refs & set(rs.variables) - seen:
            seen.add(name)
            expressions.append(rs.variables[name].expr)
    result.update({'rows:' + n: {'kind': 'rows', 'dataset': n} for n in sorted(datasets & set(rs.datasets))})
    return result


def header_locations(ws, doc, quote):
    """Return physical sheets whose headers account for the entire quotation."""
    quoted = {norm(part) for part in re.split(r'[|｜、\n]+', quote) if norm(part)}
    return [(sheet.file, sheet.sheet) for sheet in ws.inv.sheets
            if quoted and sheet.file == doc and quoted <= {norm(h) for h in sheet.headers if h}]


def _source_error(ws, mapping, cfg, kind):
    sources = cfg['sources']
    if not isinstance(sources, list) or not sources:
        return 'missing_scope_source', '核算范围缺少原文依据 sources', None
    corpus = ws._corpus()
    has_rule = False
    for index, source in enumerate(sources, 1):
        try:
            ref = SourceRef.model_validate(source)
        except ValueError:
            return 'invalid_scope_source', '范围出处须含材料名和逐字原文', index
        if ref.doc not in corpus:
            return 'unknown_scope_source_document', '范围出处的材料名不在本批材料或财务答复中', index
        if bad := check_quote(ref.doc, ref.quote, corpus):
            return 'scope_quote_not_found', bad, index
        if ref.page is not None:
            page = ws._page_texts(ref.doc).get(ref.page, '')
            if check_quote(ref.doc, ref.quote, {ref.doc: page}):
                return 'scope_quote_wrong_page', f'核算范围引文不在声明的第 {ref.page} 页', index

        header_sheets = header_locations(ws, ref.doc, ref.quote)
        if header_sheets and ws.workflow.get('plan', {}).get('files', {}).get(ref.doc) == 'rule':
            # Table detection can mistake a literal policy at the top of a rule sheet for a header.
            clauses = {norm(clause.quote) for clause in ws.clauses if clause.file == ref.doc}
            parts = re.split(r'[|｜\n]+', ref.quote)
            if any(norm(part) in clauses for part in [ref.quote, *parts]):
                mapped = {(Path(item.file or '').name, item.sheet) for item in ws.mapping.values()}
                header_sheets = [location for location in header_sheets if location in mapped]
        if header_sheets:
            if (Path(mapping.file or '').name, mapping.sheet) not in header_sheets:
                return 'scope_header_other_table', '所引表头属于其他来源表', index
        else:
            has_rule = True
    if not has_rule and (kind == 'objects' or cfg['filter'] or cfg['period'] is not None or cfg['window'] != 'period'):
        return 'scope_rule_needed', '表头可定位字段，核算对象、筛选或期间还须引用业务原文或财务答复', None
    return None


def _blank_policy_difference(ws, dataset, production, expected, missing):
    """Locate missing value rows without changing the independent selection."""
    fields = production.datasets[dataset].fields
    skipped = {name for name, definition in fields.items() if definition.if_empty == 'skip'}
    if not skipped:
        return None
    raw_fields = {name: definition.model_copy(update={'if_empty': None}) if name in skipped else definition
                  for name, definition in fields.items()}
    before_blank_policy = production.model_copy(update={
        'datasets': {dataset: production.datasets[dataset].model_copy(update={'fields': raw_fields})},
    })
    mapping = ws.mapping[dataset]
    data, issues = apply_mapping(before_blank_policy, ws.data_files, {dataset: mapping}, ws.period)
    if dataset not in data or any(issue.severity == 'blocking' for issue in issues):
        return None
    if Counter(str(row.src) for row in data[dataset].rows) != expected:
        return None
    missing_rows = set(missing)
    samples = []
    for row in data[dataset].rows:
        if row.src is None or str(row.src) not in missing_rows:
            continue
        columns = sorted({mapping.columns.get(name, name) for name in skipped if row.values.get(name) is None})
        if columns:
            samples.append({'file': row.src.file, 'sheet': row.src.sheet, 'row': row.src.row,
                            'empty_columns': columns})
    if samples:
        return {'code': 'scope_blank_value_policy',
                'source_columns': sorted({c for sample in samples for c in sample['empty_columns']}),
                'source_cells': samples if len(samples) <= 8 else [*samples[:4], *samples[-4:]]}
    return None


def blank_scope_witnesses(report):
    """Return physical observations only when every difference has this mechanical cause."""
    if not report.get('contract_valid'):
        return {}
    failures = [row for row in report.get('checks', []) if row['status'] == 'FAIL']
    witnesses = {}
    for row in failures:
        reason = row.get('difference', {})
        if reason.get('code') != 'scope_blank_value_policy' or not reason.get('source_cells'):
            return {}
        witnesses[row['scope']] = reason['source_cells']
    return witnesses


def check_populations(ws, rs, spec, root, by_level, *, scope_names=None):
    """Compare sets (or physical row multiplicities), without changing production data."""
    wanted = requirements(rs)
    if scope_names is not None:
        wanted = {label: wanted[label] for label in scope_names}
    supplied = spec.get('populations')
    issues, checks, contract_errors = [], [], []
    if not isinstance(supplied, dict):
        return {'status': 'FAIL', 'contract_valid': False, 'issues': ['独立复核缺少 populations 核算范围声明'], 'checks': []}
    for label, requirement in wanted.items():
        cfg = supplied.get(label)
        code = 'contract_shape'
        source_index = None
        try:
            if not isinstance(cfg, dict):
                raise ValueError(f'缺少 {label} 的独立核算范围')
            allowed = {'dataset', 'key', 'filter', 'period', 'window', 'sources'}
            if set(cfg) - allowed or not {'dataset', 'filter', 'period', 'window', 'sources'} <= set(cfg):
                raise ValueError('须显式填写 dataset/filter/period/window/sources；对象范围另填 key，不继承生产筛选')
            source_name = cfg['dataset']
            code = 'unknown_source_dataset'
            if source_name not in ws.datasets or source_name not in ws.mapping:
                raise ValueError(f'未知来源数据表 {source_name}')
            source_error = _source_error(ws, ws.mapping[source_name], cfg, requirement['kind'])
            if source_error:
                code, message, source_index = source_error
                raise ValueError(message)
            original = ws.datasets[source_name]
            key = cfg.get('key')
            code = 'object_key_field'
            if requirement['kind'] == 'objects' and (not isinstance(key, str) or key not in original.fields):
                raise ValueError(f'对象 key 必须是 {source_name} 的实际标准字段')
            code = 'wrong_physical_row_source'
            if requirement['kind'] == 'rows' and source_name != requirement['dataset']:
                raise ValueError('金额取数范围必须核对对应来源表，不能换表')
            period = cfg['period']
            source_mapping = ws.mapping[source_name]
            code = 'period_field'
            selected = set(original.fields) if requirement['kind'] == 'rows' else {key}
            if period in original.fields:
                selected.add(period)
            fields = {n: original.fields[n].model_copy(update={'if_empty': None}) for n in selected}
            columns = {n: c for n, c in source_mapping.columns.items() if n in selected}
            if period is not None:
                if not isinstance(period, str) or not period.strip() or ws.period is None:
                    raise ValueError('独立期间须指定本轮核算期间及可核对的字段')
                if period not in fields:
                    headers = [sheet.headers for sheet in ws.inv.sheets
                               if sheet.file == Path(source_mapping.file or '').name and sheet.sheet == source_mapping.sheet]
                    if len(headers) != 1 or headers[0].count(period) != 1:
                        raise ValueError('期间须为该数据集的标准字段或对应原表中唯一的逐字列名')
                    raw_period = period
                    period = 'independent_period'
                    while period in original.fields:
                        period += '_'
                    fields[period] = FieldDef(type='text')
                    columns[period] = raw_period
            code = 'window'
            independent_ds = DatasetDef(fields=fields, period=period, window=cfg['window'])
            # Raw source mapping operations (e.g. combined IDs) remain visible to
            # the reviewer. No production row filter or population patch is used.
            code = 'filter_shape'
            mapping = DatasetMapping.model_validate({
                **source_mapping.model_dump(),
                'columns': columns,
                'filter': cfg['filter'],
            })
            independent = RuleSet(name='independent_population', version='1',
                                  datasets={source_name: independent_ds}, pool={'expr': '0'}, allocation=[])
            code = 'source_mapping_or_read'
            raw, errors = apply_mapping(independent, ws.data_files, {source_name: mapping}, ws.period)
            blocking = [str(e) for e in errors if e.severity == 'blocking']
            if blocking:
                if any(e.code == 'filter_column' for e in errors if e.severity == 'blocking'):
                    code = 'filter_column'
                raise ValueError('独立来源读取失败：' + '；'.join(blocking[:3]))
            rows = raw[source_name].rows
            if requirement['kind'] == 'objects':
                code = 'object_key_empty'
                empty = [r for r in rows if r.values.get(key) in (None, '')]
                if empty:
                    raise ValueError(f'独立对象标识为空：{empty[0].src}')
                expected = {str(r.values[key]) for r in rows}
                nodes = by_level.get(label) or list((root.index or {}).get(label, {}).values())
                actual = {str(n.key) for n in nodes}
                # Only accept spelling aliases already attached unambiguously by
                # the real engine, never a guessed customer/business alias.
                from bonus_core.engine.tree import _norm_id
                attachments = {}
                for node in nodes:
                    for row in node.rows(source_name):
                        attachments.setdefault(str(row.src), set()).add(str(node.key))
                aliases = {}
                for row in rows:
                    raw_key = str(row.values[key])
                    hits = attachments.get(str(row.src), set())
                    if raw_key not in actual and len(hits) == 1:
                        canonical = next(iter(hits))
                        if (_norm_id(raw_key) == _norm_id(canonical)
                                and sum(_norm_id(k) == _norm_id(canonical) for k in actual) == 1):
                            aliases[raw_key] = canonical
                expected = {aliases.get(key, key) for key in expected}
                missing, extra = sorted(expected - actual), sorted(actual - expected)
            else:
                code = 'production_source_read'
                # Compare participating physical rows before entity distinct,
                # otherwise valid grouping would look like missing source rows.
                production_ds = original.model_copy(update={'key': None, 'link': None, 'distinct': False,
                                                             'population_from': None})
                production_rs = RuleSet(name='production_source_rows', version='1',
                                        datasets={source_name: production_ds}, pool={'expr': '0'}, allocation=[])
                production_data, production_errors = apply_mapping(
                    production_rs, ws.data_files, {source_name: ws.mapping[source_name]}, ws.period)
                blocking = [str(e) for e in production_errors if e.severity == 'blocking']
                if blocking:
                    raise ValueError('生产参与行无法验证：' + '；'.join(blocking[:3]))
                actual_rows = production_data[source_name].rows
                if original.population_from:
                    if root is None:
                        raise ValueError('派生实体来源须使用已准备的引擎对象范围')
                    keys = {str(r.values.get(original.key)) for r in root.rows(source_name)}
                    keys.update((root.index or {}).get(source_name, {}))
                    # Keep source multiplicities for active entity keys, even when
                    # the engine groups multiple physical rows into one object.
                    actual_rows = [r for r in actual_rows if str(r.values.get(original.key)) in keys]
                actual = Counter(str(r.src) for r in actual_rows)
                expected = Counter(str(r.src) for r in rows)
                missing, extra = sorted((expected - actual).elements()), sorted((actual - expected).elements())
            entry = {'scope': label, 'dataset': source_name, 'reference_rows': len(rows),
                     'expected_count': len(expected) if isinstance(expected, set) else sum(expected.values()),
                     'actual_count': len(actual) if isinstance(actual, set) else sum(actual.values()),
                     'missing': missing[:8], 'extra': extra[:8], 'status': 'FAIL' if missing or extra else 'PASS'}
            if requirement['kind'] == 'objects':
                entry['repeated_key_rows'] = len(rows) - len(expected)
                entry['verified_spelling_aliases'] = dict(list(aliases.items())[:8])
                entry['spelling_alias_count'] = len(aliases)
            elif missing and not extra:
                difference = _blank_policy_difference(ws, source_name, production_rs, expected, missing)
                if difference:
                    entry['difference'] = difference
            checks.append(entry)
            if missing or extra:
                message = f'{label} 核算范围与独立原文取数不符：缺少 {len(missing)}，多出 {len(extra)}。'
                if 'difference' in entry:
                    columns = '、'.join(entry['difference']['source_columns'])
                    message += f'差异来自「{columns}」的空值排除。按原文核对空单元格是否视同查不到，明确区分空单元格和数值 0。'
                else:
                    message += '核对本期对象与历史统计数据的各自范围，不能通过修改金额或盲目裁历史明细消除此差异'
                issues.append(message)
        except (ValueError, TypeError, KeyError) as exc:
            # These allowlisted tags are safe to send in a blind review retry. Local issues may include data values.
            error = {'scope': label, 'code': code}
            if source_index is not None:
                error['source_index'] = source_index
            if code == 'scope_header_other_table':
                mapping = ws.mapping[source_name]
                ref = SourceRef.model_validate(cfg['sources'][source_index - 1])
                error['declared_source'] = {'dataset': source_name, 'file': Path(mapping.file or '').name,
                                            'sheet': mapping.sheet}
                error['quoted_header_locations'] = [{'file': file, 'sheet': sheet} for file, sheet
                                                    in header_locations(ws, ref.doc, ref.quote)]
            contract_errors.append(error)
            issues.append(f'{label}：{exc}')
    return {'status': 'FAIL' if issues else 'PASS', 'contract_valid': not contract_errors, 'issues': issues,
            'checks': checks, 'contract_errors': contract_errors}
