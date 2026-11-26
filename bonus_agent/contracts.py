"""Structural financial hazards: deterministic checks with actionable counterexamples."""
from __future__ import annotations

import re
import unicodedata

from bonus_core.dsl.parser import BinOp, BoolOp, Call, Cmp, Const, Name, Not, Num, parse, walk


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

    def missing_default(ast, scope, guards=(), seen=()):
        """Find a scalar fallback for absent rows before the coefficient lookup."""
        if isinstance(ast, Name) and ast.id in rs.variables:
            if ast.id in seen:
                return False
            definition = rs.variables[ast.id]
            return missing_default(parse(definition.expr), definition.scope,
                                   guards if definition.scope == scope else (), (*seen, ast.id))
        if isinstance(ast, Call) and ast.fn == 'if' and len(ast.args) == 3:
            condition, yes, no = ast.args
            determined = next((truth for check, truth in reversed(guards) if check == condition), None)
            if determined is not None:
                return missing_default(yes if determined else no, scope, guards, seen)
            nodes = list(walk(expand(condition)))
            absent_rows = (any(isinstance(n, Call) and n.fn == 'count' for n in nodes)
                           and any(isinstance(n, Num) and n.value == 0 for n in nodes))
            if absent_rows and any(isinstance(expand(branch), Num) for branch in (yes, no)):
                return True
            return missing_default(yes, scope, guards, seen) or missing_default(no, scope, guards, seen)
        return False

    for name, variable in rs.variables.items():
        for call, guards in calls(parse(variable.expr)):
            if not isinstance(call, Call) or call.fn != 'tier_rate' or len(call.args) < 2:
                continue
            ratio = expand(call.args[1])
            if variable.ratio_default == 'rate' and variable.sources:
                continue  # An explicit rate fallback is a distinct source-bound policy.
            if missing_default(call.args[1], variable.scope, guards):
                gaps.append((f'variables.{name}',
                    f'变量 {name} 把缺少记录时的常数再次送入tier_rate查档；默认最终系数可能被换成另一档。'
                    '按原文将缺失判断放在查表外，或用tier_ratio(..., missing: 缺失条件)。'
                    '若原文明示默认的是达成率而非最终系数，须提供sources并声明ratio_default: rate。'))
                continue
            if isinstance(ratio, Call) and ratio.fn == 'ratio' and 'zero' in ratio.kw():
                if len(ratio.args) > 1 and any(nonzero(cond, ratio.args[1], truth) for cond, truth in guards):
                    continue
                if len(ratio.args) > 1 and zero_factor(parse(variable.expr), ratio.args[1]):
                    continue  # denominator zero already makes the full amount exactly zero
                gaps.append((f'variables.{name}',
                    f'变量 {name} 将 ratio 的 zero 默认达成率继续查阶梯，可能把默认最终系数变成另一档。'
                    '改用 tier_ratio(表, 分子, 分母, 原文规定的最终默认系数, missing: 缺失条件)，'
                    '或明确在查表外按分母/缺失条件分支；不得猜默认系数。'))
    gaps += [(path, message) for severity, path, message in _tier_ratio_diagnostics(rs) if severity == 'blocking']
    return gaps


def ratio_default_warnings(rs):
    """Unclear zero-denominator policy remains a warning, never a fabricated coefficient."""
    return [(path, message) for severity, path, message in _tier_ratio_diagnostics(rs) if severity == 'warning']


def _tier_ratio_diagnostics(rs):
    """Follow ratio aliases without losing their fallback provenance at a tier_ratio wrapper."""
    from bonus_core.dsl.models import TierTable
    from bonus_core.engine.evaluator import tier_rate

    def expand(ast, seen=()):
        if isinstance(ast, Name) and ast.id in rs.variables and ast.id not in seen:
            return expand(parse(rs.variables[ast.id].expr), (*seen, ast.id))
        return ast

    def known_value(ast, assignments):
        ast = expand(ast)
        for target, value in assignments:
            if ast == expand(target):
                return value
        if isinstance(ast, (Num, Const)):
            return ast.value
        if isinstance(ast, Not):
            outcome = known_value(ast.operand, assignments)
            return None if outcome is None else not outcome
        if isinstance(ast, Cmp):
            a, b = known_value(ast.left, assignments), known_value(ast.right, assignments)
            if a is None or b is None:
                return None
            return {'==': a == b, '!=': a != b, '>': a > b, '>=': a >= b,
                    '<': a < b, '<=': a <= b}.get(ast.op)
        if isinstance(ast, BoolOp):
            values = [known_value(c, assignments) for c in (ast.left, ast.right)]
            if ast.op == 'or':
                return True if True in values else (False if all(v is False for v in values) else None)
            if ast.op == 'and':
                return False if False in values else (True if all(v is True for v in values) else None)
        return None

    def at_value(condition, target, value):
        return known_value(condition, [(target, value)])

    def sentinel_test(condition, target, value):
        """Only infer the collision for an actual equality-to-fallback predicate."""
        for node in walk(expand(condition)):
            if not isinstance(node, Cmp) or node.op != '==':
                continue
            left, right = expand(node.left), expand(node.right)
            if ((left == expand(target) and isinstance(right, Num) and right.value == value)
                    or (right == expand(target) and isinstance(left, Num) and left.value == value)):
                return True
        return False

    def excludes_zero(condition, denominator, truth):
        # A known false predicate at D=0 proves the taken true branch has D!=0 (and vice versa).
        outcome = at_value(condition, denominator, 0)
        return outcome is not None and outcome != truth

    def requires_zero(condition, denominator, truth):
        condition = expand(condition)
        if isinstance(condition, Not):
            return requires_zero(condition.operand, denominator, not truth)
        if isinstance(condition, BoolOp):
            children = [requires_zero(c, denominator, truth) for c in (condition.left, condition.right)]
            return (any(children) if (condition.op == 'and' and truth or condition.op == 'or' and not truth)
                    else all(children))
        if not isinstance(condition, Cmp):
            return False
        left, right = expand(condition.left), expand(condition.right)
        pair = ((left == expand(denominator) and isinstance(right, Num) and right.value == 0)
                or (right == expand(denominator) and isinstance(left, Num) and left.value == 0))
        return pair and (condition.op == '==' and truth or condition.op == '!=' and not truth)

    def guarded_calls(ast, guards=()):
        if isinstance(ast, Call) and ast.fn == 'if' and len(ast.args) == 3:
            yield from guarded_calls(ast.args[0], guards)
            yield from guarded_calls(ast.args[1], (*guards, (ast.args[0], True)))
            yield from guarded_calls(ast.args[2], (*guards, (ast.args[0], False)))
        elif isinstance(ast, Call):
            yield ast, guards
            for child in (*ast.args, *(v for _, v in ast.kwargs)):
                yield from guarded_calls(child, guards)
        else:
            for attr in ('left', 'right', 'arg'):
                if hasattr(ast, attr):
                    yield from guarded_calls(getattr(ast, attr), guards)

    def ratios(ast, guards=(), seen=()):
        if isinstance(ast, Name) and ast.id in rs.variables and ast.id not in seen:
            yield from ratios(parse(rs.variables[ast.id].expr), guards, (*seen, ast.id))
        elif isinstance(ast, Call) and ast.fn == 'ratio' and len(ast.args) == 2 and 'zero' in ast.kw():
            yield ast, guards, seen
        elif isinstance(ast, Call) and ast.fn == 'if' and len(ast.args) == 3:
            yield from ratios(ast.args[1], (*guards, (ast.args[0], True)), seen)
            yield from ratios(ast.args[2], (*guards, (ast.args[0], False)), seen)

    def source_says_zero_coefficient(variable):
        for source in variable.sources:
            # Keep this deliberately narrow: missing records, a 100-unit budget and
            # another sentence's coefficient policy do not establish a D=0 policy.
            for sentence in re.split(r'[。；;\n]', unicodedata.normalize('NFKC', source.quote)):
                text = re.sub(r'\s+', '', sentence)
                if (re.search(r'待确认|待定|未明确|不明确|需(?:要)?确认|是否|[?？]', text)
                        or re.search(r'(?:不|未|禁止|不得|不能|不应)[^，,。；;]{0,16}系数', text)):
                    continue
                zero = re.search(r'(?:分母|除数|预算)(?:金额|数值|值)?(?:为|等于|是|==|=)?'
                                 r'(?:0(?:\.0+)?|零)(?![\d.])', text)
                coefficient = re.search(r'系数(?:均|应|直接|最终|就|必须)?'
                                        r'(?:指定|固定|默认|设|取值|取)(?:为)?'
                                        r'(?:\d+(?:\.\d+)?%?|零|一)(?![\d.])', text)
                if zero and coefficient:
                    return True
        return False

    def normal_example(ratio, value):
        """Return (candidate, proven); only free fields/constants justify a numeric witness."""
        numerator, denominator = expand(ratio.args[0]), expand(ratio.args[1])
        if numerator == denominator and value != 1:
            return None, False
        if isinstance(denominator, Num):
            if denominator.value == 0:
                return None, False
            if isinstance(numerator, Num) and numerator.value / denominator.value != value:
                return None, False
            candidate = value * denominator.value, denominator.value
        elif isinstance(numerator, Num):
            if value == 0:
                candidate = (numerator.value, 100) if numerator.value == 0 else None
            elif numerator.value == 0:
                candidate = None
            else:
                candidate = numerator.value, numerator.value / value
        else:
            candidate = value * 100, 100
        if candidate is None:
            return None, False
        for operand, assigned in zip((numerator, denominator), candidate):
            if (isinstance(operand, Call) and operand.fn in ('count', 'count_distinct')
                    and (assigned < 0 or assigned != int(assigned))):
                return None, False  # A half row is not a possible witness.
        # Arithmetic, transforms and aggregate leaf values may be correlated. We do
        # not solve their constraints, so never present the candidate as a real witness.
        return candidate, all(isinstance(operand, (Num, Name)) for operand in (numerator, denominator))

    diagnostics = []
    for name, variable in rs.variables.items():
        for call, outer_guards in guarded_calls(parse(variable.expr)):
            if call.fn != 'tier_ratio' or len(call.args) != 4:
                continue
            table = rs.tables.get(call.args[0].id) if isinstance(call.args[0], Name) else None
            outer_den, default = expand(call.args[2]), expand(call.args[3])
            if not isinstance(outer_den, Num) or outer_den.value == 0:
                continue  # Preserve the ordinary raw-numerator/raw-denominator contract.
            missing = call.kw().get('missing')
            for ratio, inner_guards, chain in ratios(call.args[1]):
                fallback = expand(ratio.kw()['zero'])
                guards = (*outer_guards, *inner_guards)
                if not isinstance(fallback, Num):
                    continue
                looked_up = (tier_rate(table, fallback.value / outer_den.value)
                             if isinstance(table, TierTable) else None)
                final = default.value if isinstance(default, Num) else None
                prefix = f'方案「{rs.name}」 variables.{name}'
                lineage = ' -> '.join(chain) or '内联ratio'
                example, proven = normal_example(ratio, fallback.value)
                # An equality sentinel also accepts a real, nonmissing ratio of that value.
                normal_values = ([(ratio.args[0], example[0]), (ratio.args[1], example[1]),
                                  (ratio, fallback.value), (call.args[1], fallback.value)] if example else [])
                # Unknown constraints do not justify a concrete, blocking witness.
                normal_reachable = proven and all(known_value(cond, normal_values) == truth for cond, truth in guards)
                normal_excluded = any(requires_zero(cond, ratio.args[1], truth)
                                      or (at_value(cond, call.args[1], fallback.value) is not None
                                          and at_value(cond, call.args[1], fallback.value) != truth)
                                      for cond, truth in guards)
                if (missing is not None and sentinel_test(missing, call.args[1], fallback.value)
                        and known_value(missing, normal_values) is True
                        and not normal_excluded and example is not None
                        and looked_up is not None and final is not None and looked_up != final):
                    if not normal_reachable:
                        diagnostics.append(('warning', f'variables.{name}',
                            f'{prefix}：ratio_default.collision_risk，missing把比例等于{fallback.value}作为缺失标记，'
                            f'而表在{fallback.value / outer_den.value}处返回{looked_up}、默认系数为{final}。'
                            f'来源链：{lineage}。当前未证明原分子/分母的变换、聚合叶值和分支约束能同时满足，'
                            '不提供虚构数值反例、不据此阻断；须核对正常输入能否达到该比例。'
                            '缺失判断应依据真实缺行/原分母条件，不凭同值推断缺失。'))
                        break
                    message = (f'{prefix}：ratio_default.collision，正常比例与缺失标记同值，导致正常档位被默认系数覆盖。'
                               f'数值反例：原分子={example[0]}、原分母={example[1]}（非零且记录存在），'
                               f'比例={fallback.value}；表在{fallback.value / outer_den.value}处返回{looked_up}，'
                               f'当前missing谓词却返回默认系数{final}。来源链：{lineage}。'
                               '缺失谓词须核对真实缺行/原分母条件，不能用计算结果等于fallback代替；'
                               '保留原分子分母传入tier_ratio或按原文在查表外分支，不自动改业务系数。')
                    diagnostics.append(('blocking', f'variables.{name}', message))
                    break
                fallback_values = [(ratio, fallback.value), (call.args[1], fallback.value), (ratio.args[1], 0)]
                if missing is not None and known_value(missing, fallback_values) is True:
                    continue  # This predicate already prevents the fallback from reaching the table.
                if any(known_value(cond, fallback_values) is not None
                       and known_value(cond, fallback_values) != truth for cond, truth in guards):
                    continue
                original_den = expand(ratio.args[1])
                if isinstance(original_den, Num) and original_den.value != 0:
                    continue  # The upstream zero fallback itself cannot be reached.
                protected = any(excludes_zero(cond, ratio.args[1], truth) for cond, truth in guards)
                protected |= missing is not None and excludes_zero(missing, ratio.args[1], False)
                if protected or (variable.ratio_default == 'rate' and variable.sources):
                    continue
                if looked_up is not None and final is not None and looked_up == final:
                    continue
                evidence = source_says_zero_coefficient(variable)
                severity = 'blocking' if evidence else 'warning'
                message = (f'{prefix}：ratio_default.shadowed，原分母为0的信息在上游ratio默认值中丢失。'
                           f'来源链：{lineage}；原分母=0时上游返回{fallback.value}，外层分母固定为{outer_den.value}，'
                           f'随后查档得到{looked_up if looked_up is not None else "未静态确定"}，'
                           f'外层默认系数{final if final is not None else "未静态确定"}不会由原分母0触发。'
                           + ('引用原文已明确零分母/预算零应使用最终系数，须保留原分母或由真实条件在查表外保护。'
                              if evidence else '当前来源未明确此处零分母政策，仅提示风险；先核对原文，不推定应取1、0或任何默认值。')
                           + '不得用比例等于fallback判断缺失；原文明确默认的是比率时，须保留来源并声明ratio_default: rate。')
                diagnostics.append((severity, f'variables.{name}', message))
                break
    return diagnostics


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
