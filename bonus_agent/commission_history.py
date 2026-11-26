"""Independent coverage check for a year-to-date shipment history binding.

The selected month/customer/amount columns must describe the required time
window, not just the current sales sheet. Candidate discovery uses physical
headers, observed months, and actual customer totals; it never uses expected
bonus amounts or a previous case's column choices.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from itertools import product
import re

from openpyxl.utils import get_column_letter

from bonus_agent.commission_sources import ChainError, month, number, pivot_label, string


HISTORY_FIELDS=('month','customer','amount')
NO_PRIOR_ANSWER='确认本批年初至上月没有出货记录，本期数据已完整'
QUESTION_PREFIX='历史出货范围确认'
TOLERANCE=Decimal('0.00001')


class HistoryCoverageError(ChainError):
    def __init__(self,message,*,replacement=None,evidence=None,finance=False,question=None):
        super().__init__(message,finance=finance,owner='reference_mapping')
        self.replacement=replacement;self.evidence=evidence or {};self.question=question


class HistoryMaterialMissing(ChainError):
    """A confirmed missing input is not a model-retry condition."""


def _header(value):
    text=string(value).casefold().translate(str.maketrans({'銷':'销','額':'额','貨':'货','幣':'币','戶':'户','簡':'简','稱':'称','實':'实','際':'际','計':'计','匯':'汇'}))
    text=re.sub(r'[^\w\u4e00-\u9fff]+','',text).replace('_','')
    return re.sub(r'^(求和项|汇总项|合计项|sumof|sum)','',text)


def _kind(value):
    h=_header(value)
    if h in ('月','月份','年月','日期','期间','核算期间','核算月','会计月份','销售日期','销售月份','出货日期','出货月份','交易日期','交易月份','month','date','period','salesmonth','salesdate'):
        return 'month'
    if h in ('客户','客户名称','客户名','customer','customername','customerid','客户编码','客户代码') or re.fullmatch(r'客户简称\d*',h):
        return 'customer'
    if h in ('salesamount','actualsales','salesrevenue','shipmentamount','shipmentvalue','actualsalesamount','salesamountlocalcurrency') or re.fullmatch(r'(?:实际)?(?:本位币|本币)?(?:销售金额|销售额|出货金额|出货额)(?:本位币|本币|人民币|rmb|cny)?',h):
        return 'amount'
    return None


def _signature(catalog,spec,period):
    key=(period,spec['sheet'],spec.get('header_row',1),tuple((k,spec['columns'][k]) for k in HISTORY_FIELDS))
    cache=getattr(catalog,'_history_signatures',None)
    if cache is None:cache={};catalog._history_signatures=cache
    if key in cache:return cache[key]
    rows=0;months=set();amounts=defaultdict(lambda:defaultdict(Decimal));pairs=set();examples={}
    for record,where,_ in catalog.sheet(spec).records(spec,HISTORY_FIELDS):
        if not record.get('month') or pivot_label(record.get('month')) or not record.get('customer') or pivot_label(record.get('customer')):continue
        when=month(record['month'],where)
        if not period[:4]+'01'<=when<=period:continue
        amount=number(record['amount'],where,blank=True)
        if amount is None or not amount:continue
        customer=string(record['customer']).casefold()
        rows+=1;months.add(when);pairs.add((customer,when));amounts[customer][when]+=amount
        examples.setdefault(when,where)
    current={c:v[period] for c,v in amounts.items() if v.get(period)}
    report={'spec':spec,'months':sorted(months),'rows':rows,'examples':examples,'current':current,'pairs':pairs,'amounts':amounts}
    cache[key]=report;return report


def _summary(item):
    return {'mapping':item['spec'],'months':item['months'],'nonzero_rows':item['rows'],
            'current_customers':len(item['current']),'month_source_examples':item['examples']}


def discover_history_sources(catalog,period):
    cache=getattr(catalog,'_history_sources',{})
    if period in cache:return cache[period]
    candidates=[]
    for sheet in catalog.sheets.values():
        for header,row in enumerate(sheet.rows[:4],1):
            columns={f:[] for f in HISTORY_FIELDS}
            for i,cell in enumerate(row,1):
                if (kind:=_kind(cell)):columns[kind].append(get_column_letter(i))
            if not all(columns.values()):continue
            for combination in product(*(columns[f] for f in HISTORY_FIELDS)):
                if len(set(combination))!=3:continue
                spec={'sheet':sheet.id,'header_row':header,'columns':dict(zip(HISTORY_FIELDS,combination))}
                try:result=_signature(catalog,spec,period)
                except (ChainError,ValueError,KeyError,IndexError):continue
                if result['rows']:candidates.append(result)
    cache[period]=candidates;catalog._history_sources=cache
    return candidates


def history_source_previews(catalog,period):
    return [{**_summary(x),'file':catalog.sheet(x['spec']).file,'sheet':catalog.sheet(x['spec']).name} for x in discover_history_sources(catalog,period)]


def _same_current(left,right):
    return bool(left) and set(left)==set(right) and all(abs(left[k]-right[k])<=TOLERANCE for k in left)


def history_confirmation(questions,period,source_key):
    """A no-history decision must be a real answer for these exact sources."""
    marker=f'{QUESTION_PREFIX}（{period} / {source_key}）'
    for q in questions or []:
        get=q.get if isinstance(q,dict) else lambda key,default=None:getattr(q,key,default)
        answer=str(get('answer') or '')
        if get('related')=='reference_mapping' and not get('withdrawn') and str(get('text','')).startswith(marker) and (answer==NO_PRIOR_ANSWER or answer.startswith(NO_PRIOR_ANSWER+'；补充说明：')):
            return True
    return False


def check_history_coverage(catalog,history,period,*,scope=None,allow_current_only=False,source_key=''):
    chosen=_signature(catalog,history,period)
    if not chosen['months']:
        raise HistoryCoverageError('所绑历史出货表没有本年有效销售记录，请核对月份、客户与实际销售金额列。',evidence={'selected':_summary(chosen)})
    reference_current=chosen['current']
    sales=(scope or {}).get('sales')
    if sales:
        try:
            actual=_signature(catalog,{'sheet':sales['sheet'],'header_row':sales.get('header_row',1),'columns':{k:sales['columns'][k] for k in HISTORY_FIELDS}},period)
            if actual['current']:reference_current=actual['current']
        except (ChainError,ValueError,KeyError):pass
    active=set(reference_current)
    prior={(c,m) for c,m in chosen['pairs'] if c in active and m<period}
    # Role-bound gross/launch/unpaid lists can corroborate history, but cannot
    # silently become the population source for all shipments. A uniquely
    # reconciling, independent shipment table is the only automatic repair.
    role_refs={v.get('sheet') for k,v in (scope or {}).items() if k in ('gross','launch','unpaid') and isinstance(v,dict)}
    more_complete=[];repairable=[]
    has_earlier=any(m<period for m in chosen['months'])
    for alternative in discover_history_sources(catalog,period):
        if alternative['spec']==history:continue
        earlier={(c,m) for c,m in alternative['pairs'] if c in active and m<period}
        added=earlier-prior
        reconciles=_same_current(reference_current,alternative['current'])
        if not added and not (not has_earlier and reconciles and any(m<period for m in alternative['months'])):continue
        more_complete.append((alternative,added))
        common_matches=prior<=earlier and all(abs(chosen['amounts'][c][m]-alternative['amounts'][c][m])<=TOLERANCE for c,m in prior)
        if alternative['spec']['sheet'] not in role_refs and reconciles and common_matches:
            repairable.append((alternative,added))
    suspect=not has_earlier or any(added for _,added in repairable)
    if suspect and more_complete:
        evidence={'selected':_summary(chosen),'alternatives':[dict(_summary(a),additional_customer_months=len(p),current_reconciles=_same_current(reference_current,a['current'])) for a,p in more_complete]}
        unique=repairable[0][0]['spec'] if len(repairable)==1 else None
        details='；'.join(f"{a['spec']['sheet']} {a['spec']['columns']} 实际月份 {','.join(a['months'])}，增加 {len(p)} 个当期客户的历史月份" for a,p in more_complete[:5])
        note=f"历史出货绑定不完整：{history['sheet']} 实际月份 {','.join(chosen['months'])}。材料中存在本年其他月份实际出货：{details}。"
        if unique:note+=f"独立业务表 {unique['sheet']} 与当前绑定的本月全部 {len(active)} 个客户实际销售逐客户一致，唯一可直接纠正的列绑定为 {unique['columns']}。"
        else:note+='须按材料确认完整出货来源，不能将历史缺失当作零或用毛利/欠款子集替代。'
        raise HistoryCoverageError(note,replacement=unique,evidence=evidence)
    if not has_earlier and int(period[4:])>1 and not allow_current_only:
        text=f'{QUESTION_PREFIX}（{period} / {source_key}）：目前已识别 {history["sheet"]} 的实际出货月份为 {", ".join(chosen["months"])}，未找到可核验的年初至上月实际出货记录。请确认本期材料是否完整，或补充历史出货。'
        raise HistoryCoverageError(text,finance=True,evidence={'selected':_summary(chosen)},question={
            'text':text,'options':[NO_PRIOR_ANSWER,'此前有出货，需要补充历史出货材料'],
            'identified':f'原始绑定 {history["sheet"]}；实际出货月份 {chosen["months"]}；本期客户 {len(active)} 个',
            'missing':'年初至上月的实际出货记录，或本期确为完整历史的财务确认',
            'impact':'月平均出货为库存及逾期比例的分母，缺失不能当零',
            'example':'若本月出货100元、此前一月出货300元，实际月均200元而非100元；风险余额相同但系数可能不同。'})
    return _summary(chosen)
