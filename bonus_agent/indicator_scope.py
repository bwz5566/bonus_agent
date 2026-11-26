"""Observations from the current formula, prepared population and physical time coverage."""
from __future__ import annotations

from bonus_core.data.population import prepare
from bonus_core.dsl.functions import FUNCTIONS
from bonus_core.dsl.parser import Call, ExprError, Name, Str, names, parse, walk
from bonus_core.engine.evaluator import EvalError, Evaluator
from bonus_core.engine.issues import EngineError
from bonus_core.engine.tree import build_tree


def _expression(expr):
    try:
        return parse(expr)
    except ExprError:
        return None


def _aggregates(tree):
    for item in walk(tree):
        if not isinstance(item, Call):
            continue
        spec = FUNCTIONS.get(item.fn)
        if spec and spec.aggregate and not (item.fn in ('min', 'max') and len(item.args) > 1):
            yield item


def indicator_period_findings(expr, history_sources):
    """Read only a current preparation report. Candidate months do not prove equivalent business scope."""
    sources = {owner.value for call in _aggregates(_expression(expr))
               if isinstance(owner := call.kw().get('of'), Str)}
    findings = []
    for item in history_sources:
        dataset = item['dataset']
        if dataset not in sources:
            continue
        alternatives = [
            {k: candidate[k] for k in ('file', 'sheet', 'columns', 'additional_months', 'read_call') if k in candidate}
            for candidate in item.get('alternatives', ()) if candidate.get('additional_months')
        ]
        if not alternatives:
            continue
        covered = sorted(item['mapped_months'])
        findings.append({
            'dimension': 'source_period', 'state': 'REVIEW', 'code': 'additional_source_months',
            'detail': f'「{dataset}」请求期间 {item["requested_period"]}，映射表只观察到 '
                      f'{", ".join(covered) or "零个已识别月份"}。候选原表有其他月份的记录。'
                      '请按原文核对对象、金额含义和有效出货月份。',
            'dataset': dataset, 'file': item['file'], 'sheet': item['sheet'],
            'mapped_months': covered, 'alternatives': alternatives,
        })
    return findings


def _probe_inputs(rs, wanted):
    trees = {name: _expression(var.expr) for name, var in rs.variables.items()}
    candidates = {name: list(_aggregates(trees[name])) for name, var in rs.variables.items()
                  if name in wanted and var.scope == 'global'}
    candidates = {name: aggregates for name, aggregates in candidates.items() if aggregates}
    if not candidates:
        return {}, {}

    grouped_uses = {name: [] for name in candidates}
    for name, var in rs.variables.items():
        if var.scope != 'global':
            for dependency in names(trees[name]) & candidates.keys():
                grouped_uses[dependency].append((name, var.scope))
    checked = {name: calls for name, calls in candidates.items()
               if grouped_uses[name] or any(call.fn == 'single_value' for call in calls)}
    return checked, grouped_uses


def global_probe_variables(rs):
    """Determine from formula structure alone whether any variables need mapped inputs."""
    checked, _ = _probe_inputs(rs, rs.variables)
    return set(checked)


def guarded_table_variables(rs):
    guarded = {name for name, table in rs.tables.items() if table.applies_when}
    if not guarded:
        return []
    return [name for name, variable in rs.variables.items()
            if any(isinstance(call, Call) and call.fn in ('tier', 'tier_rate', 'tier_ratio', 'lookup')
                   and call.args and isinstance(call.args[0], Name) and call.args[0].id in guarded
                   for call in walk(_expression(variable.expr)))]


def global_indicator_findings(rs, normalized_data, *, period=None, refs=None, variables=None,
                              scoped=()):
    """Diagnose global aggregate variables together, using normalized inputs before population patches.

    Each selected variable runs against the same population and evaluator as the engine.
    Failure of a sibling does not suppress the other probes. A reached single_value conflict
    or false table guard is attributed to the owning variable. A grouped consumer may
    deliberately use a company total, so that observation requests a source review.
    """
    checked, grouped_uses = _probe_inputs(rs, rs.variables if variables is None else set(variables))
    for name in scoped:
        if name in rs.variables:
            checked.setdefault(name, ())
    if not checked:
        return {}

    # The caller has mapped the same data as the engine. Apply corrections and derive
    # populations exactly once before constructing an isolated evaluation context.
    effective, patch_issues = prepare(rs, normalized_data)
    root, levels, tree_issues = build_tree(rs, effective)
    blocking = [issue for issue in patch_issues + tree_issues if issue.severity == 'blocking']
    if blocking:
        raise EngineError(blocking)
    ev = Evaluator(rs, root, levels, refs, period)
    result = {}
    for name in checked:
        findings = []
        consumers = []
        for consumer, scope in grouped_uses.get(name, ()):
            count = len(levels[scope]) if scope in levels else len(ev.index.get(scope, {}))
            if count > 1:
                consumers.append({'variable': consumer, 'scope': scope, 'objects': count})
        if consumers:
            first = consumers[0]
            findings.append({
                'dimension': 'grain', 'state': 'REVIEW', 'code': 'global_grouped_consumer',
                'detail': f'{first["scope"]} 有 {first["objects"]} 个对象，{first["variable"]} 的公式引用全局聚合 {name}。'
                          f'核实 {first["variable"]} 按规则需要全批值还是每个对象自己的值。',
                'consumers': consumers[:4], 'consumer_count': len(consumers),
            })
        scope = rs.variables[name].scope
        nodes = [root] if scope == 'global' else (levels.get(scope) or list(ev.index.get(scope, {}).values()))
        findings.extend(_evaluate_indicator(ev, name, scope, nodes))
        if findings:
            result[name] = findings
    return result


def _evaluate_indicator(ev, name, scope, nodes):
    if not nodes:
        return [{'dimension': 'execution', 'state': 'REVIEW', 'code': 'scope_probe_empty',
                 'detail': f'当前数据没有可执行 {name} 的 {scope} 对象。'}]
    samples = []
    for node in nodes:
        try:
            value = ev.variable(name, node).v
        except EvalError as exc:
            # Keep a helper's error attributed to the helper. Consumers may
            # legitimately guard the reference or select a narrower population.
            direct = not exc.frames or exc.frames[0]['rule_item'] == name
            if not direct:
                return []
            if guard := exc.table_applicability:
                return [{
                    'dimension': 'execution', 'state': 'CONFLICT', 'code': 'parameter.applicability',
                    'detail': f'对象「{node.id}」套用参数表「{guard["table"]}」时适用条件不成立。'
                              f'实际比较 {guard["evaluated"]}，原始行 {guard["source"]}。'
                              '核对规则中的表适用范围和公式选表条件。',
                    'node': node.id, 'table_applicability': guard,
                }]
            conflict = next((item for item in exc.aggregates
                             if item['operation'] == 'single_value' and item.get('competing_values')), None)
            if conflict:
                return [{
                    'dimension': 'grain', 'state': 'CONFLICT', 'code': 'single_value_multiple',
                    'detail': str(exc), 'dataset': conflict['dataset'], 'node': conflict['node_id'],
                    'selected_rows': conflict['selected_rows'], 'competing_values': conflict['competing_values'],
                }]
            return [{'dimension': 'execution', 'state': 'REVIEW', 'code': 'scope_probe_incomplete',
                     'detail': f'{name} 在 {node.id} 求值待查。{exc}', 'node': node.id}]
        except (IndexError, KeyError, ArithmeticError, ValueError) as exc:
            return [{'dimension': 'execution', 'state': 'REVIEW', 'code': 'scope_probe_incomplete',
                     'detail': f'当前草稿无法执行 {name}（{type(exc).__name__}）。', 'node': node.id}]
        if len(samples) < 3:
            samples.append({'node': node.id, 'value': str(value)[:120], 'value_type': type(value).__name__})
    return [{
        'dimension': 'execution', 'state': 'VERIFIED',
        'code': 'global_execution' if scope == 'global' else 'group_execution',
        'detail': f'当前版本的正式求值器已在 {scope} 的 {len(nodes)} 个实际对象执行 {name}。',
        'scope': scope, 'objects': len(nodes), 'samples': samples, **(samples[0] if scope == 'global' else {}),
    }]


def repair_context(board, active):
    """Show actual observed scope failures and their relevant material in the task card."""
    from copy import deepcopy
    from pathlib import Path
    from bonus_agent.facts import target_parts

    scheme = active.get('scheme')
    anchor = active.get('task', active.get('id'))
    if anchor not in board.state['tasks'] or not board.state.get('indicator_probes', {}).get(scheme):
        return None
    ws = board.ws
    statuses = board.statuses()
    relevant = {}
    for ident, status in statuses.items():
        spec = board.state['tasks'][ident]
        if spec['scheme'] != scheme:
            continue
        findings = [deepcopy(row) for row in status.get('indicator_findings', ())
                    if row['code'] in ('single_value_multiple', 'global_grouped_consumer', 'additional_source_months')]
        if findings:
            relevant[ident] = findings
    if anchor not in relevant or not any(f['state'] == 'CONFLICT' for f in relevant[anchor]):
        return None
    rules = ws._ruleset(scheme)
    variables = rules.variables
    ids = {target_parts(t['target'])[1]: ident for ident, t in board.state['tasks'].items()
           if t['scheme'] == scheme and t['target'].startswith('/variables/')}
    selected = [anchor, *(n for n in relevant if n != anchor)][:6]
    for ident in selected.copy():
        for finding in relevant[ident]:
            for consumer in finding.get('consumers', ()):
                child = ids.get(consumer['variable'])
                if child and child not in selected and len(selected) < 10:
                    selected.append(child)
    clauses = set()
    datasets = set()
    rows = []
    for ident in selected:
        task = board.state['tasks'][ident]
        name = target_parts(task['target'])[1]
        var = variables[name]
        clauses.update(task['clauses'])
        if var.scope in ws.datasets:
            datasets.add(var.scope)
        parsed = _expression(var.expr)
        datasets.update(field.value for call in _aggregates(parsed)
                        if isinstance(field := call.kw().get('of'), Str) and field.value in ws.datasets)
        rows.append({'task': ident, 'variable': name, 'scope': var.scope, 'expr': var.expr,
                     'clauses': task['clauses'], 'findings': relevant.get(ident, [])})
    # Candidate historical sheets may use a related physical header for the
    # business key while retaining the same date and amount. Show the actual
    # headers so an alternative can be evaluated in the same request.
    for row in rows:
        for finding in row['findings']:
            for option in finding.get('alternatives', ()):
                try:
                    sheet = ws.inv.sheet(f'{option["file"]}/{option["sheet"]}')
                except (KeyError, ValueError):
                    continue
                if sheet is not None:
                    option['available_headers'] = sheet.headers[:36]
    # Include the existing identity anchors. Their keys can be reused for other
    # populations without weakening the project association used by the pool.
    datasets.update(key for key, d in ws.datasets.items() if d.key and key in ws.mapping)
    maps = {}
    for name, d in ws.datasets.items():
        if name not in datasets or name not in ws.mapping:
            continue
        m = ws.mapping[name]
        maps[name] = {'file': Path(m.file).name, 'sheet': m.sheet, 'key': d.key, 'link': d.link,
                      'period': d.period, 'window': d.window, 'filter': m.filter,
                      'columns': {n: {'column': c, 'type': d.fields[n].type}
                                  for n, c in m.columns.items() if n in d.fields}}
    excerpts = [{'id': c.id, 'file': c.file, 'quote': c.quote[:500]} for c in ws.clauses if c.id in clauses]
    exclusions = [{'id': c.id, 'file': c.file, 'quote': c.quote[:450]} for c in ws.clauses
                  if c.id not in clauses and '不参与核算' in c.quote][:3]
    group = _group_repair_seed(ws, rules, relevant[anchor], maps, exclusions)
    if group:
        from bonus_agent.data_profile import _source_column_name
        key = group['key']
        normalized = _group_header(_source_column_name(maps[group['value_source']]['columns'][key]['column']))
        for row in rows:
            for finding in row['findings']:
                for option in finding.get('alternatives', ()):
                    options = [h for h in option.get('available_headers', ())
                               if _group_header(_source_column_name(h)) == normalized]
                    if len(options) == 1:
                        option['group_field_candidate'] = {key: options[0]}
    return {'anchor': anchor, 'scheme': scheme, 'indicators': rows, 'datasets': maps,
            'source_clauses': excerpts[:16], 'possible_exclusions': exclusions,
            **({'group_seed': group} if group else {}),
            'grouping': '从本期业务对象或实际销售建立分组实体，保留项目键和分配层级。关联表缺行有合法默认时，'
                        '分组仍须覆盖实际对象。唯一值来源需改为该实体的明细时，先清原 key，再建实体，再改 link。'
                        '宽表月份值从该实体的匹配明细行求 single_value(period_value(...), of: "预算明细")。'
                        '按原文区分缺行和显式零值。',
            'next': '核对直接反例的对象归属。相关公式和映射需联动时一次提交 submit_construction_repair；'
                    '按原文填写 expected_scope。交付相关但独立的 REVIEW 项依据充分时可一并修复，保持其他分支。'}


def _group_repair_seed(ws, rules, findings, maps, exclusions):
    """Provide editable mapping steps when the real conflicting owner has a mapped business key."""
    source = next((item.get('dataset') for item in findings if item['code'] == 'single_value_multiple'), None)
    dimension = maps.get(source, {}).get('key')
    pool_level = next((level for level in rules.allocation if level.level == rules.pool.scope), None)
    pool_key = pool_level.key if pool_level else maps.get(rules.pool.scope, {}).get('key')
    if not dimension or not pool_key:
        return None
    owners = [(n, m) for n, m in maps.items() if m['key'] == pool_key and dimension in m['columns']]
    if len(owners) != 1:
        return None
    owner, mapping = owners[0]
    period = mapping['period']
    fields = {dimension: mapping['columns'][dimension]}
    if period:
        fields[period] = mapping['columns'][period]
    scope = 'group_' + dimension
    if scope in maps or scope in ws.datasets:
        return None
    # Reuse an exact general exclusion already registered for reference rows
    # outside the active business population. The normal mapping reader still
    # validates the quote, the new key and actual orphan behavior.
    import re
    matches = [row for row in exclusions if re.search(
        r'参考(?:数据|资料|表)里有[、，, ]*但[^。；;]{1,28}里没有[^。；;]{0,36}不参与核算',
        row['quote'])]
    qualifier = {'unmatched': 'ignore', 'unmatched_basis': matches[0]['id']} if len(matches) == 1 else {}
    parent = {'name': scope, 'file': mapping['file'], 'sheet': mapping['sheet'], 'fields': fields,
              'key': dimension, 'distinct': True, 'period': period, 'window': mapping['window'],
              'filter': mapping['filter']}
    if (header := ws.mapping[owner].header_rows):
        parent['header_rows'] = header
    return {
        'key': dimension, 'scope': scope, 'business_rows': owner, 'value_source': source,
        'advice': f'唯一值来源 {source} 实际按 {dimension} 区分。修复时保持唯一值约束，'
                  f'先从本期 {owner} 建同键分组 {scope}，使用该组的历史、库存或应收关联。'
                  '同名字样的现有实体可能使用另一主键，不能只按名字切换 scope。'
                  '本组关联的历史源如需替换，请用 define 及完整 fields。',
        'datasets': [
            {'operation': 'update', 'args': {'name': source, 'changes': {'key': None}}},
            {'operation': 'define', 'args': parent},
            {'operation': 'update', 'args': {'name': source, 'changes': {'link': dimension, **qualifier}}},
        ],
    }


def _group_header(text):
    import re

    return re.sub(r'(?:\([a-z]+列\)|[#_]?\d+)$', '', text).strip()
