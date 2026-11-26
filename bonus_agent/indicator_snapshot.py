"""Resolve a customer-level snapshot ratio against a verified historical average."""

import json
import re
from pathlib import Path
from unicodedata import normalize

from bonus_agent import facts as fx
from bonus_agent.clauses import check_quote
from bonus_agent.coefficient import interval, resolve
from bonus_agent.data_profile import _source_column_name
from bonus_agent.formula_inputs import with_rebound_requirement
from bonus_agent.indicator_history import historical_average_clause
from bonus_agent.indicator_scope import _group_header
from bonus_agent.source_cells import ppt_page_index, read_ppt_tables
from bonus_core.dsl.scope import Layout


_RATIO = re.compile(r'\s*(?:[^|]+\|\s*)?对应([^,]+),\s*([^/]+)/\1的月平均金额,\s*'
                    r'(?:查不到|找不到)\2时,\s*匹配系数(?:指定为?|设为)\s*(\d+(?:\.\d+)?)[。.]?\s*')
_EXCLUSION = r'参考(?:数据|资料|表)里有[、，, ]*但[^。；;]{1,28}里没有[^。；;]{0,36}不参与核算'


def _match(text):
    return _RATIO.fullmatch(normalize('NFKC', text))


def snapshot_ratio_clause(ws, spec):
    if not spec['target'].startswith('/variables/'):
        return None
    matches = [c for c in ws.clauses if c.id in spec['clauses'] and _match(c.quote)]
    return matches[0] if len(matches) == 1 else None


def _metric(text):
    text = fx.scope_norm(text).translate(str.maketrans('應額庫滯', '应额库滞'))
    return text.replace('的', '').replace('应收款', '应收')


def snapshot_history_clauses(ws, datasets):
    """Find historical grouping rules tied to declared snapshot workbooks."""
    files = {_metric(Path(ws.mapping[name].file).stem) for name in datasets
             if name in ws.datasets and name in ws.mapping and not ws.datasets[name].period}
    return [clause.id for clause in ws.clauses
            if (match := _match(clause.quote)) and _metric(match[2]) in files]


def _coefficient_source(ws, metric):
    """Require a unique slide title identifying both operands and a complete physical series."""
    from bonus_agent.agent import ToolError

    source_titles = {metric, metric.removeprefix('呆滞')} if metric.startswith('呆滞库存') else {metric}
    candidates = []
    for file in ws._source_version:
        if not file.lower().endswith('.pptx'):
            continue
        path = ws._file(file)
        for page in ppt_page_index(path):
            titles = []
            for text in page['context']:
                parts = re.split(r'\bVS\b', text, flags=re.IGNORECASE)
                if (len(parts) == 2 and _metric(parts[0]) in source_titles
                        and re.search(r'月平均(?:出货|销售)金额$', _metric(parts[1]))):
                    titles.append(text)
            if not titles:
                continue
            for table in read_ppt_tables(path, page['page']):
                if len(table['rows']) != 2:
                    continue
                header, values = ([[line.strip() for line in cell['lines'] if line.strip()] for cell in row['cells']]
                                  for row in table['rows'])
                width = len(header)
                if (width < 3 or not re.search(r'月平均(?:出货|销售)金额$', _metric(' '.join(values[0])))
                        or not all(len(field) == 1 for field in header[1:] + values[1:])
                        or any(fx.number(field[0]) is None for field in values[1:])):
                    continue
                try:
                    resolve([interval(field[0]) for field in header[1:]])
                except ToolError:
                    continue
                for title in titles:
                    candidates.append({'cells_from': {'file': file, 'page': page['page'], 'table': table['table']},
                                       'applies_to': title, 'rows': [{'range': f'R1C{col}', 'value': f'R2C{col}'}
                                                                   for col in range(2, width + 1)]})
    return candidates[0] if len(candidates) == 1 else None


def _coefficient_consumer(board, spec, statuses, rules, source, requested):
    """Reuse the checked physical table when the plan separates the ratio and the rate."""
    all_tasks = board.state['tasks']
    cells_from = source['cells_from']
    expected = {tuple(map(int, re.fullmatch(r'R(\d+)C(\d+)', row['value']).groups())) for row in source['rows']}
    options = []
    for task in all_tasks.values():
        if (task['scheme'] != spec['scheme'] or spec['id'] not in task['depends_on']
                or not task['target'].startswith('/variables/') or board.value(task) is not None
                or task.get('expected_scope') not in (requested, None)):
            continue
        table_deps = [all_tasks[n] for n in task['depends_on'] if all_tasks[n]['target'].startswith('/tables/')]
        for other in table_deps:
            table = fx.target_parts(other['target'])[1]
            if statuses[other['id']]['status'] != 'PASS' or getattr(rules.tables.get(table), 'purpose', None) != 'coefficient':
                continue
            bound = {(f.source_cell['row'], f.source_cell['column']) for f in board.ws.facts
                     if f.source_cell and f.file == cells_from['file'] and f.page == cells_from['page']
                     and f.source_cell.get('table') == cells_from['table']
                     and any(board.owns(other, target) and target.endswith('/1') for target in f.targets)}
            if bound == expected:
                options.append((task, table))
    return options


def snapshot_ratio_input(board, spec):
    ws = board.ws
    source = snapshot_ratio_clause(ws, spec)
    corpus = ws._corpus()
    if source is None or check_quote(source.file, source.quote, corpus):
        return None
    heading, measure, fallback = _match(source.quote).groups()
    group = _group_header(_source_column_name(heading))
    metric = _metric(measure)
    rs = ws._ruleset(spec['scheme'])
    statuses = board.statuses(with_confidence=False)
    dependencies = [board.task_spec(task) for task in spec['depends_on']]
    history = [task for task in dependencies if historical_average_clause(ws, task) and statuses[task['id']]['status'] == 'PASS']
    if len(history) != 1:
        return None
    history_name = fx.target_parts(history[0]['target'])[1]
    scope = rs.variables[history_name].scope
    layout = Layout(rs)
    if scope not in layout.aux or scope not in ws.mapping:
        return None
    key = rs.datasets[scope].key
    requested = spec.get('expected_scope')
    owners = [name for name, ds in rs.datasets.items() if rs.level(requested) and ds.key == rs.level(requested).key]
    if requested in layout.aux:
        owners.append(requested)
    anchor = ws.mapping[scope]
    column = anchor.columns[key]
    same_group_source = any((m.file, m.sheet, m.header_rows) == (anchor.file, anchor.sheet, anchor.header_rows)
                            and column in m.columns.values() for owner in owners if (m := ws.mapping.get(owner)))
    if requested != scope and not same_group_source and not any(scope in layout.reach(owner) for owner in owners):
        return None
    if _group_header(_source_column_name(ws.mapping[scope].columns[key])) != group:
        return None
    exclusions = [c for c in ws.clauses if re.search(_EXCLUSION, c.quote) and not check_quote(c.file, c.quote, corpus)]
    if len(exclusions) != 1:
        return None
    references = []
    for name, ds in rs.datasets.items():
        mapping = ws.mapping.get(name)
        if not mapping or ds.period or _metric(Path(mapping.file).stem) != metric:
            continue
        if ds.key and any(other.link == ds.key for other in rs.datasets.values()):
            continue
        keys = [f for f, col in mapping.columns.items() if f in ds.fields and ds.fields[f].type in ('id', 'text', 'enum')
                and _group_header(_source_column_name(col)) == group]
        amounts = [f for f in mapping.columns if f in ds.fields and ds.fields[f].type == 'money']
        if len(keys) == len(amounts) == 1 and (not ds.key or ds.key == keys[0]):
            references.append((name, keys[0], amounts[0]))
    if len(references) != 1:
        return None
    (reference, reference_key, amount), = references
    literal = json.dumps(reference, ensure_ascii=False)
    if any(var.scope == reference or literal in var.expr for rules in (ws._ruleset(s) for s in ws.schemes)
           for var in rules.variables.values()):
        return None
    coefficient = _coefficient_source(ws, metric)
    if coefficient is None:
        return None
    consumers = _coefficient_consumer(board, spec, statuses, rs, coefficient, requested)
    downstream = any(spec['id'] in t['depends_on'] and any(board.task_spec(d)['target'].startswith('/tables/')
                     for d in t['depends_on']) for t in board.state['tasks'].values() if t['scheme'] == spec['scheme'])
    if len(consumers) > 1 or downstream and not consumers:
        return None
    name = fx.target_parts(spec['target'])[1] + '_intervals'
    table_task = spec['id'] + '_intervals'
    if not consumers and (name in rs.tables or name in rs.variables or name in rs.field_owner()
                          or table_task in board.state['tasks']):
        return None
    mapping = ws.mapping[reference]
    changes = {'link': key, 'unmatched': 'ignore', 'unmatched_basis': exclusions[0].id}
    if rs.datasets[reference].key:
        changes['key'] = None
    if key != reference_key:
        if key in mapping.columns and mapping.columns[key] != mapping.columns[reference_key]:
            return None
        field = rs.datasets[scope].fields[key].model_dump(exclude_defaults=True)
        changes['fields'] = {key: {**field, 'column': mapping.columns[reference_key]}}
    contract = {'dataset': reference, 'fields': [key, amount], 'link': key}
    original_link = rs.datasets[reference].link or rs.datasets[reference].key
    requirements = with_rebound_requirement(spec, contract, original_link)
    if requirements is None:
        return None
    aggregate = 'single_value' if rs.datasets[reference].key else 'sum'
    ratio = f'ratio({aggregate}({amount}, of: {literal}), {history_name})'
    missing = f'count(of: {literal}) == 0'
    citations = [{'doc': source.file, 'quote': source.quote}]
    proposal = {'scheme': spec['scheme'], 'task': spec['id'], 'formulas': [],
                'datasets': [{'operation': 'update', 'args': {'name': reference, 'changes': changes}}]}
    if consumers:
        consumer, name = consumers[0]
        dependent_inputs = with_rebound_requirement(consumer, contract, original_link)
        if dependent_inputs is None:
            return None
        proposal['formulas'].append({'task': spec['id'], 'expected_scope': scope, 'data_requirements': requirements,
                                    'value': {'scope': scope, 'expr': f'if({missing}, 0, {ratio})', 'sources': citations}})
        rate_formula = fx.target_parts(spec['target'])[1]
    else:
        consumer, dependent_inputs, rate_formula = spec, requirements, ratio
        proposal.update(coefficient={**coefficient, 'scheme': spec['scheme'], 'name': name},
                        coefficient_plan={'id': table_task, 'scheme': spec['scheme'], 'target': '/tables/' + name,
                                          'title': spec['title'] + '档位', 'clauses': [source.id]})
    parameter = consumer['target'] + ' 缺少记录时的系数'
    proposal['formulas'].append({
        'task': consumer['id'], 'expected_scope': scope, 'data_requirements': dependent_inputs,
        **({'depends_on': [*spec['depends_on'], table_task]} if not consumers else {}),
        'value': {'scope': scope, 'expr': f'if({missing}, {fallback}, tier_rate({name}, {rate_formula}))', 'sources': citations},
        'facts': [{'name': parameter, 'file': source.file, 'quote': source.quote, 'value': fallback}],
        'bindings': [{'name': parameter, 'targets': [spec['scheme'] + '::' + consumer['target'] + '/expr']}],
    })
    return {'source': {'file': mapping.file, 'sheet': mapping.sheet}, 'contract': contract, 'scope': scope,
            'candidate_call': proposal}
