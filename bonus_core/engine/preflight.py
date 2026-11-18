"""Deterministic basis execution diagnostics, without results or approvals.

This uses the same effective population and evaluator as the real calculation.
It neither fixes rules nor drops rows, and does not expose a reusable basis
result.  A PASS means the current inputs execute, not that policy is approved.
"""
from __future__ import annotations

from bonus_core.data.table import Datasets
from bonus_core.dsl.models import RuleSet
from bonus_core.engine.evaluator import EvalError
from bonus_core.engine.issues import EngineError
from bonus_core.engine.run import Engine


def preflight_basis(rs: RuleSet, data: Datasets, refs: dict | None = None, period=None) -> dict:
    """Evaluate all basis roots once in an isolated, non-distributing context.

    ``refs`` must be already available results supplied by the caller.  This
    function never computes referenced schemes, invokes a model, or writes a
    receipt. Missing prerequisites fail rather than being manufactured.
    """
    engine = None
    phase = 'prepare'
    try:
        engine = Engine.prepare_basis(rs, data, refs=refs, period=period)
        phase = 'evaluate'
        engine.calculate_basis()  # Discard amounts/traces; this is only a preflight.
    except EvalError as exc:
        issues = [_evaluation_issue(exc, engine)]
    except EngineError as exc:
        issues = [_engine_issue(i.to_dict(), engine) for i in exc.issues[:8]]
        return _report('FAIL', phase, engine, issues, issue_count=len(exc.issues))
    except Exception as exc:
        # Fail closed on implementation/data errors outside the normal engine
        # exception contract. Do not mislabel an unexecuted preflight as PASS.
        issues = [{
            'severity': 'blocking', 'code': 'basis_preflight_internal',
            'message': f'{type(exc).__name__}: {exc}', 'where': '',
            'repair': ['保留当前规则与原始数据，检查此异常的引擎实现；不能用默认金额绕过。'],
        }]
    else:
        warnings = [i.to_dict() for i in engine.issues if i.severity == 'warning']
        report = _report('PASS', phase, engine, [])
        report['warnings'] = warnings[:8]
        report['warning_count'] = len(warnings)
        return report
    return _report('FAIL', phase, engine, issues)


def _report(status, phase, engine, issues, *, issue_count=None):
    report = {
        'status': status, 'purpose': 'deterministic_basis_preflight',
        'phase': phase, 'issues': issues,
        'issue_count': len(issues) if issue_count is None else issue_count,
        'note': '仅检查当前基础公式能否在真实数据上执行；不代表金额确认、业务规则审核或人员分配通过。',
    }
    if engine is not None:
        objects = engine._report_nodes(engine.rs.pool.scope)
        report['summary'] = {
            'dataset_rows': {name: len(table.rows) for name, table in engine.data.items()},
            'pool_scope': engine.rs.pool.scope,
            'candidate_objects': len(objects),
            'completed_expression_traces': len(engine.ev.traces),
        }
        # Progress evidence contains identities only, never computed money.
        # A truncated list may prove an included success, never an absence.
        # evaluate() may append a scalar trace before rejecting its numeric
        # result contract. Explicit failure frames must not become a success.
        failed = {(frame.get('node_id'), frame.get('rule_item')) for issue in issues
                  for frame in [issue, *issue.get('dependency_chain', [])]
                  if frame.get('node_id') and frame.get('rule_item')}
        failed_places = {issue.get('where') for issue in issues if issue.get('where')}
        identities = list(dict.fromkeys((t.node_id, t.rule_item) for t in engine.ev.traces.values()
                                       if (t.node_id, t.rule_item) not in failed
                                       and t.id not in failed_places and t.rule_item not in failed_places))
        report['execution_evidence'] = {
            'successful': [{'node_id': node, 'rule_item': item} for node, item in identities[:128]],
            'successful_complete': len(identities) <= 128,
            'successful_scope': 'expression_evaluated_only',
            'object_ids': [node.id for node in objects[:128]],
            'objects_complete': len(objects) <= 128,
            'object_scope': engine.rs.pool.scope,
        }
        # A scalar trace can exist before its amount/check policy rejects it.
        # Only a completed basis execution certifies the current execution
        # contracts. This is never approval of business policy or amounts.
        policies = _policy_successes(engine, objects) if status == 'PASS' else []
        report['execution_evidence'].update(
            policy_successful=policies[:128],
            policy_successful_complete=len(policies) <= 128,
            # A warning predicate may remain false despite overall PASS. Do
            # not let changing its severity manufacture proof it now passed.
            policy_checks_complete=status == 'PASS' and not any(i.code == 'check_failed' for i in engine.issues),
            policy_success_scope='current_execution_contracts_only_not_business_or_amount_approval',
        )
    return report


def _target(engine, item):
    """Canonical component roots; projected check indexes are not source indexes."""
    if item == 'pool':
        return '/pool'
    if item in engine.rs.variables:
        return '/variables/' + item.replace('~', '~0').replace('/', '~1')
    if any(item == 'checks.' + check.id for check in engine.rs.checks):
        return '/checks'
    if item in ('report.details', 'report.balances', 'report.totals'):
        return '/' + item.replace('.', '/')
    if item == 'basis':
        return '/report/totals'
    return ''


def _policy_successes(engine, objects):
    """Use real executed roots, never similarly named variable traces."""
    identities = []
    seen = set()

    def add(node, item, target, trace_ids):
        traces = [engine.ev.traces.get(key) for key in trace_ids]
        if traces and all(t is not None and t.rule_item == item and t.node_id == node.id for t in traces):
            entry = {'node_id': node.id, 'rule_item': item, 'target': target,
                     'evidence_kind': 'execution_contract_passed'}
            key = (node.id, item, target)
            if key not in seen:
                seen.add(key)
                identities.append(entry)

    for node in objects:
        add(node, 'pool', '/pool', [f'{node.id}/奖金池'])
    for check in engine.rs.checks:
        for node in engine._report_nodes(check.scope):
            key = f'{node.id}/校验/{check.id}'
            trace = engine.ev.traces.get(key)
            # A failed warning did execute, but its predicate did not pass.
            if trace is not None and trace.value == 'True':
                add(node, 'checks.' + check.id, '/checks', [key])
    for balance in engine.rs.report.balances if engine.rs.report else []:
        if balance.stage != 'basis':
            continue
        for node in engine._report_nodes(balance.level):
            add(node, 'report.balances', '/report/balances',
                [f'{node.id}/对账/{balance.name}/{i}' for i in range(1 + len(balance.parts))])
    return identities


def _evaluation_issue(exc, engine):
    frames = exc.frames
    first = frames[0] if frames else {}
    issue = {
        'severity': 'blocking', 'code': 'basis_preflight_eval',
        'message': str(exc), 'where': exc.where,
        'node_id': first.get('node_id', ''),
        'source': first.get('source', ''),
        'formula': first.get('expr', ''),
        'rule_item': first.get('rule_item', ''),
        'dependency_chain': frames,
        'coverage': [dict(item) for item in exc.aggregates],
    }
    if engine is not None:
        if target := _target(engine, issue['rule_item']):
            issue['target'] = target
        issue['evidence_kind'] = 'expression_failure'
        for item in issue['coverage']:
            name = item['dataset']
            table = engine.data.get(name)
            item['dataset_rows'] = len(table.rows) if table is not None else None
            spec = engine.rs.datasets.get(name)
            if spec is not None:
                item['relationship'] = {
                    'key': spec.key, 'link': spec.link,
                    'population_from': spec.population_from,
                }
    empty = next((item for item in issue['coverage'] if item.get('selected_rows') == 0), None)
    if empty is not None:
        if empty['visible_rows']:
            issue['repair'] = [
                f'对象有 {empty["visible_rows"]} 行「{empty["dataset"]}」，但 where 筛选后为 0；核对筛选字段、单位和期间。',
                '若材料明确允许空集合，按材料补充条件分支；不得凭报错把 min/max/avg 改成 sum 或默认零。',
            ]
        else:
            issue['repair'] = [
                f'从对象来源行核对其是否属于本次核算范围，再检查「{empty["dataset"]}」的关联键和期间映射。',
                '本期核算对象与历史参考数据应分别表达；如需调整 population_from 或关系，保留公式需要的历史行。',
                '只有原文明确允许缺行时才补空集合分支；不得任意删对象或用零替代缺失依据。',
            ]
    else:
        issue['repair'] = [
            '按 source / active_row_source 回查失败对象及明细，再检查 formula 的字段类型、分母、关联与引用方案。',
            '修改映射或公式后重新执行基础预检；缺少业务口径时应补依据，不能把失败改为通过。',
        ]
    return issue


def _engine_issue(issue, engine):
    if engine is None:
        return issue
    where = issue.get('where', '')
    nodes = list(engine.nodes.values()) + [n for table in (engine.root.index or {}).values() for n in table.values()]
    node = next((n for n in sorted(nodes, key=lambda n: len(n.id), reverse=True)
                 if where == n.id or where.startswith(n.id + '/')), None)
    if node is not None:
        issue.update(node_id=node.id, node_scope='global' if node is engine.root else node.level,
                     source=str(node.attr_src) if node.attr_src else '')
    if issue['code'] == 'pool_negative':
        # The global pool rejection has no `where`; its root is nevertheless
        # the real global node. Never assign a guessed object for other scopes.
        if node is None and not where and engine.rs.pool.scope == 'global':
            node = engine.root
            issue.update(node_id=node.id, node_scope='global', source=str(node.attr_src) if node.attr_src else '')
        if node is not None:
            issue.update(rule_item='pool', target='/pool', evidence_kind='policy_failure',
                         formula=engine.rs.pool.expr)
    elif issue['code'] in ('report_unbalanced', 'report_balance_eval') and node is not None:
        issue.update(rule_item='report.balances', target='/report/balances', evidence_kind='policy_failure')
    if issue['code'] == 'check_failed':
        failed = [t for t in engine.ev.traces.values() if t.rule_item == where and t.value == 'False']
        by_id = {n.id: n for n in nodes}
        issue['failed_objects'] = len(failed)
        issue['examples'] = [{
            'node_id': trace.node_id, 'formula': trace.expr,
            'source': str(by_id[trace.node_id].attr_src) if by_id.get(trace.node_id) and by_id[trace.node_id].attr_src else '',
            'rows': list(trace.rows[:3]),
        } for trace in failed[:3]]
        if failed and any(where == 'checks.' + check.id for check in engine.rs.checks):
            first = issue['examples'][0]
            issue.update(node_id=first['node_id'], source=first['source'], formula=first['formula'],
                         rule_item=where, target='/checks', evidence_kind='policy_failure')
            failed_node = by_id.get(first['node_id'])
            if failed_node is not None:
                issue['node_scope'] = 'global' if failed_node is engine.root else failed_node.level
    return issue
