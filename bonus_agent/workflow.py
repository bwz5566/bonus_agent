"""Program-owned receipts for staged calculation. No model can approve a total."""

from __future__ import annotations

import copy
import json
import hashlib
import re
from pathlib import Path
from datetime import datetime
from decimal import Decimal
from graphlib import TopologicalSorter

from bonus_agent.recovery import fingerprint
from bonus_core.engine.allocation import round_pool
from bonus_core.dsl.phases import basis_rules, funding_only

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
               ('lint.py', 'facts.py', 'clauses.py', 'source_cells.py', 'source_coverage.py', 'source_diagnostics.py',
                'scope_diagnostics.py', 'indicator_scope.py', 'indicator_confidence.py', 'indicator_history.py',
                'indicator_budget.py', 'indicator_coefficients.py', 'indicator_expressions.py', 'indicator_snapshot.py', 'question_review.py',
                'reread.py', 'coefficient.py', 'agent.py', 'auto.py', 'case.py', 'context.py', 'advice_context.py', 'recovery.py',
                'construction.py', 'semantics.py', 'progress.py', 'rule_package.py', 'parameter_repair.py', 'contracts.py', 'formula_inputs.py',
                 'data_profile.py', 'dataset_projection.py', 'review_population.py', 'basis_review_parts.py', 'settlement_bindings.py',
                 'model_repair.py', 'supervision.py', 'table_recovery.py', 'tier_plan.py', 'amount_tiers.py', 'funding_plan.py', 'funding_components.py'))
).hexdigest()


def fail(message):
    from bonus_agent.agent import ToolError

    raise ToolError('workflow.gate：' + message)


def _review_model_identity(model, seen=()):
    if model is None or id(model) in seen:
        return None
    fields = ('base_url', 'think_body', 'extra_body', 'json_mode', 'think_max_tokens',
              'think_timeout', 'timeout', 'think_echo', 'cache', 'max_input_tokens', 'min_request_seconds')
    return {
        'name': getattr(model, 'name', ''), 'model': getattr(model, 'model', ''),
        'configuration': fingerprint({k: getattr(model, k, None) for k in fields}),
        'think_route': _review_model_identity(getattr(model, 'think_llm', None), (*seen, id(model))),
    }


class Workflow:
    def __init__(self, ws):
        self.ws = ws
        self.state = ws.workflow
        self.state.setdefault('receipts', {})
        self.state.setdefault('basis', {})
        self.state.setdefault('attempts', {})

    def basis_view(self, scheme=None):
        """A read-only validation view; deferred allocation evidence stays in the real workspace."""
        from bonus_agent import facts as fx
        from bonus_agent import clauses as cl
        from bonus_agent.construction import TaskBoard
        from bonus_agent.extract_rules import Extraction

        view = copy.copy(self.ws)
        schemes = (scheme,) if scheme else self.ws.schemes
        view._phase_source_drafts = {s: self.ws._merged(s) for s in schemes}
        if scheme:
            view.datasets = self.ws._scheme_datasets(scheme)
            view.mapping = {name: mapping for name, mapping in self.ws.mapping.items() if name in view.datasets}
            view.questions = self.questions_for(scheme)
        view._phase_name = 'basis'
        view._phase_binding_gaps = []
        view._phase_binding_evidence = []
        view.drafts = {s: basis_rules(self.ws._ruleset(s)).model_dump(mode='json', include=set(Extraction.model_fields))
                       for s in schemes}
        for scheme, draft in view.drafts.items():
            for level in draft.get('allocation', []):
                level.pop('weight', None)
            original_variables = self.ws._merged(scheme).get('variables') or {}
            for name, variable in draft.get('variables', {}).items():
                if 'scope' not in (original_variables.get(name) or {}):
                    # An implicit global default is not an explicit instruction to repeat a
                    # whole-company aggregate at each object. Preserve this validation fact.
                    variable.pop('scope', None)
        all_tasks = self.state.get('construction', {}).get('tasks', {})
        tasks = {n: task for n, task in all_tasks.items() if task['scheme'] in schemes}
        foreign_facts = {f for n, task in all_tasks.items() if n not in tasks for f in task['facts']} if scheme else set()
        foreign_clauses = {c for n, task in all_tasks.items() if n not in tasks for c in task['clauses']} if scheme else set()
        board = TaskBoard(self.ws)
        required = board.required('basis') & tasks.keys()
        deferred = board.deferred(required) & tasks.keys()
        deferred_facts = {f for n in deferred for f in tasks[n]['facts']}
        required_facts = {f for n, spec in tasks.items() if n not in deferred for f in spec['facts']}
        deferred_clauses = {c for n in deferred for c in tasks[n]['clauses']}
        required_clauses = {c for n, spec in tasks.items() if n not in deferred for c in spec['clauses']}
        later_sources = [(c.file, fx.scope_norm(c.quote)) for c in self.ws.clauses
                         if c.id in deferred_clauses - required_clauses]
        basis_sources = [(c.file, fx.scope_norm(c.quote)) for c in self.ws.clauses if c.id in required_clauses]

        def projected_targets(target):
            target_scheme, path = fx.split_target(target)
            parts = fx.target_parts(path)
            if not parts or parts[0] in ('allocation', 'payout', 'offsets'):
                return [], []
            if parts[0] == 'report' and len(parts) > 1 and parts[1] not in ('totals', 'details', 'balances'):
                return [], []
            if target_scheme in (None, '共用'):
                candidates = view.drafts
            elif not scheme or target_scheme in view.drafts:
                candidates = {target_scheme: view.drafts.get(target_scheme, {})}
            else:
                candidates = {}
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
                view._phase_binding_evidence.append({'fact': fact.model_dump(mode='json'), 'targets': omissions})
                view._phase_binding_gaps.append(f'{fact.name}：parameter.binding.phase_reference，'
                                               + cl.phase_reference_message('basis', omissions))
            if fact.targets and not targets and (omissions or fact.name not in required_facts):
                continue
            if not fact.targets and fact.name in (deferred_facts | foreign_facts) - required_facts:
                continue
            if not fact.targets and fact.name not in required_facts:
                quoted = fx.scope_norm(fact.quote)
                if (len(quoted) >= 6 and any(doc == fact.file and quoted in q for doc, q in later_sources)
                        and not any(doc == fact.file and quoted in q for doc, q in basis_sources)):
                    continue
            view.facts.append(fact.model_copy(update={'targets': targets}))
        view.clauses = [c for c in self.ws.clauses
                        if c.id not in (deferred_clauses | foreign_clauses) - required_clauses]
        if scheme:
            local_refs = cl.known_refs(view._phase_source_drafts, view.datasets)
            all_refs = cl.known_refs(self.ws.drafts, self.ws.datasets)
            # Shared source clauses may carry executable links for multiple
            # schemes. Only the foreign links are irrelevant to this receipt.
            view.clauses = [c if c.where.startswith((cl.BUILTIN, cl.NOT_APPLY, cl.ASKED)) else c.model_copy(
                update={'where': '、'.join(ref for ref in cl.refs(c.where) if ref in local_refs or ref not in all_refs)})
                            for c in view.clauses]
        return view

    def version(self, kind='formula', *, execution_profile=None, questions=None, basis_scheme=None):
        ws = self.ws
        plan = self.state.get('plan')
        body = {
            'execution_profile': execution_profile or _EXECUTION_PROFILE,
            'files': ws._source_version,
            'period': str(ws.period),
            'datasets': {n: d.model_dump(mode='json') for n, d in ws.datasets.items()},
            'mapping': {n: m.model_dump(mode='json') for n, m in ws.mapping.items()},
            'source_contract': self.state.get('source_contract'),
        }
        if kind == 'data':
            from bonus_agent.source_coverage import row_scope_clauses

            body['row_scope_evidence'] = [(clause.id, clause.file, clause.quote) for _, clause in row_scope_clauses(ws)]
            body['file_roles'] = self.state.get('plan', {}).get('files', {})
            body['structure'] = {
                s: {
                    'patches': ws._merged(s).get('patches'),
                    'allocation': [
                        {k: lv.get(k) for k in ('level', 'key', 'parent_key')}
                        for lv in ws._merged(s).get('allocation') or []
                    ],
                    'datasets': sorted(ws._scheme_datasets(s)),
                }
                for s in ws.schemes
            }
        else:
            if kind == 'basis':
                ws = self.basis_view(basis_scheme)
                from bonus_agent.construction import TaskBoard
                board = TaskBoard(self.ws)
                required = board.required('basis')
                if basis_scheme:
                    required = {n for n in required if board.state['tasks'][n]['scheme'] == basis_scheme}
                    body['datasets'] = {n: d.model_dump(mode='json') for n, d in ws.datasets.items()}
                    body['mapping'] = {n: m.model_dump(mode='json') for n, m in ws.mapping.items()}
                if plan and plan.get('output_targets'):
                    # Distribution expressions are otherwise projected out. Removing a
                    # promised output must invalidate an earlier structure check.
                    obligations = board.output_obligations('basis')
                    if basis_scheme:
                        obligations = [item for item in obligations if item['scheme'] == basis_scheme]
                        plan = self._funding_plan(plan, basis_scheme)
                    body['missing_outputs'] = obligations
                # Evidence omitted from executable rules still invalidates old PASS receipts.
                # Hash source/target identities, never presentation text from diagnostics.
                body['phase_binding_evidence'] = ws._phase_binding_evidence
            body.update(
                rules=ws._rules_print(),
                plan=plan,
                facts=[f.model_dump(mode='json') for f in ws.facts],
                # Displaying a clause side by side marks it as read. Only changes to
                # its source or rule links affect the compiled calculation version.
                clauses=[c.model_dump(mode='json', exclude={'checked'}) for c in ws.clauses],
                answers=([(q.id, q.answer, q.withdrawn) for q in ws.questions] if questions is None else questions),
                said=ws.said,
                transcripts=ws.transcripts,
                construction_plan={n:s for n,s in self.state.get('construction', {}).get('tasks', {}).items()
                                   if kind != 'basis' or n in required},
            )
        return fingerprint(body)

    @staticmethod
    def _funding_plan(plan, scheme):
        """Only promises applicable before this scheme's allocation bind its approval."""
        from bonus_agent.construction import DISTRIBUTION_REPORT_FIELDS
        from bonus_agent.facts import split_target, target_parts

        targets = {}
        for output, pointers in plan['output_targets'].items():
            for pointer in pointers:
                owner, path = split_target(pointer)
                parts = target_parts(path)
                if owner == scheme and len(parts) > 1 and parts[1] not in DISTRIBUTION_REPORT_FIELDS:
                    targets.setdefault(output, []).append(pointer)
        return {**plan, 'schemes': [scheme], 'outputs': [label for label in plan['outputs'] if label in targets],
                'output_targets': targets}

    def _receipt_version(self, name, kind):
        step, _, scheme = name.partition(':')
        if kind != 'basis' or step not in ('basis', 'review', 'approved') or scheme not in self.ws.schemes:
            return self.version(kind)
        version = self.version(kind, basis_scheme=scheme)
        pending = [parent for parent, _ in self.ws._deps(scheme)]
        upstream = {}
        while pending:
            parent = pending.pop()
            if parent in upstream or parent not in self.ws.schemes:
                continue
            approval = self.state['receipts'].get('approved:' + parent, {})
            upstream[parent] = {'rules': self.ws._merged(parent), 'approval': approval}
            pending.extend(dep for dep, _ in self.ws._deps(parent))
        if step == 'approved':
            report = self.ws._ruleset(scheme).report
            if report and any(contract.source_funding for contract in report.distributions):
                version = fingerprint((version, self.ws._merged(scheme)))
        # scheme_paid reads the upstream allocation, payout and adjustment rules.
        # They are deliberately absent from the pre-distribution rules projection.
        return fingerprint((version, upstream)) if upstream else version

    def receipt(self, name, payload=None, *, kind='formula'):
        rec = {
            'version': self._receipt_version(name, kind),
            'kind': kind,
            'at': datetime.now().isoformat(timespec='seconds'),
            'payload': copy.deepcopy(payload or {}),
        }
        self.state['receipts'][name] = rec
        return rec

    def current(self, name):
        rec = self.state['receipts'].get(name)
        return bool(rec and rec['version'] == self._receipt_version(name, rec.get('kind', 'formula')))

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
        required_sources = set(self.state.get('source_contract', {}).get('supplement_sources', []))
        if missing := sorted(name for name in required_sources if files.get(name) not in ('rule', 'rule_and_data')):
            fail('plan.confirmed_sources：本批确认与输出要求是正式依据，须登记为rule：' + '、'.join(missing))
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
        previous = self.state.get('plan', {})
        planned = {
            'summary': summary,
            'outputs': outputs,
            'files': files,
            'schemes': schemes,
            'ignored': ignored,
            'files_version': fingerprint(self.ws._source_version),
        }
        if output_targets is not None:
            planned['output_targets'] = output_targets
            old_schemes = set(previous.get('schemes', []))
            if previous.get('output_targets') and set(schemes) - old_schemes:
                for name in old_schemes.intersection(schemes, self.ws.schemes):
                    if (self._funding_plan(previous, name) == self._funding_plan(planned, name)
                            and self.current('approved:' + name)):
                        membership = self.state.setdefault('scheme_datasets', {})
                        membership.setdefault(name, sorted(self.ws._scheme_datasets(name)))
        self.state['plan'] = planned
        return {'status': 'PASS', 'plan': planned, 'next': self.hint()}

    def rule_requirements(self):
        ws = self.ws
        plan = self.state.get('plan') or {}
        relevant = {n for n, r in plan.get('files', {}).items() if r in ('rule', 'rule_and_data')}
        from bonus_agent.agent import RULE_SUFFIXES

        relevant |= {p.name for p in ws.inv.files if p.suffix.lower() in RULE_SUFFIXES and p.name not in self.skipped()}
        supplements = set(self.state.get('source_contract', {}).get('supplement_sources', []))
        relevant |= supplements
        result = []
        for n in sorted(relevant):
            facts = sum(f.file == n for f in ws.facts)
            clauses = sum(c.file == n for c in ws.clauses)
            result.append(
                {
                    'file': n,
                    'facts': facts,
                    'clauses': clauses,
                    # Confirmation and output documents can contain solely
                    # qualitative requirements. Actual formula constants still
                    # need evidence binding through the normal compiler.
                    'missing': (['facts'] if not facts and n not in supplements else []) + (['clauses'] if not clauses else []),
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
        from bonus_agent.source_coverage import check_sources, coverage_issue, join_coverage, reconcile_row_scopes

        if not self.has_plan():
            fail('材料清单或版本已变化，先完成plan_calculation')
        if not self.ws.datasets:
            fail('尚未定义数据表')
        try:
            scope_repairs, issues = reconcile_row_scopes(self.ws)
            if not issues:
                from bonus_agent.dataset_projection import recover_reference_dimensions
                scope_repairs.extend(recover_reference_dimensions(self.ws))
        except ToolError as exc:
            scope_repairs, issues = [], [str(exc)]
        counts = {}
        prepared_data = {}
        names = self.ws.schemes or [None]
        for name in names:
            if name:
                from bonus_agent.construction import TaskBoard
                rules = self.ws._ruleset(name)
                inputs = TaskBoard(self.ws).standalone_data_inputs(name, rules)
                rs = basis_rules(rules, additional_inputs=inputs)
            else:
                rs = self._data_rules()
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
                data, patched = prepare(rs, data)
            except (ToolError, ValueError) as exc:
                issues.append(f'方案「{name}」数据读入或范围准备未通过：{exc}')
                continue
            issues += [str(i) for i in patched if i.severity == 'blocking']
            issues += [message for _, message in gaps(rs, data, self.ws.mapping)]
            issues += [str(i) for i in build_tree(rs, data)[2] if i.severity == 'blocking']
            counts.update({n: len(t.rows) for n, t in data.items()})
            prepared_data.update(data)
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
        source_coverage = check_sources(self.ws, prepared_data)
        issues.extend(message for item in source_coverage if (message := coverage_issue(item)))
        from bonus_agent.data_profile import historical_source_evidence
        files = {name for name, role in self.state['plan']['files'].items() if role in ('data', 'rule_and_data')}
        history_sources = historical_source_evidence(self.ws, prepared_data, files, self.skipped())
        report = {'status': 'FAIL' if issues else 'PASS', 'counts': counts, 'issues': issues,
                  'diagnostic': diagnostic, 'source_coverage': source_coverage, 'history_sources': history_sources,
                  'join_coverage': list(join_coverage(self.ws, prepared_data)), 'scope_repairs': scope_repairs}
        self.state['data_report'] = {'version': self.version('data'), 'report': copy.deepcopy(report)}
        if not issues:
            self.receipt('data', report, kind='data')
        else:
            self.state['receipts'].pop('data', None)
        return report

    def _data_rules(self):
        from bonus_core.dsl.models import RuleSet, Pool

        return RuleSet(
            name='数据准备', version='workflow', datasets=self.ws.datasets, pool=Pool(expr='0'), allocation=[]
        )

    def query_dataset(self, dataset, expr=None, *, values_of=None, offset=0):
        """Read one mapped source without requiring any unrelated relationship or allocation."""
        from bonus_core.data.mapping import apply_mapping
        from bonus_core.dsl.models import RuleSet, Pool
        from bonus_core.engine.tree import Node, ROOT_LEVEL
        from bonus_core.engine.evaluator import Evaluator
        from bonus_agent.data_profile import query_category_fields, query_field_evidence

        self.ws._check_source_version()
        if dataset not in self.ws.datasets:
            fail('请先定义要统计的数据表')
        definition = self.ws.datasets[dataset]
        if (expr is None) == (values_of is None):
            fail('query_dataset：聚合查询提供expr，逐项查看字段取值提供values_of，每次选择一种')
        if type(offset) is not int or offset < 0 or (offset and values_of is None):
            fail('query_dataset：offset仅用于values_of分页，须为非负整数')
        if values_of is not None and (not isinstance(values_of, str) or values_of not in definition.fields or
                                      definition.fields[values_of].type not in ('text', 'enum', 'bool', 'id')):
            fail('query_dataset：values_of须是该表已映射的类别、文本、布尔或编号字段')
        if definition.population_from:
            fail('该实体范围来自有效明细；请查询population_from明细表，不能把未裁定的原表统计当成有效对象统计')
        ds = definition.model_copy(update={'key': None, 'link': None})
        rs = RuleSet(name='原表统计', version='workflow', datasets={dataset: ds},
                     pool=Pool(expr=expr if expr is not None else '0'), allocation=[])
        data, issues = apply_mapping(rs, self.ws.data_files, {dataset: self.ws.mapping[dataset]}, self.ws.period)
        blocking = [str(i) for i in issues if i.severity == 'blocking']
        if blocking:
            fail('原表统计存在数据问题：' + str(blocking[:8]))
        rows = data[dataset].rows
        result = {'dataset': dataset, 'rows': len(rows),
                  'scope': '仅按该表映射、筛选与期间统计，不证明其他关联或金额通过验收；行数为实体去重前的映射源行'}
        if expr is not None:
            root = Node(ROOT_LEVEL, ROOT_LEVEL, None)
            root.own_rows[dataset] = rows
            ev = Evaluator(rs, root, {}, period=self.ws.period)
            value = ev.evaluate(expr, root, 'source_query', 'source_query')
            result.update(value=str(value), rendered=ev.traces['source_query'].rendered)
            fields = query_category_fields(expr, ds, self.ws.mapping[dataset])
        else:
            fields = [values_of]
        if fields:
            result['field_evidence'] = [query_field_evidence(self.ws, dataset, f, rows, offset) for f in fields[:3]]
            result['remaining_fields'] = fields[3:]
            result['field_scope'] = ('各字段的取值和行数覆盖查询 where 之前的全部映射源行。'
                                     'related_clauses列出原文同时出现列名和取值的条款；业务分类按条款判定。'
                                     '取值完整时直接引用原值，未列全时按next_call翻页。')
        return result

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
        from bonus_agent.agent import ToolError

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
        board = TaskBoard(self.ws)
        construction_issues = board.gate(phase)
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
        preflight_schemes = board.runtime_blocked_schemes(issues, phase) if issues else set(self.ws.schemes)
        if preflight_schemes and phase == 'basis':
            from bonus_core.engine.preflight import preflight_basis
            for name in self.ws._order(self.ws.schemes):
                if name not in preflight_schemes:
                    continue
                refs, missing_refs = self.preflight_references(name)
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
        if not issues and phase == 'all':
            for name in self.ws.schemes:
                try:
                    self.ws._data(self.ws._ruleset(name))
                except ToolError as exc:
                    issues.append(f'方案「{name}」分配取数未通过：{exc}')
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

    def preflight_references(self, name):
        """Resolve approved references without allocating any amounts to people."""
        refs, missing = {}, []
        for dependency, revision in self.ws._deps(name):
            key = f'{dependency}/{revision}'
            if (self.current('approved:' + dependency) and dependency not in self.ws.untested
                    and revision in self.ws.results.get(dependency, {})):
                refs[key] = self.ws.results[dependency][revision]
            elif revision == 'R0' and (result := self._approved_funding_reference(dependency)) is not None:
                refs[key] = result
            else:
                missing.append(key)
        return refs, missing

    def _approved_funding_reference(self, name):
        from bonus_core.engine.run import compute

        if name not in self.ws.schemes or self.ws._deps(name):
            return None
        if not all(self.current(kind + ':' + name) for kind in ('basis', 'review', 'approved')):
            return None
        rs = self.ws._ruleset(name)
        if not funding_only(rs):
            return None
        self.ws._check_source_version()
        version = self.state['receipts']['approved:' + name]['version']
        cache = vars(self.ws).setdefault('_approved_funding_cache', {})
        hit = cache.get(name)
        if hit and hit[0] == version:
            return hit[1]
        data, _ = self.ws._data(rs)
        result = compute(rs, data, period=self.ws.period)
        self.check_basis_result(name, rs, result)
        cache[name] = (version, result)
        return result

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
        rs = basis_rules(self.ws._ruleset(name))
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

    @staticmethod
    def compare_pool(basis, pools, scope):
        actual = {key: Decimal(row['amount']) for key, row in basis['objects'].items()}
        reference = {key: round_pool(value) for key, value in pools.items()}
        issues, examples = [], []
        if reference != actual:
            issues.append('逐对象奖金池或对象集合不一致')
            examples.extend({'item': '奖金池', 'object': key, 'actual': str(actual.get(key)),
                             'independent': str(reference.get(key))}
                            for key in sorted(set(actual) | set(reference)) if actual.get(key) != reference.get(key))
        total = sum((pools if scope == 'global' else reference).values(), Decimal(0))
        if round_pool(total) != Decimal(basis['pool']):
            issues.append('奖金池汇总不一致')
        return {'issues': issues, 'examples': examples[:8], 'checked_objects': len(actual)}

    def reviewer_policy(self):
        primary = self.ws.llm
        return {'checker': _review_model_identity(getattr(primary, 'checker_llm', None)),
                'primary': _review_model_identity(primary),
                'checker_required': bool(getattr(primary, 'checker_required', False))}

    def evidence_wait(self):
        record = self.state.get('review_error', {})
        if record.get('question'):
            return record if any(
                q.id == record['question'] and self._settlement_question_scope(q)
                for q in self.ws._unanswered_questions()
            ) else None
        if record.get('kind') != 'evidence' or record.get('reviewer_policy') != self.reviewer_policy():
            return None
        if record.get('version') == self.version():
            return record
        # A code upgrade invalidates arithmetic receipts. An identical pending
        # question grounded in the same source cells can still be displayed.
        ref = self.state.get('references', {}).get(record.get('scheme'), {})
        profile = record.get('execution_profile') or ref.get('execution_profile')
        if (ref.get('scope_questions') and self.ws._unanswered_questions() and profile
                and profile != _EXECUTION_PROFILE and record.get('version') == self.version(execution_profile=profile)):
            return record
        return None

    def unresolved_review(self):
        stage, scheme = self.stage()
        if stage == 5:
            ref = self.state.get('references', {}).get(scheme, {})
            if (ref.get('spec_state') == 'unresolved' and ref.get('version') == self.version('basis')
                    and ref.get('basis_digest') == fingerprint(self.state['basis'].get(scheme))
                    and ref.get('reviewer_policy') == self.reviewer_policy()):
                return scheme, ref
        return None

    def _settlement_parent(self, scheme):
        if scheme not in self.ws.schemes:
            return None
        rs = self.ws._ruleset(scheme)
        contracts = [item.source_funding for item in rs.report.distributions if item.source_funding] if rs.report else []
        if len(contracts) != 1 or rs.payout or rs.offsets or self.ws._merged(scheme).get('revisions'):
            return None
        parent = contracts[0].scheme
        if (parent in self.ws.schemes and self.ws._deps(scheme) == {(parent, 'R0')}
                and not self.ws._deps(parent) and funding_only(self.ws._ruleset(parent))):
            return parent
        return None

    @staticmethod
    def _question_identity(question):
        return fingerprint((question.id, question.text, question.options, question.related, question.evidence))

    def _settlement_question_scope(self, question):
        scope = self.state.get('settlement_questions', {}).get(question.id, {})
        parent = scope.get('parent')
        if (parent and scope.get('question') == self._question_identity(question)
                and scope.get('files') == self.ws._source_version and scope.get('period') == str(self.ws.period)
                and scope.get('basis_digest') == fingerprint(self.state['basis'].get(parent))
                and scope.get('approval_scope') == self.state.get('confirmed_basis', {}).get(parent, {}).get('scope')
                and self._settlement_parent(scope.get('scheme')) == parent):
            return scope
        return None

    def questions_for(self, name):
        """Only source-checked questions attached after this exact funding approval are local to settlement."""
        excluded = {q.id for q in self.ws.questions
                    if self.state.get('settlement_questions', {}).get(q.id, {}).get('parent') == name
                    and self._settlement_question_scope(q)}
        return [q for q in self.ws.questions if q.id not in excluded] if excluded else self.ws.questions

    def withdraw_stale_settlement_answers(self, answers):
        """Retire questions about a different physical input version or period."""
        records = {**self.state.get('applicability_questions', {}), **self.state.get('settlement_questions', {})}
        stale = {}
        for q in self.ws.questions:
            scope = records.get(q.id, {})
            if scope.get('question') != self._question_identity(q):
                continue
            if scope.get('files') != self.ws._source_version or scope.get('period') != str(self.ws.period):
                q.withdrawn = '材料或核算期间已更新，请按当前材料重新核对口径'
                stale[q.id] = q.text
        if not stale or not answers:
            return answers
        stale_texts = set(stale.values())
        return [answer for answer in answers if answer.get('id') not in stale and answer.get('text') not in stale_texts]

    def record_settlement_question(self, scheme, text, options, evidence):
        """Persist a verified review dispute about the distribution of already approved funds."""
        from bonus_agent.agent import _next_qid
        from bonus_agent.extract_rules import Question

        self.ws._check_source_version()
        if not text.strip():
            fail('人员分配待确认问题必须说明具体口径')
        for q in self.ws._unanswered_questions():
            if (scope := self._settlement_question_scope(q)) and scope['scheme'] == scheme and q.text == text:
                return q.id
        parent = self._settlement_parent(scheme)
        blocked = self.unresolved_review()
        approval = self.state.get('confirmed_basis', {}).get(parent, {})
        if not parent or not approval.get('scope') or not self.current('approved:' + parent) or not blocked or blocked[0] != scheme:
            fail('人员分配问题须对应已确认逐对象资金及当前独立复核疑义')
        verified, errors = self.ws._evidence(evidence)
        if errors or not verified or any(item['file'] not in self.ws._source_version for item in verified):
            fail('人员分配问题须引用可逐字核对的本批材料')
        question = Question(id=_next_qid(self.ws.questions), text=text, options=options, round=self.ws.round,
                            related=f'{scheme} / 人员分配规则', evidence=verified)
        self.ws.questions.append(question)
        self.state.setdefault('settlement_questions', {})[question.id] = {
            'scheme': scheme, 'parent': parent, 'question': self._question_identity(question),
            'files': dict(self.ws._source_version), 'period': str(self.ws.period),
            'basis_digest': fingerprint(self.state['basis'][parent]), 'approval_scope': approval['scope'],
        }
        self.state['review_error'] = {'kind': 'evidence', 'scheme': scheme, 'question': question.id, 'error': text}
        self.ws.done = True
        return question.id

    def record_applicability_questions(self, scheme, review):
        """Expose source-checked, batch-relevant business reservations for a finance reply."""
        from bonus_agent.agent import _next_qid
        from bonus_agent.extract_rules import Question

        if review.get('spec_state') != 'unresolved' or review.get('reviewer_policy') != self.reviewer_policy():
            return
        issues = (review.get('spec') or {}).get('unclear')
        records = self.state.get('basis_review_parts', {}).get(scheme, {})
        if (not isinstance(issues, list) or not issues or not all(isinstance(x, str) and x.strip() for x in issues)
                or not any(row.get('section') in ('pool_applicability', 'pool_finance_recheck') and row.get('state') == 'unresolved'
                           and row.get('value', {}).get('unclear') == issues for row in records.values())):
            return
        profile = review.get('execution_profile')
        if (not profile or review.get('version') != self.version('basis', execution_profile=profile)
                or review.get('basis_digest') != fingerprint(self.state.get('basis', {}).get(scheme))):
            return
        related = f'{scheme} / 独立金额适用性复核'
        seen = {q.text for q in self.ws.questions if q.related == related and not q.withdrawn}
        scopes = self.state.setdefault('applicability_questions', {})
        for issue in issues:
            if issue in seen:
                continue
            q = Question(id=_next_qid(self.ws.questions), text=issue, round=self.ws.round, related=related)
            self.ws.questions.append(q)
            scopes[q.id] = {'scheme': scheme, 'question': self._question_identity(q),
                            'files': dict(self.ws._source_version), 'period': str(self.ws.period)}
            seen.add(issue)

    def record_inactive_applicability(self, scheme, issues, effects, candidate_digest):
        scopes = self.state.get('applicability_questions', {})
        version = self.version('basis')
        for q in self.ws.questions:
            scope = scopes.get(q.id, {})
            if (q.answer or q.withdrawn or scope.get('scheme') != scheme or scope.get('files') != self.ws._source_version
                    or scope.get('period') != str(self.ws.period) or scope.get('question') != self._question_identity(q)):
                continue
            for ident, text in issues.items():
                if q.text == text:
                    self.state.setdefault('batch_question_effects', {})[q.id] = {
                        'version': version, 'question': scope['question'], 'scheme': scheme,
                        'candidate_digest': candidate_digest, 'evidence': copy.deepcopy(effects[ident])}
                    q.diagnosis['本批影响'] = effects[ident]['reason']

    def record_independent_settlement_question(self, scheme, review):
        """Admit an independent reviewer proposal only when its literal sources verify."""
        from bonus_agent.agent import ToolError

        spec = review.get('spec')
        proposal = spec.get('settlement_question') if isinstance(spec, dict) and spec.get('unclear') else None
        if not isinstance(proposal, dict) or set(proposal) != {'text', 'options', 'sources'}:
            return None
        text, options, sources = (proposal[k] for k in ('text', 'options', 'sources'))
        valid_text = lambda value, cap: isinstance(value, str) and 0 < len(value.strip()) <= cap
        if (not valid_text(text, 1200) or not isinstance(options, list) or len(options) > 4
                or not all(valid_text(choice, 240) for choice in options)
                or not isinstance(sources, list) or not 1 <= len(sources) <= 4):
            return None
        roles = self.state.get('plan', {}).get('files', {})
        evidence = []
        for source in sources:
            if (not isinstance(source, dict) or set(source) != {'doc', 'quote'}
                    or not valid_text(source['doc'], 255) or not valid_text(source['quote'], 300)
                    or source['doc'] not in self.ws._source_version):
                return None
            evidence.append({'file': source['doc'], 'quote': source['quote']})
        if not any(roles.get(row['file']) in ('rule', 'rule_and_data') for row in evidence):
            return None
        try:
            return self.record_settlement_question(scheme, text.strip(), list(dict.fromkeys(c.strip() for c in options)), evidence)
        except ToolError:
            # The saved diagnostic still blocks release when a proposal cannot be admitted.
            return None

    def record_material_scope_review(self, scheme, label, question_id, declaration, decision_id):
        """Attach an independently reviewed source selection to an existing blank-row question."""
        from bonus_agent.question_review import retain_unaffected_withdrawals
        from bonus_agent.review_population import BLANK_SCOPE_OPTIONS, check_populations, scope_material_inputs, verified_blank_scope_rows

        self.ws._check_source_version()
        waiting = self.evidence_wait()
        ref = self.state.get('references', {}).get(scheme, {})
        rows = ref.get('scope_questions', {}).get(label)
        question = next((q for q in self.ws._unanswered_questions() if q.id == question_id), None)
        if (not waiting or waiting.get('scheme') != scheme or not rows or not question
                or question.related != f'{scheme} / {label}'
                or tuple(question.options) != BLANK_SCOPE_OPTIONS or not isinstance(decision_id, str) or not decision_id.strip()):
            fail('材料范围复核须对应当前案卷中同一条待确认问题')
        if not verified_blank_scope_rows(self.ws, question, rows):
            fail('待确认空白行的原表单元格已变化，须重做范围复核')
        rs = basis_rules(self.ws._ruleset(scheme))
        _, root, levels, _ = self.ws._independent_env(scheme, {}, basis=True)
        scope = check_populations(self.ws, rs, {'populations': {label: declaration}}, root, levels, scope_names=[label])
        if scope['status'] != 'PASS':
            fail('材料声明与本期取数范围不一致：' + '；'.join(scope['issues']))
        source = next((r for r in declaration['sources'] if r['doc'] in self.ws._source_version), None)
        if source is None:
            fail('材料范围声明须引用本批原始文件')
        answer = '按本期原文和独立复核的取数范围排除所列行'
        self.ws.answer_question(question.id, answer, file=source['doc'], quote=source['quote'])
        self.state.setdefault('material_scope_reviews', {})[question.id] = {
            'related': question.related, 'text': question.text, 'answer': question.answer, 'by': question.answered_by,
            'files': dict(self.ws._source_version), 'period': str(self.ws.period),
            'dataset_input': scope_material_inputs(self.ws, declaration['dataset']),
            'declaration': copy.deepcopy(declaration), 'decision_id': decision_id,
        }
        retain_unaffected_withdrawals(self.ws, [question])
        return scope

    def _review_workspace(self, name, rs):
        """Keep an explicitly scoped scheme's review input independent of sibling mappings."""
        scoped = name in self.state.get('scheme_datasets', {})
        questions = self.questions_for(name)
        if not scoped and questions is self.ws.questions:
            return self.ws
        view = copy.copy(self.ws)
        view.questions = questions
        if scoped:
            view.datasets = rs.datasets
            view.mapping = {name: mapping for name, mapping in self.ws.mapping.items() if name in view.datasets}
            view.drafts = {name: self.ws._merged(name)}
        return view

    def _saved_scope_review(self, name, prior, rs, structure, split_input):
        from bonus_agent import reread
        from bonus_agent.basis_review_parts import BasisReviewParts
        from bonus_agent.extract_rules import data_dictionary
        from bonus_agent.review_population import confirmed_blank_scope_rows

        if not prior:
            return None, {}
        if prior.get('spec_state') != 'unresolved' or not prior.get('scope_questions'):
            prior = prior.get('scope_origin', {})
        if not prior.get('scope_questions'):
            return None, {}
        choices = {q.id: (q, rows) for q in self.ws.questions
                   if (rows := confirmed_blank_scope_rows(self.ws, q, scheme=name))}
        profile = prior.get('execution_profile')
        if not choices or not profile:
            return None, {}
        with_pending = [(q.id, None if q.id in choices else q.answer, q.withdrawn) for q in self.ws.questions]
        before_questions = [row for row in with_pending if row[0] not in choices]
        if not any(self.version('basis', execution_profile=profile, questions=previous) == prior['version']
                   for previous in (before_questions, with_pending)):
            return None, {}
        view = self._review_workspace(name, rs)
        evidence = reread.material(view, omit_answers=choices) + '\n' + data_dictionary(rs.datasets)
        visible = BasisReviewParts.visible_structure(self, name, structure, evidence, workspace=view)
        baseline = fingerprint({**split_input, 'evidence': evidence, 'structure': visible, 'execution_profile': profile})
        checker = self.ws.llm.checker_llm
        request_signature = fingerprint((baseline, 'basis_review_parts:1', getattr(checker, 'basis_review_reasoning_effort', None)))
        if (baseline, request_signature) != (prior.get('source_signature'), prior.get('input_signature')):
            return None, {}
        replay = BasisReviewParts(self, name, rs, evidence, visible, request_signature)
        return replay, {q.related.removeprefix(f'{name} / '): (q, rows) for q, rows in choices.values()}

    def _saved_settlement_scope(self, name, prior, rs, structure, split_input, workspace):
        """Recover only an exact old population request, with the scoped answer removed."""
        from bonus_agent import reread
        from bonus_agent.basis_review_parts import BasisReviewParts
        from bonus_agent.extract_rules import data_dictionary

        if not prior or prior.get('spec_state') != 'unresolved' or not prior.get('execution_profile'):
            return None
        if not (prior.get('reviewer') or {}).get('basis_review_split') or prior.get('reviewer_policy') != split_input['reviewer_policy']:
            return None
        answered = set()
        for question in workspace.questions:
            if question.withdrawn or question.answer in (None, ''):
                continue
            scope = self._settlement_question_scope(question)
            if scope and scope['scheme'] == name and self.current('approved:' + scope['parent']):
                answered.add(question.id)
        if not answered:
            return None
        before = copy.copy(workspace)
        before.questions = [question for question in workspace.questions if question.id not in answered]
        before_evidence = reread.material(before) + '\n' + data_dictionary(rs.datasets)
        before_structure = BasisReviewParts.visible_structure(self, name, structure, before_evidence, workspace=before)
        source = fingerprint({**split_input, 'evidence': before_evidence, 'structure': before_structure,
                              'execution_profile': prior['execution_profile']})
        effort = getattr(self.ws.llm.checker_llm, 'basis_review_reasoning_effort', None)
        signature = fingerprint((source, 'basis_review_parts:1', effort))
        if (prior.get('source_signature'), prior.get('input_signature')) != (source, signature):
            return None
        return BasisReviewParts(self, name, rs, before_evidence, before_structure, signature)

    def review_basis(self, scheme=None, *, cached_only=False):
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
        rs = basis_rules(self.ws._ruleset(name))
        from bonus_agent.extract_rules import data_dictionary
        from bonus_agent.review_population import blank_scope_witnesses, requirements, check_populations

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
                for n, d in rs.datasets.items()
            },
            'data_checks': self.state['receipts']['data']['payload'],
            'allocation': [{'level': lv.level, 'key': lv.key, 'parent_key': lv.parent_key} for lv in rs.allocation],
            'columns': {n: self.ws.mapping[n].columns for n in rs.datasets},
            'source_mapping': {n: m.model_dump(mode='json', include={'file', 'sheet', 'header_rows', 'combine', 'unpivot'})
                               for n, m in self.ws.mapping.items() if n in rs.datasets},
            'details': [(d.name, d.level, d.desc) for d in (rs.report.details if rs.report else [])],
            'totals': [(t.name, t.level, t.desc) for t in (rs.report.totals if rs.report else [])],
            'population_requirements': requirements(rs),
        }
        from bonus_agent.context import guard_messages

        review_workspace = self._review_workspace(name, rs)
        evidence = rr.material(review_workspace) + '\n' + data_dictionary(rs.datasets)
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
                'content': evidence
                + '\n结构（不含生产公式）：'
                + json.dumps(structure, ensure_ascii=False)
                + '\n表达式语法：'
                + self._dsl(),
            },
        ]
        checker = getattr(self.ws.llm, 'checker_llm', None)
        policy = self.reviewer_policy()
        legacy_signature = fingerprint({'messages': msgs, 'execution_profile': _EXECUTION_PROFILE, 'reviewer_policy': policy})
        split = bool(getattr(checker, 'basis_review_split', False))
        prior = self.state.get('references', {}).get(name)
        prior_split = bool(prior and (prior.get('reviewer') or {}).get('basis_review_split'))
        split_signature, split_structure = None, None
        resume_signatures = []
        revalidated_profile = None
        attempt_contexts = self.state.setdefault('basis_review_attempt_context', {})
        previous_attempt = attempt_contexts.get(name, {})
        if split or prior_split:
            from bonus_agent.basis_review_parts import BasisReviewParts
            split_structure = BasisReviewParts.visible_structure(self, name, structure, evidence,
                                                                workspace=review_workspace)
            split_input = {'evidence': evidence, 'structure': split_structure, 'dsl': self._dsl(),
                           'execution_profile': _EXECUTION_PROFILE, 'reviewer_policy': policy}
            split_signature = fingerprint(split_input)
            if split:
                candidates = [previous_attempt]
                if prior_split and prior.get('spec_state') in ('valid', 'population_difference', 'amount_difference', 'unresolved'):
                    candidates.append(prior)
                for candidate in candidates:
                    old_profile = candidate.get('execution_profile')
                    if not old_profile or old_profile == _EXECUTION_PROFILE:
                        continue
                    old_packet = {**split_input, 'execution_profile': old_profile}
                    old_source = fingerprint(old_packet)
                    if old_source != candidate.get('source_signature'):
                        # One earlier packet format included the same scoped answers in the structure too.
                        confirmed = [{'id': q.id, 'related': q.related, 'answer': q.answer}
                                     for q in review_workspace.questions if q.answer not in (None, '') and not q.withdrawn]
                        old_source = fingerprint({**old_packet, 'structure': {**split_structure, 'confirmed_answers': confirmed}})
                    old_input = fingerprint((old_source, 'basis_review_parts:1',
                                             getattr(checker, 'basis_review_reasoning_effort', None)))
                    if (candidate.get('source_signature'), candidate.get('input_signature')) == (old_source, old_input):
                        # Each section still needs its original request identity and current validation.
                        resume_signatures.append(old_input)
                        if (candidate is prior and prior.get('spec_state') == 'valid'
                                and prior.get('basis_digest') == digest and prior.get('report', {}).get('status') == 'PASS'):
                            revalidated_profile = old_profile
        source_signature = split_signature if split else legacy_signature
        signature = (fingerprint((source_signature, 'basis_review_parts:1', getattr(checker, 'basis_review_reasoning_effort', None)))
                     if split else source_signature)
        same_input = bool(prior and prior.get('input_signature') == signature)
        same_prior_evidence = bool(prior and prior.get('source_signature', prior.get('input_signature')) ==
                                   (split_signature if prior_split else legacy_signature))
        if (same_prior_evidence and prior.get('spec_state') in ('valid', 'unresolved', 'population_difference', 'amount_difference') and
                prior.get('report', {}).get('status') == 'FAIL'):
            # Preserve a frozen opinion's original protocol and evidence identity across request-mode changes.
            same_input = True
            signature = prior['input_signature']
            source_signature = prior.get('source_signature', signature)
        amendment = getattr(self.ws, '_settlement_source_amendment', None)
        use_amendment = amendment and amendment.get('scheme') == name and prior and prior.get('spec_state') == 'unresolved'
        cached = bool(not use_amendment and (revalidated_profile or (same_input and prior.get('spec_state') in ('valid', 'unresolved'))))
        if cached_only and (not cached or prior.get('basis_digest') != digest
                            or prior.get('report', {}).get('status') != 'PASS'):
            return None
        if self.current('review:' + name) and cached and prior['report']['status'] == 'PASS':
            return {'status': 'PASS', 'scheme': name, 'cached': True, 'by': prior['reviewer']['name'],
                    'reviewer': copy.deepcopy(prior['reviewer']),
                    'next': self.hint() if self.current('approved:' + name) else '等待财务确认总额'}
        # An explicit new review cannot borrow a previous review's PASS when its
        # reviewer or evidence changes. Human approval receipts remain untouched.
        self.state['receipts'].pop('review:' + name, None)
        reviewer = copy.deepcopy(prior.get('reviewer')) if cached else None
        scope_questions = prior.get('scope_questions', {}) if cached else {}
        scope_confirmations = prior.get('scope_confirmations', {}) if cached else {}
        reused_settlement_scopes = prior['report'].get('reused_settlement_scopes', []) if cached else []
        scope_origin = prior.get('scope_origin', {}) if prior else {}
        if prior and prior.get('spec_state') == 'unresolved' and prior.get('scope_questions'):
            scope_origin = {key: prior[key] for key in ('version', 'execution_profile', 'source_signature',
                                                        'input_signature', 'scope_questions') if key in prior}
        spec, raw_response = None, None
        terminal = copy.deepcopy(prior.get('terminal')) if cached else None
        source_binding = prior.get('source_binding') if cached else None

        def remember(report, spec_state='valid', diagnostic=None):
            self.state.setdefault('references', {})[name] = {
                'version': version, 'basis_digest': digest, 'input_signature': signature, 'source_signature': source_signature,
                'execution_profile': _EXECUTION_PROFILE,
                'spec': copy.deepcopy(spec), 'spec_state': spec_state, 'report': copy.deepcopy(report),
                'reviewer': copy.deepcopy(reviewer), 'diagnostic': diagnostic, 'raw_response': raw_response,
                'reviewer_policy': policy, 'scope_questions': copy.deepcopy(scope_questions),
                'scope_confirmations': copy.deepcopy(scope_confirmations),
                'scope_origin': copy.deepcopy(scope_origin),
                **({'terminal': copy.deepcopy(terminal)} if terminal and report.get('status') == 'PASS' else {}),
                **({'source_binding': copy.deepcopy(source_binding)} if source_binding else {}),
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

        if use_amendment:
            from bonus_agent.settlement_bindings import claim

            spec, final_declaration, source_binding = claim(self, name, rs, amendment, prior)
            terminal = {'version': self.version(), 'declaration': final_declaration}
            reviewer = copy.deepcopy(prior['reviewer'])
            signature = fingerprint((signature, source_binding, spec))
            reused_settlement_scopes = sorted(spec['populations'])
        elif cached:
            spec = copy.deepcopy(prior['spec'])
        elif not split and same_prior_evidence and prior.get('spec_state') == 'amount_difference':
            report = {**prior['report'], 'cached': True}
            self.state['last_review'] = report
            return report
        elif split or (same_prior_evidence and prior.get('spec_state') == 'population_difference'):
            from bonus_agent.basis_review_parts import BasisReviewParts, PoolDifference, PopulationDifference
            actual, seen = checker, set()
            while getattr(actual, 'think_llm', None) is not None and id(actual) not in seen:
                seen.add(id(actual))
                actual = actual.think_llm
            reviewer = _review_model_identity(actual)
            reviewer.update(fallback_reason='', basis_review_split=True,
                            basis_review_reasoning_effort=getattr(checker, 'basis_review_reasoning_effort', None))
            parts = None
            if same_prior_evidence and prior.get('spec_state') == 'population_difference':
                # Keep an already observed source-grounded scope disagreement frozen, including across request-mode changes.
                spec = prior['spec']
                _, root, levels, _ = checked(self.ws._independent_env, name, {}, basis=True)
                observed = check_populations(self.ws, rs, spec, root, levels, scope_names=spec['populations'])
                if not observed['contract_valid']:
                    invalid('此前已记录的范围声明已无法通过程序核验')
                revisit = bool(split and blank_scope_witnesses(observed))
                if observed['status'] != 'PASS' and split and not revisit and (prior.get('reviewer') or {}).get('basis_review_split'):
                    parts = BasisReviewParts(self, name, rs, evidence, split_structure, signature,
                                             resume_signatures=resume_signatures)
                    revisit = parts.can_revisit_shared_rows(spec['populations'], observed)
                if observed['status'] != 'PASS' and not revisit:
                    report = {**prior['report'], 'cached': True, 'issues': observed['issues'],
                              'population': observed, 'basis_digest': digest}
                    remember(report, 'population_difference')
                    self.state['last_review'] = report
                    return report
            try:
                if split:
                    attempt_contexts[name] = {'execution_profile': _EXECUTION_PROFILE,
                                              'source_signature': source_signature, 'input_signature': signature}
                parts = parts or BasisReviewParts(self, name, rs, evidence, split_structure, signature,
                                                  resume_signatures=resume_signatures)
                saved, choices = self._saved_scope_review(name, prior, rs, structure, split_input) if split else (None, {})
                settlement = (self._saved_settlement_scope(name, prior, rs, structure, split_input, review_workspace)
                              if split and not saved else None)
                spec = parts.generate(saved_populations=saved, scope_choices=choices, settlement_scope=settlement)
                reused_settlement_scopes = parts.reused_settlement_scopes
                if reused_settlement_scopes and parts.terminal_declaration:
                    terminal = {'version': self.version(), 'declaration': copy.deepcopy(parts.terminal_declaration)}
                scope_questions = parts.scope_questions
                scope_confirmations = parts.scope_confirmations
            except PopulationDifference as exc:
                scope_confirmations = parts.scope_confirmations
                spec = {'populations': exc.selection}
                report = {'status': 'FAIL', 'issues': exc.report['issues'], 'population': exc.report,
                          'basis_digest': digest, 'cached': False, 'checked_objects': 0, 'by': reviewer['name'],
                          'reviewer': copy.deepcopy(reviewer)}
                remember(report, 'population_difference')
                self.state['last_review'] = report
                return report
            except PoolDifference as exc:
                scope_confirmations = parts.scope_confirmations
                spec = exc.declaration
                report = {'status': 'FAIL', **exc.report, 'basis_digest': digest,
                          'cached': bool(same_input and prior and prior.get('spec_state') == 'amount_difference'),
                          'by': reviewer['name'], 'reviewer': copy.deepcopy(reviewer)}
                remember(report, 'amount_difference')
                self.state['last_review'] = report
                return report
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
            reviewer = _review_model_identity(actual)
            reviewer['fallback_reason'] = self.ws.checker_down
            try:
                spec = parse_json(raw_response)
            except (LLMError, ValueError) as exc:
                invalid('独立复核输出无法解析为JSON：' + str(exc))
        if isinstance(spec, dict) and spec.get('unclear'):
            invalid('独立复核存在未明确口径，补充原文依据前保持阻断：' + str(spec['unclear']), unresolved=True)
        if isinstance(spec, dict) and 'settlement_question' in spec:
            invalid('独立复核提交了待答复问题，但未声明需要解决的业务口径')
        if not isinstance(spec, dict) or not isinstance(spec.get('pool'), str) or not spec['pool'].strip():
            invalid('独立复核须输出包含非空pool表达式的JSON对象')
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
        comparison = self.compare_pool(basis, pools, rs.pool.scope)
        issues, examples = comparison['issues'], comparison['examples']
        pool_matched = not issues

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
            'checked_objects': comparison['checked_objects'] + len(wanted),
            'population': population,
            'by': reviewer['name'],
            'reviewer': copy.deepcopy(reviewer),
        }
        if revalidated_profile:
            report['revalidated_from_profile'] = revalidated_profile
        if reused_settlement_scopes:
            report['reused_settlement_scopes'] = reused_settlement_scopes
        if source_binding:
            report['source_binding'] = source_binding
        if pool_matched and totals == actual_totals and details != wanted:
            repair = self._repair_pool_detail(name, rs, basis, spec, wanted, details)
            if repair:
                report['report_binding_repair'] = repair
                report['next'] = '逐对象分配明细已按原文绑定到已复核的奖金池。重新预核算并复核报表。'
        if not issues:
            self.receipt('review:' + name, report, kind='basis')
            approval = self.state.get('confirmed_basis', {}).get(name)
            unanswered = {q.id for q in self.ws.pending}
            questions = self.questions_for(name)
            if (approval and not any(q.id in unanswered for q in questions) and self.current('review:' + name)
                    and self.confirmation_scope(digest, self.ws._source_version, self.ws.period, questions)
                    == approval['scope']):
                self.receipt('approved:' + name, {'by': approval['by'], 'basis_digest': digest,
                                                  'original_confirmed_at': approval['at']}, kind='basis')
                if self.state.get('waiting') == name:
                    self.state.pop('waiting')
                report['confirmation_reused'] = True
                report['next'] = '精确金额版本与财务确认一致，继续人员分配。'
            else:
                try:
                    derived = self._derive_funding_authority(name, digest) if not self.ws.pending else None
                except (ToolError, EngineError, EvalError, ValueError) as exc:
                    self.state['receipts'].pop('review:' + name, None)
                    report['status'] = 'FAIL'
                    issues.append(str(exc))
                else:
                    if derived:
                        self.receipt('approved:' + name, derived, kind='basis')
                        if self.state.get('waiting') == name:
                            self.state.pop('waiting')
                        report['confirmed_funding'] = derived
                        report['next'] = '已验证逐对象资金承接，继续人员分配终检。'
                    else:
                        self.state['waiting'] = name
                        self.ws.done = True
        remember(report)
        if issues:
            self.state['last_review'] = report
        return report

    def _repair_pool_detail(self, name, rules, basis, spec, wanted, observed):
        from bonus_agent.clauses import check_quote

        scope = rules.pool.scope
        if scope == 'global':
            return None
        different = {json.loads(key)[0] for key in wanted.keys() | observed.keys()
                     if wanted.get(key) != observed.get(key)}
        if len(different) != 1:
            return None
        label, = different
        if spec['details'].get(label) != spec['pool']:
            return None
        expected = {json.dumps([label, key], ensure_ascii=False): Decimal(value['amount'])
                    for key, value in basis['objects'].items()}
        if not expected or expected != {key: value for key, value in observed.items() if json.loads(key)[0] == label}:
            return None

        requirement = re.compile(
            rf'(?:请|应当|应|必须|需)?(?:保留|输出|列出|提供|生成|展示)[^。；;\n]{{0,24}}'
            rf'(?:逐|每[个位条]|按)\s*{re.escape(scope)}[^。；;\n]{{0,24}}'
            r'(?:可分配|正常分配|实际分配)[^。；;\n]{0,12}金额[^。；;\n]{0,24}明细')
        roles = self.state.get('plan', {}).get('files', {})
        corpus = self.ws._corpus()
        source = next(({'doc': c.file, 'quote': c.quote} for c in self.ws.clauses
                       if roles.get(c.file) == 'rule' and requirement.match(c.quote.lstrip())
                       and not check_quote(c.file, c.quote, corpus)), None)
        if not source:
            return None

        draft = copy.deepcopy(self.ws.drafts[name].get('report') or {})
        lines = [item for item in draft.get('details', []) if item.get('name') == label and item.get('level') == scope]
        if len(lines) != 1 or lines[0].get('expr') == rules.pool.expr:
            return None
        item = lines[0]
        previous = item['expr']
        item.update(expr=rules.pool.expr, sources=[source])
        self.ws.set_rules(name, report=draft)
        return {'item': label, 'scope': scope, 'previous_expression': previous,
                'expression': rules.pool.expr, 'source': source, 'matched_objects': len(expected)}

    @staticmethod
    def _dsl():
        from bonus_agent.auto import prompt
        from bonus_core.dsl.functions import describe

        text = prompt('extract_rules')
        return text[text.index('## 表达式语法') : text.index('## 发放与抵扣')].replace('{functions}', describe())

    @staticmethod
    def confirmation_scope(digest, sources, period, questions):
        return fingerprint({'basis_digest': digest, 'sources': sources, 'period': str(period),
                            'answers': [(q.id, q.text, q.answer, q.withdrawn) for q in questions]})

    def approve(self, name, by, digest, *, reviewed_snapshot=False):
        if not by.strip():
            fail('确认人不能为空')
        self.require('review:' + name)
        # Offline confirmation is bound to the exact source and question status
        # already signed by the review receipt. Live jobs check their own reviewer route.
        unanswered = {q.id for q in (self.ws._unanswered_questions() if reviewed_snapshot else self.ws.pending)}
        questions = self.questions_for(name)
        if any(q.id in unanswered for q in questions):
            fail('存在待答复的业务问题，不能确认')
        prior = copy.deepcopy(self.state['basis'][name])
        if fingerprint(prior) != digest:
            fail('确认的总额版本已变化，请刷新核对')
        if self.state['receipts']['review:' + name]['payload'].get('basis_digest') != digest:
            fail('确认的金额摘要与独立复核回执不一致')
        if self.current('approved:' + name):
            approved = self.state['receipts']['approved:' + name]['payload']
            if approved.get('basis_digest') == digest:
                return {'status': 'PASS', 'scheme': name, 'by': approved['by'], 'digest': digest,
                        'already_confirmed': True}
        self.calculate_basis(name)  # Recompute from immutable source inputs, not a browser payload.
        if self.state['basis'][name] != prior:
            fail('重新计算的金额或对象范围与待确认版本不一致')
        approval = self.receipt('approved:' + name, {'by': by, 'basis_digest': digest}, kind='basis')
        self.state.setdefault('confirmed_basis', {})[name] = {
            'scope': self.confirmation_scope(digest, self.ws._source_version, self.ws.period, questions),
            'by': by, 'at': approval['at'],
        }
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

    def _derive_funding_authority(self, name, digest):
        from bonus_core.engine.run import compute

        rs = self.ws._ruleset(name)
        contracts = [item.source_funding for item in rs.report.distributions if item.source_funding] if rs.report else []
        if len(contracts) != 1 or rs.payout or rs.offsets or self.ws._merged(name).get('revisions'):
            return None
        source, = contracts
        if self.ws._deps(name) != {(source.scheme, 'R0')}:
            fail('来源资金声明须与实际承接的已确认方案一致')
        self.require('data')
        self.require('allocation_formulas')
        self.require('basis:' + name)
        self.require('review:' + name)
        funding = self._approved_funding_reference(source.scheme)
        if funding is None:
            fail('尚无有效的来源逐对象资金或财务确认')
        data, _ = self.ws._data(rs)
        result = compute(rs, data, refs={source.scheme + '/R0': funding}, period=self.ws.period)
        self.check_basis_result(name, rs, result, require_approval=False)
        prior = self.state['receipts']['approved:' + source.scheme]['payload']
        return {'basis_digest': digest, 'by': '已确认资金承接', 'origin': 'confirmed_funding',
                'funding_scheme': source.scheme, 'funding_basis_digest': prior['basis_digest'],
                'funding_confirmed_by': prior['by'], 'funding_objects': len(funding.funding)}

    def check_basis_result(self, name, rs, result, *, require_approval=True):
        """Verify the exact project population and amounts after the runtime changes."""
        basis = self.state['basis'][name]
        digest = fingerprint(basis)
        steps = ('review', 'approved') if require_approval else ('review',)
        if any(self.state['receipts'][step + ':' + name]['payload'].get('basis_digest') != digest for step in steps):
            fail('金额基线与复核或确认回执不一致')
        population = {
            node.id: {'raw': trace.value, 'amount': node.final}
            for node in result.nodes.values()
            if (node.level == rs.pool.scope or (rs.pool.scope == 'global' and node.parent is None))
            and (trace := result.traces.get(node.id + '/奖金池'))
        }
        basis_details = {item.name for item in rs.report.details if item.stage == 'basis'} if rs.report else set()
        basis_totals = {item.name for item in rs.report.totals if item.stage == 'basis'} if rs.report else set()
        details = {key: value for key, value in result.details.items() if key in basis_details}
        totals = {key: value for key, value in result.totals if key in basis_totals}
        diff = [label for label, actual, expected in (
            ('奖金池', Decimal(result.pool), Decimal(basis['pool'])), ('逐对象金额', population, basis['objects']),
            ('对象明细', details, basis['details']), ('合计', totals, basis['totals']),
        ) if actual != expected]
        if diff:
            fail('试算后的' + '、'.join(diff) + '与财务确认不一致。返回 calculate_basis 重新核对来源和金额。')

    def answered_settlement_schemes(self):
        """Find the unchanged linked schemes that can resume the standard review after an answer."""
        if self.ws.pending or not self.state.get('settlement_questions'):
            return {}
        linked = {}
        for q in self.ws.questions:
            if q.withdrawn or q.answer in (None, ''):
                continue
            if scope := self._settlement_question_scope(q):
                linked[scope['scheme']] = scope['parent']
        return linked

    def recheck(self, *, initial_compile=False, on_result=None, resumed_settlements=None):
        """数据输入在检查后变化，或公式回执失效时，重新执行相应的确定性检查。
        实测模型补一条分配层级或改一处规则就退回前一阶段、清空对话，再把整套检查和读取重做一遍，白耗步数和 tokens。
        initial_compile 允许基础任务首次就绪时编译，也允许重验已有成功回执的完整分配规则。
        曾复核的总额可复算并重验原声明，不发新请求。
        每个输入版本只自动执行一次；返回 [(检查名, 是否通过, 未通过的问题)]。on_result 收到每步自己的报告。"""
        from bonus_agent.agent import ToolError
        from bonus_core.engine.evaluator import EvalError
        from bonus_core.engine.issues import EngineError

        resumed_settlements = resumed_settlements or {}
        linked = set(resumed_settlements) | set(resumed_settlements.values())
        done = []
        for _ in range(10 if linked else 4):
            if self.ws.done:
                break
            stage, scheme = self.stage()
            if stage == 2 and not self.ws.pending:
                name, kind, phase, run = 'prepare_data', 'data', None, self.prepare_data
            elif stage == 3:
                from bonus_agent.construction import TaskBoard
                phase = 'basis' if not self.current('formulas') else 'all'
                receipts = self.state['receipts']
                passed_before = 'formulas' in receipts or 'allocation_formulas' in receipts
                if phase == 'all' and initial_compile:
                    # A scoped answer can stale the full receipt without reopening
                    # confirmed upstream funding. Revalidate it before the next
                    # model request; a first-time allocation still needs its normal turn.
                    prior_all = receipts.get('allocation_formulas', {}).get('payload', {})
                    if self.ws.pending or prior_all.get('status') != 'PASS':
                        break
                elif not passed_before and not initial_compile:
                    break
                # Do not manufacture local PASS receipts for unfinished work. Existing
                # stale receipts may be refreshed by the board's normal deterministic gate.
                board = TaskBoard(self.ws)
                issues = board.gate(phase)
                if issues and not board.runtime_blocked_schemes(issues, phase):
                    break
                name, kind = 'compile_formulas', 'basis' if phase == 'basis' else 'formula'
                run = lambda: self.compile(phase)
            elif stage in (4, 5) and not self.ws.pending:
                prior = self.state.get('references', {}).get(scheme, {})
                if (prior.get('report', {}).get('status') != 'PASS'
                        and not (stage == 4 and scheme in resumed_settlements
                                 and self.current('approved:' + resumed_settlements[scheme]))):
                    break
                kind, phase = 'basis', 'basis'
                if stage == 4:
                    name, run = 'calculate_basis', lambda: self.calculate_basis(scheme)
                else:
                    name, run = 'review_basis', lambda: self.review_basis(scheme, cached_only=True)
            elif stage == 7 and scheme in linked and not self.ws.pending:
                name, kind, phase = 'trial_run', 'formula', 'all'

                def run():
                    self.ws.trial_run(scheme)
                    return {'status': 'PASS', 'scheme': scheme, 'phase': phase}
            else:
                break
            version = self.version(kind)
            if name == 'prepare_data':
                previous = self.state.get('data_report', {})
                failed_version = previous.get('version') if previous.get('report', {}).get('status') == 'FAIL' else None
                if failed_version == version or (not failed_version and 'data' not in self.state['receipts']):
                    break
            elif name == 'compile_formulas':
                feedback = self.compile_feedback(phase, version=version)
                if feedback and feedback['status'] == 'FAIL':
                    break
            key = name + ':' + (phase or kind)
            if stage in (4, 5):
                version = self._receipt_version(('basis:' if stage == 4 else 'review:') + scheme, kind)
            prior = self.state.get('auto_checks', {}).get(key, {})
            if (name != 'compile_formulas' and prior.get('version') == version and prior.get('scheme') == scheme
                    and (name != 'trial_run' or prior.get('report', {}).get('status') != 'PASS')):
                break
            try:
                report = run()
                if report is None:
                    break  # The current evidence needs a new reviewer opinion.
                ok, issues = report.get('status') == 'PASS', report.get('issues') or []
            except (ToolError, EvalError, EngineError) as exc:
                ok, issues = False, [str(exc)]
                report = {'status': 'FAIL', 'issues': issues, 'phase': phase}
            # Parameter binding repair can change inputs during compilation. Record the
            # resulting version so that the repair itself cannot create an automatic loop.
            self.state.setdefault('auto_checks', {})[key] = {
                'version': self.version(kind) if stage < 4 else version, 'kind': kind, 'phase': phase, 'scheme': scheme,
                'report': copy.deepcopy(report),
            }
            done.append((name, ok, issues))
            if on_result:
                on_result(name, phase, scheme, ok, issues, report)
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
        schemes = self.ws._order(self.ws.schemes)
        waiting = self.state.get('waiting')
        saved_wait = (waiting in schemes[1:] and self.current('basis:' + waiting)
                      and self.current('review:' + waiting) and not self.current('approved:' + waiting))
        if saved_wait:
            digest = fingerprint(self.state['basis'].get(waiting))
            saved_wait = (self.state['receipts']['basis:' + waiting]['payload'].get('digest') == digest
                          and self.state['receipts']['review:' + waiting]['payload'].get('basis_digest') == digest)
        for s in schemes:
            if not self.current('basis:' + s):
                return 4, s
            if not self.current('review:' + s):
                return 5, s
            if not self.current('approved:' + s):
                return 6, s
            if not self.current('allocation_formulas'):
                return 3, s
            if (s not in self.ws.results or s in self.ws.untested) and not saved_wait:
                return 7, s
        return 8, None

    def hint(self):
        if held := self.evidence_wait():
            return '请补充下列事项的原文或财务答复。' + held['error'][:800]
        stage, scheme = self.stage()
        if stage == 3:
            phase = 'basis' if not self.current('formulas') else 'all'
            from bonus_agent.construction import TaskBoard
            board = TaskBoard(self.ws)
            if board.state['tasks'] and board.pool_plan_gap():
                return board.summary()['next']
            report = self.compile_feedback(phase)
            if report and report['status'] == 'FAIL':
                summary = self.compile_summary(report)
                return ('当前版本已编译未通过，先修复反例，不要原样重编译：'
                        + '；'.join(str(i)[:450] for i in summary['issues'][:3])
                        + '；完整检查：read_workspace(section="checks")')
            return board.summary()['next']
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
                + '。逐项证据与下一步：read_workspace(section="checks", name="data")'
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
            gap = construction.get('planning_gap') or {}
            if report and report['status'] == 'FAIL' and (not gap or gap.get('kind') == 'parameter_owners'):
                # Keep the nested task card and the workflow next action consistent.
                construction = {**construction, 'next': gap.get('message', next_action), 'compile_failure': self.compile_summary(report)}
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
            'submit_construction_repair': 3,
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
