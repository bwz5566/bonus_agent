"""Financial revisions of the fixed commission chain.

The ledger is authoritative even if a process dies between saving the ledger,
the case and the execution checkpoint. An objection is NOT an authorization
to change a business rule. Typed amendments are actual, scoped human decisions;
only the existing deterministic operators may consume them.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from bonus_agent import case as cs
from bonus_agent.infra.atomic import write_text
from bonus_agent.recovery import fingerprint


FILE = 'commission_revisions.json'
CURRENT_FINANCIAL_FILES = {'总额预核算.xlsx','result.xlsx','engine_results.json','results.json','commission_details.json','expected_check.txt','修订差异.xlsx','revision_diff.json'}
BASIS_NODES = ('scope_mapping', 'reference_mapping', 'project_policy',
               'coefficient_policy', 'hold_policy')
BINDINGS = BASIS_NODES + ('roster',)
OWNERS = {
    'scope_mapping': '销售、客户编码、项目键、首发、毛利与冻结项目匹配',
    'reference_mapping': '历史销售、预算、库存与逾期源数据',
    'project_policy': '基数、汇率、新老、阶梯及区域规则',
    'coefficient_policy': '达成、毛利、库存、逾期系数及月均规则',
    'hold_policy': '冻结、暂扣、项目金额舍入',
    'basis_review': '总额或项目金额待定位',
    'roster': '角色、名单、份额及人员舍入规则',
    'settlement': '指定项目角色的人员名单与份额',
    'settlement_review': '人员金额待定位',
    'export': '报表回读与重新导出',
}
KINDS = {'discrepancy': '金额或口径异议', 'rule_change': '正式规则变更',
         'material_correction': '补充或更正材料', 'recipient_change': '指定项目人员变更',
         'report_change': '报表核验'}
ROLES = ('销售负责人', '销售执行人', '商务负责人', '客户经理', 'NPD')

# These are semantic slots of the existing engine, not this month's values.
# A new kind of formula is an operator extension, not an arbitrary prompt.
# (node, path, evidence slot, UI label, permitted type/values)
FIELDS = {
    'age_months': ('project_policy', 'age.months', 'age', '新老分界月数', 'months'),
    'age_boundary': ('project_policy', 'age.old_if', 'age', '老项目边界', ('gt', 'ge')),
    'currency_offset': ('project_policy', 'currency.offset', 'currency', '汇率调整额', 'decimal'),
    'tier_method': ('project_policy', 'tier.method', 'tier', '阶梯计算方式', ('progressive', 'whole')),
    'tier_negative': ('project_policy', 'tier.negative', 'negative', '负基数处理', ('signed', 'zero')),
    'achievement_amount': ('coefficient_policy', 'achievement_amount', 'achievement', '达成分子', ('actual', 'base')),
    'history_months': ('coefficient_policy', 'history_months', 'history', '月均分母', ('nonzero', 'elapsed')),
    'budget_duplicates': ('coefficient_policy', 'budget_duplicates', 'budget_default', '预算重复处理', ('unique', 'sum')),
    'region_default': ('coefficient_policy', 'defaults.region', 'budget_default', '区域默认系数', 'factor'),
    'customer_default': ('coefficient_policy', 'defaults.customer', 'budget_default', '客户默认系数', 'factor'),
    'margin_default': ('coefficient_policy', 'defaults.gross', 'margin_default', '毛利默认系数', 'factor'),
    'stock_default': ('coefficient_policy', 'defaults.stock', 'stock_default', '库存默认系数', 'factor'),
    'overdue_default': ('coefficient_policy', 'defaults.overdue', 'overdue_default', '逾期默认系数', 'factor'),
    'hold_method': ('hold_policy', 'hold_method', 'hold', '暂扣算法', ('additive', 'multiplicative')),
    'project_round_digits': ('hold_policy', 'project_round_digits', 'project_round', '项目舍入位数', 'digits'),
    'default_share': ('roster', 'default_share', 'default_share', '空名单比例', 'share'),
    'person_rounding': ('roster', 'person_rounding', 'rounding', '人员舍入时点', ('after_sum', 'per_record')),
    'person_round_digits': ('roster', 'person_round_digits', 'rounding', '人员舍入位数', 'digits'),
}
VALUE_LABELS = {'gt': '大于', 'ge': '大于等于', 'progressive': '超额累进', 'whole': '全额阶梯',
                'signed': '保留负值', 'zero': '负值归零', 'actual': '实际销售额', 'base': '计算基数',
                'nonzero': '有效销售月份', 'elapsed': '年初至当期的全部月份',
                'unique': '唯一预算记录', 'sum': '预算明细求和', 'additive': '分别扣减后相加',
                'multiplicative': '乘剩余系数', 'after_sum': '人员汇总后舍入', 'per_record': '逐条舍入'}
MONEY = ('total', 'hold', 'debt', 'payable', 'alloc', 'round_delta')
INDICATORS = ('sales', 'base', 'fx', 'age', 'tier', 'region_actual', 'region_budget', 'region_ratio',
              'region_factor', 'customer_actual', 'customer_budget', 'customer_ratio', 'customer_factor',
              'gross', 'gross_factor', 'history_sum', 'history_months', 'history_average', 'stock',
              'stock_ratio', 'stock_factor', 'overdue', 'overdue_ratio', 'overdue_factor') + MONEY


class RevisionError(ValueError):
    pass


def _plain(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _write(path, value):
    write_text(Path(path), json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _read(path, default):
    return json.loads(Path(path).read_text(encoding='utf-8')) if path and Path(path).is_file() else copy.deepcopy(default)


def now():
    return datetime.now().isoformat(timespec='seconds')


def state_of(case):
    return (case or {}).get('workspace', {}).get('workflow', {}).get('fixed_commission')


def load(out):
    return _read(Path(out)/FILE if out else None, {'version': 1, 'revisions': [], 'case_rules': [], 'recipients': []})


def active(ledger):
    return ledger.get('revisions', [])[-1] if ledger.get('revisions') else None


def active_id(ledger):
    return (active(ledger) or {}).get('id', '')


def _get(obj, path):
    for part in path.split('.'):
        obj = obj[part]
    return obj


def _set(obj, path, value):
    parts = path.split('.')
    for part in parts[:-1]:
        obj = obj.setdefault(part, {})
    obj[parts[-1]] = value


def normalize_value(field, value):
    if field not in FIELDS:
        raise RevisionError('该规则尚无固定算子字段；请补充实际规则材料并发起复核，必要时扩展计算算子。')
    kind = FIELDS[field][-1]
    raw = str(value).strip()
    if isinstance(kind, tuple):
        if raw in kind:
            return raw
        label = [v for v in kind if VALUE_LABELS.get(v) == raw]
        if len(label) == 1:
            return label[0]
        raise RevisionError('可选新口径：' + '、'.join(f'{v}（{VALUE_LABELS.get(v, v)}）' for v in kind))
    try:
        if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)%?', raw.replace('％','%')):
            raise InvalidOperation()
        n = Decimal(raw[:-1])/100 if kind == 'share' and raw.endswith(('%', '％')) else Decimal(raw)
        if not n.is_finite():
            raise InvalidOperation()
        if kind in ('months', 'digits'):
            if n != n.to_integral_value() or not 0 <= n <= (1200 if kind == 'months' else 6):
                raise InvalidOperation()
            return int(n)
        if (kind == 'share' and not 0 <= n <= 1) or (kind == 'factor' and n < 0):
            raise InvalidOperation()
        return str(n)
    except (InvalidOperation, ValueError, OverflowError):
        raise RevisionError(f'{FIELDS[field][3]} 的新值不合法：{raw!r}；请填写明确数值及单位。') from None


def normalize_month(value):
    value = str(value or '').replace('-', '').strip()
    if not re.fullmatch(r'\d{4}(0[1-9]|1[0-2])', value):
        raise RevisionError('规则生效月份必须为 YYYY-MM。')
    return value


def _applicable(version, period):
    return (not version.get('from') or version['from'] <= period) and (not version.get('to') or period <= version['to'])


def period_months(period):
    from bonus_core.data.period import Period
    p=Period.parse(period)
    if p is None:raise RevisionError('必须明确核算期间。')
    a=p.start.year*12+p.start.month-1;b=p.end.year*12+p.end.month-1
    return [f'{n//12:04d}{n%12+1:02d}' for n in range(a,b+1)]


def select_versions(versions, period):
    """Latest authorized version per field within its actual effective interval."""
    choices=[]
    for month in period_months(period):
        chosen={}
        for version in versions:
            if _applicable(version,month):chosen[version['field']]=version
        choices.append([chosen[k] for k in sorted(chosen)])
    if any(v!=choices[0] for v in choices[1:]):
        raise RevisionError('本核算期间跨越不同已生效财务规则版本；不能用末月规则覆盖此前月份，请按真正适用区间分段核算或登记覆盖整期的明确规则。')
    return choices[0]


def effective_rules(ledger, rule_store, period):
    scenario = _read(rule_store, {'versions': []})['versions']
    return select_versions(scenario + ledger.get('case_rules', []), period)


def rule_owner(version):
    if version.get('owner') in OWNERS:return version['owner']
    return FIELDS[version['field']][0]


def adaptive_rule_fields(state):
    """Actual current clauses, not a fixed list of 18 legacy slot names."""
    if state.get('mode')!='sales_commission_v2':return {}
    from bonus_agent.adaptive_rules import normalized,PHASES
    saved=state.get('adaptive_basis_contract') or {};sources=state.get('adaptive_rule_sources',{})
    active_versions={str(v['id']):v for v in state.get('active_rules',[])}
    result={}
    for uid,decision in saved.get('clause_proposals',{}).items():
        source=sources.get(uid,{})
        for i,clause in enumerate(decision.get('clauses',[]),1):
            phase=clause['phase'];quote=clause['quote'];raw=source.get('text',quote)
            prior=active_versions.get(uid.removeprefix('Rrev_')) if uid.startswith('Rrev_') else None
            field=prior['field'] if prior and prior.get('kind')=='adaptive_clause' else 'adaptive:'+fingerprint((phase,normalized(raw),normalized(quote)))[:24]
            result[field]={'field':field,'owner':phase,'label':clause.get('meaning') or quote,'old':quote,'rule_id':f'{uid}:{i}',
                'original_quote':prior.get('original_quote') if prior else quote,
                'original_unit_text':prior.get('original_unit_text') if prior else raw,
                'source':source}
    for phase in PHASES:
        result['adaptive:add:'+phase]={'field':'adaptive:add:'+phase,'owner':phase,'label':'新增规则：'+OWNERS.get(phase,phase),'old':'（新增，不替代现有条款）'}
    return result


def roles_for_state(state):
    if state.get('mode')=='sales_commission_v2':
        return {k:v['label'] for k,v in (state.get('adaptive_roster_contract') or {}).get('roles',{}).items()}
    return {r:r for r in ROLES}


def basis_rules(rules):
    return [v for v in rules if rule_owner(v) in BASIS_NODES]


def receipt_token(state):
    """The human confirms the exact basis *and* its rejection epoch/contract.

    Legacy unamended cases retain their original amount token. A new version
    cannot be approved with a stale browser form, even for unchanged money.
    """
    if not state.get('basis_digest'):
        return ''
    if state.get('confirmation_version') != 2 and not state.get('basis_revision') and not basis_rules(state.get('active_rules', [])):
        return state['basis_digest']
    from bonus_agent.commission_chain import basis_contract_digest
    return fingerprint({'basis': state['basis_digest'], 'contract': basis_contract_digest(state),
                        'revision': state.get('basis_revision', '')})


def approval_matches(state):
    a = state.get('approval') or {}
    if a.get('contract'):
        from bonus_agent.commission_chain import basis_contract_digest
        if a['contract'] != basis_contract_digest(state):return False
    return (bool(state.get('basis_digest')) and a.get('digest') == state['basis_digest']
            and a.get('identity') == state.get('identity')
            and a.get('token', a.get('digest')) == receipt_token(state))


def _route(stage, description, owner=''):
    if owner:
        if owner not in OWNERS:
            raise RevisionError('无法识别责任环节，请选择财务问题类型。')
        return owner
    if stage == 'output':
        return 'export'
    if stage == 'settlement':
        return 'roster' if re.search('名单|比例|份额|角色|人员舍入', description) else 'settlement_review'
    matches = []
    for node, pattern in (
        ('scope_mapping', r'客户编码|送达方|项目编号|重复项目|项目键|首发.*匹配|销售.*匹配|毛利.*匹配'),
        ('reference_mapping', r'(历史|预算|库存|逾期).*(数据|列|表|匹配|金额有误)'),
        ('project_policy', r'新老|阶梯|费率|汇率|基数|端点|区域.*规则'),
        ('coefficient_policy', r'达成|毛利.*系数|月均|库存.*系数|逾期.*系数|缺省系数'),
        ('hold_policy', r'冻结|暂扣|扣减|项目.*舍入'),
    ):
        if re.search(pattern, description):
            matches.append(node)
    return matches[0] if len(matches) == 1 else 'basis_review'


def _stage(owner):
    return 'basis' if owner in BASIS_NODES + ('basis_review', 'inventory') else 'output' if owner == 'export' else 'settlement'


def _manifest_graph():
    import yaml
    from bonus_agent.commission_chain import CONFIG
    return {n['id']: tuple(n.get('needs', ())) for n in yaml.safe_load(CONFIG.read_text(encoding='utf-8'))['nodes']}


def affected_nodes(owners, graph=None):
    graph = graph or _manifest_graph()
    affected = set(owners)
    while True:
        expanded = affected | {k for k, deps in graph.items() if affected.intersection(deps)}
        if expanded == affected:
            return [k for k in graph if k in affected]
        affected = expanded


def parse_recipients(text,*,identities=False):
    """Explicit named shares, never an implicit redistribution of the pool."""
    pairs = []
    for part in re.split(r'[,，、;；\n]+', str(text).strip()):
        match = re.fullmatch(r'\s*(.+?)\s*(\d+(?:\.\d+)?)\s*[%％]\s*', part)
        if not match or not match[1].strip() or match[1].strip().endswith(('-', '+', '−')):
            raise RevisionError('请填写明确姓名及百分比，例如：张三70%，李四30%。')
        name=match[1].strip();pid=None
        if identities and (':' in name or '：' in name):
            pid,name=re.split(r'[:：]',name,maxsplit=1);pid=pid.strip();name=name.strip()
            if not pid or not name:raise RevisionError('人员身份格式应为 编号:姓名70%。')
        pairs.append({'name':name,'share':str(Decimal(match[2])/100),**({'id':pid} if pid else {})})
    if len({x.get('id',x['name']) for x in pairs}) != len(pairs):
        raise RevisionError('同一角色的姓名重复，请合并为唯一的人员份额。')
    if sum((Decimal(x['share']) for x in pairs), Decimal(0)) > 1:
        raise RevisionError('同一项目角色的份额合计不得超过100%。')
    return pairs


def _checked_artifact(out, rnd, key):
    info = (rnd.get('outputs') or {}).get(key)
    if not info:
        return None
    name, digest = info
    for p in (Path(out)/'history'/f"第{rnd['n']}轮"/name, Path(out)/name):
        if p.is_file() and cs.sha256(p) == digest:
            return p
    return None


def source_manifest(catalog):
    """Physical ID namespace, distinct from the content fingerprints."""
    result={sid:{'file':s.file,'coordinate':s.name,'kind':'sheet'} for sid,s in catalog.sheets.items()}
    result.update({sid:{'file':t.file,'coordinate':[t.sheet_name,t.row_offset,t.col_offset] if t.sheet_name else [t.page,t.table],'kind':'parameter_table'} for sid,t in catalog.tables.items()})
    result.update({sid:{'file':v['file'],'coordinate':v['where'],'kind':'rule_text','text_digest':fingerprint(v['text'])}
                   for sid,v in catalog.blocks.items() if sid.startswith('R')})
    return result


def bound_source_files(spec, manifest):
    found=set()
    def walk(value):
        if isinstance(value,dict):
            for key,item in value.items():walk(key);walk(item)
        elif isinstance(value,(list,tuple)):
            for item in value:walk(item)
        elif isinstance(value,str) and value in manifest:found.add(manifest[value]['file'])
    walk(spec)
    return found


def _save_baseline(out, case, rid):
    state = state_of(case) or {}
    rnd = case['rounds'][-1]
    basis = state.get('basis_view')
    if not basis or fingerprint(basis) != state.get('basis_digest') or state.get('receipts', {}).get('basis_review', {}).get('status') != 'PASS':
        basis = None
    settlement = None
    # A verified historical artifact is preferable to a possibly stale root
    # file left by an interrupted subsequent round.
    latest_ok = next((r for r in reversed(case['rounds']) if r.get('status') == 'OK'), None)
    if latest_ok and (p := _checked_artifact(out, latest_ok, 'details')):
        details = _read(p, {})
        if not basis or fingerprint(details.get('basis')) == fingerprint(basis):
            settlement = details.get('settlement')
            basis = basis or details.get('basis')
            if not state.get('basis_view') and (chain:=_checked_artifact(out,latest_ok,'chain')):
                state=_read(chain,state)
    folder = Path(out)/'revisions'/rid
    folder.mkdir(parents=True, exist_ok=True)
    manifest=state.get('source_manifest')
    source_paths=[Path(x['path']) for x in rnd.get('source_inputs',[])]
    if not manifest and source_paths and all(p.is_file() for p in source_paths) and cs.fingerprint(source_paths)==state.get('files'):
        # First upgrade from a pre-revision case: recover the actual old ID
        # namespace while the original material bytes are still available.
        from bonus_agent.commission_sources import Catalog
        manifest=source_manifest(Catalog(source_paths))
    snapshot = {'round': rnd['n'], 'period': state.get('period'), 'basis': basis, 'settlement': settlement,
                'basis_digest': fingerprint(basis) if basis else None, 'files': state.get('files', {}),
                'specs': state.get('specs', {}), 'approval': state.get('approval'),
                'receipts':state.get('receipts',{}),'source_manifest':manifest or {},
                'source_order':state.get('source_order') or [p.name for p in source_paths],
                'basis_revision': state.get('basis_revision', ''), 'active_rules': state.get('active_rules', []),
                'confirmed': case.get('confirmed'), 'source_inputs': rnd.get('source_inputs', [])}
    if state.get('mode')=='sales_commission_v2':
        snapshot.update(mode=state['mode'],adaptive_basis_contract=state.get('adaptive_basis_contract'),adaptive_roster_contract=state.get('adaptive_roster_contract'),adaptive_output_contract=state.get('adaptive_output_contract'))
    _write(folder/'baseline.json', snapshot)
    # The original approvals and per-round histories are never rewritten.
    # Capture verified current deliverables even if there is no older archive.
    for key in ('basis', 'excel', 'details'):
        if p := _checked_artifact(out, rnd, key):
            shutil.copy2(p, folder/p.name)
    return rid


def baseline(out, revision):
    return _read(Path(out)/'revisions'/revision['baseline']/'baseline.json', {}) if revision and out else {}


def _request_context(request):
    return {k: v for k, v in request.items() if k in ('id', 'kind', 'stage', 'owner', 'description',
            'project_keys', 'observed', 'evidence', 'created_by', 'created_at', 'rule', 'rule_expired', 'recipient', 'source_changes','material_owners','routing_reason')}


def _record_locked(out, case, ledger, requests, owners, *, source_changes=()):
    """Commit the new generation before touching a possibly stale checkpoint."""
    state = state_of(case)
    if state is None:
        raise RevisionError('还没有固定提成计算版本，请先完成来源盘点。')
    previous = active(ledger)
    rid = uuid4().hex[:16]
    snap = _save_baseline(out, case, rid)
    # Unfinished feedback is not dropped when another objection is submitted.
    pending = previous and previous['status'] != 'OK'
    if pending:
        new_requests=requests
        requests = copy.deepcopy(previous['requests']) + new_requests
        for before in requests[:-len(new_requests)]:
            for after in new_requests:
                same_rule=before.get('rule') and after.get('rule') and before['rule']['field']==after['rule']['field']
                x,y=before.get('recipient'),after.get('recipient')
                if same_rule or (x and y and (x['project'],x['role'])==(y['project'],y['role'])):
                    before['superseded_by']=after['id']
        owners = set(previous['owners']) | set(owners)
        comparison = previous['baseline']
    else:
        comparison = snap
    owners = sorted(set(owners))
    basis_changed = any(_stage(o) == 'basis' for o in owners)
    epoch = rid if basis_changed else state.get('basis_revision', '')
    if previous and not basis_changed:
        epoch = previous.get('basis_revision', epoch)
    revision = {'id': rid, 'created_at': now(), 'status': 'REVISION_PENDING', 'base_round': case['rounds'][-1]['n'],
                'snapshot': snap, 'baseline': comparison, 'requests': requests, 'owners': owners,
                'affected': affected_nodes(owners), 'basis_revision': epoch, 'basis_changed': basis_changed}
    if previous and pending:
        previous['superseded_by'] = rid
    if source_changes:
        # Preserve replaced/deleted bytes before a web upload commits them.
        folder = Path(out)/'revisions'/snap/'old_materials'
        folder.mkdir(parents=True, exist_ok=True)
        old_hashes = state.get('files', {})
        for source in map(Path, source_changes):
            if source.is_file() and old_hashes.get(source.name) == cs.sha256(source):
                shutil.copy2(source, folder/source.name)
    ledger['revisions'].append(revision)
    _write(Path(out)/FILE, ledger)
    # Current-output paths are not historical version identifiers. Preserve
    # old bytes under the revision first, and retire them from current paths.
    # An Excel file open by finance may resist rename; the ledger still
    # revokes it, and the status notice explicitly identifies stale paths.
    retired=Path(out)/'revisions'/snap/'superseded_outputs';retired.mkdir(exist_ok=True)
    stale=[]
    for name in CURRENT_FINANCIAL_FILES - (set() if basis_changed else {'总额预核算.xlsx'}):
        p=Path(out)/name
        if p.is_file():
            try:p.rename(retired/name)
            except OSError:stale.append(name)
    _write(Path(out)/'current_commission_version.json',{'revision':rid,'status':'REVISION_PENDING','superseded_paths_still_open':stale,
        'note':'旧版交付已失效；历史文件保留在 history 和 revisions。'})
    # Ledger first: even if the following save fails, all approval gates see
    # a newer generation and refuse the superseded result.
    invalidate(state, revision)
    state['status'] = 'REVISION_PENDING'
    cs.save_case(out, case)
    return revision


def submit(out, *, by, kind='discrepancy', stage='unknown', owner='', description='',
           project_keys=(), observed='', evidence='', field='', value='', scope='this_case',
           effective_from='', effective_to='', role='', shares='', rule_store=None, approver=False,
           source_changes=(), base_version=''):
    out = Path(out)
    with cs.case_lock(out):
        case = cs.load_case(out)
        if not case or not state_of(case):
            raise RevisionError('没有可修订的固定提成案卷。')
        if base_version and base_version!=edit_token(case,load(out)):
            raise RevisionError('案卷版本已改变，请刷新页面，核对本版金额和原规则后重新提交。')
        return submit_locked(out, case, by=by, kind=kind, stage=stage, owner=owner,
                             description=description, project_keys=project_keys, observed=observed,
                             evidence=evidence, field=field, value=value, scope=scope, effective_from=effective_from,
                             effective_to=effective_to, role=role, shares=shares, rule_store=rule_store,
                             approver=approver, source_changes=source_changes)


def submit_locked(out, case, *, by, kind='discrepancy', stage='unknown', owner='', description='',
                  project_keys=(), observed='', evidence='', field='', value='', scope='this_case',
                  effective_from='', effective_to='', role='', shares='', rule_store=None, approver=False,
                  source_changes=()):
    state = state_of(case)
    description = str(description).strip()
    if not state or not by.strip() or kind not in KINDS or not description:
        raise RevisionError('请填写真实反馈人、问题类别和具体说明。')
    if stage not in ('unknown', 'basis', 'settlement', 'output'):
        raise RevisionError('未知核算阶段。')
    ledger = load(out)
    request = {'id': uuid4().hex[:16], 'kind': kind, 'description': description,
               'created_by': by, 'created_at': now(), 'observed': str(observed), 'evidence': str(evidence),
               'project_keys': list(dict.fromkeys(str(k).strip() for k in project_keys if str(k).strip()))}
    detected = _route(stage, description, owner)
    rule_store = rule_store or state.get('rule_store')
    if rule_store:state['rule_store'] = str(Path(rule_store).resolve())
    if not request['project_keys'] and state.get('basis_view') and kind == 'discrepancy':
        request['project_keys'] = [key for key, p in state['basis_view']['projects'].items()
            if key in description or (p.get('product') and str(p['product']) in description) or any(len(str(v))>=2 and str(v) in description for v in p.get('key_values',{}).values())]
    if kind == 'material_correction':
        # S/T/R ids can shift when a source is replaced/added. Until a
        # provenance-preserving remap is proved, conservatively rebind all.
        detected = 'inventory'
        request['source_changes'] = [Path(p).name for p in source_changes]
    elif kind == 'rule_change':
        if not approver:
            raise RevisionError('正式修改业务规则需要财务确认权限；普通异议不能改变制度。')
        dynamic=state.get('mode')=='sales_commission_v2'
        extra={}
        if dynamic:
            choices=adaptive_rule_fields(state);choice=choices.get(field)
            if not choice:raise RevisionError('请从本版本实际规则中选择被替代条款，或选择明确责任环节新增规则。')
            new=str(value).strip()
            if not new:raise RevisionError('请填写完整、明确的新规则原文（含条件、单位及公式），不是期望金额。')
            old=choice['old'];detected=choice['owner'];label=choice['label']
            if field.startswith('adaptive:add:'):field='adaptive:'+request['id']
            extra={'kind':'adaptive_clause','statement':new,'owner':detected,'original_quote':choice.get('original_quote'),
                   'original_unit_text':choice.get('original_unit_text'),'target_rule':choice.get('rule_id'),'source':choice.get('source',{})}
        else:
            new = normalize_value(field, value)
            detected, path, _, label, _ = FIELDS[field]
            try:old = _get(state['specs'][detected], path)
            except KeyError:raise RevisionError('原规则尚未成功绑定，先补充规则材料并完成该节点核验。') from None
        period = state['period']
        if scope == 'this_case':
            months=period_months(period);start=months[0];end=months[-1]
        elif scope == 'this_period':
            start = end = normalize_month(effective_from or period)
        elif scope in ('future_periods', 'scenario'):
            start = normalize_month(effective_from)
            end = normalize_month(effective_to) if effective_to else None
            if end and end < start:
                raise RevisionError('规则失效月份不能早于生效月份。')
        else:
            raise RevisionError('请选择规则适用范围。')
        version = {'id': request['id'], 'field': field, 'label': label, 'value': new, 'old_value': old,
                   'scope': scope, 'from': start, 'to': end, 'reason': description, 'evidence': request['evidence'],
                   'by': by, 'at': request['created_at']}
        version.update(extra)
        request['rule'] = version
        if scope == 'this_case':
            prior=next((v for v in effective_rules(ledger,rule_store,period) if v['field']==field),None)
            if prior:version.update(old_value=prior['value'],supersedes=prior['id'])
            ledger['case_rules'].append(version)
        else:
            if not rule_store:
                raise RevisionError('此案卷没有场景规则版本库，请先选择固定提成场景。')
            store = Path(rule_store)
            with cs.case_lock(store.parent):
                doc = _read(store, {'version': 1, 'versions': []})
                prior = next((v for v in select_versions(doc['versions'], start) if v['field'] == field), None)
                if prior:
                    version['old_value'] = prior['value']; version['supersedes'] = prior['id']
                doc['versions'].append(version)
                _write(store, doc)
            # A future-only policy does not invalidate today's work.
            if not any(_applicable(version,m) for m in period_months(period)):
                ledger.setdefault('scheduled', []).append(request)
                _write(Path(out)/FILE, ledger)
                return {'id': request['id'], 'status': 'SCHEDULED', 'rule': version, 'affected': []}
        # Verify that an existing case-specific override isn't silently
        # masking a newly registered, broader change for this case.
        selected = effective_rules(ledger, rule_store, period)
        if not any(v['id'] == version['id'] for v in selected):
            ledger.setdefault('scheduled', []).append(request)
            _write(Path(out)/FILE, ledger)
            return {'id': request['id'], 'status': 'SCHEDULED', 'rule': version, 'affected': []}
    elif kind == 'recipient_change':
        if not approver:
            raise RevisionError('指定或替换实际收款人需要财务确认权限。')
        keys = request['project_keys']
        if len(keys) != 1 or role not in roles_for_state(state):
            raise RevisionError('人员修订必须选择一个完整项目键及一个制度角色。')
        if not state.get('basis_view') or keys[0] not in state['basis_view']['projects']:
            raise RevisionError('该项目不在当前已核验的核算范围内，请核对项目四键。')
        detected = 'settlement'
        dynamic=state.get('mode')=='sales_commission_v2';parsed=parse_recipients(shares,identities=dynamic)
        if dynamic:
            contract_=(state.get('adaptive_roster_contract') or {}).get('roles',{}).get(role,{})
            pairs=contract_.get('recipients',{}).get('pairs',[])
            if any(p.get('id') for p in pairs) and any('id' not in p for p in parsed):
                raise RevisionError('本角色按人员唯一编号识别，请填写 编号:姓名70%，编号:姓名30%，不能仅按姓名合并。')
        record = {'id': request['id'], 'project': keys[0], 'role': role, 'shares': parsed,
                  'by': by, 'at': request['created_at'], 'reason': description, 'period': state['period']}
        request['recipient'] = record
        ledger['recipients'].append(record)
    elif kind == 'report_change':
        detected = 'export'

    request.update(owner=detected, stage=_stage(detected))
    if kind == 'discrepancy' and stage in ('basis', 'settlement') and request['stage'] != stage:
        raise RevisionError('所选核算阶段与责任环节不一致，请重新选择。')
    owners = BINDINGS + ('inventory',) if detected == 'inventory' else (detected,)
    # A personnel-only change is safe only if there is a verified project
    # basis. With no prior basis, normal evaluation will establish it first.
    return _record_locked(out, case, ledger, [request], owners, source_changes=source_changes)


def invalidate(state, revision):
    if not revision or state.get('revision_id') == revision['id']:
        return
    state['revision_id'] = revision['id']
    state['basis_revision'] = revision['basis_revision']
    state['revision_owners'] = revision['owners']
    state['revision_affected'] = revision['affected']
    state['revision_findings'] = {}
    dirty = set(revision['affected'])
    # Re-bind only responsibility owners. Re-evaluate the dependent DAG;
    # independent bindings with identical scoped material can keep receipts.
    owners = set(revision['owners'])
    for name in owners | (dirty & {'basis_review', 'settlement_review'}):
        state.setdefault('receipts', {}).pop(name, None)
        state.setdefault('prior_accepted_specs', {}).pop(name, None)
    for task,owner in state.get('task_owners',{}).items():
        if owner in owners:
            state.setdefault('receipts',{}).pop(task,None)
            state.setdefault('prior_accepted_specs',{}).pop(task,None)
            state.setdefault('feedback',{}).pop(task,None)
    for name in dirty:
        state.setdefault('node_receipts', {}).pop(name, None)
        state.setdefault('revisions', {}).pop(name, None)
        state.setdefault('feedback', {}).pop(name, None)
        state.setdefault('review_disputes', {}).pop(name, None)
    if revision['basis_changed']:
        for key in ('approval', 'prior_approved_basis', 'basis_view', 'basis_digest'):
            state.pop(key, None)
    state.pop('pending_node', None)
    state.pop('error', None)
    # Objections supersede stale unanswered questions at impacted nodes.
    # Keep the original actual answer in history, but never rely on a
    # superseded answer to override an explicit new policy.
    rule_nodes = {r['owner'] for r in revision['requests'] if r['kind'] == 'rule_change'}
    for q in state.get('questions', []):
        if q.get('related') in dirty and (not q.get('answer') or q.get('related') in rule_nodes):
            q['withdrawn'] = f"修订 {revision['id']} 已替代该环节的旧问题；如仍缺依据将重新提出"


def register_detected_change(out, case, ledger, *, hashes, period, current_rules, source_order=(), by=''):
    """On a CLI resume or a changed scenario policy, don't silently retain old approval."""
    state = state_of(case)
    if not out or not state:
        return ledger
    requested = active(ledger) or {}
    if requested.get('status') == 'OK':requested = {}
    requests = []
    owners = set()
    old_order=state.get('source_order') or [x['name'] for x in case['rounds'][-1].get('source_inputs',[])]
    material_pending=requested.get('status')=='REVISION_PENDING' and any(r['kind']=='material_correction' for r in requested.get('requests',[]))
    if (hashes != state.get('files') or period != state.get('period') or (old_order and source_order and list(source_order)!=old_order)) and not material_pending:
        changed = sorted(k for k in set(hashes) | set(state.get('files', {})) if hashes.get(k) != state.get('files', {}).get(k))
        requests.append({'id': uuid4().hex[:16], 'kind': 'material_correction', 'stage': 'basis', 'owner': 'inventory',
                         'description': '程序检测到实际来源或核算期间变更，重新绑定材料。', 'source_changes': changed,
                         'project_keys': [], 'created_at': now(), 'created_by': by or '系统（输入指纹检测）'})
        owners.update(BINDINGS + ('inventory',))
    previous = {v['field']: v for v in state.get('active_rules', [])}
    selected = {v['field']: v for v in current_rules}
    already = {r.get('rule', {}).get('id') for r in requested.get('requests', [])}
    for field in sorted(set(previous) | set(selected)):
        old, new = previous.get(field), selected.get(field)
        if old == new or (new and new['id'] in already):
            continue
        owner = rule_owner(new or old)
        requests.append({'id': uuid4().hex[:16], 'kind': 'rule_change', 'stage': _stage(owner), 'owner': owner,
                         'description': '适用的已授权规则版本发生变化。' if new else '原规则版本已不在有效期间。',
                         'rule': new or old, 'rule_expired': not bool(new), 'project_keys': [], 'created_at': now(),
                         'created_by': (new or old)['by']})
        owners.add(owner)
    if requests:
        _record_locked(out, case, ledger, requests, owners)
    return ledger


def narrow_material_revision(out, ledger, revision, state, catalog):
    """Reuse unchanged bindings only when their original physical IDs are proved.

    A new/deleted source, changed rule document, changed ID namespace, or
    absent provenance deliberately falls back to the complete rebind. A
    value-only change to an existing business sheet can be routed narrowly.
    The approval is still reissued for a changed full source inventory.
    """
    if not revision or not any(r['kind']=='material_correction' for r in revision['requests']):return
    old=baseline(out,revision); before=old.get('files',{}); after=state['files']
    prior_manifest=old.get('source_manifest',{}); current_manifest=source_manifest(catalog)
    changed={k for k in set(before)|set(after) if before.get(k)!=after.get(k)}
    safe=bool(changed and set(before)==set(after) and old.get('period')==state['period']
              and old.get('source_order')==state.get('source_order') and prior_manifest and prior_manifest==current_manifest
              and all(Path(k).suffix.lower() in ('.xlsx','.xlsm') for k in changed)
              and not any(v['file'] in changed and v['kind']!='sheet' for v in current_manifest.values()))
    owners=set(); covered=set()
    if safe:
        for owner in BINDINGS:
            matched=bound_source_files(old.get('specs',{}).get(owner,{}),prior_manifest)&changed
            if matched:owners.add(owner);covered.update(matched)
        safe=covered==changed
    # Recalculate routing from scratch, including new uploads after a stop.
    direct={r['owner'] for r in revision['requests'] if r['kind']!='material_correction'}
    if safe:
        direct|=owners|{'basis_review','inventory'}
        reason='实际变更仅涉及已绑定业务表，未变更来源的物理 ID、规则原文及文件指纹均一致；复用其已核验合同。'
    else:
        direct|=set(BINDINGS)|{'inventory'}
        reason='材料增删、制度变化、物理 ID 或既有依赖不能证明不变；为避免旧坐标错绑，完整重新绑定。'
    revision['owners']=sorted(direct);revision['affected']=(['inventory']+affected_nodes(direct-{'inventory'}) if safe else affected_nodes(direct))
    revision['routing_reason']=reason
    for r in revision['requests']:
        if r['kind']=='material_correction':
            r['source_changes']=sorted(changed);r['material_owners']=sorted(owners) if safe else list(BINDINGS)
            r['routing_reason']=reason
    state['revision_owners']=revision['owners'];state['revision_affected']=revision['affected']
    if safe:
        for name in BINDINGS:
            proof=old.get('receipts',{}).get(name,{});spec=old.get('specs',{}).get(name)
            if name not in direct and name not in state.get('receipts',{}) and not state.get('feedback',{}).get(name) and spec and proof.get('status')=='PASS' and proof.get('output')==fingerprint(spec):
                state.setdefault('prior_accepted_specs',{})[name]={'spec':copy.deepcopy(spec),'receipt':copy.deepcopy(proof)}
    if out:_write(Path(out)/FILE,ledger)


def status_update(out, revision, status, round_number, **extra):
    if not out or not revision:
        return
    ledger = load(out)
    current = active(ledger)
    if not current or current['id'] != revision['id']:
        raise RevisionError('财务反馈版本已变，禁止保存旧版本的交付状态。')
    current.update(status=status, round=round_number, updated_at=now(), **_plain(extra))
    _write(Path(out)/FILE, ledger)
    _write(Path(out)/'current_commission_version.json',{'revision':revision['id'],'status':status,'round':round_number,
        'note':'仅 OK 状态为完成的新版本人员交付；待总额确认版本不包含本版正式人员结果。',**_plain(extra)})


def validate_current(out, case, *, final=False):
    """Fail closed if a newer financial request or policy exists."""
    state = state_of(case)
    if not state:
        return
    if state.get('status')=='AWAITING_TOTAL_CONFIRMATION':
        from bonus_agent.commission_chain import execution_profile
        if state.get('profile')!=execution_profile(state.get('mode','sales_commission_v1')):
            raise cs.ConfirmError('核算程序已更新，本版预核算需要从已保存节点重新核验后才能确认。')
    ledger = load(out); rev = active(ledger)
    if rev and (state.get('revision_id') != rev['id'] or (final and rev['status'] != 'OK')
                or (not final and rev['status'] not in ('AWAITING_TOTAL_CONFIRMATION', 'APPROVED', 'OK'))):
        raise cs.ConfirmError('存在待处理的财务修订；旧版金额或结果已失效，请完成新版本核验。')
    if final and rev and case['rounds'][-1].get('revision_id') != rev['id']:
        raise cs.ConfirmError('输出未属于当前财务修订版本。')
    if final:
        from bonus_agent.commission_chain import prior_approved_basis
        if state.get('status')!='OK' or not approval_matches(state) or not prior_approved_basis(state,case):
            raise cs.ConfirmError('当前版本没有对应的真实项目总额审批及独立核验。')
    if state.get('period') and effective_rules(ledger, state.get('rule_store'), state['period']) != state.get('active_rules', []):
        raise cs.ConfirmError('本期适用规则版本已变化，请重新运行对应节点。')
    paths = [Path(x['path']) for x in case['rounds'][-1].get('source_inputs', [])]
    if paths and (any(not p.is_file() for p in paths) or cs.fingerprint(paths) != state.get('files')):
        raise cs.ConfirmError('原始材料已变化，请重新核算后确认。')
    qp=Path(out)/'questions.yaml'
    if qp.is_file():
        import yaml
        answers={q.get('id'):q.get('answer') for q in yaml.safe_load(qp.read_text(encoding='utf-8')) or [] if q.get('answer') and not q.get('withdrawn')}
        if any(q.get('id') in answers and q.get('answer')!=answers[q['id']] for q in state.get('questions', []) if not q.get('withdrawn')):
            raise cs.ConfirmError('存在尚未重新核验的财务答复，请运行下一轮。')


def edit_token(case, ledger):
    state=state_of(case) or {}
    return fingerprint({'round':case['rounds'][-1]['n'],'revision':active_id(ledger),'specs':state.get('specs',{}),
                        'basis':state.get('basis_digest'),'rules':state.get('active_rules',[])})


def rules_for_node(rules, node):
    return [v for v in rules if v['field'] in FIELDS and FIELDS[v['field']][0] == node]


def apply_rule_versions(catalog, obj, node, rules):
    """Enforce explicit decisions instead of asking a model to choose again.

    Build a reproducible citation from the actual structured human record +
    the unchanged related parameters. No lookup against expected amounts.
    """
    versions = rules_for_node(rules, node)
    groups = {}
    for v in versions:
        _, path, slot, _, _ = FIELDS[v['field']]
        _set(obj, path, v['value'])
        groups.setdefault(slot, []).append(v)
    for slot, changes in groups.items():
        original=obj.get('revision_original_evidence',{}).get(slot) or obj.get('evidence',{}).get(slot)
        proven_original=False
        if isinstance(original,dict) and str(original.get('id','')).startswith('R') and original['id'] in catalog.blocks:
            raw=catalog.blocks[original['id']]['text'];quote=str(original.get('quote',''))
            if quote and re.sub(r'\s+','',quote) in re.sub(r'\s+','',raw):
                obj.setdefault('revision_original_evidence',{})[slot]=copy.deepcopy(original)
                proven_original=True
        values = {}
        for field, (owner, path, evidence_slot, _, _) in FIELDS.items():
            if owner == node and evidence_slot == slot:
                try:values[path] = _get(obj, path)
                except KeyError:pass
        if node == 'project_policy' and slot == 'currency':
            # The currency code itself is still from its original binding.
            values['currency.code'] = obj.get('currency', {}).get('code')
        lines = ['财务确认的结构化新规则，未变更字段保持原件绑定：']
        if proven_original:
            lines.append(f"原件（仅保留未被替代字段及追溯）：{original['id']} {original.get('quote','')}")
        authorized={v['field'] for v in changes}
        from bonus_agent.commission_calculation import _numbers
        for f in ('age_months','region_default','customer_default'):
            field_owner,path,group,_,_=FIELDS[f]
            if field_owner==node and group==slot and f not in authorized and path in values:
                if not proven_original or Decimal(str(values[path])) not in _numbers(original.get('quote','')):
                    raise RevisionError(f'{path} 未获本次规则变更授权，且其值没有保留的实际原件依据。')
        for v in changes:
            lines.append(f"版本 {v['id']}；{v['by']} 于 {v['at']}；{v['label']}：{v['old_value']} → {v['value']}；"
                         f"范围 {v['scope']} {v['from']} 至 {v.get('to') or '另行修订'}；理由：{v['reason']}；依据：{v.get('evidence', '')}")
        quote = '；'.join(f'{key}={value}' for key, value in sorted(values.items()))
        # Only the applicable, resolved field values are the computational
        # citation. The old contradictory clause remains in the audit record.
        sid = 'V' + fingerprint([[(v['id'], v['value']) for v in changes], values])[:20]
        catalog.blocks[sid] = {'file': '财务规则修订记录', 'where': ','.join(v['id'] for v in changes),
                               'text': '\n'.join(lines + [quote])}
        obj.setdefault('evidence', {})[slot] = {'id': sid, 'quote': quote}
    return obj


def check_reviewed_rule_values(obj,node,rules):
    for v in rules_for_node(rules,node):
        _,path,_,label,_=FIELDS[v['field']]
        try:actual=normalize_value(v['field'],_get(obj,path))
        except (KeyError,RevisionError):raise RevisionError(f'复核替换候选遗漏已授权规则 {label}。') from None
        kind=FIELDS[v['field']][-1]
        same=actual==v['value'] if isinstance(kind,tuple) else Decimal(str(actual))==Decimal(str(v['value']))
        if not same:
            raise RevisionError(f'复核替换候选试图改变已授权规则 {label}：{actual}，正式版本为 {v["value"]}。')


def authorized_source_ids(obj):
    return {v['id'] for v in obj.get('evidence', {}).values() if isinstance(v, dict) and str(v.get('id', '')).startswith('V')}


def recipient_overrides(ledger, period):
    chosen = {}
    for item in ledger.get('recipients', []):
        if item['period'] == period:
            chosen[(item['project'], item['role'])] = item
    return [chosen[k] for k in sorted(chosen)]


def recipient_source(item):
    return 'U' + item['id']


def attach_recipient_sources(catalog, overrides, revision=None):
    for item in overrides:
        text = '、'.join(f"{r['name']}{Decimal(r['share'])*100}%" for r in item['shares'])
        catalog.blocks[recipient_source(item)] = {'file': '财务人员修订记录', 'where': item['id'],
            'text': f"{item['by']} 于 {item['at']} 确认；项目 {item['project']}；角色 {item['role']}；名单：{text}；理由：{item['reason']}"}


def requests_for_node(revision, node, *, diagnosed=False):
    if not revision:
        return []
    result = []
    for r in revision['requests']:
        if r.get('superseded_by'):continue
        belongs = r['owner']==node or (r['owner']=='inventory' and node in r.get('material_owners',BINDINGS))
        if node == 'basis_review':belongs = r['stage'] == 'basis'
        elif node == 'settlement_review':belongs = r['stage'] in ('settlement', 'output')
        elif diagnosed:belongs = belongs or r['stage'] == _stage(node)
        if belongs:
            result.append(_request_context(r))
    return result


def feedback_context(out, revision, node, state, data, *, diagnosed=False):
    requests = requests_for_node(revision, node, diagnosed=diagnosed)
    if not requests:
        return {}
    old = baseline(out, revision)
    result = {'revision': revision['id'], 'requests': requests,
              'constraint': '异议不是新制度；只按原件或明确授权的新规则处理。反例用于定位，不以期望金额反推参数。'}
    if node in BINDINGS and node in old.get('specs', {}):
        result['previous_contract'] = old['specs'][node]
    if node in ('basis_review', 'settlement_review') and old.get('basis'):
        keys = {k for r in requests for k in r.get('project_keys', [])}
        old_projects = old['basis']['projects']
        if keys:
            result['previous_target_projects'] = {k: old_projects[k] for k in sorted(keys & set(old_projects))}
        result['previous_totals'] = old['basis']['totals']
    return result


def review_context(catalog, scoped, node, basis, settlement, overrides):
    """Give reviewers actual counterexamples, not an aggregate or all chat.

    All project keys/indicators are available for an unspecified basis dispute;
    explicit targets get complete rows and actual physical source snippets.
    """
    projects = (basis or {}).get('projects', {})
    keys = {k for r in scoped['requests'] for k in r.get('project_keys', [])}
    current = {k: p for k,p in projects.items() if not keys or k in keys}
    if node == 'basis_review':
        scoped['actual_project_indicators'] = [{k:p.get(k) for k in ('key','key_values','metrics','total','payable','alloc','destinations')} if 'metrics' in p else {k:p.get(k) for k in ('key','route','region','old','frozen') + INDICATORS} for p in current.values()]
        scoped['current_totals'] = (basis or {}).get('totals', {})
        checks=(basis or {}).get('checks',{})
        scoped['diagnostic_priority']={'default_counts':checks.get('default_counts',{}),'unmatched':checks.get('unmatched',{}),
            'rule':'优先核对有缺失/默认/未匹配的指标及财务反例，再检查其实际责任节点；不把低置信度直接等同错误。'}
        words = ('项目','阶梯','费率','汇率','新老','月平均','达成','毛利','库存','逾期','欠款','暂扣','舍入','计算基数','匹配')
    else:
        rows = (settlement or {}).get('rows', [])
        if not keys:
            # Keep a complete role group for every explicit recipient change;
            # the standard review also supplies representative complete groups.
            keys = {v['project'] for v in overrides}
        scoped['actual_target_role_rows'] = [r for r in rows if r['project'] in keys] if keys else []
        scoped['actual_people'] = (settlement or {}).get('people', [])
        words = ('分配','名单','姓名','角色','份额','舍入','比例','NPD','客户经理')
    # A model may cite real evidence from the current owner's material or
    # a specific, physically re-read row. Limit excerpts, not calculations.
    sources = catalog.sources(words)['text']
    if (basis or {}).get('metric_fields'):
        all_rules=(basis or {}).get('rule_coverage',{}) if node=='basis_review' else (settlement or {}).get('rule_coverage',{})
        for rule in all_rules.values():
            sid=rule['clause']['unit']
            if sid in catalog.blocks:sources[sid]=catalog.blocks[sid]
    for sid, block in catalog.blocks.items():
        if sid.startswith('V') and (node=='basis_review' or any(word in block['text'] for word in ('人员','名单'))):
            sources[sid] = block
        if sid.startswith('U') and node=='settlement_review':sources[sid] = block
    if keys:
        sheet_locations = {f'{s.file}/{s.name}': s for s in catalog.sheets.values()}
        locations = []
        for p in current.values():
            value=p.get('sources',{})
            for refs in (value.values() if isinstance(value,dict) else [value]):
                if isinstance(refs, str):refs = [refs]
                if isinstance(refs, (list,tuple)):locations.extend(x for x in refs if isinstance(x,str))
        if node=='settlement_review':locations.extend(r['source'] for r in scoped['actual_target_role_rows'] if 'source' in r)
        from openpyxl.utils import get_column_letter
        for loc in list(dict.fromkeys(locations))[:40]:
            name, sep, row = loc.rpartition('!')
            s = sheet_locations.get(name)
            if sep and s and row.isdigit() and 1 <= int(row) <= len(s.rows):
                values = s.rows[int(row)-1]
                text = ' | '.join(f'{get_column_letter(i)}={v}' for i,v in enumerate(values,1) if v is not None)
                if text:
                    sid = 'E' + fingerprint([loc,text])[:20]
                    block = {'file':s.file,'where':f'{s.name}!{row}','text':text}
                    catalog.blocks[sid]=block;sources[sid]=block
    scoped['source_evidence'] = sources
    scoped['target_keys_missing_from_current_basis'] = sorted(keys-set(projects))
    return scoped


def relevant_findings(catalog, node, reply, requests, allowed=None):
    """A bare PASS isn't evidence that a specific financial objection was read."""
    if not requests:
        return []
    rows = reply.get('feedback_findings')
    expected = {r['id'] for r in requests}
    if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows) or {r.get('id') for r in rows} != expected or len(rows) != len(expected):
        raise RevisionError(f'{node} 复核必须逐条回应实际财务反馈，缺少反馈处理结论；不能只提交 PASS。')
    for row in rows:
        if row.get('disposition') not in ('corrected', 'retained', 'verified') or any(not str(row.get(k, '')).strip() or re.fullmatch(r'无法判断|不确定|未知|无|已核验|正确|通过',str(row.get(k,'')).strip()) for k in ('identified', 'checked', 'conclusion')):
            raise RevisionError(f"财务反馈 {row.get('id')} 缺少已识别问题、实际核对项或结论。")
        refs = row.get('evidence') or []
        if not isinstance(refs, list) or not refs:
            raise RevisionError(f"财务反馈 {row['id']} 缺少可追溯实际原文。")
        for ref in refs:
            if not isinstance(ref, dict) or not str(ref.get('quote', '')).strip() or ref.get('id') not in catalog.blocks or (allowed is not None and ref.get('id') not in allowed):
                raise RevisionError(f"财务反馈 {row['id']} 的依据不是实际原文。")
            norm = lambda s: re.sub(r'\s+', '', str(s))
            if norm(ref['quote']) not in norm(catalog.blocks[ref['id']]['text']):
                raise RevisionError(f"财务反馈 {row['id']} 引用的内容不在原文中。")
    return rows


def _delta(before, after):
    try:
        a = Decimal(str(before)) if before is not None else Decimal(0)
        b = Decimal(str(after)) if after is not None else Decimal(0)
        return str(b-a) if a.is_finite() and b.is_finite() else None
    except InvalidOperation:
        return None


def _changes(before, after, path=''):
    if isinstance(before, dict) and isinstance(after, dict):
        for k in sorted(set(before) | set(after)):
            yield from _changes(before.get(k), after.get(k), f'{path}.{k}' if path else k)
    elif before != after:
        yield {'field': path, 'before': before, 'after': after, 'delta': _delta(before, after)}


def diff_report(old, basis, settlement, state, revision):
    basis = _plain(basis) if basis is not None else None
    old_basis = old.get('basis') or {}
    b, a = old_basis.get('projects', {}), (basis or {}).get('projects', {})
    project_changes = []
    targeted = {key for r in revision['requests'] for key in r.get('project_keys', [])}
    if basis is not None:
        for key in sorted(set(b) | set(a)):
            left, right = b.get(key, {}), a.get(key, {})
            dynamic=state.get('mode')=='sales_commission_v2'
            if dynamic:
                left={**left,**{'metric.'+k:v for k,v in left.get('metrics',{}).items()},**{'destination.'+k:v for k,v in left.get('destinations',{}).items()}}
                right={**right,**{'metric.'+k:v for k,v in right.get('metrics',{}).items()},**{'destination.'+k:v for k,v in right.get('destinations',{}).items()}}
                fields=('key_values','total','payable','alloc','round_delta','sources')+tuple(sorted(k for k in set(left)|set(right) if k.startswith(('metric.','destination.'))))
            else:fields=INDICATORS + ('region', 'route', 'first_date', 'old', 'frozen', 'currency', 'tier_steps', 'sources')
            for field in fields:
                before, after = left.get(field), right.get(field)
                if before != after:
                    project_changes.append({'project': key, 'field': field, 'before': before, 'after': after,
                                            'delta': _delta(before, after) if field in INDICATORS or dynamic and field.startswith(('metric.','destination.')) else None,
                                            'direct_target': key in targeted,
                                            'sources_before': left.get('sources', {}), 'sources_after': right.get('sources', {})})
    totals = list(_changes(old_basis.get('totals', {}), (basis or {}).get('totals', {}))) if basis is not None else []
    rules = list(_changes(old.get('specs', {}), state.get('specs', {})))
    files = list(_changes(old.get('files', {}), state.get('files', {})))
    def group(items, keys, values):
        result = {}
        for row in items:
            key = tuple(row[k] for k in keys)
            entry = result.setdefault(key, {k: Decimal(0) for k in values})
            for k in values:entry[k] += Decimal(str(row[k]))
        return result
    def group_diff(before, after, keys, values):
        x, y = group(before, keys, values), group(after, keys, values)
        result = []
        for key in sorted(set(x) | set(y)):
            for c in _changes(x.get(key, {}), y.get(key, {})):
                result.append(dict(zip(keys, key), **c))
        return result
    prior_s = old.get('settlement') or {}
    people, roles = [], []
    if settlement is not None:
        people_keys=('id','name','destination') if state.get('mode')=='sales_commission_v2' else ('name','destination')
        role_keys=('project','role_id','role','person_id','name','destination') if state.get('mode')=='sales_commission_v2' else ('project','role','name','destination')
        people = group_diff(prior_s.get('people', []), settlement['people'], people_keys, ('raw', 'amount'))
        roles = group_diff(prior_s.get('rows', []), settlement['rows'], role_keys, ('raw', 'share'))
    return _plain({'revision': revision['id'], 'basis_revision': revision['basis_revision'],
                   'previous_round': old.get('round'), 'previous_basis_digest': old.get('basis_digest'),
                   'current_basis_digest': state.get('basis_digest'), 'previous_totals': old_basis.get('totals', {}),
                   'current_totals': (basis or {}).get('totals', {}), 'totals': totals,
                   'projects': {'before': len(b), 'after': len(a), 'added': sorted(set(a)-set(b)), 'removed': sorted(set(b)-set(a)),
                                'changed': len({x['project'] for x in project_changes}), 'indicators': project_changes},
                   'rules': rules, 'files': files, 'people': people, 'roles': roles,
                   'settlement_compared': settlement is not None, 'previous_settlement_available': bool(prior_s),
                   'requests': revision['requests'], 'owners': revision['owners'], 'affected': revision['affected'],
                   'findings': state.get('revision_findings', {}), 'approval_reconfirmation': revision['basis_changed'],
                   'no_project_change': bool(old_basis and basis is not None and not project_changes),
                   'attribution': '按实际财务反馈、规则及源文件变化列示；未变化不代表无异议，须查看独立复核的逐条结论。'})


def assert_unchanged_basis(old, state, revision):
    if revision['basis_changed'] or not old.get('basis'):
        return
    from bonus_agent.commission_chain import basis_contract_digest
    if old.get('basis_digest') != state.get('basis_digest') or basis_contract_digest(old) != basis_contract_digest(state):
        raise RevisionError('本次人员或报表修订发现项目对象、规则合同、来源或逐项目金额实际改变；原总额确认不能复用。请发起项目金额复核，按新版本重新确认总额。')


def assert_unaffected_recipients(old, settlement, revision):
    """A targeted explicit recipient correction must not alter other roles."""
    if not old.get('settlement') or not all(r['kind']=='recipient_change' for r in revision['requests']):
        return
    targets = {(r['recipient']['project'],r['recipient']['role']) for r in revision['requests']}
    def rows(s):
        return sorted(fingerprint(r) for r in s['rows'] if (r['project'],r.get('role_id',r['role'])) not in targets)
    if rows(old['settlement']) != rows(settlement):
        raise RevisionError('指定名单修改之外的项目/角色原值、人员或来源发生变化；发现越界变更，禁止作为本次人员单独修订交付。')


def write_diff(out, report):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    out = Path(out)
    path = out/'revision_diff.json'
    _write(path, report)
    wb = Workbook(); intro = wb.active; intro.title = '修订摘要'
    def cell(v):
        if v is None:return ''
        if isinstance(v, (dict, list)):return json.dumps(v, ensure_ascii=False, default=str)[:32000]
        s = str(v)
        return "'" + s if s.startswith(('=', '+', '@')) else s
    def sheet(ws, headers, rows):
        ws.append(headers)
        for row in rows:ws.append([cell(v) for v in row])
        for c in ws[1]:
            c.font = Font(bold=True, color='FFFFFF'); c.fill = PatternFill('solid', fgColor='34536B')
        ws.freeze_panes = 'A2'; ws.auto_filter.ref = ws.dimensions
        for i in range(1, len(headers)+1):ws.column_dimensions[get_column_letter(i)].width = 28 if i < 5 else 48
    sheet(intro, ['项目', '内容'], [('修订版本', report['revision']), ('对照轮次', report['previous_round']),
          ('责任环节', report['owners']), ('受影响节点', report['affected']), ('总额需重新确认', report['approval_reconfirmation']),
          ('人员结果已经重新核算', report['settlement_compared']), ('原人员结果可比较', report['previous_settlement_available']),
          ('旧总额', report['previous_totals']), ('新总额', report['current_totals']), ('新增项目', report['projects']['added']),
          ('移除项目', report['projects']['removed']), ('说明', report['attribution'])])
    def add(name, rows, cols):
        sheet(wb.create_sheet(name), cols, ([row.get(k) for k in cols] for row in rows))
    add('总额差异', report['totals'], ['field', 'before', 'after', 'delta'])
    add('逐项目指标', report['projects']['indicators'], ['project', 'field', 'before', 'after', 'delta', 'direct_target', 'sources_before', 'sources_after'])
    add('规则合同差异', report['rules'], ['field', 'before', 'after', 'delta'])
    add('材料指纹差异', report['files'], ['field', 'before', 'after'])
    add('人员差异', report['people'], ['id','name', 'destination', 'field', 'before', 'after', 'delta'])
    add('项目角色原值差异', report['roles'], ['project', 'role_id','role','person_id', 'name', 'destination', 'field', 'before', 'after', 'delta'])
    add('反馈及处理', report['requests'], ['id', 'kind', 'description', 'observed', 'evidence', 'created_by', 'created_at', 'rule', 'recipient'])
    add('独立复核结论', [dict(node=n, **r) for n, rows in report['findings'].items() for r in rows],
        ['node', 'id', 'identified', 'checked', 'disposition', 'conclusion', 'evidence'])
    dest = out/'修订差异.xlsx'
    wb.save(dest); wb.close()
    return {'revision_diff': path, 'revision_excel': dest}


def is_explanation(text):
    # Clear modification language has precedence over a question mark.
    if re.search(r'重算|重新计算|重新核|重新审核|复核|有误|不正确|算错|驳回|撤销|调整|修改|改为|换成|修正|更正|补充|重新分配|按.+计算', text):
        return False
    return bool(re.search(r'为什么|怎么算|如何计算|计算过程|什么原因|怎么得|来源|解释|依据|多少|查看|查询|[？?]$', text))


def needs_structured_change(text):
    return bool(re.search(r'改为|改成|调整为|设为|替换为|从.{1,30}改到|新增规则|以后.{0,30}(按|计算)|重新分配.*[%％]',text))


def feedback_stage(text):
    return 'settlement' if re.search(r'人员|个人|名单|姓名|角色|分给|分配比例|客户经理',text) and not re.search(r'项目金额|总金额|总额|暂扣|冻结',text) else 'unknown'


def explanation(out, case, message):
    """Read-only explanation from the verified calculation, no paid LLM."""
    state = state_of(case) or {}
    basis = state.get('basis_view') or {}
    rev = active(load(out))
    if not basis or (rev and rev.get('status') not in ('AWAITING_TOTAL_CONFIRMATION', 'APPROVED', 'OK')):
        return '当前财务修订尚未完成项目复核，不能把历史金额当作新的核验结果。可在历次存档查看原版，再运行下一轮。'
    try:validate_current(out,case)
    except cs.ConfirmError as exc:return str(exc)+'；历次存档仍可查看，不能将历史金额当作当前有效版本。'
    lines = ['以下来自本版已核验的程序计算记录（只读查询，不修改规则或触发计算）：']
    selected = [p for key, p in basis['projects'].items() if key in message or (p.get('product') and str(p['product']) in message)
                or (p.get('customer') and str(p['customer']) in message) or any(len(str(v))>=2 and str(v) in message for v in p.get('key_values',{}).values())]
    if selected:
        for p in selected[:8]:
            if 'metrics' in p:
                labels=basis.get('metric_fields',{});lines.append(f"项目 {p['key']}："+'；'.join(f'{labels.get(k,k)}={v}' for k,v in p['metrics'].items()))
                lines.append('实际项目来源：'+'；'.join(p.get('sources',[])[:8]));continue
            lines.extend([f"项目 {p['key']}：基数 {p['base']} × 对应阶梯 → {p['tier']}；区域 {p['region_factor']}、客户 {p['customer_factor']}、毛利 {p['gross_factor']}；"
                          f"提成 {p['total']}，库存及逾期暂扣 {p['hold']}，欠款暂扣 {p['debt']}，可分配 {p['alloc']}。",
                          '来源：' + json.dumps({k:({'示例行':v[:3],'总行数':len(v)} if isinstance(v,list) and len(v)>3 else v) for k,v in p['sources'].items()}, ensure_ascii=False)])
    else:
        lines.append('；'.join(f'{k}：{v} 元' for k, v in basis['totals'].items()))
        rnd = case['rounds'][-1]
        if p := _checked_artifact(out, rnd, 'details'):
            detail = _read(p, {}).get('settlement') or {}
            for person in detail.get('people', []):
                if person['name'] in message or person.get('id') and person['id'] in message:
                    lines.append(f"{person['name']}（{person.get('id',person['name'])}）：汇总原值 {person['raw']}，本期金额 {person['amount']} 元。")
                    for row in (r for r in detail['rows'] if r.get('person_id',r['name']) == person.get('id',person['name'])):
                        lines.append(f"{row['project']} / {row['role']}：角色率 {row['role_rate']} × 个人份额 {row['share']}，原值 {row['raw']}；{row['source']}")
        lines.append('若需定位一个特定金额，请输入本版完整项目键、实际键值、人员姓名或唯一编号。')
    return '\n'.join(lines)
