"""Read and validate literal interval bounds before compiling lower-bound tables.

The amount engine uses efficient lower-bound lookup. It must not infer that a
gap in a source table is covered by the preceding rate, or silently discard an
upper bound. This adapter retains both endpoints, including the source units.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal as D


@dataclass(frozen=True)
class Interval:
    label: str
    lower: D | None
    upper: D | None
    include_lower: bool | None = None
    include_upper: bool | None = None


_NUM = r'[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:%|万元|亿元|万|亿|元)?'
_MARK = r'\((?:不?含)(?:' + _NUM + r')?\)'
_PART = re.compile(r'([+-]?(?:\d+(?:\.\d+)?|\.\d+))(%|万元|亿元|万|亿|元)?\Z')
_SCALE = {'%': D('.01'), '元': D(1), '万': D(10000), '万元': D(10000),
          '亿': D(100000000), '亿元': D(100000000)}


def _text(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value)).translate(
        str.maketrans({'萬':'万','億':'亿','於':'于','–':'-','—':'-','−':'-',
                       '～':'~','至':'~','到':'~','≤':'<=' ,'≥':'>='})))


def _parts(text):
    m = _PART.fullmatch(text)
    if m is None:
        raise ValueError(f'区间端点不是单个带单位的有限数字：{text!r}')
    return D(m[1]), m[2]


def _value(text, percent, default_unit=None):
    n, unit = _parts(text)
    unit = unit or default_unit or ('%' if percent else '元')
    if unit not in _SCALE or (unit == '%') != percent:
        raise ValueError(f'区间端点 {text!r} 的单位与指标不一致')
    return n * _SCALE[unit]


def _marker(mark, a, b, default_side, *, percent, unit):
    if not mark:
        return None, None
    text = mark[1:-1]
    included = not text.startswith('不含')
    numeric = text[2:] if not included else text[1:]
    if numeric:
        end = _value(numeric, percent, unit)
        if a == end and b != end:
            side = 'lower'
        elif b == end and a != end:
            side = 'upper'
        else:
            raise ValueError(f'端点标记 {mark} 与区间两端 {a}/{b} 不符')
    else:
        side = default_side
    return (included, None) if side == 'lower' else (None, included)


def _merge_flags(*flags):
    present = {x for x in flags if x is not None}
    if len(present) > 1:
        raise ValueError('同一区间端点同时标记包含与不包含')
    return next(iter(present), None)


def parse_interval(label, *, percent=False, unit=None) -> Interval:
    """Parse ranges, comparison prefixes, suffixes, and explicit inclusion.

    An unmarked “以下/以上” or dash does not decide a shared endpoint. The
    surrounding evidence/confirmation must supply its ownership. A qualifier
    such as ``<25%(含)`` expressly includes the stated upper endpoint.
    """
    text = _text(label)
    bracket = re.fullmatch(r'([\[(])(' + _NUM + r'),(' + _NUM + r')([\])])', text)
    if bracket:
        a, b = bracket[2], bracket[3]
        av = _value(a, percent, _parts(b)[1] or unit)
        bv = _value(b, percent, _parts(a)[1] or unit)
        return Interval(str(label), av, bv, bracket[1]=='[', bracket[4]==']')
    text = re.sub(r'(?<=\d),(?=\d{3}(?!\d))', '', text)
    band = re.fullmatch(r'(' + _NUM + r')(' + _MARK + r')?[-~](' + _NUM + r')(' + _MARK + r')?', text)
    if band:
        a, b = band[1], band[3]
        au, bu = _parts(a)[1], _parts(b)[1]
        lo, hi = _value(a, percent, bu or unit), _value(b, percent, au or unit)
        l1, h1 = _marker(band[2], lo, hi, 'lower', percent=percent, unit=au or bu or unit)
        l2, h2 = _marker(band[4], lo, hi, 'upper', percent=percent, unit=bu or au or unit)
        return Interval(str(label), lo, hi, _merge_flags(l1,l2), _merge_flags(h1,h2))
    prefix = re.fullmatch(r'(<=|>=|<|>|小于|低于|不足|不满|不超过|不高于|大于|超过|不低于|至少)(' + _NUM + r')(' + _MARK + r')?', text)
    suffix = re.fullmatch(r'(' + _NUM + r')(' + _MARK + r')?(以下|以内|以上|及以上|及以下|\+)(' + _MARK + r')?', text)
    if prefix:
        op, raw, mark = prefix.groups()
        is_upper = op in ('<=','<','小于','低于','不足','不满','不超过','不高于')
        inclusive = op in ('<=','>=','不超过','不高于','不低于','至少')
        end = _value(raw, percent, unit)
        lo, hi = (None, end) if is_upper else (end, None)
        l, h = _marker(mark, lo, hi, 'upper' if is_upper else 'lower', percent=percent, unit=_parts(raw)[1] or unit)
        return Interval(str(label), lo, hi, l if is_upper or l is not None else inclusive,
                        h if not is_upper or h is not None else inclusive)
    if suffix:
        raw, mark1, op, mark2 = suffix.groups()
        is_upper = op in ('以下','以内','及以下')
        end = _value(raw, percent, unit)
        lo, hi = (None, end) if is_upper else (end, None)
        l1, h1 = _marker(mark1, lo, hi, 'upper' if is_upper else 'lower', percent=percent, unit=_parts(raw)[1] or unit)
        l2, h2 = _marker(mark2, lo, hi, 'upper' if is_upper else 'lower', percent=percent, unit=_parts(raw)[1] or unit)
        flag = True if op.startswith('及') else None
        return Interval(str(label), lo, hi, _merge_flags(l1, l2, None if is_upper else flag),
                        _merge_flags(h1, h2, flag if is_upper else None))
    raise ValueError(f'无法确定原文区间的两个端点：{label!r}；请提供明确上下界或阈值表')


def ordered_intervals(intervals, includes, *, start=D(0), require_unbounded=True):
    """Validate a continuous schedule, including textual upper bounds.

    ``includes`` is the resolved lower-inclusivity list in ascending order.
    The first below-zero band is handled separately by the amount/rate policy.
    Returns the sorted source indices and their mathematical lower bounds.
    """
    order = sorted(range(len(intervals)), key=lambda i: (intervals[i].lower is not None,
                    intervals[i].lower if intervals[i].lower is not None else D(0)))
    if not order:
        raise ValueError('制度区间为空')
    if len(includes) != len(order) or any(type(i) is not bool for i in includes):
        raise ValueError(f'逐档端点归属数 {len(includes)} 与原文档数 {len(order)} 不符')
    rows = [intervals[i] for i in order]
    if rows[0].lower not in (None, start):
        raise ValueError(f'首档 {rows[0].label!r} 不覆盖计算起点 {start}')
    bounds = []
    for i, row in enumerate(rows):
        lower = start if row.lower is None else row.lower
        if row.upper is not None and row.upper <= lower:
            raise ValueError(f'{row.label!r} 的上界必须大于下界 {lower}')
        bounds.append(lower)
        if i == 0:
            if row.lower is not None and row.include_lower is not None and includes[i] != row.include_lower:
                raise ValueError(f'{row.label!r} 的首档下界包含标记冲突')
            continue
        prev = rows[i-1]
        if row.lower is None or prev.upper is None or prev.upper > lower:
            raise ValueError(f'档位重叠：{prev.label!r} 与 {row.label!r}')
        if prev.upper < lower:
            raise ValueError(f'档位缺口：{prev.label!r} 的上界 {prev.upper} 至 {row.label!r} 的下界 {lower}')
        if row.include_lower is not None and includes[i] != row.include_lower:
            raise ValueError(f'{row.label!r} 已明确下界是否包含，不能改写')
        if prev.include_upper is not None and includes[i] == prev.include_upper:
            raise ValueError(f'{prev.label!r} 已明确{"含上界" if prev.include_upper else "不含上界"}，与下档端点冲突')
    if require_unbounded and rows[-1].upper is not None:
        raise ValueError(f'末档 {rows[-1].label!r} 未覆盖 {rows[-1].upper} 以上；不能默认沿用末档')
    return order, bounds
