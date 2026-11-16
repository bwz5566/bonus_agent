"""Bounded task context; authoritative rules and raw rows stay in workspace tools."""
import json

from bonus_agent.llm import LLMError


def estimate_tokens(value):
    # Every UTF-8 byte may be a token: conservative when a provider tokenizer is unavailable.
    return len(json.dumps(value,ensure_ascii=False,default=str).encode('utf-8'))


def guard_messages(messages, llm, *, output_tokens=8192, tools=None):
    maximum=llm.context_window()
    estimate=estimate_tokens(messages)+(estimate_tokens(tools) if tools else 0)
    budget=getattr(llm,'max_input_tokens',96000)
    if estimate>budget or (maximum and estimate+output_tokens>maximum):
        raise LLMError(f'任务上下文预算不足：保守估算输入 {estimate} tokens，输入预算 {budget}，'
                       f'服务窗口 {maximum or "未知"}，预留输出 {output_tokens}。请拆分任务或检索相关原文，不裁掉规则后继续核算')
    return estimate


def workspace_view(ws, section, scheme=None, offset=0, limit=20, name=None):
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
                      'uncovered':[{'id':c.id,'file':c.file,'point':c.point[:200]} for c in ws.clauses if not c.where][:20]},
          'notes':{k:v[:800] for k,v in list(ws.notes.items())[-8:]},
          'lookup':'原文按文件read_document/read_parameter_table；台账/条款/映射/公式/预核算按read_workspace分页读取。覆盖索引完整保存在工作区，不能按当前摘录判断无其他规则。'}
    return json.dumps(data,ensure_ascii=False,default=str)
