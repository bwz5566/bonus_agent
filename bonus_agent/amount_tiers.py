"""Prepare documented monthly age and progressive amount inputs from physical sources."""
import json
import re
from collections import defaultdict
from unicodedata import normalize

from bonus_agent import facts as fx
from bonus_agent.clauses import check_quote
from bonus_agent.coefficient import interval
from bonus_agent.data_profile import _source_column_name
from bonus_agent.formula_inputs import with_requirements
from bonus_agent.semantics import preserves_negative
from bonus_agent.source_cells import ppt_page_index, read_ppt_tables
from bonus_core.dsl.parser import Call, ExprError, Name, Str, parse


_BASIS = re.compile(r'([^，。；]{1,24}阶梯[^，。；]{0,12}|阶梯[^，。；]{1,16})按([^，。；]{1,16})的([^，。；]{1,24}?)(?:计算|算)[。.]?')
_AGE = re.compile(r'(?:新老项目:\s*)?销售单的?月份与(首发日期|首次发货日期)的?月份对比,\s*'
                  r'(?:相差)?月数(大于|不少于)\s*(\d+)\s*个月\s*,?\s*判定为(老项目|旧项目),\s*否则为(新项目)[。.]?')
_TITLE = re.compile(r'(.+?)事业部(?:\s*\[[^\]]+\])?\s*销售提成总包计算\s*\(([^()]+)\)')


def _rule_text(text):
    return re.sub(r'\s+', '', normalize('NFKC', text).lower().translate(fx._SCOPE_SCRIPT))


def _literal(value):
    return json.dumps(value, ensure_ascii=False)


def _sources(clauses):
    return [{'doc': clause.file, 'quote': clause.quote} for clause in clauses]


def amount_tier_clause(ws, spec):
    if not spec['target'].startswith('/variables/'):
        return None
    name = fx.target_parts(spec['target'])[1]
    return next((c for c in ws.clauses if c.id in spec['clauses']
                 and (matched := _BASIS.fullmatch(_rule_text(c.quote)))
                 and matched[1] == fx.scope_norm(name)), None)


def _monthly_age(ws, rules, owner, entity, verified):
    groups = defaultdict(list)
    for clause in verified:
        if matched := _AGE.fullmatch(_rule_text(clause.quote)):
            groups[matched.groups()].append(clause)
    if len(groups) != 1 or not ws.period or ws.period.months != 1:
        return None
    (heading, operator, months, old, young), sources = next(iter(groups.items()))
    inputs = [(name, field, ds) for name, ds in rules.datasets.items()
              if name in ws.mapping and name != owner and ds.link == entity.key
              and ds.period and ds.window == 'period' and not ws.mapping[name].unpivot
              for field, column in ws.mapping[name].columns.items()
              if field in ds.fields and ds.fields[field].type == 'date'
              and fx.scope_norm(_source_column_name(column)) == heading]
    if len(inputs) != 1:
        return None
    dataset, field, ds = inputs[0]
    requirement = {'dataset': dataset, 'fields': [entity.key, field], 'link': entity.key,
                   'period_field': ds.period, 'window': 'period'}
    comparison = '>' if operator == '大于' else '>='
    expr = f'if(months_between(single_value({field}, of: {_literal(dataset)}), period_end()) {comparison} {months}, {_literal(old)}, {_literal(young)})'
    return requirement, sources, months, old, young, expr


def _age_task(board, spec, scope, age):
    requirement, sources, months, _, _, expr = age
    tasks = [task for task in board.state['tasks'].values()
             if task['scheme'] == spec['scheme'] and task['target'].startswith('/variables/')
             and (fx.target_parts(task['target'])[1] == '新老项目' or
                  (set(task['clauses']) & {c.id for c in sources} and any(
                     r['dataset'] == requirement['dataset'] and set(requirement['fields']) <= set(r['fields'])
                     for r in task.get('data_requirements', ()))))
             and task.get('expected_scope') in (None, scope)]
    if len(tasks) > 1:
        return None
    task = tasks[0] if tasks else {'id': f'{spec["id"]}:age', 'scheme': spec['scheme'],
                                 'target': '/variables/新老项目', 'title': '首发月份分类'}
    if (requirements := with_requirements(task, requirement)) is None:
        return None
    if value := board.value(task):
        if value.get('scope') != scope or value.get('expr', '').replace('period_start()', 'period_end()') != expr:
            return None
        return task, None
    task = {**task, 'expected_scope': scope, 'data_requirements': requirements, 'clauses': [c.id for c in sources]}
    ledger = [f for f in board.ws.facts if fx.numeric(f) == fx.number(months)
              and any(f.file == c.file and fx.norm(f.quote) == fx.norm(c.quote) for c in sources)]
    facts = [] if ledger else [{'name': f'{task["target"]} 月数阈值', 'file': sources[0].file,
                                'quote': sources[0].quote, 'value': months + '个月'}]
    item = {'task': task['id'], 'value': {'scope': scope, 'expr': expr, 'sources': _sources(sources)}, 'facts': facts}
    return task, item


def _region_family(value):
    token = fx.scope_norm(value)
    token = re.sub(r'(?:[一二三四五六七八九十\d]+组|地区|区|部|课)$', '', token)
    acronyms = re.findall(r'[a-z][a-z0-9]+', token)
    if acronyms:
        return acronyms[0]
    # 美加 is a geographic label covering the US and Canada.
    return {'美加': '美洲'}.get(token, token)


def _rate_sources(ws, old, young):
    found = defaultdict(list)
    for path in ws.inv.files:
        if path.suffix.lower() != '.pptx':
            continue
        for page in ppt_page_index(path):
            if page['table_count'] != 1:
                continue
            titles = [t for line in page['context'] if (t := _TITLE.fullmatch(_rule_text(line)))]
            if len(titles) != 1:
                continue
            division, caption = titles[0].groups()
            [source] = read_ppt_tables(path, page['page'])
            header, *body = source['rows']
            headings = [fx.scope_norm(' '.join(cell['lines'])) for cell in header['cells']]
            if len(headings) != 3 or '销售额阶梯' not in headings[0] or not 2 <= len(body) <= 24:
                continue
            columns = {label: [i for i, heading in enumerate(headings[1:], 2)
                               if (label in heading or label == old and '老客户' in heading)] for label in (old, young)}
            if any(len(i) != 1 for i in columns.values()) or columns[old] == columns[young]:
                continue
            rows = []
            for row in body:
                values = [[line.strip() for line in cell['lines'] if line.strip()] for cell in row['cells']]
                if (len(values) != 3 or any(len(v) != 1 for v in values)
                        or not (band := interval(values[0][0]))
                        or any(not v[0].endswith(('%', '‰')) or fx.number(v[0]) is None for v in values[1:])):
                    break
                rows.append((row['row'], band))
            else:
                rows.sort(key=lambda r: r[1]['lower'] if r[1]['lower'] is not None else -1)
                if (rows[0][1]['lower'] is not None or rows[-1][1]['upper'] is not None
                        or any(a['upper'] != b['lower'] for (_, a), (_, b) in zip(rows, rows[1:]))
                        or {b - a for (a, _), (b, _) in zip(rows, rows[1:])} not in ({1}, {-1})):
                    continue
                found[division, _region_family(caption)].append({
                    'source': {'file': path.name, 'page': page['page'], 'table': source['table']},
                    'caption': page['scope'], 'rows': [r for r, _ in rows],
                    'columns': {label: hits[0] for label, hits in columns.items()},
                    'new_only': any('按照新项目的奖励标准执行' in fx.scope_norm(line) for line in page['context']),
                })
    return found


def _route_field(board, spec, owner, scope, entity, verified, statuses):
    ws = board.ws
    region = [f for f, column in ws.mapping[owner].columns.items() if f in entity.fields
              and fx.scope_norm(_source_column_name(column)) == '区域' and entity.fields[f].type in ('text', 'enum')]
    if len(region) != 1:
        return None
    field, = region
    # An explicit exception in the tier rule must be implemented by the verified region classifier.
    overrides = [m[1] for c in verified if c.id in spec['clauses']
                 and (m := re.match(r'([^，。；]+?)的阶梯提成率.{0,20}?用\1的表', fx.scope_norm(c.quote)))]
    if not overrides:
        return field, None, None
    routes = []
    for task in board.state['tasks'].values():
        if (task['scheme'] != spec['scheme'] or not task['target'].startswith('/variables/')
                or statuses[task['id']]['status'] != 'PASS'):
            continue
        value = board.value(task)
        if not value or value.get('scope') != scope:
            continue
        try:
            root = parse(value['expr'])
        except ExprError:
            continue
        if (isinstance(root, Call) and root.fn == 'if' and len(root.args) == 3
                and isinstance(label := root.args[1], Str) and root.args[2] == Name(field)
                and any(fx.scope_norm(label.value).split('-')[-1] == name for name in overrides)
                and any(re.search(r'归类[^为]*为' + re.escape(fx.scope_norm(label.value)) + r'[。.]?$',
                                  fx.scope_norm(c.quote)) for c in verified if c.id in task['clauses'])):
            routes.append((field, label.value, task))
    return routes[0] if len(routes) == 1 else None


def _tier_routes(board, spec, scope, owner, entity, verified, statuses, age, age_task):
    ws = board.ws
    route = _route_field(board, spec, owner, scope, entity, verified, statuses)
    if not route:
        return None
    field, exception, task = route
    present = {v for v in ws._values(owner, field) if isinstance(v, str) and v}
    categories = present | ({exception} if exception else set())
    old, young = age[3:5]
    sources = _rate_sources(ws, old, young)
    if not sources or not categories:
        return None
    grouped = defaultdict(list)
    for value in sorted(categories):
        division, separator, label = fx.scope_norm(value).partition('-')
        candidates = (sources.get((division, _region_family(label))) if separator else
                      [s for (_, family), group in sources.items() if family == _region_family(division) for s in group])
        if not candidates or len(candidates) != 1:
            return {'status': 'FAIL', 'saved': False, 'task': spec['id'], 'scheme': spec['scheme'], 'target': spec['target'],
                    'issues': [f'原表与实际区域「{value}」未形成唯一对应，请核对制度区域标题和数据编码。']}
        src, = candidates
        grouped[tuple(src['source'].values())].append((value, src))
    route_name = fx.target_parts(task['target'])[1] if task else field
    age_name = fx.target_parts(age_task['target'])[1]
    return [{'condition': ' or '.join(f'{route_name} == {_literal(value)}' for value, _ in group),
             'origin': group[0][1], 'categories': [v for v, _ in group]} for group in grouped.values()], task, age_name, old, young


def amount_tier_input(board, spec):
    ws = board.ws
    anchor = amount_tier_clause(ws, spec)
    corpus = ws._corpus()
    verified = [c for c in ws.clauses if not check_quote(c.file, c.quote, corpus)]
    if anchor not in verified or ws._flow().current('approved:' + spec['scheme']):
        return None
    _, scope, base = _BASIS.fullmatch(_rule_text(anchor.quote)).groups()
    rules = ws._ruleset(spec['scheme'])
    owners = {(n, n) for n, d in rules.datasets.items() if d.key and fx.scope_norm(n) == scope and n in ws.mapping}
    for level in rules.allocation:
        if fx.scope_norm(level.level) == scope and (entity := rules.entity_dataset(level.key)) and entity[0] in ws.mapping:
            owners.add((level.level, entity[0]))
    if len(owners) != 1:
        return None
    scope, owner = next(iter(owners))
    statuses = board.statuses(with_confidence=False)
    bases = [fx.target_parts(t['target'])[1] for ident in spec['depends_on'] if (t := board.task_spec(ident))
             and t['target'].startswith('/variables/') and fx.scope_norm(fx.target_parts(t['target'])[1]) == base
             and statuses[ident]['status'] == 'PASS' and (board.value(t) or {}).get('scope') == scope]
    policies = [c for c in verified if re.fullmatch(r'按区域对应超额累进阶梯比例计算[。.]?', fx.scope_norm(c.quote))]
    negatives = [c for c in verified if '阶梯' in c.quote and preserves_negative(c.quote)]
    if len(bases) != 1 or len(policies) != 1 or not negatives:
        return None
    entity = rules.datasets[owner]
    age = _monthly_age(ws, rules, owner, entity, verified)
    if age is None or not (planned := _age_task(board, spec, scope, age)):
        return None
    age_task, formula = planned
    if formula:
        revised = {**spec, 'depends_on': list(dict.fromkeys([*spec['depends_on'], age_task['id']]))}
        return {'candidate_call': {'scheme': spec['scheme'], 'task': age_task['id'], 'formulas': [formula],
                                   'upstream_tasks': [age_task, revised]},
                'source': formula['value']['sources'], 'contract': age[0]}
    if statuses[age_task['id']]['status'] != 'PASS':
        return None
    routes = _tier_routes(board, spec, scope, owner, entity, verified, statuses, age, age_task)
    if not isinstance(routes, tuple):
        return routes
    groups, route_task, age_name, old, young = routes
    return _amount_candidate(spec, scope, bases[0], anchor, policies[0], negatives, age_task, route_task, age_name, old, young, groups)


def _amount_candidate(spec, scope, base, anchor, policy, negatives, age_task, route_task, age_name, old, young, groups):
    scheme, target = spec['scheme'], spec['target']
    name = fx.target_parts(target)[1]
    clauses = [anchor, policy, *negatives]
    plans, builds, branches = [], [], []
    for index, group in enumerate(groups, 1):
        condition, src = group['condition'], group['origin']
        source = src['source']
        calls = {}
        for label in ((young,) if src['new_only'] else (young, old)):
            col = src['columns'][label]
            table = f'{name}_g{index}_p{source["page"]}_c{col}'
            guard = f'({condition}) and {age_name} == {_literal(label)}'
            task = {'id': f'{spec["id"]}:{table}', 'scheme': scheme, 'target': '/tables/' + table,
                    'title': f'{src["caption"]} {label}', 'clauses': [c.id for c in clauses[1:]]}
            plans.append(task)
            builds.append({'task': task['id'], 'caption': src['caption'],
                           'cells': [{'row': r, 'column': c} for r in src['rows'] for c in (1, col)],
                           'call': {'name': table, 'rows': [{'lower': f'R{r}C1', 'value': f'R{r}C{col}'} for r in src['rows']],
                                    'scheme': scheme, 'kind': 'tier', 'applies_when': guard, 'below_first': 'first',
                                    'cells_from': source, 'algorithm_basis': {'file': policy.file, 'quote': policy.quote}}})
            calls[label] = f'tier({table}, {base})'
        expr = f'if({age_name} == {_literal(old)}, {calls[old]}, {calls[young]})' if old in calls else calls[young]
        branches.append((condition, expr))
    # The final group still checks its real region and age in the table guard.
    # An unrecognized category therefore raises at evaluation instead of falling into a rate.
    expr = branches[-1][1]
    for condition, branch in reversed(branches[:-1]):
        expr = f'if({condition}, {branch}, {expr})'
    dependents = [age_task, *([route_task] if route_task else []), *plans]
    revised = {**spec, 'expected_scope': scope, 'clauses': list(dict.fromkeys([*spec['clauses'], *(c.id for c in clauses)])),
               'depends_on': list(dict.fromkeys([*spec['depends_on'], *(t['id'] for t in dependents)]))}
    return {'candidate_call': {'scheme': scheme, 'task': spec['id'], 'upstream_tasks': [*plans, revised], 'tier_builds': builds,
                               'formulas': [{'task': spec['id'], 'value': {'scope': scope, 'expr': expr, 'sources': _sources(clauses)}}]},
            'routes': [{'categories': g['categories'], **g['origin']['source']} for g in groups]}
