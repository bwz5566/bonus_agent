"""Compile separate amount-rate tables from complete, verified source columns."""
import copy
import re
from collections import defaultdict
from decimal import Decimal
from unicodedata import normalize

from bonus_agent import facts as fx
from bonus_agent.clauses import check_quote
from bonus_agent.coefficient import interval
from bonus_agent.semantics import preserves_negative, stated_modes
from bonus_agent.source_cells import ppt_page_index, read_ppt_tables
from bonus_core.dsl.parser import Call, Const, ExprError, Name, names, parse, walk


def _rate_columns(ws, spec):
    by_name = defaultdict(list)
    for fact in ws.facts:
        if fact.name in spec['facts']:
            by_name[fact.name].append(fact)
    if set(by_name) != set(spec['facts']) or any(len(rows) != 1 for rows in by_name.values()):
        return None
    facts = [rows[0] for rows in by_name.values()]
    if not facts or any(not f.source_cell or not f.page or f.component != 'literal'
                        or (f.unit not in ('%', '‰') and not normalize('NFKC', f.value).strip().endswith(('%', '‰')))
                        or not f.file.lower().endswith('.pptx') or not f.source_hash
                        or f.source_hash != ws._source_version.get(f.file) for f in facts):
        return None
    locations = {(f.file, f.page, f.source_cell.get('table')) for f in facts}
    if len(locations) != 1:
        return None
    file, page, table = next(iter(locations))
    groups = defaultdict(dict)
    for fact in facts:
        cell = fact.source_cell
        row, col = cell.get('row'), cell.get('column')
        if (type(row) is not int or row < 2 or type(col) is not int or col < 2 or cell.get('line', 1) != 1
                or row in groups[col]):
            return None
        groups[col][row] = fact
    if not 2 <= len(groups) <= 6 or not 2 <= len(facts) <= 64:
        return None
    return {'file': file, 'page': page, 'table': table}, groups


def _source_rows(ws, source, groups):
    path = ws._file(source['file'])
    table = next(t for t in read_ppt_tables(path, source['page']) if t['table'] == source['table'])
    header, *body = table['rows']
    labels = {c['column']: ' '.join(x.strip() for x in c['lines'] if x.strip()) for c in header['cells']}
    if any(not labels.get(col) for col in groups) or len({fx.scope_norm(labels[col]) for col in groups}) != len(groups):
        return None
    if any(set(columns) != {row['row'] for row in body} for columns in groups.values()):
        return None
    corpus = ws._corpus()
    bands = []
    for row in body:
        index = row['row']
        cells = {c['column']: [x.strip() for x in c['lines'] if x.strip()] for c in row['cells']}
        if len(cells.get(1, ())) != 1:
            return None
        for col, column in groups.items():
            fact = column[index]
            if (len(cells.get(col, ())) != 1 or fx.number(cells[col][0]) != fx.numeric(fact)
                    or fx.numeric(fact) is None or check_quote(fact.file, fact.quote, corpus)):
                return None
        bands.append((index, interval(cells[1][0])))
    bands.sort(key=lambda pair: pair[1]['lower'] if pair[1]['lower'] is not None else Decimal('-Infinity'))
    if (bands[0][1]['lower'] is not None or bands[0][1]['upper'] is None or bands[0][1]['upper'] <= 0
            or bands[-1][1]['upper'] is not None
            or any(b['lower'] is None or b['lower'] != a['upper'] for (_, a), (_, b) in zip(bands, bands[1:]))):
        return None
    if {b - a for (a, _), (b, _) in zip(bands, bands[1:])} not in ({1}, {-1}):
        return None
    scope = ppt_page_index(path)[source['page'] - 1]['scope']
    return bands, labels, scope


def _members(board, spec, groups, labels, statuses):
    scheme = spec['scheme']
    tasks = board.state['tasks']
    name = fx.target_parts(spec['target'])[1]
    bound = [col for col, rows in groups.items() if any(board.owns(spec, t) for f in rows.values() for t in f.targets)]
    if len(bound) > 1:
        return None
    original = bound[0] if bound else min(groups)
    reserved = {fx.target_parts(t['target'])[-1] for t in tasks.values() if t['scheme'] == scheme}
    reserved.update(field for d in board.ws.datasets.values() for field in d.fields)
    result = {}
    for col, entries in sorted(groups.items()):
        facts = [f.name for f in entries.values()]
        siblings = [task for task in tasks.values() if task['id'] != spec['id'] and task['scheme'] == scheme
                    and task['target'].startswith('/tables/') and set(task['facts']) == set(facts)]
        if len(siblings) > 1 or (siblings and col == original):
            return None
        if col == original:
            member = spec
        elif siblings:
            member, = siblings
            if (statuses[member['id']]['status'] != 'PENDING' or board.value(member) is not None
                    or not set(member['clauses']) <= set(spec['clauses'])):
                return None
        else:
            ident = f'{spec["id"]}.C{col}'
            table = f'{name}_c{col}'
            if ident in tasks or table in reserved:
                return None
            member = {**spec, 'id': ident, 'target': '/tables/' + table.replace('~', '~0').replace('/', '~1')}
        result[col] = {**member, 'facts': facts, 'title': labels[col] + '阶梯费率'}
    return result


def prepare(board, spec):
    from bonus_agent.agent import ToolError
    from bonus_agent.construction import TaskBoard

    ws = board.ws
    if not spec['target'].startswith('/tables/') or not (evidence := _rate_columns(ws, spec)):
        return None
    if ws._flow().current('approved:' + spec['scheme']):
        return None
    consumers = [t for t in board.state['tasks'].values() if spec['id'] in t['depends_on']]
    if not consumers or any(t['scheme'] != spec['scheme'] or not t['target'].startswith('/variables/')
                            or board.value(t) is not None for t in consumers):
        return None
    clauses = [c for c in ws.clauses if c.id in spec['clauses'] and not check_quote(c.file, c.quote, ws._corpus())]
    if set().union(*(stated_modes(c.quote) for c in clauses)) != {'excess'}:
        return None
    policy = next(c for c in clauses if stated_modes(c.quote) == {'excess'})
    downstream_clauses = {c for t in consumers for c in t['clauses']}
    negatives = [c for c in ws.clauses if c.id in downstream_clauses and '阶梯' in c.quote
                 and preserves_negative(c.quote) and not check_quote(c.file, c.quote, ws._corpus())]
    draft = board.value(spec) or {}
    if draft and (draft.get('kind') != 'tier' or draft.get('mode') != 'excess'):
        return None
    table_guard = draft.get('applies_when')
    if table_guard:
        rules = ws._ruleset(spec['scheme'])
        known = set(rules.variables) | {f for d in rules.datasets.values() for f in d.fields}
        try:
            unresolved = names(parse(table_guard)) - known
        except ExprError:
            unresolved = None
        if not unresolved:
            return None  # A working row-level condition must be preserved and validated jointly.
    source, groups = evidence
    try:
        prepared = _source_rows(ws, source, groups)
    except (ToolError, ValueError, KeyError, StopIteration):
        return None
    if not prepared:
        return None
    bands, labels, scope = prepared
    statuses = board.statuses(with_confidence=False)
    if statuses[spec['id']]['status'] != 'PENDING' or not (members := _members(board, spec, groups, labels, statuses)):
        return None
    candidate = copy.deepcopy(ws, {id(ws.llm): ws.llm})
    result = {'task': spec['id'], 'scheme': spec['scheme'], 'target': spec['target'], 'status': 'FAIL', 'saved': False,
              'source': source, 'columns': {col: labels[col] for col in members}}
    try:
        amended = [{**t, 'depends_on': list(dict.fromkeys([*t['depends_on'], *(m['id'] for m in members.values())]))}
                   for t in consumers]
        for member in members.values():
            member['clauses'] = list(dict.fromkeys([*member['clauses'], *(c.id for c in negatives)]))
        book = TaskBoard(candidate)
        book.plan([*members.values(), *amended])
        classification = {t['target']: _classification_terms(board, t) for t in consumers}
        book.state.setdefault('tier_rate_groups', {})[spec['id']] = {
            'scheme': spec['scheme'],
            'members': {m['id']: {'target': m['target'], 'label': labels[col]} for col, m in members.items()},
            'consumers': classification, 'source': source,
        }
        registered = candidate.record_parameter_cells(**source, applies_to=scope,
                                                       cells=[{'row': row, 'column': 1} for row, _ in bands])
        if registered['status'] != 'PASS':
            return {**result, 'issues': registered.get('failed', ['未能登记阶梯区间原格'])}
        below_first = ('first' if negatives or any(preserves_negative(c.quote) for c in clauses)
                       else draft.get('below_first', 'zero'))
        lower_inclusive = draft.get('lower_inclusive', True)
        checked = []
        for col, member in members.items():
            name = fx.target_parts(member['target'])[1]
            rows = [{'lower': f'R{row}C1', 'value': f'R{row}C{col}'} for row, _ in bands]
            candidate.compile_table(name, rows, scheme=spec['scheme'], kind='tier', cells_from=source, applies_when='',
                                    algorithm_basis={'file': policy.file, 'quote': policy.quote},
                                    below_first=below_first, lower_inclusive=lower_inclusive)
            report = book.check(member['id'], include_next=False)
            if report['status'] != 'PASS':
                return {**result, 'issues': report['issues'], 'failed_task': member['id']}
            expected = [(band['lower'] or Decimal(0), fx.numeric(groups[col][row])) for row, band in bands]
            actual = [tuple(Decimal(str(v)) for v in step) for step in book.value(member)['steps']]
            if actual != expected:
                return {**result, 'issues': [f'「{labels[col]}」的引擎阶梯与原始列核对不一致']}
            checked.append({'task': member['id'], 'target': member['target'], 'status': 'PASS', 'column': col})
        candidate._check_source_version()
    except (ToolError, ValueError, KeyError, TypeError) as exc:
        return {**result, 'issues': [str(exc)]}
    ws.__dict__.update(candidate.__dict__)
    return {**result, 'status': 'PASS', 'saved': True, 'checked': checked, 'consumers': [t['id'] for t in amended]}


def _classification_terms(board, spec):
    clauses = {c.id: c for c in board.ws.clauses}
    tasks = (board.task_spec(ident) for ident in spec['depends_on'])
    result = []
    for task in tasks:
        parts = fx.target_parts(task['target'])
        if parts[0] != 'variables':
            continue
        for cid in task['clauses']:
            c = clauses[cid]
            if (re.search(r'新(?:项目|客户)', c.quote) and re.search(r'(?:老|旧)(?:项目|客户)', c.quote)
                    and '判定' in c.quote and not check_quote(c.file, c.quote, board.ws._corpus())):
                result.append(parts[1])
                break
    return result


def rate_groups(state, spec):
    return [g for g in state.get('tier_rate_groups', {}).values()
            if g['scheme'] == spec['scheme'] and spec['target'] in g['consumers']]


def dispatch_issues(board, spec, rules, expr):
    """A compiled rate column alone gives no permission to select it for every row."""
    groups = rate_groups(board.state, spec)
    if not groups:
        return []

    def expressions(root, seen):
        yield root
        for name in names(root) & rules.variables.keys():
            if name not in seen:
                seen.add(name)
                yield from expressions(parse(rules.variables[name].expr), seen)

    def uses(root):
        return {n.args[0].id for expr in expressions(root, set()) for n in walk(expr)
                if isinstance(n, Call) and n.fn == 'tier' and n.args and isinstance(n.args[0], Name)}

    try:
        root = parse(expr)
        used = uses(root)
        branches = [(n.args[0], uses(n.args[1]), uses(n.args[2])) for tree in expressions(root, set()) for n in walk(tree)
                    if isinstance(n, Call) and n.fn == 'if' and len(n.args) == 3 and not isinstance(n.args[0], Const)]
    except ExprError:
        return []  # The ordinary syntax check reports this first.
    issues = []
    for group in groups:
        columns = {fx.target_parts(t['target'])[1]: t['label'] for t in group['members'].values()}
        missing = set(columns) - used
        classifiers = set(group['consumers'][spec['target']])
        distinguished = set()
        for condition, a, b in branches:
            tested = set().union(*(names(expr) for expr in expressions(condition, set())))
            if (not classifiers or classifiers & tested) and (a - b) & columns.keys() and (b - a) & columns.keys():
                distinguished.update((a ^ b) & columns.keys())
        if missing or distinguished != set(columns):
            labels = '、'.join(columns.values())
            issues.append(f'algorithm.tier_dispatch：{spec["target"]}须根据适用条件选择原表各费率列（{labels}）。'
                          f'请用核实后的分类字段 {"、".join(sorted(classifiers)) or "及其规则"} 分别调用对应阶梯，不能遗漏、合并或重复套用。')
    return issues
