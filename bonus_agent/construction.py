"""Persistent construction tasks: model proposes dependencies, program validates every completion."""
import copy
from graphlib import TopologicalSorter, CycleError

from bonus_agent import facts as fx, clauses as cl, lint
from bonus_agent.recovery import fingerprint
from bonus_core.dsl.parser import parse, walk, Name, ExprError, Num

ROOTS = {'tables', 'variables', 'pool', 'allocation', 'report', 'payout', 'offsets', 'checks'}


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
            if key in ('expr', 'weight', 'pay_ratio', 'hold_ratio', 'people', 'total') and isinstance(part, str) and part:
                yield part
            elif key not in ('sources', 'desc'):
                yield from _strings(part)
    elif isinstance(value, list):
        for part in value:
            yield from _strings(part)


class TaskBoard:
    def __init__(self, ws):
        self.ws = ws
        self.state = ws.workflow.setdefault('construction', {'tasks': {}, 'receipts': {}, 'reports': {}})

    def plan(self, tasks, replace=False):
        from bonus_agent.agent import ToolError
        proposed = {} if replace else copy.deepcopy(self.state['tasks'])
        schemes = self.ws.workflow.get('plan', {}).get('schemes', self.ws.schemes)
        for item in tasks:
            if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k].strip() for k in ('id', 'scheme', 'target')):
                raise ToolError('construction.plan：每项须给非空id、scheme、target')
            spec = {k: copy.deepcopy(item[k]) for k in ('id', 'scheme', 'target', 'title', 'depends_on', 'clauses', 'facts') if k in item}
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
            if set(spec['clauses']) - {c.id for c in self.ws.clauses}:
                raise ToolError('construction.source：条款ID不存在')
            if set(spec['facts']) - {f.name for f in self.ws.facts}:
                raise ToolError('construction.source：参数名不存在')
            proposed[spec['id']] = spec
        targets = [(s['scheme'], s['target']) for s in proposed.values()]
        if len(set(targets)) != len(targets):
            raise ToolError('construction.plan：同一目标只允许一个任务，修改时复用id')
        if any(set(s['depends_on']) - set(proposed) for s in proposed.values()):
            raise ToolError('construction.dependency：依赖任务不存在；按基础到上层分批登记，或在同批补齐')
        try:
            list(TopologicalSorter({n: s['depends_on'] for n, s in proposed.items()}).static_order())
        except CycleError as exc:
            raise ToolError('construction.cycle：任务依赖成环，未保存') from exc
        self.state['tasks'] = proposed
        for scheme in {s['scheme'] for s in proposed.values()}:
            self.ws.drafts.setdefault(scheme, {})
        self.sync_targets()
        return {'status': 'PLANNED', **self.summary()}

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
        data = {n: _semantic(d.model_dump(mode='json')) for n, d in self.ws.datasets.items()}
        selected = {n for n, d in data.items() if fields & set(d.get('fields', {})) or any(n in t for t in text)}
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
            return fx.target_value(self.ws._merged(spec['scheme']), spec['target'])
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    def dependencies(self, spec):
        needed = set(spec['depends_on'])
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
        if parts[0] == 'variables' and parts[1] in refs:
            needed.add(spec['id'])
        return needed

    def token(self, spec, dependencies):
        related = []
        for fact in self.ws.facts:
            if fact.name in spec['facts'] or any(self.owns(spec, t) for t in fact.targets):
                related.append(fact.model_copy(update={'targets':[t for t in fact.targets if self.owns(spec,t)]}).model_dump(mode='json'))
        from bonus_agent.workflow import _EXECUTION_PROFILE
        datasets, mapping = ({}, {}) if spec['target'].startswith('/tables/') else self.data_inputs(spec)
        return fingerprint({'profile': _EXECUTION_PROFILE, 'files': self.ws._source_version, 'period': str(self.ws.period),
            'spec': spec, 'value': self.value(spec), 'facts': related,
            'clauses': [(c.id, c.file, c.quote) for c in self.ws.clauses if c.id in spec['clauses']],
            'datasets': datasets, 'mapping': mapping,
            'structure': _semantic(self.ws._merged(spec['scheme']).get('allocation', [])) if not spec['target'].startswith('/tables/') else None,
            'dependencies': dependencies})

    def owns(self, spec, target):
        scheme, path = fx.split_target(target)
        parts, root = fx.target_parts(path), fx.target_parts(spec['target'])
        return scheme in (None, spec['scheme'], '共用') and parts[:len(root)] == root

    def statuses(self):
        from bonus_agent.agent import ToolError
        tasks = self.state['tasks']
        graph = {n:self.dependencies(s) for n,s in tasks.items()}
        try:
            order = list(TopologicalSorter(graph).static_order())
        except CycleError:
            return {n:{'status':'PENDING','version':fingerprint([s,self.value(s)]),
                       'depends_on':sorted(graph[n]),'cycle':True} for n,s in tasks.items()}
        out = {}
        for name in order:
            spec = tasks[name]
            deps = {n:out[n]['version'] for n in graph[name]}
            version = self.token(spec, deps)
            ready = all(out[n]['status'] == 'PASS' for n in graph[name])
            status = 'PASS' if ready and self.state['receipts'].get(name) == version else ('PENDING' if ready else 'WAITING')
            out[name] = {'status':status, 'version':version, 'depends_on':sorted(graph[name])}
        return out

    def issues(self, spec):
        from bonus_agent.semantics import target_mode_issues
        value = self.value(spec)
        if value is None or value == [] or value == {}:
            return [f'construction.missing：先用set_rules/compile_table构建{spec["scheme"]}::{spec["target"]}']
        rs = self.ws._ruleset(spec['scheme'])
        parts = fx.target_parts(spec['target'])
        if parts[0] in ('variables', 'pool') and not rs.allocation:
            return ['construction.structure：先用set_rules声明allocation实体层级及主键，再检查公式作用域；尚未验证的结构不能签发PASS']
        prefix = '.'.join(parts)
        own = lambda where: where == prefix or where.startswith(prefix + '.')
        issues = [str(i) for i in self.ws._validate(spec['scheme'], rs) if i.severity == 'blocking' and own(i.where)
                  and not (i.code == 'scope_bad' and not rs.allocation)]
        issues += [message for _,message in lint.static_gates(rs) if message.startswith(prefix + ' ')]
        related = [f for f in self.ws.facts if f.name in spec['facts'] or any(self.owns(spec,t) for t in f.targets)]
        for fact in related:
            bindings = [t for t in fact.targets if self.owns(spec,t)]
            if not bindings:
                issues.append(f'construction.binding：{fact.name}尚未绑定到{spec["target"]}，使用bind_facts')
                continue
            candidate = fact.model_copy(update={'targets':bindings})
            error = fx.check_fact(candidate, self.ws._corpus())
            if error:
                issues.append(f'{fact.name}：{error}')
            local = candidate.model_copy(update={'targets':[spec['scheme'] + '::' + fx.split_target(t)[1]
                                                          if fx.split_target(t)[0] == '共用' else t for t in bindings]})
            issues += fx.binding_errors([local], {spec['scheme']:self.ws._merged(spec['scheme'])})
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
        issues += target_mode_issues(self.ws, spec['scheme'], spec['target'], spec['clauses'])
        return list(dict.fromkeys(issues))

    def check(self, task, *, include_next=True):
        from bonus_agent.agent import ToolError
        if task not in self.state['tasks']:
            raise ToolError('construction.task：任务不存在，先plan_formula_tasks')
        self.ws._check_source_version()
        statuses = self.statuses()
        spec = self.state['tasks'][task]
        pending = [n for n in statuses[task]['depends_on'] if statuses[n]['status'] != 'PASS']
        issues = ([f'construction.cycle：当前公式依赖成环，先用set_rules修正{spec["target"]}'] if statuses[task].get('cycle') else
                  ([f'construction.dependency：先检查依赖任务{pending}'] if pending else self.issues(spec)))
        report = {'status':'FAIL' if issues else 'PASS','task':task,'target':spec['target'],'issues':issues,
                  'version':statuses[task]['version']}
        self.state['reports'][task] = copy.deepcopy(report)
        if not issues:
            # Only a program check can issue this receipt. It never approves a monetary result.
            self.state['receipts'][task] = statuses[task]['version']
            known = cl.known_refs(self.ws.drafts, self.ws.datasets)
            ref = '.'.join(fx.target_parts(spec['target']))
            if ref in known:
                updates = {c.id:'、'.join(dict.fromkeys([*cl.refs(c.where), ref])) for c in self.ws.clauses
                           if c.id in spec['clauses'] and not c.where.startswith((cl.BUILTIN, cl.NOT_APPLY, cl.ASKED))}
                if updates:
                    self.ws.cover_clauses(updates)
        if include_next:
            report['next'] = self.summary()['next']
        return report

    def refresh(self):
        """Recheck stale receipts in dependency order, without spending a model turn per item."""
        self.sync_targets()
        for name in list(self.statuses()):
            status = self.statuses()[name]
            previous = self.state['reports'].get(name, {})
            if (status['status'] == 'PENDING' and name in self.state['receipts']
                    and not (previous.get('version') == status['version'] and previous.get('status') == 'FAIL')):
                self.check(name, include_next=False)

    def coverage_gaps(self):
        tasks = self.state['tasks']
        gaps = []
        for scheme in self.ws.workflow.get('plan', {}).get('schemes', []):
            roots = {s['target'] for s in tasks.values() if s['scheme'] == scheme}
            required = {'/pool','/allocation'} | ({'/report'} if self.ws.report_required else set())
            gaps += [f'construction.plan：{scheme}缺少构建任务{target}' for target in sorted(required-roots)]
            for root, value in self.ws._merged(scheme).items():
                if root not in ROOTS or not value:
                    continue
                targets = ['/' + root + '/' + name.replace('~','~0').replace('/','~1') for name in value] if root in ('tables','variables') else ['/' + root]
                gaps += [f'construction.plan：{scheme}的{target}未安排局部检查任务' for target in targets if target not in roots]
        assigned = {cid for s in tasks.values() for cid in s['clauses']}
        gaps += [f'construction.plan：{c.id}尚未安排到任务；补充任务clauses或按原文明确不适用' for c in self.ws.clauses
                 if not c.where and c.id not in assigned]
        return gaps

    def adopt(self):
        """Existing complete rules may be revalidated without asking the model to rebuild their plan."""
        if self.state['tasks'] or not self.ws.schemes:
            return
        if any(not self.ws._merged(s).get('pool') or not self.ws._merged(s).get('allocation') or
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

    def summary(self):
        self.refresh()
        tasks, statuses = self.state['tasks'], self.statuses()
        active = next((n for n,s in statuses.items() if s['status']=='PENDING'), None)
        if not tasks:
            return {'total':0,'passed':0,'active':None,'next':'plan_formula_tasks：按参数表→基础变量→金额变量→pool→分配→报表规划任务、依赖及条款；不要先反复compile_formulas'}
        if active is None:
            gaps = self.coverage_gaps()
            return {'total':len(tasks),'passed':sum(s['status']=='PASS' for s in statuses.values()),'active':None,
                    'next':gaps[0] if gaps else 'compile_formulas'}
        spec = tasks[active]
        if not self.ws._merged(spec['scheme']).get('allocation') and any(
                s['target'].startswith('/variables/') or s['target'] == '/pool' for s in tasks.values()):
            return {'total': len(tasks), 'passed': sum(s['status']=='PASS' for s in statuses.values()),
                    'active': {**spec, 'status': statuses[active]['status']},
                    'next': 'set_rules：先声明allocation的实体层级、key和parent_key；结构是公式作用域检查的前置条件，不需要先计算金额'}
        action = 'check_formula_task' if self.value(spec) is not None else ('compile_table' if spec['target'].startswith('/tables/') else 'set_rules')
        last = self.state['reports'].get(active,{})
        gaps = self.coverage_gaps()
        return {'total':len(tasks),'passed':sum(s['status']=='PASS' for s in statuses.values()),
                'coverage_gap_count': len(gaps), 'coverage_gaps': gaps[:4],
                'active':{**spec,'status':statuses[active]['status'],
                          'last_issues':last.get('issues',[])[:3] if last.get('version') == statuses[active]['version'] else []},
                'next':f'{action}：当前任务{active}，只处理{spec["scheme"]}::{spec["target"]}，依赖已通过；完成后check_formula_task(task="{active}")'}

    def gate(self):
        self.adopt()
        if not self.state['tasks']:
            return ['construction.plan：先plan_formula_tasks，逐个构建并check_formula_task，再整体编译']
        # A changed version of a previously checked component can be rechecked locally without another model round.
        self.refresh()
        return self.coverage_gaps() + [f'construction.task：{n}未通过局部检查或已失效，调用check_formula_task'
                                       for n,s in self.statuses().items() if s['status'] != 'PASS']
