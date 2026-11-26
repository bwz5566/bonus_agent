"""Compile an explicit conditional source expression over already checked data."""

from dataclasses import replace
import json
import re
from unicodedata import normalize

from bonus_agent import clauses, facts
from bonus_agent.data_profile import _source_column_name
from bonus_agent.formula_inputs import with_requirements
from bonus_agent.indicator_scope import _expression
from bonus_core.dsl.parser import BinOp, Call, Name, Neg, Num, Str, unparse, walk


def _conditional_parts(clause):
    quote = normalize('NFKC', clause.quote)
    return re.fullmatch(r'\s*(?:[^|]+\|\s*)?(?:当|若)?([^,，]+?)(?:属于|为|=)\s*([A-Z][A-Z0-9_]*)\s*[,，]'
                        r'\s*[^,，=]+=\s*([^,，]+)[,，]\s*否则直接使用([^。.;；]+)[。.;；]?\s*', quote)


def explicit_expression_clause(ws, spec):
    if not spec['target'].startswith('/variables/'):
        return None
    matched = [c for c in ws.clauses if c.id in spec['clauses'] and _conditional_parts(c)]
    return matched[0] if len(matched) == 1 else None


def conditional_result(clause):
    """Name of the assigned result in a supported conditional expression."""
    if not _conditional_parts(clause):
        return None
    statement = normalize('NFKC', clause.quote).split('|', 1)[-1].split(',', 2)[1]
    return statement.split('=', 1)[0].strip()


def _aggregate_parts(quote):
    return re.search(r'(?:^|[,。;])\s*(?:先)?(?:合计|汇总)(?:每个|各)'
                     r'(?P<scope>[^\s、,。;()]{1,24}?)的(?P<amount>[^、,。;(\n]{1,32})'
                     r'(?:\((?P<qualifier>[^()\n]+)\))?', normalize('NFKC', quote))


def grouped_sum_clause(ws, spec):
    scope = spec.get('expected_scope')
    if not scope or not spec['target'].startswith('/variables/'):
        return None
    matches = [c for c in ws.clauses if c.id in spec['clauses'] and (part := _aggregate_parts(c.quote))
               and facts.scope_norm(part['scope']) == facts.scope_norm(scope)]
    return matches[0] if len(matches) == 1 else None


def grouped_sum_input(board, spec):
    """Sum the explicitly named measure over the already checked, period-scoped detail."""
    from bonus_agent.data_profile import _source_sheet

    ws = board.ws
    clause = grouped_sum_clause(ws, spec)
    if not clause or not ws.period or clauses.check_quote(clause.file, clause.quote, ws._corpus()):
        return None
    rules = ws._ruleset(spec['scheme'])
    scope = spec['expected_scope']
    entity = rules.datasets.get(scope)
    level = rules.level(scope)
    key = entity.key if entity else level.key if level else None
    if not key:
        return None

    declaration = _aggregate_parts(clause.quote)
    qualifier = declaration['qualifier']
    base_only = not qualifier or facts.scope_norm(qualifier) == '计算基数'

    def heading(value):
        text = _source_column_name(value)
        if base_only:
            text = text.split('(', 1)[0]
        return facts.scope_norm(text).replace('销售额', '销售金额')

    measure = heading(declaration['amount'] if base_only else f'{declaration["amount"]}({qualifier})')
    found = []
    for requirement in spec.get('data_requirements', ()):
        dataset = requirement['dataset']
        definition, mapping = rules.datasets.get(dataset), ws.mapping.get(dataset)
        if not definition or not mapping or definition.key or definition.link != key or not definition.period:
            continue
        if definition.window != 'period' or requirement.get('window') != 'period' or mapping.unpivot:
            continue
        fields = [f for f in requirement['fields'] if f in definition.fields and f in mapping.columns
                  and definition.fields[f].type == 'money' and heading(mapping.columns[f]) == measure]
        if len(fields) == 1:
            found.append((dataset, fields[0]))
    if len(found) != 1:
        return None
    dataset, field = found[0]
    mapping = ws.mapping[dataset]
    try:
        headers = [c for c in _source_sheet(ws, mapping).headers if heading(c) == measure]
    except (KeyError, OSError, ValueError):
        return None
    if headers != [mapping.columns[field]]:
        return None
    required = {'dataset': dataset, 'fields': [field, key], 'link': key,
                'window': 'period', 'period_field': rules.datasets[dataset].period}
    requirements = with_requirements(spec, required)
    if requirements is None:
        return None
    return {'source': {'file': mapping.file, 'sheet': mapping.sheet}, 'contract': required,
            'candidate_call': {'scheme': spec['scheme'], 'task': spec['id'], 'formulas': [{
                'task': spec['id'], 'expected_scope': scope, 'data_requirements': requirements,
                'value': {'scope': scope, 'expr': f'sum({field}, of: {json.dumps(dataset, ensure_ascii=False)})',
                          'sources': [{'doc': clause.file, 'quote': clause.quote}]},
            }]}}


def _substitute(node, fields):
    if isinstance(node, Name):
        return Name(fields[node.id])
    if isinstance(node, BinOp):
        return replace(node, left=_substitute(node.left, fields), right=_substitute(node.right, fields))
    if isinstance(node, Neg):
        return replace(node, operand=_substitute(node.operand, fields))
    return node


def explicit_expression_input(board, spec):
    ws = board.ws
    clause = explicit_expression_clause(ws, spec)
    if clause is None or clauses.check_quote(clause.file, clause.quote, ws._corpus()):
        return None
    selector, category, arithmetic, unchanged = _conditional_parts(clause).groups()
    expression = _expression(arithmetic)
    if not expression or not all(isinstance(node, (BinOp, Name, Neg, Num)) for node in walk(expression)):
        return None
    headers = {node.id for node in walk(expression) if isinstance(node, Name)}
    if unchanged not in headers:
        return None
    numbers = sorted({node.value for node in walk(expression) if isinstance(node, Num)})
    statuses = board.refresh()
    rules = ws._ruleset(spec['scheme'])
    foundations = []
    for task in spec['depends_on']:
        dep = board.task_spec(task)
        parts = facts.target_parts(dep['target'])
        if dep['scheme'] != spec['scheme'] or parts[0] != 'variables' or statuses[task]['status'] != 'PASS':
            continue
        var = rules.variables.get(parts[1])
        if not var or spec.get('expected_scope') not in (None, var.scope):
            continue
        summed = _expression(var.expr)
        if (isinstance(summed, Call) and summed.fn == 'sum' and len(summed.args) == 1
                and isinstance(amount := summed.args[0], Name) and set(summed.kw()) == {'of'}
                and isinstance(source := summed.kw()['of'], Str) and source.value in ws.mapping):
            foundations.append((var.scope, source.value, amount.id))
    if len(set(foundations)) != 1:
        return None
    scope, dataset, base = foundations[0]
    definition, mapping = rules.datasets[dataset], ws.mapping[dataset]
    if base not in mapping.columns or facts.scope_norm(unchanged) not in facts.scope_norm(_source_column_name(mapping.columns[base])):
        return None
    fields = {unchanged: base}
    for source in sorted((headers - {unchanged}) | {selector}):
        header = facts.scope_norm(source)
        choices = [field for field, column in mapping.columns.items() if field in definition.fields
                   and facts.scope_norm(_source_column_name(column)).startswith(header)
                   and definition.fields[field].type in (('text', 'enum', 'id') if source == selector
                                                          else ('money', 'number', 'ratio', 'int'))]
        if len(choices) != 1:
            return None
        fields[source] = choices[0]
    cooked = unparse(_substitute(expression, fields))
    literal = lambda text: json.dumps(text, ensure_ascii=False)
    expr = f'sum(if({fields[selector]} == {literal(category)}, {cooked}, {base}), of: {literal(dataset)})'
    required = {'dataset': dataset, 'fields': sorted(set(fields.values()))}
    if definition.period:
        required.update(window=definition.window, period_field=definition.period)
    if definition.link:
        required['link'] = definition.link
    requirements = {r['dataset']: dict(r) for r in spec.get('data_requirements', ())}
    existing = requirements.get(dataset, {'fields': []})
    if any(key in existing and existing[key] != value for key, value in required.items() if key != 'fields'):
        return None
    requirements[dataset] = {**existing, **required, 'fields': sorted(set(required['fields']) | set(existing['fields']))}
    parameters = [format(n, 'f') for n in numbers]
    names = {value: f'{spec["target"]} 原文常量 {value}' for value in parameters}
    return {
        'source': {'file': mapping.file, 'sheet': mapping.sheet}, 'contract': required,
        'candidate_call': {'scheme': spec['scheme'], 'task': spec['id'], 'formulas': [{
            'task': spec['id'], 'value': {'scope': scope, 'expr': expr, 'sources': [{'doc': clause.file, 'quote': clause.quote}]},
            'expected_scope': scope, 'data_requirements': list(requirements.values()),
            'facts': [{'name': names[value], 'file': clause.file, 'quote': clause.quote, 'value': value} for value in parameters],
            'bindings': [{'name': names[value], 'targets': [spec['scheme'] + '::' + spec['target'] + '/expr']}
                         for value in parameters],
        }]},
    }
