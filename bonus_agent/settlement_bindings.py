"""Reassemble an independently amended source binding without changing payout rules."""
import copy
from dataclasses import replace

from bonus_agent.agent import ToolError
from bonus_agent.recovery import fingerprint
from bonus_agent.review_population import requirements
from bonus_core.dsl.parser import BinOp, BoolOp, Call, Cmp, Name, Neg, Not, Str, parse, unparse


def claim(flow, name, rs, envelope, prior):
    ws = flow.ws
    raw = envelope['receipt']
    provider = raw.get('provider') or {}
    declared = raw.get('declaration') or {}
    scheme = ws._ruleset(name)
    parent = flow.answered_settlement_schemes().get(name)
    if (envelope.get('scheme') != name or envelope.get('files') != ws._source_version
            or envelope.get('period') != str(ws.period) or envelope.get('answers') != [(q.id,q.answer) for q in ws.questions]
            or not parent or not flow.current('approved:' + parent) or not flow.current('allocation_formulas')
            or prior.get('spec_state') != 'unresolved' or prior.get('reviewer_policy') != flow.reviewer_policy()
            or provider.get('model') != prior['reviewer']['model'] or provider.get('status') != 'completed'
            or provider.get('usage_reported') is not True or provider.get('finish_reason') != 'stop'
            or raw.get('status') != 'RECEIVED' or not provider.get('response_id')
            or declared.get('status') != 'confirmed' or declared.get('same_rest_of_formulas') is not True
            or declared.get('unclear') != [] or not declared.get('reason')):
        raise ToolError('定向独立复核的材料、原口径或服务记录无法对应')
    records = flow.state.get('basis_review_parts', {}).get(name, {}).values()
    items = [r for r in records if r.get('section') == 'pool' and r.get('state') == 'unresolved'
             and (r.get('request') or {}).get('response_id') == raw.get('prior_response')]
    if len(items) != 1 or not raw.get('prior_response'):
        raise ToolError('定向纠正未关联唯一的独立复核原回复')
    original = items[0]
    draft = copy.deepcopy(original.get('independent_draft') or {})
    if (original['request'].get('model') != provider['model'] or not original['request'].get('usage_reported')
            or original['request'].get('response_received') is not True or draft.get('keep_reviewed_population') is not True
            or not draft.get('unclear') or not isinstance(draft.get('terminal_review'), dict)):
        raise ToolError('原独立声明没有确定的取数或完整的待验算式')
    basis = flow.state.get('basis', {}).get(name)
    if not basis or prior.get('basis_digest') != fingerprint(basis):
        raise ToolError('人员预核已变化，不能挪用旧独立声明')

    pops = {}
    for row in records:
        if row.get('section') != 'populations' or row.get('state') != 'valid':
            continue
        candidate = (row.get('value') or {}).get('populations')
        if candidate and set(candidate) == set(requirements(rs)):
            pops[fingerprint(candidate)] = candidate
    if len(pops) != 1:
        raise ToolError('独立原取数声明缺失或不唯一，须重新核对来源')
    populations = next(iter(pops.values()))

    aliases = declared.get('literal_map')
    if not isinstance(aliases, dict) or not aliases:
        raise ToolError('定向纠正须声明来源字段和字面值')
    _, root, _, _ = ws._independent_env(name, {}, basis=True)
    swaps = {}
    for field, mapping in aliases.items():
        owners = [key for key, d in rs.datasets.items() if field in d.fields]
        if len(owners) != 1 or not isinstance(mapping, dict):
            raise ToolError(f'分类字段无法在唯一原表定位：{field}')
        values = {row.values[field] for row in root.rows(owners[0]) if isinstance(row.values.get(field), str)}
        for old, new in mapping.items():
            if not isinstance(old, str) or not isinstance(new, str) or new not in values:
                raise ToolError(f'分类更正目标未在本期原表遇到：{field}')
            if old != new:
                if old in values:
                    raise ToolError(f'不能把本来有效的两个原表分类合并：{field}')
                swaps[field, old] = new
    if not swaps:
        raise ToolError('定向纠正没有新的来源字面值')
    applied = set()

    def change(node):
        if isinstance(node, Cmp) and node.op in ('==','!='):
            for key, alt in [('left','right'),('right','left')]:
                field, text = getattr(node,key), getattr(node,alt)
                if isinstance(field,Name) and isinstance(text,Str) and (ident := (field.id,text.value)) in swaps:
                    applied.add(ident)
                    return replace(node, **{alt:Str(swaps[ident])})
        if isinstance(node, (BinOp,BoolOp,Cmp)):
            return replace(node,left=change(node.left),right=change(node.right))
        if isinstance(node,(Not,Neg)):
            return replace(node,operand=change(node.operand))
        if isinstance(node,Call):
            return replace(node,args=tuple(change(a) for a in node.args),
                           kwargs=tuple((k,change(v)) for k,v in node.kwargs))
        return node

    def formula(expr):
        if not isinstance(expr,str) or not expr.strip():
            raise ToolError('独立声明缺少有效表达式')
        old = parse(expr)
        updated = change(old)
        if old == updated:
            return expr
        rendered = unparse(updated)
        if parse(rendered) != updated:
            raise ToolError('定向纠正的表达式结构发生变化')
        return rendered

    pool = draft.get('pool')
    if isinstance(pool,dict):
        if set(pool) != {'scope','expr'} or pool['scope'] != rs.pool.scope:
            raise ToolError('原独立奖金池声明的对象层级不对应')
        pool = pool['expr']
    if not isinstance(draft.get('helpers'),dict) or not isinstance(draft.get('tables'),dict):
        raise ToolError('独立草稿的参数表或辅助变量缺失')
    if rs.report and (rs.report.details or rs.report.totals):
        raise ToolError('原声明缺少本轮基础报告复核，不允许仅靠分类更正补齐')
    spec = {'pool':formula(pool),'helpers':{k:{**v,'expr':formula(v.get('expr'))} for k,v in draft['helpers'].items()},
            'tables':draft['tables'],'details':{},'totals':{},'populations':populations,'unclear':[]}
    terminal = draft['terminal_review']
    for part in ('pending','details'):
        terminal[part] = {k:formula(v) for k,v in terminal[part].items()}
    people = terminal['people']
    people['key'] = formula(people['key'])
    people['expr'] = formula(people['expr'])
    if applied != set(swaps):
        raise ToolError('存在无法对应到独立表达式的分类更正')
    evidence = {'original_response':raw['prior_response'],'amended_response':provider['response_id'],
                'source_round':raw['source_round'],'type':'checker_source_binding','fields':sorted(aliases)}
    return spec, terminal, evidence
