"""Separate evidenced current-period and historical inputs before writing a mixed funding formula."""
import copy
import json
import re

from bonus_agent.clauses import check_quote
from bonus_agent.facts import scope_norm, target_parts
from bonus_agent.formula_inputs import input_failures
from bonus_agent.indicator_history import _sales_sources, historical_average_quote, history_average_input


def upstream_tasks(tasks, ident):
    pending = list(tasks[ident]['depends_on'])
    seen = set()
    while pending:
        other = pending.pop()
        if other in seen or other not in tasks:
            continue
        seen.add(other)
        pending.extend(tasks[other]['depends_on'])
    return seen


def _current_sales(quote):
    text = scope_norm(quote)
    return bool(re.search(r'(?:本月|当月|核算月)(?:实际)?(?:销售额|销售金额|出货金额)', text)
                and re.search(r'(?:本月|当月|核算月)预算', text))


def _available_requirement(ws, row, window):
    definition, mapping = ws.datasets.get(row['dataset']), ws.mapping.get(row['dataset'])
    if definition is None and window == 'year_to_date':
        # The historical adapter verifies the physical source and coverage before a formula is saved.
        return row['period_field'] in row['fields'] and len(row['fields']) > 1
    return (definition is not None and mapping is not None and definition.window == window
            and definition.period == row['period_field'] and set(row['fields']) <= definition.fields.keys() & mapping.columns.keys()
            and ('link' not in row or definition.link == row['link']))


def pool_input_gap(board, scheme=None):
    ws = board.ws
    tasks = board.state['tasks']
    for root in tasks.values():
        if root['target'] != '/pool' or (scheme and root['scheme'] != scheme) or board.value(root) is not None:
            continue
        applicable = {c.id: c for c in ws.clauses if c.id in root['clauses']}
        periods = {c.id: c for c in applicable.values() if _current_sales(c.quote)}
        history = {c.id: c for c in applicable.values() if historical_average_quote(c.quote)}
        if not periods or not history:
            continue
        corpus = ws._corpus()
        if any(check_quote(c.file, c.quote, corpus) for c in [*periods.values(), *history.values()]):
            continue
        ancestors = [tasks[n] for n in upstream_tasks(tasks, root['id'])
                     if tasks[n]['scheme'] == root['scheme'] and tasks[n]['target'].startswith('/variables/')]
        shapes = [('period', periods), ('year_to_date', history)]
        by_window = {window: {r['dataset'] for t in ancestors if clauses.keys() & set(t['clauses']) and t.get('expected_scope')
                              for r in t.get('data_requirements', ()) if r.get('window') == window and r['fields']
                              and r.get('period_field') and _available_requirement(ws, r, window)} for window, clauses in shapes}
        current, historical = by_window['period'], by_window['year_to_date']
        wide_roots = {r['dataset'] for r in root.get('data_requirements', ()) if r.get('window') == 'year_to_date'}
        if current and historical and not current & (historical | wide_roots):
            continue
        issues = []
        for window, clauses in shapes:
            if by_window[window]:
                continue
            inputs = [t for t in ancestors if clauses.keys() & set(t['clauses'])]
            if not inputs:
                issues.append(f'{window}：依赖链缺少引用条款 {", ".join(clauses)} 的前置变量任务')
            for task in inputs:
                if not task.get('expected_scope'):
                    issues.append(f'{task["id"]}：缺少核算作用域 expected_scope')
                requirements = [r for r in task.get('data_requirements', ()) if r.get('window') == window]
                if not requirements:
                    issues.append(f'{task["id"]}：缺少统计期间为 {window} 的输入声明')
                for requirement in requirements:
                    if not requirement.get('period_field') or not requirement.get('fields'):
                        issues.append(f'{task["id"]}：{requirement["dataset"]} 缺少期间字段或所需字段')
                    elif not _available_requirement(ws, requirement, window):
                        failures = input_failures(ws, {**task, 'data_requirements': [requirement]}, None)
                        details = [row['detail'] for row in failures] or [f'「{requirement["dataset"]}」的输入声明未通过复验']
                        issues.extend(f'{task["id"]}：{detail}' for detail in details)
        if shared := current & (historical | wide_roots):
            issues.append(f'统计期间冲突：{", ".join(sorted(shared))} 同时承担当期和累计输入')
        return {'kind': 'pool_inputs', 'scheme': root['scheme'], 'target': '/pool', 'task_target': '/pool', 'task': root['id'],
                'inputs': [{'window': window, 'clauses': [{'id': c.id, 'file': c.file, 'quote': c.quote}
                                                            for c in clauses.values()],
                            'upstream_datasets': sorted(by_window[window])} for window, clauses in shapes],
                'root_requirements': root.get('data_requirements', ()), 'issues': issues,
                'message': f'奖金池任务 {root["id"]} 同时包含当月销售预算和年初累计月均金额。'
                           '复用原任务 ID，将这两类数据拆入各自的前置变量任务，声明关联对象、字段、期间和独立输入。'
                           '奖金池按任务 ID 依赖这些前置项，完整金额仍须后续计算和独立复核。'}
    return None


def _current_sale(ws, root, clauses):
    """Use the current amount and relationship already proven by data preparation."""
    datasets = ws._scheme_datasets(root['scheme'])
    declared = {row['dataset']: row for row in root.get('data_requirements', ())}
    numerators = [re.split(r'除以|[/÷]|预算', row['quote'], maxsplit=1)[0] for row in clauses]
    wording = scope_norm(' '.join(numerators)).replace('销售额', '销售金额')
    matches = []
    for name, detail, mapping, amount in _sales_sources(ws, datasets, wording):
        declared_row = declared.get(name)
        if not declared_row or not detail.link or declared_row.get('period_field') != detail.period:
            continue
        key = detail.link
        if not {amount, key, detail.period} <= set(declared_row['fields']):
            continue
        if not _available_requirement(ws, {**declared_row, 'window': 'period'}, 'period'):
            continue
        for scope, parent in datasets.items():
            origin = ws.mapping.get(scope)
            if (parent.key == key and parent.window == 'period' and origin
                    and (origin.file, origin.sheet, origin.header_rows) == (mapping.file, mapping.sheet, mapping.header_rows)
                    and origin.columns.get(key) == mapping.columns[key]
                    and (origin.combine or {}).get(key) == (mapping.combine or {}).get(key)):
                contract = {'dataset': name, 'fields': [amount, detail.period, key], 'window': 'period',
                            'period_field': detail.period, 'link': key}
                matches.append((scope, amount, contract))
    return matches[0] if len(matches) == 1 else None


def _prepare_candidate(ws, board, root, gap):
    from bonus_agent.agent import ToolError
    from bonus_agent.construction import TaskBoard
    from bonus_agent.indicator_snapshot import snapshot_history_clauses

    scheme, ident = root['scheme'], root['id']
    result = {'task': ident, 'scheme': scheme, 'target': root['target'], 'status': 'FAIL', 'saved': False}
    current, history = gap['inputs']
    sale = _current_sale(ws, root, current['clauses'])
    if not sale or len(history['clauses']) != 1:
        return {**result, 'issues': ['未唯一核对当期销售的原表、金额列、期间和关联实体，或累计条款存在多种来源。',
                                    *gap['issues']]}
    scope, amount, contract = sale
    sale_name = contract['dataset']
    sale_mapping = ws.mapping[sale_name]
    average_ids = [history['clauses'][0]['id']]
    average_ids += [clause.id for clause in ws.clauses
                    if clause.id in root['clauses'] and '月平均' in clause.quote and '对应' in clause.quote]
    average_ids += snapshot_history_clauses(ws, [r['dataset'] for r in gap['root_requirements']])
    current_task = {'id': f'{ident}:current_sales', 'scheme': scheme, 'target': '/variables/current_sales_amount',
                    'title': '当期销售金额（达成率取数基础）', 'expected_scope': scope,
                    'clauses': [row['id'] for row in current['clauses']], 'data_requirements': [contract]}
    history_task = {'id': f'{ident}:historical_average', 'scheme': scheme, 'target': '/variables/historical_average_amount',
                    'title': '年初累计月均出货金额', 'expected_scope': scope, 'clauses': list(dict.fromkeys(average_ids))}
    parts = [current_task, history_task]
    owned = {s['target'] for s in board.state['tasks'].values() if s['scheme'] == scheme}
    fields = {field for ds in ws.datasets.values() for field in ds.fields}
    rules = ws._merged(scheme)
    if any(t['id'] in board.state['tasks'] or t['target'] in owned or
           target_parts(t['target'])[1] in fields | rules.get('variables', {}).keys() | rules.get('tables', {}).keys()
           for t in parts):
        return {**result, 'issues': ['当期或历史输入的变量名已有归属，需按现有任务 ID 修正依赖。', *gap['issues']]}
    try:
        ws._check_source_version()
        historical = history_average_input(board, history_task)
        if not historical or not (call := historical.get('candidate_call')):
            return {**result, 'issues': (historical or {}).get('issues') or ['未取得可核对的累计原表与关联字段。'],
                    **({'input_failures': historical['input_failures']} if historical and 'input_failures' in historical else {})}
        history_formula, = call['formulas']
        history_task.update(expected_scope=history_formula['expected_scope'], data_requirements=history_formula['data_requirements'])
        updated = {**root, 'depends_on': [*root['depends_on'], *(t['id'] for t in parts)],
                   'data_requirements': [{**r, 'window': 'period'} if r['dataset'] == sale_name else r
                                         for r in gap['root_requirements']]}
        candidate = copy.deepcopy(ws, {id(ws.llm): ws.llm})
        candidate.plan_formula_tasks([updated, *parts])
        current_formula = {
            'task': current_task['id'],
            'value': {'scope': scope, 'expr': f'sum({amount}, of: {json.dumps(sale_name, ensure_ascii=False)})',
                      'sources': [{'doc': c['file'], 'quote': c['quote']} for c in current['clauses']]},
        }
        check = candidate.submit_construction_repair(**{**call, 'formulas': [current_formula, history_formula]})
        if not check.get('saved'):
            return {**result, 'issues': check['issues'], 'checked': check['checked']}
        if outstanding := pool_input_gap(TaskBoard(candidate), scheme):
            return {**result, 'issues': outstanding['issues']}
        for name in ws.workflow['receipts']:
            if name.startswith('approved:') and ws._flow().current(name) and not candidate._flow().current(name):
                raise ToolError('候选会使已有财务确认失效')
        candidate._check_source_version()
    except (ToolError, ValueError, KeyError, TypeError, OSError) as exc:
        return {**result, 'issues': [str(exc)]}
    ws.__dict__.update(candidate.__dict__)
    return {**result, 'status': 'PASS', 'saved': True, 'checked': check['checked'],
            'current': {'source': {'file': sale_mapping.file, 'sheet': sale_mapping.sheet}, 'contract': contract},
            'history': {k: historical[k] for k in ('source', 'window', 'contract', 'compared_group_months', 'historical_group_months')}}


def prepare_pool_inputs(ws):
    """Try the source-backed plan once per version and save only a verified replacement."""
    from bonus_agent.construction import TaskBoard

    flow = ws._flow()
    if not flow.current('data'):
        return None
    board = TaskBoard(ws)
    gap = pool_input_gap(board)
    if not gap or any(row['upstream_datasets'] for row in gap['inputs']):
        return None
    root = board.task_spec(gap['task'])
    version = flow.version()
    if board.state.get('pool_input_preflight', {}).get(root['id']) == version:
        return None
    result = _prepare_candidate(ws, board, root, gap)
    TaskBoard(ws).state.setdefault('pool_input_preflight', {})[root['id']] = version
    return result
