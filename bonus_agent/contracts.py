"""Structural financial hazards: deterministic checks with actionable counterexamples."""
from __future__ import annotations

import re
import unicodedata

from bonus_core.dsl.parser import BinOp, BoolOp, Call, Cmp, Name, Num, parse, walk


def ratio_default_gaps(rs):
    """A ratio fallback must not silently become a different final coefficient."""
    def expand(ast, seen=()):
        if isinstance(ast, Name) and ast.id in rs.variables and ast.id not in seen:
            return expand(parse(rs.variables[ast.id].expr), (*seen, ast.id))
        return ast

    gaps = []
    def zero_factor(ast, denominator):
        if ast == denominator:
            return True
        if isinstance(ast, BinOp) and ast.op == '*':
            return zero_factor(ast.left, denominator) or zero_factor(ast.right, denominator)
        return False
    def nonzero(condition, denominator, truth):
        if isinstance(condition, Cmp):
            pair = ((condition.left == denominator and isinstance(condition.right, Num) and condition.right.value == 0) or
                    (condition.right == denominator and isinstance(condition.left, Num) and condition.left.value == 0))
            return pair and ((condition.op == '!=' and truth) or (condition.op == '==' and not truth))
        if isinstance(condition, BoolOp):
            values = [nonzero(x, denominator, truth) for x in (condition.left, condition.right)]
            return any(values) if (condition.op == 'and' and truth) or (condition.op == 'or' and not truth) else all(values)
        return False

    def calls(ast, guards=()):
        if isinstance(ast, Call) and ast.fn == 'if' and len(ast.args) == 3:
            yield from calls(ast.args[0], guards)
            yield from calls(ast.args[1], (*guards, (ast.args[0], True)))
            yield from calls(ast.args[2], (*guards, (ast.args[0], False)))
        elif isinstance(ast, Call):
            yield ast, guards
            for arg in (*ast.args, *(v for _,v in ast.kwargs)):
                yield from calls(arg, guards)
        else:
            for attr in ('left','right','arg'):
                if hasattr(ast, attr):
                    yield from calls(getattr(ast, attr), guards)

    for name, variable in rs.variables.items():
        for call, guards in calls(parse(variable.expr)):
            if not isinstance(call, Call) or call.fn != 'tier_rate' or len(call.args) < 2:
                continue
            ratio = expand(call.args[1])
            if isinstance(ratio, Call) and ratio.fn == 'ratio' and 'zero' in ratio.kw():
                if variable.ratio_default == 'rate' and variable.sources:
                    continue  # explicit source-bound business intent, rechecked independently
                if len(ratio.args) > 1 and any(nonzero(cond, ratio.args[1], truth) for cond, truth in guards):
                    continue
                if len(ratio.args) > 1 and zero_factor(parse(variable.expr), ratio.args[1]):
                    continue  # denominator zero already makes the full amount exactly zero
                gaps.append((f'variables.{name}',
                    f'变量 {name} 将 ratio 的 zero 默认达成率继续查阶梯，可能把默认最终系数变成另一档。'
                    '改用 tier_ratio(表, 分子, 分母, 原文规定的最终默认系数, missing: 缺失条件)，'
                    '或明确在查表外按分母/缺失条件分支；不得猜默认系数。'))
    return gaps


def identity_conflicts(rs, data, mapping):
    """Detect contradictory composite identities; do not merge by similarity or customer name."""
    canonical = lambda x: re.sub(r'[|\s_\-/]+', '', unicodedata.normalize('NFKC', str(x))).casefold()
    norm = lambda x: unicodedata.normalize('NFKC', str(x)).strip().casefold()
    from bonus_core.data.period import Period
    dates = Period.parse('1900-01~2999-12')
    keys = {d.key: name for name, d in rs.datasets.items() if d.key}
    parents = {lv.level: rs.entity_dataset(lv.key)[0] for lv in rs.allocation if rs.entity_dataset(lv.key)}
    level_parent = {lv.level: (parents.get(rs.allocation[i-1].level), lv.parent_key)
                    for i, lv in enumerate(rs.allocation) if i}
    out = []
    relations=[]
    for name,spec in rs.datasets.items():
        targets={(keys[f],f) for f in spec.fields if f in keys and keys[f] != name}
        if spec.link in keys:
            targets.add((keys[spec.link],spec.link))
        if name in level_parent:
            targets.add(level_parent[name])
        relations.extend((name,spec,parent,field) for parent,field in targets)
    for name,spec,parent,field in relations:
        if not parent or name not in data or parent not in data or not field:
            continue
        pm, cm = mapping.get(parent), mapping.get(name)
        # We can justify a conflict only from an explicitly composite key, not a fuzzy name.
        if pm is None or cm is None or field not in (pm.combine or {}) or field not in (cm.combine or {}):
            continue
        arity = len(pm.combine[field])
        if arity < 3 or len(cm.combine[field]) != arity:
            continue
        known = {canonical(r.values.get(field)) for r in data[parent].rows}
        components = [tuple(map(norm, str(r.values.get(field, '')).split('|'))) for r in data[parent].rows]
        index = {}
        for parts in components:
            if len(parts) != arity:
                continue
            for omit in range(arity):
                index.setdefault((omit, parts[:omit] + parts[omit+1:]), []).append(parts)
        examples = []
        for row in data[name].rows:
            value = row.values.get(field)
            if value is None or canonical(value) in known:
                continue
            parts = tuple(map(norm, str(value).split('|')))
            if len(parts) != arity:
                continue
            # Distinct dates or a genuinely different customer are different records, not aliases.
            # Block only a contradictory choice between different source identity columns.
            hits = {p for omit in range(arity) if pm.combine[field][omit] != cm.combine[field][omit]
                    for p in index.get((omit, parts[:omit]+parts[omit+1:]), [])
                    if not (re.search(r'日期|年月|月份|period|date|month', pm.combine[field][omit]+' '+cm.combine[field][omit], re.I)
                            and dates.span_of(parts[omit]) is not None and dates.span_of(p[omit]) is not None)}
            if hits:
                examples.append({'source': str(row.src), 'key': str(value), 'candidates': ['|'.join(p) for p in sorted(hits)[:3]]})
        if examples:
            out.append({'id': f'identity:{name}:{parent}', 'count': len(examples), 'examples': examples[:5],
                        'message': f'「{name}」与「{parent}」存在复合身份冲突：多数身份分量相同但另一列不同。'
                                   f'核对主表 combine={pm.combine[field]} 与关联表 combine={cm.combine[field]}，'
                                   '先用 suggest_links 比较候选列，依据原文统一身份；不能按相似名称合并或 unmatched: ignore 跳过。'})
    return out
