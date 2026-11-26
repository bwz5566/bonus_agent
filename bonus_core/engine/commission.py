"""Deterministic reusable operators for the staged commission workflow.

There is no customer, month, workbook, rate, or expected amount in this module.
The compiler supplies verified schedules and policy choices from current inputs.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction

D = Decimal
ZERO = D(0)
ONE = D(1)


def rounded(amount, digits):
    return amount.quantize(ONE.scaleb(-digits), rounding=ROUND_HALF_UP)


def bracket(value, steps):
    """Select the exact bound; first band also covers negative observations."""
    selected = steps[0]
    for lower, rate, inclusive in steps:
        if value > lower or (inclusive and value == lower):
            selected = (lower, rate, inclusive)
    return selected


def tier_amount(value, steps, method, negative):
    if value < 0:
        if negative == 'signed':
            return value * steps[0][1]
        if negative == 'zero':
            return ZERO
        raise ValueError('负基数缺少已确认处理方式')
    if method == 'whole':
        return value * bracket(value, steps)[1]
    if method != 'progressive':
        raise ValueError(f'未支持的阶梯算法 {method}')
    return sum((max(ZERO,min(value,steps[i+1][0] if i+1<len(steps) else value)-lo)*rate
                for i,(lo,rate,_) in enumerate(steps)),ZERO)


def tier_check(value, steps, method, negative):
    """Independent progressive identity using rational rate differences."""
    x=Fraction(value)
    if x<0:
        return x*Fraction(steps[0][1]) if negative=='signed' else Fraction(0)
    if method=='whole':
        return x*Fraction(bracket(value,steps)[1])
    total=x*Fraction(steps[0][1])
    for prev,cur in zip(steps,steps[1:]):
        total+=max(Fraction(0),x-Fraction(cur[0]))*(Fraction(cur[1])-Fraction(prev[1]))
    return total


def coefficient(numerator, denominator, steps, default, *, missing=False):
    """Return observation and adopted factor separately; never invent a ratio."""
    if missing or numerator is None or denominator is None or denominator==0:
        return None, default
    rate=numerator/denominator
    return rate,bracket(rate,steps)[1]


def project_money(tier, region_factor, customer_factor, gross_factor, stock_factor,
                  overdue_factor, frozen, hold_method, digits):
    total=tier*region_factor*customer_factor*gross_factor
    if hold_method=='additive':
        hold=total*(ONE-stock_factor)+total*(ONE-overdue_factor)
    elif hold_method=='multiplicative':
        hold=total*(ONE-stock_factor*overdue_factor)
    else:
        raise ValueError(f'不支持的暂扣算法 {hold_method}')
    debt=total-hold if frozen else ZERO
    payable=ZERO if frozen else total-hold
    alloc=rounded(payable,digits)
    return dict(total=total,hold=hold,debt=debt,payable=payable,alloc=alloc,round_delta=alloc-payable)


def close_money(row):
    # Decimal division/multiplication may introduce tiny representation errors;
    # the distinct cent-valued settlement is checked exactly.
    residual=row['total']-row['hold']-row['debt']-row['payable']
    if abs(residual)>D('0.000000000001'):
        raise ValueError(f'项目提成去向不闭合：{residual}')


def settle_project(amount, shares, role_rates):
    """Each element is (role, recipient, share, destination, source)."""
    from collections import defaultdict
    sums=defaultdict(lambda:ZERO)
    contributions=[]
    for role,name,share,destination,source in shares:
        if role not in role_rates or not name or not ZERO<=share<=ONE:
            raise ValueError(f'无效角色/人员/名单比例：{role} {name} {share}')
        sums[role]+=share
        contributions.append(dict(role=role,name=name,share=share,role_rate=role_rates[role],
                                  destination=destination,source=source,raw=amount*role_rates[role]*share))
    if sum(role_rates.values(),ZERO)!=ONE:
        raise ValueError('制度各角色比例合计不为100%')
    if set(sums)!=set(role_rates) or any(v!=ONE for v in sums.values()):
        raise ValueError(f'名单角色份额未闭合：{dict(sums)}')
    if abs(sum((x['raw'] for x in contributions),ZERO)-amount)>D('0.000000000001'):
        raise ValueError('项目角色分配原值未闭合')
    return contributions
