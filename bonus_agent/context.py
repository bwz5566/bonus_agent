"""Bounded task context; authoritative rules and raw rows stay in workspace tools."""
import json
import math
import re

from bonus_agent.llm import LLMError


class ContextBudgetError(LLMError):
    """A local estimate/policy decision; never evidence of a provider rejection."""


_PIECES=re.compile(r'[A-Za-z_]+|[0-9]+|[ \t]+|\r?\n|[^\x00-\x7f]|.',re.S)


def estimate_tokens(value):
    """Conservative multilingual estimate, not bytes or a provider tokenizer count."""
    text=json.dumps(value,ensure_ascii=False,default=str,separators=(',',':'))
    total=0.0
    for match in _PIECES.finditer(text):
        part=match.group()
        if part[0].isascii():
            if part[0].isalnum() or part[0]=='_':total+=math.ceil(len(part)/3)
            elif part[0] in ' \t':total+=math.ceil(len(part)/4)
            else:total+=1
        else:
            total+=3 if ord(part)>0xffff else 1.5
    return math.ceil(total)


def guard_messages(messages, llm, *, output_tokens=8192, tools=None):
    maximum=llm.context_window()
    baseline=estimate_tokens(messages)+(estimate_tokens(tools) if tools else 0)
    estimate=math.ceil(baseline*max(1.0,getattr(llm,'_context_token_ratio',0)*1.2))
    budget=getattr(llm,'max_input_tokens',96000)
    if estimate>budget or (maximum and estimate+output_tokens>maximum):
        raise ContextBudgetError(f'本地上下文预算拦截（估算，非服务端超限）：估算输入 {estimate} tokens，配置输入预算 {budget}，'
                                 f'服务声明窗口 {maximum or "未知"}，预留输出 {output_tokens}。需整理任务上下文或调整预算；不能推断材料缺失')
    llm._pending_context_estimate=baseline
    return estimate


def workspace_view(ws, section, scheme=None, offset=0, limit=20, name=None, query=None, file=None, page=None):
    if not 0<=offset or not 1<=limit<=50:
        from bonus_agent.workflow import fail
        fail('读取范围须为offset>=0、1<=limit<=50')
    if section=='facts':items=[f.model_dump(mode='json') for f in ws.facts]
    elif section=='clauses':items=[c.model_dump(mode='json') for c in ws.clauses]
    elif section=='rules':items=[{'scheme':s,'rule':ws._merged(s)} for s in ([ws._scheme(scheme)] if scheme else ws.schemes)]
    elif section in ('variables','tables'):
        items=[{'scheme':s,'name':n,'definition':v} for s in ([ws._scheme(scheme)] if scheme else ws.schemes)
               for n,v in (ws._merged(s).get(section) or {}).items() if name is None or n==name]
    elif section=='mapping':items=[{'dataset':n,'definition':d.model_dump(mode='json'),'mapping':ws.mapping[n].model_dump(mode='json')} for n,d in ws.datasets.items()]
    elif section=='basis':items=[{'scheme':s,**v} for s,v in ws.workflow.get('basis',{}).items() if not scheme or s==scheme]
    else:
        from bonus_agent.workflow import fail
        fail('section须为facts/clauses/rules/variables/tables/mapping/basis')
    if name is not None and section not in ('variables','tables'):
        items=[item for item in items if name in (item.get('name'),item.get('id'),item.get('dataset'),item.get('scheme'))]
    if file is not None:items=[item for item in items if item.get('file')==file]
    if page is not None:items=[item for item in items if item.get('page')==page]
    if query is not None:
        from bonus_agent.facts import norm
        items=[item for item in items if norm(query) in norm(json.dumps(item,ensure_ascii=False))]
    return {'section':section,'total':len(items),'offset':offset,'items':items[offset:offset+limit],
            'next_offset':offset+limit if offset+limit<len(items) else None}


def task_card(ws):
    from bonus_agent.workflow import Workflow
    flow=Workflow(ws)
    data={'workflow':flow.summary(),'pending':[q.model_dump(mode='json') for q in ws.pending],
          'materials':[{'file':f.name,'bytes':f.stat().st_size} for f in ws.inv.files],
          'datasets':{n:{'key':d.key,'link':d.link,'period':d.period,'window':d.window,'fields':list(d.fields)} for n,d in ws.datasets.items()},
          'rules':{s:{'variables':list(ws._merged(s).get('variables') or {}),
                      'tables':list(ws._merged(s).get('tables') or {})} for s in ws.schemes},
          'evidence':{'facts':len(ws.facts),'clauses':len(ws.clauses),
                       'last_rejections':ws._fact_rejections[:8],
                      'uncovered':[{'id':c.id,'file':c.file,'point':c.point[:200]} for c in ws.clauses if not c.where][:20]},
          'notes':{k:v[:800] for k,v in list(ws.notes.items())[-8:]},
          'lookup':'原文按文件read_document/read_parameter_table；台账/条款/映射/公式/预核算按read_workspace分页读取。覆盖索引完整保存在工作区，不能按当前摘录判断无其他规则。'}
    return json.dumps(data,ensure_ascii=False,default=str)
