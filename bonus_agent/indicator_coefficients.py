"""Connect a source-backed optional metric to its checked coefficient series."""

from collections import defaultdict
import json
import re
from unicodedata import normalize

from bonus_agent import clauses, facts
from bonus_agent.data_profile import _source_column_name
from bonus_agent.formula_inputs import with_requirements
from bonus_agent.indicator_scope import _expression, _group_header
from bonus_agent.source_cells import read_ppt_tables
from bonus_core.dsl.parser import Call, Str
from bonus_core.dsl.scope import Layout


def _metric_rule(clause):
    text = normalize('NFKC', clause.quote)
    match = re.fullmatch(r'\s*(?:(.+?)\s*\|\s*)?按(.+?)(?:准确|精确)匹配(.+?)[,，]'
                         r'\s*(?:查不到|找不到)(.+?)时[,，]\s*匹配系数(?:指定为?|设为|为)'
                         r'\s*([-+]?\d+(?:\.\d+)?)[。.]?\s*', text)
    if match and len({facts.scope_norm(match[n]) for n in (1, 3, 4) if match[n]}) == 1:
        return match[3].strip(), match[2].strip(), facts.number(match[5])
    return None


def optional_metric_clause(ws, spec):
    if not spec['target'].startswith('/variables/'):
        return None
    candidates = [c for c in ws.clauses if c.id in spec['clauses'] and _metric_rule(c)]
    return candidates[0] if len(candidates) == 1 else None


def _series_roles(board, scheme, metric, rules, statuses):
    """Use current receipts and source coordinates to identify the role of every series."""
    ws = board.ws
    targets = {}
    for task, spec in board.state['tasks'].items():
        parts = facts.target_parts(spec['target'])
        if (spec['scheme'] == scheme and parts[0] == 'tables' and len(parts) == 2
                and statuses[task]['status'] == 'PASS'):
            targets[parts[1]] = task

    bindings = defaultdict(dict)
    for fact in ws.facts:
        if not fact.page or not fact.source_cell or ws._source_version.get(fact.file) != fact.source_hash:
            continue
        for pointer in fact.targets:
            owner, path = facts.split_target(pointer)
            parts = facts.target_parts(path)
            if (owner == scheme and len(parts) == 5 and parts[0] == 'tables' and parts[1] in targets
                    and parts[2] == 'steps' and parts[3].isdigit() and parts[4] == '1'):
                bindings[parts[1]].setdefault(int(parts[3]), set()).add(
                    (fact.file, fact.page, fact.source_cell['table'], fact.source_cell['column']))

    roles, pages = {}, {}
    for name, entries in bindings.items():
        table = rules.tables.get(name)
        if (getattr(table, 'purpose', None) != 'coefficient' or table.below_first != 'first'
                or table.applies_when or set(entries) != set(range(len(table.steps)))):
            continue
        locations = set().union(*entries.values())
        if len(locations) != 1:
            continue
        file, page, source_table, column = next(iter(locations))
        if (file, page) not in pages:
            pages[file, page] = read_ppt_tables(ws._file(file), page)
        cells = pages[file, page][source_table-1]['rows'][0]['cells']
        measurement, label = [facts.scope_norm(' '.join(cells[i]['lines'])) for i in (0, column-1)]
        role = re.match(r'^(.+?)(?:品牌)?系数', label)
        if metric not in measurement or not role:
            continue
        if role[1] in roles:
            return {}
        roles[role[1]] = {'table': name, 'task': targets[name], 'file': file, 'page': page,
                          'source_table': source_table, 'column': column}
    return roles


def _override_terms(checked, metric, scope):
    coefficient = re.escape(metric.removesuffix('率') + '系数')
    pattern = re.compile(r'(.+?)' + re.escape(facts.scope_norm(scope)) + r'的' + coefficient
                         + r'用(.+?)的' + coefficient + r'表')
    return [(match[1], match[2], c) for c in checked if (match := pattern.fullmatch(facts.scope_norm(c.quote)))]


def _routing_override(ws, board, spec, rules, checked, statuses, metric, scope):
    overrides = _override_terms(checked, metric, scope)
    if not overrides:
        return False
    if len(overrides) != 1:
        return None
    qualifier, role, source = overrides[0]
    proven = {c.file: set() for c in ws.clauses}
    for c in ws.clauses:
        proven[c.file].add(c.quote)
    routes = []
    for task, planned in board.state['tasks'].items():
        parts = facts.target_parts(planned['target'])
        if (planned['scheme'] != spec['scheme'] or len(parts) != 2 or parts[0] != 'variables'
                or statuses[task]['status'] != 'PASS'):
            continue
        var = rules.variables.get(parts[1])
        parsed = _expression(var.expr) if var and var.scope == scope else None
        if (not isinstance(parsed, Call) or parsed.fn != 'if' or len(parsed.args) != 3
                or not isinstance(label := parsed.args[1], Str)
                or not re.search(r'(?:^|[-_])' + re.escape(qualifier) + r'$', facts.scope_norm(label.value))):
            continue
        for citation in var.sources:
            if (citation.quote in proven.get(citation.doc, ())
                    and re.search(r'归类[^。；;]*为\s*' + re.escape(label.value) + r'(?:[。；;\s]|$)', citation.quote)
                    and not clauses.check_quote(citation.doc, citation.quote, ws._corpus())):
                routes.append((parts[1], label.value, role, source, task))
                break
    return routes[0] if len(routes) == 1 else None


def _raw_series(ws, metric):
    """Find complete original ratio bands with an explicit coefficient column heading."""
    from bonus_agent.agent import ToolError
    from bonus_agent.coefficient import interval, resolve
    from bonus_agent.source_cells import ppt_page_index

    found = defaultdict(list)
    flow = ws._flow()
    roles = ws.workflow.get('plan', {}).get('files', {})
    skipped = flow.skipped()
    for file, kind in roles.items():
        if kind not in ('rule', 'rule_and_data') or file in skipped or not file.lower().endswith('.pptx'):
            continue
        path = ws._file(file)
        for entry in ppt_page_index(path):
            for table in read_ppt_tables(path, entry['page']):
                heading, *body = table['rows']
                header = {cell['column']: ' '.join(cell['lines']).strip() for cell in heading['cells']}
                if not body or metric not in facts.scope_norm(header.get(1, '')):
                    continue
                rows = [{c['column']: [line.strip() for line in c['lines'] if line.strip()] for c in row['cells']}
                        for row in body]
                if any(len(row.get(1, ())) != 1 or not re.search(r'[%％‰]', row[1][0]) for row in rows):
                    continue
                try:
                    resolve([interval(row[1][0]) for row in rows])
                except ToolError:
                    continue
                for col, label in header.items():
                    role = re.match(r'^(.+?)(?:品牌)?系数', facts.scope_norm(label))
                    if col <= 1 or not role or any(len(row.get(col, ())) != 1 or facts.number(row[col][0]) is None
                                                   for row in rows):
                        continue
                    found[role[1]].append({
                        'file': file, 'page': entry['page'], 'source_table': table['table'], 'column': col,
                        'label': label, 'rows': [{'range': f'R{row["row"]}C1', 'value': f'R{row["row"]}C{col}'}
                                                for row in body],
                    })
    return {role: entries[0] for role, entries in found.items() if len(entries) == 1}


def _source_route(ws, spec, owner, scope, override):
    """Compile a stated conditional classification over the established entity key."""
    from bonus_core.engine.tree import _norm_id

    qualifier, role, policy = override
    mapping = ws.mapping[owner]
    entity = ws.datasets[owner]
    fields = defaultdict(list)
    for field, column in mapping.columns.items():
        if field in entity.fields and entity.fields[field].type in ('id', 'text', 'enum'):
            fields[_group_header(_source_column_name(column))].append(field)
    corpus = ws._corpus()
    variants = []
    grammar = r'^\s*(?:当|若)\s*(.+?)[,，]\s*归类([^为,，。；;]+)为\s*([^,，。；;]+)[。.]?\s*$'
    for clause in ws.clauses:
        match = re.fullmatch(grammar, clause.quote)
        if not match or clauses.check_quote(clause.file, clause.quote, corpus):
            continue
        predicates, heading, label = match.groups()
        if not re.search(r'(?:^|[-_])' + re.escape(qualifier) + r'$', facts.scope_norm(label)):
            continue
        fallback = fields.get(_group_header(_source_column_name(heading)), [])
        if len(fallback) != 1:
            continue
        expressions, required = [], {fallback[0]}
        for term in re.split(r'[,，]|且', predicates):
            condition = re.fullmatch(r'\s*(.+?)\s*(不等于|等于|!=|==|=)\s*(.+?)\s*', term)
            if not condition:
                break
            criterion, operator, expected = condition.groups()
            matches = fields.get(_group_header(_source_column_name(criterion)), [])
            if len(matches) != 1:
                break
            field, = matches
            expected = expected.strip('「」“”"').strip()
            observed = {v for v in ws._values(owner, field) if _norm_id(v) == _norm_id(expected)}
            if len(observed) > 1:
                break
            literal = next(iter(observed), expected)
            required.add(field)
            op = '!=' if operator in ('不等于', '!=') else '=='
            expressions.append(f'{field} {op} {json.dumps(literal, ensure_ascii=False)}')
        else:
            variants.append((clause, label.strip(), expressions, fallback[0], required))
    if len(variants) != 1:
        return None
    clause, label, conditions, fallback, required = variants[0]
    name = facts.target_parts(spec['target'])[-1] + '_routing'
    task = spec['id'] + '_routing'
    plan = {'id': task, 'scheme': spec['scheme'], 'target': '/variables/' + name,
            'title': '系数适用对象归类', 'clauses': [clause.id], 'expected_scope': scope,
            'data_requirements': [{'dataset': owner, 'fields': sorted(required | {entity.key})}]}
    formula = {'task': task, 'value': {'scope': scope, 'expr': f'if({" and ".join(conditions)}, '
               f'{json.dumps(label, ensure_ascii=False)}, {fallback})',
               'sources': [{'doc': clause.file, 'quote': clause.quote}]}}
    return (name, label, role, policy, task), plan, formula


def _division_options(ws, rules, owner, definition, allowed):
    """Recognize a routing column carried by either the entity or its own detail workbook."""
    anchor = ws.mapping[owner]
    candidates = {}
    for name, dataset in rules.datasets.items():
        mapped = ws.mapping.get(name)
        if not mapped or (mapped.file, mapped.sheet, mapped.header_rows) != (anchor.file, anchor.sheet, anchor.header_rows):
            continue
        if name != owner and (dataset.link != definition.key
                              or mapped.columns.get(definition.key) != anchor.columns.get(definition.key)
                              or (mapped.combine or {}).get(definition.key) != (anchor.combine or {}).get(definition.key)):
            continue
        for field, column in mapped.columns.items():
            if (field in dataset.fields and dataset.fields[field].type in ('text', 'enum')
                    and facts.scope_norm(_source_column_name(column)) in ('事业部', '业务部门', '部门', '区域')):
                candidates.setdefault(column, (name, field))
                if name == owner:
                    candidates[column] = (name, field)
    options = []
    for column, (name, field) in candidates.items():
        values = sorted(set(ws._values(name, field)))
        routes = {value: re.sub(r'(?:事业部|业务部门|部门|区域)$', '', facts.scope_norm(value)) for value in values}
        if routes and set(routes.values()) <= allowed:
            options.append((name, field, column, routes))
    return options


def optional_metric_input(board, spec):
    from bonus_agent.agent import ToolError

    ws = board.ws
    anchor = optional_metric_clause(ws, spec)
    if anchor is None:
        return None
    source_label, scope, fallback = _metric_rule(anchor)
    metric = facts.scope_norm(source_label)
    if spec.get('expected_scope') not in (None, scope) or fallback is None:
        return None
    corpus = ws._corpus()
    checked = [c for c in ws.clauses if c.id in spec['clauses'] and not clauses.check_quote(c.file, c.quote, corpus)]
    if anchor not in checked:
        return None
    empty = [c for c in checked if metric in facts.scope_norm(c.quote)
             and re.search(r'空白|空的|空值', c.quote) and re.search(r'(?:算|按)查不到', c.quote)
             and '低于最低档' in c.quote and re.search(r'取最低档系数', c.quote)]
    if not empty:
        return None
    fallback_values = {facts.number(n) for c in empty
                       for n in re.findall(r'系数(?:指定为?|设为|为)\s*([-+]?\d+(?:\.\d+)?)', c.quote)}
    if fallback_values != {fallback}:
        return None
    rules = ws._ruleset(spec['scheme'])
    entity = rules.entity_dataset(rules.level(scope).key) if rules.level(scope) else (
        (scope, rules.datasets[scope]) if scope in Layout(rules).aux else None)
    if not entity:
        return None
    owner, definition = entity
    candidates = [(n, field) for n, ds in rules.datasets.items() if not ds.key and ds.link == definition.key and n in ws.mapping
                  for field, column in ws.mapping[n].columns.items() if field in ds.fields and ds.fields[field].type == 'ratio'
                  and facts.scope_norm(_source_column_name(column)) == metric]
    if len(candidates) != 1:
        return None
    reference, field = candidates[0]
    if rules.datasets[reference].fields[field].if_empty not in (None, 'skip'):
        return None
    upstream = []
    formulas = []
    builds = []
    try:
        statuses = board.refresh()
        roles = _series_roles(board, spec['scheme'], metric, rules, statuses)
        raw = _raw_series(ws, metric)
        routing = _routing_override(ws, board, spec, rules, checked, statuses, metric, scope)
        if routing is None:
            overrides = _override_terms(checked, metric, scope)
            if len(overrides) != 1 or not (proposal := _source_route(ws, spec, owner, scope, overrides[0])):
                return None
            routing, plan, formula = proposal
            upstream.append(plan)
            formulas.append(formula)
        allowed = (roles.keys() | raw.keys()) - ({routing[2]} if routing else set())
        options = _division_options(ws, rules, owner, definition, allowed)
    except (IndexError, KeyError, OSError, TypeError, ValueError, ToolError):
        return None
    if len(options) != 1:
        return None
    population, group, group_column, routes = options[0]
    needed = set(routes.values()) | ({routing[2]} if routing else set())
    if not needed <= roles.keys() | raw.keys():
        return None
    base = facts.target_parts(spec['target'])[-1]
    tasks = board.state['tasks']
    names = set(rules.variables) | set(rules.tables) | set(rules.field_owner())
    targets = {t['target'] for t in tasks.values() if t['scheme'] == spec['scheme']}
    if any(t['id'] in tasks or t['target'] in targets or facts.target_parts(t['target'])[-1] in names for t in upstream):
        return None
    for role in sorted(needed - roles.keys()):
        evidence = raw[role]
        name = base + '_rate_' + re.sub(r'\W+', '_', role)
        task = spec['id'] + ':rate:' + role
        if name in names or task in tasks or '/tables/' + name in targets:
            return None
        names.add(name)
        plan = {'id': task, 'scheme': spec['scheme'], 'target': '/tables/' + name,
                'title': evidence['label'], 'clauses': [anchor.id]}
        call = {'name': name, 'scheme': spec['scheme'], 'rows': evidence['rows'], 'applies_to': evidence['label'],
                'cells_from': {'file': evidence['file'], 'page': evidence['page'], 'table': evidence['source_table']}}
        builds.append({'plan': plan, 'call': call})
        roles[role] = {k: evidence[k] for k in ('file', 'page', 'source_table', 'column')} | {'table': name, 'task': task}
    routing_field = group
    datasets = [{'operation': 'update', 'args': {'name': reference, 'changes': {'fields': {field: {'if_empty': 'skip'}}}}}]
    if (population != owner or definition.fields[group].type != 'enum'
            or set(definition.fields[group].values or ()) != set(routes)):
        # Qualify the category for this routing rule without changing the
        # original field shared with transactional datasets.
        routing_field = group + '_coefficient_group'
        if any(routing_field in ds.fields for ds in rules.datasets.values()):
            return None
        datasets.append({'operation': 'update', 'args': {'name': owner, 'changes': {'fields': {
            routing_field: {'column': group_column, 'type': 'enum', 'values': sorted(routes)},
        }}}})
    literal = lambda text: json.dumps(text, ensure_ascii=False)
    measure = f'single_value({field}, of: {literal(reference)})'
    paths = [(f'{routing_field} == {literal(value)}', roles[role]['table']) for value, role in routes.items()]
    cited = [anchor, empty[0]]
    dependencies = set(spec['depends_on']) | {roles[role]['task'] for role in needed}
    if routing:
        name, label, role, source, task = routing
        paths.insert(0, (f'{name} == {literal(label)}', roles[role]['table']))
        dependencies.add(task)
        cited.append(source)
    selected = f'tier_rate({paths[-1][1]}, {measure})'
    for condition, name in reversed(paths[:-1]):
        selected = f'if({condition}, tier_rate({name}, {measure}), {selected})'
    expr = f'if(count(of: {literal(reference)}) == 0, {fallback}, {selected})'
    data_contract = {'dataset': reference, 'fields': [definition.key, field], 'link': definition.key}
    requirements = with_requirements(spec, data_contract, {'dataset': owner, 'fields': [definition.key, routing_field]})
    if requirements is None:
        return None
    parameter = spec['target'] + ' 缺少记录时的系数'
    bindings = [{'name': parameter, 'targets': [spec['scheme'] + '::' + spec['target'] + '/expr']}]
    return {
        'source': {'file': ws.mapping[reference].file, 'sheet': ws.mapping[reference].sheet},
        'contract': data_contract, 'coefficient_series': [roles[role] for role in sorted(needed)],
        'candidate_call': {
            'scheme': spec['scheme'], 'task': spec['id'],
            'formulas': [*formulas, {
                'task': spec['id'],
                'value': {'scope': scope, 'expr': expr,
                          'sources': [{'doc': c.file, 'quote': c.quote} for c in cited]},
                'expected_scope': scope, 'depends_on': sorted(dependencies), 'data_requirements': requirements,
                'facts': [{'name': parameter, 'file': anchor.file, 'quote': anchor.quote, 'value': str(fallback)}],
                'bindings': bindings,
            }],
            'datasets': datasets, 'coefficient_builds': builds, 'upstream_tasks': upstream,
        },
    }
