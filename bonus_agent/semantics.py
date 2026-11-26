"""Check explicit algorithm statements against the rules that claim to implement them."""
import re
import unicodedata

from bonus_agent import clauses as cl, facts as fx
from bonus_core.dsl.parser import Call, Cmp, Name, Str, ExprError, parse, walk
from bonus_core.dsl.scope import is_aggregate


def stated_modes(text):
    """Find the explicitly affirmed algorithms in a source quotation."""
    text = fx.scope_norm(text)
    found = set()
    patterns = (
        ('excess', r'超额累[进進]|超额部分分段累[加计]|每段按各自(?:的)?比例|excess|marginal'),
        ('whole', r'(?:全|整)额(?:累[进進]|分[档檔])|整[笔筆]按所在[档檔]|wholeamount'),
    )
    for mode, pattern in patterns:
        for match in re.finditer(pattern, text):
            if re.search(r'不|非|取消|禁止', text[max(0, match.start()-3):match.start()]):
                continue
            found.add(mode)
    return found


def stated_mode(text):
    found = stated_modes(text)
    return next(iter(found)) if len(found) == 1 else None


def preserves_negative(text):
    text = fx.scope_norm(text)
    return bool(re.search(r'负数|负金额|负奖金|负提成', text) and
                re.search(r'照算|(?<!不)保留负|不归零|不得归零|不记为0|不能记为0', text))


def mode_basis(ws, basis):
    from bonus_agent.agent import ToolError
    error = cl.check_quote(basis.get('file', ''), basis.get('quote', ''), ws._corpus())
    if error:
        raise ToolError('algorithm.source：' + error)
    if basis.get('page') is not None and basis['page'] not in ws._quote_pages(basis['quote'], ws._page_texts(basis['file'])):
        raise ToolError('algorithm.source：算法引用不在所填页码')
    mode = stated_mode(basis['quote'])
    if mode is None:
        raise ToolError('algorithm.source：引用须明确且不矛盾地说明超额累进或整额/全额分档算法；不能从数字或表名猜测')
    return mode


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            if k not in ('sources', 'desc'):
                yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def target_mode_issues(ws, scheme, target, clause_ids):
    draft = ws._merged(scheme)
    try:
        owner = fx.target_value(draft, target)
    except (KeyError, IndexError, TypeError, ValueError):
        return []
    issues = []
    for clause in ws.clauses:
        if clause.id not in clause_ids:
            continue
        expected = stated_mode(clause.quote)  # use the verified source, not a model paraphrase
        negative = preserves_negative(clause.quote)
        if expected is None and not negative:
            continue
        error = cl.check_quote(clause.file, clause.quote, ws._corpus())
        if error:
            issues.append(f'algorithm.source：{clause.id} {error}')
            continue
        parts = fx.target_parts(target)
        tables = {parts[1]} if len(parts) >= 2 and parts[0] == 'tables' else set()
        todo, seen = list(_strings(owner)), set()
        while todo:
            text = todo.pop()
            if text in seen:
                continue
            seen.add(text)
            try:
                ast = parse(text)
            except ExprError:
                continue
            for node in walk(ast):
                if isinstance(node, Call) and node.fn == 'tier' and node.args and isinstance(node.args[0], Name):
                    tables.add(node.args[0].id)
                if isinstance(node, Name) and node.id in draft.get('variables', {}):
                    todo.append(draft['variables'][node.id]['expr'])
        if not tables and expected:
            issues.append(f'algorithm.scope：{clause.id}明确规定{expected}，但{scheme}::{target}没有对应的tier金额阶梯；'
                          '把该条款关联到金额公式或具体阶梯表，不能用整额费率乘总量替代超额分段')
        for name in sorted(tables):
            table = draft.get('tables', {}).get(name, {})
            if expected and table.get('mode', 'whole') != expected:
                issues.append(f'algorithm.mode：{scheme}::/tables/{name}使用{table.get("mode", "whole")}，'
                              f'{clause.id}《{clause.file}》原文「{clause.quote}」要求{expected}；'
                              '用compile_table按原始参数重编并提供algorithm_basis，不能只改说明')
            if negative and table.get('below_first', 'zero') != 'first':
                issues.append(f'algorithm.negative：{scheme}::/tables/{name}低于首档时归零，'
                              f'{clause.id}原文「{clause.quote}」要求保留负值；按原文核对below_first，不能静默截断')
    return issues


def _identity_literal_witnesses(ws, rules, name, clause_ids):
    variable = rules.variables[name]
    level = rules.level(variable.scope)
    entity = rules.entity_dataset(level.key) if level else None
    dataset = variable.scope if variable.scope in ws.mapping else entity[0] if entity else None
    if dataset not in ws.mapping or dataset not in rules.datasets:
        return None, []
    try:
        expression = parse(variable.expr)
    except ExprError:
        return None, []

    fields = rules.datasets[dataset].fields
    sources = [(s.doc, s.page, s.quote) for s in variable.sources]
    sources += [(c.file, None, c.quote) for c in ws.clauses if c.id in clause_ids]
    values_by_field = {}
    exact_sources = {}
    issues = []
    nodes = list(walk(expression))
    aggregate_comparisons = {id(node) for call in nodes if isinstance(call, Call) and is_aggregate(call)
                             for node in walk(call) if isinstance(node, Cmp)}
    for node in nodes:
        if not isinstance(node, Cmp) or node.op not in ('==', '!='):
            continue
        if id(node) in aggregate_comparisons:
            continue
        field, literal = node.left, node.right
        if isinstance(field, Str):
            field, literal = literal, field
        if (not isinstance(field, Name) or not isinstance(literal, Str) or not literal.value
                or field.id in rules.variables or field.id not in fields
                or fields[field.id].type not in ('id', 'text', 'enum')):
            continue
        normalized = unicodedata.normalize('NFKC', literal.value)
        for doc, page, quote in sources:
            if literal.value in quote or normalized not in unicodedata.normalize('NFKC', quote):
                continue
            key = (doc, page)
            if key not in exact_sources:
                if doc not in ws._corpus():
                    continue
                raw = '\n'.join(ws._lines(doc)) if page is None else ws._page_raw(doc).get(page, '')
                exact_sources[key] = ' '.join(raw.split())
            normalized_quote = ' '.join(quote.split())
            if normalized_quote not in exact_sources[key]:
                # Spreadsheet quotes may span cells from one physical row. The
                # excerpt renderer adds separators that were not present in those cells.
                same_sheet_row = page is None and any(
                    normalized_quote in ' '.join(' '.join(str(cell).split()) for cell in row if cell not in (None, ''))
                    for sheet in ws.inv.sheets if sheet.file == doc
                    for _, row in sheet.rows
                )
                if not same_sheet_row:
                    continue
            if field.id not in values_by_field:
                try:
                    values_by_field[field.id] = set(ws._values(dataset, field.id))
                except (IndexError, KeyError, ValueError):
                    # The data gate owns missing sheets, columns and invalid period values.
                    values_by_field[field.id] = set()
            values = values_by_field[field.id]
            if literal.value in values:
                continue
            equivalent = {value for value in values if unicodedata.normalize('NFKC', value) == normalized}
            alternatives = {value for value in equivalent if value in quote}
            if len(alternatives) == 1:
                actual, = alternatives
                issues.append((literal, actual, doc, dataset, field.id, quote, len(equivalent) == 1))
    return expression, issues


def identity_literal_issues(ws, rules, name, clause_ids=()):
    _, witnesses = _identity_literal_witnesses(ws, rules, name, clause_ids)
    return list(dict.fromkeys(
        f'construction.identity_literal：变量「{name}」比较「{dataset}.{field}」'
        f'时用的是 {literal.value!r}。材料《{doc}》原文和该列有 {actual!r}，'
        '当前写法在该列无匹配。核对字符后修正公式，保留原始标识值。'
        for literal, actual, doc, dataset, field, _, _ in witnesses
    ))


def tier_dispatch_issues(ws, rules, name, declared_dependencies):
    """Verify declared classification inputs and live labels used to select amount tiers."""
    from bonus_agent.agent import ToolError
    from bonus_agent.lint import _near_values

    variable = rules.variables[name]
    try:
        nodes = list(walk(parse(variable.expr)))
    except ExprError:
        return []
    if not any(isinstance(node, Call) and node.fn == 'tier' for node in nodes):
        return []

    used = set()
    todo = [node.id for node in nodes if isinstance(node, Name)]
    while todo:
        ref = todo.pop()
        if ref in used:
            continue
        used.add(ref)
        if ref in rules.variables:
            try:
                todo.extend(node.id for node in walk(parse(rules.variables[ref].expr)) if isinstance(node, Name))
            except ExprError:
                continue

    issues = []
    for path in declared_dependencies:
        parts = fx.target_parts(path)
        if len(parts) != 2 or parts[0] != 'variables' or parts[1] in used or parts[1] not in rules.variables:
            continue
        for source in rules.variables[parts[1]].sources:
            text = fx.scope_norm(source.quote)
            if (re.search(r'新项目|新客户', text) and re.search(r'(?:老|旧)(?:项目|客户)', text)
                    and re.search(r'判定|归类|界限|区分', text)
                    and not cl.check_quote(source.doc, source.quote, ws._corpus())):
                issues.append(f'algorithm.tier_classification：变量「{name}」计划依赖「{parts[1]}」，'
                              f'《{source.doc}》原文「{source.quote}」明确区分新旧项目，当前阶梯公式没有使用该分类。'
                              '按制度费率列分别建立表，并在适用区域明确选用新项目或旧项目费率。')
                break

    level = rules.level(variable.scope)
    entity = rules.entity_dataset(level.key) if level else None
    dataset = variable.scope if variable.scope in ws.mapping else entity[0] if entity else None
    if dataset not in ws.mapping or dataset not in rules.datasets:
        return issues
    fields = rules.datasets[dataset].fields or {}
    values = {}
    for call in nodes:
        if not isinstance(call, Call) or call.fn != 'if' or not call.args:
            continue
        for node in walk(call.args[0]):
            if not isinstance(node, Cmp) or node.op not in ('==', '!='):
                continue
            left, right = (node.right, node.left) if isinstance(node.left, Str) else (node.left, node.right)
            if (not isinstance(left, Name) or not isinstance(right, Str) or not right.value or left.id in rules.variables
                    or left.id not in fields or fields[left.id].type not in ('text', 'enum', 'id')):
                continue
            if left.id not in values:
                try:
                    values[left.id] = {value: 1 for value in ws._values(dataset, left.id) if isinstance(value, str)}
                except (ToolError, ValueError, KeyError, IndexError):
                    values[left.id] = {}
            actual = values[left.id]
            if right.value in actual or not (near := _near_values(right.value, actual)):
                continue
            issues.append(f'algorithm.tier_label：{dataset}.{left.id} 没有 {right.value!r}；实际相近取值为 '
                          f'{"、".join(near[:6])}。按原文核对地区或分类映射后再选择阶梯，避免这些记录落入默认分支。')
    return list(dict.fromkeys(issues))


def ground_identity_literals(ws, rules, name, clause_ids):
    """Bind a comparison literal when the source and its field identify one exact spelling."""
    from dataclasses import fields, is_dataclass, replace
    from bonus_core.dsl.parser import unparse

    expression, witnesses = _identity_literal_witnesses(ws, rules, name, clause_ids)
    substitutions, sources = {}, {}
    for literal, actual, doc, dataset, field, quote, unique in witnesses:
        if unique and '"' not in actual and '\n' not in actual:
            substitutions[id(literal)] = Str(actual)
            sources[id(literal)] = {'variable': name, 'dataset': dataset, 'field': field,
                                    'submitted': literal.value, 'physical': actual, 'document': doc, 'quote': quote}
    if not substitutions:
        return rules.variables[name].expr, []

    def rewrite(value):
        if id(value) in substitutions:
            return substitutions[id(value)]
        if isinstance(value, tuple):
            return tuple(rewrite(item) for item in value)
        if is_dataclass(value):
            return replace(value, **{field.name: rewrite(getattr(value, field.name)) for field in fields(value)})
        return value

    return unparse(rewrite(expression)), list(sources.values())


def semantic_gates(ws):
    issues = []
    for scheme in ws.schemes:
        rules = ws._ruleset(scheme)
        for name in rules.variables:
            issues += identity_literal_issues(ws, rules, name)
        for name, table in ws._merged(scheme).get('tables', {}).items():
            for source in table.get('sources', []):
                expected = stated_mode(source.get('quote', ''))
                if expected and table.get('mode', 'whole') != expected:
                    issues.append(f'algorithm.mode：{scheme}::/tables/{name}与其原文算法依据{expected}矛盾，按来源重编')
    for clause in ws.clauses:
        if stated_mode(clause.quote) is None and not preserves_negative(clause.quote):
            continue
        if clause.where.startswith(cl.BUILTIN):
            issues.append(f'algorithm.scope：{clause.id}明确规定阶梯算法，必须关联具体表或金额公式；引擎支持多种算法，不能用“引擎自带”代替选择')
        for ref in cl.refs(clause.where):
            if ref == 'pool' or ref.startswith(('variables.', 'tables.')):
                target = '/' + '/'.join(p.replace('~', '~0').replace('/', '~1') for p in ref.split('.', 1))
                for scheme in ws.schemes:
                    issues += target_mode_issues(ws, scheme, target, [clause.id])
    return list(dict.fromkeys(issues))
