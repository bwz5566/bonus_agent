"""Source-backed Excel input records for an already verified commission result.

This module is an output adapter, not a new financial engine. It exposes the
matched physical reference records and month groupings, and proves that the
displayed input records reproduce the frozen basis. No expected workbook is
read here. Blank reference amounts remain blank, never an invented zero.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from bonus_agent.commission_calculation import REF_FIELDS, _project_refs, ROLES
from bonus_agent.commission_sources import month, number, pivot_label, string


ZERO=Decimal(0)
REFERENCE_KINDS=('region_budget','customer_budget','stock','overdue','gross')
REFERENCE_LABELS={'region_budget':'区域预算','customer_budget':'客户预算','stock':'呆滞库存','overdue':'逾期应收',
                  'gross':'项目毛利率','history':'历史实际出货','unpaid':'欠款记录'}


def inline_role_slots(settlement):
    """One pair per actual recipient/pending share; no two-person truncation."""
    if settlement is None:return []
    count=defaultdict(int)
    for row in settlement['rows']:
        if row['role'] not in ROLES:raise ValueError(f'报表未定义实际制度角色 {row["role"]}')
        count[(row['project'],row['role'])]+=1
    slots=[]
    for role in ROLES:
        minimum=2 if role in ('销售执行人','客户经理') else 1
        width=max(minimum,max((n for (key,r),n in count.items() if r==role),default=0))
        for i in range(width):
            name=role if width==1 else f'{role}{i+1}'
            slots.append((role,i,name,name+'金额'))
    return slots


def _rollup(records,mode):
    amounts=[x['amount'] for x in records if x['amount'] is not None]
    if not amounts:return None
    if mode=='sum':return sum(amounts,ZERO)
    if len(set(amounts))!=1:raise ValueError(f'报表参考记录的唯一值存在冲突：{records}')
    return amounts[0]


def _assert_equal(actual,expected,label):
    if actual is None or expected is None:
        if actual is not expected:raise ValueError(f'{label} 报表参考数据空值与核验结果不同')
    elif Decimal(str(actual))!=Decimal(str(expected)):
        raise ValueError(f'{label} 报表参考输入 {actual} 与核验结果 {expected} 不一致')


def build_report_sources(catalog,basis,specs,period):
    projects=basis['projects'];customers={p['customer'].casefold() for p in projects.values()};regions={p['region'] for p in projects.values()}
    ref=specs['reference_mapping'];scope=specs['scope_mapping'];coef=specs['coefficient_policy'];hold=specs['hold_policy']
    groups=defaultdict(list);history=defaultdict(lambda:defaultdict(list));debt=defaultdict(list)
    project_meta={};current_sales=defaultdict(lambda:ZERO)
    for key,p in projects.items():
        current_sales[p['customer'].casefold()]+=p['sales']
        project_meta[key]={'region':p['region'],'route':p['route'],'customer_key':p['customer'].casefold(),
                           'sales_count':len(p['sources']['sales']),'sales_examples':p['sources']['sales'][:3]}
    for kind,field,active in [('region_budget','region',regions),('customer_budget','customer',customers),
                              ('stock','customer',customers),('overdue','customer',customers)]:
        for row,where,_ in catalog.sheet(ref[kind]).records(ref[kind],REF_FIELDS[kind]):
            key=string(row.get(field));key=key.casefold() if field=='customer' else key
            if not key or pivot_label(row.get(field)) or key not in active:continue
            if row.get('month') not in (None,'') and month(row['month'],where)!=period:continue
            groups[(kind,key)].append({'amount':number(row['amount'],where,blank=True),'where':where,'period':period})
    for key,rows in _project_refs(catalog,scope,'gross',period,set(projects))[0].items():
        for row,where in rows:
            groups[('gross',key)].append({'amount':number(row.get('margin'),where,blank=True),'where':where,'period':period})
    for key,rows in _project_refs(catalog,scope,'unpaid',period,set(projects))[0].items():
        for row,where in rows:
            debt[key].append({'status':string(row.get('status')),'where':where,'period':period})
    for row,where,_ in catalog.sheet(ref['history']).records(ref['history'],REF_FIELDS['history']):
        if pivot_label(row.get('month')):continue
        when=month(row['month'],where)
        if not period[:4]+'01'<=when<period:continue
        customer=string(row['customer']).casefold()
        if customer not in customers:continue
        amount=number(row['amount'],where)
        if amount:history[customer][when].append({'amount':amount,'where':where,'period':when})
    policies={'region_budget':coef['budget_duplicates'],'customer_budget':coef['budget_duplicates'],
              'stock':'sum','overdue':'sum','gross':'unique'}
    for key,p in projects.items():
        customer=p['customer'].casefold()
        for kind,entity in [('region_budget',p['region']),('customer_budget',customer),('stock',customer),('overdue',customer),('gross',key)]:
            _assert_equal(_rollup(groups.get((kind,entity),[]),policies[kind]),p[kind],f'{key}/{kind}')
        past=sum((_rollup(rows,'sum') for rows in history.get(customer,{}).values()),ZERO)
        _assert_equal(past+current_sales[customer],p['history_sum'],f'{key}/历史出货')
        actual_months=len(history.get(customer,{}))+1 if coef['history_months']=='nonzero' else int(period[4:])
        if actual_months!=p['history_months']:raise ValueError(f'{key} 报表历史出货月份数与核验结果不同')
        matched=debt.get(key,[])
        frozen=bool(matched) if hold['freeze_source']=='list' else any(x['status'] in hold['freeze_statuses'] for x in matched)
        if frozen!=p['frozen']:raise ValueError(f'{key} 报表欠款明细与实际冻结结果不同')
    return {'groups':dict(groups),'history':{k:dict(v) for k,v in history.items()},'debt':dict(debt),
            'projects':project_meta,'customers':sorted(customers),'current_sales':dict(current_sales),'policies':policies}
