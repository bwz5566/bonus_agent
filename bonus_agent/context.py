"""Bounded task context; authoritative rules and raw rows stay in workspace tools."""

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
    if section == 'facts':
        items = [f.model_dump(mode='json') for f in ws.facts]
    elif section == 'clauses':
        items = [c.model_dump(mode='json') for c in ws.clauses]
    elif section == 'rules':
        items = [{'scheme': s, 'rule': ws._merged(s)} for s in ([ws._scheme(scheme)] if scheme else ws.schemes)]
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
    else:
        from bonus_agent.workflow import fail

        fail('section须为facts/clauses/rules/variables/tables/mapping/basis')
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
        result['notice'] = f'请求条数{requested_limit}已安全调整为{limit}；按next_call继续读取'
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
        part = text[start : start + FRAGMENT_CHARS]
        end = start + len(part)
        result['item_fragment'] = {
            'format': 'json',
            'offset': start,
            'text': part,
            'next_detail_offset': end if end < len(text) else None,
        }
        if end < len(text):
            result['next_offset'] = offset
            result['next_call'] = {
                'tool': 'read_workspace',
                'args': {**args, 'offset': offset, 'limit': 1, 'detail_offset': end},
            }
            return result
        consumed = 1
    else:
        consumed = len(result['items'])
    if offset + consumed < len(items):
        result['next_offset'] = offset + consumed
        result['next_call'] = {'tool': 'read_workspace', 'args': {**args, 'offset': offset + consumed, 'limit': limit}}
    return result


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
    return json.dumps(data, ensure_ascii=False, default=str)
