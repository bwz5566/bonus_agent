"""Material coverage and literal-formula contracts for adaptive compilation.

Coverage is a closed ledger of real source units -> operative clauses ->
executed plan components. Non-operative source units are recorded and reviewed
too. For literal arithmetic expressions we additionally compile every original
symbol and compare the actual expression tree, not merely a matching quote.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from decimal import Decimal as D
import re
import unicodedata
from openpyxl.utils import get_column_letter, column_index_from_string

from bonus_agent.commission_sources import string
from bonus_core.dsl.models import to_decimal
from bonus_core.engine.adaptive import ProgramError,contract,names


PHASES=('scope_mapping','reference_mapping','project_policy','coefficient_policy','hold_policy','roster','export')
KINDS={'policy','formula','table','validation','output'}
SOURCE_KINDS={'data','rule','mixed','output','context'}
LEVELS={'source','primary','person','report'}
OPERATIONS={'population','binding','calculation','adjustment','funding','assignment','validation','parameter','report'}
# Only unambiguous schema labels can be skipped without model review. A
# native table need not have a header: a short first row, even one with no
# percent sign, can be the first real rate or a text-only business rule.
HEADER_LABELS={
    '表名','数据表','字段','字段名','列名','列','唯一键','主键','外键','编号','名称','序号','类别','类型',
    '条件','范围','区间','档位','上限','下限','比例','费率','系数','单位','金额','参数','数值','值','指标',
    '规则','公式','含义','定义','说明','备注','用途','适用范围','参数名称','指标名称','参数值','参数单位',
    'table','tablename','field','fieldname','column','columnname','key','primarykey','foreignkey',
    'uniquekey','id','name','type','category','condition','range','interval','band','salesband',
    'lowerbound','upperbound','rate','ratio','factor','value','amount','unit','parameter','metric',
    'rule','formula','description','definition','notes','remark','purpose',
}


def attach_authorized_rules(catalog,versions):
    """Expose actual, scoped financial decisions as first-class source units."""
    for version in versions:
        sid='Rrev_'+str(version['id'])
        statement=version.get('statement') or f'{version.get("label",version["field"])}：{version["value"]}'
        catalog.blocks[sid]={'file':'财务正式规则修订','where':str(version['id']),'text':statement,
                             'authority':dict(version),'phase':version.get('owner')}


def attach_image_authority(catalog,questions):
    for q in questions:
        if hasattr(q,'model_dump'):q=q.model_dump(mode='json')
        if q.get('related')!='scope_mapping' or q.get('withdrawn') or q.get('answer')!='确认列出的图像仅为装饰或说明，不含核算规则':continue
        sources=[f'{i["file"]}/{i["where"]}' for i in catalog.unreadable_blocks if f'{i["file"]}/{i["where"]}' in q.get('text','')]
        if sources:catalog.authorized_image_exclusions[q['id']]={'sources':sources,'answer':q['answer']}


def _remove_quote(raw,quote):
    """Remove an exact normalized span, retaining all other original clauses."""
    chars=[];positions=[]
    for i,c in enumerate(raw):
        for x in normalized(c):chars.append(x);positions.append(i)
    target=normalized(quote);joined=''.join(chars)
    if not target or target not in joined:return raw,False
    intervals=[];start=0
    while (at:=joined.find(target,start))>=0:
        intervals.append((positions[at],positions[at+len(target)-1]+1));start=at+len(target)
    for a,b in reversed(intervals):raw=raw[:a]+raw[b:]
    return raw,True


def normalized(text):
    return re.sub(r'\s+','',unicodedata.normalize('NFKC',str(text)).translate(str.maketrans({'×':'*','÷':'/','−':'-','–':'-','＝':'='})))


def structural_header(cells):
    labels=[normalized(string(c)).lower() for c in cells if string(c).strip()]
    return len(labels)>1 and all(label in HEADER_LABELS for label in labels)


def matrix_header(cells,following):
    """A native matrix can have a blank corner and footnoted category names.

    Recognize the physical header only from its actual shape and following
    numeric cells, never from a particular product, role, or business title.
    """
    if len(cells)<3 or string(cells[0]).strip():return False
    labels=[normalized(string(c)) for c in cells[1:] if string(c).strip()]
    if len(labels)<2 or len(set(labels))!=len(labels):return False
    if any(re.search(r'\d|%|=|[+/]',x) or _has_arithmetic(x) for x in labels):return False
    for row in following[:4]:
        if len(row)>=len(cells) and string(row[0]).strip() and sum(bool(re.fullmatch(r'[+-]?[\d.,]+%?',normalized(string(c)))) for c in row[1:])>=2:
            return True
    return False


def explicit_binding_assertions(text):
    """Recognize unambiguous, global physical-layout and amount-unit requirements.

    These are compiler assertions, not inferred financial policies. A paragraph
    may contain both document descriptions and a binding directive. The latter
    must retain its original scope and be verified against the actual contract.
    Other prose is still interpreted and independently reviewed by the model.
    """
    n=normalized(text)
    if re.match(r'^(?:示例|例如|举例|废止规则|旧规则)',n):return []
    result=[];domain=None;all_tables=False
    # Preserve the actual input/output domain across adjacent phrases. Output
    # display in 万元 is not authorization to scale incoming monetary values.
    for segment in re.split(r'[。；;，,]',n):
        inputs=list(re.finditer(r'(?:原始|输入|业务)(?:材料|数据|表|金额|字段)|源(?:表|文件|数据)',segment))
        reports=list(re.finditer(r'输出|报表|结果表|展示|显示',segment))
        any_table=bool(re.search(r'(?:所有|全部|各)(?:输入|业务|工作|数据)*表',segment))
        new_domain=('report' if reports and (not inputs or reports[-1].start()>inputs[-1].start()) else
                    'input' if inputs else 'input' if any_table and domain!='report' else domain)
        if new_domain!=domain:all_tables=False
        domain=new_domain
        if domain!='input':continue
        all_tables=all_tables or any_table
        if all_tables:
            for kind,pattern in (
                ('header_row',r'第(\d+)行(?:为|是|作为)(?:列)?表头'),
                ('data_start',r'第(\d+)行(?:起|开始)(?:为|是)?(?:正式)?数据'),
            ):
                for m in re.finditer(pattern,segment):result.append({'kind':kind,'value':int(m[1]),'source':m[0]})
        units=r'(万元|亿元|元)'
        pattern=r'(?:(?:所有|全部|各)金额(?:列)?(?:的)?单位(?:均|统一|皆)?|金额(?:列)?(?:的)?单位(?:均|统一|皆))(?:为|是|[:：])(?:人民币)?'+units
        matches=list(re.finditer(pattern,segment))
        if all_tables and not matches:
            matches=list(re.finditer(r'金额(?:列)?(?:的)?单位(?:为|是|[:：])(?:人民币)?'+units,segment))
        for m in matches:result.append({'kind':'money_unit','value':{'元':'raw','万元':'wan','亿元':'yi'}[m[1]],'source':m[0]})
    return result


def sparse_source_columns(sheet, *, limit=8):
    """Bounded physical evidence for side-column notes in a business ledger."""
    counts=defaultdict(int);cells=defaultdict(list)
    for r,row in enumerate(sheet.rows,1):
        for c,value in enumerate(row,1):
            if value in (None,''):continue
            counts[c]+=1
            if counts[c]<=limit:
                cells[c].append({'cell':f'{get_column_letter(c)}{r}','text':string(sheet.literal(r,c,display=True))[:500]})
    # Entire nonempty row coverage, not a guessed continuation below a note.
    return {get_column_letter(c):{'nonempty_count':n,'cells':cells[c]} for c,n in counts.items()
            if n<=limit and n<len(sheet.rows) and any(len(x['text'])>=12 for x in cells[c])}


def source_index(catalog):
    """No keyword gate: index all sheets and actual document/table units."""
    sheets=[]
    for s in catalog.sheets.values():
        profile={**s.profile(1,compact=True),'sparse_columns':sparse_source_columns(s)}
        # Short tables can be classified from every actual physical row,
        # rather than extrapolating rules from the first few sample rows.
        if len(s.rows)<=24 and sum(len(string(v)) for row in s.rows for v in row)<=5000:
            profile['all_rows']=[{'row':i,'cells':{get_column_letter(j):string(s.literal(i,j,display=True))[:180]
                for j,v in enumerate(row,1) if v not in (None,'')}} for i,row in enumerate(s.rows,1)]
        sheets.append(profile)
    docs=defaultdict(lambda:{'text_units':[],'tables':[],'unreadable':[]})
    for k,v in catalog.blocks.items():
        if not k.startswith('R'):continue
        docs[v['file']]['text_units'].append({'id':k,'where':v['where'],'preview':v['text'][:180]})
    for k,t in catalog.tables.items():
        if not t.sheet_name:docs[t.file]['tables'].append({'id':k,'page':t.page,'title':t.title[:220],'headers':t.rows[:1],'rows':len(t.rows)})
    for item in catalog.unreadable_blocks:docs[item['file']]['unreadable'].append({k:v for k,v in item.items() if k!='digest'})
    for item in getattr(catalog,'decorative_blocks',[]):docs[item['file']].setdefault('decorative',[]).append({'where':item['where'],'reason':item['reason'],'repeat_count':item['repeat_count']})
    for path in catalog.paths:docs[path.name]
    # The source inventory decides which sheets carry data/rules. It needs
    # the directory of documents, not every paragraph; the next nodes will
    # inspect every actual source unit in small chunks.
    for doc in docs.values():
        entries=doc['text_units'];doc['text_unit_count']=len(entries);doc['text_units']=entries[:4]
    return {'sheets':sheets,'documents':dict(docs)}


def validate_source_inventory(catalog,proposal):
    contract(proposal,required=('sheets','unreadable'),label='来源覆盖清单',owner='scope_mapping')
    sheets=proposal['sheets']
    if not isinstance(sheets,dict) or set(sheets)!=set(catalog.sheets):
        have=set(sheets) if isinstance(sheets,dict) else set()
        raise ProgramError(f'来源清单必须覆盖每张真实表；漏 {sorted(set(catalog.sheets)-have)}；多 {sorted(have-set(catalog.sheets))}',owner='scope_mapping')
    for sid,s in sheets.items():
        contract(s,required=('kind','reason','rule_ranges'),optional=('rule_cells',),label=f'{sid} 材料作用',owner='scope_mapping')
        if s['kind'] not in SOURCE_KINDS or not isinstance(s['reason'],str) or not s['reason'].strip():raise ProgramError(f'{sid} 未明确数据/规则用途',owner='scope_mapping')
        ranges=s['rule_ranges']
        if not isinstance(ranges,list):raise ProgramError(f'{sid} 规则范围必须为物理行段',owner='scope_mapping')
        seen=set()
        for pair in ranges:
            if not isinstance(pair,list) or len(pair)!=2 or any(type(x) is not int for x in pair) or not 1<=pair[0]<=pair[1]<=len(catalog.sheets[sid].rows):raise ProgramError(f'{sid} 规则行范围无效 {pair!r}',owner='scope_mapping')
            for n in range(pair[0],pair[1]+1):
                if n in seen:raise ProgramError(f'{sid} 规则行范围重叠',owner='scope_mapping')
                seen.add(n)
        rule_cells=s.get('rule_cells',[]);sheet=catalog.sheets[sid]
        if not isinstance(rule_cells,list) or any(not isinstance(a,str) or not re.fullmatch(r'[A-Z]{1,3}[1-9]\d*',a) for a in rule_cells) or len(set(rule_cells))!=len(rule_cells):
            raise ProgramError(f'{sid} 规则单元格必须为不重复的实际坐标，如 L1',owner='scope_mapping')
        for a in rule_cells:
            m=re.fullmatch(r'([A-Z]+)(\d+)',a);r=int(m[2]);c=column_index_from_string(m[1])
            if r>len(sheet.rows) or c>len(sheet.rows[r-1]) or not string(sheet.rows[r-1][c-1]):
                raise ProgramError(f'{sid} {a} 实际为空或不存在；请从物理预览和 sparse_columns 的真实非空坐标选择规则',owner='scope_mapping',kind='coverage',location=f'{sheet.file}/{sheet.name}!{a}')
            if r in seen:raise ProgramError(f'{sid} 规则单元格 {a} 已包含于整行规则范围，不得重复',owner='scope_mapping')
        if s['kind']=='mixed' and not (ranges or rule_cells):raise ProgramError(f'{sid} 混合材料必须指出实际规则行或单元格',owner='scope_mapping')
        if s['kind'] not in ('mixed','rule','output') and (ranges or rule_cells):raise ProgramError(f'{sid} 数据表规则须按 mixed 分类',owner='scope_mapping')
        if s['kind']=='rule':
            # A full rule table is always covered in full. Whether a first
            # row is a structural header is decided at the clause level.
            # The reviewer cannot accidentally exclude a valid unseen rule.
            s['rule_ranges']=[];s['rule_cells']=[]
    # An image-only rule is not a readable source. Never infer a rate from a
    # page label. The financial answer can explicitly identify it as context or
    # supply a readable transcription; no automatic guessed disposition.
    if catalog.unreadable_blocks:
        expected={f'{v["file"]}/{v["where"]}' for v in catalog.unreadable_blocks}
        decisions=proposal['unreadable']
        if not isinstance(decisions,dict) or set(decisions)!=expected:raise ProgramError(f'仍有未覆盖的图像材料：{sorted(expected)}',owner='scope_mapping',kind='material')
        for key,value in decisions.items():
            authority=value.get('authority') if isinstance(value,dict) else None
            authorized=getattr(catalog,'authorized_image_exclusions',{}).get(authority,{})
            if not isinstance(value,dict) or value.get('kind')!='context' or not value.get('reason') or key not in authorized.get('sources',[]):
                raise ProgramError(f'{key} 缺少可读取规则，需提供原件文字/表格或真实确认其不参与核算',owner='scope_mapping',kind='material')
    elif proposal['unreadable'] not in ({},[]):raise ProgramError('图像清单包含不存在的材料',owner='scope_mapping')
    return proposal


def rule_units(catalog,inventory):
    """Enumerate actual rule units, including spreadsheet and parameter rows.

    Synthetic unit IDs denote physical source positions, not invented rules.
    A document's text clauses and its native parameter rows are kept separate.
    """
    validate_source_inventory(catalog,inventory)
    units={k:dict(v) for k,v in catalog.blocks.items() if k.startswith('R')}
    covered={(v['file'],v['where'],normalized(v['text'])) for v in units.values()}
    for sid,s in inventory['sheets'].items():
        if s['kind'] not in ('rule','mixed','output'):continue
        sheet=catalog.sheets[sid]
        ranges=s['rule_ranges'] or ([[1,len(sheet.rows)]] if s['kind']!='mixed' and not s.get('rule_cells') else [])
        for start,end in ranges:
            for row in range(start,end+1):
                raw=' | '.join(string(sheet.literal(row,col,display=True)) for col in range(1,len(sheet.rows[row-1])+1) if sheet.rows[row-1][col-1] is not None).strip()
                where=f'{sheet.name}!{row}'
                signature=(sheet.file,where,normalized(raw))
                if raw and signature not in covered:
                    uid=f'{sid}R{row}';units[uid]={'file':sheet.file,'where':where,'text':raw};covered.add(signature)
        for a in s.get('rule_cells',[]):
            m=re.fullmatch(r'([A-Z]+)(\d+)',a);row=int(m[2]);col=column_index_from_string(m[1])
            raw=string(sheet.literal(row,col,display=True));where=f'{sheet.name}!{a}'
            signature=(sheet.file,where,normalized(raw))
            if signature not in covered:
                uid=f'{sid}C{a}';units[uid]={'file':sheet.file,'where':where,'text':raw};covered.add(signature)
    for tid,t in catalog.tables.items():
        if t.sheet_name:continue  # spreadsheet source rows already addressable
        for row,cells in enumerate(t.rows,1):
            raw=' | '.join(map(string,cells)).strip()
            if raw:
                unit={'file':t.file,'where':t.location(row,1),'text':raw,'table':tid,'row':row,'title':t.title}
                # Header labels are preserved at the actual parameter cells,
                # but are not meant to become standalone business equations.
                if row==1 and len(t.rows)>1 and (structural_header(cells) or matrix_header(cells,t.rows[1:])):
                    unit['structural_header']=True
                units[f'{tid}R{row}']=unit
    # A real version deliberately replaces only the selected original clause.
    # Other clauses in the same cell/paragraph remain operative. The original
    # bytes and prior approval stay in historical evidence.
    revisions=[v['authority'] for v in units.values() if v.get('authority')]
    for revision in revisions:
        old=revision.get('original_quote');source_text=revision.get('original_unit_text')
        if not old or not source_text:continue  # a newly added rule, not a replacement
        for uid,unit in units.items():
            if unit.get('authority') or normalized(unit['text'])!=normalized(source_text):continue
            active,removed=_remove_quote(unit.get('active_text',unit['text']),old)
            if removed:
                unit['active_text']=active;unit.setdefault('superseded_by',[]).append(revision['id'])
                unit['superseded_entirely']=not re.sub(r'[\s|。；;,，:：()（）]+','',active)
    for uid,unit in units.items():catalog.blocks[uid]=unit
    return units


def unit_chunks(units,*,max_chars=4800,max_units=8):
    """Small deterministic chunks; no accumulated conversation history."""
    chunk={};size=0
    for uid,unit in units.items():
        cost=len(unit['text'])+len(unit.get('title',''))+100
        if chunk and (len(chunk)>=max_units or size+cost>max_chars):
            yield chunk;chunk={};size=0
        chunk[uid]=unit;size+=cost
    if chunk:yield chunk


def _has_arithmetic(text):
    """A footnote marker or a bare signed literal is not a binary expression."""
    return bool(re.search(r'[\w%)](?:[*/+\-])(?:[+\-])?[\w(]',text) or re.search(r'(?i)(?:ROUND|MIN|MAX|ABS|IF)\(',text))


def _non_arithmetic_declaration(segment):
    if '=' not in segment:return False
    lhs,rhs=segment.split('=',1);lhs=lhs.rsplit('|',1)[-1]
    # Equality of a category/code in a conditional is not assignment to a
    # monetary formula. Identifier-composition requirements are key bindings.
    if re.match(r'(?i)^(?:当|若|如果|假如|仅当|if|when)',lhs) and re.fullmatch(r'[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+',rhs):return True
    fields=rhs.rstrip(')）').split('+')
    if len(fields)>1 and all(re.fullmatch(r'[\w]+',x) for x in fields):
        if re.search(r'(?:编号|标识|主键|唯一键|组合键|ID)$',lhs,re.I):return True
        if re.search(r'每(?:个|笔|项)[^。；;]{1,30}一行',segment) and re.search(r'(?:项目|订单|合同|客户|对象)$',lhs):return True
    return False


def _ledger_identifier_tail(segment):
    """A compact product/composite ID among actual record values is not a formula.

    An explicit equality or a labeled short formula row still requires complete
    mathematical coverage. Do not infer arithmetic from punctuation in codes.
    """
    parts=segment.split('|')
    if len(parts)<4:return False
    tail=parts[-1]
    plausible=(any(re.fullmatch(r'[+-]?\d+(?:\.\d+)?',v) for v in parts[:-1])
            and bool(re.fullmatch(r'[A-Za-z0-9_().+*/\-]+',tail))
            and bool(re.search(r'[A-Za-z]',tail)) and bool(re.search(r'\d',tail))
            and not re.match(r'(?i)(?:ROUND|MIN|MAX|ABS|IF)\(',tail)
            and not any(re.search(r'(?i)公式|formula',v) for v in parts[:-1]))
    if not plausible:return False
    # Prove the compact key is a concatenation of actual preceding fields.
    # A multi-column table may also contain a genuine ASCII arithmetic
    # expression; do not suppress it simply because digits occur in symbols.
    return any(tail==''.join(parts[i:j]) for i in range(len(parts)-2) for j in range(i+2,len(parts)))


def explicit_formulas(text):
    """Recognize an explicit RHS, not a prose mention of percentage/units.

    Strict formula obligations are compiled below. Ambiguous prose remains an
    operative clause requiring an independent semantic review.
    """
    n=normalized(str(text).replace('\n','；'));result=[]
    ledger_tail=n.split('|')[-1] if _ledger_identifier_tail(n) else None
    # A native worksheet may separate the indicator label and formula by |.
    # Commas inside MIN/MAX/ROUND are argument separators. A comma AFTER
    # the balanced expression starts explanatory prose, not another operand.
    segments=[];start=0;depth=0
    for i,char in enumerate(n):
        if char in '([':depth+=1
        elif char in ')]':depth=max(0,depth-1)
        if depth==0 and char in '。；;，,':
            if char==',' and i and n[i-1].isdigit() and re.match(r'\d{3}(?!\d)',n[i+1:]):continue
            segments.append(n[start:i]);start=i+1
    segments.append(n[start:])
    for segment in segments:
        rhs=None
        if _non_arithmetic_declaration(segment):continue
        if '=' in segment:rhs=segment.split('=',1)[1]
        elif '|' in segment and not _ledger_identifier_tail(segment) and (ledger_tail is None or segment.split('|')[-1]!=ledger_tail):
            tail=segment.split('|')[-1]
            if _has_arithmetic(tail):rhs=tail
        arithmetic=rhs and _has_arithmetic(rhs)
        if arithmetic:
            # Numeric band labels are parameter tables, not formulas.
            if re.fullmatch(r'[\d%万亿元.,<>+=-]+',rhs):continue
            result.append(rhs.strip(',，:：'))
    return list(dict.fromkeys(result))


def explicit_formula(text):
    found=explicit_formulas(text)
    return found[0] if found else None


def _formula_lhs(quote,formula):
    if not formula:return ''
    text=normalized(quote);at=text.find(formula)
    if at<0:return ''
    prefix=text[:at].removesuffix('=')
    return re.split(r'[。；;，,|：:]',prefix)[-1]


def route_clause(clause,source,formula):
    """Business grain determines the stage; internal node spelling does not.

    Old recorded candidates with a phase are accepted only if their scope is
    consistent. New candidates declare level and operation; the program owns
    the internal phase assignment. Explicit person aggregation cannot be
    compiled as an order-level adjustment before total confirmation.
    """
    phase=clause.get('phase');level=clause.get('level');operation=clause.get('operation')
    authority_owner=(source.get('authority') or {}).get('owner')
    if authority_owner in PHASES:
        # A real financial revision already has an explicit responsible node.
        # The model extracts the actual obligation, it must not be asked to
        # rediscover or contradict the authoritative internal routing.
        phase=authority_owner
        level='person' if phase=='roster' else 'report' if phase=='export' else 'source' if phase in PHASES[:2] else 'primary'
        if operation is not None and operation not in OPERATIONS:raise ProgramError(f'财务确认条款实际动作枚举无效：{operation!r}；责任节点已确定为 {phase}',owner='scope_mapping',kind='contract')
        if operation is None:operation={'scope_mapping':'population','reference_mapping':'binding','project_policy':'calculation','coefficient_policy':'adjustment','hold_policy':'funding','roster':'calculation','export':'report'}[phase]
    elif level is not None or operation is not None:
        if level not in LEVELS or operation not in OPERATIONS:raise ProgramError(f'条款层级/动作枚举无效：level={level!r}（只能 {sorted(LEVELS)}），operation={operation!r}（只能 {sorted(OPERATIONS)}）；parameter 是动作，不是层级或条款kind',owner='scope_mapping',kind='contract',details={'field_values':{'level':level,'operation':operation},'allowed':{'level':sorted(LEVELS),'operation':sorted(OPERATIONS)}})
        if level=='report' or operation=='report':phase='export'
        elif level=='person' or operation=='assignment':phase='roster'
        elif level=='source':phase='scope_mapping' if operation=='population' else 'reference_mapping'
        else:phase={'population':'scope_mapping','binding':'reference_mapping','validation':'project_policy','calculation':'project_policy','adjustment':'coefficient_policy','parameter':'project_policy','funding':'hold_policy'}.get(operation,'project_policy')
    else:
        # Harmless old spelling is not a financial policy change.
        if phase=='validation' and clause['kind']=='validation':phase='reference_mapping'
        if phase not in PHASES:raise ProgramError(f'条款缺少有效责任阶段 {phase!r}',owner='scope_mapping',kind='coverage')
        level='person' if phase=='roster' else 'report' if phase=='export' else 'source' if phase in PHASES[:2] else 'primary'
        operation={'scope_mapping':'population','reference_mapping':'binding','project_policy':'calculation','coefficient_policy':'adjustment','hold_policy':'funding','roster':'calculation','export':'report'}[phase]
    quote=clause['quote'];kind=clause['kind'];lhs=_formula_lhs(quote,formula)
    # "Population" is not synonymous with the sale/project master: a
    # source may explicitly limit the attribution roster's own rows. Such
    # a selection is a recipient-binding obligation, not a filter on sales.
    if authority_owner not in PHASES and phase=='scope_mapping' and operation=='population':
        if re.search(r'(?:分配|发放|归属|分成)(?:人员)?(?:名单|名册)|(?:人员|员工|个人)(?:名单|名册)',quote) and re.search(r'只取|仅取|只使用|仅使用|筛选|限定|按.+匹配|对应.+(?:行|记录)',quote):
            phase='roster';level='person';operation='binding'
    # Native parameter tables are evidence, not business ledgers. A numeric
    # interval/rate row cannot be satisfied by merely mapping a data source;
    # its actual project/person calculation grain must be selected explicitly.
    if source.get('table') and kind=='table' and level=='source' and operation=='parameter' and not explicit_binding_assertions(quote):
        first=normalized(quote.split('|',1)[0])
        rest=quote.split('|')[1:]
        if re.search(r'\d',first) and re.search(r'以上|以下|含|[<>≤≥\-]',first) and any(re.search(r'\d+(?:\.\d+)?\s*[%％]',x) for x in rest):
            raise ProgramError('制度区间及费率参数不能仅归source数据绑定层。请按真实制度适用对象选择level=primary或person，再以operation=parameter/adjustment等绑定实际运算，不得遗漏该档位。',owner='scope_mapping',kind='coverage',details={'actual_rate_row':quote,'allowed_level':['primary','person']})
    # Use only explicit layer language in original evidence. Do not select a
    # grain from a generic word like “员工” in a foreign-key description.
    person_scope=r'按(?:员工|人员|个人)(?:唯一)?(?:编号|ID|身份)?(?:进行)?(?:汇总|合计|发放|分配)|(?:该员工|每名员工|每位员工|每个员工|每人|人员层|个人层|员工层)[^。；;]{0,80}(?:汇总|合计|应得|实发|预发|计奖|奖金|提成)|(?:人员|个人|员工)(?:表)?(?:计奖系数|应得提成|应得金额|实发|主表全员)|(?:零奖金|无订单|无项目)人员'
    explicit_primary=bool(lhs and re.match(r'^(?:(?:每笔|单笔|每个)?(?:订单|项目|合同|产品|交易))',lhs) and not re.search(r'合计|汇总',lhs))
    personal=bool(re.search(person_scope,quote) or formula and re.search(person_scope,source.get('active_text',source['text'])))
    if personal and not explicit_primary and kind not in ('table','validation','output') and phase!='roster' and (formula or re.search(r'汇总|合计|分配|全部输出|发放|应得|计奖',quote)):
        raise ProgramError(f'原文义务明确在实际人员归属/汇总层，而非项目或数据绑定阶段：{quote[:230]}；请选择 level=person，再按实际动作编制',owner='scope_mapping',kind='coverage')
    if formula and phase in ('scope_mapping','reference_mapping'):
        raise ProgramError(f'明示公式 {formula!r} 必须进入实际项目或人员计算，不能仅绑定数据源',owner='scope_mapping',kind='coverage')
    # A numeric aggregate of primary objects is a computation, not a mere
    # declaration of the physical source relation. Keep source/relationship
    # policies separate from actual calculated metrics.
    if phase in PHASES[:2] and kind=='policy' and re.search(r'每笔|每个项目|每个订单|每项',quote) and re.search(r'金额之和|求和|计算.*(?:合计|净额)|分别汇总',quote) and not re.search(r'关联键|按.+关联|唯一键',quote):
        raise ProgramError(f'原文要求实际逐对象聚合：{quote[:230]}；请选择 level=primary, operation=calculation，而非仅登记绑定',owner='scope_mapping',kind='coverage')
    clause['phase']=phase;clause['level']=level;clause['operation']=operation
    return phase


def primary_population_invariants(obligations):
    """Source-only population closure that the deterministic engine enforces.

    A stated exclusion of reference-only objects is satisfied by the engine
    deriving groups solely from the chosen real primary dataset. It is not a
    requirement to bind a reference/roster table as a second primary source.
    """
    covered=[]
    for rid,rule in obligations.items():
        if rule.get('phase')!='scope_mapping' or rule.get('operation')!='population':continue
        text=normalized(rule['quote'])
        reference=re.search(r'(?:参考|辅助|补充)(?:数据|表|资料)',text)
        main_absent=re.search(r'(?:销售单|销售明细|订单|主集合|主数据|主对象)(?:里|中|内)?(?:并)?(?:没有|不存在|未包含|无)',text)
        excluded=re.search(r'不(?:参与|纳入)(?:核算|计算|统计)|不(?:影响|新增)(?:结果|项目|对象)',text)
        if reference and main_absent and excluded:covered.append(rid)
    return covered


def validate_clause_chunk(chunk,proposal):
    contract(proposal,required=('units',),label='规则覆盖抽取',owner='scope_mapping')
    actual=proposal['units']
    if not isinstance(actual,dict) or set(actual)!=set(chunk):
        have=set(actual) if isinstance(actual,dict) else set();missing=sorted(set(chunk)-have)
        raise ProgramError(f'规则覆盖未逐原文处理：漏 {missing}；多 {sorted(have-set(chunk))}',owner='scope_mapping',kind='coverage',rules=missing,
                           details={'missing_units':missing,'extra_units':sorted(have-set(chunk))})
    obligations={};decisions={}
    for uid,source in chunk.items():
        try:
            unit_obligations,decision=_validate_unit(uid,source,actual[uid])
        except ProgramError as exc:
            if not exc.location:exc.location=f'{source["file"]} / {source["where"]}'
            if not exc.rules:exc.rules=[uid]
            exc.details={**exc.details,'source_unit':uid,'actual_source':source.get('active_text',source['text'])}
            raise
        obligations.update(unit_obligations);decisions[uid]=decision
    return obligations,decisions


def merge_rule_review(draft,replacement,unit_ids):
    """Apply a reviewer correction at the real source-unit boundary.

    The caller still validates the complete merged contract and source
    coverage. A complete replacement can also remove an invalid extra unit.
    """
    if (not isinstance(draft,dict) or not isinstance(draft.get('units'),dict) or not isinstance(replacement,dict)
            or set(replacement)!={'units'} or not isinstance(replacement.get('units'),dict) or not replacement['units']):return deepcopy(replacement)
    expected=set(unit_ids);changed=set(replacement['units'])
    if not changed<=expected or changed==expected:return deepcopy(replacement)
    return {**deepcopy(draft),'units':{**deepcopy(draft['units']),**deepcopy(replacement['units'])}}


def source_lines(source):
    return {str(i):line for i,line in enumerate(source.get('active_text',source['text']).splitlines(),1) if line.strip()}


def _source_quote(uid,source,clause):
    raw=source.get('active_text',source['text']);quote=clause.get('quote')
    line=clause.get('source_line')
    if line is not None:
        ids=line if isinstance(line,list) else [line];lines=source_lines(source)
        if not ids or any(type(i) is not int or str(i) not in lines for i in ids) or any(b!=a+1 for a,b in zip(ids,ids[1:])):
            raise ProgramError(f'{uid} source_line 必须是当前原文有效行号或连续行号列表，实际 {line!r}',owner='scope_mapping',kind='coverage',details={'source_lines':lines})
        raw='\n'.join(lines[str(i)] for i in ids)
        if quote is None:quote=raw
    if not isinstance(quote,str) or not normalized(quote) or normalized(quote) not in normalized(raw):
        raise ProgramError(f'{uid} 引文并非当前原件实际连续文字：收到 {str(quote)[:300]!r}。不要转写繁简字或改标点；可直接提交 source_line 行号代替 quote，程序读取原文。',owner='scope_mapping',kind='coverage',details={'received_quote':quote,'source_lines':source_lines(source),'source_line':line})
    if quote in raw:clause['quote']=quote;return
    # Freeze the actual original substring, not a typographically normalized
    # model transcription. A line citation cannot override a conflicting quote.
    chars=[];positions=[]
    for i,c in enumerate(raw):
        for x in normalized(c):chars.append(x);positions.append(i)
    target=normalized(quote);at=''.join(chars).find(target)
    if at<0:raise ProgramError(f'{uid} 无法定位引文原字符区间',owner='scope_mapping',kind='coverage')
    clause['quote']=raw[positions[at]:positions[at+len(target)-1]+1]


def _validate_unit(uid,source,selected):
    contract(selected,required=('disposition','reason','clauses'),label=f'{uid} 规则覆盖',owner='scope_mapping')
    disposition=selected['disposition'];clauses=selected['clauses']
    if disposition not in ('operative','context','output') or not isinstance(selected['reason'],str) or not selected['reason'].strip() or not isinstance(clauses,list):
        raise ProgramError(f'{uid} 缺来源性质说明',owner='scope_mapping',kind='coverage')
    if source.get('structural_header') and not source.get('authority'):
        selected['disposition']='context';selected['reason']='原生表结构标题；保留实际列名和单元格，不作为单独运算义务';selected['clauses']=[]
        disposition='context';clauses=[]
    if disposition=='context' and clauses or disposition!='context' and not clauses:
        raise ProgramError(f'{uid} 规则性质与条款清单矛盾',owner='scope_mapping',kind='coverage')
    if source.get('authority') and disposition!='operative':
        raise ProgramError(f'{uid} 真实财务授权规则不能作为上下文忽略',owner='scope_mapping',kind='coverage')
    if source.get('superseded_entirely') and disposition!='context':
        raise ProgramError(f'{uid} 已被真实财务版本 {source["superseded_by"]} 替代，不得继续把旧口径编入计算',owner='scope_mapping',kind='coverage')
    decision={'disposition':disposition,'reason':selected['reason'],'source':source}
    obligations={};found_formulas=[]
    assertions=[] if source.get('superseded_entirely') else explicit_binding_assertions(source.get('active_text',source['text']))
    covered_assertions=set()
    for i,c in enumerate(clauses,1):
        contract(c,required=('meaning','kind'),optional=('quote','source_line','phase','formula','level','operation'),label=f'{uid} 条款',owner='scope_mapping')
        if c['kind'] not in KINDS:raise ProgramError(f'{uid} 条款kind={c["kind"]!r} 无效，只能 {sorted(KINDS)}；制度参数的kind为table或实际policy，parameter只属于operation',owner='scope_mapping',kind='contract',details={'received_kind':c['kind'],'allowed_kind':sorted(KINDS)})
        if not c['meaning']:raise ProgramError(f'{uid} 条款缺少实际业务含义',owner='scope_mapping',kind='coverage')
        _source_quote(uid,source,c)
        formulas=explicit_formulas(c['quote']);formula=formulas[0] if len(formulas)==1 else None
        if len(formulas)>1:raise ProgramError(f'{uid} 一个条款含多个不同算式，请按各公式分别登记完整原文及目标',owner='scope_mapping',kind='coverage')
        if not formula and c.get('formula'):
            proposed=normalized(c['formula'])
            quote_normal=normalized(c['quote'])
            declaration=any(proposed in s and _non_arithmetic_declaration(s) for s in [quote_normal,*re.split(r'[。；;，,]',quote_normal)])
            actual_normal=normalized(source.get('active_text',source['text']))
            id_tail=any(_ledger_identifier_tail(s) and proposed in s.split('|')[-1]
                        for s in [actual_normal,*re.split(r'[。；;，,]',actual_normal)])
            if proposed in quote_normal and _has_arithmetic(proposed) and not declaration and not id_tail:
                extracted=explicit_formulas(proposed);formula=extracted[0] if extracted else proposed.lstrip('=')
        if formula:
            # Literal original expression is parsed by the program, never invented by the model.
            c['kind']='formula';c['formula']=formula;found_formulas.append(formula)
        else:
            c.pop('formula',None)
            if c['kind']=='formula':c['kind']='policy'
        phase=route_clause(c,source,formula)
        if source.get('authority',{}).get('owner') in PHASES and phase!=source['authority']['owner']:
            raise ProgramError(f'{uid} 真实财务修订的责任节点不能被模型变更',owner='scope_mapping',kind='coverage')
        if disposition=='output' and phase!='export':raise ProgramError(f'{uid} 输出材料不应当成为计算参数',owner='scope_mapping',kind='coverage')
        rid=f'{uid}:{i}'
        obligations[rid]={**c,'id':rid,'unit':uid,'source':{'file':source['file'],'where':source['where']},'formula':formula}
        bound=[(j,a) for j,a in enumerate(assertions) if a['source'] in normalized(c['quote'])]
        if bound:
            if phase not in PHASES[:2]:raise ProgramError(f'{uid} 原文物理绑定不能放到计算或报告层，请登记实际表头、数据范围及单位',owner='scope_mapping',kind='coverage')
            covered_assertions.update(j for j,a in bound)
            obligations[rid]['binding_assertions']=[a for _,a in bound]
    missed=[a['source'] for j,a in enumerate(assertions) if j not in covered_assertions]
    if missed:raise ProgramError(f'{uid} 明确物理绑定尚未作为实际规则覆盖：{missed}；不能仅标为材料背景或遗漏部分要求',owner='scope_mapping',kind='coverage')
    # An original equation cannot be avoided by quoting a neighboring sentence.
    actual_formulas=explicit_formulas(source.get('active_text',source['text']))
    if disposition=='context' and actual_formulas and not source.get('superseded_entirely') and not re.search(r'示例|例如|举例|仅供|废止|原口径|旧规则',source.get('active_text',source['text'])):
        raise ProgramError(f'{uid} 原件有明确算式 {actual_formulas}，不能仅标记为上下文',owner='scope_mapping',kind='coverage')
    if disposition in ('operative','output'):
        missing=set(actual_formulas)-set(found_formulas)
        if missing:raise ProgramError(f'{uid} 未覆盖原件完整算式 {sorted(missing)!r}',owner='scope_mapping',kind='coverage')
    return obligations,decision


def _signature(expr):
    if isinstance(expr,(int,float,D)) and not isinstance(expr,bool):return ('num',str(to_decimal(expr).normalize()))
    if isinstance(expr,dict) and set(expr)=={'num'}:return ('num',str(to_decimal(expr['num']).normalize()))
    if isinstance(expr,dict) and 'op' in expr:
        op=expr['op'];args=[_signature(x) for x in expr['args']]
        if op in ('add','mul'):
            flat=[]
            for a in args:
                if isinstance(a,tuple) and a and a[0]==op:flat.extend(a[1:])
                else:flat.append(a)
            args=sorted(flat,key=repr)
        return (op,*args)
    if isinstance(expr,dict) and len(expr)==1:
        k,v=next(iter(expr.items()))
        if k in ('ref','param','key'):return (k,v)
    if isinstance(expr,(str,bool)) or expr is None:return ('literal',expr)
    raise ProgramError(f'明示公式须对应计算表达式，不可用原文引用替代运算：{expr!r}',kind='coverage')


def _same_literal_tree(actual,expected,parameters):
    """An explicitly sourced parameter can represent an original literal.

    Never replace named symbols by their coincidentally equal numeric values.
    Only a numeric leaf actually present in the original expression may be
    satisfied by an independently read parameter of exactly that value.
    """
    if _signature(actual)==_signature(expected):return True
    sig=_signature(expected)
    if sig[0]=='num' and isinstance(actual,dict) and set(actual)=={'param'}:
        value=parameters.get(actual['param'])
        if isinstance(value,(int,float,D)) and not isinstance(value,bool):return to_decimal(value)==to_decimal(sig[1])
        return False
    if not (isinstance(actual,dict) and isinstance(expected,dict) and 'op' in actual and actual.get('op')==expected.get('op')):return False
    op=actual['op']
    def flatten(expr):
        out=[]
        for a in expr['args']:
            if op in ('add','mul') and isinstance(a,dict) and a.get('op')==op:out.extend(flatten(a))
            else:out.append(a)
        return out
    aa,bb=flatten(actual),flatten(expected)
    if len(aa)!=len(bb):return False
    if op not in ('add','mul'):return all(_same_literal_tree(x,y,parameters) for x,y in zip(aa,bb))
    # Commutative source expressions retain their multiset of actual operands.
    remaining=list(aa)
    for item in bb:
        index=next((i for i,x in enumerate(remaining) if _same_literal_tree(x,item,parameters)),None)
        if index is None:return False
        remaining.pop(index)
    return True


def parse_literal_formula(formula,symbols):
    """Compile every source symbol in a literal arithmetic/ROUND expression.

    Symbols must bind to real named metrics/parameters, not invented constants
    or arbitrary replacement expressions. This deliberately rejects a fifth
    factor unless the new indicator is actually in the program.
    """
    if not isinstance(symbols,dict):raise ProgramError('公式符号绑定需要对象',kind='coverage')
    mapped={normalized(k):v for k,v in symbols.items()}
    if len(mapped)!=len(symbols) or any(not k for k in mapped):raise ProgramError('公式符号重复/为空',kind='coverage')
    signature_map={}
    for k,v in mapped.items():
        if not isinstance(v,dict) or len(v)!=1 or next(iter(v)) not in ('ref','param','key') or not isinstance(next(iter(v.values())),str):
            raise ProgramError(f'公式原文符号 {k!r} 必须指向真实具名指标/参数',kind='coverage')
        signature_map[k]=_signature(v)
    if len(set(signature_map.values()))!=len(signature_map):raise ProgramError('原文不同公式符号被合并成同一指标，需分别绑定',kind='coverage')
    text=normalized(formula);tokens=[];i=0;used=set()
    keys=sorted(mapped,key=len,reverse=True)
    while i<len(text):
        symbol=next((k for k in keys if text.startswith(k,i)),None)
        if symbol is not None:tokens.append(('value',mapped[symbol]));used.add(symbol);i+=len(symbol);continue
        fn=re.match(r'(ROUND|MIN|MAX|ABS|IF)\(',text[i:],re.I)
        if fn:tokens.append(('fn',fn[1].lower()));i+=len(fn[1]);continue
        num=re.match(r'(?:\d+(?:\.\d+)?|\.\d+)(?:%|万元|亿元|万|亿|元)?',text[i:])
        if num:tokens.append(('value',{'num':str(to_decimal(num[0].removesuffix('元')))}));i+=len(num[0]);continue
        op=next((x for x in ('>=','<=','==','!=','<>','+','-','*','/','>','<','=','(',')',',') if text.startswith(x,i)),None)
        if op is None:raise ProgramError(f'原文公式仍有未绑定符号：{text[i:i+60]!r}',kind='coverage')
        tokens.append((op,op));i+=len(op)
    if set(mapped)-used:raise ProgramError(f'公式绑定了原件中不存在的符号 {sorted(set(mapped)-used)}',kind='coverage')
    cursor=0;precedence={'=':1,'==':1,'!=':1,'<>':1,'>':1,'>=':1,'<':1,'<=':1,'+':2,'-':2,'*':3,'/':3}
    op_names={'+':'add','-':'sub','*':'mul','/':'div','=':'eq','==':'eq','!=':'ne','<>':'ne','>':'gt','>=':'ge','<':'lt','<=':'le'}
    def take(kind=None):
        nonlocal cursor
        if cursor>=len(tokens):raise ProgramError('原文公式缺少操作数/括号',kind='coverage')
        tok=tokens[cursor];cursor+=1
        if kind is not None and tok[0]!=kind:raise ProgramError(f'公式期待 {kind}，实际 {tok[0]}',kind='coverage')
        return tok
    def parse(minimum=1):
        kind,value=take()
        if kind=='value':left=value
        elif kind=='-':left={'op':'neg','args':[parse(4)]}
        elif kind=='+':left=parse(4)
        elif kind=='(':left=parse();take(')')
        elif kind=='fn':
            take('(');args=[parse()]
            while cursor<len(tokens) and tokens[cursor][0]==',':take(',');args.append(parse())
            take(')');left={'op':value,'args':args}
        else:raise ProgramError(f'原文公式不支持符号 {kind}',kind='coverage')
        while cursor<len(tokens) and precedence.get(tokens[cursor][0],0)>=minimum:
            op,_=take();right=parse(precedence[op]+1);left={'op':op_names[op],'args':[left,right]}
        return left
    expr=parse()
    if cursor!=len(tokens):raise ProgramError('原文算式没有完整解析',kind='coverage')
    return expr


def _walk(value,path=()):
    if isinstance(value,dict):
        yield path,value
        for k,v in value.items():
            if k!='rules':yield from _walk(v,path+(k,))
    elif isinstance(value,list):
        for i,v in enumerate(value):yield from _walk(v,path+(i,))


def validate_numeric_literals(expr,rule_ids,obligations,*,owner):
    """Non-structural numeric literals must be present in actual rule quotes.

    Business rate matrices should use parameter coordinates; literals in an
    explicit arithmetic expression (ROUND digits, constants, thresholds) must
    match a real token in its associated original rule, not a guessed value.
    """
    allowed={D(-1),D(0),D(1)}
    for rid in rule_ids:
        if rid not in obligations:continue
        text=normalized(obligations[rid]['quote'])
        for token in re.findall(r'(?<![\d.])[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:%|万|亿)?(?![\d.])',text):
            try:
                v=to_decimal(token);allowed.add(v);allowed.add(abs(v))
            except ValueError:pass
    def walk(v):
        raw=None
        if isinstance(v,(int,float,D)) and not isinstance(v,bool):raw=v
        elif isinstance(v,dict) and set(v)=={'num'}:raw=v['num']
        if raw is not None:
            if to_decimal(raw) not in allowed:raise ProgramError(f'计算表达式含原文或参数表不能支持的裸数 {raw!r}；请引用真实参数坐标或对应制度数字',owner=owner,kind='coverage',rules=rule_ids)
        elif isinstance(v,dict):
            for k,x in v.items():
                if k not in ('ref','key','field','param','period','schedule','dataset','aggregate','output','empty','nulls','duplicates'):walk(x)
        elif isinstance(v,list):
            for x in v:walk(x)
    walk(expr)


def verify_parameter_usage(parameters,schedules,expressions,obligations,*,owner):
    """A parameter receipt is not coverage if the parameter is never used."""
    used_params=set();used_schedules=set()
    for _,expr in _walk(expressions):
        if set(expr)=={'param'}:used_params.add(expr['param'])
        if set(expr)=={'tier'} and isinstance(expr['tier'],dict):used_schedules.add(expr['tier'].get('schedule'))
    for kind,specs,used in [('参数',parameters,used_params),('阶梯',schedules,used_schedules)]:
        for name,spec in specs.items():
            rs=spec.get('rules',[])
            if not rs or any(r not in obligations for r in rs):raise ProgramError(f'{kind} {name} 缺少实际有效规则来源',owner=owner,kind='coverage',metric=name)
            if name not in used:raise ProgramError(f'{kind} {name} 虽已读取但没有进入实际计算表达式，不能仅靠来源引用通过',owner=obligations[rs[0]]['phase'],kind='coverage',metric=name,rules=rs)


def coverage(obligations,components,*,phases,formula_bindings=None,metrics=None,parameters=None,
             keys=(),roles=None,validate_literals=True,required_ids=None,financial_metrics=None,person_metrics=None,person_keys=()):
    """Every operative clause must attach to a real component in its phase.

    ``components`` contains only compiled datasets, source parameters, metrics,
    checks, funding, roster or output contracts, never the model's explanatory
    summary. Literal expressions get a separate structural consistency check.
    """
    phases=set(phases);wanted={rid for rid,c in obligations.items() if c['phase'] in phases}
    if required_ids is not None:wanted&=set(required_ids)
    links=defaultdict(list)
    for phase,component in components.items():
        for path,item in _walk(component):
            if 'rules' not in item:continue
            rs=names(item['rules'],f'{phase}/{path} 规则引用',phase,nonempty=False)
            for rid in rs:
                if rid not in obligations:raise ProgramError(f'{phase}/{path} 引用不存在的实际条款 {rid!r}',owner=phase,kind='coverage')
                # A physical dataset, field constraint or relationship is a
                # single executable binding shared by the two source stages.
                # Its real source-rule receipt remains valid whichever of
                # those stages created it; do not require a duplicate import
                # or a redundant synthetic check. Calculations/roster/report
                # still have separate responsibility and are not transferable.
                responsible=obligations[rid]['phase']
                source_binding=phase in PHASES[:2] and responsible in PHASES[:2] and path and path[0]=='datasets'
                if responsible==phase or source_binding:links[rid].append({'phase':phase,'path':list(path)})
    missing=wanted-set(links)
    if missing:
        rid=sorted(missing)[0];clause=obligations[rid]
        raise ProgramError(f'有效规则尚未进入任何执行组件：{rid} {clause["quote"]!r}；共 {len(missing)} 条',owner=clause['phase'],kind='coverage',rules=[rid],details={'missing':sorted(missing)})
    formula_bindings=formula_bindings or {};metrics=metrics or {};parameters=parameters or {};roles=roles or {};person_metrics=person_metrics or {}
    if validate_literals:
        for rid in sorted(wanted):
            clause=obligations[rid];literal=clause.get('formula')
            if not literal:continue
            b=formula_bindings.get(rid)
            if b is None:raise ProgramError(f'{rid} 明示原文公式 {literal!r} 尚未绑定到实际表达式',owner=clause['phase'],kind='coverage',rules=[rid])
            try:
                contract(b,required=('target','symbols'),optional=('path',),label=f'{rid} 公式绑定',owner=clause['phase'])
                target=b['target']
                if target in metrics:
                    if rid not in metrics[target]['rules'] and clause['phase']!='export':raise ProgramError('公式目标没有对应原文条款')
                    if financial_metrics is not None and clause['phase']=='hold_policy' and target not in financial_metrics:
                        raise ProgramError('总额/暂扣公式没有连接到实际资金去向，不能仅计算一个未使用的公式')
                    actual=metrics[target]['expr']
                    symbol_metrics=metrics;symbol_keys=keys
                elif isinstance(target,str) and target.startswith('person:') and target[7:] in person_metrics:
                    m=person_metrics[target[7:]]
                    if rid not in m['rules'] and clause['phase']!='export':raise ProgramError('人员层公式没有进入实际人员指标')
                    actual=m['expr'];symbol_metrics=person_metrics;symbol_keys=person_keys
                elif isinstance(target,str) and target.startswith('role:') and target[5:] in roles:
                    role=roles[target[5:]]
                    if rid not in role['rules']:raise ProgramError('角色公式没有对应原文条款')
                    actual=role['rate'];symbol_metrics=metrics;symbol_keys=keys
                elif target=='settlement_contribution' and clause['phase']=='roster':
                    # This is the actual fixed operator of the allocation
                    # engine, not a promise to execute a new formula.
                    actual={'op':'mul','args':[{'ref':'@alloc'},{'ref':'@role_rate'},{'ref':'@share'}]};symbol_metrics=metrics;symbol_keys=keys
                else:raise ProgramError(f'原文公式目标 {target!r} 不存在')
                for part in b.get('path',[]):actual=actual[part]
                for sym,expr in b['symbols'].items():
                    if not isinstance(expr,dict) or len(expr)!=1:raise ProgramError(f'{sym} 未绑定单一指标')
                    k,v=next(iter(expr.items()))
                    permitted={'ref':set(symbol_metrics)|({'@alloc','@role_rate','@share'} if target=='settlement_contribution' else set()),'param':set(parameters),'key':set(symbol_keys)}
                    if k not in permitted or v not in permitted[k]:raise ProgramError(f'{sym} 引用了不存在的 {k} {v}')
                expected=parse_literal_formula(literal,b['symbols'])
                if not _same_literal_tree(actual,expected,parameters):raise ProgramError(f'原文完整算式与实际计算树不同：原文 {literal!r} -> {expected!r}，执行 {actual!r}')
            except (ProgramError,KeyError,TypeError,IndexError,ValueError) as exc:
                raise ProgramError(f'{rid} 公式覆盖失败：{exc}',owner=clause['phase'],kind='coverage',rules=[rid]) from exc
    return {rid:{'clause':obligations[rid],'components':links[rid]} for rid in sorted(wanted)}
