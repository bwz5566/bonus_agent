"""Compile source coordinates and execute the fixed project/person chain.

No model-generated expressions are executed. Business changes within the
supported operations are data/contract changes, not source-code changes.
Unsupported or ambiguous choices are reported at the owning node.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from decimal import Decimal as D
from fractions import Fraction
from typing import Any

from bonus_agent.commission_sources import ChainError, month, number, pivot_label, population, project_key, string
from bonus_agent.commission_history import check_history_coverage
from bonus_core.data.table import to_date
from bonus_core.engine.commission import (ZERO, ONE, bracket, coefficient, rounded, tier_amount, tier_check,
                                            project_money, close_money, settle_project)
from bonus_core.engine.intervals import parse_interval, ordered_intervals


KEY = ('month','code','customer','product')
SCOPE_FIELDS = {'sales':(*KEY,'region','amount','currency','fx','series'),
                'launch':(*KEY,'first_date'),'gross':(*KEY,'margin'),'unpaid':(*KEY,'status')}
REF_FIELDS = {'history':('month','customer','amount'),'region_budget':('region','amount'),
              'customer_budget':('customer','amount'),'stock':('customer','amount'),'overdue':('customer','amount')}
ROLES = ('销售负责人','销售执行人','商务负责人','客户经理','NPD')
PENDING_NPD = 'NPD未分配'


def choose(value, options, label):
    if value not in options:
        raise ChainError(f'{label}={value!r} 不在支持的运算类型 {list(options)} 中；需明确规则或扩展此节点')
    return value


def validated_maps(catalog, result, fields):
    # Names are fixed semantic slots. Columns, files and header rows are not.
    out={}
    for name, required in fields.items():
        spec=result.get(name)
        if isinstance(spec,dict):
            # The real Flash reply used explicit optional fields adjacent to
            # columns instead of inside it. Normalize this harmless shape
            # difference before physical coordinate validation, not another LLM
            # round. Conflicting values remain errors.
            for field in ('exclude','pay_status','month'):
                if field in spec and isinstance(spec[field],str):
                    cols=spec.setdefault('columns',{})
                    if field in cols and cols[field]!=spec[field]:raise ChainError(f'{name}.{field} 列声明互相矛盾')
                    cols[field]=spec.pop(field)
        out[name]=catalog.sheet(spec).validate(spec,required)
    return out


def validate_scope_coverage(catalog,result,period):
    mapped=validated_maps(catalog,result,SCOPE_FIELDS)
    def keys(name):
        values=set()
        for row,where,_ in catalog.sheet(mapped[name]).records(mapped[name],SCOPE_FIELDS[name]):
            if pivot_label(row.get('month')) or not row.get('month'):continue
            if month(row['month'],where)!=period:continue
            # A mapping diagnostic, not the authoritative calculation filter:
            # compare the nonzero rows before choosing the formal policy.
            if name=='sales' and number(row['amount'],where,blank=True) in (None,ZERO):continue
            values.add(project_key(row,where))
        return values
    sale,launch=keys('sales'),keys('launch')
    if not sale:raise ChainError('销售绑定没有当期非零物理行')
    if sale-launch:raise ChainError(f'销售四键与首发日期覆盖不一致：{len(sale&launch)}/{len(sale)}；未匹配示例 {sorted(sale-launch)[:4]}。先核对同名客户列、客户编码与产品列，不能用另一参考表建项目。')
    return mapped


def validate_reference_mapping(catalog,result,period,*,scope=None,allow_current_only=False,source_key=''):
    mapped=validated_maps(catalog,result,REF_FIELDS)
    for name in ('region_budget','customer_budget'):
        spec=mapped[name];sheet=catalog.sheet(spec)
        from openpyxl.utils import column_index_from_string
        header=sheet.rows[spec.get('header_row',1)-1][column_index_from_string(spec['columns']['amount'])-1]
        hit=re.search(r'(?<!\d)(\d{1,2})月',str(header))
        if hit and int(hit[1])!=int(period[4:]):
            raise ChainError(f'{name} 绑定到 {header!r}，核算月为 {period}，请核对预算列，不能用其他月份')
    check_history_coverage(catalog,mapped['history'],period,scope=scope,allow_current_only=allow_current_only,source_key=source_key)
    return mapped


def _numbers(text):
    return [D(x) for x in re.findall(r'\d+(?:\.\d+)?',str(text).replace(',',''))]


def _rate(text, *, percent=False, where=''):
    s=string(text).replace('％','%')
    if not re.fullmatch(r'[+-]?\d+(?:\.\d+)?\s*%?',s):
        raise ChainError(f'{where}：比例/系数单元格不是单一数字 {text!r}')
    n=D(s.rstrip('%').strip())
    return n/D(100) if percent or s.endswith('%') else n


def _business_family(text):
    """Recognise explicit product-family labels in current source headings."""
    s=str(text).upper()
    if 'BIU' in s:return 'BIU'
    if 'ODM' in s:return 'ODM'
    if re.search(r'\bPI\b',s) or 'PI系数' in s:return 'PI'
    return ''


def _steps(bands, *, percent, includes, where):
    # Do not manufacture a contiguous schedule from lower bounds alone.
    # Read both literal endpoints/units before translating into the fast table.
    try:
        parsed=[parse_interval(label,percent=percent) for label,_,_ in bands]
        order,bounds=ordered_intervals(parsed,includes)
    except ValueError as exc:
        raise ChainError(f'{where}：{exc}') from exc
    rows=[(lo,bands[i][1],bands[i][0],bands[i][2]) for lo,i in zip(bounds,order)]
    return [(x,v,inc) for (x,v,_,_),inc in zip(rows,includes)], [dict(lower=x,rate=v,inclusive=inc,label=label,source=src) for (x,v,label,src),inc in zip(rows,includes)]


def compile_project_policy(catalog, proposal, regions, allowed):
    p=proposal
    for key in ('filter','currency','age','tier','routes','tier_tables','overrides'):
        if key not in p:raise ChainError(f'项目规则缺少 {key}')
    if type(p['filter'].get('exclude_zero')) is not bool or not isinstance(p['filter'].get('exclude_values'),list):
        raise ChainError('filter 必须明确是否排除零销售及排除标记列表')
    offset=number(p['currency'].get('offset'),'汇率调整额')
    age=p['age'].get('months')
    if type(age) is not int or not 0<=age<=1200:raise ChainError('age.months 不是有效月数')
    choose(p['age'].get('old_if'),('gt','ge'),'新老分界')
    choose(p['tier'].get('method'),('progressive','whole'),'阶梯算法')
    choose(p['tier'].get('negative'),('signed','zero'),'负基数')
    choose(p['tier'].get('endpoint'),('lower','upper'),'未明示端点归属')
    if not set(regions)<=set(p['routes']):raise ChainError(f'地区路线必须覆盖销售真实区域；缺少 {sorted(set(regions)-set(p["routes"]))}')
    ev=catalog.evidence(p,('filter','currency','age','tier','negative','endpoint','routing'),allowed)
    for sid in allowed:
        if not sid.startswith('R'):continue  # scoped revision records are not source-wide exclusion rules
        text=catalog.blocks[sid]['text']
        # An explicit source-level exclusion is not interchangeable with a
        # coincidentally equivalent zero-sale filter in this month.
        mark=re.search(r'判断为[“\"]([^”\"]+)[”\"]',text)
        if mark and ('不参与核算' in text or '不计算' in text) and mark[1] not in p['filter']['exclude_values']:
            raise ChainError(f'{sid} 原文明确排除判断标记 {mark[1]!r}；filter.exclude_values 不能遗漏')
    if age not in _numbers(ev['age']['quote']):raise ChainError('新老阈值不在引用的具体原文中')
    signed_offset=re.search(r'currency\.offset=([+-]?\d+(?:\.\d+)?)',ev['currency']['quote']) if str(p['evidence']['currency']['id']).startswith('V') else None
    if offset not in _numbers(ev['currency']['quote']) and not (signed_offset and offset==D(signed_offset[1])):raise ChainError('汇率调整额不在引用的具体原文中')
    agequote=ev['age']['quote']
    if re.search(r'大于\s*\d+',agequote) and p['age']['old_if']!='gt':raise ChainError('原文“月数大于阈值”不包含等于，不能用ge')
    if ('大于等于' in agequote or '≥' in agequote) and p['age']['old_if']!='ge':raise ChainError('原文明确包含等于，不能用gt')
    refs=set(p['routes'].values())
    for o in p['overrides']:
        if o.get('region') not in regions:raise ChainError(f'例外条件区域不存在：{o}')
        choose(o.get('customer_condition'),('in','not_in'),'区域例外客户条件')
        if not o.get('customers'):raise ChainError('例外客户条件不能为空')
        quot=o.get('source')
        if quot not in allowed or not all(c in catalog.blocks[quot]['text'] for c in o['customers']):
            raise ChainError(f'客户例外缺少逐字原文：{o}')
        refs.add(o['route'])
    if set(p['tier_tables'])!=refs:raise ChainError('地区路线与新老阶梯表的引用集合不一致')
    schedules={}; evidence=[]
    for ref,spec in p['tier_tables'].items():
        t=catalog.table(ref)
        b,n,o=(spec.get(k) for k in ('band_col','new_col','old_col'))
        if len({b,n,o})!=3:raise ChainError(f'{ref} 新、老、区间必须用不同列')
        h=spec.get('header_row',1)
        if '新' not in t.cell(h,n) or not re.search(r'老|旧|舊',t.cell(h,o)):
            raise ChainError(f'{ref} 新老列头不符：{t.cell(h,n)!r}/{t.cell(h,o)!r}')
        for state,c in [('new',n),('old',o)]:
            bands=[]
            for i in range(h+1,len(t.rows)+1):
                label=t.cell(i,b)
                if not string(label):continue
                value=_rate(t.cell(i,c),percent=True,where=t.location(i,c))
                bands.append((label,value,{'band':t.location(i,b),'rate':t.location(i,c),'raw':t.cell(i,c)}))
            inc=spec.get('includes') or [True]+[p['tier']['endpoint']=='lower']*(len(bands)-1)
            steps,proof=_steps(bands,percent=False,includes=inc,where=ref)
            schedules[(ref,state)]=steps
            evidence.append(dict(route=ref,age=state,steps=proof,title=t.title))
    return schedules,evidence,ev


def compile_coefficient_policy(catalog, p, tier_routes, allowed):
    choose(p.get('achievement_amount'),('actual','base'),'达成率分子')
    choose(p.get('achievement_region'),('raw',),'区域达成粒度（按实际销售区域；其他粒度须有独立预算匹配算子）')
    choose(p.get('history_months'),('nonzero','elapsed'),'历史月均分母')
    choose(p.get('history_window'),('year_to_date',),'历史时间范围')
    choose(p.get('budget_duplicates'),('unique','sum'),'预算重复处理')
    choose(p.get('risk_aggregate'),('sum',),'库存逾期汇总')
    choose(p.get('missing_margin'),('default',),'毛利缺值')
    choose(p.get('below_margin'),('first',),'毛利低于最低档')
    ev=catalog.evidence(p,('achievement','history','budget_default','margin_default','stock_default','overdue_default','margin_low','risk_aggregate'),allowed)
    # The aggregation grain is independently supported by the two matched
    # risk-indicator rules, not by the downstream additive deduction formula.
    # Repair only citation ownership if a model cited the latter by mistake;
    # do not replace the chosen numeric operator or any source value.
    if '客户' not in ev['risk_aggregate']['quote'] and all('客户' in ev[k]['quote'] and '金额' in ev[k]['quote'] for k in ('stock_default','overdue_default')):
        p['evidence']['risk_aggregate']=dict(p['evidence']['overdue_default'])
        p['risk_aggregation_basis']={'stock':p['evidence']['stock_default'],'overdue':p['evidence']['overdue_default'],
                                    'operation':'按客户关联的实际金额明细汇总；原文定义客户级风险比例，程序按匹配行累加'}
        ev=catalog.evidence(p,('achievement','history','budget_default','margin_default','stock_default','overdue_default','margin_low','risk_aggregate'),allowed)
    defaults={k:number(p.get('defaults',{}).get(k),f'{k} 缺失系数') for k in ('region','customer','gross','stock','overdue')}
    for name,evkey in [('region','budget_default'),('customer','budget_default'),('gross','margin_default'),('stock','stock_default'),('overdue','overdue_default')]:
        if defaults[name] not in _numbers(ev[evkey]['quote']):
            raise ChainError(f'{name} 的默认系数 {defaults[name]} 未出现在该项引用原文；请引用明示系数的句子')
    tables={};proof=[]
    for key in ('region','customer','stock','overdue'):
        spec=p.get('tables',{}).get(key) or {}
        t=catalog.table(spec.get('table'))
        hr,row=spec.get('header_row'),spec.get('value_row')
        cols=spec.get('columns')
        if not isinstance(cols,list) or len(set(cols))!=len(cols):raise ChainError(f'{key} 系数档列无效')
        label=t.cell(row,1)
        if key=='region' and '客户' in label and '区域' not in label:raise ChainError(f'{key} 误选客户达成行 {label!r}')
        if key=='customer' and '区域' in label and '客户' not in label:raise ChainError(f'{key} 误选区域达成行 {label!r}')
        if key=='stock' and '逾期' in t.title and '库存金额' not in t.title:raise ChainError('呆滞库存误选逾期应收制度表')
        if key=='overdue' and '库存金额' in t.title and '逾期' not in t.title:raise ChainError('逾期应收误选库存制度表')
        bands=[(t.cell(hr,c),_rate(t.cell(row,c),where=t.location(row,c)),{'band':t.location(hr,c),'rate':t.location(row,c),'raw':t.cell(row,c)}) for c in cols]
        tables[key],e=_steps(bands,percent=True,includes=spec.get('includes',[]),where=f'{key}/{t.id}')
        proof.append(dict(indicator=key,steps=e))
    if set(p.get('gross_routes',{}))!=set(tier_routes):raise ChainError('每个阶梯路线都需要相应毛利规则列，不能默认错用PI')
    for route,spec in p['gross_routes'].items():
        t=catalog.table(spec.get('table')); h=spec.get('header_row',1); b=spec.get('band_col'); c=spec.get('value_col')
        if b==c:raise ChainError(f'{route} 毛利上下界列与系数列相同')
        family=_business_family(catalog.table(route).title)
        selected_family=_business_family(t.cell(h,c))
        if family and selected_family and family!=selected_family:
            raise ChainError(f'{route} 阶梯适用 {family} 却绑定 {t.id} 毛利列 {t.cell(h,c)!r}；请按所属产品/事业部选择')
        bands=[(t.cell(i,b),_rate(t.cell(i,c),where=t.location(i,c)),{'band':t.location(i,b),'rate':t.location(i,c),'raw':t.cell(i,c)}) for i in range(h+1,len(t.rows)+1) if string(t.cell(i,b))]
        tables['gross:'+route],e=_steps(bands,percent=True,includes=spec.get('includes',[]),where=route+'/毛利')
        proof.append(dict(indicator='gross:'+route,steps=e))
    return tables,defaults,proof,ev


def compile_hold_policy(catalog,p,allowed):
    choose(p.get('hold_method'),('additive','multiplicative'),'暂扣算法')
    choose(p.get('freeze_source'),('list','status'),'冻结依据')
    if p.get('freeze_source')=='status' and not p.get('freeze_statuses'):
        raise ChainError('状态冻结需要明确状态列表')
    if type(p.get('project_round_digits')) is not int or p['project_round_digits'] not in range(0,7):
        raise ChainError('项目金额舍入位数无效')
    ev=catalog.evidence(p,('total','hold','freeze','project_round'),allowed)
    # v1 is a specific calculation contract, not a generic formula reader.
    # Never accept an explicit formula using a fifth factor merely because its
    # citation exists. The adaptive program compiler handles other expressions.
    raw=re.sub(r'\s+','',ev['total']['quote']).replace('×','*').replace('乘以','*').replace('＊','*')
    if '*' in raw:
        body=re.split(r'[=＝|：:]',raw)[-1].strip('。；;')
        known=(r'阶梯(?:提成(?:金额)?|金额)',r'区域(?:达成(?:率)?)?系数',r'客户(?:达成(?:率)?)?系数',r'毛利(?:率)?系数')
        terms=body.split('*')
        matches=[next((j for j,pattern in enumerate(known) if re.fullmatch(pattern,t.strip('()（）'))),-1) for t in terms]
        if sorted(matches)!=list(range(len(known))):
            raise ChainError(f'原文提成总额公式超出固定v1算子：{ev["total"]["quote"]!r}；需按完整公式编制动态规则，不能沿用旧乘积',owner='hold_policy')
    quote=ev['hold']['quote'].replace(' ','')
    if '+' in quote and ('库存' in quote or '呆滞' in quote) and '逾期' in quote and p['hold_method']!='additive':raise ChainError('暂扣原文分别计算两项并相加，不能改为乘法')
    if ('到分' in ev['project_round']['quote'] or '保留到分' in ev['project_round']['quote']) and p['project_round_digits']!=2:raise ChainError('项目保留到分等于小数2位')
    return ev


def _project_refs(catalog,mapping,kind,period,active):
    rows=defaultdict(list); unmatched=[]
    for row,where,_ in catalog.sheet(mapping[kind]).records(mapping[kind],SCOPE_FIELDS[kind]):
        if pivot_label(row.get('month')):continue
        if month(row['month'],where)!=period:continue
        key=project_key(row,where)
        if key not in active:unmatched.append(where);continue
        rows[key].append((row,where))
    return rows,unmatched


def _reference_amount(catalog,mapping,kind,period,active,key,duplicate):
    amounts={}; sources=defaultdict(list); ignored=[]; normalize=lambda x:string(x).casefold() if key=='customer' else string(x)
    for row,where,raw in catalog.sheet(mapping[kind]).records(mapping[kind],REF_FIELDS[kind]):
        identity=normalize(row.get(key))
        if pivot_label(row.get(key)) or not identity:
            ignored.append({'where':where,'reason':'汇总标签或空白占位，不生成对象'});continue
        if row.get('month') not in (None,'') and month(row['month'],where)!=period:continue
        if identity not in active:ignored.append({'where':where,'reason':'参考表无当期销售对象'});continue
        amount=number(row['amount'],where,blank=True)
        sources[identity].append(where)
        if amount is None:continue
        if identity in amounts and duplicate=='unique' and amounts[identity]!=amount:
            raise ChainError(f'{kind} 的 {identity} 存在不同重复金额：{amounts[identity]} 与 {amount}；{sources[identity]}',finance=True)
        amounts[identity]=(amounts.get(identity,ZERO)+amount if duplicate=='sum' else amount)
    return amounts,sources,ignored


def _single(entries,field,key,converter,*,missing=False):
    found=[]
    for row,where in entries:
        if row.get(field) not in (None,''):
            found.append((converter(row[field],where),where))
    values={v for v,_ in found}
    if len(values)>1:raise ChainError(f'{key} 的 {field} 有冲突值 {found}',finance=True)
    if not values and not missing:raise ChainError(f'{key} 缺少 {field}，检查参考表四键、期间及原始值',finance=True)
    return next(iter(values),None),[w for _,w in found]


def calculate_basis(catalog, specs, period, *, context=None):
    """Compute all project indicators from the current materials only."""
    context=context or {}
    # Calculation and approval use the same independent source-coverage gate;
    # a cached model PASS is not permission to treat one month as a year.
    validate_reference_mapping(catalog,specs['reference_mapping'],period,scope=specs['scope_mapping'],
        allow_current_only=context.get('history_single_month_confirmed',False),source_key=context.get('source_key',''))
    m={**specs['scope_mapping'],**specs['reference_mapping']}
    p,c,h=(specs[k] for k in ('project_policy','coefficient_policy','hold_policy'))
    sales,excluded=population(catalog,m,period,p)
    groups=defaultdict(list)
    for s in sales:groups[s['key']].append(s)
    active=set(groups); active_customers={s['customer'].casefold() for s in sales}; regions={s['region'] for s in sales}
    tier,tierproof,_=compile_project_policy(catalog,p,regions,set(catalog.blocks))
    ct,defaults,coefproof,_=compile_coefficient_policy(catalog,c,p['tier_tables'],set(catalog.blocks))
    compile_hold_policy(catalog,h,set(catalog.blocks))
    project_refs,unmatched={},{}
    for name in ('launch','gross','unpaid'):
        project_refs[name],unmatched[name]=_project_refs(catalog,m,name,period,active)
    if set(project_refs['launch'])!=active:
        miss=sorted(active-set(project_refs['launch']))
        raise ChainError(f'项目首发日期未覆盖销售四键：{len(miss)}个，示例 {miss[:4]}。检查月份、客户编码、简称及产品列；不能当零或改用参考表建项目。',finance=True,owner='scope_mapping')
    refs,src,ignored={},{},{}
    for kind,entity,keys in [('region_budget','region',regions),('customer_budget','customer',active_customers),('stock','customer',active_customers),('overdue','customer',active_customers)]:
        refs[kind],src[kind],ignored[kind]=_reference_amount(catalog,m,kind,period,keys,entity,c['budget_duplicates'] if 'budget' in kind else 'sum')
    actual_region=defaultdict(lambda:ZERO); actual_customer=defaultdict(lambda:ZERO)
    history=defaultdict(lambda:defaultdict(lambda:ZERO)); history_src=defaultdict(list)
    classified={};base={};sales_actual={}
    adjustment=number(p['currency']['offset'],'汇率调整额')
    currency=p['currency'].get('code')
    for key,rows in groups.items():
        for field in ('customer','region','currency','fx','series','code','product'):
            if len({s[field] for s in rows})!=1:
                raise ChainError(f'{key} 同项目 {field} 有多个原值：{[(x[field],x["where"]) for x in rows[:5]]}；需明确项目属性或汇率汇总规则',finance=True)
        s=rows[0]; cust=s['customer'].casefold(); route=p['routes'][s['region']]
        for override in p['overrides']:
            inside=s['customer'] in override['customers']
            if s['region']==override['region'] and inside==(override['customer_condition']=='in'):route=override['route']
        classified[key]=route
        amount=sum((x['amount'] for x in rows),ZERO);sales_actual[key]=amount
        if s['currency']==currency:
            if s['fx'] is None or s['fx']==0:raise ChainError(f'{key} 币种 {currency} 汇率缺失或为零',finance=True)
            base[key]=sum((x['amount']/x['fx']*(x['fx']-adjustment) for x in rows),ZERO)
        else:base[key]=amount
        measure=amount if c['achievement_amount']=='actual' else base[key]
        entity=s['region'] if c['achievement_region']=='raw' else route
        actual_region[entity]+=measure;actual_customer[cust]+=measure
        # Current month is always recomputed from raw sales, not a copied pivot.
        history[cust][period]+=amount;history_src[cust].extend(x['where'] for x in rows)
    # Past history is independent of the current project's existence in that
    # historical month, and never filtered by the full current project key.
    history_current=defaultdict(lambda:ZERO)
    for row,where,_ in catalog.sheet(m['history']).records(m['history'],REF_FIELDS['history']):
        if pivot_label(row.get('month')):continue
        when=month(row['month'],where)
        if not (period[:4]+'01'<=when<=period):continue
        cust=string(row['customer']).casefold()
        if cust not in active_customers:continue
        amt=number(row['amount'],where)
        if amt==0:continue
        if when==period:history_current[cust]+=amt;continue
        history[cust][when]+=amt;history_src[cust].append(where)
    for cust,amt in history_current.items():
        if abs(amt-history[cust][period])>D('0.00001'):
            raise ChainError(f'历史源本期实际销售与当期销售原表不一致：{cust} 历史 {amt} / 当期 {history[cust][period]}；核对期间及数据源',finance=True)
    projects={}; boundaries=0; cross_checks=0
    for route,age in tier:
        steps=tier[(route,age)]
        for lower,_,_ in steps:
            for x in (lower-D('.01'),lower,lower+D('.01')):
                check=tier_check(x,steps,p['tier']['method'],p['tier']['negative'])
                result=tier_amount(x,steps,p['tier']['method'],p['tier']['negative'])
                if abs(Fraction(result)-check)>Fraction(D('0.000000000001')):raise ChainError(f'{route}/{age} 档位边界运算不一致：{x}')
                boundaries+=1
    for key,rows in groups.items():
        s=rows[0]; cust=s['customer'].casefold(); route=classified[key]
        launch,launchsrc=_single(project_refs['launch'][key],'first_date',key,lambda x,w:to_date(x))
        age=(int(period[:4])-launch.year)*12+int(period[4:])-launch.month
        if age<0:raise ChainError(f'{key} 首发日期在核算月之后：{launch}',finance=True)
        old=age>p['age']['months'] if p['age']['old_if']=='gt' else age>=p['age']['months']
        gm,gmsrc=_single(project_refs['gross'].get(key,[]),'margin',key,lambda x,w:number(x,w),missing=True)
        ra=s['region'] if c['achievement_region']=='raw' else route
        arr=actual_region[ra]; arb=refs['region_budget'].get(ra)
        aca=actual_customer[cust]; acb=refs['customer_budget'].get(cust)
        region_ratio,region_factor=coefficient(arr,arb,ct['region'],defaults['region'])
        cust_ratio,cust_factor=coefficient(aca,acb,ct['customer'],defaults['customer'])
        gross_ratio,gross_factor=coefficient(gm,ONE,ct['gross:'+route],defaults['gross'])
        monthly=history[cust]; months=len(monthly) if c['history_months']=='nonzero' else int(period[4:])
        hist_sum=sum(monthly.values(),ZERO)
        avg=hist_sum/D(months) if months else None
        st=refs['stock'].get(cust);od=refs['overdue'].get(cust)
        if avg in (None,ZERO) and any(x not in (None,ZERO) for x in (st,od)):
            raise ChainError(f'{s["customer"]} 有风险余额但月平均销售为{avg}，须明确分母为零的规则',finance=True,owner='coefficient_policy')
        stock_ratio,stock_factor=coefficient(st,avg,ct['stock'],defaults['stock'])
        overdue_ratio,overdue_factor=coefficient(od,avg,ct['overdue'],defaults['overdue'])
        steps=tier[(route,'old' if old else 'new')]
        ta=tier_amount(base[key],steps,p['tier']['method'],p['tier']['negative'])
        independent=tier_check(base[key],steps,p['tier']['method'],p['tier']['negative'])
        if abs(Fraction(ta)-independent)>Fraction(D('0.000000000001')):raise ChainError(f'{key} 阶梯提成独立算术不一致')
        debts=project_refs['unpaid'].get(key,[])
        frozen=bool(debts) if h['freeze_source']=='list' else any(string(r['status']) in h['freeze_statuses'] for r,_ in debts)
        money=project_money(ta,region_factor,cust_factor,gross_factor,stock_factor,overdue_factor,frozen,h['hold_method'],h['project_round_digits'])
        close_money(money)
        # Algebraic independent remainder from the original factors; never
        # infer the correct formula by matching a financial reference amount.
        frac_total=independent*Fraction(region_factor)*Fraction(cust_factor)*Fraction(gross_factor)
        frac_remaining=frac_total*(Fraction(stock_factor)+Fraction(overdue_factor)-1 if h['hold_method']=='additive' else Fraction(stock_factor)*Fraction(overdue_factor))
        alt=D(frac_remaining.numerator)/D(frac_remaining.denominator) if not frozen else ZERO
        if rounded(alt,h['project_round_digits'])!=money['alloc']:
            raise ChainError(f'{key} 独立有理数复算不一致：{alt} 对 {money["alloc"]}')
        cross_checks+=1
        projects[key]=dict(key=key,month=period,customer=s['customer'],code=s['code'],product=s['product'],series=s['series'],region=s['region'],route=route,
            fx=s['fx'],currency=s['currency'],sales=sales_actual[key],base=base[key],first_date=launch.isoformat(),age=age,old=old,tier=ta,tier_steps=steps,
            region_actual=arr,region_budget=arb,region_ratio=region_ratio,region_factor=region_factor,
            customer_actual=aca,customer_budget=acb,customer_ratio=cust_ratio,customer_factor=cust_factor,
            gross=gross_ratio,gross_factor=gross_factor,history_sum=hist_sum,history_months=months,history_average=avg,
            stock=st,stock_ratio=stock_ratio,stock_factor=stock_factor,overdue=od,overdue_ratio=overdue_ratio,overdue_factor=overdue_factor,
            frozen=frozen,**money,sources={'sales':[x['where'] for x in rows],'launch':launchsrc,'gross':gmsrc,
                'unpaid':[w for _,w in debts],'history':history_src[cust],'region_budget':src['region_budget'].get(ra,[]),
                'customer_budget':src['customer_budget'].get(cust,[]),'stock':src['stock'].get(cust,[]),'overdue':src['overdue'].get(cust,[])})
    sums={field:sum((v[field] for v in projects.values()),ZERO) for field in ('total','hold','debt','payable','alloc','round_delta')}
    totals={'正常核算分配提成奖':rounded(sums['alloc'],2),'客户欠款暂扣奖金':rounded(sums['debt'],2),'呆滞+逾期系数暂扣奖金':rounded(sums['hold'],2),'合计':rounded(sums['total'],2)}
    return dict(projects=projects, totals=totals, sums=sums,pool=totals['正常核算分配提成奖'],
                objects={k:{'raw':v['payable'],'amount':v['alloc']} for k,v in projects.items()},
                checks=dict(sales_rows=len(sales),excluded=excluded,projects=len(projects),historical_customers=len(history),
                            unmatched={k:len(v) for k,v in unmatched.items()},reference_ignored=ignored,
                            default_counts={k:sum(v[k] is None for v in projects.values()) for k in ('region_ratio','customer_ratio','gross','stock_ratio','overdue_ratio')},
                            project_cross_checks=cross_checks,tier_boundary_checks=boundaries),
                parameter_evidence=tierproof+coefproof)


def _role_rate(t,row,col,variant,unit):
    raw=t.cell(row,col)
    lines=[s.strip() for s in raw.splitlines() if s.strip()]
    if len(lines)>1:
        matches=[line for line in lines if re.sub(r'^\s*[\d.]+\s*%?\s*','',line).strip()==variant]
        if len(matches)!=1:raise ChainError(f'{t.location(row,col)} 区域分支 {variant!r} 无唯一匹配：{lines}')
        raw=matches[0]
        raw=re.match(r'^\s*([\d.]+\s*%?)',raw)[1].strip()
    return _rate(raw,percent=unit=='percent',where=t.location(row,col))


def compile_roster(catalog, p, routes, allowed):
    # Normalize harmless envelopes observed in real structured model replies.
    # The values themselves still come from the model's actual source binding.
    nested=p.get('roster')
    if isinstance(nested,dict):
        for k in ('role_unit','share_unit','default_share','empty_role','person_rounding','person_round_digits','roles','routes','evidence'):
            if k in nested:
                if k in p and p[k]!=nested[k]:raise ChainError(f'名单节点 {k} 内外两处声明冲突')
                p[k]=nested.pop(k)
    for role,spec in (p.get('roles') or {}).items():
        if isinstance(spec.get('share'),list):
            cols=spec['share']
            if len(cols)>1:raise ChainError(f'{role} 多个比例列需要明确逐名对应关系')
            spec['share']=cols[0] if cols else None
        col=spec.get('share')
        if col and spec.get('text_precedence') and col in (spec.get('names') or []) and len(spec['names'])>1:
            # A text-share cell is not itself a fallback name column.
            spec['names']=[x for x in spec['names'] if x!=col]
    rmap=p.get('roster');catalog.sheet(rmap).validate(rmap,KEY)
    choose(p.get('role_unit'),('percent','ratio'),'制度角色裸数单位')
    choose(p.get('share_unit'),('percent','ratio'),'名单比例裸数单位')
    choose(p.get('person_rounding'),('after_sum','per_record'),'人员舍入时点')
    choose(p.get('empty_role'),('pending',),'空名单处理')
    if type(p.get('person_round_digits')) is not int or p['person_round_digits'] not in range(7):raise ChainError('人员舍入位数无效')
    share=number(p.get('default_share'),'空比例默认值')
    if not ZERO<=share<=ONE:raise ChainError('空比例默认值不在0～1')
    if set(p.get('roles',{}))!=set(ROLES):raise ChainError('目前固定角色接口是销售负责人、销售执行人、商务负责人、客户经理、NPD；新增角色需要扩展节点')
    ev=catalog.evidence(p,('default_share','role_unit','role_empty','manager_priority','rounding','npd','no_transfer'),allowed)
    # When the authority explicitly identifies a text column and fallback
    # name columns, check those physical coordinates as a semantic contract.
    manager_rule=ev['manager_priority']['quote']
    specific=re.search(r'([A-Z]+)\s*有文字.*?\1\s*为空时\s*按\s*([A-Z]+)\s*/\s*([A-Z]+)',manager_rule)
    if specific:
        m=p['roles']['客户经理']
        if m.get('share')!=specific[1] or set(m.get('names',[]))!={specific[2],specific[3]} or m.get('text_precedence') is not True:
            raise ChainError(f'财务原文明确 {specific[1]} 有文字优先、空时取 {specific[2]}/{specific[3]} 姓名，当前名单绑定不符')
    for label,spec in p['roles'].items():
        if label=='NPD' and spec.get('pending') is True:continue
        cols=spec.get('names') or []
        if not cols:raise ChainError(f'{label} 缺少名单姓名列；无名角色应明确pending')
        fake={'sheet':rmap['sheet'],'header_row':rmap.get('header_row',1),'columns':{f'name{i}':col for i,col in enumerate(cols)}}
        if spec.get('share'):fake['columns']['share']=spec['share']
        catalog.sheet(fake).validate(fake,tuple(fake['columns']))
    if set(p.get('routes',{}))!=set(routes):raise ChainError('每个阶梯路线都必须匹配完整制度角色表')
    rates={};proof=[]
    for route, spec in p['routes'].items():
        t=catalog.table(spec.get('table'));nc,oc=spec.get('new_col'),spec.get('old_col');h=spec.get('header_row',1)
        if nc==oc or '新' not in t.cell(h,nc) or not re.search('老|旧|舊',t.cell(h,oc)):
            raise ChainError(f'{route} 制度角色表的新老列错误')
        if set(spec.get('rows',{}))!=set(ROLES) or len(set(spec['rows'].values()))!=len(ROLES):raise ChainError(f'{route} 角色行映射不完整或重复')
        family=_business_family(catalog.table(route).title);role_family=_business_family(t.title)
        if family=='BIU':family='PI'
        if family and role_family and family!=role_family:raise ChainError(f'{route} 应属 {family}，角色制度表却是 {role_family}')
        simplify=str.maketrans({'負':'负','責':'责','執':'执','務':'务','經':'经','銷':'销','售':'售','戶':'户'})
        for role,row in spec['rows'].items():
            label=string(t.cell(row,1)).translate(simplify)
            if label in ROLES and label!=role:raise ChainError(f'{route} 角色 {role} 错绑 {t.location(row,1)} 原文角色 {label}')
        if '南方' in catalog.table(route).title and '非南方' in spec.get('variant',''):raise ChainError(f'{route} 南方区域匹配了非南方角色行')
        for age,col in [('new',nc),('old',oc)]:
            r={role:_role_rate(t,row,col,spec.get('variant',''),p['role_unit']) for role,row in spec['rows'].items()}
            if sum(r.values(),ZERO)!=ONE:raise ChainError(f'{route}/{age} 角色合计 {sum(r.values(),ZERO)}，应为100%，请核对单位和地区分支')
            rates[(route,age)]=r
            proof.append({'route':route,'age':age,'rates':{role:{'rate':val,'source':t.location(spec['rows'][role],col),'raw':t.cell(spec['rows'][role],col)} for role,val in r.items()}})
    return rates,share,proof,ev


def _parse_shares(value,names,policy,spec,where):
    raw=string(value)
    names=list(dict.fromkeys(n for n in names if n))
    if raw and spec.get('text_precedence') and ('%' in raw or '％' in raw):
        parts=re.split(r'[,，、;；\n]+',raw)
        parsed=[]
        for part in parts:
            match=re.fullmatch(r'\s*(.+?)\s*(\d+(?:\.\d+)?)\s*[%％]\s*',part)
            if not match:raise ChainError(f'{where}：无法解析带姓名比例 {part!r}，不能猜测',finance=True)
            parsed.append((match[1].strip(),D(match[2])/100))
        return parsed
    if not names:return []
    if len(names)>1:
        raise ChainError(f'{where}：有多名人员 {names}，但没有可唯一对应的姓名比例 {raw!r}',finance=True,owner='roster',
                         examples=[f'项目可分配1000元、角色10%时，两名各50%为各50元；单人100%则一人为100元，需实际比例。'])
    if not raw:return [(names[0],number(policy['default_share'],'默认名单比例'))]
    return [(names[0],_rate(raw,percent=policy['share_unit']=='percent',where=where))]


def calculate_settlement(catalog,p,basis,period,*,recipient_overrides=()):
    from openpyxl.utils import column_index_from_string
    from bonus_agent.commission_revision import recipient_source
    projects=basis['projects'];rates,default,proof,_=compile_roster(catalog,p,{r['route'] for r in projects.values()},set(catalog.blocks))
    overrides={}
    for item in recipient_overrides:
        key,role=item.get('project'),item.get('role')
        if key not in projects or role not in ROLES or item.get('period')!=period:
            raise ChainError(f'人员修订 {item.get("id")} 的项目、角色或核算月已不匹配当前对象；需核对适用范围',finance=True,owner='roster')
        if (key,role) in overrides:raise ChainError(f'{key}/{role} 有冲突的人员修订')
        sid=recipient_source(item)
        if sid not in catalog.blocks:raise ChainError(f'{key}/{role} 人员修订缺少实际财务确认记录')
        selected=[];names=set()
        for row in item.get('shares',[]):
            name=string(row.get('name'));weight=number(row.get('share'),sid)
            if not name or name in names or not ZERO<=weight<=ONE:raise ChainError(f'{sid} 人员份额无效')
            names.add(name);selected.append((name,weight,f'财务人员修订记录/{item["id"]}'))
        if not selected or sum((x[1] for x in selected),ZERO)>ONE:raise ChainError(f'{sid} 缺少实名份额或合计超过100%')
        overrides[(key,role)]=selected
    # Rates may include a valid route not exercised this month. Requiring exact
    # current coverage is enough; no prior month recipients are imported.
    roster=defaultdict(list);ignored=[]
    for row,where,raw in catalog.sheet(p['roster']).records(p['roster'],KEY):
        if pivot_label(row.get('month')):continue
        if month(row['month'],where)!=period:continue
        key=project_key(row,where)
        if key not in projects:ignored.append(where);continue
        roster[key].append((raw,where))
    all_rows=[];people=defaultdict(lambda:ZERO);pending=defaultdict(lambda:ZERO);per_record=p['person_rounding']=='per_record'
    digits=p['person_round_digits']; checks=0
    for key,project in projects.items():
        amounts=rates[(project['route'],'old' if project['old'] else 'new')]
        share_rows=[]
        for role,spec in p['roles'].items():
            if (key,role) in overrides:
                selected=overrides[(key,role)]
            elif spec.get('pending') is True:
                share_rows.append((role,PENDING_NPD if role=='NPD' else role+'未分配',ONE,'pending','制度：未提供实名名单'));continue
            else:
                selected=[]
                for raw,where in roster.get(key,[]):
                    cell=lambda col:raw[column_index_from_string(col)-1] if col else None
                    names=[string(cell(col)) for col in spec.get('names',[])]
                    entries=_parse_shares(cell(spec.get('share')),names,p,spec,where)
                    selected.extend((name,weight,where) for name,weight in entries)
            # Identical duplicate rows must not silently double-pay. A missing
            # roster leaves an explicit pending role; no invented recipients.
            weights=defaultdict(lambda:ZERO); src=defaultdict(list)
            for name,weight,where in selected:
                if not ZERO<=weight<=ONE:raise ChainError(f'{key}/{role} 名单比例不在0～1：{weight}',finance=True)
                weights[name]+=weight;src[name].append(where)
            used=sum(weights.values(),ZERO)
            if used>ONE:raise ChainError(f'{key}/{role} 名单份额超过100%：{dict(weights)}',finance=True)
            for name,weight in weights.items():share_rows.append((role,name,weight,'person','；'.join(src[name])))
            if used<ONE:share_rows.append((role,role+'未分配',ONE-used,'pending','财务修订未指定份额' if (key,role) in overrides else '名单空值/角色份额剩余'))
        contributions=settle_project(project['alloc'],share_rows,amounts)
        for row in contributions:
            row['project']=key
            target=people if row['destination']=='person' else pending
            target[row['name']]+=rounded(row['raw'],digits) if per_record else row['raw']
            # Cross-check each raw contribution separately without matching
            # any amount from the reference workpaper.
            exact=Fraction(project['alloc'])*Fraction(row['role_rate'])*Fraction(row['share'])
            if abs(Fraction(row['raw'])-exact)>Fraction(D('0.000000000001')):raise ChainError(f'{key}/{row["name"]} 角色原值复算不一致')
            checks+=1
        all_rows.extend(contributions)
    if abs(sum((r['raw'] for r in all_rows),ZERO)-basis['sums']['alloc'])>D('0.000000000001'):raise ChainError('人员与待分配原值没有覆盖全部项目资金')
    if set(people)&set(pending):raise ChainError('待分配类别与实际姓名重复，须明确不同身份',finance=True)
    items=[dict(name=n,raw=v,amount=rounded(v,digits),destination=kind) for kind,group in [('person',people),('pending',pending)] for n,v in sorted(group.items())]
    real=sum((r['amount'] for r in items if r['destination']=='person'),ZERO)
    waiting=sum((r['amount'] for r in items if r['destination']=='pending'),ZERO)
    return dict(rows=all_rows,people=items,allocated=real,pending=waiting,residual=basis['sums']['alloc']-real-waiting,
                checks=dict(role_rows=checks,people=len(people),pending_categories=len(pending),unmatched_roster=ignored,
                            missing_roster=sorted(set(projects)-set(roster))),parameter_evidence=proof)
