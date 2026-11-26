"""Bounded task context; authoritative rules and raw rows stay in workspace tools."""

import difflib
import json
import math
import re

from bonus_agent.llm import LLMError


class ContextBudgetError(LLMError):
    """A local estimate/policy decision; never evidence of a provider rejection."""


_PIECES = re.compile(r'[A-Za-z_]+|[0-9]+|[ \t]+|\r?\n|[^\x00-\x7f]|.', re.S)


def estimate_tokens(value):
    """Conservative multilingual estimate, not bytes or a provider tokenizer count."""
    text = json.dumps(value, ensure_ascii=False, default=str, separators=(',', ':'))
    total = 0.0
    for match in _PIECES.finditer(text):
        part = match.group()
        if part[0].isascii():
            if part[0].isalnum() or part[0] == '_':
                total += math.ceil(len(part) / 3)
            elif part[0] in ' \t':
                total += math.ceil(len(part) / 4)
            else:
                total += 1
        else:
            total += 3 if ord(part) > 0xFFFF else 1.5
    return math.ceil(total)


def guard_messages(messages, llm, *, output_tokens=8192, tools=None):
    maximum = llm.context_window()
    baseline = estimate_tokens(messages) + (estimate_tokens(tools) if tools else 0)
    estimate = math.ceil(baseline * max(1.0, getattr(llm, '_context_token_ratio', 0) * 1.2))
    budget = getattr(llm, 'max_input_tokens', 96000)
    if estimate > budget or (maximum and estimate + output_tokens > maximum):
        raise ContextBudgetError(
            f'本地上下文预算拦截（估算，非服务端超限）：估算输入 {estimate} tokens，配置输入预算 {budget}，'
            f'服务声明窗口 {maximum or "未知"}，预留输出 {output_tokens}。需整理任务上下文或调整预算；不能推断材料缺失'
        )
    llm._pending_context_estimate = baseline
    return estimate


# Each read_workspace call re-sends the whole prompt, so a page should carry as much as the runner's
# result cap allows (agent.MAX_RESULT_CHARS = 6000, minus room for notes the runner appends).
PAGE_CHARS = 5400
FRAGMENT_CHARS = 3800  # raw JSON text; escaping inside the result makes it longer
# Listing views of the ledger: enough to pick a name and see what it means. Full records (quote, source
# coordinates, hashes) are ~4x larger and made a 135-fact ledger unreadable within the history budget.
_BRIEF = {'facts': ('name', 'value', 'unit', 'page', 'context', 'meaning'),
          'clauses': ('id', 'file', 'point', 'where'),
          'construction': ('id', 'scheme', 'target', 'title', 'status', 'confidence_level',
                           'depends_on', 'planned_depends_on', 'missing_pool')}


def _brief(section, item):
    out = {k: item[k] for k in _BRIEF[section] if item.get(k) not in (None, '', [])}
    if section == 'facts':
        name = str(item.get('name', ''))
        if not name.startswith(f"{item.get('file')}::"):
            out['file'] = item.get('file')  # coordinate names already carry the file
        elif f"::P{item.get('page')}." in name:
            out.pop('page', None)  # ...and the page
        value, meaning = str(item.get('value', '')), str(out.get('meaning', ''))
        if value and meaning.endswith(f' | {value}'):
            out['meaning'] = meaning[: -len(value) - 3]  # the value is already shown
        if item.get('targets'):
            out['bound'] = len(item['targets'])
    elif section == 'construction':
        out['fact_count'] = len(item.get('facts') or [])
        out['clause_count'] = len(item.get('clauses') or [])
        issues = (item.get('report') or {}).get('issues') or []
        if issues:
            out['issue_count'] = len(issues)
    return out


def _drop_empty(value):
    """Mapping definitions repeat unit/values/if_empty: null for every field; absent means the same."""
    if isinstance(value, dict):
        return {k: _drop_empty(v) for k, v in value.items() if v is not None and v != {} and v != []}
    if isinstance(value, list):
        return [_drop_empty(v) for v in value]
    return value


def tool_result(ws, tool, value, limit=5400):
    """Keep large structured feedback as valid JSON with a persistent, pageable full result."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    from bonus_agent.recovery import fingerprint
    # Serialize once: stored Decimal/date values must replay exactly as they were shown.
    stored = json.loads(text)
    ws.workflow.setdefault('tool_reports', {})[tool] = stored
    result = {'tool': tool, 'status': value.get('status', 'READ') if isinstance(value, dict) else 'READ',
              'digest': fingerprint(stored), 'notice': '完整结果已保存，按next_call读取；不重复执行原工具。',
              'next_call': {'tool': 'read_workspace', 'args': {'section': 'tool_results', 'name': tool, 'limit': 10}}}
    if isinstance(value, dict):
        result['counts'] = {k: len(v) for k, v in value.items() if isinstance(v, list)}
    return json.dumps(result, ensure_ascii=False)


def _bounded_page(result):
    if len(json.dumps(result, ensure_ascii=False, default=str)) > 5800:
        return {'section': result['section'], 'status': 'QUERY_TOO_LARGE', 'items': [],
                'notice': '查询名称或过滤参数过长，无法容纳完整分页回执；缩短query/name，或按file/page分批查询。原记录未截断。'}
    return result


def _pointer(parts):
    return '/' + '/'.join(str(p).replace('~', '~0').replace('/', '~1') for p in parts)


def _rule_bindings(ws, scheme, parts):
    """Show existing bindings only; equal numeric values never create a binding."""
    from bonus_agent.facts import split_target, target_parts
    out = []
    for fact in getattr(ws, 'facts', []):
        targets = []
        for target in fact.targets:
            qualifier, path = split_target(target)
            bound = target_parts(path)
            if qualifier not in (None, scheme) or bound[:len(parts)] != parts:
                continue
            targets.append(target)
        if targets:
            out.append({'name': fact.name, 'file': fact.file, 'value': fact.value,
                        'targets': targets[:5], 'targets_complete': len(targets) <= 5,
                        'next_call': {'tool': 'read_workspace', 'args': {'section': 'facts', 'name': fact.name}}})
    return out


def _rule_index(ws, scheme, parts, value):
    """Index immediate children with executable read calls, without serializing every rule."""
    children = value.items() if isinstance(value, dict) else enumerate(value)
    result = []
    for key, child in children:
        path = [*parts, str(key)]
        item = {'scheme': scheme, 'name': str(key), 'target': f'{scheme}::{_pointer(path)}',
                'kind': 'object' if isinstance(child, dict) else 'array' if isinstance(child, list) else 'value',
                'binding_count': len(_rule_bindings(ws, scheme, path)),
                'next_call': {'tool': 'read_workspace',
                              'args': {'section': 'rules', 'scheme': scheme, 'name': _pointer(path)}}}
        if isinstance(child, (dict, list)):
            item['entries'] = len(child)
        # A short coefficient matrix, its bounds and scalar policies fit in the
        # index. Keep the existing pointers for evidence and larger definitions.
        if parts and parts[0] == 'tables' and len(parts) >= 2 and not isinstance(child, dict):
            if not isinstance(child, list) or all(not isinstance(v, dict) for v in child):
                if len(json.dumps(child, ensure_ascii=False, default=str)) <= 460:
                    item['value'] = child
        if isinstance(child, dict):
            item.update({k: child[k] for k in ('scope', 'level', 'kind') if isinstance(child.get(k), str)})
        result.append(item)
    return result


def _rule_node(ws, scheme, parts, value):
    bindings = _rule_bindings(ws, scheme, parts)
    return {'scheme': scheme, 'name': _pointer(parts), 'target': f'{scheme}::{_pointer(parts)}',
            'definition': value, 'bindings': bindings[:8], 'binding_count': len(bindings),
            'bindings_complete': len(bindings) <= 8}


def workspace_view(
    ws,
    section,
    scheme=None,
    offset=0,
    limit=20,
    name=None,
    query=None,
    file=None,
    page=None,
    detail_offset=None,
    detail=False,
):
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or (detail_offset is not None and (type(detail_offset) is not int or detail_offset < 0))
    ):
        from bonus_agent.workflow import fail

        fail('读取位置offset/detail_offset须为非负整数，limit须为整数')
    requested_limit = limit
    limit = max(1, min(limit, 50))  # Read-only sizing is safe to normalize, never a business correction.
    notes = []
    rule_index = False
    pointer = None
    if section == 'rules' and isinstance(name, str) and (name.startswith('/') or '::/' in name):
        from bonus_agent.facts import split_target
        from bonus_agent.workflow import fail
        qualifier, pointer = split_target(name)
        if qualifier is not None:
            if qualifier not in ws.schemes or (scheme is not None and qualifier != scheme):
                fail('规则指针的方案不存在或与scheme不一致；须指定同一真实方案，不能改读其他方案')
            scheme = qualifier
        if scheme is not None and scheme not in ws.schemes:
            fail('规则指针指定的scheme不存在，不能改读其他方案')
    if scheme and scheme not in ws.schemes and section in ('rules', 'variables', 'tables', 'basis'):
        # 只读：方案名写错（如共用部分已合并进各方案的「共用」）不报错，改读全部方案，免得反复撞同一个错被判卡住
        notes.append(f'没有方案「{scheme}」（共用部分已合并进每个方案），已改为读全部方案 {ws.schemes}')
        scheme = None
    if section == 'rules' and name is not None and name not in ws.schemes:
        # rules 的 name 只认方案名；写的是表或变量名时直接给那张表/变量
        hit = next((sec for sec in ('tables', 'variables') for s in ws.schemes if name in (ws._merged(s).get(sec) or {})), None)
        if hit:
            notes.append(f'「{name}」是{"表" if hit == "tables" else "变量"}，不是方案；已按 section={hit} 给出')
            section = hit
    if section == 'facts':
        items = [f.model_dump(mode='json') for f in ws.facts]
    elif section == 'clauses':
        items = [c.model_dump(mode='json') for c in ws.clauses]
    elif section == 'rules':
        items = [{'scheme': s, 'rule': ws._merged(s)} for s in ([ws._scheme(scheme)] if scheme else ws.schemes)]
        # A component query must not return fragments starting at the beginning of the entire rule.
        from bonus_agent.construction import ROOTS
        component = name if name is not None and name not in ws.schemes else query
        if pointer is not None:
            from bonus_agent.facts import target_parts, target_value
            selected = []
            parts = target_parts(pointer)
            for item in items:
                try:
                    value = target_value(item['rule'], pointer)
                except (KeyError, IndexError, TypeError, ValueError):
                    notes.append(f'{item["scheme"]}::{pointer} 不存在；按规则组件索引核对真实名称和列表索引')
                    continue
                node = _rule_node(ws, item['scheme'], parts, value)
                if (not detail and detail_offset is None and isinstance(value, (dict, list))
                        and ((len(parts) == 1 and parts[0] in ROOTS - {'pool'})
                             or isinstance(value, list)
                             or len(json.dumps(node, ensure_ascii=False, default=str)) > PAGE_CHARS // 2)):
                    selected += _rule_index(ws, item['scheme'], parts, value)
                    rule_index = True
                else:
                    selected.append(node)
            items = selected
            # The pointer was resolved above; it is not a literal child name filter.
            name = pointer
        elif (name is None and query is None and not detail and detail_offset is None
              and any(set(item['rule']) & ROOTS for item in items)):
            items = [entry for item in items for entry in _rule_index(ws, item['scheme'], [], item['rule'])]
            rule_index = True
        elif component in ROOTS:
            items = [{'scheme': item['scheme'], 'name': component, 'definition': item['rule'].get(component)} for item in items]
            query = component
            name = None
        elif component and any(component in (item['rule'].get(root) or {}) for item in items for root in ('variables', 'tables')):
            items = [{'scheme': item['scheme'], 'name': component, 'definition': item['rule'][root][component]}
                     for item in items for root in ('variables', 'tables') if component in (item['rule'].get(root) or {})]
            query = component
            name = None
    elif section in ('variables', 'tables'):
        items = [
            {'scheme': s, 'name': n, 'definition': v}
            for s in ([ws._scheme(scheme)] if scheme else ws.schemes)
            for n, v in (ws._merged(s).get(section) or {}).items()
            if name is None or n == name
        ]
    elif section == 'mapping':
        items = [
            _drop_empty({'dataset': n, 'definition': d.model_dump(mode='json'), 'mapping': ws.mapping[n].model_dump(mode='json')})
            for n, d in ws.datasets.items()
        ]
        if detail and name in ws.datasets:
            args = _drop_empty(ws._dataset_definition(name))
            next(item for item in items if item['dataset'] == name)['define_args'] = args
    elif section == 'basis':
        items = [{'scheme': s, **v} for s, v in ws.workflow.get('basis', {}).items() if not scheme or s == scheme]
    elif section == 'checks' and name == 'data':
        saved = ws.workflow.get('data_report', {})
        if not saved or saved.get('version') != ws._flow().version('data'):
            return {'section': 'checks', 'status': 'STALE' if saved else 'NOT_RUN', 'items': [],
                    'next_call': {'tool': 'prepare_data', 'args': {}}}
        report = saved['report']
        items = sorted(report.get('source_coverage', ()), key=lambda item: item['status'] == 'MAPPED')
        items += [{'kind': 'history', **history} for history in report.get('history_sources', ())]
        items += [{'kind': 'join', **join} for join in report.get('join_coverage', ())]
        items += [{'kind': 'issue', 'message': message} for message in report['issues']]
        name = None
    elif section == 'checks':
        from bonus_agent.workflow import Workflow
        saved = ws.workflow.get('formula_report', {})
        if not saved:
            return {'section': 'checks', 'status': 'NOT_RUN', 'items': [],
                    'next_call': {'tool': 'compile_formulas', 'args': {}}, 'notice': '尚无公式检查，先compile_formulas'}
        flow = Workflow(ws)
        current = (flow.compile_feedback(saved['report'].get('phase', 'all')) is not None
                   if saved.get('phase_version') else saved.get('version') == flow.version())
        if not current:
            return {'section': 'checks', 'status': 'STALE', 'items': [],
                    'next_call': {'tool': 'compile_formulas', 'args': {}}, 'notice': '规则或证据已变化，重新compile_formulas；不能把旧检查当当前结论'}
        items = [{'kind': 'issue', 'index': i, 'message': message} for i, message in enumerate(saved['report']['issues'])]
        items += [{'kind': 'warning', 'index': i, 'message': message} for i, message in enumerate(saved['report']['warnings'])]
        items += [{'kind': 'preflight', 'name': 'preflight:' + scheme, 'index': i, **issue}
                  for scheme, report in saved['report'].get('preflight', {}).items()
                  for i, issue in enumerate(report.get('issues', []))]
        if name == 'graph':
            items = []
            for graph_scheme, graph in saved['report'].get('graph', {}).items():
                if scheme is not None and scheme != graph_scheme:
                    continue
                items.append({'kind': 'graph', 'name': 'graph', 'scheme': graph_scheme,
                              'phase': saved['report'].get('phase'), 'order': graph.get('order', [])})
                items += [{'kind': 'dependency', 'name': 'graph', 'scheme': graph_scheme,
                           'node': node, 'depends_on': dependencies}
                          for node, dependencies in graph.get('dependencies', {}).items()]
    elif section == 'tool_results':
        reports = ws.workflow.get('tool_reports', {})
        chosen = {name: reports[name]} if name in reports else (reports if name is None else {})
        items = []
        for tool, value in chosen.items():
            if isinstance(value, dict):
                for field, entry in value.items():
                    items += ([{'name': tool, 'field': field, 'index': i, 'value': item} for i, item in enumerate(entry)]
                              if isinstance(entry, list) else [{'name': tool, 'field': field, 'value': entry}])
            else:
                items += [{'name': tool, 'index': i, 'value': item} for i, item in enumerate(value)]
    elif section == 'construction':
        from bonus_agent.construction import TaskBoard
        board = TaskBoard(ws)
        statuses = board.statuses()
        items = []
        for n, spec in board.state['tasks'].items():
            status = statuses[n]
            item = {'name': n, **spec, **status, 'report': None}
            if report := board.state['reports'].get(n):
                item['report_current'] = report.get('version') == status['version']
                if item['report_current']:
                    item['report'] = report
                else:
                    item['previous_report'] = {'version': report.get('version'), 'status': report.get('status')}
            if set(spec['depends_on']) != set(status['depends_on']):
                item['planned_depends_on'] = spec['depends_on']
            items.append(item)
    else:
        from bonus_agent.workflow import fail

        fail('section须为facts/clauses/rules/variables/tables/mapping/basis/checks/tool_results/construction')
    if name is not None and section not in ('variables', 'tables') and pointer is None:
        items = [
            item
            for item in items
            if name in (item.get('name'), item.get('id'), item.get('dataset'), item.get('scheme'))
        ]
    if file is not None:
        items = [item for item in items if item.get('file') == file]
    if page is not None:
        items = [item for item in items if item.get('page') == page]
    if query is not None:
        from bonus_agent.facts import norm

        items = [item for item in items if norm(query) in norm(json.dumps(item, ensure_ascii=False))]
    brief = section in _BRIEF and name is None and not detail
    if brief:
        items = [_brief(section, item) for item in items]  # filters above still search the full records
    args = {
        k: v
        for k, v in dict(
            section=section, scheme=scheme, name=name, query=query, file=file, page=page, detail=detail or None
        ).items()
        if v is not None
    }
    result = {
        'section': section,
        'total': len(items),
        'offset': offset,
        'requested_limit': requested_limit,
        'page_limit': limit,
        'items': [],
        'next_offset': None,
        'next_call': None,
    }
    if brief:
        result['view'] = 'brief：单条完整记录用 name 读取，或 detail=true；任务完整记录含条款、参数及检查报告'
    if rule_index:
        result['view'] = '规则组件索引：按每项next_call读取精确节点；已有绑定随节点列出，不按相同数字自动关联。detail=true可读取原完整定义。'
    if pointer is not None and not items and notes:
        parent = pointer.rsplit('/', 1)[0]
        result.update(status='NOT_FOUND', next_call={'tool': 'read_workspace', 'args': {
            'section': 'rules', **({'scheme': scheme} if scheme else {}),
            **({'name': parent} if parent else {}),
        }})
    if requested_limit != limit:
        notes.append(f'请求条数{requested_limit}已安全调整为{limit}；按next_call继续读取')
    if name is not None and not items and pointer is None:
        # 名字对不上时说清楚有哪些，不让模型换着方案名、section 反复试
        pool = sorted({n for s in ws.schemes for sec in ('tables', 'variables') for n in (ws._merged(s).get(sec) or {})}
                      | set(ws.schemes) | set(ws.datasets))
        near = difflib.get_close_matches(name, pool, n=6, cutoff=0.3)
        notes.append(f'没有叫「{name}」的{section}' + (f'；名字相近的有 {near}' if near else '')
                     + (f'；已有的表 {sorted({n for s in ws.schemes for n in (ws._merged(s).get("tables") or {})})[:40]}'
                        if section in ('rules', 'tables') else '') + '。不要换方案名重试')
    if notes:
        result['notice'] = '；'.join(notes)
    # Stay below the runner's character cap while preserving whole records and real offsets.
    context = None
    for item in items[offset : offset + limit] if detail_offset is None else []:
        shown = item
        if brief and section == 'facts' and result['items'] and item.get('context') == context:
            shown = {k: v for k, v in item.items() if k != 'context'}  # same table as the row above
        candidate = {**result, 'items': result['items'] + [shown]}
        if len(json.dumps(candidate, ensure_ascii=False, default=str)) > PAGE_CHARS:
            break
        result['items'].append(shown)
        context = item.get('context')
    if brief and section == 'facts' and context is not None:
        result['view'] += '；context 与上一条相同的不再重复'
    if not result['items'] and offset < len(items):
        # A single large definition needs explicit fragments, not a silently cut JSON object.
        text = json.dumps(items[offset], ensure_ascii=False, default=str)
        start = detail_offset or 0
        if start >= len(text):
            result.update(status='INVALID_OFFSET', notice=f'detail_offset={start}超出本条长度{len(text)}；按next_call从本条开头重新读取',
                          next_call={'tool': 'read_workspace', 'args': {**args, 'offset': offset, 'limit': 1, 'detail_offset': 0}})
            return _bounded_page(result)
        part = text[start : start + FRAGMENT_CHARS]
        end = start + len(part)
        result['item_fragment'] = {
            'format': 'json',
            'offset': start,
            'text': part,
            'next_detail_offset': end if end < len(text) else None,
            'total_chars': len(text),
        }
        # JSON escaping can make a short fragment exceed the runner cap. Size the actual envelope.
        while len(json.dumps(result, ensure_ascii=False, default=str)) > PAGE_CHARS and len(part) > 1:
            part = part[:max(1, len(part) // 2)]
            end = start + len(part)
            result['item_fragment'].update(text=part, next_detail_offset=end if end < len(text) else None)
        if end < len(text):
            result['next_offset'] = offset
            result['next_call'] = {
                'tool': 'read_workspace',
                'args': {**args, 'offset': offset, 'limit': 1, 'detail_offset': end},
            }
            return _bounded_page(result)
        consumed = 1
    else:
        consumed = len(result['items'])
    if offset + consumed < len(items):
        result['next_offset'] = offset + consumed
        result['next_call'] = {'tool': 'read_workspace', 'args': {**args, 'offset': offset + consumed, 'limit': limit}}
    return _bounded_page(result)


def _fact_evidence(ws, fact):
    item = {key: getattr(fact, key) for key in
            ('name', 'value', 'unit', 'file', 'page', 'quote', 'context', 'source_cell', 'unit_basis', 'targets')}
    item['read_source'] = {'tool': 'read_document', 'args': {'file': fact.file, **({'page': fact.page} if fact.page else {})}}
    # These are existing quotations, not an inferred unit or a newly accepted binding.
    lines = ws._chat_text().splitlines()
    excerpts = []
    for index, line in enumerate(lines):
        if fact.file in line or fact.context and fact.context in line:
            excerpt = '\n'.join(lines[index:index + 2])
            if excerpt not in excerpts and len(excerpt) <= 1400:
                excerpts.append(excerpt)
            if len(excerpts) == 2:
                break
    if excerpts or fact.unit_basis:
        item['existing_financial_context'] = {
            'excerpts': excerpts,
            'next_call': {'tool': 'read_document', 'args': {'file': '财务对话'}},
            'instruction': '按原文适用范围复用已有确认及unit_basis，不重复询问已确认单位；这里没有自动改值或绑定。',
        }
    return item


def _issue_tasks(issue, fact, tasks, schemes):
    """Match recorded ownership/paths, never numbers or names occurring in advice examples."""
    from bonus_agent.facts import split_target, target_parts

    if fact is not None:
        owners = [t for t in tasks if fact.name in t.get('facts', [])]
        for target in fact.targets:
            scheme, path = split_target(target)
            parts = target_parts(path)
            owners += [t for t in tasks if (scheme is None or scheme == t['scheme'])
                       and parts[:len(target_parts(t['target']))] == target_parts(t['target'])]
        return list({t['id']: t for t in owners}.values())
    # Error messages append examples and repair advice after their diagnostic sentence.
    head = re.split(r'[。；\n（]', issue, maxsplit=1)[0]
    qualified = [s for s in schemes if f'方案「{s}」' in head or f'{s}::' in head]
    found = []
    for task in tasks:
        if qualified and task['scheme'] not in qualified:
            continue
        aliases = (task['target'], '.'.join(target_parts(task['target'])))
        if any(re.search(r'(?<![\w./])' + re.escape(path) + r'(?=$|[./\s：:,，;；)）])', head) for path in aliases):
            found.append(task)
    return found


def _repair_focus(ws, flow, construction):
    """One current compiler blocker becomes actionable context; no rules/receipts are changed."""
    if construction is None or construction.get('active'):
        return None
    if (planning := construction.get('planning_gap')) and planning.get('kind') != 'parameter_owners':
        return None
    saved = ws.workflow.get('formula_report', {}).get('report', {})
    report = flow.compile_feedback(saved.get('phase', 'all'))
    if not report or report.get('status') != 'FAIL' or not report.get('issues'):
        return None
    from bonus_agent.construction import TaskBoard
    tasks = list(ws.workflow.get('construction', {}).get('tasks', {}).values())
    required = TaskBoard(ws).required(report.get('phase'))
    cases = []
    for issue in report['issues']:
        fact = next((f for f in ws.facts if issue.startswith(f.name + '：')), None)
        cases.append((issue, fact, _issue_tasks(issue, fact, tasks, ws.schemes)))
    # An exact target/declared owner outranks an unassigned global fact. Ambiguity is retained.
    issue, fact, owners = next((c for c in cases if len(c[2]) == 1),
                              next((c for c in cases if c[1] is not None or c[2]), cases[0]))
    focus = {'phase': report.get('phase'), 'issue': issue[:1200], 'issue_truncated': len(issue) > 1200,
             'remaining_issues': len(report['issues']) - 1,
             'full_report': {'tool': 'read_workspace', 'args': {'section': 'checks'}}}
    def entry(task):
        return {**{k: task.get(k) for k in ('id', 'scheme', 'target')},
                'deferred': task['id'] not in required,
                'clauses': task.get('clauses', [])[:4], 'clause_count': len(task.get('clauses', [])),
                'facts': task.get('facts', [])[:2], 'fact_count': len(task.get('facts', []))}
    if len(owners) == 1:
        task = owners[0]
        focus.update(kind='task', task=entry(task),
                     next_call={'tool': 'read_workspace', 'args': {'section': 'construction', 'name': task['id']}},
                     instruction=f'全局检查回流到已有任务{task["id"]}；按本条反例修复来源、公式或绑定，再check_formula_task。局部PASS不代表全局已通过，deferred仅记录当前计划阶段。')
        selected = [fact] if fact is not None else [f for f in ws.facts if f.name in task.get('facts', [])][:3]
        focus['evidence'] = {'facts': [_fact_evidence(ws, f) for f in selected],
                             'clauses': [{'id': c.id, 'file': c.file, 'quote': c.quote}
                                         for c in ws.clauses if c.id in task.get('clauses', [])][:4]}
    elif fact is not None:
        kind = 'parameter_owner_ambiguous' if owners else ('parameter_ownership' if not fact.targets else 'parameter_binding')
        focus.update(kind=kind, fact= fact.name,
                     candidate_tasks=[entry(t) for t in (owners or tasks)[:8]],
                     candidate_count=len(owners or tasks),
                     candidates_are='仅已有声明的中性索引，不是推荐；未按数值、角色名称或业务阶段推断归属。',
                     next_call={'tool': 'read_workspace', 'args': {'section': 'facts', 'name': fact.name}},
                     task_index={'tool': 'read_workspace', 'args': {'section': 'construction', 'limit': 8}},
                     instruction=f'先明确参数「{fact.name}」的归属，有多个owner须按原文核对适用范围。已有合适目标则用plan_formula_tasks复用原id、scheme、target并保留depends_on/clauses/已有facts，在facts中加入本参数；材料要求的新目标尚不存在时，按原文新建带依赖和来源的公式任务，禁止新建同目标重复任务。归属不是绑定或豁免：仍须核对节点并bind_facts，不按相同数字选节点，不自动延后无主参数。',
                     plan_usage='plan_formula_tasks(tasks=[已有任务完整声明（facts保留原项并加入本参数），或原文要求的新目标任务（含id/scheme/target/depends_on/clauses/facts）], replace=False)',
                     evidence={'facts': [_fact_evidence(ws, fact)], 'clauses': []})
        cell = fact.source_cell or {}
        peers = [f for f in ws.facts if f.file == fact.file and f.page == fact.page
                 and (not cell or (f.source_cell or {}).get('table') == cell.get('table'))
                 and not f.targets and not any(f.name in t.get('facts', []) for t in tasks)
                 and any(message.startswith(f.name + '：') for message in report['issues'])]
        focus['same_source_unassigned_count'] = len(peers)
    else:
        focus.update(kind='compile_issue', next_call=focus['full_report'],
                     instruction='先处理这一具体编译反例；当前没有唯一任务归属，不能猜节点或把全局FAIL当局部PASS。')
        if owners:
            focus['candidate_tasks'] = [entry(t) for t in owners[:8]]
            focus['candidate_count'] = len(owners)
    return focus


def _tier_repair_context(ws, active):
    """Expose the complete physical header and source coordinates of referenced tiers."""
    from bonus_agent import facts as fx
    from bonus_agent.agent import ToolError
    from bonus_agent.construction import TaskBoard
    from bonus_core.dsl.parser import Call, ExprError, Name, parse, walk
    import re

    try:
        formula = parse(active['formula'])
    except (KeyError, ExprError):
        return None
    scheme = active['scheme']
    board = TaskBoard(ws)
    rules = ws._ruleset(scheme)
    table_names = {n.args[0].id for n in walk(formula) if isinstance(n, Call)
                   and n.fn == 'tier' and n.args and isinstance(n.args[0], Name)}
    tables, pages = {}, {}
    for task in board.state['tasks'].values():
        parts = fx.target_parts(task['target'])
        if task['scheme'] != scheme or parts[0] != 'tables' or parts[1] not in table_names:
            continue
        name = parts[1]
        if name not in rules.tables:
            continue
        tables[name] = {'task': task['id'], 'guard': rules.tables[name].applies_when, 'clauses': task['clauses']}
        for fact in ws.facts:
            if not fact.page or not fact.file.lower().endswith('.pptx') or not any(board.owns(task, ref) for ref in fact.targets):
                continue
            key = (fact.file, fact.page)
            if key not in pages and len(pages) < 8:
                try:
                    page = ws.read_parameter_table(*key)
                except ToolError:
                    continue
                pages[key] = {
                    'file': fact.file, 'page': fact.page, 'scope': page['page_scope'],
                    'context': page['page_context'],
                    'tables': [{'table': t['table'],
                                'rows': [[{'column': c['column'], 'text': '\n'.join(c['lines'])} for c in row['cells']]
                                         for row in t['rows'][:12]]} for t in page['tables'][:2]],
                }
            if key in pages:
                tables[name]['source'] = {'file': fact.file, 'page': fact.page}
                break
    tier_mode = next((c for c in ws.clauses if c.id in active.get('clauses', []) and '超额累进' in c.quote), None)
    variants = []
    for name, spec in tables.items():
        source = spec.get('source')
        if not source or not tier_mode or '_new_' not in name:
            continue
        old_name = name.replace('_new_', '_old_', 1)
        if old_name in rules.tables:
            continue
        page = pages[source['file'], source['page']]
        if any('按照新项目的奖励标准执行' in text for text in page['context']):
            continue
        if len(page['tables']) != 1:
            continue
        grid = page['tables'][0]
        header, *body = grid['rows']
        old = [cell['column'] for cell in header if '旧项目' in cell['text'] or '老客户' in cell['text']]
        if len(old) != 1 or not body or not all(row[0]['column'] == 1 and old[0] <= len(row)
                                                and re.search(r'\d(?:\.\d+)?\s*%', row[old[0] - 1]['text'])
                                                for row in body):
            continue
        cells_from = {**source, 'table': grid['table']}
        positions = [(row, not ws._UPPER_BAND.search(cells[0]['text']))
                     for row, cells in enumerate(body, 2)]
        if sum(not lower for _, lower in positions) != 1:
            continue
        variant = {
            'name': old_name, 'task': 'tier_' + old_name, 'clauses': spec['clauses'],
            'register': {**cells_from, 'applies_to': page['scope'],
                         'cells': [*[{'row': row, 'column': old[0]} for row in range(2, 2 + len(body))],
                                   *[{'row': row, 'column': 1} for row, lower in positions if lower]]},
            'compile': {
                'cells_from': cells_from, 'rows': [
                    {'value': f'R{row}C{old[0]}', **({'lower': f'R{row}C1'} if lower else {})}
                    for row, lower in positions],
                'algorithm_basis': {'file': tier_mode.file, 'quote': tier_mode.quote},
                'applies_when': spec['guard'], 'below_first': rules.tables[name].below_first,
                'lower_inclusive': rules.tables[name].lower_inclusive,
            },
        }
        variants.append(variant)
    routing_columns = {f['source']['column'] for f in active['table_applicability'].get('field_evidence', ())
                       if f.get('source', {}).get('column')}
    routing = [{'id': c.id, 'file': c.file, 'quote': c.quote} for c in ws.clauses
               if '归类' in c.quote and any(column in c.quote for column in routing_columns)]
    return {'task': active['task'], 'scheme': scheme, 'defined_tiers': tables, 'source_pages': list(pages.values()),
            'routing_clauses': routing[:8],
            'variant_candidates': variants,
            'next': '按原文的各区域和新旧列补齐参数表任务。费率按原格编译，用实际分类值同步修改选表分支与表的适用条件。'}


def task_card(ws, *, data=None):
    if data is None:
        data = task_card_data(ws)
    return json.dumps(data, ensure_ascii=False, default=str)


def _recipient_scheme_plan(plan, scheme, sources):
    """Offer a structural plan when the material explicitly requires rounding after grouping."""
    from bonus_agent.facts import split_target, target_parts

    if plan['schemes'] != [scheme] or not plan.get('output_targets'):
        return None
    after_sum = next((source for source in sources if re.search(
        r'先.{0,12}(?:人员|每人|同一人).{0,16}汇总.{0,12}(?:再|后).{0,12}(?:四舍五入|舍入|保留到分)',
        source['quote'])), None)
    if after_sum is None:
        return None
    recipient = scheme + '人员分配'
    targets = {}
    moved = []
    for output, pointers in plan['output_targets'].items():
        targets[output] = []
        for pointer in pointers:
            owner, path = split_target(pointer)
            parts = target_parts(path)
            if owner == scheme and len(parts) >= 2 and parts[1] in ('people', 'pending', 'distributions'):
                pointer = recipient + '::' + path
                moved.append(pointer)
            targets[output].append(pointer)
    if not any(pointer.endswith('::/report/people') for pointer in moved):
        return None
    args = {k: plan[k] for k in ('summary', 'outputs', 'files', 'ignored')}
    args.update(schemes=[scheme, recipient], output_targets=targets)
    return {'source': after_sum, 'funding_scheme': scheme, 'recipient_scheme': recipient,
            'call': {'tool': 'plan_calculation', 'args': args}}


def task_card_data(ws):
    from bonus_agent.workflow import Workflow

    flow = Workflow(ws)
    data = {
        'workflow': flow.summary(),
        'pending': [q.model_dump(mode='json') for q in ws.pending],
        'materials': [{'file': f.name, 'bytes': f.stat().st_size} for f in ws.inv.files],
        'datasets': {
            n: {'key': d.key, 'link': d.link, 'period': d.period, 'window': d.window, 'fields': list(d.fields)}
            for n, d in ws.datasets.items()
        },
        'rules': {
            s: {
                'variables': list(ws._merged(s).get('variables') or {}),
                'tables': list(ws._merged(s).get('tables') or {}),
            }
            for s in ws.schemes
        },
        'evidence': {
            'facts': len(ws.facts),
            'clauses': len(ws.clauses),
            'last_rejections': ws._fact_rejections[:8],
            'uncovered': [{'id': c.id, 'file': c.file, 'point': c.point[:200]} for c in ws.clauses if not c.where][:20],
        },
        'notes': {k: v[:800] for k, v in list(ws.notes.items())[-8:]},
        'lookup': '原文按文件read_document/read_parameter_table；台账/条款/映射/公式/预核算按read_workspace分页读取。覆盖索引完整保存在工作区，不能按当前摘录判断无其他规则。',
    }
    report = ws.workflow.get('formula_report', {})
    if report:
        data['last_formula_check'] = {
            'current': ((flow.compile_feedback(report['report'].get('phase', 'all')) is not None)
                        if report.get('phase_version') else report.get('version') == flow.version()),
            'issue_count': len(report['report']['issues']),
            'first_issues': [x[:450] for x in report['report']['issues'][:3]],
            'lookup': 'read_workspace(section="checks")；current=false须先重新compile_formulas',
        }
    construction = data['workflow'].get('construction') or {}
    active = construction.get('runtime_repair') or construction.get('active')
    if construction.get('next', '').startswith('plan_formula_tasks'):
        # Small plans can use the full exact registry. Larger registries stay
        # available through the normal paginated citation interface.
        entries = [{'name': f.name, 'value': f.value, 'file': f.file} for f in ws.facts[:12]]
        if len(json.dumps(entries, ensure_ascii=False)) > 2400:
            entries = []
        construction['planning_facts'] = {
            'items': entries, 'total': len(ws.facts), 'read_call': {'tool': 'read_workspace', 'args': {'section': 'facts'}},
        }
    saved_data = ws.workflow.get('data_report', {})
    histories = saved_data.get('report', {}).get('history_sources', ())
    if histories and data['workflow']['stage'] <= 3 and saved_data.get('version') == flow.version('data'):
        with_alternatives = [source for source in histories if source['alternatives']]
        if with_alternatives:
            data['history_sources'] = with_alternatives[:2]
    if active:
        if data['workflow']['stage'] == 3 and active.get('scheme') in ws.schemes:
            from bonus_agent.construction import TaskBoard
            from bonus_agent.indicator_scope import repair_context
            from bonus_agent.indicator_history import history_repair
            from bonus_agent.indicator_budget import budget_repair
            from bonus_agent.recovery import fingerprint
            board = TaskBoard(ws)
            scope_repair = (repair_context(board, active) if active.get('confidence_level') == 'CONFLICT'
                            or construction.get('runtime_repair') else None)
            if not scope_repair and flow.current('data'):
                key = fingerprint((saved_data.get('version'), ws._merged(active['scheme']), ws.clauses))
                if getattr(ws, '_history_repair_key', None) != key:
                    ws._history_repair = (history_repair(board, active['scheme'], histories)
                                          or budget_repair(board, active['scheme']))
                    ws._history_repair_key = key
                scope_repair = ws._history_repair
            if scope_repair:
                construction['scope_repair'] = scope_repair
            elif active.get('table_applicability'):
                key = fingerprint((ws._source_version, active['formula'], ws._merged(active['scheme']).get('tables')))
                if getattr(ws, '_tier_repair_key', None) != key:
                    ws._tier_repair = _tier_repair_context(ws, active)
                    ws._tier_repair_key = key
                if ws._tier_repair:
                    resumed_candidate = getattr(ws, '_resume_tier_candidate', None)
                    if resumed_candidate and resumed_candidate['args'].get('task') == active['task']:
                        construction['tier_repair'] = {**ws._tier_repair, 'candidate_call': resumed_candidate}
                    else:
                        construction['tier_repair'] = ws._tier_repair
        data['evidence']['uncovered'] = [x for x in data['evidence']['uncovered'] if x['id'] in active.get('clauses', [])]
        data['current_task_evidence'] = {
            'clauses':[{'id':c.id,'file':c.file,'quote':c.quote,'point':c.point}
                       for c in ws.clauses if c.id in active.get('clauses', [])][:6],
            'facts':[{'name':f.name,'value':f.value,'unit':f.unit,'file':f.file,'page':f.page}
                     for f in ws.facts if f.name in active.get('facts', [])][:6],
        }
        scheme = active.get('scheme')
        if (data['workflow']['stage'] == 3 and active.get('target') in ('/allocation', '/report')
                and scheme in ws.schemes and flow.current('approved:' + scheme)):
            from bonus_agent.construction import TaskBoard

            board = TaskBoard(ws)
            statuses, basis_tasks = board.statuses(with_confidence=False), board.required('basis')
            role_clauses = set(active.get('clauses', ()))
            report = ws._merged(scheme).get('report') or {}
            for spec in board.state['tasks'].values():
                if spec['scheme'] == scheme and spec['target'] in ('/allocation', '/report'):
                    role_clauses.update(spec['clauses'])
            sources = [{'id': c.id, 'file': c.file, 'quote': c.quote}
                       for c in ws.clauses if c.id in role_clauses]
            tables = [
                {'task': n, 'target': s['target'], 'clauses': s['clauses'],
                 'definition': {k: v for k, v in board.value(s).items() if k not in ('sources', 'desc', 'confidence')}}
                for n, s in board.state['tasks'].items()
                if n not in basis_tasks and s['scheme'] == scheme and s['target'].startswith('/tables/')
                and statuses[n]['status'] == 'PASS' and board.value(s) and role_clauses.intersection(s['clauses'])
            ]
            rs = ws._ruleset(scheme)
            plan = data['workflow']['plan']
            proposal = _recipient_scheme_plan(plan, scheme, sources) if rs.pool.scope != 'global' and not report.get('people') else None
            data['personnel_task'] = {
                'scheme': scheme, 'target': active['target'], 'basis_scope': rs.pool.scope,
                'basis_scope_keys': board.scope_keys(rs), 'basis_objects': len(flow.state['basis'][scheme]['objects']),
                'basis_pool': {'scope': rs.pool.scope, 'expr': rs.pool.expr, 'negative': rs.pool.negative},
                'basis_detail': [item['name'] for item in report.get('details', []) if item.get('stage', 'basis') == 'basis'],
                'allocation': ws._merged(scheme).get('allocation') or [],
                'rule_sources': sources, 'checked_role_tables': tables[:6],
                'answers': [{'id': q.id, 'text': q.text, 'answer': q.answer} for q in ws.questions
                            if q.answer and not q.withdrawn],
            }
            if proposal:
                data['personnel_task']['scheme_proposal'] = proposal
    elif focus := _repair_focus(ws, flow, data['workflow'].get('construction')):
        data['current_task_evidence'] = focus.pop('evidence', {'facts': [], 'clauses': []})
        data['repair_focus'] = focus
        data['workflow']['next'] = focus['instruction']
        construction = data['workflow']['construction']
        construction['next'] = focus['instruction']
        if 'compile_failure' in construction:
            # The full report remains pageable; do not repeat dozens of unrelated blockers here.
            construction['compile_failure'] = {k: v for k, v in construction['compile_failure'].items()
                                               if k in ('status', 'phase', 'issue_count')}
            construction['compile_failure'].update(issue_count=focus['remaining_issues'] + 1,
                                                   issues=[focus['issue']], remaining_issues=focus['remaining_issues'])
        data['last_formula_check']['first_issues'] = [focus['issue']]
    return data
