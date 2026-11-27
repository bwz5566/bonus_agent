"""Write the fixed chain's financial workbook, with formula caches and readback.

Reference/expected workbooks are deliberately not accepted here. The report
contains only the current source-derived amounts, formulas and provenance.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal as D
from pathlib import Path
from tempfile import NamedTemporaryFile
from datetime import datetime
import hashlib

import xlsxwriter
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from bonus_core.engine.run import Result,NodeResult,ENGINE_VERSION
from bonus_core.engine.commission import ZERO,rounded
from bonus_agent.recovery import fingerprint
from bonus_agent.commission_report_sources import build_report_sources, inline_role_slots, REFERENCE_LABELS, REFERENCE_KINDS


REPORT_FORMAT_VERSION='commission-workpaper-v2'


def report_profile():
    paths=(Path(__file__),Path(__file__).with_name('commission_report_sources.py'))
    return hashlib.sha256(b''.join(p.read_bytes() for p in paths)).hexdigest()


def result_object(basis,settlement,state):
    nodes={}
    for key,row in basis['projects'].items():
        nid='项目:'+key
        nodes[nid]=NodeResult(nid,'项目',key,key,None,'0',str(row['payable']),str(row['alloc']),False)
    people=[{'name':x['name'],**({'person_id':x['id']} if 'id' in x else {}),'final':str(x.get('entitled',x['amount'])),'held':str(x['amount'] if x['destination']=='pending' else ZERO),
             'paid':str(x['amount'] if x['destination']=='person' else ZERO),'holds':{},'destination':x['destination'],'raw':str(x['raw']),
             **({'person_metrics':jsonable_metrics(x['metrics']),'person_deductions':jsonable_metrics(x['destinations'])} if 'metrics' in x else {})}
            for x in settlement['people']]
    revision_contract={'specs':state['specs'],'rules':state.get('active_rules',[]),'recipients':state.get('recipient_overrides',[])}
    adaptive=state.get('mode')=='sales_commission_v2'
    if adaptive:revision_contract.update(basis=state.get('adaptive_basis_contract'),roster=state.get('adaptive_roster_contract'))
    totals=[(k,str(v)) for k,v in basis['totals'].items()]
    if personal:=settlement.get('person_basis'):totals.extend(('人员层：'+k,str(v)) for k,v in personal['totals'].items())
    reconciliation={'name':'人员与待分配舍入对账','total':str(basis['sums']['alloc']),'allocated':str(settlement['allocated']),
                              'pending':str(settlement['pending']),'residual':str(settlement['residual'])}
    if bridge:=settlement.get('funding_bridge'):reconciliation['funding_bridge']=jsonable_metrics(bridge)
    return Result('提成奖','adaptive-commission-v2' if adaptive else 'fixed-commission-v1',ENGINE_VERSION,fingerprint({'files':state['files'],'contract':revision_contract,'basis':fingerprint(basis),'settlement':fingerprint(settlement),'profile':state['profile'],'revision':state.get('revision_id','')}),
                  str(basis['sums']['alloc']),str(basis['pool']),['项目'],nodes,{}, {},[],data_hash=fingerprint(state['files']),
                  totals=totals,people=people,rounding=[reconciliation],
                  rules_hash=fingerprint(revision_contract))


def jsonable_metrics(value):
    import json
    return json.loads(json.dumps(value,ensure_ascii=False,default=str))


def write_workbook(path,basis,settlement,specs,state,*,catalog,report_metadata=None):
    # All financial decisions have already been made by the verified engine.
    # The output adapter reconstructs the actual reference inputs, not a
    # second model-authored calculation or a copy of an expected workbook.
    report_inputs=build_report_sources(catalog,basis,specs,state['period'])
    slots=inline_role_slots(settlement)
    project_rows={key:i for i,key in enumerate(sorted(basis['projects']),2)}
    row_end=len(project_rows)+1
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with NamedTemporaryFile(dir=path.parent,suffix='.xlsx',delete=False) as temp:
        target=Path(temp.name)
    try:
        with xlsxwriter.Workbook(str(target),{'strings_to_formulas':False,'strings_to_urls':False}) as book:
            project=book.add_worksheet('提成奖核算底稿');summary=book.add_worksheet('核算汇总')
            params=book.add_worksheet('参数与来源');roles=book.add_worksheet('角色分配') if settlement else None
            sources=book.add_worksheet('数据追溯');receipts=book.add_worksheet('节点验收')
            versions=book.add_worksheet('版本与审批')
            project_source=book.add_worksheet('项目来源')
            reference_data=book.add_worksheet('参考数据明细')
            history_months=book.add_worksheet('历史出货月汇总')
            fmt_h=book.add_format({'bold':True,'bg_color':'#214963','font_color':'white','text_wrap':True,'valign':'vcenter'})
            fmt_money=book.add_format({'num_format':'#,##0.00;[Red](#,##0.00);0.00'})
            def money_at(digits):
                mask='#,##0'+('.'+'0'*digits if digits else '')
                return book.add_format({'num_format':mask+';[Red]('+mask+');'+('0.'+'0'*digits if digits else '0')})
            fmt_project=money_at(specs['hold_policy']['project_round_digits'])
            fmt_person=money_at(specs['roster']['person_round_digits']) if settlement else fmt_money
            fmt_tail=money_at(max(specs['hold_policy']['project_round_digits'],specs['roster']['person_round_digits'])) if settlement else fmt_money
            fmt_prec=book.add_format({'num_format':'0.00000000'})
            fmt_rate=book.add_format({'num_format':'0.0000%'})
            fmt_date=book.add_format({'num_format':'yyyy-mm-dd'})
            def header(ws,labels,widths=None):
                ws.write_row(0,0,labels,fmt_h);ws.set_row(0,36);ws.freeze_panes(1,1)
                for i,w in enumerate(widths or [18]*len(labels)):ws.set_column(i,i,w)
                ws.hide_gridlines(2)
            def value(ws,r,c,v,fmt=None):
                if v is None:ws.write_blank(r,c,None,fmt)
                elif isinstance(v,D):ws.write_number(r,c,float(v),fmt)
                elif isinstance(v,bool):ws.write_boolean(r,c,v,fmt)
                else:ws.write(r,c,v,fmt)
            def formula(ws,r,c,expr,cached,fmt=None):
                if isinstance(cached,D):cached=float(cached)
                if cached is None:cached=''
                ws.write_formula(r,c,expr,fmt,cached)
            header(params,['计算节点','路线/指标','适用','下限','费率/系数','含下界','原文区间','来源位置','原文值'],[18,18,15,18,18,12,32,65,25])
            pr=1; ranges={}; param_cells={}
            for item in basis['parameter_evidence']:
                key=('tier',item['route'],item['age']) if 'route' in item else ('coef',item['indicator'])
                refs=[]
                for band in item['steps']:
                    label=item.get('route',item.get('indicator'))
                    params.write_row(pr,0,[key[0],label,item.get('age','')]);value(params,pr,3,band['lower'],fmt_prec);value(params,pr,4,band['rate'],fmt_prec)
                    value(params,pr,5,band['inclusive']);value(params,pr,6,band['label']);value(params,pr,7,band['source']['band']+'；'+band['source']['rate']);value(params,pr,8,band['source']['raw'])
                    refs.append((f"'参数与来源'!$D${pr+1}",f"'参数与来源'!$E${pr+1}",band['inclusive']))
                    pr+=1
                ranges[key]=refs
            for item,entries in [('currency_offset',{'value':specs['project_policy']['currency']['offset'],'source':specs['project_policy']['evidence']['currency']}),
                                 ('age_threshold',{'value':specs['project_policy']['age']['months'],'source':specs['project_policy']['evidence']['age']}),
                                 ('project_digits',{'value':specs['hold_policy']['project_round_digits'],'source':specs['hold_policy']['evidence']['project_round']})]:
                params.write_row(pr,0,['政策',item]);value(params,pr,4,D(str(entries['value'])));value(params,pr,7,str(entries['source']));param_cells[item]=f"'参数与来源'!$E${pr+1}";pr+=1
            for k,v in specs['coefficient_policy']['defaults'].items():
                params.write_row(pr,0,['默认系数',k]);value(params,pr,4,D(str(v)));param_cells['default_'+k]=f"'参数与来源'!$E${pr+1}";pr+=1
            if settlement:
                for item in settlement['parameter_evidence']:
                    for role,info in item['rates'].items():
                        params.write_row(pr,0,['角色',item['route'],item['age']]);value(params,pr,3,role);value(params,pr,4,info['rate'],fmt_rate);value(params,pr,7,info['source']);value(params,pr,8,info['raw']);
                        param_cells[('role',item['route'],item['age'],role)]=f"'参数与来源'!$E${pr+1}";pr+=1
                params.write_row(pr,0,['政策','person_digits']);value(params,pr,4,specs['roster']['person_round_digits']);param_cells['person_digits']=f"'参数与来源'!$E${pr+1}";pr+=1
            headers=['项目编号','月份','客户编码','客户简称2','产品型号','产品系列','结算币','汇率','销售区域','首发日期','新老项目','销售金额(本位币)','计算基数','阶梯提成','最终提成比例','区域实际销售','区域预算','区域达成比例','区域达成系数','客户实际销售','客户预算','客户达成比例','客户达成系数','毛利率','毛利系数','历史销售合计','实际出货月份数','月平均出货','呆滞库存金额','呆滞库存比例','库存系数','逾期应收金额','逾期应收款比例','逾期系数','提成总额','呆滞+逾期暂扣','应发提成总额','欠款标记','欠款不发放','分配提成金额','项目舍入差额','阶梯来源路线']
            route_col=len(headers)-1;role_start=len(headers)
            for _,_,name,amount in slots:headers.extend((name,amount))
            note_col=len(headers);match_col=note_col+1;total_col=note_col+2;check_col=note_col+3
            headers.extend(['未回款备注','匹配列（项目编号）','角色金额合计','角色对账差额'])
            if len(headers)>16384:raise ValueError('本批角色人数超过 Excel 主表可容纳的列数，不能静默截断实名分配。')
            widths=[55,14,15,24,33,16,10,12,25,15]+[18]*(role_start-10)+[20,18]*len(slots)+[36,55,20,20]
            header(project,headers,widths)
            header(sources,['项目编号','指标源','原始物理位置'],[55,24,100])
            header(project_source,['项目编号','销售区域原值','适用阶梯路线','客户归一键','当期销售源行数','销售来源示例（全量见数据追溯）'],[55,25,23,27,20,90])
            for key,n in project_rows.items():
                p=report_inputs['projects'][key]
                project_source.write_row(n-1,0,[key,p['region'],p['route'],p['customer_key'],p['sales_count'],'；'.join(p['sales_examples'])])
            region_keys=f"'项目来源'!$B$2:$B${row_end}";customer_keys=f"'项目来源'!$D$2:$D${row_end}"
            sales_range=f"'提成奖核算底稿'!$L$2:$L${row_end}"
            header(reference_data,['指标类型','匹配对象','核算月份','原始金额/比例','原文状态','实际来源位置'],[20,55,16,23,35,85])
            ref_r=1;ref_ranges={}
            def write_reference_group(group,kind,entity,records):
                nonlocal ref_r
                if not records:return
                start=ref_r+1
                for record in records:
                    reference_data.write_row(ref_r,0,[REFERENCE_LABELS[kind],entity,record['period']])
                    value(reference_data,ref_r,3,record.get('amount'),fmt_rate if kind=='gross' else fmt_prec)
                    value(reference_data,ref_r,4,record.get('status'))
                    value(reference_data,ref_r,5,record['where']);ref_r+=1
                ref_ranges[group]=(start,ref_r)
            for kind in REFERENCE_KINDS:
                for (what,entity),records in sorted(report_inputs['groups'].items()):
                    if what==kind:write_reference_group((kind,entity),kind,entity,records)
            for customer,months in sorted(report_inputs['history'].items()):
                for when,records in sorted(months.items()):write_reference_group(('history',customer,when),'history',customer,records)
            for key,records in sorted(report_inputs['debt'].items()):write_reference_group(('unpaid',key),'unpaid',key,records)
            def ref_range(group,col='D'):
                start,end=ref_ranges[group];return f"'参考数据明细'!${col}${start}:${col}${end}"
            def reference_formula(kind,entity):
                group=(kind,entity)
                if group not in ref_ranges:return '=""'
                rng=ref_range(group)
                if report_inputs['policies'][kind]=='sum':return f'=IF(COUNT({rng})=0,"",SUM({rng}))'
                # The verified unique budget/margin can have identical duplicate
                # records. Display all; conflicting edits must not average them.
                return f'=IF(COUNT({rng})=0,"",IF(MIN({rng})=MAX({rng}),MAX({rng}),NA()))'
            def frozen_formula(key):
                group=('unpaid',key)
                if group not in ref_ranges:return '="正常"'
                if specs['hold_policy']['freeze_source']=='list':
                    test=f'COUNTA({ref_range(group,"B")})>0'
                else:
                    statuses=specs['hold_policy']['freeze_statuses'];rng=ref_range(group,'E')
                    terms=['SUMPRODUCT(--EXACT('+rng+',"'+str(x).replace('"','""')+'"))' for x in statuses]
                    test='('+ '+'.join(terms)+')>0'
                return f'=IF({test},"欠款","正常")'
            header(history_months,['客户归一键','实际出货月份','实际销售额','有效出货月份标志','来源/范围'],[28,18,23,20,75])
            hist_r=1;hist_ranges={}
            for customer in report_inputs['customers']:
                start=hist_r+1
                for when,records in sorted(report_inputs['history'].get(customer,{}).items()):
                    n=hist_r+1;rng=ref_range(('history',customer,when))
                    history_months.write_row(hist_r,0,[customer,when])
                    formula(history_months,hist_r,2,f'=SUM({rng})',sum((r['amount'] for r in records),ZERO),fmt_prec)
                    formula(history_months,hist_r,3,f'=--(SUMPRODUCT(--ISNUMBER({rng}),--({rng}<>0))>0)',1)
                    value(history_months,hist_r,4,rng);hist_r+=1
                n=hist_r+1;history_months.write_row(hist_r,0,[customer,state['period']])
                formula(history_months,hist_r,2,f'=SUMPRODUCT(--EXACT({customer_keys},A{n}),{sales_range})',report_inputs['current_sales'][customer],fmt_prec)
                formula(history_months,hist_r,3,f'=--(SUMPRODUCT(--EXACT({customer_keys},A{n}))>0)',1)
                value(history_months,hist_r,4,'当期实际销售：按项目来源客户与主表销售金额重新汇总');hist_r+=1
                hist_ranges[customer]=(start,hist_r)
            if ref_r>1:reference_data.autofilter(0,0,ref_r-1,5)
            history_months.autofilter(0,0,hist_r-1,4)
            project_source.autofilter(0,0,row_end-1,5)
            source_r=1
            def coef_expr(cell,refs,default):
                expr=refs[0][1]
                for lo,rate,inc in refs[1:]:expr=f'IF({cell}{">=" if inc else ">"}{lo},{rate},{expr})'
                return f'IF(ISNUMBER({cell}),{expr},{default})'
            for i,(key,row) in enumerate(sorted(basis['projects'].items()),1):
                n=i+1
                for c,k in enumerate(('key','month','code','customer','product','series','currency','fx')):value(project,i,c,row[k],fmt_prec if k=='fx' else None)
                formula(project,i,8,f"='项目来源'!B{n}",row['region'])
                project.write_datetime(i,9,datetime.fromisoformat(row['first_date']),fmt_date)
                ageop='>' if specs['project_policy']['age']['old_if']=='gt' else '>='
                formula(project,i,10,f'=IF((VALUE(LEFT(B{n},4))-YEAR(J{n}))*12+VALUE(RIGHT(B{n},2))-MONTH(J{n}){ageop}{param_cells["age_threshold"]},"老项目","新项目")','老项目' if row['old'] else '新项目')
                value(project,i,11,row['sales'],fmt_money)
                currency=str(specs['project_policy']['currency']['code']).replace('"','""')
                formula(project,i,12,f'=IF(G{n}="{currency}",L{n}/H{n}*(H{n}-{param_cells["currency_offset"]}),L{n})',row['base'],fmt_money)
                def tier_for(age):
                    refs=ranges[('tier',row['route'],age)]
                    if specs['project_policy']['tier']['method']=='progressive':
                        terms=[f'MAX(0,MIN(M{n},{refs[j+1][0] if j+1<len(refs) else f"M{n}"})-{lo})*{rate}' for j,(lo,rate,inc) in enumerate(refs)]
                        tier='+'.join(terms)
                    else:tier=f'M{n}*'+coef_expr(f'M{n}',refs,refs[0][1])
                    negative=f'M{n}*{refs[0][1]}' if specs['project_policy']['tier']['negative']=='signed' else '0'
                    return f'IF(M{n}<0,{negative},{tier})'
                formula(project,i,13,f'=IF(K{n}="老项目",{tier_for("old")},{tier_for("new")})',row['tier'],fmt_money)
                formula(project,i,14,f'=IF(M{n}=0,"",N{n}/M{n})',row['tier']/row['base'] if row['base'] else None,fmt_rate)
                customer=row['customer'].casefold();sales_col='L' if specs['coefficient_policy']['achievement_amount']=='actual' else 'M'
                amount_range=f'${sales_col}$2:${sales_col}${row_end}'
                formula(project,i,15,f'=SUMPRODUCT(--EXACT({region_keys},I{n}),{amount_range})',row['region_actual'],fmt_money)
                formula(project,i,19,f"=SUMPRODUCT(--EXACT({customer_keys},'项目来源'!D{n}),{amount_range})",row['customer_actual'],fmt_money)
                for c,k,entity in [(16,'region_budget',row['region']),(20,'customer_budget',customer),(23,'gross',key),(28,'stock',customer),(31,'overdue',customer)]:
                    formula(project,i,c,reference_formula(k,entity),row[k],fmt_rate if k=='gross' else fmt_money)
                hs,he=hist_ranges[customer]
                formula(project,i,25,f"=SUM('历史出货月汇总'!$C${hs}:$C${he})",row['history_sum'],fmt_money)
                hist_count=f"=SUM('历史出货月汇总'!$D${hs}:$D${he})" if specs['coefficient_policy']['history_months']=='nonzero' else f'=VALUE(RIGHT(B{n},2))'
                formula(project,i,26,hist_count,row['history_months'])
                for c,expr,k in [(17,f'IF(OR(NOT(ISNUMBER(Q{n})),Q{n}=0),"",P{n}/Q{n})','region_ratio'),(21,f'IF(OR(NOT(ISNUMBER(U{n})),U{n}=0),"",T{n}/U{n})','customer_ratio'),
                                   (27,f'IF(AA{n}=0,"",Z{n}/AA{n})','history_average'),(29,f'IF(OR(NOT(ISNUMBER(AC{n})),AB{n}=0),"",AC{n}/AB{n})','stock_ratio'),(32,f'IF(OR(NOT(ISNUMBER(AF{n})),AB{n}=0),"",AF{n}/AB{n})','overdue_ratio')]:
                    formula(project,i,c,'='+expr,row[k],fmt_money if k=='history_average' else fmt_rate)
                for c,letter,k,table,default in [(18,'R','region_factor','region','region'),(22,'V','customer_factor','customer','customer'),(24,'X','gross_factor','gross:'+row['route'],'gross'),(30,'AD','stock_factor','stock','stock'),(33,'AG','overdue_factor','overdue','overdue')]:
                    formula(project,i,c,'='+coef_expr(f'{letter}{n}',ranges[('coef',table)],param_cells['default_'+default]),row[k],fmt_prec)
                formula(project,i,34,f'=N{n}*S{n}*W{n}*Y{n}',row['total'],fmt_money)
                hold=f'AI{n}*(1-AE{n})+AI{n}*(1-AH{n})' if specs['hold_policy']['hold_method']=='additive' else f'AI{n}*(1-AE{n}*AH{n})'
                formula(project,i,35,'='+hold,row['hold'],fmt_money)
                formula(project,i,36,f'=AI{n}-AJ{n}',row['total']-row['hold'],fmt_money)
                formula(project,i,37,frozen_formula(key),'欠款' if row['frozen'] else '正常')
                formula(project,i,38,f'=IF(AL{n}="欠款",AK{n},0)',row['debt'],fmt_money)
                formula(project,i,39,f'=ROUND(AK{n}-AM{n},{param_cells["project_digits"]})',row['alloc'],fmt_project)
                formula(project,i,40,f'=AN{n}-(AK{n}-AM{n})',row['round_delta'],fmt_prec)
                formula(project,i,route_col,f"='项目来源'!C{n}",row['route'])
                value(project,i,note_col,'；'.join(dict.fromkeys(r['status'] for r in report_inputs['debt'].get(key,[]) if r['status'])))
                formula(project,i,match_col,f'=B{n}&"|"&C{n}&"|"&D{n}&"|"&E{n}',key)
                for label,locations in row['sources'].items():
                    # One row per physical source, no Excel single-cell limit.
                    for loc in locations:
                        sources.write_row(source_r,0,[key,label,loc]);source_r+=1
            project.autofilter(0,0,row_end-1,len(headers)-1)
            header(summary,['核算类型 / 分配人员','本期金额',f'核算月份：{state["period"][:4]}-{state["period"][4:]}'],[41,23,68])
            for idx,(k,col) in enumerate([('正常核算分配提成奖','AN'),('客户欠款暂扣奖金','AM'),('呆滞+逾期系数暂扣奖金','AJ'),('合计','AI')],1):
                summary.write(idx,0,k);formula(summary,idx,1,f"=ROUND(SUM('提成奖核算底稿'!{col}2:{col}{row_end}),2)",basis['totals'][k],fmt_money)
            summary.write(5,0,'项目舍入原值差额');formula(summary,5,1,f"=SUM('提成奖核算底稿'!AO2:AO{row_end})",basis['sums']['round_delta'],fmt_prec)
            if settlement:
                header(roles,['项目编号','角色','姓名/待分配','去向','项目分配金额','制度角色比例','个人份额','角色原始金额','结算汇总输入','名单来源'],[55,18,23,18,20,17,17,22,22,70])
                role_sum=defaultdict(lambda:ZERO)
                role_records=defaultdict(list)
                for i,row in enumerate(settlement['rows'],1):
                    n=i+1;p=basis['projects'][row['project']];pn=project_rows[row['project']]
                    roles.write_row(i,0,[row['project'],row['role'],row['name'],row['destination']]);formula(roles,i,4,f"='提成奖核算底稿'!AN{pn}",p['alloc'],fmt_money)
                    old_rate=param_cells[('role',p['route'],'old',row['role'])];new_rate=param_cells[('role',p['route'],'new',row['role'])]
                    formula(roles,i,5,f"=IF('提成奖核算底稿'!K{pn}=\"老项目\",{old_rate},{new_rate})",row['role_rate'],fmt_rate)
                    value(roles,i,6,row['share'],fmt_rate);formula(roles,i,7,f'=E{n}*F{n}*G{n}',row['raw'],fmt_prec)
                    is_per=specs['roster']['person_rounding']=='per_record';digits=specs['roster']['person_round_digits'];settle_value=rounded(row['raw'],digits) if is_per else row['raw']
                    formula(roles,i,8,f'=ROUND(H{n},{param_cells["person_digits"]})' if is_per else f'=H{n}',settle_value,fmt_prec)
                    value(roles,i,9,row['source']);role_sum[row['project']]+=row['raw'];role_records[(row['project'],row['role'])].append((n,row))
                rend=len(settlement['rows'])+1;roles.autofilter(0,0,rend-1,9)
                for key,n in project_rows.items():
                    amount_cells=[]
                    for slot,(role,ordinal,_,_) in enumerate(slots):
                        column=role_start+slot*2;records=role_records[(key,role)]
                        if ordinal<len(records):
                            r,contribution=records[ordinal]
                            formula(project,n-1,column,f"='角色分配'!C{r}",contribution['name'])
                            formula(project,n-1,column+1,f"=$AN{n}*'角色分配'!$F${r}*'角色分配'!$G${r}",contribution['raw'],fmt_project)
                        else:
                            formula(project,n-1,column,'=""',None)
                            formula(project,n-1,column+1,'=""',None,fmt_project)
                        amount_cells.append(f'{get_column_letter(column+2)}{n}')
                    formula(project,n-1,total_col,'=SUM('+','.join(amount_cells)+')',role_sum[key],fmt_prec)
                    formula(project,n-1,check_col,f'={get_column_letter(total_col+1)}{n}-AN{n}',role_sum[key]-basis['projects'][key]['alloc'],fmt_prec)
                idx=8;summary.write_row(idx-1,0,['分配人员/类别','分配金额','资金去向'],fmt_h)
                for item in settlement['people']:
                    n=idx+1;summary.write(idx,0,item['name']);value(summary,idx,2,'实名人员' if item['destination']=='person' else '待分配')
                    # EXACT avoids wildcard matching and merging distinct
                    # same-spelling/case-sensitive financial recipient keys.
                    formula(summary,idx,1,f"=ROUND(SUMPRODUCT(--EXACT('角色分配'!$C$2:$C${rend},A{n}),'角色分配'!$I$2:$I${rend}),{param_cells['person_digits']})",item['amount'],fmt_person);idx+=1
                people_end=idx;named_row=idx+1;summary.write(idx,0,'实名人员合计')
                formula(summary,idx,1,f'=SUMIF(C9:C{people_end},"实名人员",B9:B{people_end})',settlement['allocated'],fmt_person);idx+=1
                pending_row=idx+1;summary.write(idx,0,'待分配合计')
                formula(summary,idx,1,f'=SUMIF(C9:C{people_end},"待分配",B9:B{people_end})',settlement['pending'],fmt_person);idx+=1
                tail_row=idx+1;summary.write(idx,0,'舍入尾差（不转加个人）')
                formula(summary,idx,1,f"=SUM('提成奖核算底稿'!AN2:AN{row_end})-B{named_row}-B{pending_row}",settlement['residual'],fmt_tail);idx+=1
                closed_row=idx+1;summary.write(idx,0,'分配合计（含待分配及尾差）')
                formula(summary,idx,1,f'=SUM(B{named_row}:B{tail_row})',basis['sums']['alloc'],fmt_tail);idx+=1
                summary.write(idx,0,'可分配金额核对差额');formula(summary,idx,1,f'=ROUND(B{closed_row},2)-B2',ZERO,fmt_money);idx+=1
                summary.write(idx,0,'分类暂扣与总额核对差额')
                formula(summary,idx,1,'=ROUND(B5-SUM(B2:B4),2)',basis['totals']['合计']-sum((basis['totals'][k] for k in ('正常核算分配提成奖','客户欠款暂扣奖金','呆滞+逾期系数暂扣奖金')),ZERO),fmt_money)
            else:summary.write(7,0,'总额等待财务确认，尚未计算人员')
            header(receipts,['节点','状态','独立复核','验收意见/证据指纹'],[25,14,25,100])
            for i,(k,r) in enumerate(state['receipts'].items(),1):receipts.write_row(i,0,[k,r.get('status'),r.get('reviewer'),str(r.get('review',''))[:32000]])
            from bonus_agent.commission_revision import receipt_token
            header(versions,['版本字段','实际值'],[32,100])
            approval=state.get('approval') or {}
            entries=[('财务修订版本',state.get('revision_id') or '初始版本'),('项目审批版本',state.get('basis_revision') or '初始版本'),
                     ('项目逐项金额指纹',state.get('basis_digest','')),('本版确认凭据',receipt_token(state)),('执行程序指纹',state.get('profile','')),
                     ('总额审批人',approval.get('by','尚未确认')),('总额确认时间',approval.get('at','')),('人员修订记录',str(state.get('recipient_overrides',[]))),
                     ('适用规则版本',str(state.get('active_rules',[]))),('报表格式版本',REPORT_FORMAT_VERSION),('导出程序指纹',report_profile())]
            if report_metadata:
                entries.extend([('报表修订编号',report_metadata.get('id','')),('原核算轮次',report_metadata.get('source_round','')),
                                ('报表修订说明','仅调整报表结构及公式，原项目和人员金额逐项复算一致；未产生新的财务审批'),
                                ('原正式结果文件指纹',report_metadata.get('source_workbook_sha256','')),
                                ('原最终财务确认',str(report_metadata.get('source_final_confirmation') or '尚未最终确认'))])
            for i,(name,content) in enumerate(entries,1):versions.write_row(i,0,[name,str(content)[:32000]])
        # A formula cache is generated by the deterministic calculation, not
        # by an external spreadsheet program. Check both cached values and
        # existence of formulas before atomic publication.
        book=load_workbook(target,read_only=True,data_only=True)
        try:
            pr=list(book['提成奖核算底稿'].values)[1:]
            from decimal import Decimal
            observed={row[0]:Decimal(str(row[39])) for row in pr}
            if len(pr)!=len(observed) or observed!={k:v['alloc'] for k,v in basis['projects'].items()}:raise ValueError('Excel 项目金额回读不一致')
            def equal(actual,expected):
                if expected is None:return actual in (None,'')
                if isinstance(expected,(D,int)):
                    try:return abs(D(str(actual))-D(str(expected)))<=D('0.000001')
                    except (ValueError,ArithmeticError):return False
                return actual==expected
            source_fields={8:'region',15:'region_actual',16:'region_budget',19:'customer_actual',20:'customer_budget',23:'gross',25:'history_sum',26:'history_months',28:'stock',31:'overdue'}
            for row in pr:
                key=row[0];p=basis['projects'][key]
                if any(not equal(row[c],p[k]) for c,k in source_fields.items()):raise ValueError(f'{key} Excel 参考数据公式回读不一致')
                if row[37]!=('欠款' if p['frozen'] else '正常') or row[route_col]!=p['route'] or row[match_col]!=key:raise ValueError(f'{key} Excel 来源、路线或项目键回读不一致')
                if settlement:
                    for slot,(role,ordinal,_,_) in enumerate(slots):
                        column=role_start+slot*2;records=role_records[(key,role)]
                        if ordinal<len(records):
                            _,item=records[ordinal]
                            if row[column]!=item['name'] or not equal(row[column+1],item['raw']):raise ValueError(f'{key}/{role} 主表人员明细回读不一致')
                        elif row[column] not in ('',None) or row[column+1] not in ('',None):raise ValueError(f'{key}/{role} 主表多出未核验的人员明细')
                    if not equal(row[total_col],p['alloc']) or not equal(row[check_col],ZERO):raise ValueError(f'{key} 主表角色金额未闭合')
            sum_rows=list(book['核算汇总'].values)
            if {r[0]:Decimal(str(r[1])) for r in sum_rows[1:5]}!=basis['totals']:raise ValueError('Excel 核算总额回读不同')
            if settlement:
                out_people={r[0]:Decimal(str(r[1])) for r in sum_rows[8:8+len(settlement['people'])]}
                if out_people!={x['name']:x['amount'] for x in settlement['people']}:raise ValueError('Excel 人员及待分配回读不同')
                totals_by_label={r[0]:r[1] for r in sum_rows if r[0]}
                for label,amount in [('实名人员合计',settlement['allocated']),('待分配合计',settlement['pending']),('舍入尾差（不转加个人）',settlement['residual']),('分配合计（含待分配及尾差）',basis['sums']['alloc']),('可分配金额核对差额',ZERO)]:
                    if not equal(totals_by_label.get(label),amount):raise ValueError(f'Excel 汇总 {label} 回读不同')
            if book['版本与审批']['B2'].value!=(state.get('revision_id') or '初始版本') or book['版本与审批']['B4'].value!=state.get('basis_digest',''):
                raise ValueError('Excel 核算版本回读不同')
        finally:book.close()
        formulas=load_workbook(target,read_only=True,data_only=False)
        try:
            expected_columns={8,10}|set(range(12,41))|{route_col,match_col}
            if settlement:expected_columns|=set(range(role_start,note_col))|{total_col,check_col}
            for n,row in enumerate(formulas['提成奖核算底稿'].iter_rows(min_row=2),2):
                if any(row[c].data_type!='f' for c in expected_columns):raise ValueError(f'Excel 项目第{n}行必需计算/人员字段缺公式')
            sum_formula_rows=list(formulas['核算汇总'].values)
            if not all(isinstance(sum_formula_rows[n][1],str) and sum_formula_rows[n][1].startswith('=') for n in range(1,5)):raise ValueError('Excel 汇总缺公式')
            if settlement:
                calc_labels={'实名人员合计','待分配合计','舍入尾差（不转加个人）','分配合计（含待分配及尾差）','可分配金额核对差额'}
                if {row[0] for row in sum_formula_rows if row[0] in calc_labels and isinstance(row[1],str) and row[1].startswith('=')}!=calc_labels:raise ValueError('Excel 人员/待分配合计缺公式')
                if formulas['角色分配']['H2'].data_type!='f':raise ValueError('Excel 角色明细缺公式')
        finally:formulas.close()
        target.replace(path)
        return {'format':REPORT_FORMAT_VERSION,'export_profile':report_profile(),'project_columns':len(headers),'role_slots':len(slots),'reference_records':ref_r-1,'history_month_rows':hist_r-1}
    finally:
        target.unlink(missing_ok=True)
