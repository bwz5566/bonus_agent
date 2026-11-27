"""Versioned, model-free re-export of a finished fixed commission case.

A report format correction does not rescind or fabricate a financial
approval. Recompute the full frozen basis and settlement from the actual
sources, require exact equality, then publish a new report file alongside
the original approved round. Never overwrite the historical result.xlsx.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from bonus_agent import case as cs, commission_revision as cr
from bonus_agent.commission_calculation import calculate_basis, calculate_settlement
from bonus_agent.commission_chain import NODE_ORDER, basis_contract_digest
from bonus_agent.commission_export import REPORT_FORMAT_VERSION, report_profile, write_workbook
from bonus_agent.commission_history import history_confirmation
from bonus_agent.commission_sources import Catalog
from bonus_agent.infra.atomic import write_text
from bonus_agent.recovery import fingerprint


REPORTS='report_versions'
MANIFEST='manifest.json'


def reexport_fixed_case(out: Path,by: str):
    out=Path(out)
    if not str(by).strip():raise cs.ConfirmError('请记录实际报表修订人。')
    with cs.case_lock(out):
        case,rnd,outputs=cs._checked_round(out);state=cr.state_of(case)
        if not state or 'details' not in outputs or 'excel' not in outputs:
            raise cs.ConfirmError('仅支持已完成固定提成链路的正式结果重新导出。')
        if state.get('receipts',{}).get('settlement_review',{}).get('status')!='PASS' or state.get('node_receipts',{}).get('export',{}).get('status')!='PASS':
            raise cs.ConfirmError('原核算缺少实际人员独立复核及导出回读回执。')
        details=out/outputs['details'][0];original=out/outputs['excel'][0]
        original_case_hash=cs.sha256(out/cs.CASE_FILE)
        frozen=json.loads(details.read_text(encoding='utf8'))
        paths=[Path(x['path']) for x in rnd['source_inputs']]
        if cs.fingerprint(paths)!=state['files']:raise cs.ConfirmError('实际来源已变化，不能按旧核验金额直接导出。')
        catalog=Catalog(paths);specs=copy.deepcopy(state['specs']);rules=state.get('active_rules',[])
        overrides=cr.recipient_overrides(cr.load(out),state['period'])
        if overrides!=state.get('recipient_overrides',[]):raise cs.ConfirmError('人员修订版本已变化，须重新核算。')
        dynamic=state.get('mode')=='sales_commission_v2'
        if dynamic:
            from bonus_agent.adaptive_rules import attach_authorized_rules,attach_image_authority
            from bonus_agent.adaptive_compiler import replay_settlement
            from bonus_agent.adaptive_export import FORMAT,write_adaptive_workbook
            attach_authorized_rules(catalog,rules)
            attach_image_authority(catalog,state.get('questions',[]))
            _,basis,settlement,_=replay_settlement(catalog,state['adaptive_basis_contract'],state['adaptive_roster_contract'],state['period'],overrides=overrides)
        else:
            for node in (*NODE_ORDER[:5],'roster'):cr.apply_rule_versions(catalog,specs[node],node,rules)
            if fingerprint(specs)!=fingerprint(state['specs']):raise cs.ConfirmError('当前规则合同与已核验合同不同，不能仅修订报表。')
            source_key=fingerprint(state['files'])[:16]
            basis=calculate_basis(catalog,specs,state['period'],context={
                'history_single_month_confirmed':history_confirmation(state.get('questions',[]),state['period'],source_key),'source_key':source_key})
            cr.attach_recipient_sources(catalog,overrides)
            settlement=calculate_settlement(catalog,specs['roster'],basis,state['period'],recipient_overrides=overrides)
        if fingerprint(basis)!=state['basis_digest'] or fingerprint(basis)!=fingerprint(frozen['basis']):
            raise cs.ConfirmError('逐项目复算与原批准的实际核算不一致；须发起财务修订，不能只更改报表。')
        if fingerprint(settlement)!=fingerprint(frozen['settlement']):
            raise cs.ConfirmError('逐角色/人员复算与原正式结果不一致；须重新核验人员金额。')
        rid='report-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid4().hex[:8]
        dest=out/REPORTS/rid;dest.mkdir(parents=True,exist_ok=False)
        final_confirmation=case.get('confirmed') or {}
        meta={'id':rid,'created_by':str(by).strip(),'created_at':datetime.now().isoformat(timespec='seconds'),
              'source_round':rnd['n'],'period':state['period'],'source_calculation_profile':state['profile'],
              'source_basis_digest':state['basis_digest'],'source_basis_contract':basis_contract_digest(state),
              'source_settlement_digest':fingerprint(settlement),'source_details_sha256':cs.sha256(details),
              'source_workbook_sha256':cs.sha256(original),'source_case_sha256':original_case_hash,
              'source_total_approval':state['approval'],'source_final_confirmation':final_confirmation if final_confirmation.get('round')==rnd['n'] else None,
              'format':FORMAT if dynamic else REPORT_FORMAT_VERSION,'export_profile':cs.sha256(Path(__file__).with_name('adaptive_export.py')) if dynamic else report_profile(),'provider_calls':0,
              'financial_amounts_changed':False,'new_financial_approval':False}
        workbook=dest/'result.xlsx'
        if dynamic:
            proof=write_adaptive_workbook(workbook,basis,settlement,state,layout=state.get('adaptive_output_contract'))
            if cs.sha256(Path(__file__).with_name('adaptive_export.py'))!=meta['export_profile']:raise cs.ConfirmError('报表生成过程中导出代码发生变化，未发布新版。')
        else:
            proof=write_workbook(workbook,basis,settlement,specs,state,catalog=catalog,report_metadata=meta)
            if proof['export_profile']!=meta['export_profile']:raise cs.ConfirmError('报表生成过程中导出程序版本发生变化，未发布新版。')
        if cs.fingerprint(paths)!=state['files'] or cs.sha256(details)!=outputs['details'][1] or cs.sha256(original)!=outputs['excel'][1] or cs.sha256(out/cs.CASE_FILE)!=original_case_hash:
            raise cs.ConfirmError('报表导出期间核验版本发生变化；未发布本版报表。')
        meta.update(status='PASS',workbook='result.xlsx',workbook_sha256=cs.sha256(workbook),readback=proof,
                    totals=frozen['basis']['totals'],project_count=len(basis['projects']),role_rows=len(settlement['rows']),
                    people=settlement['checks']['people'],source_recomputed_exactly=True)
        write_text(dest/MANIFEST,json.dumps(meta,ensure_ascii=False,indent=2,default=str))
        return workbook,meta


def read_report(path):
    """Validate an explicit historical report before serving it."""
    path=Path(path);manifest=path.parent/MANIFEST
    if path.name!='result.xlsx' or not manifest.is_file() or not path.is_file():raise cs.ConfirmError('没有已核验的报表修订文件。')
    meta=json.loads(manifest.read_text(encoding='utf8'))
    if meta.get('id')!=path.parent.name or meta.get('status')!='PASS' or meta.get('workbook')!=path.name or meta.get('workbook_sha256')!=cs.sha256(path):
        raise cs.ConfirmError('报表修订文件指纹不符，请重新导出。')
    return meta


def reports_for_case(out,case):
    """Reports tied to another financial round stay visible as historical."""
    out=Path(out);root=out/REPORTS
    if not root.is_dir():return []
    last=((case or {}).get('rounds') or [{}])[-1];state=cr.state_of(case) or {};records=[]
    revision=cr.active(cr.load(out))
    for folder in sorted(root.iterdir(),reverse=True):
        if not folder.is_dir() or not (folder/MANIFEST).is_file():continue
        try:meta=read_report(folder/'result.xlsx')
        except (ValueError,OSError,cs.ConfirmError):continue
        current=(last.get('status')=='OK' and state.get('status')=='OK' and meta['source_round']==last.get('n')
                 and meta['source_basis_digest']==state.get('basis_digest') and meta['source_details_sha256']==last.get('outputs',{}).get('details',[None,None])[1]
                 and (not revision or (revision['status']=='OK' and state.get('revision_id')==revision['id'])))
        records.append({'metadata':meta,'current':bool(current),'relative':(folder/'result.xlsx').relative_to(out).as_posix()})
    return records
