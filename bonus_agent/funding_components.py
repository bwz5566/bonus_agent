"""Plan named amount prerequisites from verified source expressions."""
import copy
import re
from unicodedata import normalize

from bonus_agent import facts
from bonus_agent.clauses import check_quote
from bonus_agent.funding_plan import _current_sales, pool_input_gap
from bonus_agent.indicator_budget import _budget_groups
from bonus_agent.indicator_expressions import conditional_result
from bonus_agent.indicator_history import historical_average_quote


_NAME = r'[^\W\d]\w*'
_PRODUCT = re.compile(rf'{_NAME}(?:\s*[×*]\s*{_NAME})+')
_EQUATION = re.compile(r'(?:^|[:：]|先算)\s*(?:「([^」]+)」|([^\W\d]\w*))\s*=\s*([^。；;\n]+)')
_NAMED_AMOUNT = re.compile(r'记为「([^」]+)」')


def _label(text):
    return re.sub(r'\W+', '', facts.scope_norm(text)).replace('总金额', '总额')


def _uses_calculation_basis(quote, name):
    for fragment in re.split(r'[。；;，,]', quote):
        if re.search(r'不用|不按|不采用|不使用|不得|不要|而非', fragment):
            continue
        if re.search(r'(?:按|采用|使用|取|以).{0,12}' + re.escape(_label(name)), _label(fragment)):
            return True
    return False


def _product(ws, clauses, root_clauses):
    """Accept one named product, recorded verbatim in both the ledger and a clause."""
    subjects = [_label(c.quote) for c in root_clauses]
    candidates = {}
    for fact in ws.facts:
        if not fact.name.endswith('公式') or fact.source_hash != ws._source_version.get(fact.file):
            continue
        label = _label(fact.name[:-2])
        if not re.fullmatch(_NAME, label) or not any(label in subject for subject in subjects):
            continue
        text = normalize('NFKC', fact.value).strip()
        if not _PRODUCT.fullmatch(text) or normalize('NFKC', fact.quote).strip() != text:
            continue
        matching = [c.id for c in clauses if c.file == fact.file and normalize('NFKC', c.quote).strip() == text]
        terms = [_label(term) for term in re.split(r'[×*]', text)]
        if matching and len(set(terms)) == len(terms) and label not in terms:
            candidates[(label, tuple(terms))] = matching
    if len(candidates) != 1:
        return None
    (label, terms), cited = next(iter(candidates.items()))
    return label, terms, cited


def product_clause(ws, spec):
    if not spec['target'].startswith('/variables/') or len(spec['depends_on']) < 2:
        return None
    matches = [c for c in ws.clauses if c.id in spec['clauses'] and _PRODUCT.fullmatch(normalize('NFKC', c.quote).strip())]
    return matches[0] if len(matches) == 1 else None


def product_input(board, spec):
    ws = board.ws
    clause = product_clause(ws, spec)
    if clause is None or check_quote(clause.file, clause.quote, ws._corpus()) or not spec.get('expected_scope'):
        return None
    name = facts.target_parts(spec['target'])[1]
    label = _label(name)
    quote = normalize('NFKC', clause.quote).strip()
    if not any(f.name.endswith('公式') and _label(f.name[:-2]) == label and f.file == clause.file
               and f.source_hash == ws._source_version.get(f.file) and f.quote == clause.quote
               and normalize('NFKC', f.value).strip() == quote for f in ws.facts):
        return None
    dependencies = [board.task_spec(n) for n in spec['depends_on']]
    factors = {facts.target_parts(t['target'])[1] for t in dependencies
               if t['scheme'] == spec['scheme'] and t['target'].startswith('/variables/')}
    sequence = [term.strip() for term in re.split(r'[×*]', quote)]
    if set(sequence) != factors or len(sequence) != len(factors):
        return None
    item = {'task': spec['id'], 'value': {'scope': spec['expected_scope'], 'expr': ' * '.join(sequence),
                                         'sources': [{'doc': clause.file, 'quote': clause.quote}]}}
    return {'candidate_call': {'scheme': spec['scheme'], 'task': spec['id'], 'formulas': [item]},
            'product': sequence}


def _arithmetic_branches(root_clauses, product):
    assignments = {}
    for clause in root_clauses:
        if pair := _EQUATION.search(clause.quote):
            title, plain, rhs = pair.groups()
            name = _label(title or plain)
        elif pair := _NAMED_AMOUNT.search(clause.quote):
            name, rhs = _label(pair[1]), clause.quote[:pair.start()]
        else:
            continue
        if re.fullmatch(_NAME, name) and name != product and product in _label(rhs):
            assignments.setdefault(name, []).append((clause, rhs))
    return {name: candidates[0] for name, candidates in assignments.items() if len(candidates) == 1}


def _snapshot_clauses(ws, root, verified, name):
    from bonus_agent.indicator_snapshot import _match, _metric, snapshot_history_clauses

    sources = snapshot_history_clauses(ws, [r['dataset'] for r in root.get('data_requirements', ())])
    stem = name.removesuffix('系数')
    return [clause.id for clause in verified if clause.id in sources
            and (match := _match(clause.quote)) and stem in _metric(match[2])]


def _draft_tasks(board, root, verified, root_clauses, product):
    ws, tasks = board.ws, board.state['tasks']
    scheme, ident = root['scheme'], root['id']
    label, factors, clauses = product
    anchors = {n: tasks[n] for n in root['depends_on']}
    by_clause = {c.id: c for c in verified}
    period, history = [], []
    for n, spec in anchors.items():
        value = board.value(spec)
        if (spec['scheme'] != scheme or not spec['target'].startswith('/variables/')
                or not isinstance(value, dict) or not value.get('scope')):
            return []
        quotes = [by_clause[c].quote for c in spec['clauses'] if c in by_clause]
        if any(_current_sales(quote) for quote in quotes):
            period.append(n)
        elif any(historical_average_quote(quote) for quote in quotes):
            history.append(n)
        else:
            return []  # A substantive monetary plan already exists.
    if not period or len(period) > 1 or len(history) > 1:
        return []
    anchor = period[0]
    scope = board.value(anchors[anchor])['scope']
    branches = _arithmetic_branches(root_clauses, label)
    descriptors = {}

    def add(name, cited, deps=(), *, scope=None, title=None):
        spec = {'id': f'{ident}:amount:{name}', 'scheme': scheme, 'target': '/variables/' + name,
                'title': title or name, 'clauses': list(dict.fromkeys(cited)), 'depends_on': list(dict.fromkeys(deps))}
        if scope:
            spec['expected_scope'] = scope
        descriptors[name] = spec
        return spec['id']

    # Compile a documented conditional basis with the same checked sales input.
    for factor in factors:
        direct = [c for c in verified if factor in _label(c.quote) and _label(c.quote) != _label('*'.join(factors))]
        basis = [(name, c) for c in verified if (name := conditional_result(c))
                 and any(_uses_calculation_basis(other.quote, name) for other in direct)]
        if factor.endswith('系数'):
            subject = factor.removesuffix('系数')
            group_rules = [c for c in verified if any(_label(group).startswith(subject) for group in _budget_groups(c.quote))]
            if len(group_rules) == 1:
                direct = [*group_rules, *(c for c in root_clauses if _current_sales(c.quote)
                                         or ('预算' in c.quote and '系数' in c.quote)), *direct]
            else:
                direct += [c for c in verified if subject + '率' in _label(c.quote)]
        dep = [anchor]
        if len(basis) == 1:
            name, source = basis[0]
            key = _label(name)
            if re.fullmatch(_NAME, key) and key not in {*factors, label, *branches}:
                dep.append(add(key, [source.id], [anchor], scope=scope, title=name))
        add(factor, [c.id for c in direct[:8]] or clauses, dep)

    gross = add(label, clauses, [descriptors[name]['id'] for name in factors], scope=scope)
    for name, (clause, rhs) in branches.items():
        for symbol in re.findall(r'\(\s*1\s*[-−]\s*([^\W\d]\w*)\s*\)', rhs):
            key = _label(symbol)
            if key in descriptors or key in branches:
                continue
            rules = _snapshot_clauses(ws, root, verified, key)
            if len(rules) == len(history) == 1:
                add(key, rules, history, scope=board.value(anchors[history[0]])['scope'])
            else:
                add(key, [clause.id], history)
        related = [c.id for c in root_clauses if name in _label(c.quote)]
        add(name, [clause.id, *related], [gross], scope=scope)
    for name, (_, rhs) in branches.items():
        expression = _label(rhs)
        spec = descriptors[name]
        spec['depends_on'] += [item['id'] for k, item in descriptors.items()
                               if k not in (name, label) and k in expression]

    table_fields = {field for ds in ws.datasets.values() for field in ds.fields}
    occupied_targets = {t['target'] for t in tasks.values() if t['scheme'] == scheme}
    rules = ws._merged(scheme)
    occupied_names = table_fields | rules.get('variables', {}).keys() | rules.get('tables', {}).keys()
    if any(t['id'] in tasks or t['target'] in occupied_targets or name in occupied_names
           for name, t in descriptors.items()):
        return []
    root = {**root, 'depends_on': [*root['depends_on'], gross, *(descriptors[n]['id'] for n in branches)]}
    return [*descriptors.values(), root]


def prepare_funding_components(ws):
    """Require intermediate tasks before trying a high-level monetary formula."""
    from bonus_agent.agent import ToolError
    from bonus_agent.construction import TaskBoard

    if not ws._flow().current('data'):
        return None
    board = TaskBoard(ws)
    if pool_input_gap(board):
        return None
    roots = [t for t in board.state['tasks'].values() if t['target'] == '/pool' and board.value(t) is None]
    if not roots:
        return None
    corpus = ws._corpus()
    verified = [c for c in ws.clauses if not check_quote(c.file, c.quote, corpus)]
    for root in roots:
        root_clauses = [c for c in verified if c.id in root['clauses']]
        product = _product(ws, verified, root_clauses)
        if not product or not (parts := _draft_tasks(board, root, verified, root_clauses, product)):
            continue
        result = {'task': root['id'], 'scheme': root['scheme'], 'target': root['target'],
                  'status': 'FAIL', 'saved': False}
        candidate = copy.deepcopy(ws, {id(ws.llm): ws.llm})
        try:
            candidate._check_source_version()
            report = candidate.plan_formula_tasks(parts)
            candidate_flow = candidate._flow()
            if not candidate_flow.current('data') and candidate.prepare_data()['status'] != 'PASS':
                raise ToolError('拆分后的方案未通过现有输入范围复验')
            candidate_board = TaskBoard(candidate)
            if candidate_board.statuses(with_confidence=False)[root['id']]['status'] != 'WAITING':
                raise ToolError('金额前置任务尚未阻止过早构建奖金池')
            if any(name.startswith('approved:') and ws._flow().current(name) and not candidate_flow.current(name)
                   for name in ws.workflow['receipts']):
                raise ToolError('候选会使已有财务确认失效')
            candidate._check_source_version()
        except (ToolError, ValueError, KeyError, TypeError, OSError) as exc:
            return {**result, 'issues': [str(exc)]}
        ws.__dict__.update(candidate.__dict__)
        return {**result, 'status': 'PASS', 'saved': True, 'validated_targets': report['validated_targets'],
                'formula': {'amount': product[0], 'factors': list(product[1]), 'clauses': product[2]}}
    return None
