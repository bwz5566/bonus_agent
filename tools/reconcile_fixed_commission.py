"""Independently reconcile a *finished* fixed-chain case to the 1112 workpaper.

This adapter is deliberately outside the execution/LLM path. Reference sheet
and columns describe the independent finance workbook's layout, not model
input mappings. Never use the reference to select a calculation rule.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from decimal import Decimal as D, ROUND_HALF_UP
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from bonus_agent.case import load_case,sha256
from tools.compare_commission_facts import compare,LEVELS


def cents(value):
    return D(str(value)).quantize(D('.01'),rounding=ROUND_HALF_UP)


def destination(name):
    # The two report labels denote the same explicitly pending NPD role.
    return 'NPD未分配' if name in ('其他参与人员待分配','NPD未分配') else name


def put(rows,period,level,key,metric,amount,source):
    rows.append(dict(period=period,level=level,key=list(key),metric=metric,
                     amount=str(amount),source=source))


def candidate_facts(payload,period):
    b,s=payload['basis'],payload['settlement'];r=[]
    for metric,v in b['totals'].items():put(r,period,'total',[period],metric,cents(v),'commission_details.json/basis/totals/'+metric)
    project_metrics={'提成总额':'total','库存逾期暂扣':'hold','欠款暂扣':'debt','正常可分配':'alloc'}
    for key,x in b['projects'].items():
        for label,metric in project_metrics.items():put(r,period,'project',key.split('|'),label,cents(x[metric]),'commission_details.json/basis/projects/'+key+'/'+metric)
    for item in s['people']:
        put(r,period,'pending' if item['destination']=='pending' else 'person',[destination(item['name'])],'应分',cents(item['amount']),'commission_details.json/settlement/people/'+item['name'])
    for item in s['rows']:
        put(r,period,'role_raw',item['project'].split('|')+[item['role'],destination(item['name'])],'角色原值',D(item['raw']),item['source'])
    put(r,period,'rounding',[period],'人员与待分配尾差',D(s['residual']),'commission_details.json/settlement/residual')
    return r


def reference_facts(reference,period,project_sheet,summary_sheet):
    b=load_workbook(reference,read_only=True,data_only=True);r=[];notes=[]
    try:
        sheet=b[project_sheet];summary=list(b[summary_sheet].values)
        for idx,metric in [(2,'正常核算分配提成奖'),(3,'客户欠款暂扣奖金'),(4,'呆滞+逾期系数暂扣奖金'),(5,'合计')]:
            # The independently supplied reference labels this same category
            # with a Chinese comma. Normalize only punctuation, not amount.
            label=str(summary[idx][0]).strip().replace('，','').replace(',','')
            if label!=metric:raise ValueError(f'参考汇总第{idx+1}行标题变动：{summary[idx][0]!r}，需要确认独立映射')
            put(r,period,'total',[period],metric,cents(summary[idx][1]),f'{summary_sheet}!B{idx+1}')
        pm={'提成总额':25,'库存逾期暂扣':26,'欠款暂扣':28,'正常可分配':29}
        role_cols=[(30,'销售负责人'),(32,'销售执行人'),(34,'销售执行人'),(36,'商务负责人'),(38,'客户经理'),(40,'客户经理')]
        role_values=defaultdict(list)
        for i,row in enumerate(sheet.values,1):
            if str(row[0])!=period.replace('-',''):continue
            key=[str(row[c]).strip() for c in (0,1,2,5)]
            for label,col in pm.items():
                value=row[col]
                if value is None and col==26 and row[22]==1 and row[24]==1:
                    notes.append(f'{project_sheet}!{get_column_letter(col+1)}{i}：底稿为空，库存和逾期系数均为1，按规则0核对');value=0
                if value is None:raise ValueError(f'{project_sheet}!{get_column_letter(col+1)}{i} 缺实际金额')
                put(r,period,'project',key,label,cents(value),f'{project_sheet}!{get_column_letter(col+1)}{i}')
            for col,role in role_cols:
                who=row[col];amt=row[col+1]
                if who in ('',None,0):
                    if amt not in ('',None,0):raise ValueError(f'{project_sheet} 第{i}行{role} 有金额但无姓名/类别')
                    continue
                if amt is None:raise ValueError(f'{project_sheet} 第{i}行{role} 缺分配金额')
                role_values[tuple(key+[role,destination(str(who))])].append((D(str(amt)),f'{project_sheet}!{get_column_letter(col+2)}{i}'))
            role_values[tuple(key+['NPD','NPD未分配'])].append((D(str(row[43])),f'{project_sheet}!AR{i}'))
        for key,pairs in role_values.items():put(r,period,'role_raw',key,'角色原值',sum((v for v,_ in pairs),D(0)),'；'.join(loc for _,loc in pairs))
        amounts=[]
        for idx,row in enumerate(summary[9:],10):
            name=row[0]
            if not name:continue
            n=destination(str(name));value=cents(row[1]);amounts.append(value)
            put(r,period,'pending' if ('未分配' in n or '待分配' in n) else 'person',[n],'应分',value,f'{summary_sheet}!B{idx}')
        put(r,period,'rounding',[period],'人员与待分配尾差',cents(summary[2][1])-sum(amounts,D(0)),f'{summary_sheet}!B3 减逐人和待分配到分后的合计')
    finally:b.close()
    return r,notes


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--case',required=True,type=Path);ap.add_argument('--reference',required=True,type=Path)
    ap.add_argument('--out',required=True,type=Path);ap.add_argument('--period',required=True)
    ap.add_argument('--project-sheet',default='提成奖核算底稿');ap.add_argument('--summary-sheet',default='核算汇总9月结果')
    a=ap.parse_args(argv);case=load_case(a.case);rnd=case['rounds'][-1]
    if rnd['status']!='OK':raise ValueError('候选结果尚未正式完成，先冻结后比对')
    a.out.mkdir(parents=True,exist_ok=True)
    frozen=a.case/'commission_details.json'
    if rnd['outputs']['details'][1]!=sha256(frozen):raise ValueError('候选金额已变动，不能对账')
    payload=json.loads(frozen.read_text(encoding='utf8'))
    candidate=candidate_facts(payload,a.period)
    # Reference opened only AFTER the candidate digest was checked and the
    # candidate ledger was extracted. No reference is passed to an LLM.
    def write_rows(name,rows):
        p=a.out/name
        p.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in rows),encoding='utf8');return p
    left=write_rows('candidate.jsonl',candidate)
    ref,notes=reference_facts(a.reference,a.period,a.project_sheet,a.summary_sheet)
    right=write_rows('reference.jsonl',ref)
    result=compare(left,right,a.period,LEVELS,D('.01'),D('.000001'))
    result.update(candidate_details_sha256=sha256(frozen),reference_workbook_sha256=sha256(a.reference),
                  reference_blank_notes=notes,case_round=rnd['n'])
    (a.out/'comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({k:v for k,v in result.items() if k not in ('differences','reference_blank_notes')},ensure_ascii=False,indent=2))
    print('differences',len(result['differences']))
    return 0 if result['status']=='MATCH' else 1


if __name__=='__main__':raise SystemExit(main())
