"""Program-owned receipts for staged calculation. No model can approve a total."""

from __future__ import annotations

import copy
import json
import hashlib
from pathlib import Path
from datetime import datetime
from decimal import Decimal
from graphlib import TopologicalSorter

from bonus_agent.recovery import fingerprint
from bonus_core.engine.allocation import round_pool

STAGES = (
    ('盘点材料', 'plan_calculation'),
    ('整理规则', 'record_facts / record_clauses'),
    ('准备数据', 'prepare_data'),
    ('编译公式', 'compile_formulas'),
    ('基础金额', 'calculate_basis'),
    ('独立复核总额', 'review_basis'),
    ('等待总额确认', '由财务在总额确认页操作'),
    ('人员分配', 'trial_run'),
    ('全量终检', 'review_calculation / verify / finish'),
)

_CORE = Path(__file__).resolve().parent.parent / 'bonus_core'
_EXECUTION_PROFILE = hashlib.sha256(
    b''.join(p.read_bytes() for p in sorted(_CORE.rglob('*.py'))) + Path(__file__).read_bytes()
    + b''.join(Path(__file__).with_name(n).read_bytes() for n in
               ('lint.py', 'facts.py', 'clauses.py', 'source_cells.py', 'source_diagnostics.py', 'scope_diagnostics.py', 'question_review.py', 'reread.py', 'coefficient.py', 'agent.py', 'context.py', 'advice_context.py', 'recovery.py', 'construction.py', 'semantics.py', 'progress.py', 'rule_package.py', 'contracts.py', 'data_profile.py', 'review_population.py'))
).hexdigest()


def fail(message):
    from bonus_agent.agent import ToolError

    raise ToolError('workflow.gate：' + message)


class Workflow:
    def __init__(self, ws):
        self.ws = ws
        self.state = ws.workflow
        self.state.setdefault('receipts', {})
        self.state.setdefault('basis', {})
        self.state.setdefault('attempts', {})

    def basis_view(self):
        """A read-only validation view; deferred allocation evidence stays in the real workspace."""
        from bonus_core.dsl.validator import basis_rules
        from bonus_agent import facts as fx
        from bonus_agent.construction import TaskBoard
        from bonus_agent.extract_rules import Extraction

        view = copy.copy(self.ws)
        view._phase_source_drafts = {s: self.ws._merged(s) for s in self.ws.schemes}
        view._phase_name = 'basis'
        view._phase_binding_gaps = []
        view._phase_binding_evidence = []
        view.drafts = {s: basis_rules(self.ws._ruleset(s)).model_dump(mode='json', include=set(Extraction.model_fields))
                       for s in self.ws.schemes}
        for scheme, draft in view.drafts.items():
            for level in draft.get('allocation', []):
                level.pop('weight', None)
            original_variables = self.ws._merged(scheme).get('variables') or {}
            for name, variable in draft.get('variables', {}).items():
                if 'scope' not in (original_variables.get(name) or {}):
                    # An implicit global default is not an explicit instruction to repeat a
                    # whole-company aggregate at each object. Preserve this validation fact.
                    variable.pop('scope', None)
        tasks = self.state.get('construction', {}).get('tasks', {})
        required = TaskBoard(self.ws).required('basis')
        deferred_facts = {f for n, spec in tasks.items() if n not in required for f in spec['facts']}
        required_facts = {f for n, spec in tasks.items() if n in required for f in spec['facts']}

        def projected_targets(target):
            scheme, path = fx.split_target(target)
            parts = fx.target_parts(path)
            if not parts or parts[0] in ('allocation', 'payout', 'offsets'):
                return [], []
            if parts[0] == 'report' and len(parts) > 1 and parts[1] not in ('totals', 'details', 'balances'):
                return [], []
            candidates = view.drafts if scheme in (None, '共用') else {scheme: view.drafts.get(scheme, {})}
            out, omitted = [], []
            for name, draft in candidates.items():
                if parts[0] in ('variables', 'tables') and len(parts) > 1 and parts[1] not in draft.get(parts[0], {}):
                    original = self.ws._merged(name) if name in self.ws.schemes else {}
                    try:
                        fx.target_value(original, path)
                    except (KeyError, IndexError, TypeError, ValueError):
                        pass  # A genuinely missing target must reach normal binding validation.
                    else:
                        omitted.append({'scheme': name, 'path': '/' + '/'.join(
                            str(p).replace('~', '~0').replace('/', '~1') for p in parts)})
                        continue
                mapped = list(parts)
                array_path = parts[:2] if parts[0] == 'report' and len(parts) > 2 else (parts[:1] if parts[0] == 'checks' and len(parts) > 1 else None)
                if array_path:
                    index_at = len(array_path)
                    try:
                        original = fx.target_value(self.ws._merged(name), '/' + '/'.join(array_path))
                        item = original[int(parts[index_at])]
                        if item.get('stage', 'basis') != 'basis':
                            continue
                        selected = fx.target_value(draft, '/' + '/'.join(array_path))
                        identity = 'name' if parts[0] == 'report' else 'id'
                        mapped[index_at] = str(next(i for i, value in enumerate(selected) if value[identity] == item[identity]))
                    except (KeyError, IndexError, ValueError, TypeError, StopIteration):
                        pass  # Keep an invalid original target visible to binding validation.
                out.append(name + '::/' + '/'.join(str(p).replace('~', '~0').replace('/', '~1') for p in mapped))
            return out, omitted

        view.facts = []
        for fact in self.ws.facts:
            projected = [projected_targets(original) for original in fact.targets]
            targets = list(dict.fromkeys(t for selected, _ in projected for t in selected))
            omissions = []
            for _, omitted in projected:
                for entry in omitted:
                    parts = fx.target_parts(entry['path'])
                    owners = {n for n, spec in tasks.items() if spec['scheme'] == entry['scheme']
                              and parts[:len(fx.target_parts(spec['target']))] == fx.target_parts(spec['target'])}
                    is_required = bool(owners & required) if owners else (
                        fact.name in required_facts or fact.name not in deferred_facts)
                    if fact.name in required_facts and not targets:
                        # Sharing a fact may defer its other targets, but cannot erase its
                        # only proof when an active basis task explicitly requires it.
                        is_required = True
                    if is_required:
                        omissions.append(entry)
            if omissions:
                from bonus_agent.clauses import phase_reference_message
                view._phase_binding_evidence.append({'fact': fact.model_dump(mode='json'), 'targets': omissions})
                view._phase_binding_gaps.append(f'{fact.name}：parameter.binding.phase_reference，'
                                               + phase_reference_message('basis', omissions))
            if fact.targets and not targets and (omissions or fact.name not in required_facts):
                continue
            if not fact.targets and fact.name in deferred_facts - required_facts:
                continue
            view.facts.append(fact.model_copy(update={'targets': targets}))
        deferred_clauses = {c for n, spec in tasks.items() if n not in required for c in spec['clauses']}
        required_clauses = {c for n, spec in tasks.items() if n in required for c in spec['clauses']}
        view.clauses = [c for c in self.ws.clauses if c.id not in deferred_clauses - required_clauses]
        return view

    def version(self, kind='formula'):
        ws = self.ws
        body = {
            'execution_profile': _EXECUTION_PROFILE,
            'files': ws._source_version,
            'period': str(ws.period),
            'datasets': {n: d.model_dump(mode='json') for n, d in ws.datasets.items()},
            'mapping': {n: m.model_dump(mode='json') for n, m in ws.mapping.items()},
        }
        if kind == 'data':
            body['structure'] = {
                s: {
                    'patches': ws._merged(s).get('patches'),
                    'allocation': [
                        {k: lv.get(k) for k in ('level', 'key', 'parent_key')}
                        for lv in ws._merged(s).get('allocation') or []
                    ],
                }
                for s in ws.schemes
            }
        else:
            if kind == 'basis':
                ws = self.basis_view()
                from bonus_agent.construction import TaskBoard
                required = TaskBoard(self.ws).required('basis')
                if self.state.get('plan', {}).get('output_targets'):
                    # Distribution expressions are otherwise projected out. Removing a
                    # promised output must invalidate an earlier structure check.
                    body['missing_outputs'] = TaskBoard(self.ws).output_obligations('basis')
                # Evidence omitted from executable rules still invalidates old PASS receipts.
                # Hash source/target identities, never presentation text from diagnostics.
                body['phase_binding_evidence'] = ws._phase_binding_evidence
            body.update(
                rules=ws._rules_print(),
                plan=self.state.get('plan'),
                facts=[f.model_dump(mode='json') for f in ws.facts],
                clauses=[c.model_dump(mode='json', exclude={'checked'} if kind == 'basis' else set()) for c in ws.clauses],
                answers=[(q.id, q.answer, q.withdrawn) for q in ws.questions],
                said=ws.said,
                transcripts=ws.transcripts,
                construction_plan={n:s for n,s in self.state.get('construction', {}).get('tasks', {}).items()
                                   if kind != 'basis' or n in required},
            )
        return fingerprint(body)

    def receipt(self, name, payload=None, *, kind='formula'):
        rec = {
            'version': self.version(kind),
            'kind': kind,
            'at': datetime.now().isoformat(timespec='seconds'),
            'payload': copy.deepcopy(payload or {}),
        }
        self.state['receipts'][name] = rec
        return rec

    def current(self, name):
        rec = self.state['receipts'].get(name)
        return bool(rec and rec['version'] == self.version(rec.get('kind', 'formula')))

    def require(self, name):
        self.ws._check_source_version()
        if not self.current(name):
            fail(f'{name} 尚未通过或版本已变化；下一步：{self.hint()}')
        if name.startswith('basis:'):
            scheme = name.split(':', 1)[1]
            if self.state['receipts'][name]['payload'].get('digest') != fingerprint(self.state['basis'].get(scheme)):
                fail('预核算工件与通过回执不一致，请重新核算')

    def has_plan(self):
        return self.state.get('plan', {}).get('files_version') == fingerprint(self.ws._source_version)

    def skipped(self):
        return {x['ref'] for x in self.state.get('plan', {}).get('ignored', [])} if self.has_plan() else set()

    def plan(self, summary, outputs, files, schemes, ignored=None, output_targets=None):
        known = {p.name for p in self.ws.inv.files}
        if not summary.strip() or not outputs or not schemes or len(set(schemes)) != len(schemes):
            fail('计划须包含任务说明、非空输出清单和不重复的方案名')
        if any(not isinstance(x, str) or not x.strip() for x in [*outputs, *schemes]):
            fail('输出和方案名须为非空文字')
        if set(files) != known:
            fail(f'材料分类须覆盖全部文件；缺少 {sorted(known-set(files))}，未知 {sorted(set(files)-known)}')
        if any(v not in ('rule', 'data', 'rule_and_data', 'reference', 'ignore') for v in files.values()):
            fail('材料用途须为rule/data/rule_and_data/reference/ignore；忽略依据另登记到条款/说明')
        ignored = ignored or []
        if any(
            not isinstance(x, dict) or x.get('ref') not in known or not str(x.get('reason', '')).strip()
            for x in ignored
        ):
            fail('忽略项须给真实文件名ref和非空reason')
        if {x['ref'] for x in ignored} != {n for n, r in files.items() if r == 'ignore'}:
            extra = {x['ref']: files[x['ref']] for x in ignored if files[x['ref']] != 'ignore'}
            missing = sorted(n for n, r in files.items() if r == 'ignore' and n not in {x['ref'] for x in ignored})
            fail(
                f'plan.ignore_role：ignored中用途不为ignore的文件及实际用途={extra}；缺少忽略依据={missing}。'
                'reference保留作参考时从ignored删除；真正不参与时才分类为ignore并写依据。两处必须一致'
            )
        if output_targets is None:
            output_targets = self.state.get('plan', {}).get('output_targets')
        if output_targets is not None:
            from bonus_agent.construction import validate_output_targets
            output_targets = validate_output_targets(outputs, schemes, output_targets)
        self.state['plan'] = {
            'summary': summary,
            'outputs': outputs,
            'files': files,
            'schemes': schemes,
            'ignored': ignored,
            'files_version': fingerprint(self.ws._source_version),
        }
        if output_targets is not None:
            self.state['plan']['output_targets'] = output_targets
        return {'status': 'PASS', 'plan': self.state['plan'], 'next': self.hint()}

    def rule_requirements(self):
        ws = self.ws
        plan = self.state.get('plan') or {}
        relevant = {n for n, r in plan.get('files', {}).items() if r in ('rule', 'rule_and_data')}
        from bonus_agent.agent import RULE_SUFFIXES

        relevant |= {p.name for p in ws.inv.files if p.suffix.lower() in RULE_SUFFIXES and p.name not in self.skipped()}
        result = []
        for n in sorted(relevant):
            facts = sum(f.file == n for f in ws.facts)
            clauses = sum(c.file == n for c in ws.clauses)
            result.append(
                {
                    'file': n,
                    'facts': facts,
                    'clauses': clauses,
                    'missing': (['facts'] if not facts else []) + (['clauses'] if not clauses else []),
                }
            )
        return result

    def rules_ready(self):
        missing = [item['file'] for item in self.rule_requirements() if item['missing']]
        # Automatic document coverage remains mandatory; model classification cannot exempt policy documents.
        missing += self.ws._page_gaps(self.skipped())
        return missing

    def prepare_data(self):
        from bonus_core.data.population import prepare, gaps
        from bonus_core.engine.tree import build_tree
        from bonus_core.dsl.validator import validate_structure
        from bonus_agent.agent import ToolError

        if not self.has_plan():
            fail('材料清单或版本已变化，先完成plan_calculation')
        if not self.ws.datasets:
            fail('尚未定义数据表')
        issues = []
        counts = {}
        names = self.ws.schemes or [None]
        for name in names:
            rs = self.ws._ruleset(name) if name else self._data_rules()
            issues += [str(i) for i in validate_structure(rs) if i.severity == 'blocking']
            keys = {d.key for d in rs.datasets.values() if d.key}
            level_keys = {lv.key for lv in rs.allocation}
            for n, d in rs.datasets.items():
                if d.link and d.link not in keys | level_keys:
                    # 只说“没有父实体”时，实测模型会把已有实体的 key 改成不唯一的字段（被拒），再原地反复查询直到卡住
                    issues.append(f'{n} 关联字段 {d.link} 没有父实体：没有哪张实体表以 {d.link} 为 key。'
                                  f'可另建一张按 {d.link} 合并的实体表（define_dataset，key={d.link}、distinct=true，'
                                  f'fields 只放 {d.link} 和同一 {d.link} 下取值唯一的字段），'
                                  f'或把 {n} 改为按已有实体的 key 关联（现有：{"、".join(sorted(keys | level_keys)) or "无"}）；'
                                  '不要把已有实体的 key 改成不唯一的字段')
                if d.key in level_keys and d.link:
                    issues.append(f'{n} 是分配实体，不能同时声明link')
            try:
                data, _ = self.ws._data(rs)
            except ToolError as exc:
                issues.append(f'方案「{name}」数据读入未通过：{exc}')
                continue
            data, patched = prepare(rs, data)
            issues += [str(i) for i in patched if i.severity == 'blocking']
            issues += [message for _, message in gaps(rs, data, self.ws.mapping)]
            if rs.allocation:
                issues += [str(i) for i in build_tree(rs, data)[2] if i.severity == 'blocking']
            else:
                for n, d in rs.datasets.items():
                    if d.key:
                        keys = [r.values.get(d.key) for r in data[n].rows]
                        if None in keys or len(set(keys)) != len(keys):
                            issues.append(f'{n} 主键为空或重复')
            counts.update({n: len(t.rows) for n, t in data.items()})
        try:
            diagnostic = self.ws.diagnose_datasets()
        except ToolError as exc:
            diagnostic = {'issues': [{'code': 'dataset.diagnostic', 'message': str(exc)}]}
        issues += [
            json.dumps(i, ensure_ascii=False)
            for i in diagnostic['issues']
            if i.get('code') not in ('dataset.reference', 'dataset.identity')
            or self.ws.datasets[i['dataset']].unmatched != 'ignore'
        ]
        report = {'status': 'FAIL' if issues else 'PASS', 'counts': counts, 'issues': issues, 'diagnostic': diagnostic}
        self.state['data_report'] = {'version': self.version('data'), 'report': copy.deepcopy(report)}
        if not issues:
            self.receipt('data', report, kind='data')
        return report

    def _data_rules(self):
        from bonus_core.dsl.models import RuleSet, Pool

        return RuleSet(
            name='数据准备', version='workflow', datasets=self.ws.datasets, pool=Pool(expr='0'), allocation=[]
        )

    def query_dataset(self, dataset, expr):
        """Read one mapped source without requiring any unrelated relationship or allocation."""
        from bonus_core.data.mapping import apply_mapping
        from bonus_core.dsl.models import RuleSet, Pool
        from bonus_core.engine.tree import Node, ROOT_LEVEL
        from bonus_core.engine.evaluator import Evaluator

        self.ws._check_source_version()
        if dataset not in self.ws.datasets:
            fail('请先定义要统计的数据表')
        definition = self.ws.datasets[dataset]
        if definition.population_from:
            fail('该实体范围来自有效明细；请查询population_from明细表，不能把未裁定的原表统计当成有效对象统计')
        ds = definition.model_copy(update={'key': None, 'link': None})
        rs = RuleSet(name='原表统计', version='workflow', datasets={dataset: ds}, pool=Pool(expr=expr), allocation=[])
        data, issues = apply_mapping(rs, self.ws.data_files, {dataset: self.ws.mapping[dataset]}, self.ws.period)
        blocking = [str(i) for i in issues if i.severity == 'blocking']
        if blocking:
            fail('原表统计存在数据问题：' + str(blocking[:8]))
        root = Node(ROOT_LEVEL, ROOT_LEVEL, None)
        root.own_rows[dataset] = data[dataset].rows
        ev = Evaluator(rs, root, {}, period=self.ws.period)
        value = ev.evaluate(expr, root, 'source_query', 'source_query')
        return {
            'dataset': dataset,
            'rows': len(data[dataset].rows),
            'value': str(value),
            'rendered': ev.traces['source_query'].rendered,
            'scope': '仅按该表映射、筛选与期间统计，不证明其他关联或金额通过验收',
        }

    def compile(self, phase=None):
        from bonus_agent.construction import TaskBoard
        from bonus_agent.agent import ToolError
        try:
            phase = phase or TaskBoard(self.ws).phase()
            self.ws._check_source_version()
            prior = self.compile_feedback(phase)
            if prior and prior['status'] == 'FAIL':
                # A failed deterministic check is useful feedback, not a reason to run the
                # identical preflight again. A changed rule/evidence version invalidates it.
                self._invalidate_compilation(phase)
                return {**self.compile_summary(prior), 'cached': True}
            report = self._compile(phase)
        except Exception as exc:
            self._invalidate_compilation(phase or 'basis')
            if isinstance(exc, ToolError):
                self._save_compile_report({'status': 'FAIL', 'issues': [str(exc)], 'warnings': [],
                                           'graph': {}, 'repaired': [], 'phase': phase or 'basis', 'preflight': {},
                                           'static_checks_complete': False})
            raise
        if report['status'] != 'PASS':
            self._invalidate_compilation(phase)
        return report

    def _save_compile_report(self, report):
        from bonus_agent.construction import TaskBoard
        kind = 'basis' if report.get('phase') == 'basis' else 'formula'
        version = self.version()
        board = TaskBoard(self.ws)
        self.state['formula_report'] = {
            'version': version, 'kind': kind,
            'phase_version': self.version(kind) if kind == 'basis' else version,
            'task_receipts': {name: board.state['receipts'].get(name)
                              for name in board.required(report.get('phase'))},
            'report': copy.deepcopy(report),
        }

    def compile_feedback(self, phase='basis', *, version=None):
        """Only current, phase-specific diagnostics may guide the next repair."""
        saved = self.state.get('formula_report', {})
        report = saved.get('report') or {}
        if report.get('phase') != phase:
            return None
        receipts = self.state.get('construction', {}).get('receipts', {})
        if any(receipts.get(name) != token for name, token in saved.get('task_receipts', {}).items()):
            return None
        if saved.get('phase_version'):
            kind = 'basis' if phase == 'basis' else 'formula'
            return report if saved['phase_version'] == (version or self.version(kind)) else None
        # Existing cases did not record a projected version. Do not guess whether a
        # failure still applies after any rules/evidence change.
        return report if saved.get('version') == self.version() else None

    def _invalidate_compilation(self, phase):
        receipts = self.state['receipts']
        names = [name for name in receipts if name == 'allocation_formulas' or
                 (phase == 'basis' and (name == 'formulas' or name.startswith(('basis:', 'review:', 'approved:'))))]
        if names:
            self.state['invalidated_receipts'] = {
                'reason': '当前编译未通过，旧回执不得继续放行', 'phase': phase,
                'receipts': {name: receipts.pop(name) for name in names},
            }
        if phase == 'basis':
            self.state.pop('waiting', None)
            self.ws.done = False

    def _compile(self, phase):
        from bonus_core.dsl.parser import names, parse
        from bonus_core.dsl.validator import validate_basis
        from bonus_agent.construction import TaskBoard

        self.require('data')
        phase = phase or TaskBoard(self.ws).phase()
        missing = self.rules_ready()
        if missing:
            fail('规则证据未齐：' + str(missing[:8]))
        planned, declared = self.state['plan']['schemes'], self.ws.schemes
        empty = [s for s in declared if s not in planned and not any((self.ws.drafts.get(s) or {}).values())]
        for s in empty:            # 计划外、没有任何内容的方案：直接去掉，不算不一致
            self.ws.drafts.pop(s, None)
        declared = self.ws.schemes
        if set(declared) != set(planned):
            extra, missing = sorted(set(declared) - set(planned)), sorted(set(planned) - set(declared))
            fail(f'已声明方案与计划不一致：计划 {planned}，规则里 {declared}。'
                 + (f'多出 {extra}：把其中的表和变量用 set_rules 写到计划里的方案，再在多出的方案里把这些项写成 null，'
                    '清空后程序自动去掉。' if extra else '')
                 + (f'缺少 {missing}：用 set_rules(scheme=…) 写出这些方案的规则。' if missing else '')
                 + '确实要改方案，先依据原文用 plan_calculation 调整计划')
        repaired = self.ws.validate_parameters(repair=True)['repaired']
        issues = []
        graph = {}
        warnings = []
        from bonus_agent.lint import compile_warnings, reached
        from bonus_agent.semantics import semantic_gates
        construction_issues = TaskBoard(self.ws).gate(phase)
        view = self.basis_view() if phase == 'basis' else self.ws
        construction_issues += semantic_gates(view)
        all_reached = set().union(*(reached(view._ruleset(s)) for s in view.schemes))
        for s in self.ws.schemes:
            rs = view._ruleset(s)
            warnings += [f'方案「{s}」：{message}' for message in compile_warnings(rs, all_reached)]
            validation = validate_basis(rs) if phase == 'basis' else self.ws._validate(s, rs)
            issues += [str(i) for i in validation if i.severity == 'blocking']
            if phase == 'basis':
                issues += [str(i) for i in self.ws._validate(s, rs)
                           if i.severity == 'blocking' and i.code == 'global_in_level']
            from bonus_agent.lint import static_gates
            issues += [f'方案「{s}」：{message}' for _, message in static_gates(rs)]
            from bonus_core.dsl.parser import ExprError
            try:
                deps = {n: names(parse(v.expr)) & set(rs.variables) for n, v in rs.variables.items()}
            except ExprError:
                deps = {}  # The validator above reports the exact malformed expression.
            try:
                order = list(TopologicalSorter(deps).static_order())
            except ValueError as exc:
                issues.append(str(exc))
                order = []
            graph[s] = {'order': order, 'dependencies': {n: sorted(v) for n, v in deps.items()}}
        self.ws._order(self.ws.schemes)  # Inter-scheme cycles are also invalid.
        # Structural blockers must remain visible even with hundreds of evidence gaps.
        issues = list(dict.fromkeys(issues + construction_issues + view._binding_gaps() + view._clause_gaps(self.skipped())))
        preflight = {}
        if not issues and phase == 'basis':
            from bonus_agent.agent import ToolError
            from bonus_core.engine.preflight import preflight_basis
            for name in self.ws._order(self.ws.schemes):
                refs, missing_refs = {}, []
                for dependency, revision in self.ws._deps(name):
                    if (not self.current('approved:' + dependency) or dependency in self.ws.untested
                            or revision not in self.ws.results.get(dependency, {})):
                        missing_refs.append(f'{dependency}/{revision}')
                    else:
                        refs[f'{dependency}/{revision}'] = self.ws.results[dependency][revision]
                if missing_refs:
                    # Compilation cannot approve a dependency or calculate recipients.
                    preflight[name] = {'status': 'DEFERRED', 'dependencies': missing_refs,
                                       'reason': '等待上游已确认且有效的结果；尚未完成本方案求值预检'}
                    continue
                try:
                    rs = view._ruleset(name)
                    data, _ = view._data(rs)
                    result = preflight_basis(rs, data, refs, self.ws.period)
                    if result['status'] == 'FAIL':
                        from bonus_agent.scope_diagnostics import scope_diagnostics
                        empty = next((c['dataset'] for issue in result.get('issues', [])
                                      for c in issue.get('coverage', [])
                                      if c.get('visible_rows') == 0 and c.get('dataset')), None)
                        scope = scope_diagnostics(self.ws, scheme=name, data=data, focus=empty, limit=1)
                        result['scope_context'] = scope
                        if result.get('issues'):
                            # The existing checks/preflight read entry exposes this
                            # focused observation alongside the original failure.
                            result['issues'][0]['scope_context'] = scope
                except ToolError as exc:
                    result = {'status': 'FAIL', 'issues': [{'message': str(exc)}]}
                preflight[name] = result
                for issue in result.get('issues', []) if result['status'] == 'FAIL' else []:
                    brief = {k: issue[k] for k in ('code', 'message', 'where', 'node_id', 'source', 'formula', 'repair') if k in issue}
                    brief['coverage'] = [{k: c[k] for k in ('dataset', 'visible_rows', 'selected_rows', 'dataset_rows', 'active_row_source') if k in c}
                                         for c in issue.get('coverage', [])[:2]]
                    issues.append(f'方案「{name}」基础求值预检：' + json.dumps(brief, ensure_ascii=False)[:2400])
        report = {'status': 'FAIL' if issues else 'PASS', 'issues': issues, 'warnings': warnings,
                  'graph': graph, 'repaired': repaired, 'phase': phase, 'preflight': preflight,
                  # Static diagnostics were enumerated, not necessarily passed.
                  # Runtime preflight coverage is recorded separately by scheme above.
                  'static_checks_complete': True}
        self._save_compile_report(report)
        if not issues:
            self.receipt('allocation_formulas' if phase == 'all' else 'formulas', report,
                         kind='formula' if phase == 'all' else 'basis')
        return self.compile_summary(report)

    def compile_summary(self, report):
        """Bound tool output without losing the full diagnostic or its continuation address."""
        preview, chars = [], 0
        for issue in report['issues']:
            if len(preview) >= 6 or chars + len(issue) > 3000:
                break
            preview.append(issue)
            chars += len(issue)
        return {
            'status': report['status'], 'issues': preview, 'issue_count': len(report['issues']),
            'warnings': report['warnings'][:3], 'warning_count': len(report['warnings']),
            'repaired': report.get('repaired', [])[:3],
            'graph': {s: {'variables': len(v['order'])} for s, v in report['graph'].items()},
            'next_call': {'tool': 'read_workspace', 'args': {'section': 'checks', 'offset': len(preview), 'limit': 6}}
                         if len(preview) < len(report['issues']) or len(report['warnings']) > 3 else None,
            'next': '按issues逐项修复；完整检查见read_workspace(section="checks")。草稿保存不等于校验通过。'
                    if report['issues'] else ('trial_run' if report.get('phase') == 'all' else 'calculate_basis'),
            'phase': report.get('phase', 'all'),
            'static_checks_complete': report.get('static_checks_complete') is True,
            'preflight': {s: {'status': r['status'], 'summary': r.get('summary'),
                              **({'scope_context': r['scope_context']} if r.get('scope_context') else {}),
                              'dependencies': r.get('dependencies', []),
                              'details_call': {'tool': 'read_workspace', 'args': {'section': 'checks', 'name': 'preflight:' + s}} if r.get('issues') else None}
                          for s, r in report.get('preflight', {}).items()},
        }

    def references(self, name):
        refs = {}
        for scheme, revision in self.ws._deps(name):
            self.require('approved:' + scheme)
            if scheme not in self.ws.results or scheme in self.ws.untested:
                self.ws._trial(scheme)
            if revision not in self.ws.results[scheme]:
                fail(f'依赖修订 {scheme}/{revision} 不存在')
            refs[f'{scheme}/{revision}'] = self.ws.results[scheme][revision]
        return refs

    def calculate_basis(self, scheme=None):
        from bonus_core.engine.run import Engine

        self.require('data')
        self.require('formulas')
        name = self.ws._scheme(scheme)
        rs = self.ws._ruleset(name)
        data, _ = self.ws._data(rs)
        basis = Engine.prepare_basis(rs, data, self.references(name), self.ws.period).calculate_basis()
        self.state['basis'][name] = basis
        self.receipt('basis:' + name, {'digest': fingerprint(basis)}, kind='basis')
        return {
            'status': 'PASS',
            'scheme': name,
            'digest': fingerprint(basis),
            'pool': basis['pool'],
            'totals': basis['totals'],
            'objects': len(basis['objects']),
            'next': 'review_basis；此处没有计算人员分配或实发金额',
        }

    def review_basis(self, scheme=None):
        from bonus_agent import reread as rr
        from bonus_agent.agent import ToolError
        from bonus_agent.llm import LLMError, RunBudgetError, parse_json
        from bonus_core.engine.evaluator import EvalError
        from bonus_core.engine.issues import EngineError

        name = self.ws._scheme(scheme)
        self.require('basis:' + name)
        version = self.version('basis')
        basis = self.state['basis'][name]
        digest = fingerprint(basis)
        from bonus_core.dsl.validator import basis_rules
        rs = basis_rules(self.ws._ruleset(name))
        from bonus_agent.extract_rules import data_dictionary
        from bonus_agent.review_population import requirements, check_populations

        structure = {
            'scheme': name,
            'pool_scope': rs.pool.scope,
            'period': str(self.ws.period),
            'data_scope': {
                n: {
                    'period': d.period,
                    'window': d.window,
                    'population_from': d.population_from,
                    'filter': self.ws.mapping[n].filter,
                }
                for n, d in self.ws.datasets.items()
            },
            'data_checks': self.state['receipts']['data']['payload'],
            'allocation': [{'level': lv.level, 'key': lv.key, 'parent_key': lv.parent_key} for lv in rs.allocation],
            'columns': {n: m.columns for n, m in self.ws.mapping.items()},
            'source_mapping': {n: m.model_dump(mode='json', include={'file', 'sheet', 'header_rows', 'combine', 'unpivot'})
                               for n, m in self.ws.mapping.items()},
            'details': [(d.name, d.level, d.desc) for d in (rs.report.details if rs.report else [])],
            'totals': [(t.name, t.level, t.desc) for t in (rs.report.totals if rs.report else [])],
            'population_requirements': requirements(rs),
        }
        from bonus_agent.context import guard_messages

        msgs = [
            {
                'role': 'system',
                'content': '【独立总额复核】只按原文和数据字典重建公式，不看生产公式或答案。只输出JSON对象：'
                '{"pool":"独立奖金池表达式","helpers":{"变量":{"scope":"层级或global","expr":"表达式"}},'
                '"tables":{},"details":{"报告名":"独立表达式"},"totals":{"汇总名":"独立表达式"},'
                '"populations":{"待核范围名":{"dataset":"来源表名","key":"对象标识标准字段",'
                '"filter":{},"period":null,"window":"period","sources":[{"doc":"规则文件","quote":"逐字范围依据"}]}},"unclear":[]}。'
                'populations必须覆盖population_requirements；从原文件重新选取对象，不继承主规则的筛选、期间或默认值。'
                'filter使用原始列名，period为日期标准字段或明确null；window为period/year_to_date/to_date。'
                '对象范围key可来自当期业务明细，程序按标识去重；rows:开头的范围不用key，核对该表实际参与的原始行。'
                '不得盲目照抄当前映射；如果独立范围需要的字段或依据缺失，写unclear。'
                '每个报告项都须给独立公式；材料不清楚写unclear，不猜测。金额由程序计算。'
                '数据概况中的期间、筛选、实体范围和关联是待核对的映射事实，不是已获业务批准的核算范围。'
                '必须按原材料独立核对本期核算对象集合、历史累计窗口、真实分类值和空值口径。'
                '属性提取失败不表示正常分类应为空串或默认类别；独立重建每个正常分类分支，不能把技术修复后的可执行公式当作业务依据。'
                '父实体与当期明细覆盖不同，须区分本期奖金对象和历史累计数据；不能直接裁掉历史实体或默认所有父对象都应核算。'
                '仅在同一映射上算出相同金额不足以证明范围正确；无法证明对象范围符合原文时，'
                '在unclear中列出具体表、字段、期间或覆盖数量及需核对的来源，不能签发同意。'
                '概况标记unknown、values_truncated或fields_complete=false时，不得将未展示内容当作不存在。',
            },
            {
                'role': 'user',
                'content': rr.material(self.ws)
                + '\n'
                + data_dictionary(self.ws.datasets)
                + '\n结构（不含生产公式）：'
                + json.dumps(structure, ensure_ascii=False)
                + '\n表达式语法：'
                + self._dsl(),
            },
        ]
        def model_identity(model, seen=()):
            if model is None or id(model) in seen:
                return None
            return {
                'name': getattr(model, 'name', ''), 'model': getattr(model, 'model', ''),
                # Include request limits: increasing a truncated review's output
                # allowance must not reuse its old response or retry allowance.
                # Only stable configuration belongs here, not discovered provider
                # limits or usage. Store a digest, never credentials or endpoint secrets.
                'configuration': fingerprint({k: getattr(model, k, None) for k in
                                              ('base_url', 'think_body', 'extra_body', 'json_mode',
                                               'think_max_tokens', 'think_timeout', 'timeout',
                                               'think_echo', 'cache', 'max_input_tokens',
                                               'min_request_seconds')}),
                'think_route': model_identity(getattr(model, 'think_llm', None), (*seen, id(model))),
            }

        checker = getattr(self.ws.llm, 'checker_llm', None)
        policy = {'checker': model_identity(checker), 'primary': model_identity(self.ws.llm),
                  'checker_required': bool(getattr(self.ws.llm, 'checker_required', False))}
        signature = fingerprint({'messages': msgs, 'execution_profile': _EXECUTION_PROFILE, 'reviewer_policy': policy})
        prior = self.state.get('references', {}).get(name)
        same_input = bool(prior and prior.get('input_signature') == signature)
        cached = bool(same_input and prior.get('spec_state') in ('valid', 'unresolved'))
        if self.current('review:' + name) and cached and prior['report']['status'] == 'PASS':
            return {'status': 'PASS', 'scheme': name, 'cached': True, 'by': prior['reviewer']['name'],
                    'reviewer': copy.deepcopy(prior['reviewer']), 'next': '等待财务确认总额'}
        # An explicit new review cannot borrow a previous review's PASS when its
        # reviewer or evidence changes. Human approval receipts remain untouched.
        self.state['receipts'].pop('review:' + name, None)
        reviewer = copy.deepcopy(prior.get('reviewer')) if cached else None
        spec, raw_response = None, None

        def remember(report, spec_state='valid', diagnostic=None):
            self.state.setdefault('references', {})[name] = {
                'version': version, 'basis_digest': digest, 'input_signature': signature,
                'spec': copy.deepcopy(spec), 'spec_state': spec_state, 'report': copy.deepcopy(report),
                'reviewer': copy.deepcopy(reviewer), 'diagnostic': diagnostic, 'raw_response': raw_response,
            }

        def invalid(message, *, unresolved=False):
            report = {'status': 'FAIL', 'issues': [message], 'basis_digest': digest,
                      'cached': cached, 'checked_objects': 0, 'by': (reviewer or {}).get('name', ''),
                      'reviewer': copy.deepcopy(reviewer)}
            remember(report, 'unresolved' if unresolved else 'invalid', message)
            self.state['last_review'] = report
            fail(message)

        def checked(fn, *args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (ToolError, EvalError, EngineError, ValueError) as exc:
                invalid('独立复核声明未通过程序检查：' + str(exc))

        if cached:
            spec = copy.deepcopy(prior['spec'])
        else:
            key = f'{name}:{signature}'
            attempts = self.state['attempts'].get(key, 0)
            if attempts >= 2:
                fail('同份独立复核输入已达预算，须补充证据或修复程序；修改主公式不能重置顾问预算')
            request = copy.deepcopy(msgs)
            if same_input and prior.get('spec_state') == 'invalid':
                request += [
                    {'role': 'assistant', 'content': prior.get('raw_response') or
                     json.dumps(prior.get('spec'), ensure_ascii=False)},
                    {'role': 'user', 'content': '独立声明的程序诊断：' + str(prior['diagnostic']) +
                     '\n请仅根据原材料修复上述声明，重新输出完整JSON。没有增加调用次数上限；'
                     '这里没有提供生产公式、生产金额或参考答案。业务依据不足仍须写unclear。'},
                ]
            request_model = rr.guard_request(self.ws, request)
            self.state['attempts'][key] = attempts + 1
            try:
                raw_response = rr._ask(self.ws, request)
            except RunBudgetError as exc:
                call = getattr(request_model, 'last_call', {})
                reserve_block = (call.get('status') == 'budget_blocked' and call.get('request_attempts') == []
                                 and not call.get('budget_request_sequence') and not call.get('response_received'))
                if getattr(exc, 'request_not_sent', False) or reserve_block:
                    # The deadline can cross its minimum between guard and reserve.
                    # No reviewer opinion was requested; preserve this attempt.
                    self.state['attempts'][key] = attempts
                raise
            actual = checker if checker is not None and not self.ws.checker_down else self.ws.llm
            think = actual is checker or not bool(self.ws.checker_down and getattr(self.ws.llm, 'think_llm', None))
            seen = set()
            while think and getattr(actual, 'think_llm', None) is not None and id(actual) not in seen:
                seen.add(id(actual))
                actual = actual.think_llm
            reviewer = model_identity(actual)
            reviewer['fallback_reason'] = self.ws.checker_down
            try:
                spec = parse_json(raw_response)
            except (LLMError, ValueError) as exc:
                invalid('独立复核输出无法解析为JSON：' + str(exc))
        if not isinstance(spec, dict) or not isinstance(spec.get('pool'), str) or not spec['pool'].strip():
            invalid('独立复核须输出包含非空pool表达式的JSON对象')
        if spec.get('unclear'):
            invalid('独立复核存在未明确口径，补充原文依据前保持阻断：' + str(spec['unclear']), unresolved=True)
        for field in ('helpers', 'tables', 'details', 'totals'):
            if field in spec and not isinstance(spec[field], dict):
                invalid(f'独立复核的{field}须为JSON对象')
        _, root, by_level, ev = checked(self.ws._independent_env, name, spec, basis=True)
        population = check_populations(self.ws, rs, spec, root, by_level)
        if not population['contract_valid']:
            invalid('独立核算范围声明无效：' + '；'.join(population['issues'][:3]))
        if population['status'] != 'PASS':
            report = {'status': 'FAIL', 'issues': population['issues'], 'population': population,
                      'basis_digest': digest, 'cached': cached, 'checked_objects': 0,
                      'by': reviewer['name'], 'reviewer': copy.deepcopy(reviewer)}
            remember(report)
            self.state['last_review'] = report
            return report
        targets = ([root] if rs.pool.scope == 'global' else
                   by_level.get(rs.pool.scope) or list((root.index or {}).get(rs.pool.scope, {}).values()))
        if not targets:
            fail('独立奖金池没有核算对象：' + rs.pool.scope)
        pools = checked(self.ws._independent_eval, ev, spec['pool'], targets, '预核算奖金池')
        actual = {k: Decimal(v['amount']) for k, v in basis['objects'].items()}
        issues = []
        reference_pools = {k: round_pool(v) for k, v in pools.items()}
        examples = []

        def differences(label, actual, reference):
            for key in sorted(set(actual) | set(reference)):
                if actual.get(key) != reference.get(key):
                    examples.append(
                        {
                            'item': label,
                            'object': key,
                            'actual': str(actual.get(key)),
                            'independent': str(reference.get(key)),
                        }
                    )

        if reference_pools != actual:
            issues.append('逐对象奖金池或对象集合不一致')
            differences('奖金池', actual, reference_pools)
        expected_total = round_pool(
            sum((v if rs.pool.scope == 'global' else round_pool(v) for v in pools.values()), Decimal(0))
        )
        if expected_total != Decimal(basis['pool']):
            issues.append('奖金池汇总不一致')
        details = checked(self.ws._independent_details, name, spec, basis=True)
        wanted = {
            json.dumps([report, nid], ensure_ascii=False): Decimal(v['amount'])
            for report, rows in basis['details'].items()
            for nid, v in rows.items()
        }
        if details != wanted:
            issues.append('逐对象明细金额或覆盖集合不一致')
            differences('对象明细', wanted, details)
        totals = {}
        for item in rs.report.totals if rs.report else []:
            expr = (spec.get('totals') or {}).get(item.name)
            if not isinstance(expr, str) or not expr:
                invalid(f'缺少汇总项 {item.name} 的独立公式')
            nodes = (
                [root]
                if item.level == 'global'
                else by_level.get(item.level) or list((root.index or {}).get(item.level, {}).values())
            )
            if not nodes:
                fail('独立汇总没有核算对象')
            totals[item.name] = round_pool(
                sum(checked(self.ws._independent_eval, ev, expr, nodes, '预核算汇总' + item.name).values(), Decimal(0))
            )
        actual_totals = {k: Decimal(v) for k, v in basis['totals'].items()}
        if totals != actual_totals:
            issues.append('各类汇总金额不一致')
            differences('汇总', actual_totals, totals)
        checked(self.ws._independent_holes, ev.rs, ev)
        report = {
            'status': 'FAIL' if issues else 'PASS',
            'issues': issues,
            'examples': examples[:8],
            'basis_digest': digest,
            'cached': cached,
            'checked_objects': len(actual) + len(wanted),
            'population': population,
            'by': reviewer['name'],
            'reviewer': copy.deepcopy(reviewer),
        }
        remember(report)
        if not issues:
            self.receipt('review:' + name, report, kind='basis')
            self.state['waiting'] = name
            self.ws.done = True
        else:
            self.state['last_review'] = report
        return report

    @staticmethod
    def _dsl():
        from bonus_agent.auto import prompt
        from bonus_core.dsl.functions import describe

        text = prompt('extract_rules')
        return text[text.index('## 表达式语法') : text.index('## 发放与抵扣')].replace('{functions}', describe())

    def approve(self, name, by, digest):
        if not by.strip():
            fail('确认人不能为空')
        self.require('review:' + name)
        if self.ws.pending:
            fail('存在待答复的业务问题，不能确认')
        prior = copy.deepcopy(self.state['basis'][name])
        if fingerprint(prior) != digest:
            fail('确认的总额版本已变化，请刷新核对')
        self.calculate_basis(name)  # Recompute from immutable source inputs, not a browser payload.
        if self.state['basis'][name] != prior:
            fail('重新计算的金额或对象范围与待确认版本不一致')
        self.receipt('approved:' + name, {'by': by, 'basis_digest': digest}, kind='basis')
        self.state.pop('waiting', None)
        return {'status': 'PASS', 'scheme': name, 'by': by, 'digest': digest}

    def guard_allocation(self, name):
        self.require('data')
        self.require('formulas')
        self.require('basis:' + name)
        self.require('review:' + name)
        self.require('approved:' + name)
        if not self.current('allocation_formulas'):
            report = self.compile('all')
            if report['status'] != 'PASS':
                fail('人员分配公式尚未通过完整检查：' + '；'.join(report['issues']))
        digest = fingerprint(self.state['basis'][name])
        if any(
            self.state['receipts'][k + name]['payload'].get('basis_digest') != digest for k in ('review:', 'approved:')
        ):
            fail('金额基线与复核或确认回执不一致')

    def recheck(self, *, initial_compile=False):
        """改动让已通过的“准备数据 / 编译公式”回执失效时，程序自己重跑这两项确定性检查。
        实测模型补一条分配层级或改一处规则就退回前一阶段、清空对话，再把整套检查和读取重做一遍，白耗步数和 tokens。
        initial_compile还允许基础任务首次全部通过后自动编译；不执行付费复核或金额确认。
        每个输入版本只自动执行一次；返回 [(检查名, 是否通过, 未通过的问题)]。"""
        done = []
        for _ in range(2):
            stage, _scheme = self.stage()
            if stage == 2 and 'data' in self.state['receipts']:
                name, kind, phase, run = 'prepare_data', 'data', None, self.prepare_data
            elif stage == 3:
                from bonus_agent.construction import TaskBoard
                phase = 'basis' if not self.current('formulas') else 'all'
                passed_before = 'formulas' in self.state['receipts'] or 'allocation_formulas' in self.state['receipts']
                if (phase == 'all' and initial_compile) or (not passed_before and not initial_compile):
                    break
                # Do not manufacture local PASS receipts for unfinished work. Existing
                # stale receipts may be refreshed by the board's normal deterministic gate.
                if TaskBoard(self.ws).gate(phase):
                    break
                name, kind = 'compile_formulas', 'basis' if phase == 'basis' else 'formula'
                run = lambda: self.compile(phase)
            else:
                break
            version = self.version(kind)
            if name == 'compile_formulas':
                feedback = self.compile_feedback(phase, version=version)
                if feedback and feedback['status'] == 'FAIL':
                    break
            key = name + ':' + (phase or kind)
            prior = self.state.get('auto_checks', {}).get(key, {})
            if name != 'compile_formulas' and prior.get('version') == version:
                break
            from bonus_agent.agent import ToolError
            try:
                report = run()
                ok, issues = report.get('status') == 'PASS', report.get('issues') or []
            except ToolError as exc:
                ok, issues = False, [str(exc)]
                report = {'status': 'FAIL', 'issues': issues, 'phase': phase}
            # Parameter binding repair can change inputs during compilation. Record the
            # resulting version so that the repair itself cannot create an automatic loop.
            self.state.setdefault('auto_checks', {})[key] = {
                'version': self.version(kind), 'kind': kind, 'phase': phase,
                'report': copy.deepcopy(report),
            }
            done.append((name, ok, issues))
            if not ok:
                break
        return done

    def stage(self):
        if not self.has_plan():
            return 0, None
        if self.rules_ready():
            return 1, None
        if not self.current('data'):
            return 2, None
        if not self.current('formulas'):
            return 3, None
        for s in self.ws._order(self.ws.schemes):
            if not self.current('basis:' + s):
                return 4, s
            if not self.current('review:' + s):
                return 5, s
            if not self.current('approved:' + s):
                return 6, s
            if not self.current('allocation_formulas'):
                return 3, s
            if s not in self.ws.results or s in self.ws.untested:
                return 7, s
        return 8, None

    def hint(self):
        stage, scheme = self.stage()
        if stage == 3:
            phase = 'basis' if not self.current('formulas') else 'all'
            report = self.compile_feedback(phase)
            if report and report['status'] == 'FAIL':
                summary = self.compile_summary(report)
                return ('当前版本已编译未通过，先修复反例，不要原样重编译：'
                        + '；'.join(str(i)[:450] for i in summary['issues'][:3])
                        + '；完整检查：read_workspace(section="checks")')
            from bonus_agent.construction import TaskBoard
            return TaskBoard(self.ws).summary()['next']
        if stage == 1:
            missing = [item for item in self.rule_requirements() if item['missing']]
            if missing:
                return '只补缺失项，不重读已登记的整份参数表：' + '；'.join(
                    f"《{item['file']}》已有{item['facts']}个参数、{item['clauses']}条条款，须用"
                    + '/'.join('record_facts' if part == 'facts' else 'record_clauses' for part in item['missing'])
                    + '补齐原文登记'
                    for item in missing[:3]
                )
            gaps = self.ws._page_gaps(self.skipped())
            return '只核对并登记尚未覆盖的原文页：' + '；'.join(gaps[:3])
        last = self.state.get('data_report', {})
        if (
            stage == 2
            and last.get('version') == self.version('data')
            and last.get('report', {}).get('status') == 'FAIL'
        ):
            return (
                '先按数据反例修正字段、期间、完整编号或有效对象范围，再prepare_data；不要原样重试：'
                + str(last['report']['issues'][:2])[:650]
            )
        return STAGES[stage][1] + (f'（方案：{scheme}）' if scheme else '')

    def summary(self):
        from bonus_agent.construction import TaskBoard
        index, scheme = self.stage()
        construction = TaskBoard(self.ws).summary() if index == 3 else None
        next_action = self.hint()
        if construction is not None:
            phase = 'basis' if not self.current('formulas') else 'all'
            report = self.compile_feedback(phase)
            if report and report['status'] == 'FAIL':
                # Keep the nested task card and the workflow next action consistent.
                construction = {**construction, 'next': next_action, 'compile_failure': self.compile_summary(report)}
        return {
            'stage': index,
            'name': STAGES[index][0],
            'scheme': scheme,
            'next': next_action,
            'construction': construction,
            'rule_requirements': self.rule_requirements() if index == 1 else [],
            'plan': self.state.get('plan'),
            'receipts': {n: {'current': self.current(n), 'at': r['at']} for n, r in self.state['receipts'].items()},
        }

    def allowed(self, tool, stage=None):
        if stage is None:
            stage, _ = self.stage()
        minima = {
            'prepare_data': 1,
            'compile_formulas': 3,
            'calculate_basis': 4,
            'review_basis': 5,
            'trial_run': 7,
            'evaluate': 4,
            'explain': 7,
            'review_calculation': 8,
            'verify': 8,
            'preview_repair': 8,
            'verify_repair': 8,
            'apply_repair': 8,
        }
        if tool == 'finish':
            return stage == 8 or bool(self.ws.pending)
        if tool == 'trial_run' and stage == 3 and self.current('formulas'):
            # The normal entry point may compile already-built distribution rules on resume.
            # guard_allocation still checks the requested scheme's exact approval and full rules.
            return any(self.current('approved:' + s) for s in self.ws.schemes)
        return stage >= minima.get(tool, 0)
