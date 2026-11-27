"""Dynamic financial workpapers from a frozen adaptive program.

Columns/role slots are derived from the actual contract. Scalar arithmetic,
source-backed queries and tier calculations are expressed as Excel formulas
where Excel has an equivalent operation; other values retain their exact
program expression and provenance (never a fake self-referencing formula).
Every financial row and each named/pending identity is read back before publish.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date,datetime
from decimal import Decimal as D
import json
import re
from pathlib import Path
from tempfile import NamedTemporaryFile

import xlsxwriter
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.datetime import to_excel

from bonus_agent.recovery import fingerprint
from bonus_core.data.period import Period
from bonus_core.engine.adaptive import ProgramError,contract,names


FORMAT='adaptive-commission-workpaper-v1'
SECTIONS={'project','summary','roles','person','parameters','trace','coverage','approvals'}


def validate_layout(layout,basis,settlement=None):
    contract(layout,required=('columns','sections','rules','formula_bindings'),optional=('groups','person_columns','person_totals'),label='输出布局',owner='export')
    keys=set(basis['key_fields']);metrics=set(basis['metric_fields']);labels=set()
    if not isinstance(layout['columns'],list) or not isinstance(layout['sections'],dict):raise ProgramError('报表字段/章节定义无效',owner='export')
    for col in layout['columns']:
        contract(col,required=('label','source','rules'),label='报表列',owner='export')
        src=col['source']
        if not isinstance(src,dict) or len(src)!=1:raise ProgramError('报表列必须指向实际指标或项目键',owner='export')
        k,v=next(iter(src.items()))
        if k not in ('key','ref') or v not in (keys if k=='key' else metrics):raise ProgramError(f'输出列 {col["label"]} 引用了未计算字段 {src}',owner='export')
        if col['label'] in labels:raise ProgramError(f'报表列名称重复：{col["label"]}',owner='export')
        labels.add(col['label'])
    for name,section in layout['sections'].items():
        if name not in SECTIONS:raise ProgramError(f'报表章节 {name!r} 未支持，不能仅挂引用',owner='export',kind='unsupported')
        if name=='person' and not (settlement or {}).get('person_basis'):raise ProgramError('实际没有独立人员层指标，不能虚构人员核算章节',owner='export')
        contract(section,required=('rules',),label='报表章节',owner='export')
    for group in layout.get('groups',[]):
        contract(group,required=('label','by','metrics','rules'),label='分组汇总',owner='export')
        if not group['label'] or not isinstance(group['by'],list) or not group['by'] or not isinstance(group['metrics'],list) or not group['metrics']:
            raise ProgramError('分组汇总字段无效',owner='export')
        for src in group['by']:
            if not isinstance(src,dict) or len(src)!=1:raise ProgramError('汇总分组应为实际键或指标',owner='export')
            k,v=next(iter(src.items()))
            if k not in ('key','ref') or v not in (keys if k=='key' else metrics):raise ProgramError('分组汇总引用不存在的列',owner='export')
        if not set(group['metrics'])<=metrics:raise ProgramError('汇总计算引用不存在的指标',owner='export')
    pb=(settlement or {}).get('person_basis') or {}
    for col in layout.get('person_columns',[]):
        contract(col,required=('label','source','rules'),label='人员层报表列',owner='export')
        src=col['source']
        if not isinstance(src,dict) or len(src)!=1:raise ProgramError('人员列必须对应已核算人员指标',owner='export')
        k,v=next(iter(src.items()))
        if k not in ('key','ref') or v not in (pb.get('key_fields',[]) if k=='key' else pb.get('metric_fields',{})):raise ProgramError(f'人员输出列未计算 {src}',owner='export')
    for item in layout.get('person_totals',[]):
        contract(item,required=('metric','label','rules'),label='人员汇总指标',owner='export')
        if item['metric'] not in pb.get('metric_fields',{}):raise ProgramError('人员汇总指标未计算',owner='export')
    return layout


def default_layout():return {'columns':[],'sections':{},'rules':[],'formula_bindings':{},'groups':[]}


def verify_explicit_output_fields(layout,basis,settlement,obligations):
    """Recognizable enumerated report columns must actually exist.

    The output rule is not satisfied merely by citing it on a section. For
    prose without an explicit delimited field list, the independent layout
    reviewer still checks its stated requirement.
    """
    project=set(basis['key_fields'])|set(basis['metric_fields'].values())|{c['label'] for c in layout.get('columns',[])}|{'项目编号'}
    personal=(settlement or {}).get('person_basis') or {}
    people=set(personal.get('key_fields',[]))|set(personal.get('metric_fields',{}).values())|{c['label'] for c in layout.get('person_columns',[])}|{'姓名','人员ID','人员编号','金额','待分配','去向'}
    if settlement:
        groups=defaultdict(int)
        for r in settlement['rows']:groups[(r['project'],r['role'])]+=1
        counts=defaultdict(int)
        for (_,role),n in groups.items():counts[role]=max(counts[role],n)
        for role,n in counts.items():
            for i in range(1,n+1):
                label=role+(str(i) if n>1 else '');project.update((label,label+'金额'))
    norm=lambda s:re.sub(r'\s+','',str(s))
    p_names={norm(s) for s in project};e_names={norm(s) for s in people}
    for rid,clause in obligations.items():
        if clause['phase']!='export':continue
        for match in re.finditer(r'(?:输出|列示|列出)([^。；;：:\n]{0,30})[：:]([^。；;\n]+)',clause['quote']):
            desc,raw=match.groups();raw=re.split(r'[,，]\s*(?:并|同时|以及汇总|汇总)',raw,maxsplit=1)[0]
            labels=[x.strip() for x in re.split(r'[、,，]',raw) if x.strip()]
            # A sentence rather than an enumerated field list remains under
            # semantic review instead of fabricating column names from prose.
            if len(labels)<2 or any(len(x)>45 or re.search(r'需要|应当|按照|根据|如果|必须',x) for x in labels):continue
            names=e_names if re.search(r'人员|员工|个人|收款人',desc) else p_names
            missing=[x for x in labels if norm(x) not in names]
            if missing:raise ProgramError(f'{rid} 输出要求中有明确列但当前布局未提供：{missing}；请绑定对应已核算键/指标，确实缺指标时返回其计算节点',owner='export',kind='coverage',rules=[rid],details={'missing_columns':missing,'available':sorted(names)})


def _json(x):return json.dumps(x,ensure_ascii=False,default=str,separators=(',',':'))


def _ref(sheet,row,col):return "'"+sheet+"'!"+get_column_letter(col+1)+str(row+1)


def _range(sheet,start,end,col):return "'"+sheet+"'!"+get_column_letter(col+1)+str(start+1)+':'+get_column_letter(col+1)+str(end+1)


class _NotExcel(ValueError):pass


def literal(x):
    if x is None:return '""'
    if isinstance(x,bool):return 'TRUE' if x else 'FALSE'
    if isinstance(x,(int,float,D)):return str(x)
    if isinstance(x,date):return f'DATE({x.year},{x.month},{x.day})'
    return '"'+str(x).replace('"','""')+'"'


def excel_expr(expr,refs,params,schedules,queries,period,fields=None):
    """Translate the supported scalar subset, preserving typed references."""
    def go(x):return excel_expr(x,refs,params,schedules,queries,period,fields)
    if not isinstance(expr,dict):return literal(expr)
    if 'op' in expr:
        op=expr['op'];a=[go(x) for x in expr['args']]
        binary={'add':'+','sub':'-','mul':'*','div':'/','eq':'=','ne':'<>','lt':'<','le':'<=','gt':'>','ge':'>='}
        if op in binary:return '('+binary[op].join(a)+')'
        if op=='neg':return '(-'+a[0]+')'
        if op in ('abs','min','max','round','if','and','or','not','year','month'):return op.upper()+'('+','.join(a)+')'
        if op=='coalesce':
            out=a[-1]
            for x in reversed(a[:-1]):out=f'IF({x}="",{out},{x})'
            return out
        if op=='is_missing':return f'({a[0]}="")'
        if op=='in':return 'OR('+','.join(f'EXACT({a[0]},{x})' for x in a[1:])+')'
        if op=='months_between':return f'((YEAR({a[1]})-YEAR({a[0]}))*12+MONTH({a[1]})-MONTH({a[0]}))'
        if op=='days_between':return f'({a[1]}-{a[0]})'
        if op=='quarter':return f'ROUNDUP(MONTH({a[0]})/3,0)'
        if op=='concat':return '('+'&'.join(a)+')'
        raise _NotExcel(f'Excel没有与 {op} 完全一致的运算')
    kind,x=next(iter(expr.items()))
    if kind in ('ref','key'):
        return refs[(kind,x)]
    if kind=='param':return params[x]
    if kind=='field':
        if fields is None or x not in fields:raise _NotExcel(f'字段 {x} 无Excel来源')
        return fields[x]
    if kind=='num':return str(D(str(x)))
    if kind=='period':
        p=Period.parse(period)
        return literal({'month':p.end.strftime('%Y%m'),'start':p.start,'end':p.end,'year_start':date(p.end.year,1,1),'month_number':p.end.month,'year':p.end.year}[x])
    if kind=='query':
        key=fingerprint(x)
        if key not in queries:raise _NotExcel('当前条件未执行此查询，不能假造未选分支的Excel来源')
        return queries[key]
    if kind=='tier':
        v=go(x['value']);schedule=schedules[x['schedule']];bands=schedule['bands'];method=schedule['method']
        rate=bands[0][1]
        for low,r,inc in bands[1:]:rate=f'IF({v}{">=" if inc else ">"}{low},{r},{rate})'
        if x['output']=='rate':
            if schedule['below']=='zero':rate=f'IF({v}<{bands[0][0]},0,{rate})'
            return rate
        if method=='whole':out=f'({v}*{rate})'
        else:
            parts=[]
            for i,(low,r,_) in enumerate(bands):
                top=f'MIN({v},{bands[i+1][0]})' if i+1<len(bands) else v
                parts.append(f'MAX(0,{top}-{low})*{r}')
            out='SUM('+','.join(parts)+')'
        if schedule['below']=='zero':out=f'IF(AND({v}>=0,{v}<{bands[0][0]}),0,{out})'
        neg=schedule['negative']
        if neg in ('zero','signed'):out=f'IF({v}<0,'+('0' if neg=='zero' else f'{v}*{bands[0][1]}')+','+out+')'
        return out
    raise _NotExcel(f'不能生成 {kind} 的Excel表达式')


def _equal(a,b):
    if b is None:return a in (None,'')
    if isinstance(b,D):
        if a is None or isinstance(a,bool):return False
        try:return abs(D(str(a))-b)<=D('0.000000000001')*max(D(1),abs(b))
        except Exception:return False
    if isinstance(b,date):return a.date()==b if isinstance(a,datetime) else a==b
    return a==b


def _money_equal(a,b,digits):
    # A relative tolerance alone would allow cent-level errors for a large
    # project. The published currency precision must always agree exactly.
    from decimal import ROUND_HALF_UP
    if not _equal(a,b):return False
    try:
        u=D(1).scaleb(-digits)
        return D(str(a)).quantize(u,rounding=ROUND_HALF_UP)==D(str(b)).quantize(u,rounding=ROUND_HALF_UP)
    except Exception:return False


def write_adaptive_workbook(path,basis,settlement,state,*,layout=None):
    layout=validate_layout(layout or default_layout(),basis,settlement)
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with NamedTemporaryFile(dir=path.parent,suffix='.xlsx',delete=False) as temp:target=Path(temp.name)
    projects=basis['projects'];order=list(projects);metrics={}
    contract_state=state['adaptive_basis_contract']
    for part in contract_state['parts'].values():metrics.update(part.get('metrics',{}))
    funding=basis['funding'];digits=funding['summary_digits']
    # A requested layout can rename/reorder columns, not omit actual fields.
    cols=[];seen=set()
    for col in layout['columns']:
        kind,name=next(iter(col['source'].items()));cols.append((kind,name,col['label']));seen.add((kind,name))
    cols.extend(('key',k,k) for k in basis['key_fields'] if ('key',k) not in seen)
    cols.extend(('ref',k,label) for k,label in basis['metric_fields'].items() if ('ref',k) not in seen)
    canonical={}
    for i,(kind,k,label) in enumerate(cols,1):canonical.setdefault((kind,k),i)
    money_ids={funding[k] for k in ('total','payable','alloc')}|set(funding['destinations'])
    role_groups=defaultdict(list);slots=[]
    if settlement:
        for row in settlement['rows']:role_groups[(row['project'],row['role_id'])].append(row)
        roles=state['adaptive_roster_contract']['roles']
        for rid,r in roles.items():
            count=max((len(role_groups[(k,rid)]) for k in order),default=0)
            slots.extend((rid,i,r['label']+(str(i+1) if count>1 else '')) for i in range(count))
    if 1+len(cols)+2*len(slots)>16384:raise ProgramError('报表列数超出Excel容量，不截断数据',owner='export')
    formula_expected=[];project_rows={};person_rows=[];fallbacks=[];summary_checks={};inline_checks={}
    try:
        with xlsxwriter.Workbook(str(target),{'strings_to_formulas':False,'strings_to_urls':False}) as book:
            work=book.add_worksheet('提成奖核算底稿');summary=book.add_worksheet('核算汇总');parameters=book.add_worksheet('参数与来源')
            sources=book.add_worksheet('数据追溯');queries=book.add_worksheet('计算查询');ledger=book.add_worksheet('规则覆盖');receipts=book.add_worksheet('节点验收');versions=book.add_worksheet('版本与审批')
            role_sheet=book.add_worksheet('角色分配') if settlement else None
            personal=(settlement or {}).get('person_basis');person_sheet=book.add_worksheet('人员核算底稿') if personal else None
            grouped=book.add_worksheet('分组汇总') if layout.get('groups') else None
            hfmt=book.add_format({'bold':True,'bg_color':'#214963','font_color':'white','text_wrap':True});moneyfmt=book.add_format({'num_format':'#,##0.'+'0'*max(digits,2)})
            datefmt=book.add_format({'num_format':'yyyy-mm-dd'});prec=book.add_format({'num_format':'0.##########'})
            def head(ws,labels):
                ws.write_row(0,0,labels,hfmt);ws.freeze_panes(1,1);ws.hide_gridlines(2);ws.set_column(0,len(labels)-1,20);ws.set_column(0,0,42)
            def value(ws,r,c,v,fmt=None):
                if v is None:ws.write_blank(r,c,None,fmt)
                elif isinstance(v,bool):ws.write_boolean(r,c,v,fmt)
                elif isinstance(v,(int,float,D)):ws.write_number(r,c,float(v),fmt or prec)
                elif isinstance(v,date):ws.write_datetime(r,c,v,datefmt)
                else:ws.write_string(r,c,str(v)[:32760],fmt)
            def formula(ws,r,c,expr,v,fmt=None):
                if len(expr)>8190:raise _NotExcel('公式超过Excel长度限制')
                if isinstance(v,D):cache=float(v)
                elif isinstance(v,date):cache=to_excel(v);fmt=datefmt
                elif v is None:cache=''
                else:cache=v
                ws.write_formula(r,c,'='+expr,fmt,cache);formula_expected.append((ws.name,r,c))
            def metric_value(ws,r,c,expr,raw,loc,fmt=None):
                try:formula(ws,r,c,expr(),raw,fmt)
                except (_NotExcel,KeyError,ValueError,TypeError) as exc:
                    value(ws,r,c,raw,fmt);fallbacks.append({'location':loc,'reason':str(exc)})
            head(parameters,['参数ID','类型/原文区间','实际数值/下限','费率','含下界','来源','适用规则'])
            p_refs={};s_refs={};pr=1
            proofs=dict(basis['parameter_evidence'])
            if settlement:
                # Additional roster parameters are resolved from its real
                # source contract by the replay path, not from expected money.
                proofs.update(settlement.get('input_parameter_evidence',{}))
            for name,item in proofs.items():
                if item.get('kind')=='schedule':
                    b=[]
                    for band in item['bands']:
                        parameters.write_row(pr,0,[name,band['label']]);value(parameters,pr,2,band['lower']);value(parameters,pr,3,band['rate']);value(parameters,pr,4,band['inclusive']);value(parameters,pr,5,band['band_source']+'；'+band['rate_source']);value(parameters,pr,6,_json(item['rules']))
                        b.append((_ref(parameters.name,pr,2),_ref(parameters.name,pr,3),band['inclusive']));pr+=1
                    s_refs[name]={**item,'bands':b}
                else:
                    parameters.write_row(pr,0,[name,item.get('raw','')]);value(parameters,pr,2,item['value']);value(parameters,pr,5,item['source']);value(parameters,pr,6,_json(item['rules']));p_refs[name]=_ref(parameters.name,pr,2);pr+=1
            head(sources,['实际源坐标','语义字段','原始标准化值','用途/说明']);src_refs={};sr=1
            def source_cell(location,field,raw,why=''):
                nonlocal sr
                k=(location,field,fingerprint(raw))
                if k not in src_refs:
                    sources.write_row(sr,0,[str(location),str(field)]);value(sources,sr,2,raw);value(sources,sr,3,why)
                    src_refs[k]=_ref(sources.name,sr,2);sr+=1
                return src_refs[k]
            head(queries,['项目编号','指标ID','真实来源/查询表达式','查询值','首次出现标记','核验说明']);qr=1
            headers=['项目编号']+[label for _,_,label in cols]+[label+suffix for _,_,label in slots for suffix in ('','金额')]
            # Distinct semantic IDs may have the same label; maintain a unique
            # visible header without changing the supplied first occurrence.
            used=set()
            for i,label in enumerate(headers):
                candidate=label;j=2
                while candidate in used:candidate=f'{label}（{j}）';j+=1
                headers[i]=candidate;used.add(candidate)
            head(work,headers);work.set_column(0,0,72)
            for ri,key in enumerate(order,1):project_rows[key]=ri
            contribution_refs={}
            def render_queries(key,p,refs,pool):
                nonlocal qr
                qs={}
                for name,traces in p['traces'].items():
                    for t in traces:
                        if t['kind']=='query_ref':t=pool[t['id']]
                        if t['kind']=='cell':source_cell(t['source'],t['field'],t['value'],f'{key}/{name}')
                        if t['kind']!='query':continue
                        q=t['query'];first=qr;distinct=set()
                        for item in t['items']:
                            queries.write_row(qr,0,[key,name,item['source']]);row_fields={}
                            for f,v in item['fields'].items():
                                if f in contribution_refs.get(item['source'],{}):row_fields[f]=contribution_refs[item['source']][f]
                                else:row_fields[f]=source_cell(item['cells'].get(f,item['source']),f,v)
                            if 'value' in q:
                                metric_value(queries,qr,3,lambda q=q,row_fields=row_fields:excel_expr(q['value'],refs,p_refs,s_refs,qs,state['period'],row_fields),item['value'],f'{item["source"]}/{name}')
                            else:value(queries,qr,3,1)
                            sig=fingerprint(item['value']);value(queries,qr,4,1 if sig not in distinct else 0);distinct.add(sig);qr+=1
                        queries.write_row(qr,0,[key,name,_json(q)]);value(queries,qr,5,f'匹配 {t["selected"]} 行，实际值 {t["numeric_or_values"]}；规则冻结的选择范围')
                        agg=t['aggregate'];result=t['result']
                        if qr==first:e='""' if result is None else literal(result)
                        else:
                            span=_range(queries.name,first,qr-1,3)
                            if agg=='exists':e=f'COUNT({span})>0'
                            elif agg=='count':e=f'COUNT({span})'
                            elif agg=='count_distinct':e=f'SUM({_range(queries.name,first,qr-1,4)})'
                            elif agg=='unique':e=_ref(queries.name,first,3)
                            else:e=f'{agg.upper() if agg!="avg" else "AVERAGE"}({span})'
                        formula(queries,qr,3,e,result);qs[fingerprint(q)]=_ref(queries.name,qr,3);qr+=1
                return qs
            for key in order:
                ri=project_rows[key];p=projects[key];value(work,ri,0,key)
                refs={name:_ref(work.name,ri,c) for name,c in canonical.items()};qs=render_queries(key,p,refs,basis.get('trace_pool',{}))
                for ci,(kind,name,_) in enumerate(cols,1):
                    raw=p['key_values'][name] if kind=='key' else p['metrics'][name]
                    if kind=='key':value(work,ri,ci,raw);continue
                    metric_value(work,ri,ci,lambda name=name:excel_expr(metrics[name]['expr'],refs,p_refs,s_refs,qs,state['period']),raw,f'{key}/{name}',moneyfmt if name in money_ids else None)
            # Every role amount is an actual project-allocation × role × share
            # formula. Dynamic identity and name columns are separate.
            role_ref={}
            if role_sheet:
                head(role_sheet,['项目编号','角色ID','角色','人员ID','姓名/待分配','角色比例','个人比例','角色原值','汇总前金额','去向','来源'])
                counters=defaultdict(int);person_round=settlement['rounding'];rd=person_round['digits']
                for i,row in enumerate(settlement['rows'],1):
                    role_sheet.write_row(i,0,[row['project'],row['role_id'],row['role'],row['person_id'],row['name']]);value(role_sheet,i,5,row['role_rate']);value(role_sheet,i,6,row['share'])
                    cell=_ref(work.name,project_rows[row['project']],canonical[('ref',funding['alloc'])]);formula(role_sheet,i,7,f'{cell}*F{i+1}*G{i+1}',row['raw'])
                    actual=row['raw'].quantize(D(1).scaleb(-rd),rounding='ROUND_HALF_UP') if person_round['when']=='per_record' else row['raw']
                    formula(role_sheet,i,8,f'ROUND(H{i+1},{rd})' if person_round['when']=='per_record' else f'H{i+1}',actual)
                    value(role_sheet,i,9,row['destination']);value(role_sheet,i,10,row['source']);k=(row['project'],row['role_id']);index=counters[k];counters[k]+=1;role_ref[(k[0],k[1],index)]=i
                    loc=f'已确认项目归属/{row["project"]}/{row["role_id"]}/{i}'
                    contribution_refs[loc]={f:_ref(role_sheet.name,i,c) for f,c in [('project',0),('role_id',1),('person_id',3),('name',4),('raw',7),('destination',9)]}
                    contribution_refs[loc]['rounded_record']=f'ROUND({_ref(role_sheet.name,i,7)},{rd})'
                start=1+len(cols)
                for key in order:
                    for j,(rid,i,label) in enumerate(slots):
                        rr=role_ref.get((key,rid,i));ri=project_rows[key]
                        if rr is None:continue
                        row=role_groups[(key,rid)][i];formula(work,ri,start+2*j,_ref(role_sheet.name,rr,4),row['name']);formula(work,ri,start+2*j+1,_ref(role_sheet.name,rr,7),row['raw'])
                        inline_checks[(ri,start+2*j)]=row['name'];inline_checks[(ri,start+2*j+1)]=row['raw']
            personal_cols=[];personal_canonical={};personal_rows={};person_id_rows={}
            if person_sheet:
                plan=settlement['person_plan'];pmetrics=plan['program']['metrics'];pseen=set()
                for col in layout.get('person_columns',[]):
                    k,v=next(iter(col['source'].items()));personal_cols.append((k,v,col['label']));pseen.add((k,v))
                personal_cols.extend(('key',k,k) for k in personal['key_fields'] if ('key',k) not in pseen)
                personal_cols.extend(('ref',k,label) for k,label in personal['metric_fields'].items() if ('ref',k) not in pseen)
                for ci,(k,v,_) in enumerate(personal_cols,1):personal_canonical.setdefault((k,v),ci)
                head(person_sheet,['人员唯一键']+[label for _,_,label in personal_cols]);person_sheet.set_column(0,0,42)
                for ri,(identity,person) in enumerate(personal['projects'].items(),1):
                    personal_rows[identity]=ri;pid=str(person['key_values'][plan['identity_key']]);person_id_rows[pid]=ri;value(person_sheet,ri,0,identity)
                    refs={k:_ref(person_sheet.name,ri,ci) for k,ci in personal_canonical.items()};qs=render_queries(identity,person,refs,personal.get('trace_pool',{}))
                    for ci,(kind,name,_) in enumerate(personal_cols,1):
                        raw=person['key_values'][name] if kind=='key' else person['metrics'][name]
                        if kind=='key':value(person_sheet,ri,ci,raw)
                        else:metric_value(person_sheet,ri,ci,lambda name=name:excel_expr(pmetrics[name]['expr'],refs,p_refs,s_refs,qs,state['period']),raw,f'人员/{pid}/{name}')
            head(summary,['名称','金额','人员ID/附注','去向']);summary_rows={};row=1
            total_ids={funding['labels']['alloc']:funding['alloc'],funding['labels']['total']:funding['total'],**{v:k for k,v in funding['destinations'].items()}}
            for label,amount in basis['totals'].items():
                mid=total_ids[label];value(summary,row,0,label);span=_range(work.name,1,len(order),canonical[('ref',mid)]);formula(summary,row,1,f'ROUND(SUM({span}),{digits})',amount,moneyfmt);summary_rows[label]=row;row+=1
            if settlement:
                row+=1;summary.write_row(row,0,['人员/待分配','金额','人员唯一ID','去向'],hfmt);row+=1;begin=row
                end_role=len(settlement['rows']);ids=_range(role_sheet.name,1,end_role,3);dest=_range(role_sheet.name,1,end_role,9);amounts=_range(role_sheet.name,1,end_role,8)
                for item in settlement['people']:
                    value(summary,row,0,item['name']);value(summary,row,2,item['id']);value(summary,row,3,item['destination']);line=row+1
                    if person_sheet and item['destination']=='person':
                        e=_ref(person_sheet.name,person_id_rows[item['id']],personal_canonical[('ref',personal['funding']['alloc'])])
                    else:
                        e=f'SUMPRODUCT(--EXACT({ids},C{line}),--EXACT({dest},D{line}),{amounts})'
                        if settlement['rounding']['when']!='none':e=f'ROUND({e},{settlement["rounding"]["digits"]})'
                    formula(summary,row,1,e,item['amount'],moneyfmt)
                    person_rows.append((row,item));row+=1
                amount_span=_range(summary.name,begin,row-1,1);destination_span=_range(summary.name,begin,row-1,3);named_row=row
                value(summary,row,0,'实名人员合计');formula(summary,row,1,f'SUMIF({destination_span},"person",{amount_span})',settlement['allocated'],moneyfmt);row+=1;pending_row=row
                value(summary,row,0,'待分配合计');formula(summary,row,1,f'SUMIF({destination_span},"pending",{amount_span})',settlement['pending'],moneyfmt);row+=1
                summary_checks[named_row]=settlement['allocated'];summary_checks[pending_row]=settlement['pending']
                span=_range(work.name,1,len(order),canonical[('ref',funding['alloc'])]);deductions=[];adjust_ref=None
                if person_sheet:
                    bridge=settlement['funding_bridge'];pf=personal['funding'];pn=len(personal_rows)
                    def psum(mid):return f'SUM({_range(person_sheet.name,1,pn,personal_canonical[("ref",mid)])})'
                    row+=1;value(summary,row,0,'人员上游归属合计');formula(summary,row,1,psum(settlement['person_plan']['attributed_metric']),bridge['named_attributed'],moneyfmt);attrib_row=row
                    row+=1;value(summary,row,0,'人员规则调整后应得合计');formula(summary,row,1,psum(pf['total']),bridge['entitled'],moneyfmt);entitled_row=row
                    row+=1;value(summary,row,0,'人员规则调整（归属减应得）');formula(summary,row,1,f'B{attrib_row+1}-B{entitled_row+1}',bridge['rule_adjustment'],moneyfmt);adjust_ref=f'B{row+1}'
                    summary_checks.update({attrib_row:bridge['named_attributed'],entitled_row:bridge['entitled'],row:bridge['rule_adjustment']})
                    for mid,label in bridge['destination_labels'].items():
                        row+=1;value(summary,row,0,label);formula(summary,row,1,psum(mid),bridge['deductions'][mid],moneyfmt);deductions.append(f'B{row+1}')
                        summary_checks[row]=bridge['deductions'][mid]
                    for item in layout.get('person_totals',[]):
                        vals=[p['metrics'][item['metric']] for p in personal['projects'].values()]
                        if not all(isinstance(x,D) for x in vals):raise ProgramError('人员汇总字段不是金额/数值',owner='export')
                        row+=1;value(summary,row,0,item['label']);formula(summary,row,1,psum(item['metric']),sum(vals,D(0)),moneyfmt)
                        summary_checks[row]=sum(vals,D(0))
                    row+=1
                value(summary,row,0,'舍入尾差');e=f'SUM({span})-B{named_row+1}-B{pending_row+1}'
                if adjust_ref:e+='-'+adjust_ref+''.join('-'+x for x in deductions)
                formula(summary,row,1,e,settlement['residual'],moneyfmt)
                summary_checks[row]=settlement['residual']
            else:summary.write(row+1,0,'本版本总额等待真实确认，未计算人员金额。')
            if grouped:
                head(grouped,['分组要求','分组值','指标ID','合计']);gr=1
                for definition in layout['groups']:
                    groups=defaultdict(list)
                    for key,p in projects.items():
                        vals=[]
                        for src in definition['by']:
                            typ,v=next(iter(src.items()));vals.append(p['key_values'][v] if typ=='key' else p['metrics'][v])
                        groups[tuple(vals)].append(key)
                    for vals,keys_ in groups.items():
                        for mid in definition['metrics']:
                            refs_=[_ref(work.name,project_rows[k],canonical[('ref',mid)]) for k in keys_];vals_=[projects[k]['metrics'][mid] for k in keys_]
                            if not all(isinstance(x,D) for x in vals_):raise ProgramError(f'分组汇总 {mid} 不是金额/数值',owner='export')
                            grouped.write_row(gr,0,[definition['label'],_json(vals),mid]);formula(grouped,gr,3,'SUM('+','.join(refs_)+')',sum(vals_,D(0)));gr+=1
            head(ledger,['来源条款/单元','阶段','实际原文/理由','实际计算组件','状态']);lr=1
            all_coverage={**basis.get('rule_coverage',{}),**((settlement or {}).get('rule_coverage',{})),**state.get('adaptive_output_coverage',{})}
            for rid,record in all_coverage.items():
                clause=record['clause'];ledger.write_row(lr,0,[rid,clause['phase'],clause['quote'][:32760],_json(record['components'])[:32760],'已进入执行合同']);lr+=1
            for uid,item in state.get('adaptive_rule_decisions',basis.get('rule_unit_decisions',{})).items():
                ledger.write_row(lr,0,[uid,item['disposition'],item['reason'],item['file']+'/'+item['where'],'来源已覆盖']);lr+=1
            for fallback in fallbacks:
                ledger.write_row(lr,0,[fallback['location'],'程序公式','已按程序表达式执行并独立数值验证；Excel采用冻结程序值',fallback['reason'],'不可编辑计算语义']);lr+=1
            confidence=book.add_worksheet('指标可信度');head(confidence,['计算层','指标ID','指标名称','最低证据分','需复核对象数','实际风险示例']);cr=1
            for level,current in [('项目',basis)]+([('人员',personal)] if personal else []):
                for mid,item in sorted(current.get('indicator_confidence',{}).items(),key=lambda x:(x[1]['min_score'],x[0])):
                    confidence.write_row(cr,0,[level,mid,current['metric_fields'].get(mid,mid),item['min_score'],item['risk_projects'],_json(item['examples'])[:32760]]);cr+=1
            head(receipts,['节点','状态','独立复核','证据']);
            for i,(k,v) in enumerate(state['receipts'].items(),1):receipts.write_row(i,0,[k,v.get('status'),v.get('reviewer',''),str(v.get('review',''))[:32760]])
            head(versions,['版本字段','值']);approval=state.get('approval') or {}
            from bonus_agent.commission_revision import receipt_token
            vals={'报表格式':FORMAT,'执行程序':state['profile'],'规则/来源合同':fingerprint(contract_state),'总额金额指纹':state['basis_digest'],'确认凭据':receipt_token(state),'审批人':approval.get('by','尚未确认'),'财务修订':state.get('revision_id',''),'核算期间':state['period'],'Excel程序值说明':'条件查询是本版本冻结的选择；规则变化必须重新编制，不按编辑底稿绕过复核。'}
            for i,(k,v) in enumerate(vals.items(),1):versions.write_row(i,0,[k,str(v)])
        # Independent readback of the complete dynamic financial contract.
        cached=load_workbook(target,read_only=True,data_only=True)
        try:
            w=cached['提成奖核算底稿'];observed=list(w.values)[1:]
            if len(observed)!=len(order):raise ProgramError('Excel项目对象覆盖不一致',owner='export')
            for row,key in zip(observed,order):
                if row[0]!=key:raise ProgramError('Excel项目唯一键不一致',owner='export')
                for i,(kind,name,_) in enumerate(cols,1):
                    expected=projects[key]['key_values'][name] if kind=='key' else projects[key]['metrics'][name]
                    valid=_money_equal(row[i],expected,digits) if kind=='ref' and name in money_ids else _equal(row[i],expected)
                    if not valid:raise ProgramError(f'Excel {key}/{name} 回读不一致：{row[i]!r}/{expected!r}',owner='export')
            for (ri,ci),expected in inline_checks.items():
                if not _equal(observed[ri-1][ci],expected):raise ProgramError('Excel项目内联人员/角色金额回读不同',owner='export')
            sum_sheet=list(cached['核算汇总'].values)
            for name,ri in summary_rows.items():
                if not _money_equal(sum_sheet[ri][1],basis['totals'][name],digits):raise ProgramError(f'Excel汇总 {name} 回读不一致',owner='export')
            for ri,item in person_rows:
                vals=sum_sheet[ri][:4];person_digits=personal['funding']['summary_digits'] if personal else settlement['rounding']['digits']
                if vals[0]!=item['name'] or not _money_equal(vals[1],item['amount'],person_digits) or tuple(vals[2:])!=(item['id'],item['destination']):raise ProgramError('Excel实名/待分配回读不一致',owner='export')
            for ri,expected in summary_checks.items():
                if not _money_equal(sum_sheet[ri][1],expected,max(digits,settlement['rounding']['digits'])):raise ProgramError(f'Excel资金去向汇总 {sum_sheet[ri][0]} 回读不一致',owner='export')
            if settlement:
                actual=list(cached['角色分配'].values)[1:]
                if len(actual)!=len(settlement['rows']):raise ProgramError('Excel角色行数不一致',owner='export')
                for row,expected in zip(actual,settlement['rows']):
                    if row[:5]!=(expected['project'],expected['role_id'],expected['role'],expected['person_id'],expected['name']) or not _equal(row[7],expected['raw']):raise ProgramError('Excel角色金额/人员键回读不一致',owner='export')
                if personal:
                    observed=list(cached['人员核算底稿'].values)[1:]
                    if len(observed)!=len(personal['projects']):raise ProgramError('Excel人员主集合覆盖不一致',owner='export')
                    for row,(identity,p) in zip(observed,personal['projects'].items()):
                        if row[0]!=identity:raise ProgramError('Excel人员唯一键回读不一致',owner='export')
                        for i,(kind,name,_) in enumerate(personal_cols,1):
                            expected=p['key_values'][name] if kind=='key' else p['metrics'][name]
                            if not _equal(row[i],expected):raise ProgramError(f'Excel人员层 {identity}/{name} 回读不一致',owner='export')
        finally:cached.close()
        formulas=load_workbook(target,read_only=True,data_only=False)
        try:
            # Scan each sheet once rather than quadratic per-cell streaming.
            required=defaultdict(set)
            for s,r,c in formula_expected:required[s].add((r,c))
            for name,required_cells in required.items():
                seen=set()
                for ri,row in enumerate(formulas[name].iter_rows()):
                    for ci,cell in enumerate(row):
                        if (ri,ci) in required_cells and cell.data_type=='f':seen.add((ri,ci))
                if seen!=required_cells:raise ProgramError(f'Excel {name} 实际公式缺失',owner='export')
        finally:formulas.close()
        target.replace(path)
        return {'format':FORMAT,'formula_count':len(formula_expected),'program_value_count':len(fallbacks),'projects':len(order),'people':len(person_rows)}
    finally:target.unlink(missing_ok=True)
