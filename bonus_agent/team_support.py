"""Program-owned repair queue, incremental component edits and source coverage."""
import copy
from bonus_agent.recovery import fingerprint


def repair_inputs(ws):
    from bonus_agent.construction import _semantic
    return fingerprint({'sources':ws._source_version,'said':ws.said,
        'answers':[(q.id,q.answer,q.withdrawn) for q in ws.questions],
        'facts':[f.model_dump(mode='json') for f in ws.facts],
        'clauses':[(c.id,c.file,c.quote) for c in ws.clauses],
        'rules':_semantic(ws.drafts),
        'datasets':{n:_semantic(d.model_dump(mode='json')) for n,d in ws.datasets.items()},
        'mapping':{n:_semantic(m.model_dump(mode='json')) for n,m in ws.mapping.items()}})


def request_repair(ws, kind, reason, task=None):
    from bonus_agent.agent import ToolError
    if kind not in ('rules','data','coordinator') or not reason.strip():
        raise ToolError('repair.request：kind为rules/data/coordinator，reason须明确问题及证据位置')
    board=ws.workflow.get('construction',{}).get('tasks',{})
    if task is not None and task not in board:
        raise ToolError('repair.request：未知任务')
    team=ws.workflow.setdefault('team',{})
    queue=team.setdefault('repairs',{})
    # Narration changes cannot buy new transfers for the same upstream inputs.
    inputs=repair_inputs(ws)
    key=fingerprint([task,kind,inputs])
    rec=queue.setdefault(key,{'id':key,'task':task,'kind':kind,'attempts':0,'status':'PENDING','inputs':inputs})
    if rec['status']=='PENDING':
        rec['reason']=reason
        return {'status':'REPAIR_QUEUED','repair':rec}
    if rec['attempts']>=2:
        raise ToolError('repair.budget：同份输入已转派两次，须补证据；不重置预算或代替业务答复')
    rec.update(status='PENDING',reason=reason)
    return {'status':'REPAIR_QUEUED','repair':rec}


def finish_repair(ws, repair):
    from bonus_agent.agent import ToolError
    queue=ws.workflow.get('team',{}).get('repairs',{})
    rec=queue.get(repair)
    if not rec or rec['status']!='PENDING':raise ToolError('repair.return：没有此待处理转派')
    if any(r.get('parent')==repair and r['status']=='PENDING' for r in queue.values()):
        raise ToolError('repair.child：先完成下游修复再返回原任务')
    if repair_inputs(ws)==rec['inputs']:
        raise ToolError('repair.no_change：参数、规则、关联均没有变化，不能以note或反思结束修复')
    if rec['kind']=='data':
        report=ws.prepare_data()
        if report['status']!='PASS':
            return {'status':'FAIL','repair':repair,'issues':report.get('issues',[]),
                    'next':'数据仍未通过prepare_data，按保存的反例继续修复，未返回原任务'}
    rec.update(status='RETURNED',attempts=rec['attempts']+1)
    task=rec['task']
    if task:
        record=ws.workflow['team'].get('tasks',{}).get('formula:'+task)
        if record:record.update(status='READY',problem='修复已返回，须重新校验')
    return {'status':'REPAIR_RETURNED','task':task,'next':'原任务读取新版本并重新check_formula_task；尚未授予通过回执'}


def patch_component(ws, scheme, path, value, bindings=None):
    """One JSON-pointer child at a time; root validation still uses set_rules."""
    from bonus_agent.agent import ToolError
    from bonus_agent.facts import target_parts
    parts=target_parts(path)
    if len(parts)<2 or parts[0] not in ('allocation','report','payout','offsets','checks','variables','tables'):
        raise ToolError('component.path：须为受支持组件的子路径；整根用set_rules')
    root=parts[0]
    original=ws._merged(scheme).get(root)
    updated=copy.deepcopy(original if original is not None else ([] if root in ('allocation','offsets','checks') else {}))
    node=updated
    for i,key in enumerate(parts[1:-1]):
        following=parts[i+2]
        if isinstance(node,list):
            if not key.isdigit() or int(key)>=len(node):raise ToolError('component.path：父项不存在；先提交父项')
            node=node[int(key)]
        elif isinstance(node,dict):
            node=node.setdefault(key,[] if following.isdigit() or following=='-' else {})
        else:raise ToolError('component.path：父项不是容器')
    key=parts[-1]
    if isinstance(node,list):
        if key=='-' or key==str(len(node)):node.append(copy.deepcopy(value))
        elif key.isdigit() and int(key)<len(node):node[int(key)]=copy.deepcopy(value)
        else:raise ToolError('component.path：列表位置越界')
    elif isinstance(node,dict):node[key]=copy.deepcopy(value)
    else:raise ToolError('component.path：父项不是容器')
    output=ws.set_rules(scheme=scheme,bindings=bindings,**{root:updated})
    return {'status':'DRAFT','path':path,'result':output,'issues':sorted(str(x) for x in getattr(ws,'last_blocking',[])),
            'next':'继续分块构建；必须check_formula_task和compile_formulas，DRAFT不代表校验通过'}


def deferred_requirements(spec):
    rows=spec.get('requirements')
    if not isinstance(rows,list):return []
    result=[]
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('targets'),list):continue
        if any(t in {'allocation','report.people','payout','offsets','checks'} for t in row['targets'] if isinstance(t,str)):
            result.append(copy.deepcopy(row))
    return result


def coverage(ws, spec):
    """Independent source obligations must point at actual independent formulas."""
    from bonus_agent.facts import norm
    from bonus_agent.clauses import NOT_APPLY,ASKED
    expected={r['file'] for r in ws._flow().rule_requirements()}
    errors=[]
    reviewed=spec.get('sources_reviewed')
    obligations=spec.get('requirements')
    if not isinstance(reviewed,list) or not all(isinstance(x,str) for x in reviewed) or set(reviewed)!=expected:
        errors.append('coverage.sources：独立复核须逐项列出全部规则来源sources_reviewed')
    if not isinstance(obligations,list) or not obligations:
        return errors+['coverage.requirements：独立复核缺少原文业务要求清单']
    valid=[]
    corpus=ws._corpus()
    targets={'pool'}|{'details.'+n for n in spec.get('details',{})}|{'totals.'+n for n in spec.get('totals',{})}
    targets|={'helpers.'+n for n in spec.get('helpers',{})}|{'tables.'+n for n in spec.get('tables',{})}
    # Personnel and payment rules are verified after human approval of the basis,
    # never forced into a pre-allocation pool expression.
    later={'allocation','payout','offsets','checks','report.people'}
    targets|={t for t in later if any(ws._merged(s).get(t.split('.')[0]) for s in ws.schemes)}
    for item in obligations:
        if (not isinstance(item,dict) or item.get('file') not in expected or
                len(norm(item.get('quote','')))<6 or not str(item.get('point','')).strip() or
                not norm(item['quote']) in norm(corpus.get(item['file'],'')) or
                not isinstance(item.get('targets'),list) or not item['targets'] or
                not all(isinstance(x,str) for x in item['targets']) or set(item['targets'])-targets):
            errors.append('coverage.evidence：业务要求须有真实原文、说明及有效独立算式目标')
        else:valid.append(item)
    for clause in ws.clauses:
        if clause.where.startswith((NOT_APPLY,ASKED)):continue
        quote=norm(clause.quote)
        if clause.file in expected and not any(x['file']==clause.file and
                (quote in norm(x['quote']) or norm(x['quote']) in quote) for x in valid):
            errors.append('coverage.clause：'+clause.id+' '+clause.point+' 未对应独立原文要求及算式')
    contracts=spec.get('data_contracts')
    if not isinstance(contracts,list) or any(not isinstance(x,dict) for x in contracts):
        errors.append('coverage.grain：缺少逐数据集的独立粒度、关联和期间契约')
    else:
        found=[x.get('dataset') for x in contracts]
        if len(found)!=len(set(str(x) for x in found)) or set(str(x) for x in found)!=set(ws.datasets):
            errors.append('coverage.grain：数据契约必须逐项覆盖所有数据集，不重复、不遗漏')
        for item in contracts:
            name=item.get('dataset')
            if not isinstance(name,str) or name not in ws.datasets:continue
            if (not isinstance(item.get('file'),str) or len(norm(item.get('quote','')))<6 or
                    norm(item['quote']) not in norm(corpus.get(item['file'],''))):
                errors.append('coverage.grain：'+name+' 缺少原文依据')
                continue
            for key in ('key','link','period','window','population_from'):
                if key not in item or item[key]!=getattr(ws.datasets[name],key):
                    errors.append('coverage.grain：'+name+'.'+key+' 与独立原文判定不一致')
    return list(dict.fromkeys(errors))
