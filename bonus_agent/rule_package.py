"""Submit one evidence-bound construction artifact as a single transaction."""
from __future__ import annotations

import copy

from bonus_agent import facts as fx
from bonus_agent.construction import TaskBoard


def submit(ws, task: str, value, bindings=None, facts=None):
    from bonus_agent.agent import ToolError

    candidate = copy.deepcopy(ws, {id(ws.llm): ws.llm})
    board = TaskBoard(candidate)
    spec = board.state['tasks'].get(task)
    if spec is None:
        raise ToolError('construction.task：任务不存在，先用plan_formula_tasks声明目标、依赖和来源')
    parts = fx.target_parts(spec['target'])
    if value is None or not isinstance(value, (dict, list)):
        raise ToolError('construction.package：value须为当前目标的完整对象或列表；删除组件使用set_rules')
    payload = {parts[0]: {parts[1]: value}} if len(parts) == 2 else {parts[0]: value}
    try:
        candidate._check_source_version()
        if facts:
            candidate.record_facts(facts)
            if candidate._fact_rejections:
                raise ToolError('construction.evidence：' + '；'.join(candidate._fact_rejections))
            if candidate.pending:
                raise ToolError('construction.evidence：参数存在未解决冲突；先登记冲突并依据原文或财务答复解决')
        # The caller submits this artifact's bindings only. Preserve other targets in code,
        # avoiding repeated model reads/copies of a parameter's entire binding ledger.
        existing = {f.name: set(f.targets) for f in ws.facts}
        merged_bindings = []
        for binding in bindings or []:
            added = set(binding.get('targets', [])) - existing.get(binding.get('name'), set())
            if any(not board.owns(spec, target) for target in added):
                raise ToolError('construction.binding：新增绑定必须指向当前任务；其他组件请分别提交')
            kept = {target for target in existing.get(binding.get('name'), set()) if not board.owns(spec, target)}
            merged_bindings.append({**binding, 'targets': sorted(kept | set(binding.get('targets', [])))})
        candidate.set_rules(scheme=spec['scheme'], bindings=merged_bindings, **payload)
        report = board.check(task)
        if report['status'] != 'PASS':
            return {**report, 'saved': False,
                    'next': '按issues修正当前规则包后重新submit_formula_task；原规则、证据及通过回执未替换'}
    except ToolError as exc:
        return {'status': 'FAIL', 'task': task, 'target': spec['target'], 'saved': False,
                'issues': [str(exc)],
                'next': '修正当前规则包后重新submit_formula_task；未保存部分公式或部分证据'}
    ws.__dict__.update(candidate.__dict__)
    return {**report, 'saved': True,
            'scope': '当前构件及其前置条件检查通过；金额仍须阶段编译、独立核算与最终验收'}
