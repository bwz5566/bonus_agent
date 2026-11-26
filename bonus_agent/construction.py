"""Persistent construction tasks: model proposes dependencies, program validates every completion."""
import copy
import json
import re
from collections import deque
from decimal import Decimal
from graphlib import TopologicalSorter, CycleError

from bonus_agent import facts as fx, clauses as cl, lint
from bonus_agent.formula_inputs import input_failures, input_message, input_preflight_version, normalize_requirements
from bonus_agent.indicator_confidence import assess_indicator, repair_priority_key
from bonus_agent.recovery import fingerprint
from bonus_core.dsl.parser import parse, walk, BinOp, Call, Name, ExprError, Neg, Num

ROOTS = {'tables', 'variables', 'pool', 'allocation', 'report', 'payout', 'offsets', 'checks'}
REPORT_ROWS = {'totals', 'details', 'pending', 'balances', 'distributions'}
DISTRIBUTION_ROOTS = {'/allocation', '/payout', '/offsets'}
DISTRIBUTION_REPORT_FIELDS = {'people', 'pending', 'distributions'}


def _pool_tasks(tasks):
    return {s['scheme']: n for n, s in tasks.items() if s['target'] == '/pool'}


def _planned_dependencies(spec, tasks, pools, phase):
    deps = set(spec['depends_on'])
    if phase == 'basis' and spec['target'] == '/report':
        deps -= {n for n in deps if tasks[n]['target'] in DISTRIBUTION_ROOTS}
        if spec['scheme'] in pools:
            deps.add(pools[spec['scheme']])
    return deps


def validate_output_targets(outputs, schemes, targets):
    """Optional promises of report fields, separate from calculation task declarations."""
    from bonus_agent.agent import ToolError
    if not isinstance(targets, dict) or set(targets) != set(outputs):
        raise ToolError('plan.output_targets：请为每项输出提供至少一个实际报表位置，键名须与outputs一致')
    for name, pointers in targets.items():
        if not isinstance(pointers, list) or not pointers or not all(isinstance(p, str) for p in pointers):
            raise ToolError(f'plan.output_targets：{name}须提供非空的报表位置列表')
        for pointer in pointers:
            scheme, path = fx.split_target(pointer)
            parts = fx.target_parts(path)
            if (scheme not in schemes or not path.startswith('/report/') or len(parts) not in (2, 3)
                    or not all(p.strip() for p in parts)
                    or not (parts[1] in REPORT_ROWS or parts[1:] == ['people'])):
                raise ToolError(f'plan.output_targets：无法识别{pointer}；使用计划中的方案::/report/people、'
                                '方案::/report/totals 或 方案::/report/details/明细名称 等报表位置')
    return {k: list(dict.fromkeys(v)) for k, v in targets.items()}


def _has_output(report, target, phase):
    _, field, *named = fx.target_parts(target)
    if not isinstance(report, dict):
        return False
    value = report.get(field)
    if field == 'people':
        return isinstance(value, str) and bool(value.strip())
    if not isinstance(value, list) or not value:
        return False
    if any(not isinstance(row, dict) for row in value):
        return False
    rows = [row for row in value if (not named or row.get('name') == named[0])
            and (phase != 'basis' or row.get('stage', 'basis') == 'basis')]
    if not rows or (named and len(rows) != 1):
        return False
    fields = ('name', 'level', 'total' if field in ('balances', 'distributions') else 'expr')
    for row in rows:
        if not all(isinstance(row.get(k), str) and row[k].strip() for k in fields):
            return False
        if field == 'balances':
            parts = row.get('parts')
            if not isinstance(parts, list) or not parts or not all(isinstance(p, str) and p.strip() for p in parts):
                return False
    return True


def _semantic(value):
    """Descriptions are not executable inputs; financial fields and policies are."""
    if isinstance(value, dict):
        return {k: _semantic(v) for k, v in value.items() if k != 'desc'}
    if isinstance(value, list):
        return [_semantic(v) for v in value]
    return value


def _strings(value):
    if isinstance(value, dict):
        for key, part in value.items():
            if key in ('expr', 'applies_when', 'weight', 'pay_ratio', 'hold_ratio', 'people', 'total', 'allocated') and isinstance(part, str) and part:
                yield part
            elif key == 'parts' and isinstance(part, list):
                yield from (expr for expr in part if isinstance(expr, str) and expr)
            elif key not in ('sources', 'desc'):
                yield from _strings(part)
    elif isinstance(value, list):
        for part in value:
            yield from _strings(part)


def guard_table_target(ws, scheme, name, value_names):
    """Catch an off-plan write using a value assigned to another table in this scheme."""
    from bonus_agent.agent import ToolError
    if not ws.staged:
        return
    planned = [s for s in ws.workflow.get('construction', {}).get('tasks', {}).values()
               if s['scheme'] == scheme and s['target'].startswith('/tables/')
               and (not s['id'].startswith('auto:') or s.get('facts'))]
    if not planned or any(fx.target_parts(s['target']) == ['tables', name] for s in planned):
        return
    values = set(value_names)
    bound = set()
    for fact in ws.facts:
        if fact.name in values:
            for target in fact.targets:
                qualifier, path = fx.split_target(target)
                if qualifier in (None, scheme, '共用'):
                    bound.add((fact.name, tuple(fx.target_parts(path)[:2])))
    owners = []
    for spec in planned:
        parts = tuple(fx.target_parts(spec['target']))
        missing = next((n for n in spec['facts'] if n in values and (n, parts) not in bound), None)
        if missing:
            owners.append((spec, missing))
    if not owners:
        return
    targets = '、'.join(f'{s["id"]} → {s["target"]}' for s, _ in owners[:5])
    first, sample = owners[0]
    read = {'tool': 'read_workspace', 'args': {'section': 'construction', 'name': first['id']}}
    raise ToolError(f'construction.table_target：方案「{scheme}」待写表「{name}」未在计划中声明，'
                    f'本次取值参数「{sample}」等已分配给 {targets}。本次未写入。'
                    '核对原文列和适用范围。属于原任务时用其目标表名编译；如需拆表或共用取值，'
                    '先用 plan_formula_tasks 分别声明各表的目标、来源和下游依赖，再编译。\nnext_call='
                    + json.dumps(read, ensure_ascii=False))


class TaskBoard:
    def __init__(self, ws):
        self.ws = ws
        self.state = ws.workflow.setdefault('construction', {'tasks': {}, 'receipts': {}, 'reports': {}})

    def phase(self):
        """Allocation formulas are validated after the current basis is approved."""
        flow = self.ws._flow()
        return 'all' if any(flow.current('approved:' + s) for s in self.ws.schemes) else 'basis'

    def funding_scheme(self, scheme):
        from bonus_core.dsl.phases import funding_only

        if scheme not in self.ws.schemes or not funding_only(self.ws._ruleset(scheme)):
            return False
        targets = self.ws.workflow.get('plan', {}).get('output_targets', {})
        fields = {fx.target_parts(path)[1] for pointers in targets.values() for pointer in pointers
                  for owner, path in [fx.split_target(pointer)] if owner == scheme and len(fx.target_parts(path)) > 1}
        return bool(fields and not fields.intersection(DISTRIBUTION_REPORT_FIELDS))

    def task_spec(self, task):
        """Reject a component name used as a task ID with exact existing-task hints."""
        from bonus_agent.agent import ToolError
        if task in self.state['tasks']:
            return self.state['tasks'][task]
        scheme, path = fx.split_target(task)
        candidates = []
        for ident, spec in self.state['tasks'].items():
            if scheme is not None and scheme != spec['scheme']:
                continue
            if path == spec['target'] or path == fx.target_parts(spec['target'])[-1]:
                candidates.append({'id': ident, 'scheme': spec['scheme'], 'target': spec['target'],
                                   'next_call': {'tool': 'read_workspace',
                                                 'args': {'section': 'construction', 'name': ident}}})
        if candidates:
            detail = ('已有目标对应任务（不可重新建同目标任务，也未自动选择）：'
                      + json.dumps(candidates[:5], ensure_ascii=False)
                      + (f'；共 {len(candidates)} 个，须按方案核对' if len(candidates) > 1 else ''))
        else:
            detail = ('先read_workspace(section="construction", query=所需公式名)查已有任务；'
                      '确无该目标时才用plan_formula_tasks声明目标、依赖和来源')
        raise ToolError(f'construction.task：任务ID「{task}」不存在；task须用计划中的id，不能用变量名替代。' + detail)

    def required(self, phase=None):
        phase = phase or self.phase()
        if phase == 'all':
            return {n for n, spec in self.state['tasks'].items()
                    if spec['target'] != '/allocation' or not self.funding_scheme(spec['scheme'])}
        from bonus_core.dsl.validator import basis_rules
        projected = {s: basis_rules(self.ws._ruleset(s)) for s in self.ws.schemes}
        required = set()
        tasks = self.state['tasks']
        pools = _pool_tasks(tasks)
        for name, spec in tasks.items():
            parts = fx.target_parts(spec['target'])
            rs = projected[spec['scheme']]
            if (parts[0] in ('pool', 'report') or (parts[0] == 'checks' and rs.checks)
                    or (parts[0] == 'variables' and parts[1] in rs.variables)
                    or (parts[0] == 'tables' and parts[1] in rs.tables)):
                required.add(name)
        # Planned dependencies matter before a pool/report expression has been written.
        pending = list(required)
        while pending:
            name = pending.pop()
            spec = tasks[name]
            deps = _planned_dependencies(spec, tasks, pools, phase)
            if fx.target_parts(spec['target'])[0] in ('tables', 'variables'):
                deps.update(self.expression_dependencies(spec))
            for dependency in deps:
                if dependency not in required:
                    required.add(dependency)
                    pending.append(dependency)
        return required

    def deferred(self, required):
        # Until a scheme has a pool task its funding dependencies are unknown.
        # Only explicit distribution roots already have a proven later phase.
        tasks = self.state['tasks']
        pools = _pool_tasks(tasks)
        return {n for n, s in tasks.items() if n not in required
                and (s['scheme'] in pools or s['target'] in DISTRIBUTION_ROOTS)}

    def standalone_data_inputs(self, scheme, rules):
        """Recheck validated standalone formulas until a consuming phase owns their source fields."""
        tasks = self.state['tasks']
        upstream = {dependency for task in tasks.values() for dependency in task['depends_on']}
        checked = {fx.target_parts(task['target'])[1] for ident, task in tasks.items()
                   if task['scheme'] == scheme and task['target'].startswith('/variables/')
                   and ident in self.state['receipts'] and ident not in upstream}
        return checked.intersection(rules.variables) - lint.reached(rules) if checked else set()

    def pool_plan_gap(self):
        pools = _pool_tasks(self.state['tasks'])
        return next((s for s in self.ws.workflow.get('plan', {}).get('schemes', []) if s not in pools), None)

    def pool_plan_context(self, scheme):
        # Expose the downstream plan's actual references. Some are person
        # weights, so they cannot be adopted as funding rules without evidence.
        tasks = self.state['tasks']
        refs = {}
        for spec in tasks.values():
            if spec['scheme'] != scheme or spec['target'] not in DISTRIBUTION_ROOTS | {'/report'}:
                continue
            for dep in spec['depends_on']:
                item = tasks[dep]
                if item['scheme'] == scheme and item['target'].startswith(('/variables/', '/tables/')):
                    refs[dep] = {'id': dep, 'target': item['target'], 'title': str(item.get('title') or '')[:120],
                                 'clauses': item['clauses'][:4]}
        return {'scheme': scheme, 'target': '/pool', 'downstream_inputs': list(refs.values())[:6],
                'input_count': len(refs), 'read_plan': {'tool': 'read_workspace',
                                                       'args': {'section': 'construction', 'name': scheme, 'limit': 6}}}

    @staticmethod
    def pool_plan_hint(scheme):
        return (f'plan_formula_tasks：补齐 {scheme}::/pool（本方案可分配总额）的任务，'
                '按本方案原文列出计算该金额直接依赖的已有任务 ID（depends_on），保留其他任务（replace=false）。'
                'planning_gap.downstream_inputs列出分配或报表实际引用的构件，按条款区分金额与人员权重。'
                '如直接从原始字段计算，可不填依赖。')

    def structure_issues(self, scheme):
        from bonus_core.dsl.validator import validate_structure
        return [str(i) for i in validate_structure(self.ws._ruleset(scheme)) if i.severity == 'blocking']

    def _checked_rules(self, spec):
        rs = self.ws._ruleset(spec['scheme'])
        if spec['target'] in ('/report', '/checks', '/pool') and self.phase() == 'basis':
            from bonus_core.dsl.validator import basis_rules
            return basis_rules(rs)
        return rs

    def register_required_roots(self):
        """Seed an empty board from the current output contract without accepting a formula."""
        ws = self.ws
        flow = ws._flow()
        if (not ws.staged or ws.done or ws.pending or self.state['tasks'] or not flow.current('data')
                or flow.stage()[0] != 3 or self.phase() != 'basis'):
            return None
        missing = dict.fromkeys((row['scheme'], row['task_target']) for row in self.output_obligations('basis')
                                if row['kind'] == 'missing_task')
        if not missing:
            return None
        tasks = [{'id': f'auto:{scheme}:{target}', 'scheme': scheme, 'target': target}
                 for scheme, target in missing]
        candidate = copy.deepcopy(ws, {id(ws.llm): ws.llm, id(ws.inv): ws.inv})
        report = TaskBoard(candidate).plan(tasks)
        from bonus_agent.agent import ToolError
        if {(r['scheme'], r['target']) for r in report['validated_targets'] if r['unique']} != set(missing):
            raise ToolError('construction.plan：登记回执未覆盖方案要求的目标，保留原计划')
        if not candidate._flow().current('data'):
            check = candidate.prepare_data()
            if check['status'] != 'PASS' or not candidate._flow().current('data'):
                raise ToolError('construction.plan：登记后的数据复验未通过，保留原计划。' + str(check.get('issues', [])[:3]))
            report['data_rechecked'] = True
        ws.__dict__.update(candidate.__dict__)
        return {**report, 'source': '已登记核算方案的交付目标', 'saved': True}

    def plan(self, tasks, replace=False):
        from difflib import get_close_matches

        from bonus_agent.agent import ToolError
        if not isinstance(tasks, list) or not tasks:
            raise ToolError('construction.plan：须提交至少一项任务。查询已有计划请用 read_workspace(section="construction")。')
        before = self.state['tasks']
        proposed = {} if replace else copy.deepcopy(self.state['tasks'])
        schemes = self.ws.workflow.get('plan', {}).get('schemes', self.ws.schemes)
        clause_ids = {clause.id for clause in self.ws.clauses}
        fact_names = {fact.name for fact in self.ws.facts}
        for item in tasks:
            if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k].strip() for k in ('id', 'scheme', 'target')):
                raise ToolError('construction.plan：每项须给非空id、scheme、target')
            allowed = ('id', 'scheme', 'target', 'title', 'depends_on', 'clauses', 'facts', 'expected_scope', 'data_requirements')
            spec = {k: copy.deepcopy(item[k]) for k in allowed if k in item}
            existing = proposed.get(spec['id'])
            if existing and not replace:
                same_target = (existing['scheme'] == spec['scheme']
                               and fx.target_parts(existing['target']) == fx.target_parts(spec['target']))
                if same_target:
                    for key in allowed:
                        if key in existing and key not in spec:
                            spec[key] = copy.deepcopy(existing[key])
                elif 'expected_scope' in existing:
                    raise ToolError(f'construction.scope_contract：{spec["id"]} 已绑定 {existing["scheme"]}::{existing["target"]}，'
                                    '新目标请单独登记任务及其条款')
            for ident, prior in list(proposed.items()):
                if ident.startswith('auto:') and ident != spec['id'] and (prior['scheme'], prior['target']) == (spec['scheme'], spec['target']):
                    proposed.pop(ident)
                    for other in proposed.values():
                        other['depends_on'] = [spec['id'] if d == ident else d for d in other.get('depends_on', [])]
            parts = fx.target_parts(spec['target'])
            if (spec['scheme'] not in schemes or not spec['target'].startswith('/') or not parts or parts[0] not in ROOTS
                    or len(parts) != (2 if parts[0] in ('tables', 'variables') else 1)):
                raise ToolError('construction.target：scheme须在核算计划内；target如/variables/base、/tables/rate、/pool、/allocation、/report')
            for key in ('depends_on', 'clauses', 'facts'):
                values = spec.setdefault(key, [])
                if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                    raise ToolError(f'construction.plan：{key}须为字符串数组')
                spec[key] = list(dict.fromkeys(values))
            if unknown := sorted(set(spec['clauses']) - clause_ids):
                raise ToolError(f'construction.source：任务 {spec["id"]} 引用的条款 ID 不存在：{unknown[:6]}。'
                                '用 read_workspace(section="clauses") 核对已登记 ID，按原 ID 重提；本批未保存。')
            if 'data_requirements' in spec:
                try:
                    spec['data_requirements'] = normalize_requirements(spec['data_requirements'])
                except ValueError as exc:
                    raise ToolError(f'construction.data_dependency：{exc}') from exc
            if 'expected_scope' in spec:
                scope = spec['expected_scope']
                if parts[0] != 'variables' or not isinstance(scope, str) or not scope.strip() or not spec['clauses']:
                    raise ToolError('construction.scope_contract：只有变量任务可声明 expected_scope，'
                                    '须填写 global 或具体层级、关联实体名，并在 clauses 绑定已登记的条款 ID')
                spec['expected_scope'] = scope.strip()
            if unknown := sorted(set(spec['facts']) - fact_names):
                similar = {name: get_close_matches(name, sorted(fact_names), n=3, cutoff=0.35) for name in unknown[:6]}
                raise ToolError(f'construction.source：任务 {spec["id"]}（{spec["scheme"]}::{spec["target"]}）'
                                '引用了未登记的参数名。下列键为提交值，数组为可核对的台账原名：'
                                + json.dumps(similar, ensure_ascii=False)
                                + '。用 read_workspace(section="facts",name=台账原名) 核对原文及数值，'
                                '确认后按原名重提；本批未保存。')
            proposed[spec['id']] = spec
        targets = [(s['scheme'], tuple(fx.target_parts(s['target']))) for s in proposed.values()]
        if len(set(targets)) != len(targets):
            conflicts = []
            for scheme, parts in dict.fromkeys(targets):
                ids = [n for n, item in proposed.items() if (item['scheme'], tuple(fx.target_parts(item['target']))) == (scheme, parts)]
                if len(ids) > 1:
                    existing = [n for n in ids if n in self.state['tasks']]
                    target = '/' + '/'.join(p.replace('~', '~0').replace('/', '~1') for p in parts)
                    conflicts.append({'scheme': scheme, 'target': target, 'ids': ids, 'existing_ids': existing})
            raise ToolError('construction.plan：同一目标只允许一个任务，修改时复用id；本批未保存。冲突：'
                            + json.dumps(conflicts, ensure_ascii=False)
                            + '。用existing_ids中的原id修改；需要原任务详情时read_workspace(section="construction",name=原id)，无需遍历全部任务。')
        new_ids = {item['id'] for item in tasks}
        reserved = {(s['scheme'], tuple(fx.target_parts(s['target']))): ident for ident, s in proposed.items()}
        draft_names = {scheme: self.ws._merged(scheme) for scheme in {s for s, _ in targets}}
        fields = {field for dataset in self.ws.datasets.values() for field in dataset.fields}
        for ident in sorted(new_ids):
            spec = proposed[ident]
            parts = fx.target_parts(spec['target'])
            if len(parts) != 2:
                continue
            root, name = parts
            other = 'tables' if root == 'variables' else 'variables'
            other_id = reserved.get((spec['scheme'], (other, name)))
            if other_id or name in draft_names[spec['scheme']].get(other, {}) or name in fields:
                conflict = f'任务 {other_id} 的目标 /{other}/{name}' if other_id else f'已登记字段或构件 {name}'
                raise ToolError(f'construction.name_clash：任务 {ident} 的 {spec["target"]} 与{conflict}重名，'
                                '会导致 DSL 解析冲突。用同一个任务 ID 重新登记不同的目标名；本批未保存。')
        if any(set(s['depends_on']) - set(proposed) for s in proposed.values()):
            raise ToolError('construction.dependency：依赖任务不存在；按基础到上层分批登记，或在同批补齐')
        try:
            list(TopologicalSorter({n: s['depends_on'] for n, s in proposed.items()}).static_order())
            pools = _pool_tasks(proposed)
            list(TopologicalSorter({n: _planned_dependencies(s, proposed, pools, 'basis')
                                    for n, s in proposed.items()}).static_order())
        except CycleError as exc:
            raise ToolError('construction.cycle：任务依赖成环，未保存；总额及其上游不能依赖由该总额生成的报表。'
                            f'请修正依赖链 {exc.args[1]}') from exc
        self.state['tasks'] = proposed
        for scheme in {s['scheme'] for s in proposed.values()}:
            self.ws.drafts.setdefault(scheme, {})
        self.sync_targets()
        summary = self.summary()
        # Receipt of this successful operation, not a claim that any formula or
        # monetary result passed. Recovery can match an earlier duplicate target
        # to the original ID only after the whole plan is unique and saved.
        validated = []
        for ident in dict.fromkeys(item['id'] for item in tasks):
            spec = self.state['tasks'][ident]
            prior = before.get(ident, {})
            target = '/' + '/'.join(p.replace('~', '~0').replace('/', '~1') for p in fx.target_parts(spec['target']))
            validated.append({'id': ident, 'scheme': spec['scheme'], 'target': target, 'unique': True,
                              'reused': prior.get('scheme') == spec['scheme'] and
                              fx.target_parts(prior.get('target', '')) == fx.target_parts(spec['target'])})
        return {'status': 'PLANNED', **summary, 'validated_targets': validated}

    def sync_targets(self):
        """Enroll existing components without inventing formulas or clause ownership."""
        if not self.state['tasks']:
            return
        owned = {(s['scheme'], s['target']) for s in self.state['tasks'].values()}
        schemes = self.ws.workflow.get('plan', {}).get('schemes', self.ws.schemes)
        for scheme in schemes:
            for root, value in self.ws._merged(scheme).items():
                if root not in ROOTS or not value:
                    continue
                for name in (list(value) if root in ('tables', 'variables') else [None]):
                    target = '/' + root + ('/' + name.replace('~', '~0').replace('/', '~1') if name else '')
                    if (scheme, target) in owned:
                        continue
                    ident = f'auto:{scheme}:{target}'
                    ref = root + ('.' + name if name else '')
                    self.state['tasks'][ident] = {'id': ident, 'scheme': scheme, 'target': target,
                        'title': '已有组件的程序检查', 'depends_on': [], 'facts': [],
                        'clauses': [c.id for c in self.ws.clauses if ref in cl.refs(c.where)]}
                    owned.add((scheme, target))

    def data_inputs(self, spec):
        """Conservative dependency closure, including entity and reference relationships."""
        draft = self.ws._merged(spec['scheme'])
        fields, text = set(), list(_strings(self.value(spec)))
        for expression in text:
            try:
                fields |= {n.id for n in walk(parse(expression)) if isinstance(n, Name)}
            except ExprError:
                pass
        fields -= set(draft.get('variables', {})) | set(draft.get('tables', {}))
        data = {n: _semantic(d.model_dump(mode='json')) for n, d in self.ws._scheme_datasets(spec['scheme']).items()}
        selected = {n for n, d in data.items() if fields & set(d.get('fields', {})) or any(n in t for t in text)}
        selected.update(data.keys() & {row['dataset'] for row in spec.get('data_requirements', ())})
        # Roots can change allocation topology; their signatures must cover the entire graph.
        if spec['target'] in ('/allocation', '/report', '/payout', '/offsets', '/checks'):
            selected = set(data)
        changed = True
        while changed:
            changed = False
            keys = {data[n].get('key') for n in selected} - {None}
            links = {data[n].get('link') for n in selected} - {None}
            for n, d in data.items():
                if n not in selected and (d.get('key') in links or d.get('link') in keys or
                                          any(data[s].get('population_from') == n for s in selected)):
                    selected.add(n)
                    changed = True
        return {n: data[n] for n in sorted(selected)}, {
            n: _semantic(self.ws.mapping[n].model_dump(mode='json')) for n in sorted(selected) if n in self.ws.mapping}

    def value(self, spec):
        try:
            value = fx.target_value(self.ws._merged(spec['scheme']), spec['target'])
            if spec['target'] in ('/report', '/checks') and value and self.phase() == 'basis':
                from bonus_core.dsl.validator import basis_rules
                rs = basis_rules(self.ws._ruleset(spec['scheme']))
                return rs.report.model_dump(mode='json') if spec['target'] == '/report' else [c.model_dump(mode='json') for c in rs.checks]
            return value
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    def expression_dependencies(self, spec):
        needed = set()
        refs = set()
        for text in _strings(self.value(spec)):
            try:
                refs |= {n.id for n in walk(parse(text)) if isinstance(n, Name)}
            except ExprError:
                continue
        for name, task in self.state['tasks'].items():
            parts = fx.target_parts(task['target'])
            if name != spec['id'] and task['scheme'] == spec['scheme'] and len(parts) == 2 and parts[1] in refs:
                needed.add(name)
        parts = fx.target_parts(spec['target'])
        if parts[0] in ('variables', 'tables') and parts[1] in refs:
            needed.add(spec['id'])
        return needed

    def dependencies(self, spec, *, phase=None, pools=None):
        tasks = self.state['tasks']
        phase = phase or self.phase()
        if phase == 'all' and spec['target'] == '/report' and self.funding_scheme(spec['scheme']):
            phase = 'basis'
        planned = _planned_dependencies(spec, tasks, _pool_tasks(tasks) if pools is None else pools,
                                        phase)
        return planned | self.expression_dependencies(spec)

    def token(self, spec, dependencies, *, prepared_version=None):
        related = []
        for fact in self.ws.facts:
            if fact.name in spec['facts'] or any(self.owns(spec, t) for t in fact.targets):
                related.append(fact.model_copy(update={'targets':[t for t in fact.targets if self.owns(spec,t)]}).model_dump(mode='json'))
        from bonus_agent.workflow import _EXECUTION_PROFILE
        from bonus_agent.tier_plan import rate_groups
        value = self.value(spec)
        constant_table = spec['target'].startswith('/tables/') and not (value or {}).get('applies_when')
        datasets, mapping = ({}, {}) if constant_table else self.data_inputs(spec)
        phase = self.phase() if spec['target'] in ('/report', '/checks', '/pool') else None
        checks_basis = spec['target'] == '/pool' or (spec['target'] == '/report' and phase == 'basis')
        checks_objects = (spec['target'].startswith('/variables/') and isinstance(value, dict)
                          and value.get('scope', 'global') == 'global' and 'single_value' in value.get('expr', ''))
        return fingerprint({'profile': _EXECUTION_PROFILE, 'files': self.ws._source_version, 'period': str(self.ws.period),
            'spec': spec, 'value': value, 'facts': related,
            'clauses': [(c.id, c.file, c.quote) for c in self.ws.clauses if c.id in spec['clauses']],
            'datasets': datasets, 'mapping': mapping,
            'structure': [{k: lv.get(k) for k in ('level', 'key', 'parent_key')}
                          for lv in self.ws._merged(spec['scheme']).get('allocation', [])]
                         if not spec['target'].startswith('/tables/') else None,
            'validation_phase': phase,
            'basis_data_prepared': bool(self.ws.workflow.get('receipts', {}).get('data')) if checks_basis else None,
            'indicator_data': prepared_version if checks_objects else None,
            'dependencies': dependencies,
            'tier_rate_groups': rate_groups(self.state, spec)})

    def owns(self, spec, target):
        scheme, path = fx.split_target(target)
        parts, root = fx.target_parts(path), fx.target_parts(spec['target'])
        return scheme in (None, spec['scheme'], '共用') and parts[:len(root)] == root

    def statuses(self, *, with_confidence=True):
        from bonus_agent.agent import ToolError
        tasks = self.state['tasks']
        phase = self.phase()
        pools = _pool_tasks(tasks)
        graph = {n: self.dependencies(s, phase=phase, pools=pools) for n, s in tasks.items()}
        try:
            order = list(TopologicalSorter(graph).static_order())
        except CycleError as exc:
            out = {n:{'status':'PENDING','version':fingerprint([s,self.value(s)]),
                       'depends_on':sorted(graph[n]),'cycle':True, 'cycle_path':list(exc.args[1])}
                    for n,s in tasks.items()}
            if with_confidence:
                self.add_confidence(out)
            return out
        out = {}
        data_receipt = self.ws.workflow.get('receipts', {}).get('data', {})
        prepared_version = data_receipt.get('version') if data_receipt and self.ws._flow().current('data') else None
        for name in order:
            spec = tasks[name]
            deps = {n:out[n]['version'] for n in graph[name]}
            version = self.token(spec, deps, prepared_version=prepared_version)
            missing_pool = phase == 'basis' and spec['target'] == '/report' and spec['scheme'] not in pools
            ready = not missing_pool and all(out[n]['status'] == 'PASS' for n in graph[name])
            status = 'PASS' if ready and self.state['receipts'].get(name) == version else ('PENDING' if ready else 'WAITING')
            out[name] = {'status':status, 'version':version, 'depends_on':sorted(graph[name])}
            if missing_pool:
                out[name]['missing_pool'] = True
        measured = self._current_findings(out, prepared_version)
        for name, status in out.items():
            spec = tasks[name]
            if any(out[n]['status'] != 'PASS' for n in status['depends_on']):
                status['status'] = 'WAITING'
            elif status['status'] == 'PASS' and spec['target'].startswith('/variables/'):
                variable = fx.target_parts(spec['target'])[1]
                if any(f['state'] == 'CONFLICT' for f in measured.get(spec['scheme'], {}).get(variable, ())):
                    status['status'] = 'PENDING'
        if with_confidence:
            self.add_confidence(out, findings=measured)
        return out

    def _probe_version(self, scheme, statuses, data_version):
        """A consumer's scope can change the interpretation of an upstream aggregate."""
        dependencies = self.ws._deps(scheme)
        return fingerprint({
            'data': data_version,
            'tasks': {n: statuses[n]['version'] for n, t in self.state['tasks'].items() if t['scheme'] == scheme},
            'external': [(name, revision, self.ws.workflow['receipts'].get('approved:' + name),
                          name in self.ws.untested, revision in self.ws.results.get(name, {}))
                         for name, revision in sorted(dependencies)],
        })

    def inspect_indicators(self, scheme, statuses=None):
        """Check the independent drafted variables together, reusing measurements for this version."""
        from bonus_agent.agent import ToolError
        from bonus_agent.indicator_scope import (
            global_indicator_findings, global_probe_variables, guarded_table_variables, indicator_period_findings,
        )
        from bonus_core.engine.issues import EngineError

        flow = self.ws._flow()
        if statuses is None:
            self.ws._check_source_version()
        if not flow.current('data'):
            return {}
        statuses = statuses if statuses is not None else self.statuses(with_confidence=False)
        receipt = self.ws.workflow['receipts']['data']
        version = self._probe_version(scheme, statuses, receipt['version'])
        probes = self.state.setdefault('indicator_probes', {})
        if probes.get(scheme, {}).get('version') == version:
            return probes[scheme]['findings']
        rs = self.ws._ruleset(scheme)
        history = receipt.get('payload', {}).get('history_sources', [])
        findings = {name: rows for name, var in rs.variables.items()
                    if (rows := indicator_period_findings(var.expr, history))}
        contracted = [fx.target_parts(t['target'])[1] for t in self.state['tasks'].values()
                      if t['scheme'] == scheme and t.get('expected_scope')]
        contracted.extend(guarded_table_variables(rs))
        if contracted or global_probe_variables(rs):
            refs, missing = flow.preflight_references(scheme)
            if not missing:
                try:
                    data, _ = self.ws._data(rs)
                    observed = global_indicator_findings(rs, data, period=self.ws.period, refs=refs, scoped=contracted)
                except (ToolError, EngineError, ValueError):
                    # Data/structure checks own their failures. No observation on these objects.
                    observed = {}
                for name, rows in observed.items():
                    for row in rows:
                        if guard := row.get('table_applicability'):
                            row['table_applicability'] = self._table_applicability(rs, data, name, guard)
                    findings.setdefault(name, []).extend(rows)
        probes[scheme] = {'version': version, 'findings': findings}
        return findings

    def _current_findings(self, statuses, data_version):
        if not data_version:
            return {}
        return {scheme: batch['findings'] for scheme, batch in self.state.get('indicator_probes', {}).items()
                if scheme in self.ws.schemes and batch['version'] == self._probe_version(scheme, statuses, data_version)}

    def add_confidence(self, statuses, *, findings=None):
        """Project evidence only from reports matching this calculation version."""
        tasks = self.state['tasks']
        flow = self.ws._flow()
        if findings is None:
            version = self.ws.workflow['receipts']['data']['version'] if flow.current('data') else None
            findings = self._current_findings(statuses, version)
        current = {n: r for n, r in self.state['reports'].items()
                   if n in statuses and r.get('version') == statuses[n]['version']}
        counterexamples = {}
        for item in current.values():
            repair = item.get('execution', {}).get('repair')
            if repair and item.get('issues'):
                counterexamples.setdefault(repair['task'], []).append(item['issues'][0])
        reviewed = {}
        transcript_version = fingerprint((self.ws.transcripts, self.ws._chat_text()))
        for name, status in statuses.items():
            spec = tasks[name]
            scheme, target = spec['scheme'], spec['target']
            parts = fx.target_parts(target)
            record = current.get(name, {})
            evidence = {}
            if spec['clauses'] or spec['facts']:
                bound = '、'.join([*spec['clauses'], *spec['facts']][:4])
                evidence['rule_source'] = {'state': 'PARTIAL', 'reason': f'任务已登记来源 {bound}，仍需核对适用性。'}
            if record.get('source_text_version') == transcript_version and record.get('checked_rule_source'):
                evidence['rule_source'] = record['checked_rule_source']
            issues = record.get('issues', [])
            examples = list(counterexamples.get(name, []))
            if violation := record.get('definition_counterexample'):
                examples.append(violation)
            if parts[0] == 'variables':
                variable = self.ws._merged(scheme).get('variables', {}).get(parts[1], {})
                if variable:
                    scope = variable.get('scope', 'global')
                    expected = spec.get('expected_scope')
                    reason = f'公式作用域 {scope}。'
                    if expected:
                        reason += f'计划条款 {", ".join(spec["clauses"])} 指定 {expected}。'
                    evidence['grain'] = {'state': 'PARTIAL', 'reason': reason + '须核对实际对象及其关联键。'}
                for issue in issues:
                    if issue.startswith('indicator.scope_mismatch'):
                        evidence['grain'] = {'state': 'CONFLICT', 'reason': issue}
                        examples.append(issue)
                observations = findings.get(scheme, {}).get(parts[1], [])
                if observations:
                    status['indicator_findings'] = observations
                for finding in observations:
                    dimension = finding['dimension']
                    conflict = finding['state'] == 'CONFLICT'
                    if conflict:
                        examples.append(finding['detail'])
                        if finding['code'] == 'single_value_multiple':
                            evidence['execution'] = {'state': 'CONFLICT', 'reason': finding['detail']}
                    if evidence.get(dimension, {}).get('state') != 'CONFLICT':
                        evidence[dimension] = {'state': 'PARTIAL' if finding['state'] == 'REVIEW' else finding['state'],
                                               'reason': finding['detail']}
            execution = record.get('execution', {})
            if counterexamples.get(name):
                evidence['execution'] = {'state': 'CONFLICT', 'reason': counterexamples[name][0]}
            elif execution.get('status') == 'PASS':
                evidence['execution'] = {'state': 'VERIFIED', 'reason': f'{name} 本版基础预检已在实际对象上求值。'}
            if target == '/pool' and 'review:' + scheme in self.ws.workflow['receipts']:
                if scheme not in reviewed:
                    review = self.ws.workflow['receipts']['review:' + scheme].get('payload', {})
                    reviewed[scheme] = review if (flow.current('review:' + scheme) and review.get('status') == 'PASS'
                        and review.get('basis_digest') == fingerprint(self.ws.workflow.get('basis', {}).get(scheme))) else {}
                if review := reviewed[scheme]:
                    evidence['independent'] = {'state': 'VERIFIED', 'reason': f'本版奖金池按对象完成独立复核，回执 {review["basis_digest"]}。'}
            assessment = assess_indicator(f'{scheme}::{target}', evidence, counterexamples=examples)
            status['confidence'] = assessment
            status['confidence_level'] = assessment['level']

    def repair_order(self, owner, statuses):
        """Keep the failing formula first, then inspect only its reachable inputs."""
        pending, distances = deque([owner]), {owner: 0}
        while pending:
            task = pending.popleft()
            for dependency in statuses[task]['depends_on']:
                if dependency not in distances:
                    distances[dependency] = distances[task] + 1
                    pending.append(dependency)
        def priority(name):
            status = statuses[name]
            assessment = status['confidence']
            observed_risk = any(row['state'] == 'REVIEW' for row in status.get('indicator_findings', ()))
            return (name != owner, assessment['level'] != 'CONFLICT', not observed_risk,
                    repair_priority_key(assessment), distances[name], name)

        ordered = sorted(distances, key=priority)
        items = []
        for name in ordered[:6]:
            status = statuses[name]
            confidence = status['confidence']
            findings = [row for row in status.get('indicator_findings', []) if row['state'] != 'VERIFIED']
            reason = (confidence['counterexamples'] or [x['detail'] for x in findings] or
                      [confidence['dimensions'][d]['reason'] for d in confidence['gaps']])
            items.append({'task': name, 'target': self.state['tasks'][name]['target'], 'level': confidence['level'],
                          'reason': reason[0][:240] if reason else '', 'distance': distances[name]})
        return {'items': items, 'total': len(ordered)}

    def checked_rule_source(self, spec):
        """Verify explicit formula citations in the same source corpus used for binding."""
        from bonus_agent.source_diagnostics import explicit_sources, source_reference_error

        sources = list(explicit_sources(self.value(spec)))
        if not sources:
            return None
        corpus = self.ws._corpus()
        docs, errors = [], []
        for path, source in sources:
            doc = source.get('doc', '')
            if gap := source_reference_error(self.ws, source, corpus):
                # A projected report index is not an editable full-rule pointer.
                # Point repairs at the actual task and give its offending quote.
                section = ('（当前阶段 ' + str(path[0]) + '）') if len(path) > 2 else ''
                errors.append(f'{spec["scheme"]}::{spec["target"]}{section}「{doc}」'
                              f'引文 {source.get("quote", "")[:140]!r}：{gap}')
            if doc not in docs:
                docs.append(doc)
        if errors:
            return {'state': 'PARTIAL', 'reason': errors[0], 'errors': errors}
        return {'state': 'VERIFIED', 'reason': f'当前公式的 {len(sources)} 条引文已逐字核对。文件 ' + '、'.join(docs)}

    def basis_execution(self, spec, rs):
        """Execute a basis component and its dependencies on their actual objects before saving."""
        from bonus_agent.agent import ToolError
        from bonus_core.dsl.phases import basis_rules
        from bonus_core.engine.preflight import preflight_basis

        refs, missing = self.ws._flow().preflight_references(spec['scheme'])
        if missing:
            return {'status': 'DEFERRED', 'dependencies': missing}, []
        is_pool = spec['target'] == '/pool'
        subject = '奖金池' if is_pool else '基础报表'
        scoped = basis_rules(rs.model_copy(update={'report': None if is_pool else rs.report, 'checks': []}))
        try:
            data, _ = self.ws._data(scoped)
        except ToolError as exc:
            return {'status': 'FAIL'}, [str(exc)]
        result = preflight_basis(scoped, data, refs, self.ws.period)
        summary = {'status': result['status'], **result.get('summary', {})}
        issues = []
        for failure in result['issues']:
            target = failure.get('target') or spec['target']
            owner = next((t['id'] for t in self.state['tasks'].values()
                          if t['scheme'] == spec['scheme'] and t['target'] == target), spec['id'])
            source = failure.get('formula') or failure.get('where') or ''
            issues.append(f'construction.runtime：{subject}在实际对象求值失败，先修复任务 {owner} '
                          f'（{target}）。{failure["message"]} 当前算式或位置：{source}。'
                          '核对该对象的数据范围、关联键、原始取值与适用条款后重提。')
            if 'repair' not in summary:
                task = self.state['tasks'][owner]
                summary['repair'] = {'task': owner, 'target': task['target'], 'scheme': task['scheme'],
                                     'component': target,
                                     'node': failure.get('node_id') or failure.get('where'),
                                     'formula': source, 'clauses': task['clauses'], 'facts': task['facts'],
                                     'next': f'先按反例修正 {task["scheme"]}::{task["target"]}（任务 {owner}），'
                                             f'再对{subject}任务 {spec["id"]} 执行局部检查。'}
                if target.startswith('/variables/'):
                    summary['repair']['scope_keys'] = self.scope_keys(rs)
                observed = []
                for item in failure.get('coverage', ()):
                    if not item.get('competing_values'):
                        continue
                    sources = [{**source, 'mapping_call': {'tool': 'read_workspace', 'args': {
                        'section': 'mapping', 'name': source['dataset']}}} for source in item['source_scopes']]
                    observed.append({**{k: item[k] for k in ('dataset', 'operation', 'expr', 'selected_rows',
                                                              'competing_values')}, 'source_scopes': sources})
                    if len(observed) == 2:
                        break
                if observed:
                    summary['repair']['aggregate_evidence'] = observed
                if conflict := failure.get('field_conflict'):
                    summary['repair']['field_conflict'] = conflict
                if reference := failure.get('scope_reference'):
                    summary['repair']['scope_reference'] = self._scope_reference_evidence(task, reference, rs)
                if applicability := failure.get('table_applicability'):
                    summary['repair']['table_applicability'] = self._table_applicability(
                        scoped, data, failure.get('rule_item'), applicability)
                if policy := failure.get('policy'):
                    summary['repair']['policy'] = policy
                    summary['repair']['guidance'] = failure.get('repair', [])
                    if policy.get('target') == '/pool/negative':
                        sources = self._negative_value_sources(spec['scheme'], scoped)
                        summary['repair']['policy_sources'] = sources
                        if sources and task['target'] == '/pool' and rs.pool.negative != 'allow':
                            citations = [s.model_dump(exclude_none=True, mode='json') for s in rs.pool.sources]
                            for source in sources:
                                citation = {'doc': source['file'], 'quote': source['quote']}
                                if citation not in citations:
                                    citations.append(citation)
                            summary['repair']['policy_candidate'] = {
                                'tool': 'submit_formula_task',
                                'args': {'task': task['id'], 'changes': {'negative': 'allow', 'sources': citations}},
                            }
        return summary, issues

    def _scope_reference_evidence(self, task, reference, rs):
        dependent = next((t for t in self.state['tasks'].values()
                          if t['scheme'] == task['scheme']
                          and fx.target_parts(t['target']) == ['variables', reference['variable']]), None)
        clauses = set(dependent['clauses']) if dependent else set()
        sources = [c for c in self.ws.clauses if c.id in clauses]
        evidence = {**reference, 'referenced_task': dependent['id'] if dependent else None,
                    'source_clauses': [{'file': c.file, 'quote': c.quote} for c in sources][:4],
                    'guidance': '核对已记录的编号和原文。如果原文明示可缺少匹配项，在引用该变量前用 has_ref("关联实体名") 分支，'
                                '仅在确实无对应记录时用原文规定的值。无关联键须先修映射。'}
        if candidate := self._optional_coefficient_call(task, reference, rs, sources):
            evidence['candidate_call'] = candidate
        return evidence

    def _optional_coefficient_call(self, task, reference, rs, sources):
        """Propose a local guard only for a direct arithmetic use with an explicit missing-case coefficient."""
        parts = fx.target_parts(task['target'])
        if parts[0] != 'variables' or not reference['values'] or not any(
                item['pool_objects_reaching_scope'] for item in reference.get('coverage', ())):
            return None
        inp = rs.variables.get(reference['variable'])
        if inp is None:
            return None
        owner = rs.variables[parts[1]]
        try:
            factor, calculation = parse(inp.expr), parse(owner.expr)
        except ExprError:
            return None
        if not (isinstance(factor, Call) and factor.fn == 'tier_ratio' and len(factor.args) == 4
                and isinstance(factor.args[3], Num) and all(isinstance(n, (BinOp, Name, Neg, Num)) for n in walk(calculation))):
            return None
        pattern = (r'(?:查不到|找不到|未查到|未匹配|无对应记录|不在[^。；;\n]{0,30}内)'
                   r'[^。；;\n]{0,100}?(?:系数|倍率)[”」\s]*(?:固定为|指定为|设为)'
                   r'\s*[“「"]?\s*(\d+(?:\.\d+)?)(?![\d.%％])')
        corpus = self.ws._corpus()
        verified = [(c, re.findall(pattern, c.quote)) for c in sources if not cl.check_quote(c.file, c.quote, corpus)]
        stated = {Decimal(value) for _, values in verified for value in values}
        fallback = factor.args[3].value
        if stated != {fallback}:
            return None
        variable, scope = reference['variable'], json.dumps(reference['scope'], ensure_ascii=False)
        expr = re.sub(rf'(?<!\w){re.escape(variable)}(?!\w)',
                      lambda _: f'if(has_ref({scope}), {variable}, {fallback})', owner.expr)
        citations = [item.model_dump(exclude_none=True) for item in owner.sources]
        seen = {(item.doc, item.quote) for item in owner.sources}
        for clause, values in verified:
            if values and (clause.file, clause.quote) not in seen:
                citations.append({'doc': clause.file, 'quote': clause.quote})
                seen.add((clause.file, clause.quote))
        return {'tool': 'submit_formula_task', 'args': {'task': task['id'], 'changes': {'expr': expr, 'sources': citations}}}

    def _negative_value_sources(self, scheme, rules):
        from bonus_agent.semantics import preserves_negative

        sources = set()
        for task in self.state['tasks'].values():
            if task['scheme'] != scheme:
                continue
            parts = fx.target_parts(task['target'])
            if parts == ['pool'] or (len(parts) == 2 and parts[0] in ('variables', 'tables')
                                     and parts[1] in getattr(rules, parts[0])):
                sources.update(task['clauses'])
        candidates = [clause for clause in self.ws.clauses
                      if clause.id in sources and preserves_negative(clause.quote)]
        if not candidates:
            return []
        corpus = self.ws._corpus()
        return [{'id': clause.id, 'file': clause.file, 'quote': clause.quote} for clause in candidates
                if not cl.check_quote(clause.file, clause.quote, corpus)][:3]

    def _table_applicability(self, rs, data, rule_item, applicability):
        """Attach categories from the actual entity scope of the failing formula."""
        from bonus_agent.data_profile import query_category_fields, query_field_evidence

        variable = rs.variables.get(rule_item)
        scope = variable.scope if variable else rs.pool.scope
        dataset = scope if scope in rs.datasets and rs.datasets[scope].key else None
        if dataset is None:
            key = next((level.key for level in rs.allocation if level.level == scope), None)
            entity = rs.entity_dataset(key) if key else None
            dataset = entity[0] if entity else None
        mapping = self.ws.mapping.get(dataset)
        if not mapping or dataset not in data:
            return applicability
        fields = query_category_fields(applicability['condition'], rs.datasets[dataset], mapping)
        domains = []
        for field in fields[:2]:
            observed = query_field_evidence(self.ws, dataset, field, data[dataset].rows)
            if len(json.dumps(observed, ensure_ascii=False, default=str)) <= 1800:
                domains.append(observed)
        return {**applicability, 'dataset': dataset, 'field_evidence': domains}

    @staticmethod
    def scope_keys(rs):
        from bonus_core.dsl.scope import Layout
        return {**{level.level: level.key for level in rs.allocation},
                **{name: definition.key for name, definition in Layout(rs).aux.items()}}

    def coefficient_input_issues(self, spec, rs, variable):
        """A declared input coefficient table must be used by the formula or its helpers."""
        required = {}
        for ident in spec['depends_on']:
            dependency = self.state['tasks'][ident]
            parts = fx.target_parts(dependency['target'])
            if dependency['scheme'] == spec['scheme'] and parts[0] == 'tables':
                name = parts[1]
                if getattr(rs.tables.get(name), 'purpose', None) == 'coefficient':
                    required[name] = ident
        if not required:
            return []
        pending, visited, used = [variable], set(), set()
        while pending:
            name = pending.pop()
            if name in visited:
                continue
            visited.add(name)
            try:
                expression = parse(rs.variables[name].expr)
            except ExprError:
                continue
            for item in walk(expression):
                if isinstance(item, Name) and item.id in rs.variables:
                    pending.append(item.id)
                if (isinstance(item, Call) and item.fn in ('tier_rate', 'tier_ratio') and item.args
                        and isinstance(item.args[0], Name)):
                    used.add(item.args[0].id)
        return [f'construction.coefficient_input：{spec["target"]}声明依赖系数表 {name}（任务 {required[name]}），'
                f'当前公式 {rs.variables[variable].expr} 及辅助变量未用该表取系数。'
                f'按原文核对比率的分子、分母、对象范围和缺失口径，使用 tier_ratio({name}, …) 或 tier_rate({name}, 比率)。'
                '补取原始字段时保留成品系数公式。'
                for name in sorted(required.keys() - used)]

    def issues(self, spec):
        from bonus_agent.semantics import identity_literal_issues, target_mode_issues
        value = self.value(spec)
        if value is None or value == [] or value == {}:
            return [f'construction.missing：先用set_rules/compile_table构建{spec["scheme"]}::{spec["target"]}']
        rs = self._checked_rules(spec)
        parts = fx.target_parts(spec['target'])
        if parts[0] != 'tables':
            structure = self.structure_issues(spec['scheme'])
            if structure:
                return ['construction.structure：先修复实体主键、明细角色和关联，再检查公式；此检查不要求金额权重。' + issue
                        for issue in structure]
        prefix = '.'.join(parts)
        own = lambda where: where == prefix or where.startswith(prefix + '.')
        from bonus_core.dsl.validator import validate_basis
        validation = validate_basis(rs) if spec['target'] in ('/report', '/checks', '/pool') and self.phase() == 'basis' else self.ws._validate(spec['scheme'], rs)
        issues = [str(i) for i in validation if i.severity == 'blocking' and own(i.where)]
        if parts[0] == 'variables' and any(i.code == 'scope_bad' and own(i.where) for i in validation):
            keys = json.dumps(self.scope_keys(rs), ensure_ascii=False)
            issues.append(f'construction.scope：当前对象作用域及其主键为 {keys}。global 会把全批次合并为一个对象。'
                          '条款按组取数时，选用有相应主键的作用域；如缺少该分组实体，先按材料补充实体和明细关联。')
        if spec['target'] in ('/report', '/checks', '/pool') and self.phase() == 'basis':
            issues += [str(i) for i in self.ws._validate(spec['scheme'], rs)
                       if i.severity == 'blocking' and i.code == 'global_in_level' and own(i.where)]
        issues += [message for _,message in lint.static_gates(rs) if message.startswith(prefix + ' ')]
        from bonus_agent.contracts import ratio_default_gaps
        try:
            issues += [message for where, message in ratio_default_gaps(rs) if own(where)]
        except ExprError:
            # The ordinary syntax check owns malformed, possibly unrelated draft expressions.
            pass
        checking_ws = self.ws._flow().basis_view() if spec['target'] in ('/report', '/checks') and self.phase() == 'basis' else self.ws
        related = [f for f in checking_ws.facts if f.name in spec['facts'] or any(self.owns(spec,t) for t in f.targets)]
        supporting = set()
        if parts[0] == 'tables' and any(not f.targets and f.source_cell for f in related):
            from bonus_agent.parameter_repair import covered_range_labels
            supporting = covered_range_labels(self.ws, related, {spec['scheme']: checking_ws._merged(spec['scheme'])})
        table_hint = ('；先核对该参数与表的适用范围。原表不同费率列应分别建表并登记任务，'
                      '用 plan_formula_tasks 将参数放到各自任务，之后按原文绑定' if parts[0] == 'tables' else '，使用bind_facts')
        for fact in related:
            bindings = [t for t in fact.targets if self.owns(spec,t)]
            if not bindings:
                if parts[0] == 'tables' and (fact.name, fact.file, fact.quote, spec['scheme'], parts[1]) in supporting:
                    if error := fx.check_fact(fact, self.ws._corpus()):
                        issues.append(f'{fact.name}：{error}')
                    continue
                issues.append(f'construction.binding：{fact.name}尚未绑定到{spec["target"]}' + table_hint)
                continue
            candidate = fact.model_copy(update={'targets':bindings})
            error = fx.check_fact(candidate, self.ws._corpus())
            if error:
                issues.append(f'{fact.name}：{error}')
            local = candidate.model_copy(update={'targets':[spec['scheme'] + '::' + fx.split_target(t)[1]
                                                          if fx.split_target(t)[0] == '共用' else t for t in bindings]})
            issues += fx.binding_errors([local], {spec['scheme']:checking_ws._merged(spec['scheme'])})
        # Candidate locations in a global error are suggestions, never ownership evidence.
        # Global unbound facts remain blocking at compile; local tasks inspect only assigned/bound facts.
        import hashlib
        from bonus_agent.agent import CHAT_SOURCE
        for fact in related:
            source = self.ws._chat_text().encode() if fact.file == CHAT_SOURCE else self.ws._file(fact.file).read_bytes()
            if not fact.source_hash or fact.source_hash != hashlib.sha256(source).hexdigest():
                issues.append(f'{fact.name}：原文版本变化或缺少指纹，需要重新登记')
        sourced = {fx.numeric(f) for f in related if any(self.owns(spec,t) for t in f.targets)}
        constants = {number for number,where in lint.constants(rs) if own(where)} - lint.FREE
        for number in constants - sourced:
            issues.append(f'construction.constant：{spec["target"]}中的{number}缺少该位置的参数绑定，不能用别处同值充当依据')
        if parts[0] == 'tables':
            issues += [f'construction.binding：{path}={amount} 缺少精确参数绑定'
                       for path, amount in fx.unbound_table_cells({parts[1]: value}, related, spec['scheme'])]
        issues += target_mode_issues(checking_ws, spec['scheme'], spec['target'], spec['clauses'])
        if spec['target'] == '/report':
            for gap in self.output_obligations(self.phase()):
                if gap['scheme'] == spec['scheme'] and gap['output']:
                    issues.append(f'construction.report_output：本阶段交付项「{gap["output"]}」缺少 {gap["target"]}')
        if parts[0] == 'variables':
            issues += self.coefficient_input_issues(spec, rs, parts[1])
            if 'expected_scope' in spec and rs.variables[parts[1]].scope != spec['expected_scope']:
                issues.append(f'indicator.scope_mismatch：{spec["scheme"]}::{spec["target"]} 按计划条款 {", ".join(spec["clauses"])} '
                              f'声明按 {spec["expected_scope"]} 计算，实际公式作用域为 {rs.variables[parts[1]].scope}。'
                              '请核对条款和实际分组键，再修复公式或有依据地更新计划。')
            issues += identity_literal_issues(checking_ws, rs, parts[1], spec['clauses'])
            from bonus_agent.semantics import tier_dispatch_issues
            from bonus_agent.tier_plan import dispatch_issues
            declared = [self.state['tasks'][dep]['target'] for dep in spec['depends_on']]
            issues += tier_dispatch_issues(checking_ws, rs, parts[1], declared)
            issues += dispatch_issues(self, spec, rs, rs.variables[parts[1]].expr)
        return list(dict.fromkeys(issues))

    def check(self, task, *, include_next=True, link_clauses=True):
        from bonus_agent.agent import ToolError
        spec = self.task_spec(task)
        self.ws._check_source_version()
        statuses = self.statuses(with_confidence=False)
        definition_counterexample = None
        execution = None
        unmet_inputs = []
        pending = [n for n in statuses[task]['depends_on'] if statuses[n]['status'] != 'PASS']
        if statuses[task].get('missing_pool'):
            issues = [self.pool_plan_hint(spec['scheme'])]
        elif statuses[task].get('cycle'):
            issues = [f'construction.cycle：当前公式依赖成环，先修正{spec["target"]}的任务依赖或表达式']
        elif spec['target'] == '/allocation' and (structural := self.structure_issues(spec['scheme'])):
            issues = structural
        elif pending:
            issues = [f'construction.dependency：先检查依赖任务{pending}']
        elif unmet_inputs := input_failures(self.ws, spec, self.value(spec)):
            issues = [input_message(row) for row in unmet_inputs]
        else:
            issues = self.issues(spec)
            definition_counterexample = next((message for message in issues
                if message.startswith(('[阻断]', 'construction.coefficient_input', 'indicator.scope_mismatch',
                                       'algorithm.tier_dispatch'))
                or 'parameter.binding.path_missing' in message or f'：{spec["scheme"]}::{spec["target"]}/' in message), None)
        if not unmet_inputs and (spec['target'].startswith('/variables/') or spec['target'] in ('/pool', '/report')):
            findings = self.inspect_indicators(spec['scheme'], statuses)
            parts = fx.target_parts(spec['target'])
            if parts[0] == 'variables':
                for row in findings.get(parts[1], []):
                    if row['state'] != 'CONFLICT':
                        continue
                    issues.append(f'{row["code"]}：{row["detail"]}')
                    if guard := row.get('table_applicability'):
                        execution = {'status': 'FAIL', 'repair': {
                            'task': task, 'scheme': spec['scheme'], 'target': spec['target'],
                            'node': row['node'], 'formula': self.value(spec)['expr'],
                            'clauses': spec['clauses'], 'facts': spec['facts'], 'table_applicability': guard,
                            'next': f'按实际对象和原始规则修复任务 {task} 的选表或适用范围，再提交复核。',
                        }}
        checked_source = self.checked_rule_source(spec)
        if checked_source and checked_source['state'] != 'VERIFIED':
            issues += ['source.quote：' + message for message in checked_source['errors']]
        checks_basis = spec['target'] == '/pool' or (spec['target'] == '/report' and self.phase() == 'basis')
        if not issues and checks_basis and self.ws.workflow.get('receipts', {}).get('data'):
            execution, issues = self.basis_execution(spec, self.ws._ruleset(spec['scheme']))
        report = {'status':'FAIL' if issues else 'PASS','task':task,'scheme':spec['scheme'],'target':spec['target'],'issues':issues,
                  'version':statuses[task]['version']}
        if definition_counterexample:
            report['definition_counterexample'] = definition_counterexample
        if unmet_inputs:
            report['input_failures'] = unmet_inputs
        if execution is not None:
            report['execution'] = execution
        if checked_source:
            report['checked_rule_source'] = checked_source
            report['source_text_version'] = fingerprint((self.ws.transcripts, self.ws._chat_text()))
        self.state['reports'][task] = copy.deepcopy(report)
        if not issues:
            # Only a program check can issue this receipt. It never approves a monetary result.
            self.state['receipts'][task] = statuses[task]['version']
        else:
            self.state['receipts'].pop(task, None)
        if not issues and link_clauses:
            known = cl.known_refs(self.ws.drafts, self.ws.datasets)
            ref = '.'.join(fx.target_parts(spec['target']))
            if ref in known:
                clause_ids = set(spec['clauses'])
                if clause_ids and spec['target'].startswith(('/tables/', '/variables/')):
                    basis = self.required('basis')
                    if task not in basis:
                        # A task outside the basis cannot automatically add a
                        # new basis-clause link. Its own source remains in its task.
                        clause_ids.difference_update(cid for n in basis for cid in self.state['tasks'][n]['clauses'])
                updates = {c.id:'、'.join(dict.fromkeys([*cl.refs(c.where), ref])) for c in self.ws.clauses
                           if c.id in clause_ids and not c.where.startswith((cl.BUILTIN, cl.NOT_APPLY, cl.ASKED))}
                if updates:
                    self.ws.cover_clauses(updates)
        self.add_confidence(statuses)
        report['confidence'] = statuses[task]['confidence']
        if issues:
            owner = (execution or {}).get('repair', {}).get('task', task)
            report['repair_order'] = self.repair_order(owner, statuses)
        if include_next:
            report['next'] = self.summary()['next']
        return report

    def _bound_parameter_table(self, spec):
        """An existing table is eligible for a local check once every cell is bound."""
        parts = fx.target_parts(spec['target'])
        if len(parts) != 2 or parts[0] != 'tables':
            return False
        value = self.value(spec)
        if not isinstance(value, dict):
            return False
        table = {parts[1]: value}
        return (next(fx.table_cells(table), None) is not None
                and next(fx.unbound_table_cells(table, self.ws.facts, spec['scheme']), None) is None)

    def refresh(self):
        """Recheck stale receipts in dependency order, without spending a model turn per item."""
        self.sync_targets()
        statuses = self.statuses()
        evidence_changed = False
        for name in list(statuses):
            status = statuses[name]
            previous = self.state['reports'].get(name, {})
            if (status['status'] == 'PENDING' and (name in self.state['receipts'] or previous.get('status') == 'FAIL'
                                                   or self._bound_parameter_table(self.state['tasks'][name]))
                    and not (previous.get('version') == status['version'] and previous.get('status') == 'FAIL')):
                report = self.check(name, include_next=False, link_clauses=False)
                evidence_changed = True
                if report['status'] == 'PASS':
                    statuses = self.statuses()
                    evidence_changed = False
        return self.statuses() if evidence_changed else statuses

    def output_obligations(self, phase='basis'):
        """Missing output definitions needed by the current calculation phase."""
        plan = self.ws.workflow.get('plan', {})
        owned = {(s['scheme'], s['target']) for s in self.state['tasks'].values()}
        obligations = []
        roots = ['/allocation', '/pool'] if phase == 'all' else ['/pool']
        if self.ws.report_required:
            roots.append('/report')
        for scheme in plan.get('schemes', []):
            for target in roots:
                if target == '/allocation' and self.funding_scheme(scheme):
                    continue
                if (scheme, target) not in owned:
                    obligations.append({'output': '', 'scheme': scheme, 'target': target,
                                        'task_target': target, 'kind': 'missing_task'})
        for output, pointers in plan.get('output_targets', {}).items():
            for pointer in pointers:
                scheme, target = fx.split_target(pointer)
                parts = fx.target_parts(target)
                if phase == 'basis' and len(parts) > 1 and parts[1] in DISTRIBUTION_REPORT_FIELDS:
                    continue
                if (scheme, '/report') not in owned:
                    kind = 'missing_task'
                elif not _has_output(self.ws._merged(scheme).get('report'), target, phase):
                    kind = 'missing_component'
                else:
                    continue
                obligations.append({'output': output, 'scheme': scheme, 'target': target,
                                    'task_target': '/report', 'kind': kind})
        return obligations

    def unassigned_clauses(self):
        assigned = {cid for spec in self.state['tasks'].values() for cid in spec['clauses']}
        return [c for c in self.ws.clauses if not c.where and c.id not in assigned]

    def evidence_plan_gap(self, phase):
        clauses = self.unassigned_clauses()
        if clauses:
            items = [{'id': c.id, 'file': c.file, 'quote': c.quote[:650], 'point': c.point,
                      **({'truncated': True} if len(c.quote) > 650 else {})} for c in clauses[:6]]
            gap = {'kind': 'clause_owners', 'target': clauses[0].id, 'clauses': items, 'remaining': len(clauses)}
            field = 'clauses'
        else:
            report = self.ws._flow().compile_feedback(phase) or {}
            if report.get('status') != 'FAIL':
                return None
            owned = {name for t in self.state['tasks'].values() for name in t['facts']}
            missing = {issue.partition('：')[0] for issue in report['issues'] if '：缺少精确规则位置' in issue}
            pending = [f for f in self.ws.facts if f.name in missing - owned and not f.targets]
            if not pending:
                return None
            first = pending[0]
            def source(f):
                return f.file, f.page, (f.source_cell or {}).get('table')
            group = [f for f in pending if source(f) == source(first)][:20]
            items = [{'name': f.name, 'file': f.file, 'page': f.page, 'cell': f.source_cell,
                      'quote': f.quote[:400], 'value': str(f.value), 'unit': f.unit,
                      **({'truncated': True} if len(f.quote) > 400 else {})} for f in group]
            gap = {'kind': 'parameter_owners', 'target': first.name, 'facts': items, 'remaining': len(pending)}
            field = 'facts'
        schemes = self.ws.workflow['plan']['schemes']
        tasks = sorted(self.state['tasks'].values(), key=lambda spec: spec['target'].count('/'))
        return {**gap, 'scheme': schemes[0] if len(schemes) == 1 else '', 'schemes': schemes,
                'tasks': [{k: t[k] for k in ('id', 'scheme', 'target', 'title') if k in t} for t in tasks[:48]],
                'task_count': len(tasks), 'message': f'用 plan_formula_tasks 为 {gap["target"]} 等已登记证据安排负责的任务。'
                f'按原文和任务目标选择，已有任务复用原 ID，仅追加 {field}。人员专用义务可先登记。'
                '确实缺少构件时可新建任务。通过条件是当前缺项实际减少。'}

    def coverage_gaps(self, phase=None):
        tasks = self.state['tasks']
        gaps = []
        for row in self.output_obligations(phase or self.phase()):
            if row['output']:
                action = ('plan_formula_tasks登记/report任务' if row['kind'] == 'missing_task'
                          else '完善/report任务中的实际输出定义')
                gaps.append(f'construction.plan：交付项「{row["output"]}」缺少{row["scheme"]}::{row["target"]}；{action}')
            else:
                gaps.append(f'construction.plan：{row["scheme"]}缺少构建任务{row["target"]}')
        for scheme in self.ws.workflow.get('plan', {}).get('schemes', []):
            roots = {s['target'] for s in tasks.values() if s['scheme'] == scheme}
            for root, value in self.ws._merged(scheme).items():
                if root not in ROOTS or not value:
                    continue
                targets = ['/' + root + '/' + name.replace('~','~0').replace('/','~1') for name in value] if root in ('tables','variables') else ['/' + root]
                gaps += [f'construction.plan：{scheme}的{target}未安排局部检查任务' for target in targets if target not in roots]
        gaps += [f'construction.plan：{c.id}尚未安排到任务；补充任务clauses或按原文明确不适用' for c in self.unassigned_clauses()]
        return gaps

    def adopt(self):
        """Existing complete rules may be revalidated without asking the model to rebuild their plan."""
        if self.state['tasks'] or not self.ws.schemes:
            return
        if any(not self.ws._merged(s).get('pool') or
               (not self.ws._merged(s).get('allocation') and not self.funding_scheme(s)) or
               (self.ws.report_required and not self.ws._merged(s).get('report')) for s in self.ws.schemes):
            return
        tasks = []
        for scheme in self.ws.schemes:
            for root,value in self.ws._merged(scheme).items():
                if root not in ROOTS or not value:
                    continue
                names = list(value) if root in ('variables','tables') else [None]
                for name in names:
                    target = '/' + root + (('/' + name.replace('~','~0').replace('/','~1')) if name else '')
                    ref = root + ('.' + name if name else '')
                    tasks.append({'id':f'auto{len(tasks)+1}','scheme':scheme,'target':target,
                                  'clauses':[c.id for c in self.ws.clauses if ref in cl.refs(c.where)]})
        self.plan(tasks)
        for name in self.statuses():
            self.check(name)

    def checked_inputs(self, task, statuses):
        """Bound the card by complete definitions; evidence and larger inputs retain exact read addresses."""
        tasks = self.state['tasks']
        deps = statuses[task]['depends_on']
        if tasks[task]['target'] == '/report' and self.phase() == 'basis':
            # A report may need already verified amounts inside the pool's expression.
            # Follow the effective basis dependencies, leaving deferred allocation off this card.
            discovered = set(deps)
            pending = deque(deps)
            deps = list(deps)
            while pending:
                for ident in statuses[pending.popleft()]['depends_on']:
                    if ident in discovered or statuses[ident]['status'] != 'PASS':
                        continue
                    discovered.add(ident)
                    pending.append(ident)
                    if tasks[ident]['target'].startswith('/variables/'):
                        deps.append(ident)
        items, remaining = [], 2600
        for ident in deps[:8]:
            if statuses[ident]['status'] != 'PASS':
                continue
            spec = tasks[ident]
            target = spec['target']
            item = {'task': ident, 'scheme': spec['scheme'], 'target': target, 'confidence_level': statuses[ident]['confidence_level'],
                    'read_call': {'tool': 'read_workspace', 'args': {
                        'section': 'rules', 'scheme': spec['scheme'], 'name': target, 'detail': True}}}
            if target == '/pool' or target.startswith(('/variables/', '/tables/')):
                value = self.value(spec)
                if isinstance(value, dict):
                    definition = {k: v for k, v in value.items() if k not in ('sources', 'desc', 'confidence', 'confirmed_by')}
                    size = len(json.dumps(definition, ensure_ascii=False, default=str))
                    if size <= remaining:
                        item['definition'] = definition
                        remaining -= size
            items.append(item)
        return {'items': items, 'total': len(deps)}

    def summary(self, *, statuses=None):
        statuses = self.refresh() if statuses is None else statuses
        tasks = self.state['tasks']
        phase = self.phase()
        required = self.required(phase)
        ready = [n for n, s in statuses.items() if n in required and s['status'] == 'PENDING']
        active = ready[0] if ready else None
        if phase == 'all' and ready:
            # Start with work that leads to personnel outputs. Unrelated draft
            # tasks still go through the validation gate before release.
            roots = [n for n, task in tasks.items() if statuses[n]['status'] != 'PASS'
                     and task['target'] in DISTRIBUTION_ROOTS | {'/report'}]
            delivery = set(roots)
            while roots:
                for dependency in statuses[roots.pop()]['depends_on']:
                    if dependency not in delivery:
                        delivery.add(dependency)
                        roots.append(dependency)
            active = next((n for n in ready if n in delivery), active)
        if not tasks:
            return {'total':0,'passed':0,'active':None,'next':'plan_formula_tasks：按参数表→基础变量→金额变量→pool→分配→报表规划任务、依赖及条款；不要先反复compile_formulas'}
        for scheme in dict.fromkeys(s['scheme'] for s in tasks.values()):
            structural = self.structure_issues(scheme)
            if structural:
                return {'total':len(tasks), 'passed':sum(s['status']=='PASS' for s in statuses.values()),
                        'active':{'id':'structure:' + scheme, 'scheme':scheme, 'target':'/structure',
                                  'status':'PENDING', 'last_issues':structural[:4]},
                        'structure_issues':structural,
                        'next':'update_dataset / set_rules：先修复实体结构：' + '；'.join(structural[:4])
                               + '。主键须唯一，明细与实体角色须一致；这里只核对结构，不要求先写金额或分配权重。'}
        if scheme := self.pool_plan_gap():
            return {'total': len(tasks), 'passed': sum(s['status'] == 'PASS' for s in statuses.values()),
                    'active': None, 'planning_gap': self.pool_plan_context(scheme),
                    'next': self.pool_plan_hint(scheme)}
        from bonus_agent.funding_plan import pool_input_gap
        if gap := pool_input_gap(self):
            return {'total': len(tasks), 'passed': sum(s['status'] == 'PASS' for s in statuses.values()),
                    'active': None, 'planning_gap': gap, 'next': 'plan_formula_tasks：' + gap['message']}
        if active and statuses[active].get('cycle'):
            cycle = statuses[active]['cycle_path']
            return {'total': len(tasks), 'passed': 0, 'active': None,
                    'next': 'construction.cycle：先用plan_formula_tasks修正任务依赖或用set_rules修正表达式。依赖环 ' +
                            ' → '.join(cycle)}
        if active is None:
            gaps = self.coverage_gaps()
            if not self.output_obligations(phase) and (gap := self.evidence_plan_gap(phase)):
                return {'total': len(tasks), 'passed': sum(s['status'] == 'PASS' for s in statuses.values()),
                        'active': None, 'planning_gap': gap, 'next': gap['message']}
            return {'total':len(tasks),'passed':sum(s['status']=='PASS' for s in statuses.values()),'active':None,
                    'next':gaps[0] if gaps else 'compile_formulas'}
        spec = tasks[active]
        last = self.state['reports'].get(active,{})
        current_issues = last.get('issues', []) if last.get('version') == statuses[active]['version'] else []
        node = self.value(spec)
        action = 'check_formula_task' if node is not None else 'submit_formula_task'
        if spec['target'].startswith('/tables/') and (node is None or any(
                issue.startswith(('construction.binding', 'construction.constant')) for issue in current_issues)):
            tool = ('compile_coefficient_table' if (node or {}).get('purpose') == 'coefficient'
                    or (node is None and '系数' in spec.get('title', '')) else 'compile_table')
            table = json.dumps(fx.target_parts(spec['target'])[1], ensure_ascii=False)
            action = f'{tool}(name={table})，按原文坐标编译并绑定；不同适用列须分别规划表及使用它们的依赖'
        elif current_issues:
            action = '先按当前反例修正任务或其依赖，再复核。' + current_issues[0][:300]
        inputs = statuses[active]['depends_on']
        extra = {}
        if spec['target'] == '/report':
            phase = self.phase()
            outputs = [{'output': row['output'], 'target': row['target']}
                       for row in self.output_obligations(phase)
                       if row['scheme'] == spec['scheme'] and row['output']]
            extra['report_task'] = {'phase': phase, 'outputs': outputs,
                                    'deferred_inputs': [n for n in spec['depends_on'] if n not in inputs]}
            if phase == 'basis':
                extra['report_task']['scope_keys'] = self.scope_keys(self._checked_rules(spec))
                action = ('submit_formula_task：按 report_task.outputs 提交本期基础金额报表。'
                          'details、totals 每行写 name、level、expr、sources，stage 为 basis；'
                          '有指定行名时按计划原名填写。已有报表用 changes 替换需要更新的字段，包含该字段仍要保留的行。'
                          '本阶段使用 checked_inputs 中的奖金池和已建变量，人员分配在财务确认总额后构建。')
        gaps = self.coverage_gaps()
        repair = last.get('execution', {}).get('repair') if current_issues else None
        next_step = f'{action}：当前任务{active}，只处理{spec["scheme"]}::{spec["target"]}，依赖已通过；完成后check_formula_task(task="{active}")'
        if repair:
            extra['runtime_repair'] = repair
            next_step = repair['next']
        if current_issues:
            extra['repair_order'] = self.repair_order(repair['task'] if repair else active, statuses)
        if unmet_inputs := input_failures(self.ws, spec, node):
            extra['input_failures'] = unmet_inputs
            next_step = f'任务 {active} 的数据前置条件未通过。' + input_message(unmet_inputs[0])
        prior_input = self.state.get('input_preflight', {}).get(active, {})
        if (prior_input.get('report', {}).get('status') == 'FAIL'
                and prior_input['version'] == input_preflight_version(self.ws, spec)):
            report = prior_input['report']
            extra['input_preflight'] = {k: report[k] for k in ('stage', 'issues', 'input_failures', 'data_input', 'next')
                                        if k in report}
            next_step = report['next']
        return {'total':len(tasks),'passed':sum(s['status']=='PASS' for s in statuses.values()),
                'coverage_gap_count': len(gaps), 'coverage_gaps': gaps[:4],
                'checked_inputs': self.checked_inputs(repair['task'] if repair else active, statuses),
                **extra, 'active':{**spec,'depends_on':inputs,'status':statuses[active]['status'],
                          'confidence_level':statuses[active]['confidence_level'],
                          'last_issues':current_issues[:3]},
                'next':next_step}

    def gate(self, phase=None):
        self.adopt()
        if not self.state['tasks']:
            return ['construction.plan：先plan_formula_tasks，逐个构建并check_formula_task，再整体编译']
        # A changed version of a previously checked component can be rechecked locally without another model round.
        statuses = self.refresh()
        required = self.required(phase)
        # Refresh may revalidate a restored task without updating clause links.
        # Recover only blank links from current, executable, in-phase owners.
        missing = {c.id for c in self.ws.clauses if not c.where}
        if missing:
            refs = {}
            view = self.ws._flow().basis_view() if (phase or self.phase()) == 'basis' else self.ws
            known = cl.known_refs(view.drafts, view.datasets)
            for ident, spec in self.state['tasks'].items():
                if ident not in required or statuses[ident]['status'] != 'PASS':
                    continue
                ref = '.'.join(fx.target_parts(spec['target']))
                if ref in known:
                    for cid in spec['clauses']:
                        if cid in missing:
                            refs.setdefault(cid, []).append(ref)
            if refs:
                self.ws.cover_clauses({cid: '、'.join(dict.fromkeys(paths)) for cid, paths in refs.items()})
        return self.coverage_gaps(phase) + [f'construction.task：{n}未通过局部检查或已失效，调用check_formula_task'
                                        for n,s in statuses.items() if n in required and s['status'] != 'PASS']

    def runtime_blocked_schemes(self, issues, phase):
        """Recognize a current basis execution failure that the full compiler can diagnose."""
        if phase != 'basis' or not issues or not all(i.startswith('construction.task：') for i in issues):
            return set()
        statuses = self.statuses()
        blocked = set()
        for ident, task in self.state['tasks'].items():
            if task['target'] in ('/pool', '/report'):
                record = self.state['reports'].get(ident, {})
                if (record.get('version') == statuses[ident]['version']
                        and record.get('execution', {}).get('status') == 'FAIL'):
                    blocked.add(task['scheme'])
        return blocked
