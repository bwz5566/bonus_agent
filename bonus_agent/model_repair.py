"""Ask an alternate model for a scoped construction repair and validate it locally."""

import json
from contextlib import nullcontext
from pathlib import Path

from bonus_agent import facts as fx
from bonus_agent.clauses import check_quote
from bonus_agent.construction import TaskBoard
from bonus_agent.failure import DIAGNOSIS_FIELDS, _recorded_feedback, question_diagnosis
from bonus_agent.llm import LLMError
from bonus_agent.recovery import fingerprint
from bonus_agent.tool_specs import TOOLS
from bonus_core.dsl.functions import describe as describe_functions


CONSTRUCTION_TOOLS = frozenset({'submit_formula_task', 'check_formula_task', 'submit_construction_repair', 'compile_formulas'})
_TABLE_ISSUES = ('construction.binding', 'construction.constant', 'algorithm.', 'parameter.')
REPAIR_SCHEMA = {
    'type': 'object', 'required': ['diagnosis', 'repair'], 'additionalProperties': False,
    'properties': {
        'diagnosis': {'type': 'object', 'properties': {key: {'type': 'string'} for key in DIAGNOSIS_FIELDS},
                      'required': list(DIAGNOSIS_FIELDS), 'additionalProperties': False},
        'repair': {'type': 'object', 'properties': {
            'formulas': {'type': 'array', 'items': {'type': 'object'}},
            'datasets': {'type': 'array', 'items': {'type': 'object'}},
            'tiers': {'type': 'array', 'items': {'type': 'object'}},
        }, 'required': ['formulas', 'datasets', 'tiers'], 'additionalProperties': False},
    },
}
PROMPT = """你是提成核算的故障修复模型。只处理证据包中的当前任务和直接相关任务。
只返回 JSON，字段名按 schema 原样输出。diagnosis 的 item 写项目或指标，identified 写已核对的事实和来源，missing 写发生失败时具体缺了什么，impact 写影响哪一步。
repair 为 {"formulas":[{"task":"已有任务ID","value":{"scope":"实际作用域","expr":"引擎表达式","sources":[{"doc":"原文件名","quote":"逐字原文"}]}}],"datasets":[],"tiers":[]}。字段格式见工具协议。程序负责数据与真实对象求值检查。
failed_attempt 是被拒绝、未保存的尝试；定位问题时参考其中的表达式和已逐字验证的 sources。registered_parameters 中有同一原文和同一公式位置的参数时，优先复用其名称和绑定目标。
如果 failed_task 指向 /tables/，只修该参数表。用已核单元格和原文，给同一任务的 tiers 编译候选，或在 formulas 里准确修改表定义和绑定。表的 value/changes 不接受 facts。已有不同费率列的适用条件需保留证据，不能借用不同列的同值参数。
如果 failed_task 指向 /report，按 required_outputs 精确提交缺少的行。用已经通过的依赖表达式，保留来源、已有分配和人员定义。有现有报表时仅用 changes 补齐需改的字段，包含该字段原有且仍适用的行。基础阶段只补 totals、details、balances 的基础金额定义。
公式使用数据包中的 DSL。当前对象的字段直接写字段名。对明细聚合写 sum(field, of: "数据集名")。禁止使用表名.字段、SQL、Python 或 Excel 函数写法。
使用原始字段、规则原文和已有数据定义。保留已有审批、数据范围和历史窗口，不把缺失或空白当成真实零值。材料缺少业务口径、规则冲突或没有足够证据时，repair 中的三个数组留空，并说明具体缺口。不得编造原文、默认值或最终金额。"""


def _target(board, statuses, steps, reason):
    specs = board.state['tasks']
    for step in reversed(steps):
        if step.ok or step.args.get('tool', step.tool) not in CONSTRUCTION_TOOLS:
            continue
        feedback = _recorded_feedback(step.result)
        ident = feedback.get('failed_task') or feedback.get('task') or step.args.get('task')
        if isinstance(ident, str) and ident in specs and statuses[ident]['status'] == 'PENDING':
            return specs[ident], step.result, step.args
    feedback = _recorded_feedback(reason)
    ident = feedback.get('failed_task') or feedback.get('task')
    if isinstance(ident, str) and ident in specs and statuses[ident]['status'] == 'PENDING':
        return specs[ident], reason, None
    candidates = [spec for ident, spec in specs.items() if statuses[ident]['status'] == 'PENDING'
                  and (f'construction:{ident}:{spec["target"]}' in reason
                       or (reason.startswith('公式构建停滞') and f'{spec["scheme"]}::{spec["target"]}' in reason))]
    if len(candidates) == 1:
        return candidates[0], reason, None
    if any(not step.ok and step.args.get('tool', step.tool) == 'compile_formulas' for step in steps):
        from bonus_agent.advice_context import build_runtime_focus_context

        runtime = build_runtime_focus_context(board.ws)
        if runtime:
            failed = runtime['failure']
            task = (failed.get('repair') or {}).get('task')
            if task in specs and specs[task]['scheme'] == runtime['scheme']:
                return specs[task], json.dumps(failed, ensure_ascii=False, default=str), None
    return None, '', None


def _failed_attempt(args, task, corpus):
    if not args:
        return {}
    if isinstance(args.get('formulas'), list):
        args = next((f for f in args['formulas'] if isinstance(f, dict) and f.get('task') == task), {})
    if args.get('task') != task:
        return {}
    parts = args.get('value') or args.get('changes')
    if not isinstance(parts, dict):
        return {}
    result = {key: parts[key] for key in ('scope', 'expr') if isinstance(parts.get(key), str)
              and len(parts[key]) <= 3200}
    sources = parts.get('sources')
    result['sources'] = [{'doc': s['doc'], 'quote': s['quote']} for s in (sources[:8] if isinstance(sources, list) else ())
                         if isinstance(s, dict) and isinstance(s.get('doc'), str) and isinstance(s.get('quote'), str)
                         and 0 < len(s['quote']) <= 450 and not check_quote(s['doc'], s['quote'], corpus)]
    return result


def _packet(ws, board, spec, statuses, evidence, submitted=None):
    scheme, ident = spec['scheme'], spec['id']
    dependents = [s for s in board.state['tasks'].values() if s['scheme'] == scheme and ident in s['depends_on']]
    consumers = [s for s in dependents if s['target'].startswith('/variables/') and statuses[s['id']]['status'] != 'PASS'][:4]
    deps = [board.task_spec(task) for task in statuses[ident]['depends_on'][:8]]
    cited = {cid for task in [spec, *deps, *consumers] for cid in task['clauses']}
    corpus = ws._corpus()
    attempt = _failed_attempt(submitted, ident, corpus)
    source_pairs = {(s['doc'], fx.norm(s['quote'])) for s in attempt.get('sources', ())}
    clauses = [{'id': c.id, 'file': c.file, 'quote': c.quote} for c in ws.clauses
               if (c.id in cited or (c.file, fx.norm(c.quote)) in source_pairs)
               and len(c.quote) <= 1500 and not check_quote(c.file, c.quote, corpus)]
    clauses.sort(key=lambda c: (c['file'], fx.norm(c['quote'])) not in source_pairs)
    value = board.value(spec)
    needed = {r['dataset'] for task in [spec, *deps, *consumers] for r in task.get('data_requirements', ())}
    focused = json.dumps([spec, value, attempt, evidence], ensure_ascii=False, default=str)
    rules = ws._ruleset(scheme)
    scope = spec.get('expected_scope') or (value or {}).get('scope')
    level = rules.level(scope)
    owner = rules.entity_dataset(level.key) if level else ((scope, rules.datasets[scope]) if scope in rules.datasets else None)
    if owner:
        needed.add(owner[0])
    datasets = []
    for name, ds in rules.datasets.items():
        if name not in ws.mapping:
            continue
        mapping = ws.mapping[name]
        match = name in needed or name in focused or name == spec.get('expected_scope')
        rank = (0 if match else 1, len(datasets))
        row = {'dataset': name, 'file': Path(mapping.file).name, 'sheet': mapping.sheet,
               'key': ds.key, 'link': ds.link, 'period': ds.period, 'window': ds.window}
        if match:
            row['fields'] = {field: {'column': column, 'type': ds.fields[field].type,
                                     'if_empty': ds.fields[field].if_empty}
                             for field, column in list(mapping.columns.items())[:25] if field in ds.fields}
        datasets.append((rank, row))
    data = [row for _, row in sorted(datasets)[:25]]
    protocol = next(t['parameters'] for t in TOOLS if t['name'] == 'submit_construction_repair')
    cells = []
    for fact in ws.facts:
        relevant = ((fact.file, fx.norm(fact.quote)) in source_pairs or fact.name in spec['facts']
                    or any(board.owns(spec, t) for t in fact.targets))
        if relevant and not check_quote(fact.file, fact.quote, corpus):
            cells.append({'name': fact.name, 'file': fact.file, 'page': fact.page,
                          'cell': fact.source_cell, 'quote': fact.quote[:250], 'value': fact.value,
                          'unit': fact.unit, 'targets': fact.targets})
    cells.sort(key=lambda f: (f['file'], fx.norm(f['quote'])) not in source_pairs)
    return {'period': str(ws.period) if ws.period else None, 'failed_task': spec, 'failure': evidence[:2600], 'value': value,
            'dsl_functions': describe_functions(),
            'scope_source': {'scope': scope, 'dataset': owner[0], 'key': owner[1].key} if owner else None,
            'data_check': {'current_pass': ws._flow().current('data'),
                           'meaning': '已映射的字段和原表位置可用于提议候选。提交后程序会按当前期间和实际对象验算，金额独立复核在后续阶段。'},
            'dependencies': [{'task': s, 'saved': board.value(s)} for s in deps],
            'related_tasks': consumers, 'verified_clauses': clauses[:12], 'datasets': data,
            **({'failed_attempt': attempt} if attempt else {}),
            **({'registered_parameters': cells[:24]} if cells else {}),
            **({'report_phase': board.phase(), 'required_outputs': [r for r in board.output_obligations(board.phase())
                   if r['scheme'] == scheme and r['task_target'] == '/report']} if spec['target'] == '/report' else {}),
            'submission_protocol': {key: protocol['properties'][key] for key in ('formulas', 'datasets', 'tiers')}}


def run(ws, journal, model, steps, reason):
    """One candidate per task version. Model output is never a successful receipt."""
    from bonus_agent.agent import ToolError

    if not model or not ws.staged or ws.pending or ws.done or ws._flow().stage()[0] != 3:
        return None
    budget = getattr(model, 'run_budget', None)
    if budget is not None and budget.optional_timeout(model):
        return None
    if not reason and not any(not step.ok and step.args.get('tool', step.tool) in CONSTRUCTION_TOOLS for step in steps):
        return None
    board = TaskBoard(ws)
    statuses = board.statuses(with_confidence=False)
    spec, evidence, submitted = _target(board, statuses, steps, reason)
    if not spec or not (spec['target'].startswith(('/variables/', '/tables/')) or spec['target'] == '/report'):
        return None
    table = spec['target'].startswith('/tables/')
    report = spec['target'] == '/report'
    issues = _recorded_feedback(evidence).get('issues', [])
    if table and not any(str(issue).startswith(_TABLE_ISSUES) for issue in issues):
        return None  # A tool-shape error or missing upstream task already has native guidance.
    if report and not any(str(issue).startswith('construction.report_output') for issue in issues):
        return None
    ident, scheme = spec['id'], spec['scheme']
    attempt = fingerprint((scheme, spec['target'], statuses[ident]['version'], model.model))
    attempts = journal.state.setdefault('model_repairs', {})
    if attempt in attempts:
        return None
    result = {'status': 'FAIL', 'saved': False, 'scheme': scheme, 'task': ident, 'target': spec['target'],
              'model': model.model, 'executor': ws.llm.model}
    attempts[attempt] = dict(result, status='STARTED')
    journal.save()
    try:
        ws._check_source_version()
        packet = _packet(ws, board, spec, statuses, evidence, submitted)
        context = json.dumps(packet, ensure_ascii=False, default=str)
        if len(context) > 24000:
            raise ValueError('当前任务证据包超出修复模型的局部输入上限，需先缩小失败范围。')
        with budget.optional_request(handoff_model=model) if budget is not None else nullcontext():
            raw = model.chat_json([{'role': 'system', 'content': PROMPT}, {'role': 'user', 'content': context}],
                                  schema=REPAIR_SCHEMA, think=True, max_tokens=8192)
        if not isinstance(raw, dict) or 'repair' not in raw:
            raise ValueError('修复模型未提供可校验的结构化候选')
        diagnosis = question_diagnosis(raw.get('diagnosis'))
        if diagnosis is None:
            check = _recorded_feedback(evidence)
            identified = next(iter(check.get('issues', [])), evidence.splitlines()[0] if evidence else spec['target'])
            diagnosis = {'item': f'{scheme} / {spec.get("title") or spec["target"]} / {ident}',
                         'identified': str(identified)[:550],
                         'missing': '失败时该任务尚未取得当前数据版本的通过回执。',
                         'impact': '候选通过真实对象求值检查后，才能继续下游计算。'}
            result['diagnosis_source'] = 'program_feedback'
        result['diagnosis'] = diagnosis
        if raw.get('repair') is None:
            result['issues'] = [diagnosis['missing']]
        else:
            repair = raw['repair']
            allowed = {ident} if table or report else {ident, *(s['id'] for s in packet['related_tasks'])}
            if (not isinstance(repair, dict) or set(repair) - {'formulas', 'datasets', 'tiers'}
                    or not isinstance(repair.get('formulas'), list)
                    or any(not isinstance(item, dict) or not isinstance(item.get('task'), str)
                           or item['task'] not in allowed for item in repair['formulas'])
                    or any(not isinstance(repair.get(key, []), list) for key in ('datasets', 'tiers'))):
                raise ValueError('修复候选超出当前任务或不符合联合提交协议')
            if table and (repair.get('datasets') or any(not isinstance(item, dict) or item.get('task') != ident
                                                       for item in repair.get('tiers', []))):
                raise ValueError('本次参数表候选只能修改当前表，不能改数据映射或其他任务')
            if repair['formulas'] or repair.get('tiers'):
                if report:
                    _check_report_candidate(board, spec, repair)
                checked = ws.submit_construction_repair(scheme, ident, **repair)
                result.update(checked)
            else:
                result['issues'] = [diagnosis['missing']]
    except (LLMError, ToolError, ValueError, TypeError, KeyError) as exc:
        result['issues'] = [str(exc)[:900]]
        if isinstance(exc, LLMError) and budget is not None and (timeout := budget.optional_timeout(model)):
            result.update(status='DEFERRED', timeout=timeout)
        result.setdefault('diagnosis', {'item': spec.get('title') or spec['target'],
                                       'identified': '程序已定位失败任务 ' + ident + '。' + result['issues'][0],
                                       'missing': '当前任务尚缺通过校验的修复候选。',
                                       'impact': '该任务的后续计算仍需原有校验。'})
    attempts[attempt] = result.copy()
    journal.save()
    return result


def _check_report_candidate(board, spec, repair):
    if repair.get('datasets') or repair.get('tiers') or len(repair['formulas']) != 1:
        raise ValueError('本次候选仅能修复当前报表，参数表和数据映射须单独校验')
    proposal, = repair['formulas']
    saved = board.ws._merged(spec['scheme']).get('report') or {}
    updates = proposal.get('changes') if saved else proposal.get('value')
    if not isinstance(updates, dict) or (saved and proposal.get('value') is not None):
        raise ValueError('已有报表须用 changes 局部更新；新报表须用 value 比对交付项')
    if board.phase() != 'basis':
        return
    basis_rows = {'totals', 'details', 'balances'}
    if set(updates) - (basis_rows | {'sources'}):
        raise ValueError('总额确认前的候选仅可修改基础金额报表字段')
    for field in basis_rows & updates.keys():
        rows = updates[field]
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError(f'报表 {field} 须提供完整行列表')
        other_phase = [row for row in saved.get(field, []) if row.get('stage', 'basis') != 'basis']
        if [row for row in rows if row.get('stage', 'basis') != 'basis'] != other_phase:
            raise ValueError('总额确认前须保留既有人员分配阶段的报表行')
