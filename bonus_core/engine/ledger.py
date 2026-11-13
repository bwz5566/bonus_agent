"""Release historical holds using full payout rules and frozen earned amounts."""
from collections import defaultdict
from decimal import Decimal as D
from bonus_core.data.patches import apply_patches
from bonus_core.engine.evaluator import Evaluator
from bonus_core.engine.issues import EngineError, Issue
from bonus_core.engine.run import Engine, Result, rule_fingerprint
from bonus_core.engine.tree import build_tree


def _fail(message):
    raise EngineError([Issue.block('ledger_baseline', message)])


def ledger_entries(res: Result) -> list[dict]:
    groups = {nid:nid for nid in res.payouts}
    for group in res.rollups.values():
        groups.update({nid:group.node_id for nid in group.members})
    baseline = {'nodes':{nid:n.final for nid,n in res.nodes.items()},
                'paid':{p.node_id:p.paid for p in res.payout_rows()}, 'groups':groups,
                'deducted':{p.node_id:p.deducted for p in res.payout_rows()},
                'holds':{nid:dict(p.holds) for nid,p in res.payouts.items()},'rules_hash':res.rules_hash}
    return [{'node_id':nid,'condition':c,'due':p.final,'held':h,'released':'0.00',
            'rule_version':res.rule_version,'origin_hash':res.input_hash,'settlement':baseline,'settled_offset':'0.00'}
            for nid,p in res.payouts.items() for c,h in p.holds.items() if D(h)>0]


def release(rs_then, entries, updated) -> list[dict]:
    """Recompute all conditions and offsets; subtract original paid and cumulative releases.

    Persist release_now into released transactionally before a subsequent call.
    Supply complete origin batches, including fully released entries.
    """
    batches=defaultdict(list)
    for i,e in enumerate(entries):
        if e['rule_version'] != rs_then.version:
            _fail('version_mismatch：台账与规则版本不一致')
        batches[e['origin_hash']].append((i,e))
    out={}
    for batch in batches.values():
        baseline=batch[0][1].get('settlement')
        if baseline is None:
            if rs_then.offsets or len(rs_then.payout)!=1:
                _fail('旧台账缺少完整发放基线，多个条件或抵扣须迁移后补发')
            dues={e['node_id']:e['due'] for _,e in batch}
            baseline={'nodes':dues,'groups':{nid:nid for nid in dues},
                      'paid':{e['node_id']:str(D(e['due'])-D(e['held'])) for _,e in batch}}
        if any(e.get('settlement',baseline)!=baseline for _,e in batch):
            _fail('同一原始批次的发放基线不一致')
        if rs_then.offsets and 'deducted' not in baseline:
            _fail('缺少历史抵扣基线，须迁移完整台账后补发')
        if baseline.get('rules_hash') and baseline['rules_hash'] != rule_fingerprint(rs_then):
            _fail('历史规则内容已变化；同名版本不能用于另一份规则')
        keys=[(e['node_id'],e['condition']) for _,e in batch]
        if len(set(keys))!=len(keys): _fail('台账条件重复')
        if 'holds' in baseline:
            wanted={(nid,c):D(h) for nid,holds in baseline['holds'].items() for c,h in holds.items() if D(h)>0}
            if set(keys)!=set(wanted) or any(D(e['held'])!=wanted[(e['node_id'],e['condition'])] for _,e in batch):
                _fail('原批次台账不完整或历史挂起金额被修改')
        data,issues=apply_patches(rs_then,updated)
        root,levels,tree_issues=build_tree(rs_then,data)
        blocking=[x for x in [*issues,*tree_issues] if x.severity=='blocking']
        if blocking: raise EngineError(blocking)
        nodes={n.id:n for n in root.walk()}
        for nid,amount in baseline['nodes'].items():
            if nid in nodes: nodes[nid].final=D(amount)
        if 'holds' in baseline and any(n.id not in baseline['nodes'] for n in levels[rs_then.leaf.level]):
            _fail('最新数据含原批次外的发放对象，请按原批次筛选')
        engine=Engine.__new__(Engine)
        engine.rs,engine.root,engine.by_level=rs_then,root,levels
        engine.ev=Evaluator(rs_then,root,levels)
        payouts=engine._payouts()
        rollups=engine._rollups(payouts)
        groups={nid:nid for nid in payouts}
        for group in rollups.values():
            groups.update({nid:group.node_id for nid in group.members})
        if any(nid in groups and groups[nid]!=group for nid,group in baseline['groups'].items()):
            _fail('发放对象的归属人员已经变化')
        current=rollups or payouts
        by_group=defaultdict(list)
        for i,e in batch:
            if e['node_id'] not in nodes:
                out[i]={**e,'release_now':'0.00','note':'最新数据中找不到原发放对象'}
                continue
            if D(e['due'])!=D(baseline['nodes'][e['node_id']]): _fail('历史应得与基线不一致')
            posted=[D(e['released']),D(e.get('settled_offset','0')),D(e['held'])]
            if any(not v.is_finite() or v<0 for v in posted) or sum(posted[:2])>posted[2]:
                _fail('累计补发/抵扣超出历史挂起或非有效金额')
            if e['condition'] not in {c.id for c in rs_then.payout}: _fail('condition_missing：挂起条件已不存在')
            by_group[baseline['groups'][e['node_id']]].append((i,e))
        for group,items in by_group.items():
            if group not in current: _fail('原汇总对象已变化')
            already=sum((D(e['released']) for _,e in items),D(0))
            available=max(D(0),D(current[group].paid)-D(baseline['paid'][group])-already)
            settled=sum((D(e.get('settled_offset','0')) for _,e in items),D(0))
            prior_ded=D(baseline.get('deducted',{}).get(group,'0'))
            if D(current[group].deducted)<prior_ded+settled:
                _fail('已入账的抵扣金额减少，需要核对预发历史，不能自动再次支付')
            offset_available=max(D(0),D(current[group].deducted)-prior_ded-settled)
            def priority(item):
                _,e=item
                current_hold=D(payouts[e['node_id']].holds.get(e['condition'],'0'))
                reduction=max(D(0),D(e['held'])-current_hold-D(e['released']))
                return (-reduction,e['node_id'],e['condition'])
            for i,e in sorted(items,key=priority):
                offset_settled=D(e.get('settled_offset','0'))
                remaining=D(e['held'])-D(e['released'])-offset_settled
                now=min(remaining,available)
                available-=now
                offset_now=min(remaining-now,offset_available)
                offset_available-=offset_now
                out[i]={**e,'release_now':str(now),'still_held':str(remaining-now-offset_now),
                        'settled_offset':str(offset_settled+offset_now),'offset_now':str(offset_now),
                        'current_conditions':payouts[e['node_id']].holds}
    return [out[i] for i in range(len(entries))]
