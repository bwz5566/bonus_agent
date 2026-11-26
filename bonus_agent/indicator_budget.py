"""Propose budget ratios on the grain and reference key stated by the source."""
from collections import Counter, defaultdict
from dataclasses import replace
from decimal import Decimal
import json
import re
from pathlib import Path

from bonus_agent.clauses import check_quote
from bonus_agent.data_profile import _source_column_name
from bonus_agent.facts import scope_norm, target_parts
from bonus_agent.formula_inputs import with_rebound_requirement
from bonus_agent.indicator_scope import _expression, _group_header
from bonus_core.dsl.parser import BinOp, BoolOp, Call, Cmp, Name, Neg, Not, Num, Str, names, unparse


_MISSING_BUDGET = (r'(?:查不到|找不到|未查到)预算[^。；;\n]{0,100}?(?:系数|倍率)'
                   r'(?:指定为?|固定为|设为)\s*(\d+(?:\.\d+)?)(?![\d.%％])')
_ZERO_BUDGET = r'预算(?:为|等于)\s*0(?![\d.])'
_MONTH_HEADING = r'(?<!\d)(1[0-2]|[1-9])\s*月'


def _budget_groups(quote):
    match = re.search(r'月份?[、，,]\s*([^、，,|。]+)[、，,]', quote)
    if match and '销售额' in quote and '预算' in quote:
        return [_group_header(_source_column_name(v)) for v in re.split(r'\s*[与和]\s*', match[1])]
    return []


def monthly_budget_clause(ws, spec):
    if not spec['target'].startswith('/variables/'):
        return None
    matches = [c for c in ws.clauses if c.id in spec['clauses'] and _budget_groups(c.quote)
               and re.search(_MISSING_BUDGET, c.quote)]
    return matches[0] if len(matches) == 1 else None


def _monthly_fields(definition, mapping):
    fields = {}
    for field, column in mapping.columns.items():
        months = re.findall(_MONTH_HEADING, _source_column_name(column))
        if len(months) != 1 or field not in definition.fields or definition.fields[field].type != 'money':
            continue
        month, = months
        if month in fields:
            return None
        fields[month] = field
    return [fields[str(n)] for n in range(1, 13)] if len(fields) == 12 else None


def _budget_key(definition, mapping, groups):
    fields = [field for field in mapping.columns if field in definition.fields
              and definition.fields[field].type in ('id', 'text', 'enum')
              and _group_header(_source_column_name(mapping.columns[field])) in groups]
    if key := (definition.key or definition.link):
        return key if key in fields else None
    return fields[0] if len(fields) == 1 else None


def _checked_sales_anchors(board, spec, rules, statuses):
    """A verified current sales aggregate also establishes the underlying detail."""
    sources = set()
    for ident in spec['depends_on']:
        dependency = board.task_spec(ident)
        parts = target_parts(dependency['target'])
        if dependency['scheme'] != spec['scheme'] or parts[:1] != ['variables'] or statuses[ident]['status'] != 'PASS':
            continue
        value = rules.variables.get(parts[1])
        expr = _expression(value.expr) if value else None
        if (isinstance(expr, Call) and expr.fn == 'sum' and len(expr.args) == 1 and isinstance(expr.args[0], Name)
                and set(expr.kw()) == {'of'} and isinstance(source := expr.kw()['of'], Str)):
            sources.add((source.value, expr.args[0].id))
    return sources


def _budget_match_issue(ws, sales, sales_key, reference, reference_key):
    from bonus_core.engine.tree import _norm_id

    actual = set(ws._values(sales, sales_key))
    normalized = defaultdict(set)
    for key in actual:
        normalized[_norm_id(key)].add(key)
    matched = Counter()
    for key in ws._values(reference, reference_key):
        options = {key} if key in actual else normalized.get(_norm_id(key), set())
        if len(options) > 1:
            return '存在预算名称对应多个当期核算对象'
        matched.update(options)
    if not matched:
        return '两列没有可核对的关联值'
    if duplicates := sum(count > 1 for count in matched.values()):
        return f'{duplicates} 个当期核算对象各有多条预算记录，材料须说明取哪一条或如何汇总'
    return ''


def monthly_budget_input(board, spec):
    """Build the declared ratio and coefficient together, using active sales as the population."""
    from bonus_agent.agent import ToolError

    ws = board.ws
    source = monthly_budget_clause(ws, spec)
    corpus = ws._corpus()
    if source is None or check_quote(source.file, source.quote, corpus):
        return None
    groups = _budget_groups(source.quote)
    own = [c for c in ws.clauses if c.id in spec['clauses'] and not check_quote(c.file, c.quote, corpus)]
    zero = [c for c in own if re.search(_ZERO_BUDGET, c.quote) and re.search(_MISSING_BUDGET, c.quote)]
    actual = [c for c in own if '当月实际销售额' in c.quote and '当月预算' in c.quote]
    defaults = {Decimal(v) for c in own for v in re.findall(_MISSING_BUDGET, c.quote)}
    if len(zero) != 1 or len(actual) != 1 or len(defaults) != 1:
        return None
    default, = defaults
    rs = ws._ruleset(spec['scheme'])
    tasks = board.state['tasks']
    statuses = board.statuses(with_confidence=False)
    consumers = []
    for task in tasks.values():
        if (task['scheme'] != spec['scheme'] or not task['target'].startswith('/variables/')
                or spec['id'] not in task['depends_on'] or zero[0].id not in task['clauses']
                or spec.get('expected_scope') != task.get('expected_scope') or board.value(task) is not None):
            continue
        for dep in task['depends_on']:
            parts = target_parts(tasks[dep]['target'])
            if (tasks[dep]['scheme'] == spec['scheme'] and parts[0] == 'tables'
                    and statuses[dep]['status'] == 'PASS' and getattr(rs.tables.get(parts[1]), 'purpose', None) == 'coefficient'):
                consumers.append((task, parts[1]))
    if len(consumers) > 1:
        return None
    table_task = None
    coefficient_source = None
    if consumers:
        consumer, table = consumers[0]
    else:
        from bonus_agent.coefficient import achievement_series_call, checked_achievement_series

        output = scope_norm(spec.get('title') or target_parts(spec['target'])[-1])
        subjects = {re.sub(r'(?:简称|名称|编码|代码|编号)$', '', group) for group in groups}
        if not output.endswith('系数') or len(subjects) != 1:
            return None
        subject, = subjects
        series = checked_achievement_series(board, spec['scheme'], subject, statuses)
        if series:
            table_task, table = series
        else:
            table = target_parts(spec['target'])[-1] + '_intervals'
            table_task = spec['id'] + '_intervals'
            owned = any(t['scheme'] == spec['scheme'] and target_parts(t['target']) == ['tables', table] for t in tasks.values())
            if table_task in tasks or owned or table in rs.tables or table in rs.variables or table in rs.field_owner():
                return None
            coefficient_source = achievement_series_call(ws, subject, table, spec['scheme'])
            if coefficient_source is None:
                return None
        consumer = spec
    sales_anchors = _checked_sales_anchors(board, spec, rs, statuses)
    actual_text = scope_norm(actual[0].quote)
    owners = {(ds.key, ws.mapping[name].file, ws.mapping[name].sheet)
              for name, ds in rs.datasets.items() if ds.key and ds.period and name in ws.mapping}
    sales, budgets = [], []
    for name, ds in rs.datasets.items():
        mapping = ws.mapping.get(name)
        if not mapping:
            continue
        reference_key = _budget_key(ds, mapping, groups)
        if reference_key in mapping.columns and not ds.period and '预算' in (name + mapping.file + mapping.sheet):
            header = _group_header(_source_column_name(mapping.columns[reference_key]))
            if header in groups and (months := _monthly_fields(ds, mapping)):
                budgets.append((name, months))
        if not ds.key and ds.period and ds.window == 'period':
            for field, column in mapping.columns.items():
                heading = scope_norm(_source_column_name(column))
                if (field in ds.fields and ds.fields[field].type == 'money' and heading and heading in actual_text
                        and ((ds.link, mapping.file, mapping.sheet) in owners or (name, field) in sales_anchors)):
                    sales.append((name, field))
    if len(sales) != 1 or len(budgets) != 1:
        return None
    (reference, months), = budgets
    (sale, amount), = sales
    of_sales = json.dumps(sale, ensure_ascii=False)
    numerator = f'sum({amount}, of: {of_sales})'
    draft = {'scope': reference, 'expr': f'tier_ratio({table}, {numerator}, period_value({", ".join(months)}), {default})'}
    try:
        proposal = grouped_budget_candidate(board, spec, draft, coefficient_source=coefficient_source)
    except ToolError as exc:
        return {'task': spec['id'], 'scheme': spec['scheme'], 'target': spec['target'],
                'status': 'FAIL', 'saved': False, 'stage': 'data', 'issues': [str(exc)]}
    if proposal is None:
        return None
    formula = proposal['formulas'][0]
    scope = formula['expected_scope']
    coefficient_expr = formula['value']['expr']
    parameter = consumer['target'] + ' 预算不可用系数'
    inputs = [(formula, spec)]
    if table_task is not None:
        factor = formula
        factor['depends_on'] = list(dict.fromkeys([*spec['depends_on'], table_task]))
        if coefficient_source:
            proposal['coefficient_plan'] = {'id': table_task, 'scheme': spec['scheme'], 'target': '/tables/' + table,
                                            'title': subject + '达成系数档位', 'clauses': [source.id]}
    else:
        ratio_call = _expression(coefficient_expr)
        missing = unparse(ratio_call.kw()['missing'])
        denominator = unparse(ratio_call.args[2])
        formula['value']['expr'] = f'if({missing}, 0, ratio({numerator}, {denominator}))'
        # A genuine zero numerator is still classified by its coefficient band.
        factor = {'task': consumer['id'], 'expected_scope': scope,
                  'value': {'scope': scope, 'expr': coefficient_expr, 'sources': list(formula['value']['sources'])}}
        proposal['formulas'].append(factor)
        inputs.append((factor, consumer))
    factor['facts'] = [{'name': parameter, 'file': zero[0].file, 'quote': zero[0].quote, 'value': str(default)}]
    factor['bindings'] = [{'name': parameter, 'targets': [spec['scheme'] + '::' + consumer['target'] + '/expr']}]
    reference_changes = proposal['datasets'][-1]['args']['changes']
    field_updates = reference_changes.setdefault('fields', {})
    field_updates.update({field: {'if_empty': '0'} for field in months if rs.datasets[reference].fields[field].if_empty is None})
    group_key = reference_changes['link']
    contract = {'dataset': reference, 'fields': [group_key, *months], 'link': group_key}
    previous_key = rs.datasets[reference].key or rs.datasets[reference].link
    sales_input = {'dataset': sale, 'fields': [amount, group_key], 'window': 'period',
                   'period_field': rs.datasets[sale].period}
    for item, prior in inputs:
        requirements = with_rebound_requirement(prior, contract, previous_key, sales_input)
        if requirements is None:
            return None
        item['data_requirements'] = requirements
    return {'contract': contract, 'source': {'file': ws.mapping[reference].file, 'sheet': ws.mapping[reference].sheet},
            'scope': scope, 'candidate_call': proposal}


def _same_plan_id(board, scheme, variable):
    return next((spec for spec in board.state['tasks'].values()
                 if spec['scheme'] == scheme and target_parts(spec['target']) == ['variables', variable]), None)


def _group_owner(ws, rules):
    pool = rules.pool.scope
    if pool in ws.datasets and ws.datasets[pool].key:
        return pool
    key = next((level.key for level in rules.allocation if level.level == pool), None)
    matched = [name for name, d in ws.datasets.items() if key and d.key == key and name in ws.mapping]
    return matched[0] if len(matched) == 1 else None


def _scope(ws, owner, key, *, group_key=None):
    source = ws.mapping[owner]
    group_key = group_key or key
    for name, definition in ws.datasets.items():
        if definition.key != group_key:
            continue
        origin = ws.mapping.get(name)
        if origin and (origin.file, origin.sheet, origin.header_rows, origin.columns.get(group_key)) == (
                source.file, source.sheet, source.header_rows, source.columns[key]):
            return name, None
    name = 'group_' + key
    if name in ws.datasets:
        return None, None
    definition = ws.datasets[owner]
    fields = {f: {**definition.fields[f].model_dump(exclude_defaults=True), 'column': source.columns[f]}
              for f in [key, definition.period] if f}
    if group_key != key:
        fields[group_key] = fields.pop(key)
    return name, {'operation': 'define', 'args': {
        'name': name, 'file': Path(source.file).name, 'sheet': source.sheet, 'fields': fields,
        'key': group_key, 'distinct': True, 'period': definition.period, 'window': definition.window,
        'filter': source.filter, 'header_rows': source.header_rows,
    }}


def _verified_group_key(ws, rules, source, options, heading, clauses):
    """Choose a duplicate physical field only when a compound reference corroborates it."""
    from bonus_agent.agent import ToolError

    mapping = ws.mapping[source]
    link = rules.datasets[source].link
    columns = (mapping.combine or {}).get(link, ())
    if not columns or not any(heading in scope_norm(c.quote) and '同名' in c.quote
                              and '参考表' in c.quote and '对得上' in c.quote for c in clauses):
        return None
    selected = [key for key in options if mapping.columns[key] in columns]
    if len(selected) != 1:
        return None
    key, = selected
    index = columns.index(mapping.columns[key])

    def project_keys(field):
        parts = list(columns)
        parts[index] = mapping.columns[field]
        return set(ws._values(source, link, {**mapping.combine, link: parts}))

    try:
        correct = project_keys(key)
        alternatives = [project_keys(f) for f in options if f != key]
        for ref, definition in rules.datasets.items():
            ref_map = ws.mapping.get(ref)
            if ref == source or not ref_map or not definition.period or definition.link != link:
                continue
            components = (ref_map.combine or {}).get(link, ())
            if len(components) != len(columns) or _group_header(_source_column_name(components[index])) != heading:
                continue
            observed = set(ws._values(ref, link))
            if observed and observed <= correct and all(observed - alternative for alternative in alternatives):
                return key
    except (KeyError, ValueError, ToolError):
        return None
    return None


def _group_field_in_lookup(node, dataset, fields, field):
    if isinstance(node, Call):
        if (node.fn == 'single_value' and node.kw() == {'of': Str(dataset)} and len(node.args) == 1
                and isinstance(node.args[0], Name) and node.args[0].id in fields):
            return replace(node, args=(Name(field),))
        return replace(node, args=tuple(_group_field_in_lookup(x, dataset, fields, field) for x in node.args),
                       kwargs=tuple((k, _group_field_in_lookup(x, dataset, fields, field)) for k, x in node.kwargs))
    if isinstance(node, (BinOp, BoolOp, Cmp)):
        return replace(node, left=_group_field_in_lookup(node.left, dataset, fields, field),
                       right=_group_field_in_lookup(node.right, dataset, fields, field))
    if isinstance(node, (Neg, Not)):
        return replace(node, operand=_group_field_in_lookup(node.operand, dataset, fields, field))
    return node


def _bind_group_on_projects(board, spec, rules, source, key, heading, clauses):
    """Expose the certified physical key on the matching project population and recheck affected rules."""
    ws = board.ws
    mapping = ws.mapping[source]
    link = rules.datasets[source].link
    updates, formulas = [], []
    for name, ds in rules.datasets.items():
        target = ws.mapping.get(name)
        if (ds.key != link or target is None or (target.file, target.sheet) != (mapping.file, mapping.sheet)
                or (target.combine or {}).get(link) != (mapping.combine or {}).get(link)):
            continue
        if key not in ds.fields:
            definition = rules.datasets[source].fields[key].model_dump(exclude_defaults=True)
            updates.append({'operation': 'update', 'args': {'name': name, 'changes': {'fields': {
                key: {**definition, 'column': mapping.columns[key]},
            }}}})
        if key in ds.fields and target.columns.get(key) != mapping.columns[key]:
            return None
        other_fields = {f for f, column in target.columns.items() if column != mapping.columns[key]
                        and _group_header(_source_column_name(column)) == heading}
        certified = {(c.file, c.quote) for c in clauses if re.search(re.escape(heading) + r'\d', scope_norm(c.quote))}
        for variable, value in rules.variables.items():
            plan = _same_plan_id(board, spec['scheme'], variable)
            if (not plan or value.scope not in (name, spec.get('expected_scope'))
                    or not any((c.doc, c.quote) in certified for c in value.sources)):
                continue
            tree = _expression(value.expr)
            if tree is None or not names(tree) & other_fields:
                continue
            proposal = _group_field_in_lookup(tree, name, other_fields, key)
            if proposal != tree:
                formulas.append({'task': plan['id'], 'changes': {'expr': unparse(proposal)}})
    return updates, formulas


def _move_reference(node, dataset, factor, default):
    if isinstance(node, Call):
        args = node.args
        if (node.fn == 'if' and len(args) == 3 and isinstance(args[0], Call) and args[0].fn == 'has_ref'
                and len(args[0].args) == 1 and isinstance(args[0].args[0], Str) and args[0].args[0].value == dataset
                and isinstance(args[1], Name) and args[1].id == factor and args[2] == default):
            return args[1]
        return replace(node, args=tuple(_move_reference(x, dataset, factor, default) for x in args),
                       kwargs=tuple((k, _move_reference(x, dataset, factor, default)) for k, x in node.kwargs))
    if isinstance(node, (BinOp, BoolOp, Cmp)):
        return replace(node, left=_move_reference(node.left, dataset, factor, default),
                       right=_move_reference(node.right, dataset, factor, default))
    if isinstance(node, (Neg, Not)):
        return replace(node, operand=_move_reference(node.operand, dataset, factor, default))
    return node


def _empty_budget_labels(ws, mapping, key, months):
    """Read marked pivot placeholders. A named business group is never inferred empty."""
    sheet = ws.inv.sheet(f'{Path(mapping.file).name}/{mapping.sheet}')
    key_column = sheet.headers.index(mapping.columns[key])
    columns = [sheet.headers.index(mapping.columns[m]) for m in months]
    return {label for _, row in sheet.rows if key_column < len(row)
            and isinstance(label := row[key_column], str)
            and re.fullmatch(r'[(（](?:空白|blank)[)）]', label.strip(), re.IGNORECASE)
            and all(i >= len(row) or row[i] in (None, '') for i in columns)}


def grouped_budget_candidate(board, spec, value, *, coefficient_source=None):
    """Move a source-backed monthly budget lookup off the reference population."""
    from bonus_agent.agent import ToolError

    ws = board.ws
    if not isinstance(value, dict) or not ws.period or ws.period.label != ws.period.end.strftime('%Y-%m'):
        return None
    if not isinstance(value.get('scope'), str) or not isinstance(value.get('expr'), str):
        return None
    ratio = _expression(value['expr'])
    if not isinstance(ratio, Call) or ratio.fn != 'tier_ratio' or len(ratio.args) != 4:
        return None
    table, sales, denominator, default = ratio.args
    reference = value['scope']
    month_value = denominator
    if isinstance(denominator, Call) and denominator.fn == 'single_value':
        ref_source = denominator.kw().get('of')
        if len(denominator.args) != 1 or set(denominator.kw()) != {'of'} or not isinstance(ref_source, Str):
            return None
        reference = ref_source.value
        month_value = denominator.args[0]
    if reference not in ws.datasets or reference not in ws.mapping:
        return None
    ref = ws.datasets[reference]
    if ref.period:
        return None
    rules = ws._ruleset(spec['scheme'])
    if (not isinstance(table, Name) or not isinstance(sales, Call) or sales.fn != 'sum' or len(sales.args) != 1
            or not isinstance(sales.args[0], Name) or set(sales.kw()) != {'of'}
            or not isinstance(month_value, Call) or month_value.fn != 'period_value' or month_value.kwargs
            or len(month_value.args) != 12 or not all(isinstance(v, Name) for v in month_value.args)
            or not isinstance(default, Num)):
        return None
    definition = rules.tables.get(table.id)
    if definition is not None and getattr(definition, 'purpose', None) != 'coefficient':
        return None
    reference_literal = json.dumps(reference, ensure_ascii=False)
    zero = f'{unparse(denominator)} == 0'
    missing = ratio.kw().get('missing')
    if set(ratio.kw()) - {'missing'} or missing not in (
            None, _expression(zero), _expression(f'count(of: {reference_literal}) == 0 or {zero}')):
        return None
    source = sales.kw()['of']
    if not isinstance(source, Str) or source.value == reference or source.value not in ws.mapping:
        return None
    owner = ws.datasets[source.value]
    ref_map, sale_map = ws.mapping[reference], ws.mapping[source.value]
    amount_field = sales.args[0].id
    if (not owner.period or owner.window != 'period' or not ref_map.file or not sale_map.file
            or not ref_map.sheet
            or amount_field not in sale_map.columns or amount_field not in owner.fields
            or owner.fields[amount_field].type != 'money'):
        return None
    months = [v.id for v in month_value.args]
    for index, field in enumerate(months, 1):
        if field not in ref.fields or field not in ref_map.columns:
            return None
        heading = _source_column_name(ref_map.columns[field])
        if re.findall(_MONTH_HEADING, heading) != [str(index)]:
            return None

    corpus = ws._corpus()
    clauses = [c for c in ws.clauses if not check_quote(c.file, c.quote, corpus)]
    own = [c for c in clauses if c.id in spec['clauses']]
    keys_by_rule = {_budget_key(ref, ref_map, _budget_groups(c.quote)) for c in own} - {None}
    if len(keys_by_rule) != 1:
        return None
    ref_key, = keys_by_rule
    group = _group_header(_source_column_name(ref_map.columns[ref_key]))
    group_sources = {}
    for clause in own:
        headings = _budget_groups(clause.quote)
        if len(headings) == 1 and headings == [group]:
            group_sources[group] = clause
        elif len(headings) == 2 and headings.count(group) == 1:
            group_sources[next(v for v in headings if v != group)] = clause
    amount_heading = scope_norm(_source_column_name(sale_map.columns[amount_field]))
    actual_sources = [c for c in own if '当月实际销售额' in c.quote and '当月预算' in c.quote
                      and amount_heading and amount_heading in scope_norm(c.quote)]
    zero_sources = [c for c in own if re.search(_ZERO_BUDGET, c.quote)]
    defaults = {Decimal(v) for c in own for v in re.findall(_MISSING_BUDGET, c.quote)}
    exclusions = [c for c in clauses if re.search(
        r'参考(?:数据|资料|表)里有[、，, ]*但[^。；;]{1,28}里没有[^。；;]{0,36}不参与核算', c.quote)]
    if len(group_sources) != 1 or not actual_sources or not zero_sources or defaults != {default.value} or len(exclusions) != 1:
        return None
    sale_heading, group_source = next(iter(group_sources.items()))
    keys = [field for field, column in sale_map.columns.items()
            if field in owner.fields and owner.fields[field].type in ('id', 'text', 'enum')
            and _group_header(_source_column_name(column)) == sale_heading]
    alignment, project_formulas = [], []
    if len(keys) == 1:
        key, = keys
    else:
        key = _verified_group_key(ws, rules, source.value, keys, sale_heading, clauses)
        if key is None or (attached := _bind_group_on_projects(board, spec, rules, source.value, key, sale_heading, clauses)) is None:
            return None
        alignment, project_formulas = attached
    if not (ref.key or ref.link):
        try:
            issue = _budget_match_issue(ws, source.value, key, reference, ref_key)
        except (KeyError, ValueError, OSError) as exc:
            issue = f'无法从映射列读取关联值（{type(exc).__name__}）'
        if issue:
            raise ToolError(f'construction.data_dependency：销售表「{source.value}」的「{sale_map.columns[key]}」'
                            f'与预算表「{reference}」的「{ref_map.columns[ref_key]}」需要唯一对应。{issue}。')
    if any(var.scope == reference for scheme in ws.schemes for var in ws._ruleset(scheme).variables.values()):
        return None  # a saved rule may already rely on that entity's existing population
    coefficient = None
    if definition is None:
        from bonus_agent.coefficient import registered_coefficient_call

        coefficient = coefficient_source or registered_coefficient_call(ws, group_source, table.id, spec['scheme'])
        if coefficient is None:
            return None

    name, parent = _scope(ws, source.value, key)
    if not name:
        return None
    group_key = key
    mirrors = []
    if parent and any(n != reference and d.key == key for n, d in ws.datasets.items()):
        # Keep existing owners such as another optional reference table. The
        # new grouping key is backed by the same column on actual source rows.
        group_key = f'{key}_group_key'
        if any(group_key in d.fields for d in ws.datasets.values()):
            return None
        name, parent = _scope(ws, source.value, key, group_key=group_key)
        if not name:
            return None
        for n, d in ws.datasets.items():
            mapped = ws.mapping.get(n)
            if not mapped or key not in d.fields or (
                    mapped.file, mapped.sheet, mapped.columns.get(key)
            ) != (sale_map.file, sale_map.sheet, sale_map.columns[key]):
                continue
            field = {**d.fields[key].model_dump(exclude_defaults=True), 'column': mapped.columns[key]}
            mirrors.append({'operation': 'update', 'args': {'name': n, 'changes': {'fields': {group_key: field}}}})
        if len(mirrors) > 5:
            return None
    # A previously standalone reference can own the same key as the sales group.
    # Release that ownership before adding the group. An existing link can be
    # patched after the new parent is in place.
    datasets = ([{'operation': 'update', 'args': {'name': reference, 'changes': {'key': None}}}] if ref.key else [])
    if parent:
        datasets.append(parent)
    datasets.extend(alignment)
    datasets.extend(mirrors)
    patch = {'link': group_key, 'unmatched': 'ignore', 'unmatched_basis': exclusions[0].id}
    if group_key != ref_key:
        patch['fields'] = {group_key: {**owner.fields[key].model_dump(exclude_defaults=True), 'column': ref_map.columns[ref_key]}}
    try:
        placeholders = _empty_budget_labels(ws, ref_map, ref_key, months)
        if placeholders and placeholders.isdisjoint(ws._values(source.value, key)):
            filters = dict(ref_map.filter or {})
            if ref_map.columns[ref_key] not in filters:
                filters[ref_map.columns[ref_key]] = {'not': sorted(placeholders)}
                patch['filter'] = filters
    except (KeyError, ValueError):
        return None
    datasets.append({'operation': 'update', 'args': {'name': reference, 'changes': patch}})
    denominator = f'single_value({unparse(month_value)}, of: {reference_literal})'
    missing = f'count(of: {reference_literal}) == 0 or {denominator} == 0'
    expression = f'tier_ratio({unparse(table)}, {unparse(sales)}, {denominator}, {unparse(default)}, missing: {missing})'
    sources = list(value.get('sources') or ())
    for clause in (group_source, actual_sources[0], zero_sources[0]):
        citation = {'doc': clause.file, 'quote': clause.quote}
        if citation not in sources:
            sources.append(citation)
    formula = {'task': spec['id'], 'expected_scope': name,
               'value': {**value, 'scope': name, 'expr': expression, 'sources': sources}}
    return {'scheme': spec['scheme'], 'task': spec['id'], 'formulas': [formula, *project_formulas], 'datasets': datasets,
            **({'coefficient': coefficient} if coefficient else {})}


def budget_repair(board, scheme):
    ws = board.ws
    rules = ws._ruleset(scheme)
    owner = _group_owner(ws, rules)
    if owner is None:
        return None
    columns = ws.mapping[owner].columns
    exclusions = [c for c in ws.clauses if re.search(
        r'参考(?:数据|资料|表)里有[、，, ]*但[^。；;]{1,28}里没有[^。；;]{0,36}不参与核算', c.quote)]
    if len(exclusions) != 1:
        return None
    for factor, variable in rules.variables.items():
        reference = variable.scope
        if reference not in ws.datasets or reference == owner or not (ref := ws.datasets[reference]).key:
            continue
        spec = _same_plan_id(board, scheme, factor)
        ratio = _expression(variable.expr)
        if (not spec or not isinstance(ratio, Call) or ratio.fn != 'tier_ratio' or len(ratio.args) != 4
                or not isinstance(ratio.args[1], Name) or not isinstance(ratio.args[3], Num)):
            continue
        numerator = ratio.args[1].id
        if numerator not in rules.variables or rules.variables[numerator].scope != 'global':
            continue
        summed = _expression(rules.variables[numerator].expr)
        plan = _same_plan_id(board, scheme, numerator)
        if not plan or not plan['clauses'] or not isinstance(summed, Call) or summed.fn != 'sum':
            continue
        source = summed.kw().get('of')
        reference_map = ws.mapping[reference]
        ref_header = _group_header(_source_column_name(reference_map.columns[ref.key]))
        clause = None
        for row in ws.clauses:
            if row.id in spec['clauses'] and '预算' in row.quote and '销售额' in row.quote and '查不到' in row.quote:
                text = _source_column_name(row.quote)
                if ref_header and ref_header in text:
                    clause = row
                    break
        if clause is None or not isinstance(source, Str) or source.value not in ws.datasets:
            continue
        heading = _source_column_name(clause.quote).split('销售额')[0]
        candidates = []
        for key, column in columns.items():
            if key not in ws.datasets[source.value].fields or ws.datasets[owner].fields[key].type not in ('id', 'text', 'enum'):
                continue
            label = _group_header(_source_column_name(column))
            if label and label in heading:
                candidates.append((heading.index(label), key))
        candidates.sort()
        if not candidates:
            continue
        tied = [key for position, key in candidates if position == candidates[0][0]]
        if len(tied) > 1:
            # Two semantic names can point to exactly the same physical field.
            # Reuse the one with an established active group in that case.
            if len({columns[key] for key in tied}) != 1:
                continue
            established = {d.key for n, d in ws.datasets.items() if n != reference and d.key in tied
                           and ws.mapping[n].file == ws.mapping[owner].file}
            if len(established) != 1:
                continue
            key, = established
        else:
            key, = tied
        scope, parent = _scope(ws, owner, key)
        if scope is None:
            continue
        args = [unparse(part) for part in ratio.args]
        table_name = json.dumps(reference, ensure_ascii=False)
        period_value = f'single_value({args[2]}, of: {table_name})'
        missing = f'count(of: {table_name}) == 0'
        zero_policy = next((c for c in ws.clauses if c.id in spec['clauses']
                            and re.search(r'预算为\s*0\b', c.quote)), None)
        if zero_policy:
            missing += f' or {period_value} == 0'
        expr = f'tier_ratio({args[0]}, {args[1]}, {period_value}, {args[3]}, missing: {missing})'
        patch = {'link': key, 'unmatched': 'ignore', 'unmatched_basis': exclusions[0].id}
        if key != ref.key:
            patch['fields'] = {key: {**ws.datasets[owner].fields[key].model_dump(exclude_defaults=True),
                                     'column': reference_map.columns[ref.key]}}
        datasets = [{'operation': 'update', 'args': {'name': reference, 'changes': {'key': None}}}]
        if parent:
            datasets.append(parent)
        datasets.append({'operation': 'update', 'args': {'name': reference, 'changes': patch}})
        formulas = [{'task': plan['id'], 'changes': {'scope': scope}, 'expected_scope': scope},
                    {'task': spec['id'], 'changes': {'scope': scope, 'expr': expr}, 'expected_scope': scope}]
        for other, value in rules.variables.items():
            if (other_plan := _same_plan_id(board, scheme, other)) and (tree := _expression(value.expr)):
                repaired = _move_reference(tree, reference, factor, ratio.args[3])
                if repaired != tree:
                    formulas.append({'task': other_plan['id'], 'changes': {'expr': unparse(repaired)}})
        return {
            'anchor': plan['id'], 'scheme': scheme, 'code': 'budget_scope_stated_by_source',
            'evidence': {'key': key, 'reference_header': reference_map.columns[ref.key],
                         'clauses': [{'id': c.id, 'file': c.file, 'quote': c.quote}
                                     for c in (clause, exclusions[0], zero_policy) if c]},
            'candidate_call': {'tool': 'submit_construction_repair', 'args': {
                'scheme': scheme, 'task': plan['id'], 'formulas': formulas, 'datasets': datasets,
            }},
        }
    return None
