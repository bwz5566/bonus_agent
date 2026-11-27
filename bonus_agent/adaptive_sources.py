"""Compile physical material bindings into the adaptive financial program.

The model chooses coordinates and semantic fields. Actual values are always
read again by this module. A dataset may consist of many sheets/files and its
grain, units, exclusions, and duplicate handling are explicit contracts.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal as D
import re
import unicodedata

from openpyxl.utils import column_index_from_string,get_column_letter

from bonus_agent.commission_sources import Catalog,ChainError,string,month
from bonus_core.data.table import to_date,to_bool
from bonus_core.dsl.models import to_decimal
from bonus_core.engine.adaptive import ProgramError,Record,Dataset,Inputs,Schedule,contract,names
from bonus_core.engine.intervals import parse_interval,ordered_intervals,Interval
from bonus_agent.adaptive_rules import normalized


FIELD_KINDS={'text','id','number','money','ratio','month','date','bool'}
UNITS={'raw','percent','wan','yi'}
UNIT_SCALE={'raw':D(1),'percent':D('.01'),'wan':D(10000),'yi':D(100000000)}


def convert_value(value,spec,*,location='',unit=None):
    """Convert one atomic value without treating a missing value as zero."""
    kind=spec['kind'];unit=unit or spec.get('unit','raw')
    if value is None or isinstance(value,str) and not value.strip():
        if spec.get('nullable',False):return None
        raise ProgramError(f'{location} 必需字段为空',owner='scope_mapping',kind='data',location=location)
    try:
        if kind in ('text','id'):
            val=string(value)
            if spec.get('normalize')=='casefold':val=val.casefold()
            return val
        if kind=='month':return month(value,location)
        if kind=='date':return to_date(value)
        if kind=='bool':return to_bool(value)
        if kind not in ('number','money','ratio') or unit not in UNITS:raise ValueError(f'未知字段类型或单位 {kind}/{unit}')
        if isinstance(value,str):
            raw=unicodedata.normalize('NFKC',value).strip()
            suffix=next((s for s in ('万元','亿元','%','万','亿','元') if raw.endswith(s)),None)
            if suffix is not None:
                if kind=='ratio' and suffix!='%' or kind=='money' and suffix=='%':raise ValueError('数值后缀与指标单位不符')
                explicit={'万元':'wan','万':'wan','亿元':'yi','亿':'yi','%':'percent','元':'raw'}[suffix]
                return to_decimal(raw[:-len(suffix)])*UNIT_SCALE[explicit]
        return to_decimal(value)*UNIT_SCALE[unit]
    except (ValueError,TypeError,ChainError,ArithmeticError) as exc:
        raise ProgramError(f'{location} 原值 {value!r} 不能按 {kind}/{unit} 转换：{exc}',owner='scope_mapping',kind='data',location=location) from exc


def _field_spec(spec,name,owner):
    contract(spec,required=('kind','nullable'),optional=('unit','normalize','label','minimum','maximum','allowed','rules'),label=f'字段 {name}',owner=owner)
    if spec['kind'] not in FIELD_KINDS or type(spec['nullable']) is not bool:raise ProgramError(f'字段 {name} 类型或空值政策无效',owner=owner)
    if spec.get('unit','raw') not in UNITS or spec.get('normalize','trim') not in ('trim','casefold'):
        raise ProgramError(f'字段 {name} 单位或归一化策略无效',owner=owner)
    return spec


def _check_value(value,spec,location,owner):
    if value is None:return
    for bound,cmp in [('minimum',lambda a,b:a<b),('maximum',lambda a,b:a>b)]:
        if bound in spec:
            limit=convert_value(spec[bound],{**spec,'nullable':False,'unit':'raw'},location=location)
            if cmp(value,limit):raise ProgramError(f'{location} 原值 {value} 违反 {bound}={limit}',owner=owner,kind='data',location=location,rules=spec.get('rules',[]))
    if 'allowed' in spec:
        if not isinstance(spec['allowed'],list) or value not in [convert_value(x,{**spec,'nullable':False,'unit':'raw'},location=location) for x in spec['allowed']]:
            raise ProgramError(f'{location} 原值 {value!r} 不在授权值域 {spec["allowed"]}',owner=owner,kind='data',location=location,rules=spec.get('rules',[]))


def read_datasets(catalog: Catalog, specs: dict, *, period, owner='scope_mapping'):
    """Build datasets and an exhaustive physical-row coverage report."""
    if not isinstance(specs,dict):raise ProgramError('数据集合同须为对象',owner=owner)
    result={};diagnostics={}
    for name,ds in specs.items():
        try:
            contract(ds,required=('label','fields','sources','grain','rules'),optional=('relationships','purpose'),label=f'数据集 {name}',owner=owner)
            if ds.get('purpose','business') not in ('business','lookup'):raise ProgramError(f'{name} 数据用途无效',owner=owner)
            fields=ds['fields']
            if not isinstance(fields,dict) or not fields:raise ProgramError(f'{name} 缺少语义字段',owner=owner)
            for f,s in fields.items():_field_spec(s,f,owner)
            grain=contract(ds['grain'],required=('kind','keys','duplicates','rules'),label=f'{name} 数据粒度',owner=owner)
            if grain['kind'] not in ('detail','unique') or grain['duplicates'] not in ('error','identical'):
                raise ProgramError(f'{name} 粒度必须声明明细或唯一对象，以及重复处理',owner=owner)
            keys=names(grain['keys'],f'{name} 粒度键',owner,nonempty=grain['kind']=='unique')
            if not set(keys)<=set(fields):raise ProgramError(f'{name} 粒度键不在字段合同中',owner=owner)
            if not isinstance(ds['sources'],list) or not ds['sources']:raise ProgramError(f'{name} 没有实际材料',owner=owner)
            nowhere={f for f in fields if all(isinstance(src,dict) and isinstance(src.get('absent_fields'),dict) and f in src['absent_fields'] for src in ds['sources'])}
            rows=[];seen_physical=set();excluded=[];source_summary=[];seen_keys={};ranges_by_sheet=defaultdict(list);headers_by_sheet=defaultdict(set);outside_by_sheet=defaultdict(list)
            for src in ds['sources']:
                contract(src,required=('sheet','header_row','columns'),optional=('start_row','end_row','skip','constants','excluded_ranges','absent_fields'),label=f'{name} 物理来源',owner=owner)
                sheet=catalog.sheet(src);h=src['header_row'];cols=src['columns'];constants=src.get('constants',{});absent=src.get('absent_fields',{})
                if not all(isinstance(v,dict) for v in (cols,constants,absent)):raise ProgramError(f'{name}/{sheet.id} columns/constants/absent_fields 必须是对象',owner=owner)
                supplied=set(cols)|set(constants)|set(absent);overlap=(set(cols)&set(constants))|(set(cols)&set(absent))|(set(constants)&set(absent))
                if overlap or supplied!=set(fields):
                    missing=sorted(set(fields)-supplied);extra=sorted(supplied-set(fields))
                    raise ProgramError(f'{name}/{sheet.id} 语义字段绑定不完整：未绑定 {missing}，多余 {extra}，重复 {sorted(overlap)}。若本来源确实没有可空字段，需以 absent_fields 明确实际缺列证据，不能猜测映射或填0。',owner=owner,kind='contract',details={'missing_fields':missing,'extra_fields':extra,'overlap':sorted(overlap),'source':sheet.file+'/'+sheet.name})
                raw_cols={};units={}
                for f,c in cols.items():
                    if isinstance(c,dict):
                        contract(c,required=('column','unit'),label=f'{name}.{f} 列单位',owner=owner)
                        units[f]=c['unit'];c=c['column']
                        if units[f] not in UNITS:raise ProgramError(f'{name}.{f} 未知列单位',owner=owner)
                    raw_cols[f]=c
                sheet.validate({'sheet':sheet.id,'header_row':h,'columns':raw_cols},tuple(raw_cols))
                indices={k:column_index_from_string(v) for k,v in raw_cols.items()}
                # Explicit field constants must be traceable to an actual
                # document/cell or the selected period, not model inventions.
                constant_values={};constant_locations={}
                headers={normalized(string(v)) for v in sheet.rows[h-1] if string(v).strip()}
                for f,proof in absent.items():
                    contract(proof,required=('reason','rules'),label=f'{name}.{f} 来源缺列',owner=owner)
                    if not fields[f]['nullable'] or f in keys or not isinstance(proof['reason'],str) or not proof['reason'].strip():
                        raise ProgramError(f'{name}/{sheet.id} 缺失字段 {f} 必须可空、非粒度键并说明实际缺列证据；不能将待计算的派生指标伪装为物理缺列，应在公式节点由真实原始字段计算',owner=owner,kind='binding')
                    names(proof['rules'],f'{name}.{f} 缺列条款',owner,nonempty=False)
                    known={normalized(f),normalized(fields[f].get('label',f))}
                    for other in ds['sources']:
                        col=other.get('columns',{}).get(f)
                        if isinstance(col,dict):col=col.get('column')
                        if not col:continue
                        try:known.add(normalized(catalog.sheet(other).literal(other['header_row'],column_index_from_string(col))))
                        except (ChainError,ValueError,TypeError,KeyError):continue
                    if headers&known:raise ProgramError(f'{name}/{sheet.id} 声明缺列的 {f} 实际存在已知表头 {sorted(headers&known)}，必须真实绑定',owner=owner,kind='binding')
                    constant_values[f]=None;constant_locations[f]=f'{sheet.file}/{sheet.name}!{h}：来源未提供 {f}；{proof["reason"]}'
                for f,source in constants.items():
                    if source=={'period':'month'}:
                        from bonus_core.data.period import Period
                        window=Period.parse(period)
                        if window is None or window.months!=1:raise ProgramError('跨月核算不能把整个期间赋成一个月份常量；必须读取实际期间列或分月源',owner=owner,kind='binding')
                        constant_values[f]=convert_value(window.end.strftime('%Y%m'),fields[f],location=f'{name}.{f} 核算期')
                        constant_locations[f]='本版核算期间'
                    else:
                        literal,loc=read_literal(catalog,source)
                        constant_values[f]=convert_value(literal,fields[f],location=loc)
                        constant_locations[f]=loc
                # A monthly budget/other wide column with an explicit month
                # in its header must not be bound to the wrong current period.
                for f,i in indices.items():
                    heading=string(sheet.literal(h,i,display=True))
                    hit=re.fullmatch(r'(?:\d{4}年)?\s*(\d{1,2})\s*月(?:预算|计划|实际|金额|销量|销售额)?',heading)
                    if hit and any(v=={'period':'month'} for v in constants.values()) and int(hit[1])!=window.end.month:
                        raise ProgramError(f'{sheet.file}/{sheet.name}!{get_column_letter(i)}{h} 的月份与核算期间 {period} 不符',owner=owner,kind='binding')
                start=src.get('start_row',h+1);end=src.get('end_row',len(sheet.rows))
                if type(start) is not int or type(end) is not int or not h<start<=end+1 or end>len(sheet.rows):
                    raise ProgramError(f'{name}/{sheet.id} 物理范围 {start}..{end} 无效',owner=owner)
                ranges_by_sheet[sheet.id].append((start,end));headers_by_sheet[sheet.id].add(h)
                for section in src.get('excluded_ranges',[]):
                    contract(section,required=('start','end','reason','rules'),label=f'{name} 源外范围排除',owner=owner)
                    a,b=section['start'],section['end']
                    if type(a) is not int or type(b) is not int or not 1<=a<=b<=len(sheet.rows) or not section['reason']:
                        raise ProgramError(f'{name}/{sheet.id} 排除范围无效',owner=owner)
                    outside_by_sheet[sheet.id].append((a,b,section['reason']))
                skip=[]
                for filter_spec in src.get('skip',[]):
                    contract(filter_spec,required=('column','values','reason','rules'),label=f'{name} 排除行',owner=owner)
                    index=column_index_from_string(filter_spec['column'])
                    if index>len(sheet.rows[h-1]) or not isinstance(filter_spec['values'],list) or not filter_spec['reason']:
                        raise ProgramError(f'{name} 排除列/条件无实际依据',owner=owner)
                    skip.append((index,{string(x) for x in filter_spec['values']},filter_spec['reason']))
                count=0;dupes=0;empty_rows=0
                for n in range(start,end+1):
                    raw=sheet.rows[n-1];where=f'{sheet.file}/{sheet.name}!{n}'
                    if all(v in (None,'') for v in raw):empty_rows+=1;continue
                    reason=next((reason for i,values,reason in skip if string(raw[i-1] if i<=len(raw) else None) in values),None)
                    if reason:
                        excluded.append({'where':where,'reason':reason});continue
                    physical=(sheet.id,n)
                    if physical in seen_physical:raise ProgramError(f'{name} 同一物理行被多次导入：{where}',owner=owner,kind='duplicate',location=where)
                    seen_physical.add(physical)
                    vals=dict(constant_values);locations=dict(constant_locations)
                    for f,i in indices.items():
                        loc=f'{sheet.file}/{sheet.name}!{get_column_letter(i)}{n}'
                        # Percent-style Excel cells contain true ratios;
                        # converting their displayed suffix once retains units.
                        display=fields[f]['kind'] in ('id','ratio','number','money')
                        cell=sheet.literal(n,i,display=display) if i<=len(raw) else None
                        try:vals[f]=convert_value(cell,fields[f],location=loc,unit=units.get(f))
                        except ProgramError as exc:exc.owner=owner;exc.dataset=name;raise
                        locations[f]=loc
                    for f,v in vals.items():_check_value(v,fields[f],locations[f],owner)
                    if keys:
                        key=tuple(vals[k] for k in keys)
                        if any(v is None or v=='' for v in key):raise ProgramError(f'{name} 数据粒度键存在空值 {key}：{where}',owner=owner,kind='data',location=where)
                        if key in seen_keys:
                            before=seen_keys[key]
                            if grain['duplicates']=='identical' and before.values==vals:
                                dupes+=1;excluded.append({'where':where,'reason':'经明确声明的同键完全相同记录去重','first':before.source});continue
                            raise ProgramError(f'{name} 数据粒度键 {key} 重复：{before.source} / {where}；不能猜测加总或去重。若真实材料是多条明细对应同一主对象，保留明细粒度并由population分组、公式节点聚合，不得声明已聚合唯一表',owner=owner,kind='duplicate',dataset=name,location=where,
                                               details={'first':before.source,'second':where,'key':list(key),'first_values':before.values,'second_values':vals})
                    record=Record(vals,where,locations);rows.append(record);count+=1
                    if keys:seen_keys[key]=record
                source_summary.append({'sheet':sheet.id,'file':sheet.file,'name':sheet.name,'header_row':h,'range':[start,end],
                                       'physical_rows':max(0,end-start+1),'included':count,'blank':empty_rows,'duplicate_identical':dupes,'columns':raw_cols,
                                       'absent_fields':{f:{**proof,'source':constant_locations[f]} for f,proof in absent.items()}})
            if nowhere:raise ProgramError(f'{name} 字段 {sorted(nowhere)} 在所有来源均声明缺列，不能当作物理数据字段。若是业务派生指标，删除该物理字段，在后续公式节点由已有真实字段计算；若是实际必需输入，明确材料缺口。absent_fields仅用于部分来源真缺列。',owner=owner,kind='binding',dataset=name,details={'all_sources_absent':sorted(nowhere)})
            for sid,ranges in ranges_by_sheet.items():
                sheet=catalog.sheets[sid];start=min(headers_by_sheet[sid])+1
                for n in range(start,len(sheet.rows)+1):
                    if n in headers_by_sheet[sid] or all(x in (None,'') for x in sheet.rows[n-1]):continue
                    covered=any(a<=n<=b for a,b in ranges);explicit=next((r for a,b,r in outside_by_sheet[sid] if a<=n<=b),None)
                    if covered and explicit:raise ProgramError(f'{name}/{sheet.id} 第{n}行同时被纳入和排除',owner=owner,kind='binding')
                    if not covered and not explicit:
                        raise ProgramError(f'{name} 未交代实际数据范围之外的非空行 {sheet.file}/{sheet.name}!{n}；必须绑定或明确排除依据',owner=owner,kind='coverage',dataset=name,location=f'{sheet.file}/{sheet.name}!{n}')
                    if explicit:excluded.append({'where':f'{sheet.file}/{sheet.name}!{n}','reason':explicit})
            result[name]=Dataset(fields,tuple(rows),tuple(ds['rules']))
            diagnostics[name]={'grain':grain,'sources':source_summary,'included':len(rows),'excluded':excluded,
                               'sample':[{'source':r.source,'values':r.values} for r in rows[:3]],
                               'null_counts':{k:sum(r.values[k] is None for r in rows) for k in fields}}
        except ChainError as exc:
            raise ProgramError(str(exc),owner=owner,dataset=name,kind='binding') from exc
        except ProgramError as exc:
            exc.dataset=exc.dataset or name;exc.owner=owner;raise
    return result,diagnostics


def read_literal(catalog,source):
    """Resolve a parameter from an actual table/sheet cell or exact quote."""
    if not isinstance(source,dict):raise ProgramError('参数必须指向原始单元格或规则原文')
    try:
        if 'table' in source:
            contract(source,required=('table','row','column'),label='参数表坐标')
            t=catalog.table(source['table']);r,c=source['row'],source['column']
            return t.cell(r,c),t.location(r,c)
        if 'sheet' in source:
            contract(source,required=('sheet','row','column'),label='工作表参数坐标')
            s=catalog.sheet(source);r=source['row'];col=source['column']
            c=column_index_from_string(col) if isinstance(col,str) else col
            return s.literal(r,c,display=True),f'{s.file}/{s.name}!{get_column_letter(c)}{r}'
        if 'block' in source:
            contract(source,required=('block','quote','value'),label='原文参数')
            sid=source['block'];quote=source['quote'];value=source['value']
            if sid not in catalog.blocks or not isinstance(quote,str) or not quote.strip():raise ProgramError('参数缺少实际原文ID或引用')
            raw=catalog.blocks[sid];norm=normalized
            if norm(quote) not in norm(raw['text']):raise ProgramError(f'{sid} 引用不在提供的原文中')
            token=norm(value)
            # Never validate 1 by finding the character inside 10 or 0.1.
            if re.fullmatch(r'[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:%|万|亿|万元|亿元|元)?',token):
                contains=bool(re.search(r'(?<![\d.])'+re.escape(token)+r'(?![\d.%％万亿元])',norm(quote)))
                # Explicit small Chinese numerals in count/precision phrases
                # are source values, not a model's conversion guess.
                cn={'零':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
                if not contains and re.fullmatch(r'[0-9]',token):
                    contains=any(cn[m[1]]==int(token) for m in re.finditer(r'(?<![零一二两三四五六七八九十百千万亿])([零一二两三四五六七八九])(?=位小数|位人员|名员工|名人员|人|名|条记录|次|处)',norm(quote)))
            else:contains=bool(token and token in norm(quote))
            if not contains:raise ProgramError(f'{sid} 所选参数 {value!r} 不在精确引用中')
            return value,f'{raw["file"]}/{raw["where"]}：{quote}'
    except (ChainError,ValueError) as exc:
        if isinstance(exc,ProgramError):raise
        raise ProgramError(str(exc)) from exc
    raise ProgramError('参数未指向 table、sheet 或 block')


def read_parameters(catalog,specs,*,owner='project_policy'):
    values={};proof={}
    if not isinstance(specs,dict):raise ProgramError('参数合同必须是对象',owner=owner)
    for name,p in specs.items():
        try:
            contract(p,required=('source','kind','unit','rules'),optional=('label',),label=f'参数 {name}',owner=owner)
            if 'label' in p and not isinstance(p['label'],str):raise ProgramError(f'参数 {name} label 只能为描述性文字',owner=owner)
            value,loc=read_literal(catalog,p['source'])
            if p['kind'] not in FIELD_KINDS or p['unit'] not in UNITS:raise ProgramError(f'{name} 参数类型/单位无效',owner=owner)
            values[name]=convert_value(value,{'kind':p['kind'],'unit':p['unit'],'nullable':False},location=loc)
            proof[name]={'source':loc,'raw':string(value),'value':values[name],'unit':p['unit'],'rules':p['rules']}
        except ProgramError as exc:exc.owner=owner;exc.metric=name;raise
    return values,proof


def read_schedules(catalog,specs,*,owner='project_policy'):
    result={};proof={}
    if not isinstance(specs,dict):raise ProgramError('阶梯表合同必须是对象',owner=owner)
    for name,s in specs.items():
        try:
            contract(s,required=('bound_unit','rate_unit','method','negative','below','includes','rules'),optional=('bands','layout','interpretation'),label=f'阶梯 {name}',owner=owner)
            if s['bound_unit'] not in UNITS or s['rate_unit'] not in UNITS or s['method'] not in ('whole','progressive') or s['negative'] not in ('signed','zero','error') or s['below'] not in ('first','zero','error'):
                raise ProgramError(f'{name} 阶梯计算、单位、负值或最低档口径未明确',owner=owner)
            raw_bands=s.get('bands')
            if 'layout' in s:
                if raw_bands is not None:raise ProgramError(f'{name} 阶梯不能同时声明bands和layout',owner=owner)
                layout=s['layout'];orientation=layout.get('orientation') if isinstance(layout,dict) else None
                common=('orientation',);source_keys=('table','sheet')
                if orientation=='rows':
                    contract(layout,required=common+('rows','band_column','rate_column'),optional=source_keys,label=f'{name} 纵向阶梯',owner=owner)
                elif orientation=='columns':
                    contract(layout,required=common+('columns','band_row','rate_row'),optional=source_keys,label=f'{name} 横向阶梯',owner=owner)
                else:raise ProgramError(f'{name} 未知阶梯表布局',owner=owner)
                source={k:layout[k] for k in source_keys if k in layout}
                if len(source)!=1:raise ProgramError(f'{name} layout 必须指向一个实际表',owner=owner)
                if orientation=='rows':raw_bands=[{'label':{**source,'row':r,'column':layout['band_column']},'rate':{**source,'row':r,'column':layout['rate_column']}} for r in layout['rows']]
                else:raw_bands=[{'label':{**source,'row':layout['band_row'],'column':c},'rate':{**source,'row':layout['rate_row'],'column':c}} for c in layout['columns']]
            if not isinstance(raw_bands,list) or not raw_bands:raise ProgramError(f'{name} 没有实际参数格',owner=owner)
            bands=[];refs=[];intervals=[]
            for band in raw_bands:
                contract(band,required=('label','rate'),label=f'{name} 档位格',owner=owner)
                label,loc1=read_literal(catalog,band['label']);raw,loc2=read_literal(catalog,band['rate'])
                rate=convert_value(raw,{'kind':'number','unit':s['rate_unit'],'nullable':False},location=loc2)
                mode=s.get('interpretation','intervals')
                if mode=='intervals':
                    unit={'raw':'元','wan':'万','yi':'亿','percent':'%'}[s['bound_unit']]
                    intervals.append(parse_interval(label,percent=s['bound_unit']=='percent',unit=unit))
                elif mode=='thresholds':
                    # Explicit lower-threshold schedules are not interval
                    # strings with the upper endpoint thrown away.
                    if not isinstance(label,(str,int,float,D)):raise ValueError('下限不是单个数值')
                    lo=convert_value(label,{'kind':'number','unit':s['bound_unit'],'nullable':False},location=loc1)
                    intervals.append(Interval(string(label),lo,None))
                else:raise ValueError(f'未知档位表含义 {mode}')
                bands.append(rate);refs.append({'label':string(label),'band_source':loc1,'rate_source':loc2,'raw_rate':string(raw),'rate':rate})
            includes=s['includes']
            if s.get('interpretation')=='thresholds':
                order=sorted(range(len(intervals)),key=lambda i:intervals[i].lower)
                bounds=[intervals[i].lower for i in order]
                if len(set(bounds))!=len(bounds) or len(includes)!=len(bounds) or any(type(x) is not bool for x in includes):raise ValueError('阈值重复或端点未完整声明')
            else:
                finite=[x.lower for x in intervals if x.lower is not None]
                start=min(finite) if finite and all(x.lower is not None for x in intervals) else D(0)
                if start>0 and s['below']=='first':raise ValueError('首档从正阈值开始，需要明确阈值以下是零还是不允许')
                order,bounds=ordered_intervals(intervals,includes,start=start)
            if s['method']=='progressive' and bounds[0]<0:raise ValueError('超额累进不能从负下限凭空累计金额')
            steps=tuple((lo,bands[i],inc) for lo,i,inc in zip(bounds,order,includes))
            evidence=tuple({**refs[i],'lower':lo,'inclusive':inc,'upper':intervals[i].upper} for lo,i,inc in zip(bounds,order,includes))
            result[name]=Schedule(steps,s['method'],s['negative'],s['below'],evidence)
            proof[name]={'kind':'schedule','bands':evidence,'rules':s['rules'],'method':s['method'],'negative':s['negative'],'below':s['below']}
        except (ProgramError,ChainError,ValueError,TypeError) as exc:
            if isinstance(exc,ProgramError):exc.owner=owner;exc.metric=name;raise
            raise ProgramError(f'{name}：{exc}',owner=owner,metric=name,kind='contract') from exc
    return result,proof


def check_relationships(datasets,specs,*,allow_unbound=False,owner='reference_mapping'):
    result={}
    for name,ds in specs.items():
        for i,rel in enumerate(ds.get('relationships',[])):
            contract(rel,required=('fields','target','target_fields','cardinality','rules'),label=f'{name} 关联关系',owner=owner)
            target=rel['target'];keys=names(rel['fields'],f'{name} 外键',owner);target_keys=names(rel['target_fields'],f'{name} 目标键',owner)
            if len(keys)!=len(target_keys) or not set(keys)<=set(datasets[name].fields) or rel['cardinality'] not in ('exactly_one','at_least_one','optional_unique','optional_many'):
                raise ProgramError(f'{name} 关联关系键/匹配数量无效',owner=owner)
            if target not in datasets:
                if allow_unbound:continue
                raise ProgramError(f'{name} 关联目标 {target} 尚未绑定',owner=owner,dataset=target,kind='binding')
            if not set(target_keys)<=set(datasets[target].fields):raise ProgramError(f'{target} 缺少实际关联目标字段',owner=owner,dataset=target,kind='binding')
            index=defaultdict(list)
            for row in datasets[target].rows:index[tuple(row.values[k] for k in target_keys)].append(row.source)
            matched=0;optional_missing=0
            for row in datasets[name].rows:
                key=tuple(row.values[k] for k in keys);hits=index.get(key,[]) if not any(v is None or v=='' for v in key) else []
                n=len(hits);mode=rel['cardinality'];ok=n==1 if mode=='exactly_one' else n>=1 if mode=='at_least_one' else n<=1 if mode=='optional_unique' else True
                if not ok:raise ProgramError(f'{row.source} 关联 {target} 的键 {key} 实际匹配 {n} 行，不符合 {mode}',owner=owner,dataset=name,location=row.source,kind='data',rules=rel['rules'],details={'target':target,'key':list(key),'actual_matches':hits})
                matched+=bool(n);optional_missing+=not n
            result[f'{name}:{i}']={'target':target,'matched':matched,'optional_missing':optional_missing,'cardinality':rel['cardinality'],'rules':rel['rules']}
    return result


def compile_inputs(catalog, datasets, parameters, schedules, *, period, owner='scope_mapping',allow_unbound=False,owners=None):
    if owners:
        data={};diag={};values={};p={};tables={};t={}
        for name,s in datasets.items():
            d,q=read_datasets(catalog,{name:s},period=period,owner=owners.get('datasets',{}).get(name,owner));data.update(d);diag.update(q)
        for name,s in parameters.items():
            d,q=read_parameters(catalog,{name:s},owner=owners.get('parameters',{}).get(name,'project_policy'));values.update(d);p.update(q)
        for name,s in schedules.items():
            d,q=read_schedules(catalog,{name:s},owner=owners.get('schedules',{}).get(name,'project_policy'));tables.update(d);t.update(q)
    else:
        data,diag=read_datasets(catalog,datasets,period=period,owner=owner);values,p=read_parameters(catalog,parameters,owner=owner);tables,t=read_schedules(catalog,schedules,owner=owner)
    rels=check_relationships(data,datasets,allow_unbound=allow_unbound,owner='roster' if owner=='roster' else 'reference_mapping')
    for name,rep in rels.items():diag[name.split(':',1)[0]].setdefault('relationships',[]).append(rep)
    if set(values)&set(tables):raise ProgramError('参数与阶梯表ID重复')
    return Inputs(data,values,tables,{**p,**t},diag)
