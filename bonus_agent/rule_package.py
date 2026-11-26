"""Validate and commit evidence-bound constructions on an isolated workspace."""
from __future__ import annotations

import copy
import json
import re

from bonus_agent import facts as fx
from bonus_agent.construction import TaskBoard
from bonus_agent.formula_inputs import input_preflight_versions
from bonus_agent.recovery import fingerprint
from bonus_core.data.mapping import required_fields
from bonus_core.dsl.parser import Call, ExprError, Name, parse, walk


def _archived_failed_steps(out, last):
    if not out or not last or last.get('status') != 'FAILED' or type(last.get('n')) is not int:
        return []
    path = out / 'history' / f'第{last["n"]}轮' / 'agent_log.json'
    try:
        log = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    if (not isinstance(log, dict) or log.get('error') != last.get('error')
            or not isinstance(log.get('steps'), list)):
        return []
    return [s for s in log['steps'] if isinstance(s, dict) and not s.get('ok') and isinstance(s.get('args'), dict)]


def archived_tier_candidate(out, last):
    """Reconnect a submitted formula with the same author's missing tier inputs, for revalidation."""
    attempts = [s for s in _archived_failed_steps(out, last) if s.get('tool') == 'submit_construction_repair']
    if len(attempts) < 2:
        return None
    latest = attempts[-1]
    args = latest['args']
    if args.get('tiers') or args.get('datasets'):
        return None
    try:
        feedback, _ = json.JSONDecoder().raw_decode(latest['result'].lstrip())
    except (ValueError, KeyError):
        return None
    if (feedback.get('stage') != 'formula' or feedback.get('failed_task') != args.get('task')
            or len(feedback.get('issues', ())) != 1):
        return None
    issue = feedback['issues'][0]
    if not issue.startswith('construction.dependency'):
        return None
    missing = set(re.findall(r"'([^']+)'", issue))
    if not missing:
        return None
    for earlier in reversed(attempts[:-1]):
        proposal = earlier['args']
        tiers = proposal.get('tiers') or ()
        if (proposal.get('scheme') == args.get('scheme') and proposal.get('task') == args.get('task')
                and all(isinstance(tier, dict) for tier in tiers)
                and {tier.get('task') for tier in tiers} == missing):
            return {'tool': 'submit_construction_repair', 'args': {**args, 'tiers': tiers}}
    return None


def _repair_budget(ws, spec, value, bindings=None, facts=None):
    from bonus_agent.agent import ToolError
    from bonus_agent.indicator_budget import grouped_budget_candidate

    try:
        proposal = grouped_budget_candidate(TaskBoard(ws), spec, value)
    except ToolError:
        return None
    if proposal is None:
        return None
    item = proposal['formulas'][0]
    if bindings:
        item['bindings'] = bindings
    if facts:
        item['facts'] = facts
    return {**_submit_prepared(ws, spec, proposal), 'automatic_repair': 'grouped_budget'}


def _submit_prepared(ws, spec, proposal):
    from bonus_agent.agent import ToolError

    coefficient = proposal.pop('coefficient', None)
    plan = proposal.pop('coefficient_plan', None)
    builds = proposal.pop('coefficient_builds', [])
    tiers = proposal.pop('tier_builds', [])
    if coefficient:
        builds = [*builds, {'call': coefficient, 'plan': plan}]
    plans = [*proposal.pop('upstream_tasks', ()), *(b['plan'] for b in builds if b.get('plan'))]
    candidate = ws
    reports = []
    if builds or plans or tiers:
        candidate = copy.deepcopy(ws, {id(ws.llm): ws.llm})
        try:
            if plans:
                TaskBoard(candidate).plan(plans)
            for build in builds:
                call = build['call']
                name = call['name']
                candidate.compile_coefficient_table(**call)
                board = TaskBoard(candidate)
                board.sync_targets()
                table_task = next((t['id'] for t in board.state['tasks'].values()
                                   if t['scheme'] == spec['scheme'] and fx.target_parts(t['target']) == ['tables', name]), None)
                if table_task is None:
                    raise ToolError(f'系数表 {name} 没有对应的构建任务')
                checked = board.check(table_task, include_next=False, link_clauses=False)
                if checked['status'] != 'PASS':
                    raise ToolError('；'.join(checked['issues']))
                reports.append({'table': name, 'source': call['cells_from']})
            for build in tiers:
                call = build['call']
                source = call['cells_from']
                registered = candidate.record_parameter_cells(**source, cells=build['cells'], applies_to=build['caption'])
                if registered['status'] != 'PASS':
                    raise ToolError('；'.join(registered.get('failed') or ['费率原格尚未通过登记']))
                candidate.compile_table(**call)
            tier_reports = [TaskBoard(candidate).check(build['task'], include_next=False, link_clauses=False)
                            for build in tiers]
            if failed := next((r for r in tier_reports if r['status'] != 'PASS'), None):
                raise ToolError('；'.join(failed['issues']))
        except ToolError as exc:
            return {'task': spec['id'], 'scheme': spec['scheme'], 'target': spec['target'],
                    'status': 'FAIL', 'saved': False, 'stage': 'tier' if tiers else 'coefficient',
                    'issues': [str(exc)], 'next': '先按原表核对系数表、适用对象或边界，再提交公式。'}
    result = submit_repair(candidate, **proposal)
    if result.get('saved') and candidate is not ws:
        ws.__dict__.update(candidate.__dict__)
        if len(reports) == 1:
            result['coefficient_input'] = reports[0]
        elif reports:
            result['coefficient_inputs'] = reports
        if tiers:
            result['tier_inputs'] = [{'table': b['call']['name'], 'source': b['call']['cells_from']} for b in tiers]
    return result


def resume_grouped_budget(ws, out, last):
    """Revalidate the failed grouping proposal from the same saved round."""
    board = TaskBoard(ws)
    if not board.state['tasks'] or not ws._flow().current('data'):
        return None
    statuses = board.statuses(with_confidence=False)
    for step in reversed(_archived_failed_steps(out, last)):
        if step.get('tool') != 'submit_formula_task':
            continue
        args = step['args']
        task = args.get('task')
        if not isinstance(task, str) or task not in statuses or statuses[task]['status'] == 'PASS':
            continue
        spec = board.task_spec(task)
        if f'construction:{task}:{spec["target"]}' not in last['error']:
            continue
        if repaired := _repair_budget(ws, spec, args.get('value'), args.get('bindings'), args.get('facts')):
            return repaired
    return None


def prepare_formula_inputs(ws):
    """Prepare evidenced inputs for ready tasks before calling a model."""
    from bonus_agent.amount_tiers import amount_tier_clause, amount_tier_input
    from bonus_agent.coefficient import prepare_planned_coefficient
    from bonus_agent.funding_components import product_clause, product_input
    from bonus_agent.indicator_budget import monthly_budget_clause, monthly_budget_input
    from bonus_agent.indicator_coefficients import optional_metric_clause, optional_metric_input
    from bonus_agent.indicator_expressions import (
        explicit_expression_clause, explicit_expression_input, grouped_sum_clause, grouped_sum_input,
    )
    from bonus_agent.indicator_history import historical_average_clause, history_average_input
    from bonus_agent.indicator_snapshot import snapshot_ratio_clause, snapshot_ratio_input
    from bonus_agent.tier_plan import prepare as prepare_tier_columns

    if not ws._flow().current('data'):
        return None
    board = TaskBoard(ws)
    providers = ((amount_tier_clause, amount_tier_input), (product_clause, product_input),
                 (historical_average_clause, history_average_input),
                 (optional_metric_clause, optional_metric_input),
                 (explicit_expression_clause, explicit_expression_input), (grouped_sum_clause, grouped_sum_input),
                 (monthly_budget_clause, monthly_budget_input),
                 (snapshot_ratio_clause, snapshot_ratio_input))
    unbuilt = {}
    for spec in board.state['tasks'].values():
        if spec['target'].startswith('/tables/') and len(spec['facts']) >= 2:
            unbuilt[spec['id']] = lambda board, spec: prepare_tier_columns(board, spec) or prepare_planned_coefficient(board, spec)
        elif board.value(spec) is None:
            for applicable, resolve in providers:
                if applicable(ws, spec):
                    unbuilt[spec['id']] = resolve
                    break
    if not unbuilt:
        return None
    summary = board.summary()
    active = summary.get('active')
    if not active:
        return None
    preferred = summary.get('runtime_repair', {}).get('task') or active['id']
    candidates = [preferred]
    if board.phase() == 'basis' and not summary.get('runtime_repair') and preferred in board.state['tasks']:
        required = board.required('basis')
        states = board.statuses(with_confidence=False)
        candidates.extend(n for n in unbuilt if n != preferred and n in required and states[n]['status'] == 'PENDING'
                          and board.task_spec(n)['scheme'] == active['scheme'])
    for ident in candidates:
        if ident not in unbuilt:
            continue
        spec = board.task_spec(ident)
        versions = input_preflight_versions(ws, spec)
        version = versions['version']
        old = board.state.get('input_preflight', {}).get(ident, {})
        # Independent ready work may be completed opportunistically. A failure
        # in that work belongs to its own task and must wait for dispatch.
        # Do not keep retrying a speculative failure as other components change.
        if (ident != preferred and old.get('report', {}).get('status') == 'FAIL'
                and old.get('inputs_version') == versions['inputs_version']):
            continue
        if old.get('version') == version:
            if ident == preferred and old.get('deferred'):
                old['deferred'] = False
                return old['report']
            continue
        proposal = unbuilt[ident](board, spec)
        if proposal is None:
            continue
        call = proposal.pop('candidate_call', None)
        if call is not None:
            report = _submit_prepared(ws, spec, call)
            result = {**report, 'data_input': proposal}
        else:
            result = proposal
        deferred = ident != preferred and not (result.get('status') == 'PASS' and result.get('saved') is True)
        TaskBoard(ws).state.setdefault('input_preflight', {})[ident] = {
            **versions, 'report': result, 'deferred': deferred,
        }
        if not deferred:
            return result
    return None


def _submission_value(candidate, spec, value, changes):
    from bonus_agent.agent import ToolError

    if value is not None and changes is not None:
        raise ToolError('construction.package：value与changes互斥；完整替换用value，仅修改已有对象部分字段用changes')
    if changes is not None:
        if not isinstance(changes, dict) or not changes:
            raise ToolError('construction.package：changes须为非空对象；只提供明确要替换的顶层字段')
        try:
            # Read the full stored target. TaskBoard.value may intentionally
            # project report/checks during basis validation; merging that view
            # would silently discard later distribution definitions.
            current = fx.target_value(candidate._merged(spec['scheme']), spec['target'])
        except (KeyError, IndexError, TypeError, ValueError):
            current = None
        if not isinstance(current, dict):
            raise ToolError('construction.package：changes仅适用于已存在的对象目标；新目标或列表目标须用value完整提交')
        # One explicit top-level replacement per supplied field. Nested maps
        # and lists are not patched by guessed keys, positions or row identity.
        value = {**copy.deepcopy(current), **copy.deepcopy(changes)}
    if value is None or not isinstance(value, (dict, list)):
        raise ToolError('construction.package：须提供value完整对象/列表，或changes修改已有对象字段；删除组件使用set_rules')
    return value


def _stage(candidate, board, task, value, bindings=None, facts=None):
    from bonus_agent.agent import ToolError

    spec = board.task_spec(task)
    parts = fx.target_parts(spec['target'])
    if spec['target'] == '/pool':
        from bonus_agent.funding_plan import pool_input_gap

        if gap := pool_input_gap(board, spec['scheme']):
            raise ToolError('construction.plan：' + gap['message'])
    payload = {parts[0]: {parts[1]: value}} if len(parts) == 2 else {parts[0]: value}
    existing = {f.name: set(f.targets) for f in candidate.facts}
    if facts:
        candidate.record_facts(facts)
        if candidate._fact_rejections:
            raise ToolError('construction.evidence：' + '；'.join(candidate._fact_rejections))
        if candidate.pending:
            raise ToolError('construction.evidence：参数存在未解决冲突；先登记冲突并依据原文或财务答复解决')
    # Bindings for other components are retained even when a fact has multiple targets.
    merged_bindings = []
    for binding in bindings or []:
        added = set(binding.get('targets', [])) - existing.get(binding.get('name'), set())
        if any(not board.owns(spec, target) for target in added):
            raise ToolError('construction.binding：新增绑定必须指向当前任务；其他组件请分别提交')
        kept = {target for target in existing.get(binding.get('name'), set()) if not board.owns(spec, target)}
        merged_bindings.append({**binding, 'targets': sorted(kept | set(binding.get('targets', [])))})
    candidate.set_rules(scheme=spec['scheme'], bindings=merged_bindings, **payload)
    if parts[0] == 'variables':
        from bonus_agent.parameter_repair import declared_parameter_repairs

        for repair in declared_parameter_repairs(candidate, task_id=task):
            try:
                candidate.bind_facts([repair])
            except ToolError:
                continue  # Leave the exact unbound-parameter diagnosis to the task checker.


def _missing_tier_inputs(board, spec):
    value = board.value(spec)
    if not isinstance(value, dict) or not isinstance(value.get('expr'), str):
        return []
    try:
        tree = parse(value['expr'])
    except ExprError:
        return []
    names = board.ws._merged(spec['scheme']).get('tables') or {}
    tools = {'tier': 'compile_table', 'tier_ratio': 'compile_coefficient_table',
             'tier_rate': 'compile_coefficient_table'}
    missing = {(term.args[0].id, tools[term.fn]) for term in walk(tree)
               if isinstance(term, Call) and term.fn in tools and term.args
               and isinstance(term.args[0], Name) and term.args[0].id not in names}
    return [{'name': name, 'tool': tool} for name, tool in sorted(missing)]


def _missing_formula_inputs(board, spec, report):
    names = {match[1] for issue in report['issues']
             if (match := re.search(r'name_unknown: 无法识别的名字 ([^\s()]+)', issue))}
    if not names:
        return []
    targets = {t['target']: t['id'] for t in board.state['tasks'].values() if t['scheme'] == spec['scheme']}
    return [{'name': name, 'task': targets.get('/variables/' + name)} for name in sorted(names)]


def plan_evidence_owners(ws, focus, tasks, replace=False):
    """Commit an additive plan for cited obligations, preserving the existing rule packages."""
    from bonus_agent.agent import ToolError
    from bonus_agent.clauses import check_quote

    if replace or not isinstance(tasks, list) or not tasks:
        raise ToolError('construction.plan：请按本次缺项提交非空 tasks，保留已有计划')
    ws._check_source_version()
    candidate = copy.deepcopy(ws, {id(ws.llm): ws.llm})
    board = TaskBoard(candidate)
    if focus['kind'] == 'clause_owners':
        field, name = 'clauses', 'id'
        outstanding = {c.id: c for c in board.unassigned_clauses()}
    else:
        field, name = 'facts', 'name'
        owned = {fact for t in board.state['tasks'].values() for fact in t['facts']}
        outstanding = {f.name: f for f in ws.facts if not f.targets and f.name not in owned}
    requested = {item[name] for item in focus[field]} & outstanding.keys()
    added, changes = set(), {}
    prior_targets = {(s['scheme'], s['target']) for s in board.state['tasks'].values()}
    for item in tasks:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ('id', 'scheme', 'target')):
            raise ToolError('construction.plan：每个条款负责人须有 id、scheme、target')
        ident = item['id']
        old = board.state['tasks'].get(ident)
        names = item.get(field)
        if not isinstance(names, list) or any(not isinstance(key, str) for key in names):
            raise ToolError(f'construction.plan：{field} 须填写已登记的证据标识数组')
        new = set(names) - set(old[field] if old else ())
        if unexpected := new - requested:
            raise ToolError(f'construction.plan：{ident} 的 {field} 提交了本次缺项之外的名称 {sorted(unexpected)[:4]}。'
                            f'本次须用原名 {focus["target"]}，已有证据由程序保留')
        if not old and (item['scheme'], item['target']) in prior_targets:
            raise ToolError('construction.plan：已有目标请复用原任务 ID')
        if old and any(v != old.get(k) for k, v in item.items() if k != field):
            raise ToolError(f'construction.plan：{ident} 已存在，本次只可追加 {field}，保留原方案、目标和依赖')
        if not new:
            if not old:
                raise ToolError(f'construction.plan：新任务 {ident} 缺少本次证据 {field}，首项为 {focus["target"]}')
            continue
        previous = changes.get(ident)
        if previous and any(previous.get(k) != v for k, v in item.items() if k != field):
            raise ToolError(f'construction.plan：{ident} 同批出现了相冲突的任务声明')
        base = previous or old or {}
        changes[ident] = {**base, **item, field: list(dict.fromkeys([*base.get(field, []), *names]))}
        added.update(new)
    if focus['target'] not in added:
        raise ToolError(f'construction.plan：本次须处理当前缺项 {focus["target"]}')
    if field == 'clauses':
        corpus = ws._corpus()
        for key in added:
            clause = outstanding[key]
            if bad := check_quote(clause.file, clause.quote, corpus):
                raise ToolError(f'construction.source：{key} {bad}')
    report = board.plan(list(changes.values()))
    for change in changes.values():
        if not set(change[field]) <= set(board.task_spec(change['id'])[field]):
            raise ToolError('construction.plan：候选未实际消除证据归属缺项')
    flow = candidate._flow()
    if ws._flow().current('data') and not flow.current('data') and candidate.prepare_data()['status'] != 'PASS':
        raise ToolError('construction.plan：候选导致当前数据复验失败')
    if any(not flow.current(key) for key in ws.workflow['receipts'] if key.startswith('approved:') and ws._flow().current(key)):
        raise ToolError('construction.plan：候选会使已确认的资金口径失效')
    report['assigned_' + field] = sorted(added)
    ws.__dict__.update(candidate.__dict__)
    return report


def submit(ws, task: str, value=None, bindings=None, facts=None, *, changes=None):
    from bonus_agent.agent import ToolError

    candidate = copy.deepcopy(ws, {id(ws.llm): ws.llm})
    board = TaskBoard(candidate)
    spec = board.task_spec(task)
    value = _submission_value(candidate, spec, value, changes)
    if ws.staged and spec['target'] == '/allocation' and board.phase() != 'all':
        return {'status': 'FAIL', 'saved': False, 'scheme': spec['scheme'], 'task': task, 'target': spec['target'],
                'issues': ['construction.phase：人员分配须在本版基础金额复核并确认后提交'],
                'next': ws._flow().hint()}
    try:
        candidate._check_source_version()
        _stage(candidate, board, task, value, bindings, facts)
        if changes is not None and spec['target'] == '/report' and board.phase() == 'basis':
            # A pre-existing pool discovered with the report can satisfy the real dependency locally.
            pool = f"auto:{spec['scheme']}:/pool"
            if pool in board.dependencies(spec) and pool not in board.state['reports']:
                board.check(pool, include_next=False, link_clauses=False)
        report = board.check(task)
        if report['status'] != 'PASS':
            if report.get('input_failures'):
                return {**report, 'saved': False, 'next': report['issues'][0]}
            if ws.staged and changes is None and (repaired := _repair_budget(ws, spec, value, bindings, facts)):
                return repaired
            if missing := _missing_tier_inputs(board, spec):
                steps = '、'.join(f"{row['name']}（{row['tool']}）" for row in missing)
                return {**report, 'saved': False, 'missing_tables': missing,
                        'next': f'先按材料的原始档位、单位和取值编制 {steps}，核对绑定后再提交当前公式。'}
            if missing := _missing_formula_inputs(board, spec, report):
                return {'missing_components': missing, **report, 'saved': False,
                        'next': '先核对未定义名称的原表字段或公式来源。缺少计算构件时用 plan_formula_tasks 登记其条款和作用域，'
                                '将它接入当前任务依赖，提交并检查前置公式后再重新提交当前公式。'}
            repair = report.get('execution', {}).get('repair', {})
            return {**report, 'saved': False,
                    'next': repair.get('next', '按issues修正当前规则包后重新submit_formula_task；原规则、证据及通过回执未替换')}
        if ws.staged and ws._flow().current('data'):
            # A new expression can make a previously optional cell mandatory.
            # Probe the actual mapping before committing this otherwise valid task.
            current = ws._ruleset(spec['scheme'])
            proposed = candidate._ruleset(spec['scheme'])
            try:
                former = required_fields(current, ws.period)
                newly_read = any(fields - former.get(dataset, set())
                                 for dataset, fields in required_fields(proposed, ws.period).items())
            except ExprError:
                newly_read = False  # Another unfinished formula belongs to its own checker.
            if newly_read:
                try:
                    candidate._data(proposed)
                except ToolError as exc:
                    if changes is None and (repaired := _repair_budget(ws, spec, value, bindings, facts)):
                        return repaired
                    return {**report, 'status': 'FAIL', 'saved': False, 'issues': [str(exc)],
                            'next': '新公式启用了尚有数据缺口的字段。先按原始行和业务口径修正映射，再提交当前公式。'}
    except ToolError as exc:
        return {'status': 'FAIL', 'task': task, 'scheme': spec['scheme'], 'target': spec['target'], 'saved': False,
                'issues': [str(exc)],
                'next': '修正当前规则包后重新submit_formula_task；未保存部分公式或部分证据'}
    ws.__dict__.update(candidate.__dict__)
    return {**report, 'saved': True,
            **({'updated_fields': sorted(changes)} if changes is not None else {}),
            'scope': '当前构件及其前置条件检查通过；金额仍须阶段编译、独立核算与最终验收'}


def submit_repair(ws, scheme, task, formulas, datasets=(), tiers=()):
    """Apply a joint candidate only after the selected counterexample is rechecked."""
    from bonus_agent.agent import ToolError

    if not ws.staged or scheme not in ws.schemes:
        raise ToolError('construction.repair：须使用分阶段核算中的已有方案')
    if not (formulas or tiers) or len(formulas) > 12 or len(datasets) > 8 or len(tiers) > 6:
        raise ToolError('construction.repair：每批须提交公式或参数表，至多 12 项公式、6 张参数表和 8 项映射')
    candidate = copy.deepcopy(ws, {id(ws.llm): ws.llm})
    board = TaskBoard(candidate)
    original_statuses = board.statuses(with_confidence=False)
    anchor = board.task_spec(task)
    formula_tasks = [item['task'] for item in formulas]
    table_tasks = {item['task'] for item in tiers}
    if (anchor['scheme'] != scheme or task not in {*formula_tasks, *table_tasks}
            or len(set(formula_tasks)) != len(formula_tasks)):
        raise ToolError('construction.repair：公式任务须包含选中的反例任务，且每个任务仅提交一次')
    if any(board.task_spec(ident)['scheme'] != scheme for ident in formula_tasks):
        raise ToolError('construction.repair：本次候选限于同一个方案的已登记任务')
    if any(board.task_spec(ident)['target'] == '/allocation' for ident in formula_tasks) and board.phase() != 'all':
        raise ToolError('construction.repair：收到本版总额确认后再修改人员分配任务')
    prior_flow = ws._flow()
    preserve_basis = [name for name in ws.schemes if prior_flow.current('basis:' + name)]
    base = fingerprint((ws.datasets, ws.mapping, ws.drafts, ws.facts, board.state['tasks'],
                        ws.workflow.get('scheme_datasets', {})))
    result = {'task': task, 'scheme': scheme, 'target': anchor['target'],
              'status': 'FAIL', 'saved': False, 'checked': []}

    def reject(stage, issues):
        return {**result, 'stage': stage, 'issues': issues[:6],
                'next': '按本次反例修正相关映射或公式后重提；原工作区未替换'}

    try:
        candidate._check_source_version()
        staged_datasets = []
        for index, item in enumerate(datasets, 1):
            try:
                _stage_dataset(candidate, item)
                staged_datasets.append(item['args']['name'])
            except (KeyError, TypeError, ValueError, ToolError) as exc:
                args = item.get('args', {})
                name = args.get('name', '') if isinstance(args, dict) else ''
                return reject('mapping', [f'第 {index} 项映射 {name}：{exc}'])
        membership = candidate.workflow.get('scheme_datasets', {}).get(scheme)
        if membership is not None and staged_datasets:
            # The mappings explicitly supplied with this proposal belong to
            # the selected scheme. Keep other schemes' memberships intact.
            candidate.set_scheme_datasets(scheme, [*membership, *staged_datasets])
        compiled_tiers = _stage_tiers(candidate, board, scheme, tiers)
        contracts = []
        for proposal in formulas:
            staged = dict(proposal)
            if staged.get('changes') and staged.get('value') in ({}, []):
                staged.pop('value')
            elif staged.get('value') is not None and staged.get('changes') == {}:
                staged.pop('changes')
            input_contract = {key: staged.pop(key) for key in ('expected_scope', 'data_requirements', 'depends_on') if key in staged}
            if input_contract:
                spec = board.task_spec(staged['task'])
                contracts.append({**spec, **input_contract})
            staged['value'] = _submission_value(candidate, board.task_spec(staged['task']),
                                                 staged.get('value'), staged.pop('changes', None))
            _stage(candidate, board, **staged)
        if contracts:
            board.plan(contracts)
        if fingerprint((candidate.datasets, candidate.mapping, candidate.drafts, candidate.facts, board.state['tasks'],
                        candidate.workflow.get('scheme_datasets', {}))) == base:
            return reject('candidate', ['映射、公式及证据与当前版本相同'])
        preparation = candidate.prepare_data()
        if preparation['status'] != 'PASS':
            return reject('data', preparation['issues'])
        statuses = board.refresh()
        findings = board.inspect_indicators(scheme)
        anchor_parts = fx.target_parts(anchor['target'])
        if anchor_parts[0] == 'variables' and not board.task_spec(task).get('expected_scope'):
            from bonus_agent.indicator_scope import global_indicator_findings
            rules = candidate._ruleset(scheme)
            refs, missing = candidate._flow().preflight_references(scheme)
            if missing:
                return reject('execution', ['上游方案回执尚未就绪，暂不能复验当前反例'])
            data, _ = candidate._data(rules)
            observed = global_indicator_findings(
                rules, data, period=candidate.period, refs=refs, variables=(), scoped=[anchor_parts[1]])
            for name, reports in observed.items():
                findings = {**findings, name: reports}
        changed_tasks = {*formula_tasks, *compiled_tiers}
        for ident in statuses:
            status = statuses[ident]
            if ident not in changed_tasks:
                if (original_statuses.get(ident, {}).get('status') == 'PASS' and status['status'] == 'PENDING'
                        and status.get('confidence_level') == 'CONFLICT'):
                    return reject('regression', status['confidence']['counterexamples'])
                continue
            report = board.check(ident, include_next=False)
            result['checked'].append({'task': ident, 'status': report['status'], 'target': report['target']})
            if report['status'] != 'PASS':
                return {**reject('formula', report['issues']), 'failed_task': ident,
                        'repair_order': report.get('repair_order'),
                        **({'execution': report['execution']} if 'execution' in report else {})}
            parts = fx.target_parts(report['target'])
            if parts[0] == 'variables' and (ident == task or board.task_spec(ident).get('expected_scope')):
                checks = [f for f in findings.get(parts[1], ()) if f['dimension'] == 'execution']
                if not any(f['state'] == 'VERIFIED' for f in checks):
                    return {**reject('execution', [f['detail'] for f in findings.get(parts[1], ())]
                                     or ['未取得当前对象的完整求值证据']), 'failed_task': ident}
        # A formerly valid calculated basis must still work. Rechecking the candidate
        # never carries forward an approval of a different set of objects or amounts.
        if preserve_basis:
            flow = candidate._flow()
            compiled = flow.compile('basis')
            if compiled['status'] != 'PASS':
                return reject('downstream', compiled['issues'])
            for name in preserve_basis:
                candidate.calculate_basis(name)
        candidate._check_source_version()
    except (KeyError, TypeError, ValueError, ToolError) as exc:
        return reject('candidate', [str(exc)])

    statuses = board.refresh()
    required = board.required()
    pending = [n for n in statuses if n in required and statuses[n]['status'] != 'PASS']
    ws.__dict__.update(candidate.__dict__)
    return {**result, 'status': 'PASS', 'saved': True, 'pending_count': len(pending), 'pending': pending[:6],
            'next': '按新的任务检查继续；基础金额与正式人员结果仍需本版复核和确认'}


def _stage_dataset(candidate, item):
    from bonus_agent.agent import ToolError

    operation = item['operation']
    args = copy.deepcopy(item['args'])
    if operation not in ('define', 'update') or not isinstance(args, dict):
        raise ToolError('操作须为 define 或 update，args 须为对象')
    changes = args if operation == 'define' else args.get('changes')
    if not isinstance(changes, dict):
        raise ToolError('update 的 args 须含 name 和 changes 对象')
    if operation == 'update' and (unexpected := set(changes) & {'file', 'sheet', 'header_rows', 'distinct'}):
        raise ToolError(f'update 不接收 {", ".join(sorted(unexpected))}；替换物理表或去重方式时用 define，'
                        '传入 name、file、sheet、fields 以及本指标所需的完整 key/link/period/window')
    if isinstance(source := changes.get('unmatched_basis'), str):
        clause = next((c for c in candidate.clauses if c.id == source), None)
        if clause is None:
            raise ToolError(f'unmatched_basis 条款 {source} 未登记；须给已登记条款 ID 或 {{file,quote}}')
        changes['unmatched_basis'] = {'file': clause.file, 'quote': clause.quote}
    if operation == 'define':
        if 'fields' not in args and isinstance(args.get('columns'), dict) and all(
                isinstance(field, dict) and 'column' in field and 'type' in field for field in args['columns'].values()):
            args['fields'] = args.pop('columns')
        candidate.define_dataset(**args)
    else:
        candidate.update_dataset(**args)


def _stage_tiers(candidate, board, scheme, tiers):
    """Register and compile each new tier against current, physical document cells."""
    from bonus_agent.agent import ToolError

    tasks = []
    for item in tiers:
        name, ident = item['name'], item['task']
        target = '/tables/' + name.replace('~', '~0').replace('/', '~1')
        prior = board.state['tasks'].get(ident)
        if ((prior and (prior['scheme'], prior['target']) != (scheme, target))
                or any(t['id'] == ident or t['target'] == target for t in tasks)):
            raise ToolError(f'construction.repair：档位表 {name} 与任务 {ident} 的已有目标冲突')
        tasks.append({'id': ident, 'scheme': scheme, 'target': target,
                      'title': name, 'clauses': item['clauses']})
    if tasks:
        board.plan(tasks)
    for item in tiers:
        registered = candidate.record_parameter_cells(**item['register'])
        if registered['status'] != 'PASS':
            raise ToolError(f'construction.repair：{item["name"]} 有参数格未登记 ' + str(registered.get('failed', [])))
        args = copy.deepcopy(item['compile'])
        if locations := args.get('cells_from'):
            bounds = {position: value for position, _, value, upper, _ in candidate._band_cells(locations)
                      if value is not None and not upper}
            rows = args['rows']
            if all('lower' not in row or row['lower'] in bounds for row in rows):
                args['rows'] = sorted(rows, key=lambda row: bounds[row['lower']] if 'lower' in row else 0)
        candidate.compile_table(item['name'], scheme=scheme, **args)
    return [task['id'] for task in tasks]
