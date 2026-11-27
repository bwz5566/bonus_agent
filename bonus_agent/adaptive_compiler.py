"""Node-scoped compilation and replay of adaptive commission contracts.

The runner supplies the existing checkpointed Flash/Pro node interface. Each
rule chunk, dataset binding, metric group and role phase gets an independent
candidate/validation/review, rather than one enormous conversation. Frozen
contracts can be recompiled model-free before approval and export.
"""
from __future__ import annotations

from copy import deepcopy
from collections import defaultdict
import re

from openpyxl.utils import get_column_letter,column_index_from_string

from bonus_agent.adaptive_rules import (PHASES,coverage,rule_units,source_index,unit_chunks,source_lines,merge_rule_review,
    validate_clause_chunk,validate_source_inventory,validate_numeric_literals,verify_parameter_usage,normalized,primary_population_invariants)
from bonus_agent.adaptive_sources import compile_inputs,read_datasets,read_parameters,read_schedules
from bonus_agent.recovery import fingerprint
from bonus_core.engine.adaptive import (CalculationProgram,Inputs,ProgramError,contract,names,OPERATORS,
    apply_person_program,CONTRIBUTION_DATASET,CONTRIBUTION_FIELDS,canonical_program,canonical_roster,canonical_expression,Context,Record)
from bonus_core.dsl.models import to_decimal


MODE='sales_commission_v2'
BASIS_PHASES=PHASES[:5]
METRIC_PHASES=PHASES[2:5]
FRAGMENT_FIELDS=('metrics','parameters','schedules','checks','formula_bindings')


EXPRESSION_HELP='''表达式是JSON数据，禁止生成Python。形式：数字/文字/bool/null，{"key":"项目键字段"}、{"ref":"已有指标ID"}、{"field":"当前明细行字段"}、{"param":"真实参数ID"}、{"period":"month/start/end/year_start/month_number/year"}；{"op":"mul","args":[{"ref":"x"},{"param":"r"}]}。
聚合：{"query":{"dataset":"实际数据集ID或$project(本项目明细)/$population(已筛选本期全集)","aggregate":"sum/avg/count/count_distinct/unique/min/max/exists","match":{"该数据集字段":{"key":"项目键"}},"where":{"op":"eq","args":[{"field":"month"},{"period":"month"}]},"value":{"field":"金额字段"},"empty":"error/null/zero","nulls":"error/ignore","duplicates":"error/identical（仅unique必填）"}}。match={}代表显式全表范围，不能把全表当项目。where可省；count/exists不用value。字段作用域仅query/主集合where内；指标引用用ref。if条件只选一支，coalesce区分空与零。
阶梯：{"tier":{"schedule":"已编译阶梯ID","value":{"ref":"基数"},"output":"amount/rate"}}。参数仅指向真实格或原文：{"source":{"table":"T编号","row":2,"column":3},"kind":"number/ratio/text/date/month/bool/id/money","unit":"raw/percent/wan/yi","rules":["条款ID"]}；也可source={"sheet":"S编号","row":2,"column":"B"}或{"block":"实际原文单元ID","quote":"原文","value":"确在引文中的单值"}。
阶梯定义：{"bands":[{"label":实际格坐标,"rate":实际格坐标}],"bound_unit":"raw/percent/wan/yi","rate_unit":"raw/percent","method":"whole/progressive","negative":"signed/zero/error","below":"first/zero/error","includes":[从低到高逐档布尔值],"rules":[条款ID]}；原文为纯数值下限表才可设 interpretation="thresholds"。
所有业务运算必须有所在阶段条款ID。未知算子不得省略；用questions说明确切缺口及影响。'''
EXPRESSION_HELP+='\n支持的op：'+', '.join(OPERATORS)
EXPRESSION_HELP+='\n同一物理阶梯表可用紧凑 layout 替代 bands：纵向 {"orientation":"rows","table":"T编号","rows":[实际行号],"band_column":实际区间列整数,"rate_column":实际费率列整数}；横向 {"orientation":"columns","table":"T编号","columns":[实际列号],"band_row":实际行号,"rate_row":实际行号}。也可用sheet与实际列字母。不能省略真实档位。'

DATASET_HELP='''数据集合同：datasets={"语义ID":{"label":"用途","fields":{"语义字段":{"kind":"id/text/number/money/ratio/month/date/bool","nullable":false,"unit":"raw/percent/wan/yi（可省默认raw）","normalize":"trim/casefold（可省）","minimum":明确下界（可省）, "maximum":明确上界（可省）, "allowed":[明确允许原值]（可省）, "rules":[字段校验条款ID]（可省）}},"sources":[{"sheet":"实际S编号","header_row":实际行号,"columns":{"语义字段":"列字母"},"start_row":首数据行（可省）, "end_row":末行（可省）, "skip":[{"column":"实际判断列","values":[实际排除原值],"reason":"排除理由","rules":[条款ID]}]（可省）}],"grain":{"kind":"detail/unique","keys":[行身份字段，如预算唯一对象或明细单号；明细无唯一号可空],"duplicates":"error/identical","rules":[条款ID]},"relationships":[{"fields":[外键字段],"target":"已定义的目标数据集ID","target_fields":[真实目标键],"cardinality":"exactly_one/at_least_one/optional_unique","rules":[条款ID]}]（可省）,"rules":[条款ID]}}。
同一语义数据集可绑定多源，每个来源单独列位置/表头；不同来源单位时 columns字段值可为{"column":"实际列","unit":"raw/percent/wan/yi"}。实际标题提供月度的宽表可用 constants:{"月份字段":{"period":"month"}}；不能把不知月份的表当本月。同一语义列恰有一个来源；缺值保持null，0不是缺失。先明确原始明细还是已汇总唯一对象，再定义去重/加总；不要根据金额猜。'''
DATASET_HELP+='\n若实际表存在不参与的非空物理行段，必须注明 excluded_ranges:[{"start":实际行,"end":实际行,"reason":"为何不参与","rules":[实际条款ID]}]，不能截断未解释的物理行。制度表若当参数查找数据集使用，dataset.purpose="lookup"；不可由它创建项目主集合。实际原始外键须用relationships校验全表，不只验证有计算结果的匹配行。'
DATASET_HELP+='\n多来源的同一语义字段若在某一真实来源完全没有列，必须 fields 中声明 nullable:true，且该来源使用 absent_fields:{"该语义字段":{"reason":"实际表头缺少此字段的证据","rules":[相关原文条款ID]}}；程序填真实缺失null并保留缺列来源，不得绑定不相关列、填0或假设业务状态。非空字段与粒度键不能声明缺列；每个来源的columns/constants/absent_fields合计应恰好覆盖语义字段。'
DATASET_HELP+='\n数据集只读真实物理字段或有明确来源的常量。求和、计数、是否满足出货/资格条件等派生指标应在后续公式节点编译，不能声明成所有来源均absent_fields的原始字段。原始明细可有多个相同项目键；population.keys定义主对象分组，不要把同一批明细重新声明为已经汇总的唯一项目数据集。'


def _empty_fragment():return {'metrics':{},'parameters':{},'schedules':{},'checks':[],'formula_bindings':{}}


def _merge_map(target,incoming,label,owner):
    if not isinstance(incoming,dict):raise ProgramError(f'{label} 需对象',owner=owner)
    clash=set(target)&set(incoming)
    if clash:raise ProgramError(f'{label} 重复声明：{sorted(clash)}',owner=owner)
    target.update(deepcopy(incoming))


def _validate_fragment(fragment,owner,*,funding=False,period=None):
    contract(fragment,required=FRAGMENT_FIELDS+ (('funding',) if funding else ()),label=f'{owner} 计算片段',owner=owner)
    if not isinstance(fragment['checks'],list):raise ProgramError('检查项必须为列表',owner=owner)
    for k in ('metrics','parameters','schedules','formula_bindings'):
        if not isinstance(fragment[k],dict):raise ProgramError(f'{owner}/{k} 必须为对象',owner=owner)
    canonical_program(fragment,period)
    for metric in fragment['metrics'].values():
        if not isinstance(metric,dict):raise ProgramError('指标必须为对象',owner=owner)
        if metric.get('owner',owner)!=owner:raise ProgramError('指标不能改变责任节点',owner=owner)
        metric['owner']=owner
    for check in fragment['checks']:
        if not isinstance(check,dict):raise ProgramError('检查项必须为对象',owner=owner)
        if check.get('owner',owner)!=owner:raise ProgramError('检查不能改变责任节点',owner=owner)
        check['owner']=owner
    return fragment


def _merge_fragment(target,incoming,owner):
    for k in ('metrics','parameters','schedules','formula_bindings'):_merge_map(target[k],incoming[k],k,owner)
    target['checks'].extend(deepcopy(incoming['checks']))
    return target


def _clauses_for(obligations,phase):return {k:v for k,v in obligations.items() if v['phase']==phase}


def _compact(rules):return {k:{'meaning':v['meaning'],'kind':v['kind'],'phase':v['phase'],**({'binding_assertions':v['binding_assertions']} if v.get('binding_assertions') else {})} for k,v in rules.items()}


def _binding_context(catalog,inventory,stage):
    # Provide actual header/coordinate candidates for every data or mixed
    # source. Names and language do not decide whether it is visible.
    return [dict(s.profile(1,compact=True),material_kind=inventory['sheets'][sid]['kind']) for sid,s in catalog.sheets.items() if inventory['sheets'][sid]['kind'] in ('data','mixed','rule')]


def _schema_summary(data,*,physical=False):
    summary={k:{'label':v['label'],'fields':v['fields'],'grain':v['grain']['keys'],'sources':[s['sheet'] for s in v['sources']]} for k,v in data.items()}
    if physical:
        for k,v in data.items():summary[k].update(physical_bindings=v['sources'],grain_contract=v['grain'],relationships=v.get('relationships',[]),rules=v['rules'],purpose=v.get('purpose','business'))
    return summary


def _metric_summary(metrics):return {k:{'label':v['label'],'type':v.get('type'),'owner':v.get('owner')} for k,v in metrics.items()}


def source_attributes(scope):
    """Expose raw attributes only when the physical grain proves one row per object."""
    pop=scope['population'];ds=scope['datasets'].get(pop['dataset'])
    if not ds:return {}
    grain=ds['grain']
    if grain['kind']!='unique' or not grain['keys'] or not set(grain['keys'])<=set(pop['keys']):return {}
    result={}
    for field,spec in ds['fields'].items():
        if field in pop['keys']:continue
        ident=f'source:{pop["dataset"]}:{field}'
        query={'dataset':'$project','aggregate':'unique','match':{},'value':{'field':field},
               'empty':'null' if spec['nullable'] else 'error','nulls':'ignore' if spec['nullable'] else 'error','duplicates':'error'}
        result[ident]={'label':spec.get('label',field),'expr':{'query':query},'type':{'money':'number','ratio':'number','number':'number','date':'date','bool':'bool'}.get(spec['kind'],'text'),
                       'rules':list(dict.fromkeys(spec.get('rules') or ds.get('rules') or pop['rules'])),'owner':'scope_mapping'}
    return result


def _project_source_expr(expr,scope,attributes,*,row=False):
    """A project field isn't a row field unless the grain/key proves its value."""
    if not isinstance(expr,dict):return expr
    pop=scope['population'];by_field={m['expr']['query']['value']['field']:key for key,m in attributes.items()}
    if set(expr)=={'field'} and not row:
        f=expr['field']
        if f in pop['keys']:return {'key':f}
        if f in by_field:return {'ref':by_field[f]}
    if set(expr)=={'op','args'} and isinstance(expr['args'],list):return {'op':expr['op'],'args':[_project_source_expr(x,scope,attributes,row=row) for x in expr['args']]}
    if set(expr)=={'tier'} and isinstance(expr['tier'],dict) and 'value' in expr['tier']:
        return {'tier':{**expr['tier'],'value':_project_source_expr(expr['tier']['value'],scope,attributes,row=row)}}
    if set(expr)=={'query'} and isinstance(expr['query'],dict):
        q=expr['query'];f=q.get('value',{}).get('field') if isinstance(q.get('value'),dict) and set(q['value'])=={'field'} else None
        same_object=(q.get('dataset')=='$project' and q.get('match')=={} or
                     q.get('dataset')==pop['dataset'] and q.get('match')=={k:{'key':k} for k in pop['keys']})
        if not row and f in by_field and same_object and 'where' not in q and q.get('aggregate') in ('unique','min','max'):
            # A max/min of a proven singleton non-null row is the same raw
            # field. Preserve null semantics on nullable attributes.
            target=attributes[by_field[f]]['expr']['query'];nullable=scope['datasets'][pop['dataset']]['fields'][f]['nullable']
            if not nullable and q.get('empty')=='error' and q.get('nulls')=='error' or q==target:return {'ref':by_field[f]}
        q=dict(q)
        if isinstance(q.get('match'),dict):q['match']={k:_project_source_expr(x,scope,attributes,row=row) for k,x in q['match'].items()}
        for k in ('where','value'):
            if k in q:q[k]=_project_source_expr(q[k],scope,attributes,row=True)
        return {'query':q}
    return expr


def _replace_formula_atom(expr,atom,ref):
    """Replace a real project-level operand, never a row-dependent query subtree."""
    if expr==atom:return ref,1
    if isinstance(expr,dict) and set(expr)=={'op','args'} and isinstance(expr['args'],list):
        args=[];n=0
        for item in expr['args']:
            out,count=_replace_formula_atom(item,atom,ref);args.append(out);n+=count
        return {'op':expr['op'],'args':args},n
    return expr,0


def bind_formula_atoms(fragment,available,scope,obligations,phase,period):
    """Materialize actual model-selected source operands as named DAG metrics.

    No formula or aggregation choice is invented. The identical query/tier
    subtree already present in the candidate is lifted verbatim, and the
    original literal formula is still compared to the resulting expression.
    """
    attrs=source_attributes(scope);metrics=fragment['metrics']
    _canonical_parameter_sources(fragment.get('parameters',{}),obligations)
    for m in metrics.values():m['expr']=_project_source_expr(m['expr'],scope,attrs)
    for c in fragment['checks']:c['expr']=_project_source_expr(c['expr'],scope,attrs)
    for rid,binding in list(fragment['formula_bindings'].items()):
        if rid not in obligations:raise ProgramError(f'公式绑定引用不存在的原文条款 {rid}',owner=phase,kind='coverage')
        literal=obligations[rid].get('formula')
        if not literal:
            # Prose still has an operative rule receipt; it is not a literal
            # formula and must not invent a second symbolic contract.
            fragment['formula_bindings'].pop(rid);continue
        if not isinstance(binding,dict) or not isinstance(binding.get('symbols'),dict) or binding.get('target') not in metrics:continue
        target=metrics[binding['target']];symbols=binding['symbols']
        for symbol,raw in list(symbols.items()):
            expr=_project_source_expr(canonical_expression(raw,period),scope,attrs);name=normalized(symbol)
            if re.fullmatch(r'[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:%|万元|亿元|万|亿|元)?',name) and re.search(r'(?<![\d.])'+re.escape(name)+r'(?![\d.])',normalized(literal)):
                try:
                    scalar=expr.get('num') if isinstance(expr,dict) and set(expr)=={'num'} else expr if isinstance(expr,(int,float)) and not isinstance(expr,bool) else None
                    if scalar is not None and to_decimal(scalar)==to_decimal(name.removesuffix('元')):symbols.pop(symbol);continue
                except (ValueError,TypeError):pass
            symbols[symbol]=expr
            if not isinstance(expr,dict) or set(expr) not in ({'query'},{'tier'}):continue
            existing=next((k for k,v in {**available,**metrics}.items() if v.get('expr')==expr),None)
            helper=existing or 'source_read:'+fingerprint({'symbol':symbol,'expr':expr})[:20]
            if helper==binding['target']:continue
            original=target['expr'];subtree=original
            path=binding.get('path',[])
            try:
                for part in path:subtree=subtree[part]
            except (KeyError,TypeError,IndexError):continue
            replacement,count=_replace_formula_atom(subtree,expr,{'ref':helper})
            if not count:continue
            if not existing:
                if helper in metrics or helper in available:raise ProgramError('源读取辅助指标ID冲突',owner=phase)
                metrics[helper]={'label':symbol,'expr':expr,'rules':[rid],'owner':phase}
            if path:
                parent=original
                for part in path[:-1]:parent=parent[part]
                parent[path[-1]]=replacement
            else:target['expr']=replacement
            symbols[symbol]={'ref':helper}


def _metric_refs(expr):
    if isinstance(expr,dict):
        if set(expr)=={'ref'} and isinstance(expr['ref'],str):return {expr['ref']}
        return set().union(*(_metric_refs(v) for v in expr.values()))
    if isinstance(expr,list):return set().union(*(_metric_refs(v) for v in expr))
    return set()


def _typed_formula_bindings(value,obligations,owner):
    """Normalize a model's list only when the actual rule IDs are explicit."""
    if isinstance(value,dict):return value
    if not isinstance(value,list):raise ProgramError('公式符号绑定须为条款ID到合同的映射',owner=owner)
    result={}
    for item in value:
        if not isinstance(item,dict) or not isinstance(item.get('rules'),list) or any(rid not in obligations for rid in item['rules']):
            raise ProgramError('公式列表缺真实原文条款ID',owner=owner,kind='coverage')
        actual=[rid for rid in item['rules'] if obligations[rid].get('formula')]
        if not actual:continue  # actual nonliteral rules retain their normal coverage
        if len(actual)!=1 or actual[0] in result or not {'target','symbols'}<=set(item):raise ProgramError('每个原文明确公式需要单独符号绑定',owner=owner,kind='coverage')
        result[actual[0]]={k:item[k] for k in ('target','symbols','path') if k in item}
    return result


def _canonical_parameter_sources(parameters,obligations):
    if not isinstance(parameters,dict):return
    for p in parameters.values():
        if not isinstance(p,dict) or not isinstance(p.get('source'),dict):continue
        src=p['source'];rid=src.get('block')
        # A model may quote the actual clause ID it was shown rather than
        # the document-unit ID. Only map a real ledger clause to its exact
        # original source; the source reader still verifies the quotation.
        if isinstance(rid,str) and rid in obligations:src['block']=obligations[rid]['unit']


def canonical_person_candidate(v,data,obligations,period):
    """Use the same exact expression/source protocol in the personnel DAG."""
    contract(v,required=('program','identity_key','name_metric','attributed_metric','contribution_amount','rules','parameters','schedules','formula_bindings'),label='人员层指标',owner='roster')
    canonical_program(v['program'],period)
    v['formula_bindings']=_typed_formula_bindings(v['formula_bindings'],obligations,'roster')
    _canonical_parameter_sources(v['parameters'],obligations)
    program=v['program'];pscope={'datasets':data,'population':program['population']};attrs=source_attributes(pscope)
    bindings=deepcopy(v['formula_bindings'])
    for b in bindings.values():
        if isinstance(b,dict) and isinstance(b.get('target'),str) and b['target'].startswith('person:'):b['target']=b['target'][7:]
    local={'metrics':program['metrics'],'checks':program.get('checks',[]),'formula_bindings':bindings}
    for ident in set(attrs)&set(local['metrics']):
        expr=local['metrics'][ident].get('expr')
        if expr!=attrs[ident]['expr'] and _project_source_expr(expr,pscope,attrs)!={'ref':ident}:raise ProgramError('人员指标ID与程序保留的实际底层属性冲突',owner='roster')
        local['metrics'].pop(ident)
    bind_formula_atoms(local,attrs,pscope,obligations,'roster',period)
    used=_metric_refs([m['expr'] for m in local['metrics'].values()]+[c['expr'] for c in local['checks']])
    used.update([v['name_metric'],v['attributed_metric'],*(program['funding'][k] for k in ('total','payable','alloc')),*program['funding']['destinations']])
    for ident in sorted(used&set(attrs)):
        m=deepcopy(attrs[ident]);m['owner']='roster';m['label']='原始字段：'+m['label'];local['metrics'][ident]=m
    for b in bindings.values():
        if isinstance(b,dict) and isinstance(b.get('target'),str) and b['target'] in local['metrics']:b['target']='person:'+b['target']
    v['formula_bindings']=bindings
    for m in program['metrics'].values():m['owner']='roster'
    for check in program.get('checks',[]):check['owner']='roster'
    return v


def direct_funding_contract(phase_rules,obligations,combined):
    """Resolve a proven one-owner, no-pool handoff without inventing deductions.

    This optimization is deliberately narrow: an explicitly direct assignment,
    no primary deduction policy, exactly one terminal literal money formula,
    and no unconnected primary numeric metric. Otherwise the normal model
    selection/review is required. Personnel arithmetic still awaits approval.
    """
    if phase_rules:return None
    primary={rid:r for rid,r in obligations.items() if r['phase'] in METRIC_PHASES}
    if any(re.search(r'暂扣|扣减|冻结|抵扣|预发|留存|扣款|计税',r['quote']) for r in primary.values()):return None
    assignment={rid:r for rid,r in obligations.items() if r['phase']=='roster' and r.get('operation')=='assignment'}
    direct=[]
    for rid,r in assignment.items():
        quote=normalized(r['quote'])
        single=re.search(r'每(?:笔|个|项|单|件|张)?[^,，。；;]{0,16}?(?:仅|只)(?:归属于?|对应|分配给)(?:一名|一个|一位|单一|唯一)(?:员工|人员|收款人|业务员|销售员|责任人)',quote)
        no_pool=re.search(r'不(?:设|设置|使用|进行|采用)[^,，。；;]{0,20}(?:奖金池|奖励池|权重分配|人员分摊|角色分配)',quote)
        if single and no_pool:direct.append(rid)
    if not direct:return None
    metrics=combined['metrics'];bindings={rid:b for rid,b in combined['formula_bindings'].items() if rid in primary and primary[rid].get('formula') and b.get('target') in metrics}
    targets={b['target'] for b in bindings.values()}
    def dependencies(mid):
        found=set();todo=[mid]
        while todo:
            m=todo.pop()
            if m in found or m not in metrics:continue
            found.add(m);todo.extend(_metric_refs(metrics[m]['expr']))
        return found
    terminal=[m for m in targets if not any(m in dependencies(other) for other in targets if other!=m)]
    if len(terminal)!=1:return None
    metric=terminal[0];deps=dependencies(metric)
    if any(k not in deps and v.get('owner') in METRIC_PHASES and v.get('type') not in ('text','date','bool') for k,v in metrics.items()):return None
    formula_rules=[rid for rid,b in bindings.items() if b['target']==metric]
    lhs=[]
    for rid in formula_rules:
        left=normalized(primary[rid]['quote']).split('=',1)[0]
        label=re.split(r'[。；;，,|：:]',left)[-1]
        if re.search(r'奖金|提成|佣金|奖励金额|报酬|应发金额|结算金额',label):lhs.append(label)
    if not lhs or len(set(lhs))!=1:return None
    expr=metrics[metric]['expr'];digits=None
    if isinstance(expr,dict) and expr.get('op')=='round' and isinstance(expr.get('args'),list) and len(expr['args'])==2:
        raw=expr['args'][1]
        if isinstance(raw,dict) and set(raw)=={'num'}:raw=raw['num']
        try:
            value=to_decimal(raw)
            if value==int(value) and 0<=int(value)<=8:digits=int(value)
        except (ValueError,TypeError,ArithmeticError):pass
    if digits is None:return None
    # Explicit final-display precision is separate from the primary formula.
    display=[];cn={'零':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8}
    for rid,r in obligations.items():
        for hit in re.finditer(r'(?:最终|汇总|合计)金额(?:统一)?(?:显示|保留)([0-8零一二两三四五六七八])位小数',normalized(r['quote'])):
            display.append((rid,cn[hit[1]] if hit[1] in cn else int(hit[1])))
    if display and len({n for _,n in display})!=1:return None
    if display:digits=display[0][1]
    ids=list(dict.fromkeys([*formula_rules,*direct,*(rid for rid,_ in display)]));label=lhs[0]
    funding={'total':metric,'payable':metric,'alloc':metric,'destinations':{},'labels':{'total':label+'总额','alloc':label+'待归属金额'},'summary_digits':digits,'rules':ids}
    return funding,{'terminal_formula':metric,'dependencies':sorted(deps),'direct_assignment_rules':direct,'formula_rules':formula_rules,'source_quotes':{rid:obligations[rid]['quote'] for rid in ids}}


def _source_context(catalog,units,clauses):
    selected={v['unit']:units[v['unit']] for v in clauses.values()}
    tids={u['table'] for u in selected.values() if 'table' in u}
    # The exact text unit/page that defines a rate may be next to the native
    # table. Retrieve all referenced page tables, not a fixed title keyword.
    for u in selected.values():
        hit=re.search(r'第(\d+)页',u['where'])
        if hit:tids.update(t.id for t in catalog.tables.values() if t.file==u['file'] and t.page==int(hit[1]))
    cells=[]
    for uid,u in selected.items():
        m=re.fullmatch(r'(S\d+)R(\d+)',uid)
        if m:
            s=catalog.sheets[m[1]];row=int(m[2]);cells.append({'sheet':s.id,'row':row,'cells':{get_column_letter(i):str(s.literal(row,i,display=True)) if s.literal(row,i,display=True) is not None else '' for i in range(1,len(s.rows[row-1])+1)}})
            tids.update(t.id for t in catalog.tables.values() if t.file==s.file and t.sheet_name==s.name)
    return {'rules':clauses,'original_units':selected,'parameter_tables':[catalog.table(t).profile() for t in sorted(tids)],'rule_sheet_cells':cells}


def _probe_plan(population,fragment):
    # Only a local, unpublished probe to validate the completed metric prefix.
    # The actual final funding contract is never supplied by this placeholder.
    metrics=deepcopy(fragment['metrics']);pid='__validation_only_zero__'
    if pid in metrics:raise ProgramError('指标名称使用了内部验证保留ID')
    metrics[pid]={'label':'局部验证占位（不发布）','expr':0,'rules':[]}
    return {'population':population,'metrics':metrics,'checks':fragment['checks'],
            'funding':{'total':pid,'payable':pid,'alloc':pid,'destinations':{},'labels':{'total':'验证总额','alloc':'验证分配'},'summary_digits':2,'rules':[]}}


def _compile_prefix(catalog,datasets,population,fragment,period,*,calculate=True,allow_unbound=False,owner='scope_mapping'):
    inputs=compile_inputs(catalog,datasets,fragment['parameters'],fragment['schedules'],period=period,allow_unbound=allow_unbound,owner=owner)
    program=CalculationProgram(inputs,_probe_plan(population,fragment),period)
    if calculate:program.basis()
    return program


def apply_literal_source_exclusions(catalog,scope,rules):
    """Compile explicit quoted column/value exclusions from actual rules.

    The scope compiler owns physical row selection; a model cannot turn an
    exact text marker into an inferred numeric status or omit a confirmed
    exclusion on one of several sources.
    """
    pop=scope['population'];ds=scope['datasets'].get(pop['dataset'],{})
    applied=[]
    pattern=r'「([^」]+)」列(?:值)?(?:为|等于|标记为)「([^」]+)」[^。；;]{0,80}?(?:不参与(?:核算|计算|统计)|不纳入(?:核算|计算|统计)|均排除|全部排除)'
    for rid,rule in rules.items():
        quote=rule['quote']
        if rule.get('phase')!='scope_mapping' or re.search(r'例如|示例|是否|待确认',quote):continue
        for m in re.finditer(pattern,quote):
            heading,value=m[1].strip(),m[2].strip();found=[]
            for src in ds.get('sources',[]):
                sheet=catalog.sheet(src);h=src['header_row']
                if type(h) is not int or not 1<=h<=len(sheet.rows):continue
                cols=[get_column_letter(i) for i,v in enumerate(sheet.rows[h-1],1) if v is not None and normalized(v)==normalized(heading)]
                for col in cols:
                    found.append(f'{sheet.id}!{col}{h}')
                    skips=src.setdefault('skip',[])
                    old=next((item for item in skips if item.get('column')==col and value in item.get('values',[])),None)
                    if old is None:skips.append({'column':col,'values':[value],'reason':quote,'rules':[rid]})
                    else:old['rules']=list(dict.fromkeys(old.get('rules',[])+[rid]))
            if not found:raise ProgramError(f'原文明确要求按「{heading}」列的「{value}」标记排除，但主数据源没有实际同名物理列',owner='scope_mapping',kind='binding',rules=[rid],details={'actual_rule':quote})
            applied.append(rid)
    pop['rules']=list(dict.fromkeys(pop['rules']+applied))
    return applied


def validate_explicit_zero_exclusion(catalog,scope,rules,program):
    """Check explicit zero-only exclusions using source labels and actual DSL.

    A sign-sensitive expression (e.g. x != abs(x)) is not equivalent to the
    literal instruction x != 0. Other actual source restrictions remain
    permitted; only isolated predicates on the stated field are probed.
    """
    pop=scope['population'];ds=scope['datasets'][pop['dataset']]
    labels=defaultdict(set)
    for src in ds['sources']:
        sheet=catalog.sheet(src);h=src['header_row']
        for field,binding in src['columns'].items():
            if ds['fields'][field]['kind'] not in ('number','money','ratio'):continue
            col=binding['column'] if isinstance(binding,dict) else binding
            heading=normalized(sheet.literal(h,column_index_from_string(col),display=True))
            labels[field].update([heading,re.sub(r'\([^)]*\)$','',heading)])
    def field_refs(expr):
        if isinstance(expr,dict):
            if set(expr)=={'field'}:return {expr['field']}
            return set().union(*(field_refs(x) for x in expr.values()))
        if isinstance(expr,list):return set().union(*(field_refs(x) for x in expr))
        return set()
    def conjuncts(expr):
        if isinstance(expr,dict) and expr.get('op')=='and':return [x for a in expr.get('args',[]) for x in conjuncts(a)]
        return [expr]
    for field,headings in labels.items():
        zero_rules=[]
        for rid,r in rules.items():
            raw=normalized(r['quote'])
            if any(h and re.search(re.escape(h)+r'(?:为|等于|=)0(?:元)?[^。；;]{0,80}(?:不计算|不参与|不计入|不纳入|排除|剔除)',raw) for h in headings):zero_rules.append(rid)
        if not zero_rules:continue
        selected=program._population_rows
        bad=next((row for row in selected if row.values.get(field)==0),None)
        if bad:raise ProgramError(f'原文明确零值不参与，但实际主集合保留 {field}=0',owner='scope_mapping',kind='coverage',location=bad.source,rules=zero_rules)
        # If another original rule explicitly restricts the same amount's
        # sign or threshold, do not equate the entire amount filter to !=0.
        more=any(any(h and h in normalized(r['quote']) for h in headings) and re.search(r'大于|小于|超过|少于|正数|负数|正值|负值|为正|为负|[<>≥≤]',r['quote']) and re.search(r'只|仅|排除|不参与|不计算|剔除',r['quote']) for r in rules.values())
        isolated=[x for x in conjuncts(pop['where']) if field_refs(x)=={field}]
        if not isolated or more:continue
        probes=[-1,0,1]
        for value in probes:
            actual=all(program._bool(program.eval(x,Context(row=Record({field:to_decimal(value)},'原文零值规则反例')))) for x in isolated)
            if actual!=(value!=0):
                raise ProgramError(f'原文该字段仅明确排除0，但当前独立字段筛选在 {field}={value} 时结果为 {actual}，不等价于非零；其他条件应按各自真实条款另行筛选',owner='scope_mapping',kind='coverage',rules=zero_rules,details={'field':field,'test_value':value,'expected':value!=0,'actual':actual,'actual_predicates':isolated})


def _financial_metrics(program):
    f=program.funding;todo=[f['total'],f['payable'],f['alloc'],*f['destinations']];found=set()
    while todo:
        x=todo.pop()
        if x in found:continue
        found.add(x);todo.extend(program.deps[x])
    return found


def _validate_data_use(inventory,datasets,*,complete=False):
    used={s['sheet'] for d in datasets.values() for s in d['sources']}
    for dataset,d in datasets.items():
        for source in d['sources']:
            sid=source['sheet'];kind=inventory['sheets'].get(sid,{}).get('kind')
            if sid not in inventory['sheets']:
                raise ProgramError(f'{dataset} 的source.sheet={sid!r} 不是实际S编号。必须保留physical_sheets中的真实ID，不能改用工作表名称或猜测位置。',owner='scope_mapping',kind='binding',dataset=dataset,details={'invalid_sheet_id':sid,'actual_sheet_ids':list(inventory['sheets'])})
            if kind not in ('data','mixed') and not (kind=='rule' and d.get('purpose')=='lookup'):
                raise ProgramError(f'数据集错误使用非数据材料 {sid}；制度表仅可明示为lookup参数关联源',owner='scope_mapping',kind='coverage')
    if complete:
        missing={sid for sid,v in inventory['sheets'].items() if v['kind'] in ('data','mixed')}-used
        if missing:raise ProgramError(f'已识别业务数据表尚未绑定：{sorted(missing)}；需核对是否遗漏指标/人员或重新认定材料用途',owner='reference_mapping',kind='coverage')


def _validate_global_bindings(catalog,inventory,obligations,datasets,*,owner,complete=False):
    """Global physical directives must hold on actual source contracts.

    When the source explicitly governs all input tables, binding the personnel
    master before approval is a data-quality check, not calculating any person's
    payout. Existing datasets can be reused by the later attribution program.
    """
    requirements=[(rid,a) for rid,c in obligations.items() for a in c.get('binding_assertions',[])]
    if not requirements:return
    if complete:_validate_data_use(inventory,datasets,complete=True)
    for name,ds in datasets.items():
        for src in ds['sources']:
            sheet=catalog.sheets[src['sheet']]
            if inventory['sheets'][sheet.id]['kind']=='rule':continue
            location=f'{sheet.file}/{sheet.name}'
            for rid,a in requirements:
                kind=a['kind'];expected=a['value'];h=src['header_row']
                if kind in ('header_row','data_start'):
                    actual=h if kind=='header_row' else src.get('start_row',h+1)
                    if actual!=expected:
                        raise ProgramError(f'{location} 实际绑定第{actual}行与制度物理绑定「{a["source"]}」不符',owner=owner,kind='binding',dataset=name,location=location,rules=[rid],details={'expected':expected,'candidate':actual})
                elif kind=='money_unit':
                    for f,column in src['columns'].items():
                        field=ds['fields'][f];col=column['column'] if isinstance(column,dict) else column
                        unit=column.get('unit',field.get('unit','raw')) if isinstance(column,dict) else field.get('unit','raw')
                        header=sheet.literal(h,column_index_from_string(col),display=True)
                        # A generic number field with a monetary source label
                        # also cannot bypass an explicit amount-unit rule.
                        money=field['kind']=='money' or field['kind']=='number' and bool(re.search(r'(?:金额|额|amount)(?:\s*[（(][^）)]*[）)])?$',str(header),re.I))
                        if money and unit!=expected:
                            where=f'{location}!{col}{h}';label=field.get('label',f)
                            raise ProgramError(f'{where} 金额字段「{label}」单位 {unit} 与制度金额单位「{a["source"]}」不符',owner=owner,kind='binding',dataset=name,metric=f,location=where,rules=[rid],details={'candidate_unit':unit,'expected_unit':expected})


def _restore_obligations(catalog,saved):
    units=rule_units(catalog,saved['inventory'])
    obligations,decisions=validate_clause_chunk(units,{'units':saved['clause_proposals']})
    return units,obligations,decisions


def replay_basis(catalog,saved,period):
    """Recompile only original, current-version bindings and actual rule units."""
    _,obligations,decisions=_restore_obligations(catalog,saved)
    parts=saved['parts'];scope=parts['scope_mapping'];reference=parts['reference_mapping']
    scope_rules=_clauses_for(obligations,'scope_mapping');rechecked=deepcopy(scope)
    apply_literal_source_exclusions(catalog,rechecked,scope_rules)
    if rechecked!=scope:raise ProgramError('已保存主集合未包含当前原文明确的物理标记排除规则，需要重新绑定',owner='scope_mapping',kind='coverage')
    datasets=deepcopy(scope['datasets']);_merge_map(datasets,reference['datasets'],'数据集','reference_mapping')
    _validate_data_use(saved['inventory'],datasets)
    _validate_global_bindings(catalog,saved['inventory'],obligations,datasets,owner='reference_mapping',complete=True)
    fragment=_empty_fragment();attrs=source_attributes(scope)
    if 'source_attributes' in parts and parts['source_attributes'].get('metrics')!=attrs:
        raise ProgramError('底层原始属性与实际唯一粒度合同不一致',owner='scope_mapping',kind='binding')
    fragment['metrics'].update(attrs)
    for phase in METRIC_PHASES:_merge_fragment(fragment,_validate_fragment(deepcopy(parts[phase]),phase,funding=phase=='hold_policy',period=period),phase)
    fragment['checks'][:0]=deepcopy(reference.get('checks',[]))
    plan={'population':scope['population'],'metrics':fragment['metrics'],'checks':fragment['checks'],'funding':parts['hold_policy']['funding']}
    owners={'datasets':{**{k:'scope_mapping' for k in scope['datasets']},**{k:'reference_mapping' for k in reference['datasets']}},
        'parameters':{k:ph for ph in METRIC_PHASES for k in parts[ph]['parameters']},'schedules':{k:ph for ph in METRIC_PHASES for k in parts[ph]['schedules']}}
    inputs=compile_inputs(catalog,datasets,fragment['parameters'],fragment['schedules'],period=period,owners=owners)
    p=CalculationProgram(inputs,plan,period)
    for m in p.metrics.values():validate_numeric_literals(m['expr'],m['rules'],obligations,owner=m.get('owner','project_policy'))
    verify_parameter_usage(fragment['parameters'],fragment['schedules'],{'metrics':p.metrics,'population':p.population,'checks':p.checks},obligations,owner='hold_policy')
    proof=coverage(obligations,{ph:parts[ph] for ph in BASIS_PHASES},phases=BASIS_PHASES,
                   formula_bindings=fragment['formula_bindings'],metrics=fragment['metrics'],parameters=inputs.parameters,
                   keys=p.keys,financial_metrics=_financial_metrics(p))
    basis=p.basis();validate_explicit_zero_exclusion(catalog,scope,scope_rules,p)
    basis['rule_coverage']=proof;basis['data_bindings']=inputs.diagnostics
    # Roster/output rule decisions are not part of the monetary basis. They
    # are still retained in the full manifest and exported at finalization.
    basis_units={v['unit'] for v in obligations.values() if v['phase'] in BASIS_PHASES}
    basis['rule_unit_decisions']={k:{'disposition':v['disposition'],'reason':v['reason'],'file':v['source']['file'],'where':v['source']['where']} for k,v in decisions.items() if k in basis_units}
    return p,basis,obligations,datasets,fragment


def replay_settlement(catalog,saved,roster,period,*,basis=None,overrides=(),attribution_only=False):
    p,current,obligations,datasets,fragment=replay_basis(catalog,saved,period)
    if basis is not None and fingerprint(basis)!=fingerprint(current):raise ProgramError('分配前项目金额、规则及来源回执发生变化',owner='roster')
    _merge_map(datasets,roster['datasets'],'名单数据集','roster')
    _validate_data_use(saved['inventory'],datasets,complete=True)
    parameters=deepcopy(fragment['parameters']);schedules=deepcopy(fragment['schedules'])
    _merge_map(parameters,roster['parameters'],'人员参数','roster');_merge_map(schedules,roster['schedules'],'人员阶梯','roster')
    # The approved basis snapshot must stay unchanged when roster inputs are
    # added. The calculation program's original basis contexts are preserved.
    p.inputs=compile_inputs(catalog,datasets,parameters,schedules,period=period,owner='roster')
    roster_plan=canonical_roster({k:roster[k] for k in ('roles','rounding','rules')},period)
    p.validate_roster(roster_plan)
    for r in roster['roles'].values():validate_numeric_literals(r['rate'],r['rules'],obligations,owner='roster')
    settlement=p.settlement(roster_plan,current,overrides=overrides)
    if roster.get('distribution_kind')=='direct':
        if len(roster['roles'])!=1 or any(r['destination']!='person' or r['share']!=1 or r['role_rate']!=1 for r in settlement['rows']) or len(settlement['rows'])!=len(current['projects']):
            raise ProgramError('直接归属模式要求每个项目/订单仅一个真实人员，全部归属、不使用权重分配',owner='roster',kind='contract')
    if not attribution_only:
        person_plan=roster.get('person_plan')
        if roster.get('person_program_required',False) and not person_plan:raise ProgramError('人员层有效计算未编译，不能把基础归属金额当最终实发',owner='roster',kind='coverage')
        pm={};pk=[]
        if person_plan:
            canonical_program(person_plan['program'],period)
            pm=person_plan['program']['metrics'];pk=person_plan['program']['population']['keys']
            for m in pm.values():validate_numeric_literals(m['expr'],m['rules'],obligations,owner='roster')
            settlement=apply_person_program(p.inputs,current,settlement,person_plan,period)
            all_people=[rid for rid,r in obligations.items() if r['phase']=='roster' and re.search(r'(?:人员|员工|个人)(?:主)?表[^。；;]{0,40}(?:全部输出|全员输出|所有人员|零奖金|无订单|无项目)|(?:所有|全体|全部)人员[^。；;]{0,30}(?:输出|发放)',r['quote'])]
            if all_people:
                population=person_plan['program']['population'];master=p.inputs.datasets.get(population['dataset'])
                actual=sum(x['destination']=='person' for x in settlement['people'])
                if master is None or actual!=len(master.rows):
                    raise ProgramError('实际原文要求完整人员主表含零金额对象；人员核算全集不能只用有归属记录的员工或过滤原始主表',owner='roster',kind='coverage',rules=all_people,
                                       details={'population_dataset':population['dataset'],'source_rows':len(master.rows) if master else None,'actual_people':actual})
        verify_parameter_usage(roster['parameters'],roster['schedules'],{'roles':roster['roles'],'person':person_plan},obligations,owner='roster')
        proof=coverage(obligations,{'roster':roster},phases=['roster'],formula_bindings=roster['formula_bindings'],metrics=p.metrics,
                   parameters=p.inputs.parameters,keys=p.keys,roles=roster['roles'],person_metrics=pm,person_keys=pk)
        settlement['rule_coverage']=proof
    settlement['input_parameter_evidence']={k:v for k,v in p.inputs.parameter_evidence.items() if k not in current['parameter_evidence']}
    return p,current,settlement,obligations


class AdaptiveCompiler:
    def __init__(self,catalog,period,request,event,state):
        self.catalog=catalog;self.period=period;self.request=request;self.event=event;self.state=state

    def ask(self,name,owner,data,prompt,validate,**options):
        return self.request(name,data,prompt,validate,owner=owner,**options)

    def build_basis(self):
        cat=self.catalog;period=self.period
        unsupported=[f'{x["file"]}/{x["where"]}' for x in cat.unreadable_blocks if not any(f'{x["file"]}/{x["where"]}' in v['sources'] for v in cat.authorized_image_exclusions.values())]
        if unsupported:raise ProgramError('图像型材料缺少可读取规则或真实排除授权',owner='scope_mapping',kind='material',details={'sources':unsupported})
        idx=source_index(cat)
        inventory=self.ask('adaptive/source_inventory','scope_mapping',{'period':period,**idx},
            '盘点材料作用，不编制公式。每张真实S表必须给出 {"kind":"data/rule/mixed/output/context","reason":"真实性质及用途","rule_ranges":[]或实际物理行范围数组[[起行,末行]],"rule_cells":[]或实际单元格坐标数组}。'
            '业务数据及名单为data；规则/数据混合才mixed；不按文件名字面排除未知资料。制度表/输出要求不得用作销售数据。所有文档文字和原生表后续逐条覆盖。'
            '完整制度规则表使用kind=rule、rule_ranges=[]、rule_cells=[]；程序覆盖所有物理行，表头是否为背景在条款层处理。all_rows若存在表示该短表所有物理行，不要猜测未展示内容。混合表中若说明只在某个侧栏或表头单元格，必须使用rule_cells（例如["L1"]），rule_ranges留空；只有实际整行属于规则才标rule_ranges。sparse_columns列出该列全部实际非空坐标和值，严禁将孤立说明推断为整列或后续数据行。产品组合编码、拼接主键和状态明细仍是数据，不因含有+-/而变成公式。逐张核对真实坐标。'
            '输出{"sheets":{"S编号":上述对象},"unreadable":{}}。无法读取的图像需要真实确认非业务规则或补材料，不能猜。'
            ,lambda v:validate_source_inventory(cat,v))
        units=rule_units(cat,inventory);proposals={};rules={}
        self.state['adaptive_rule_sources']={k:{'file':v['file'],'where':v['where'],'text':v['text']} for k,v in units.items()}
        pending={};proofs=self.state.setdefault('adaptive_rule_unit_proofs',{})
        answer_key=fingerprint([(q.get('id'),q.get('answer')) for q in self.state.get('questions',[]) if q.get('related')=='scope_mapping' and q.get('answer') and not q.get('withdrawn')])
        for uid,unit in units.items():
            if unit.get('superseded_entirely'):
                proposals[uid]={'disposition':'context','reason':'已由真实财务规则版本替代：'+','.join(unit['superseded_by']),'clauses':[]}
            elif unit.get('structural_header'):
                proposals[uid]={'disposition':'context','reason':'原生表结构标题；保留实际列名和单元格，不作为单独运算义务','clauses':[]}
            elif uid in proofs and proofs[uid].get('digest')==fingerprint(unit) and proofs[uid].get('answer_key')==answer_key and proofs[uid].get('receipt',{}).get('status')=='PASS' and (candidate:=self.state['specs'].get(proofs[uid].get('task'),{})) and fingerprint(candidate)==proofs[uid]['receipt'].get('output') and candidate.get('units',{}).get(uid)==proofs[uid]['proposal']:
                proposals[uid]=deepcopy(proofs[uid]['proposal'])
            else:pending[uid]=unit
        retained=self.state.get('unchanged_rule_candidates',{});revalidated=[]
        for uid,unit in list(pending.items()):
            digest=fingerprint(unit);old=retained.get(digest)
            if not old or old.get('answer_key')!=answer_key or old.get('file')!=unit.get('file') or old.get('sha256')!=self.state.get('files',{}).get(unit.get('file')):continue
            actual={'units':{uid:deepcopy(old['proposal'])}}
            try:validate_clause_chunk({uid:unit},actual)
            except ProgramError:continue
            task='adaptive/recovered_rule/'+uid;proof={'status':'PASS','output':fingerprint(actual),'reviewer':old['reviewer'],'review':old['review_receipt'].get('review',''),
                'confidence':'unchanged_original_source_real_review_revalidated','historical_task':old['historical_task'],'historical_unit':old['historical_unit'],
                'historical_receipt':fingerprint(old['review_receipt']),'source_sha256':old['sha256'],'recovered':True}
            self.state['specs'][task]=actual;self.state['receipts'][task]=proof;self.state.setdefault('task_owners',{})[task]='scope_mapping'
            proposals[uid]=actual['units'][uid];proofs[uid]={'digest':digest,'answer_key':answer_key,'task':task,'proposal':deepcopy(proposals[uid]),'receipt':proof}
            pending.pop(uid);revalidated.append(uid)
        if revalidated:self.event('adaptive/rules/材料更正后原文重验',True,{'reused_units':revalidated,'note':'来源文件哈希及原文单元一致，真实旧独立复核已重新校验；本次数据绑定、金额及审批均重新执行'})
        # On a technical parser upgrade, recover individually valid clauses
        # from the actual independent model responses in this same case. Do
        # not make a failed large chunk erase unrelated correctly read units.
        if pending and answer_key==fingerprint([]) and 'scope_mapping' not in self.state.get('revision_owners',[]) and not self.state.get('feedback',{}).get('scope_mapping'):
            drafts={};reviewed=[]
            for ev in self.state.get('candidate_archive',[]):
                name=ev.get('node','')
                if not name.startswith('adaptive/rules/'):continue
                if name.endswith('/解析') and ev.get('status')=='PASS':drafts[name.removesuffix('/解析')]=ev.get('result')
                elif name.endswith('/独立复核') and isinstance(ev.get('result'),dict):
                    task=name.removesuffix('/独立复核');review=ev['result'];draft=drafts.get(task)
                    selected=merge_rule_review(draft,review.get('replacement'),(draft or {}).get('units',{})) if review.get('status')=='REVISE' else draft if review.get('status')=='PASS' else None
                    if isinstance(selected,dict) and isinstance(selected.get('units'),dict) and not selected.get('questions') and not review.get('questions'):
                        reviewed.append((task,ev,selected))
            for old_task,ev,candidate in reversed(reviewed):
                for uid in list(pending):
                    if uid not in candidate['units']:continue
                    actual={'units':{uid:deepcopy(candidate['units'][uid])}}
                    try:validate_clause_chunk({uid:pending[uid]},actual)
                    except ProgramError:continue
                    task='adaptive/recovered_rule/'+uid;proof={'status':'PASS','output':fingerprint(actual),'reviewer':ev.get('model'),'review':ev['result'].get('reason',''),
                        'confidence':'same_source_real_independent_response_revalidated','historical_response':fingerprint(ev),'historical_task':old_task,'recovered':True}
                    self.state['specs'][task]=actual;self.state['receipts'][task]=proof;self.state.setdefault('task_owners',{})[task]='scope_mapping'
                    proposals[uid]=actual['units'][uid];proofs[uid]={'digest':fingerprint(pending.pop(uid)),'answer_key':answer_key,'task':task,'proposal':deepcopy(proposals[uid]),'receipt':proof}
            if recovered:= [k for k in proposals if k in proofs and proofs[k].get('receipt',{}).get('recovered')]:
                self.event('adaptive/rules/历史原文逐条重验',True,{'reused_units':recovered,'note':'仅复用同源真实独立复核响应中现程序能完整验证的条款；错误层级或引用重新编制'})
        for chunk in unit_chunks(pending):
            task='adaptive/rules/'+fingerprint(chunk)[:16]
            visible={uid:{**unit,**({'source_lines':source_lines(unit)} if len(source_lines(unit))>1 else {})} for uid,unit in chunk.items()}
            reply=self.ask(task,'scope_mapping',{'period':period,'units':visible},
                '只覆盖当前一小组原文，不构造公式合同或节点名称。逐个真实unit ID返回 {"disposition":"operative/context/output","reason":"实际理由","clauses":[{"quote":"unit.text 或 active_text 内真实连续原文，不许引用title上下文","meaning":"一个可验证业务义务","level":"source/primary/person/report","operation":"population/binding/calculation/adjustment/funding/assignment/validation/parameter/report","kind":"policy/formula/table/validation/output"}]}。\n'
                '枚举严格分开：level只有source、primary、person、report；kind只有policy、formula、table、validation、output；parameter只属于operation。表格比例行通常是kind=table；实际适用到的主对象或人员决定level，而不是因为来源是参数表就归source。繁简混排的多行文字可用source_line（从1计的实际行号或连续行号列表）代替quote，程序直接取原件；不要改写引文。\n'
                '若单元已包含真实财务authority.owner，它仅表示财务正式指定的责任节点；程序会按该节点确定phase和level，你只需提取实际原文义务及其操作，不需要另行更改内部路由。原始制度费率区间是待使用的计算参数，不是业务表绑定。\n'
                '层级：source只表示原始表、关联键、主集合及数据完整性；primary是在订单/项目/实际主对象上计算金额、比率、汇总、舍入；person是确认上游归属后按实际员工/收款人汇总，计奖系数、预发抵扣、人员应得与实发、零人员覆盖和全部角色名单，也包含一单仅归属一个人的直接归属。report只表示输出要求。人员层运算不能放到项目层。实际逐对象求和不能只登记原始数据绑定。\n'
                '动作按原文选择：population主集合、binding字段关联、calculation普通计算、adjustment系数、funding资金归属/总额、assignment人员归属、validation实际检查、parameter制度参数、report实际输出。程序根据level和operation负责内部路由。'
                '标题、纯说明等context必须明示原因；带有效规则、费率或分支不能以context跳过。若来源有active_text仅它仍有效。一个条款只对应一个真实算式；程序直接读取原文算式，不要自行把自然语言改写成原文不存在的公式。跨项目与人员层的段落拆成各自实际原文条款。每个原文单元恰一项。输出{"units":{...}}。',
                lambda v,ch=chunk:validate_clause_chunk(ch,v))
            validate_clause_chunk(chunk,reply);proposals.update(reply['units'])
            for uid,unit in chunk.items():proofs[uid]={'digest':fingerprint(unit),'answer_key':answer_key,'task':task,'proposal':deepcopy(reply['units'][uid]),'receipt':deepcopy(self.state['receipts'][task])}
        rules,decisions=validate_clause_chunk(units,{'units':proposals})
        self.state['adaptive_rule_decisions']={k:{'disposition':v['disposition'],'reason':v['reason'],'file':v['source']['file'],'where':v['source']['where']} for k,v in decisions.items()}
        profiles=_binding_context(cat,inventory,'basis');parts={}
        scope_rules=_clauses_for(rules,'scope_mapping')
        def validate_scope(v):
            contract(v,required=('datasets','population'),label='项目范围绑定',owner='scope_mapping')
            canonical_program(v,period)
            _validate_data_use(inventory,v['datasets'])
            _validate_global_bindings(cat,inventory,rules,v['datasets'],owner='scope_mapping')
            main=v['datasets'].get(v['population'].get('dataset'),{})
            if main.get('purpose')=='lookup' or any(inventory['sheets'][s['sheet']]['kind']=='rule' for s in main.get('sources',[])):
                raise ProgramError('实际项目主集合不能由制度/费率参数表生成',owner='scope_mapping',kind='coverage')
            apply_literal_source_exclusions(cat,v,scope_rules)
            p=_compile_prefix(cat,v['datasets'],v['population'],_empty_fragment(),period,allow_unbound=True)
            validate_explicit_zero_exclusion(cat,v,scope_rules,p)
            v['population']['rules']=list(dict.fromkeys(v['population']['rules']+primary_population_invariants(scope_rules)))
            coverage(rules,{'scope_mapping':v},phases=['scope_mapping'],keys=p.keys)
        scope=self.ask('scope_mapping','scope_mapping',{'period':period,'physical_sheets':profiles,'rules':scope_rules,
                'source_binding_requirements':_compact({k:v for k,v in rules.items() if v['phase']=='reference_mapping' and v['level']=='source'})},
            '只建立真实项目主集合和项目级原始数据绑定，不预制派生数据表。项目键由本次制度指定，不预设月份/客户/产品。项目主集合来自真实业务明细；参考表不擅自新增项目。主集合where为逐明细布尔筛选：例如原文明确仅排除零值，应使用字段!=0，不能使用字段!=abs(字段)或只保留正数。'
            +DATASET_HELP+'\n'+EXPRESSION_HELP+'\n输出{"datasets":{...},"population":{"dataset":"主数据集ID","keys":[由原文决定的任意组合键字段],"where":真实明细筛选表达式（无则true）, "rules":[条款ID]}}。'
            '先覆盖范围、主对象属性和后续计算所需的真实主字段。参考/历史及其他粒度源可留给下一绑定节点；人员主表若是主对象外键目标、参数来源或全表校验对象，也必须在总额确认前绑定并核查实际数据，只有人员金额计算须留到确认后。',validate_scope)
        parts['scope_mapping']=scope;datasets=deepcopy(scope['datasets'])
        ref_rules=_clauses_for(rules,'reference_mapping')
        def validate_ref(v):
            contract(v,required=('datasets','checks'),label='参考源绑定',owner='reference_mapping')
            data=deepcopy(datasets);_merge_map(data,v['datasets'],'数据集','reference_mapping');_validate_data_use(inventory,data)
            _validate_global_bindings(cat,inventory,rules,data,owner='reference_mapping',complete=True)
            if not isinstance(v['checks'],list):raise ProgramError('检查项须列表',owner='reference_mapping')
            canonical_program(v,period)
            for check in v['checks']:
                contract(check,required=('id','expr','message','rules'),optional=('owner',),label='补充源真实校验',owner='reference_mapping')
                if not isinstance(check['expr'],dict):raise ProgramError(f'{check["id"]} 必须为可执行布尔表达式 JSON，不能用文字或常量伪造检查；已有实际relationships无需重复检查',owner='reference_mapping',kind='contract',rules=check['rules'])
                check['owner']='reference_mapping'
            coverage(rules,{'scope_mapping':scope,'reference_mapping':v},phases=['reference_mapping'])
            # Compile the actual merged data now: all explicit foreign-key
            # targets and cardinalities must work before formula requests.
            if v['checks']:
                probe=_empty_fragment();probe['checks']=v['checks'];_compile_prefix(cat,data,scope['population'],probe,period,owner='reference_mapping')
            else:compile_inputs(cat,data,{}, {},period=period,owner='reference_mapping')
        reference=self.ask('reference_mapping','reference_mapping',{'period':period,'physical_sheets':profiles,'rules':ref_rules,
                'global_binding_assertions':{k:v['binding_assertions'] for k,v in rules.items() if v.get('binding_assertions')},
                'required_basis_metrics':_compact({k:v for k,v in rules.items() if v['phase'] in METRIC_PHASES}), 'already_bound':_schema_summary(datasets,physical=True),'population':scope['population']},
            '只绑定补充、历史、预算、考核等真实参考数据。已有主数据集不要重复声明。按当前规则确定期段、单位、粒度、重复记录含义；历史和当期来源不能混淆。所有已声明原始外键的真实目标表均需在本节点完成绑定及实际校验；有适用于全部业务表的明确布局/单位约束时，把全部业务表（含后续人员主表与人员层参数原表）预先绑定并校验，不执行人员金额。'
            +DATASET_HELP+'\n已有already_bound含实际字段、粒度、物理绑定、规则引用和relationships；程序直接执行这些已登记的关系、字段约束、表头及单位检查，已有源规则回执跨两个绑定节点有效。请独立核对它们是否实际完成本节点义务，不重复声明，不虚构检查。仅补尚未绑定的源和无法由现有合同覆盖的真实数据检查。已有关系完整且无新增需求时明确输出{"datasets":{},"checks":[]}。'
            '\n输出{"datasets":{...},"checks":[{"id":"检查ID","expr":真实布尔表达式JSON对象,"message":"不符时的具体含义","rules":[条款ID]}]}。禁止将SQL/集合符号/自然语言/常量true当成可执行表达式；仅本层数据检查，依赖未来指标的检查留后续计算节点。',validate_ref,archive_epoch='physical-binding-evidence-v2')
        parts['reference_mapping']=reference;_merge_map(datasets,reference['datasets'],'数据集','reference_mapping')
        aggregate=_empty_fragment();aggregate['checks']=deepcopy(reference['checks'])
        attrs=source_attributes(scope)
        if attrs:
            parts['source_attributes']={'metrics':attrs};aggregate['metrics'].update(deepcopy(attrs))
            self.event('adaptive/source_attributes',True,{'metrics':list(attrs),'reason':'实际主集合粒度保证每对象唯一源行，保留原始字段及其真实查询'})
        for phase in METRIC_PHASES:
            phase_rules=_clauses_for(rules,phase)
            merged=self._compile_phase(phase,phase_rules,rules,units,datasets,scope,aggregate)
            if phase=='hold_policy':
                combined=deepcopy(aggregate);_merge_fragment(combined,merged,phase)
                def valid_funding(v):
                    probe_parts={**parts,phase:{**merged,'funding':v}}
                    replay_basis(cat,{'inventory':inventory,'clause_proposals':proposals,'parts':probe_parts},period)
                source_formulae={rid:{'quote':rules[rid]['quote'],'target':b['target'],'symbols':b['symbols']} for rid,b in combined['formula_bindings'].items() if rid in rules and rules[rid].get('formula')}
                handoff={rid:{'quote':r['quote'],'meaning':r['meaning']} for rid,r in rules.items() if r['phase']=='roster' and (r.get('operation') in ('assignment','funding') or re.search(r'舍入|四舍五入|不设|不计算',r['quote']))}
                if direct:=direct_funding_contract(phase_rules,rules,combined):
                    funding,proof=direct;valid_funding(funding)
                    task='adaptive/hold_policy/funding';receipt={'status':'PASS','inputs':fingerprint(proof),'output':fingerprint(funding),'reviewer':'程序','review':'实际单一归属原文与唯一终点公式可完全证明，无需创造项目暂扣阶段；后续总额仍进行独立模型复核','confidence':'deterministic_direct_funding'}
                    self.state['specs'][task]=deepcopy(funding);self.state['receipts'][task]=receipt;self.state.setdefault('task_owners',{})[task]=phase
                    self.event(task,True,{'receipt':receipt,'proof':proof})
                else:
                    funding=self.ask('adaptive/hold_policy/funding',phase,{'period':period,'rules':phase_rules,'metrics':_metric_summary(combined['metrics']),
                           'compiled_original_formulas':source_formulae,'actual_handoff_rules':handoff,
                           'stage_contract':{'scope':'primary objects before human total confirmation','allowed_metric_ids':list(combined['metrics']),'person_calculation_after_confirmation':True},
                           'existing_expressions':{k:v['expr'] for k,v in combined['metrics'].items() if v.get('owner')=='hold_policy'}},
                        '只绑定总额确认前主对象层已经实际编译的资金指标，不再创造新公式。总额确认金额不是最终人员实发；无论最终输出要求为何，都禁止在本节点创建或引用尚未编制的人员汇总、个人系数、实发指标。total/payable/alloc必须从stage_contract.allowed_metric_ids中选择。核对compiled_original_formulas和actual_handoff_rules实际计算终点及归属方式。本阶段rules为空表示没有本阶段新增规则，不要求设置暂扣或奖励池；若原文的主对象提成全额直接归属人员且无主对象扣减，total/payable/alloc可指向同一个已计算金额，destinations为空，人员层扣抵留总额确认后。'
                        '输出{"total":"总额指标ID","payable":"未额外舍入的归属金额指标ID","alloc":"按本批舍入后的归属金额指标ID（若无项目舍入可同payable）","destinations":{"其他资金去向指标ID":"报表标签"},"labels":{"total":"总额名称","alloc":"实际归属金额名称"},"summary_digits":本批汇总位数,"rules":[实际条款ID]}。标签须唯一。每主对象总额=未额外舍入归属金额+所有暂扣/其他去向。确保所有影响金额的有效规则确实进入资金计算。',valid_funding)
                merged['funding']=funding
            parts[phase]=merged;_merge_fragment(aggregate,merged,phase)
            self.state['specs'][phase]=deepcopy(merged)
            self.event(phase,True,{'metrics':list(merged['metrics']),'rule_count':len(phase_rules),'groups_validated':True})
        saved={'inventory':inventory,'clause_proposals':proposals,'parts':parts}
        p,basis,obligations,ds,fragment=replay_basis(cat,saved,period)
        self.state['adaptive_basis_contract']=saved
        return p,basis,obligations,ds,fragment

    def _compile_phase(self,phase,phase_rules,all_rules,units,datasets,scope,previous):
        # The lifecycle is fixed, the business stages are conditional. An
        # empty obligation set must not trigger an LLM to hallucinate rules
        # from other phases or invent a coefficient/deduction just to fill it.
        if not phase_rules:return _empty_fragment()
        # The outline carries only compact semantic obligations and existing
        # dependency names. Actual formulas and raw parameter cells are fetched
        # separately for each bounded group.
        outline_data={'period':self.period,'phase':phase,'obligations':_compact(phase_rules),'available_metrics':_metric_summary(previous['metrics']),
                      'data_contracts':_schema_summary(datasets),'project_keys':scope['population']['keys']}
        def valid_outline(v):
            contract(v,required=('groups',),label='指标分组',owner=phase)
            if not isinstance(v['groups'],list):raise ProgramError('指标分组须列表',owner=phase)
            declared=set();covered=set();ids=set()
            for group in v['groups']:
                contract(group,required=('id','purpose','rules','metrics'),label='局部指标组',owner=phase)
                gid=group['id']
                if not isinstance(gid,str) or not re.fullmatch(r'[A-Za-z0-9_\-]+',gid) or gid in ids:raise ProgramError('指标组ID无效/重复',owner=phase)
                ids.add(gid);r=names(group['rules'],'指标组条款',phase,nonempty=False)
                if not set(r)<=set(phase_rules):raise ProgramError('指标组引用非本阶段条款',owner=phase)
                covered.update(r)
                metrics=group['metrics']
                if not isinstance(metrics,dict):raise ProgramError('指标组需要名称/用途映射',owner=phase)
                if metrics and not r:raise ProgramError('指标组有实际指标但无本阶段原文规则；不能创造无依据的指标',owner=phase,kind='coverage')
                if declared&set(metrics) or set(metrics)&set(previous['metrics']):raise ProgramError('指标重复声明',owner=phase)
                declared.update(metrics)
            if covered!=set(phase_rules):raise ProgramError(f'指标组漏规则：{sorted(set(phase_rules)-covered)}',owner=phase,kind='coverage')
        outline=self.ask(f'adaptive/{phase}/outline',phase,outline_data,
            '只规划本阶段需要的具名指标组和依赖顺序，不编制公式或复制参数。按先基础后依赖排组，每组只关注相关规则；将同一指标/费率表的规则放同组。'
            '新指标来自本次材料，不固定数量或名称。项目规则组负责基数及基本金额；系数组仅按材料需要编制调节指标；暂扣/总额组实现资金公式及舍入。已有指标可直接引用。'
            '输出{"groups":[{"id":"唯一英文ID","purpose":"用途","rules":[本阶段实际条款ID],"metrics":{"本组新增指标ID":"指标名称/定义"}}]}；条款全部覆盖。费率/政策组可以没有新增指标；必须在后续实际表达式中使用。',valid_outline)
        merged=_empty_fragment()
        for group in outline['groups']:
            gid=group['id'];clauses={r:phase_rules[r] for r in group['rules']}
            available=deepcopy(previous);_merge_fragment(available,merged,phase)
            data={'period':self.period,'group':group,'data_contracts':_schema_summary(datasets),'project_keys':scope['population']['keys'],
                  'available_metrics':_metric_summary(available['metrics']),'available_parameters':list(available['parameters']),'available_schedules':list(available['schedules']),
                  'dependency_digest':fingerprint({'datasets':datasets,'scope':scope,'available':available}),
                  **_source_context(self.catalog,units,clauses)}
            def validate(v,available=available,group=group):
                _validate_fragment(v,phase,period=self.period)
                required=set(group['metrics'])
                if not required<=set(v['metrics']):raise ProgramError(f'指标组缺少规划指标：{sorted(required-set(v["metrics"]))}',owner=phase)
                bind_formula_atoms(v,available['metrics'],scope,all_rules,phase,self.period)
                # Additional named source operands are legitimate when they
                # are used by a planned calculation. Unused new amounts are not.
                used=set(required);todo=list(required)
                while todo:
                    m=todo.pop()
                    for dep in _metric_refs(v['metrics'][m]['expr']) & set(v['metrics']):
                        if dep not in used:used.add(dep);todo.append(dep)
                extras=set(v['metrics'])-required
                if extras-used:raise ProgramError(f'新增辅助指标没有进入实际规划计算：{sorted(extras-used)}',owner=phase,kind='coverage')
                if any(not set(v['metrics'][k]['rules'])&set(group['rules']) for k in extras):raise ProgramError('新增辅助指标缺本组真实原文来源',owner=phase,kind='coverage')
                combined=deepcopy(available);_merge_fragment(combined,v,phase)
                for m in v['metrics'].values():validate_numeric_literals(m['expr'],m['rules'],all_rules,owner=phase)
                probe_program=_compile_prefix(self.catalog,datasets,scope['population'],combined,self.period,owner=phase)
                # The final phase coverage will also include funding links.
                formula_ids={r for r in group['rules'] if all_rules[r].get('formula')}
                if formula_ids:
                    coverage(all_rules,{phase:v},phases=[phase],required_ids=formula_ids,formula_bindings=combined['formula_bindings'],
                             metrics=combined['metrics'],parameters=probe_program.inputs.parameters,keys=scope['population']['keys'])
            reply=self.ask(f'adaptive/{phase}/group/{gid}',phase,data,
                '只编制本指标组。实际公式必须包含原文全部因子、顺序和例外；不要计算最终金额。先定义一次实际来源参数/阶梯，再以具名指标引用。'+EXPRESSION_HELP+
                '\n输出{"metrics":{"规划指标ID":{"label":"实际含义","expr":表达式,"type":"number/text/bool/date","rules":[对应条款ID]}},"parameters":{...},"schedules":{...},"checks":[{"id":"具名检查","expr":布尔表达式,"message":"不通过说明","rules":[条款ID]}],"formula_bindings":{"明示公式条款ID":{"target":"该公式真实指标ID","symbols":{"原文完整符号":{"ref":"指标ID"}或{"param":"参数ID"}或{"key":"项目键"}},"path":[]（可省，确为if分支时写实际args路径）}}}。'
                '每个原文非数字符号都必须绑定，不能只引用算式而漏新因子。直接复用available_metrics中已经由真实唯一源行产生的底层属性；数值字面量不用登记符号。'
                '必须保留所有规划指标；可以增加被实际规划公式引用的、带本组证据的具名源查询辅助指标（如原始合同额、实际费率），并用ref绑定原文符号。不要额外创造未使用的指标。无法实现的业务语义给具体反例，不能改用旧公式。',validate)
            _merge_fragment(merged,reply,phase)
        return merged

    def build_roster(self,basis,obligations,datasets,fragment,*,overrides=()):
        saved=self.state['adaptive_basis_contract'];units=rule_units(self.catalog,saved['inventory']);rules=_clauses_for(obligations,'roster')
        # When upstream direct assignment and a separate employee computation
        # are explicit, the role prompt must not contain the employee payment
        # formulas. They are executed after the immutable contribution relation.
        direct_proof=self.state.get('receipts',{}).get('adaptive/hold_policy/funding',{}).get('confidence')=='deterministic_direct_funding'
        assignment_rules={rid:r for rid,r in rules.items() if r.get('operation')=='assignment'}
        personal_rules={rid:r for rid,r in rules.items() if rid not in assignment_rules}
        separate=bool(direct_proof and assignment_rules and any(r.get('formula') or r.get('operation')=='population' for r in personal_rules.values()))
        attribution_rules=assignment_rules if separate else rules
        profile=_binding_context(self.catalog,saved['inventory'],'roster')
        def valid_map(v):
            contract(v,required=('datasets',),label='人员源绑定',owner='roster')
            data=deepcopy(datasets);_merge_map(data,v['datasets'],'人员数据集','roster');_validate_data_use(saved['inventory'],data,complete=True)
            read_datasets(self.catalog,v['datasets'],period=self.period,owner='roster')
        mapping=self.ask('adaptive/roster/mapping','roster',{'period':self.period,'rules':_compact(rules),'physical_sheets':profile,'already_bound':_schema_summary(datasets),'project_keys':basis['key_fields']},
            '只绑定尚未登记的人员及份额真实来源。总额已经确认，不修改项目规则或人员金额。已有数据集中实际人员主表、参数和身份字段应直接复用，不重复绑定。可按人员唯一ID或明确的姓名口径，支持长表、宽表、多个名单源和各角色不同粒度。'+DATASET_HELP+'\n输出{"datasets":{...}}；已有全部所需数据或制度明确待分配，可返回空 datasets。',valid_map)
        data=deepcopy(datasets);_merge_map(data,mapping['datasets'],'人员数据集','roster')
        # A role list is dynamic. The policy phase needs its own complete
        # evidence, but never the entire project-computation history.
        def valid_roster(v):
            contract(v,required=('roles','rounding','rules','parameters','schedules','formula_bindings'),optional=('distribution_kind','person_program_required'),label='人员归属计划',owner='roster')
            canonical_roster(v,self.period)
            v['formula_bindings']=_typed_formula_bindings(v['formula_bindings'],obligations,'roster')
            _canonical_parameter_sources(v['parameters'],obligations)
            if v.get('distribution_kind','roles') not in ('roles','direct') or type(v.get('person_program_required',False)) is not bool:raise ProgramError('人员归属模式或是否另行按人员核算未明确',owner='roster')
            if separate:
                if v.get('distribution_kind')!='direct' or v.get('person_program_required') is not True:raise ProgramError('实际原文证明单一归属及独立人员计算，必须保留对应阶段',owner='roster',kind='coverage',rules=list(attribution_rules))
                for role in v['roles'].values():
                    rec=role.get('recipients',{});pairs=rec.get('pairs',[])
                    for pair in pairs:
                        if 'name' not in pair and pair.get('id'):pair['name']=pair['id'] # identity-only technical contribution; master supplies final name
                    if len(pairs)==1 and not pairs[0].get('share') and rec.get('missing')=='error' and role.get('rate')==1:
                        rec['missing_share']='single';rec['default_share']=1
                coverage(obligations,{'roster':v},phases=['roster'],required_ids=set(attribution_rules),formula_bindings=v['formula_bindings'],roles=v['roles'],metrics=fragment['metrics'],parameters=fragment['parameters'],keys=basis['key_fields'])
            replay_settlement(self.catalog,saved,{**v,'datasets':mapping['datasets']},self.period,basis=basis,overrides=overrides,attribution_only=v.get('person_program_required',False))
        role_context=_source_context(self.catalog,units,attribution_rules)
        if len(attribution_rules)>20 or len(str(role_context))>16000:
            roster=self._compile_role_groups(basis,attribution_rules,units,data,fragment,valid_roster)
        else:roster=self.ask('roster','roster',{'period':self.period,'project_keys':basis['key_fields'],'metrics':basis['metric_fields'],'data_contracts':_schema_summary(data),
                'existing_parameters':list(fragment['parameters']), 'existing_schedules':list(fragment['schedules']),
                'stage_contract':{'separate_direct_person_calculation':separate,'remaining_person_rules':len(personal_rules) if separate else None,'total_confirmed':True},**role_context},
            '总额冻结，按本次制度动态声明所有分配角色（不限名称和数目），不得重算项目。每个项目角色比例合计必须为100%。若规则明确每单仅归属一个员工而非奖金池权重分配，采用distribution_kind=direct，仅一个直接归属角色，rate=1、单一实际员工100%，禁止其他分拆。按已有项目键/指标关联实际名单。若汇总到员工后还需计奖系数、预发抵扣、个人发放等，person_program_required=true；本节点只做上游实际归属，后续另编人员层指标，不能把归属金额直接当作最终实发。'+EXPRESSION_HELP+
            '\n输出{"roles":{"角色ID":{"label":"角色名称","rate":真实角色系数表达式,"rules":[条款ID],"recipients":{"kind":"rows/columns","dataset":"实际名单数据集","match":{"名单字段":{"key":"项目键"}},"where":可选行条件,"pairs":[{"name":"姓名字段","id":"可选员工唯一ID字段","share":"可选个人比例字段"}],"text_share":"可选姓名加百分比优先文字字段","missing_share":"single/equal/remaining_equal/error","default_share":仅single必填的比例表达式,"missing":"pending/error","duplicates":"error/identical/sum","pending_label":"仅missing=pending时必填，实际待分配名称","rules":[条款ID]}}},"rounding":{"when":"after_sum/per_record/none","digits":整数,"rules":[条款ID]},"rules":[全局分配规则ID],"parameters":{...},"schedules":{...},"formula_bindings":{...}}。'
            '额外字段：distribution_kind="roles/direct"、person_program_required=true/false。归属阶段无独立舍入规则时rounding.when="none"。确无实名名单的角色可用 recipients={"kind":"pending","label":"实际待分配类别","rules":[条款ID]}。每个角色都要明确缺值和剩余份额，不能默认按旧名单。明示项目金额×角色率×个人比例的公式可绑定target="settlement_contribution"，symbols分别引用@alloc/@role_rate/@share。'
            '若stage_contract.separate_direct_person_calculation=true，只编制当前实际单一归属，distribution_kind=direct、person_program_required=true，禁止把后续人员公式或全员汇总写进本节点。若归属源只有员工唯一编号，可将真实编号字段同时用作当前贡献记录的name/id，后续人员主表提供真实姓名。每个主对象须按实际键精准匹配其本人，不把全表当名单。无明示归属算式时formula_bindings={}；不要把已确定的后续步骤当业务问题。',valid_roster,**({'archive_epoch':'direct-attribution-stage-v2'} if separate else {}))
        full={**roster,'datasets':mapping['datasets']}
        if full.get('person_program_required'):
            _,_,attribution,_=replay_settlement(self.catalog,saved,full,self.period,basis=basis,overrides=overrides,attribution_only=True)
            def person_contract(v):
                return canonical_person_candidate(v,data,obligations,self.period)
            def merge_person(v):
                person_contract(v);candidate=deepcopy(full)
                for k in ('parameters','schedules','formula_bindings'):_merge_map(candidate[k],v[k],f'人员层{k}','roster')
                candidate['person_plan']={k:deepcopy(v[k]) for k in ('program','identity_key','name_metric','attributed_metric','contribution_amount','rules')}
                return candidate
            def valid_person(v):replay_settlement(self.catalog,saved,merge_person(v),self.period,basis=basis,overrides=overrides)
            reply=self.ask('adaptive/roster/person_plan','roster',{'period':self.period,'rules':personal_rules if separate else rules,'data_contracts':_schema_summary(data),
                'approved_contribution_relation':{'dataset':CONTRIBUTION_DATASET,'fields':CONTRIBUTION_FIELDS,'row_count':len(attribution['rows'])},
                'base_funding':basis['funding'],'existing_parameters':list(fragment['parameters'])+list(full['parameters'])},
                '独立编制人员层。使用实际人员主表建立人员全集，包括无项目人员（若制度要求），按照真实人员唯一编号关联程序生成的approved_contributions明细。它的raw为已确认项目实际归属原值，rounded_record仅在实际归属逐条舍入时是舍入值，否则等于raw；不要读取期望结果。'+EXPRESSION_HELP+
                '\n输出{"program":{"population":{"dataset":"真实人员主数据集","keys":[人员唯一编号字段],"where":真实筛选,"rules":[条款ID]},"metrics":{"指标ID":{"label":"名称","expr":表达式,"type":"number/text/bool/date","rules":[条款ID]}},"checks":[...],"funding":{"total":"实际人员应得指标","payable":"未最终舍入实发指标","alloc":"实际最终实发指标","destinations":{"从人员应得扣除的实际金额指标":"类别"},"labels":{"total":"应得总额","alloc":"本次实发"},"summary_digits":整数,"rules":[条款ID]}},"identity_key":"人员键","name_metric":"姓名指标","attributed_metric":"实际汇总上游归属金额指标","contribution_amount":"raw/rounded_record","rules":[条款ID],"parameters":{},"schedules":{},"formula_bindings":{"公式规则ID":{"target":"person:该实际人员指标ID","symbols":{原文符号:实际人员指标引用}}}}。'
                '按本次规则完成汇总、个人系数、扣减及发放；原归属金额与人员应得差异由程序单独对账。为原文每个非数字符号建立实际具名指标；不可直接引用未绑定的上一层字段。若预发超出本次应得，先建立实际可抵扣金额和超预发指标，不将超发登记当回收款。资金去向必须闭合。',valid_person)
            full=merge_person(reply)
        self.state['adaptive_roster_contract']=deepcopy(full)
        p,b,s,_=replay_settlement(self.catalog,saved,full,self.period,basis=basis,overrides=overrides)
        return p,b,s

    def _compile_role_groups(self,basis,rules,units,datasets,fragment,validate_full):
        """Large role policies use bounded role-specific requests.

        The threshold is a context budget, not a limit on business roles.
        Shared policy stays explicit and the final validator evaluates the
        complete role set once, including its actual 100% coverage.
        """
        cat=self.catalog;saved=self.state['adaptive_basis_contract']
        def validate_outline(v):
            contract(v,required=('groups','global_rules','distribution_kind','person_program_required'),label='角色组计划',owner='roster')
            if v['distribution_kind'] not in ('roles','direct') or type(v['person_program_required']) is not bool or not isinstance(v['groups'],list) or not v['groups']:
                raise ProgramError('角色组计划不完整',owner='roster')
            ids=set();roles=set();covered=set(names(v['global_rules'],'全局/人员层规则','roster',nonempty=False))
            for g in v['groups']:
                contract(g,required=('id','roles','rules','purpose'),label='角色组',owner='roster')
                if not isinstance(g['id'],str) or not re.fullmatch(r'[A-Za-z0-9_\-]+',g['id']) or g['id'] in ids or not isinstance(g['roles'],dict) or not g['roles'] or set(g['roles'])&roles:
                    raise ProgramError('角色组ID/角色声明不唯一',owner='roster')
                ids.add(g['id']);roles.update(g['roles']);covered.update(names(g['rules'],'角色组规则','roster',nonempty=False))
            if covered!=set(rules):raise ProgramError(f'角色组遗漏或虚构条款 {sorted(set(rules)^covered)}',owner='roster',kind='coverage')
            if v['distribution_kind']=='direct' and len(roles)!=1:raise ProgramError('直接归属只允许一个归属角色',owner='roster')
        outline=self.ask('adaptive/roster/outline','roster',{'period':self.period,'obligations':_compact(rules),'data_contracts':_schema_summary(datasets),'project_keys':basis['key_fields']},
            '只将当前制度的实际角色规则分成小组，避免一个请求编制所有角色。按真实角色和相关费率表归组；共享名单缺值/人员汇总舍入/人员层计奖规则放global_rules。'
            '输出{"groups":[{"id":"英文唯一ID","roles":{"真实角色ID":"角色名"},"rules":[相关规则ID],"purpose":"角色用途"}],"global_rules":[共享及人员层条款ID],"distribution_kind":"direct/roles","person_program_required":true或false}。不得漏条款。',validate_outline)
        combined={'roles':{},'parameters':{},'schedules':{},'formula_bindings':{}}
        for group in outline['groups']:
            group_rules={k:rules[k] for k in group['rules']}
            shared={k:rules[k] for k in outline['global_rules'] if rules[k]['kind']!='formula'}
            def validate(v,group=group):
                contract(v,required=('roles','parameters','schedules','formula_bindings'),label='局部角色合同',owner='roster')
                canonical_roster(v,self.period)
                if set(v['roles'])!=set(group['roles']):raise ProgramError('未按局部角色组的全部角色编制',owner='roster')
                params=deepcopy(fragment['parameters']);schedules=deepcopy(fragment['schedules']);roles=deepcopy(combined['roles'])
                for k,old in [('parameters',params),('schedules',schedules),('roles',roles)]:
                    _merge_map(old,combined[k] if k!='roles' else {},k,'roster');_merge_map(old,v[k],k,'roster')
                program,_,_,_,_=replay_basis(cat,saved,self.period)
                program.inputs=compile_inputs(cat,datasets,params,schedules,period=self.period,owner='roster')
                program.validate_roster({'roles':roles,'rounding':{'when':'none','digits':2,'rules':[]},'rules':[]})
                # Validate actual identities and shares before moving to a
                # different role. Do not demand the partial role group total
                # equal 100% until all groups have been compiled.
                for context,_ in program._contexts.values():
                    for role in v['roles'].values():
                        rate=program._num(program.eval(role['rate'],context))
                        if not 0<=rate<=1:raise ProgramError('本角色比例超出0至1',owner='roster')
                        program._recipients(role['recipients'],context)
            v=self.ask('adaptive/roster/role_group/'+group['id'],'roster',{'period':self.period,'group':group,
                        'project_keys':basis['key_fields'],'metric_ids':basis['metric_fields'],'data_contracts':_schema_summary(datasets),
                        'shared_rules':_compact(shared),'existing_parameters':list(fragment['parameters'])+list(combined['parameters']),
                        **_source_context(cat,units,group_rules)},
                '只编制本组真实角色，不修改其它角色、人员汇总或项目金额。'+EXPRESSION_HELP+
                '\n输出{"roles":{"组内角色ID":{"label":"角色名称","rate":比例表达式,"rules":[条款ID],"recipients":{"kind":"rows/columns","dataset":"已绑定名单","match":{"名单字段":项目键/指标表达式},"pairs":[{"name":"姓名字段","id":"可选ID","share":"可选份额字段"}],"where":可选筛选,"text_share":"可选优先文字比例列","missing_share":"single/equal/remaining_equal/error","default_share":仅single必填,"missing":"pending/error","duplicates":"error/identical/sum","pending_label":"待分配名称","rules":[条款ID]}}},"parameters":{},"schedules":{},"formula_bindings":{}}。无实名角色使用 recipients={"kind":"pending","label":"实际待分配类别","rules":[条款ID]}。',validate)
            for k in combined:_merge_map(combined[k],v[k],k,'roster')
        def assembled(v):
            contract(v,required=('rounding','rules','formula_bindings'),label='角色全局合同',owner='roster')
            result=deepcopy(combined);_merge_map(result['formula_bindings'],v['formula_bindings'],'公式绑定','roster')
            result.update(rounding=v['rounding'],rules=v['rules'],distribution_kind=outline['distribution_kind'],person_program_required=outline['person_program_required'])
            return result
        final=self.ask('roster','roster',{'period':self.period,'roles':{k:v['label'] for k,v in combined['roles'].items()},
            'global_rules':{k:rules[k] for k in outline['global_rules']},'distribution_kind':outline['distribution_kind'],'person_program_required':outline['person_program_required']},
            '所有角色组已逐组核验。本节点只确定归属/人员汇总舍入时点和全局资金规则；需要后续人员计奖层的规则保留给下一节点。输出{"rounding":{"when":"after_sum/per_record/none","digits":整数,"rules":[条款ID]},"rules":[全局条款ID],"formula_bindings":{全局明示公式绑定}}。不能重写角色。',lambda v:validate_full(assembled(v)))
        return assembled(final)

    def basis_review(self,basis):
        saved=self.state['adaptive_basis_contract'];parts=saved['parts']
        # All executed expressions/coverage are already audited per group;
        # the final reviewer receives contract composition and actual funding
        # examples, not an accumulation of model reasoning history.
        projects=list(basis['projects'].values());examples=projects[:3]+projects[-3:]
        fields=('key','key_values','metrics','total','payable','alloc','destinations')
        metrics={}
        for part in parts.values():metrics.update(part.get('metrics',{}))
        f=basis['funding'];todo=[f[k] for k in ('total','payable','alloc')]+list(f['destinations']);needed=set()
        while todo:
            mid=todo.pop()
            if mid in needed or mid not in metrics:continue
            needed.add(mid);todo.extend(_metric_refs(metrics[mid]['expr']))
        handoff=[{'quote':c['quote'],'meaning':c['meaning']} for u in saved['clause_proposals'].values() for c in u.get('clauses',[]) if c.get('phase')=='roster' and c.get('operation')=='assignment']
        return {'funding':basis['funding'],'checks':basis['checks'],'rule_coverage':{k:{'quote':v['clause']['quote'],'components':v['components']} for k,v in basis['rule_coverage'].items()},
                'funding_expressions':{k:metrics[k] for k in metrics if k in needed},'actual_handoff_rules':handoff,
                'source_counts':{k:{'included':v['included'],'grain':v['grain'],'relationships':v.get('relationships',[])} for k,v in basis['data_bindings'].items()},
                'confidence_priority':sorted(basis.get('indicator_confidence',{}).items(),key=lambda x:(x[1]['min_score'],x[0])),
                'project_examples':[{k:v[k] for k in fields} for v in examples]},(
            '独立核对规则完整性、数据粒度、所有资金去向和实际对象。程序已逐项目以两种数值路径计算。不要根据手算总数或期望答案重选规则。若有错误给责任节点、条款ID、指标ID、对象及具体原文反例。'
            '优先核查confidence_priority中的低证据分指标、实际缺失或同值记录；低分本身不是算错，必须用实际规则证据裁决。'
            '输出{"status":"PASS","reason":"真实核验点"}或{"status":"REVISE","owner":"scope_mapping/reference_mapping/project_policy/coefficient_policy/hold_policy","reason":"实际证据"}或questions。')

    def settlement_review(self,basis,settlement):
        by_project=defaultdict(list)
        for row in settlement['rows']:by_project[row['project']].append(row)
        chosen={}
        for k,rows in by_project.items():
            p=basis['projects'][k];feature=(p['alloc']<0,tuple(sorted((r['role_id'],r['destination']) for r in rows)),any(r['share']!=1 for r in rows))
            chosen.setdefault(feature,k)
        # Give the reviewer a genuinely complete small batch. For large
        # batches explicitly distinguish representative complete groups from
        # the whole population, which the deterministic engine checks in full.
        complete=len(by_project)<=24 and len(settlement['rows'])<=200
        selected=list(by_project) if complete else list(chosen.values())[:24]
        identity_rules={rid:{'quote':v['clause']['quote'],'source':v['clause']['source']} for rid,v in basis.get('rule_coverage',{}).items()
                        if re.search(r'姓名|重名|人员编号|员工编号|唯一身份|唯一编号|身份键|同名|关联键',v['clause']['quote'])}
        named=[r for r in settlement['people'] if r['destination']=='person'];same_names=defaultdict(list)
        for row in named:same_names[row['name']].append(row['id'])
        duplicate_names={name:ids for name,ids in same_names.items() if len(ids)>1}
        payload={'funding':basis['funding'],'rounding':settlement['rounding'],'coverage':settlement['rule_coverage'],'checks':settlement['checks'],
                 'complete_projects':[{'key':k,'alloc':basis['projects'][k]['alloc'],'rows':by_project[k]} for k in selected],
                 'review_scope':{'full_project_population_in_payload':complete,'total_projects':len(by_project),'included_projects':len(selected),'total_role_rows':len(settlement['rows']),
                                 'included_role_rows':sum(len(by_project[k]) for k in selected),'all_objects_program_checked':True,
                                 'note':'此处每个项目的行完整。若full_project_population_in_payload=false，仅提供代表性分组，不能用样本和解释全体总额。'},
                 'identity_source_rules':identity_rules,'identity_checks':{'named_people':len(named),'unique_ids':len({r['id'] for r in named}),'same_display_name_distinct_ids':duplicate_names,
                    'identity_key':(settlement.get('person_plan') or {}).get('identity_key'),'person_master':(settlement.get('person_plan') or {}).get('program',{}).get('population',{}).get('dataset')}}
        if personal:=settlement.get('person_basis'):
            examples={}
            for row in settlement['people']:
                if row['destination']!='person':continue
                feature=(row['attributed']==0,row['entitled']!=row['attributed'],row['amount']==0,row['amount']!=row['entitled'],tuple(sorted(k for k,v in row['destinations'].items() if v)))
                examples.setdefault(feature,row)
            payload.update(personal_funding=personal['funding'],personal_examples=named if len(named)<=40 else list(examples.values()),funding_bridge=settlement['funding_bridge'])
            payload['review_scope'].update(full_person_population_in_payload=len(named)<=40,total_people=len(named),included_people=len(payload['personal_examples']))
        return payload,(
            '独立核对实际角色比例、名单唯一身份、所有分配去向及舍入。先看review_scope，若全体标志为false，complete_projects是有明确采样范围的完整项目例子，不可把样本之和与全体总额比较；程序已逐对象精确复核。若全体标志为true，逐项核对全部项目。'
            '必须按identity_source_rules实际原文和identity_checks的人员身份字段核对；若原文允许姓名重名，则同名但不同唯一编号的人员不是冲突，不能合并。每项目只归一人是按该项目的实际身份数判断，不是要求不同员工的姓名不同。'
            '若有独立人员核算层，基础归属不是应得或最终实发；核对人员全集、汇总、计奖调整、实际抵扣及全过程资金去向。'
            '输出{"status":"PASS","reason":"具体来源和核验点"}或{"status":"REVISE","owner":"roster","reason":"实际错误","counterexample":{"rule_id":"实际提供的条款ID","quote":"实际连续原文","object":"有错的实际项目或人员编号","target":"确有错误的指标/字段","actual":"当前事实","expected":"按原文正确事实"}}或questions。')

    def build_report(self,basis,settlement,obligations):
        from bonus_agent.adaptive_export import default_layout,validate_layout,verify_explicit_output_fields
        rules=_clauses_for(obligations,'export');saved=self.state['adaptive_basis_contract']
        if not rules:
            layout=default_layout();self.state['adaptive_output_contract']=layout;self.state['adaptive_output_coverage']={};return layout
        fragment=_empty_fragment()
        fragment['metrics'].update(saved['parts'].get('source_attributes',{}).get('metrics',{}))
        for phase in METRIC_PHASES:_merge_fragment(fragment,saved['parts'][phase],phase)
        personal=settlement.get('person_basis') or {};person_plan=settlement.get('person_plan') or {};pm=person_plan.get('program',{}).get('metrics',{});pk=personal.get('key_fields',[])
        parameter_ids={**fragment['parameters'],**self.state['adaptive_roster_contract'].get('parameters',{})}
        def validate(v):
            validate_layout(v,basis,settlement)
            verify_explicit_output_fields(v,basis,settlement,obligations)
            coverage(obligations,{'export':v},phases=['export'],formula_bindings=v['formula_bindings'],metrics=fragment['metrics'],
                     parameters=parameter_ids,keys=basis['key_fields'],person_metrics=pm,person_keys=pk)
        layout=self.ask('adaptive/export/layout','export',{'rules':rules,'project_keys':basis['key_fields'],'available_metrics':basis['metric_fields'],
                'funding':basis['funding'],'roles':{k:v['label'] for k,v in self.state['adaptive_roster_contract']['roles'].items()},
                'person_keys':pk,'person_metrics':personal.get('metric_fields',{}),'person_funding':personal.get('funding')},
            '仅根据输出要求安排已经计算的表格，不改变规则或金额。全部项目键、所有已编制指标、实际角色明细及实名/待分配汇总都会保留。'
            '为本次要求的列名称、顺序和分组指定实际来源；不要新造金额列。输出{"columns":[{"label":"报表字段名","source":{"ref":"真实指标ID"}或{"key":"真实项目键"},"rules":[对应输出条款ID]}],"sections":{"project/summary/roles/parameters/trace/coverage/approvals":{"rules":[对应输出条款ID]}},"groups":[{"label":"分组名","layer":"project或person","by":[{"key":"本层实际键"}或{"ref":"本层实际指标ID"}],"metrics":[本层汇总数值指标ID],"rules":[条款ID]}],"rules":[全局输出条款ID],"formula_bindings":{明示输出公式条款ID:{"target":"相同的已计算指标ID","symbols":{...}}}}。分组的键和指标必须来自同一实际计算层，不跨层混用。'
            '按要求增加project_totals:[{"metric":"实际项目金额指标ID","label":"汇总名称","rules":[条款ID]}]。如有独立人员层，可增加person_columns（同columns格式但引用实际person_keys/person_metrics）、person_totals（同project_totals格式但引用实际人员指标）、sections.person。需要汇总各金额列时，涵盖两层实际金额列，不把费率、比例、系数或编号当成金额加总。人员层公式目标用person:指标ID。明确输出要求无法由现有指标满足时，给出具体缺失字段及应返回补充的责任节点。',validate)
        self.state['adaptive_output_contract']=layout
        self.state['adaptive_output_coverage']=coverage(obligations,{'export':layout},phases=['export'],formula_bindings=layout['formula_bindings'],
             metrics=fragment['metrics'],parameters=parameter_ids,keys=basis['key_fields'],person_metrics=pm,person_keys=pk)
        return layout
