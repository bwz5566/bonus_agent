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
_BRIEF = {'facts': ('name', 'value', 'unit', 'page', 'context', 'meaning'), 'clauses': ('id', 'file', 'point', 'where')}


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
        if component in ROOTS:
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
    elif section == 'basis':
        items = [{'scheme': s, **v} for s, v in ws.workflow.get('basis', {}).items() if not scheme or s == scheme]
    elif section == 'checks':
        from bonus_agent.workflow import Workflow
        saved = ws.workflow.get('formula_report', {})
        if not saved:
            return {'section': 'checks', 'status': 'NOT_RUN', 'items': [],
                    'next_call': {'tool': 'compile_formulas', 'args': {}}, 'notice': '尚无公式检查，先compile_formulas'}
        if saved.get('version') != Workflow(ws).version():
            return {'section': 'checks', 'status': 'STALE', 'items': [],
                    'next_call': {'tool': 'compile_formulas', 'args': {}}, 'notice': '规则或证据已变化，重新compile_formulas；不能把旧检查当当前结论'}
        items = [{'kind': 'issue', 'index': i, 'message': message} for i, message in enumerate(saved['report']['issues'])]
        items += [{'kind': 'warning', 'index': i, 'message': message} for i, message in enumerate(saved['report']['warnings'])]
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
        items = [{'name':n, **spec, **statuses[n], 'report':board.state['reports'].get(n)}
                 for n,spec in board.state['tasks'].items()]
    else:
        from bonus_agent.workflow import fail

        fail('section须为facts/clauses/rules/variables/tables/mapping/basis/checks/tool_results/construction')
    if name is not None and section not in ('variables', 'tables'):
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
        result['view'] = 'brief：单条完整记录（原文、坐标）用 name 读取，或 detail=true'
    if requested_limit != limit:
        notes.append(f'请求条数{requested_limit}已安全调整为{limit}；按next_call继续读取')
    if name is not None and not items:
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


def task_card(ws):
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
            'current': report.get('version') == flow.version(),
            'issue_count': len(report['report']['issues']),
            'first_issues': [x[:450] for x in report['report']['issues'][:3]],
            'lookup': 'read_workspace(section="checks")；current=false须先重新compile_formulas',
        }
    active = (data['workflow'].get('construction') or {}).get('active')
    if active:
        data['evidence']['uncovered'] = [x for x in data['evidence']['uncovered'] if x['id'] in active['clauses']]
        data['current_task_evidence'] = {
            'clauses':[{'id':c.id,'file':c.file,'quote':c.quote} for c in ws.clauses if c.id in active['clauses']][:6],
            'facts':[{'name':f.name,'value':f.value,'unit':f.unit,'file':f.file,'page':f.page}
                     for f in ws.facts if f.name in active['facts']][:6],
        }
    return json.dumps(data, ensure_ascii=False, default=str)
