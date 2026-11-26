"""Compare two independently prepared commission ledgers after calculation. No model calls."""

import argparse
import hashlib
import json
from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
from pathlib import Path


LEVELS = ('total', 'project', 'person', 'role_raw', 'pending', 'rounding')


def decimal_text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('金额和精度必须是十进制字符串')
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f'无效的十进制数 {value!r}') from exc
    if not number.is_finite():
        raise ValueError(f'金额或精度必须是有限数 {value!r}')
    return number


def unique_fields(pairs):
    fields = {}
    for name, value in pairs:
        if name in fields:
            raise ValueError(f'JSON 字段重复 {name!r}')
        fields[name] = value
    return fields


def read_ledger(path, period, unit):
    data = path.read_bytes()
    rows = {}
    for line_no, line in enumerate(data.decode('utf-8-sig').splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line, object_pairs_hook=unique_fields)
            if not isinstance(row, dict) or row.get('period') != period or row.get('level') not in LEVELS:
                raise ValueError('期间或层级与本次核对不符')
            parts = row.get('key')
            if not isinstance(parts, list) or not parts:
                raise ValueError('key 必须是非空的业务键数组')
            if not all(isinstance(s, str) and s.strip() for s in [*parts, row.get('metric'), row.get('source')]):
                raise ValueError('业务键、指标和来源定位均须填写')
            identity = (row['level'], tuple(parts), row['metric'])
            if identity in rows:
                raise ValueError(f'业务键及指标重复 {identity!r}')
            amount = decimal_text(row.get('amount'))
            if row['level'] != 'role_raw' and Fraction(amount) % unit:
                raise ValueError('结算金额超出本批精度，请核对已确认的舍入时点')
            rows[identity] = (amount, row['source'])
        except ValueError as exc:
            raise ValueError(f'{path.name}:{line_no} {exc}') from exc
    if not rows:
        raise ValueError(f'{path.name} 没有核对行')
    return rows, hashlib.sha256(data).hexdigest()


def compare(candidate, reference, period, required, unit, raw_role_atol):
    if candidate.samefile(reference):
        raise ValueError('候选结果和独立底稿指向同一文件')
    if unit <= 0 or not 0 <= raw_role_atol < unit:
        raise ValueError('结算单位须大于零，角色原值容差须非负且小于结算单位')
    actual, candidate_hash = read_ledger(candidate, period, Fraction(unit))
    expected, reference_hash = read_ledger(reference, period, Fraction(unit))
    counts = {name: dict(Counter(key[0] for key in rows))
              for name, rows in [('candidate', actual), ('reference', expected)]}
    missing_levels = {name: sorted(set(required) - set(values)) for name, values in counts.items()}
    differences = []
    matched = 0
    for key in sorted(actual.keys() | expected.keys()):
        observed, benchmark = actual.get(key), expected.get(key)
        delta = None
        if observed is None:
            reason = 'missing'
        elif benchmark is None:
            reason = 'extra'
        else:
            tolerance = raw_role_atol if key[0] == 'role_raw' else 0
            left, right = observed[0], benchmark[0]
            with localcontext() as ctx:
                # Keep aligned integer and fractional digits, including a possible carry.
                highest_place = max(left.adjusted(), right.adjusted())
                lowest_place = min(left.as_tuple().exponent, right.as_tuple().exponent)
                ctx.prec = highest_place - lowest_place + 2
                delta = left - right
            if delta.copy_abs() <= tolerance:
                matched += 1
                continue
            reason = 'amount'
        differences.append({'reason': reason, 'level': key[0], 'key': key[1], 'metric': key[2],
                            'candidate': {'amount': str(observed[0]), 'source': observed[1]} if observed else None,
                            'reference': {'amount': str(benchmark[0]), 'source': benchmark[1]} if benchmark else None,
                            'candidate_minus_reference': str(delta) if delta is not None else None})
    return {'status': 'DIFFERENT' if differences or any(missing_levels.values()) else 'MATCH',
            'period': period, 'unit': str(unit), 'raw_role_atol': str(raw_role_atol),
            'candidate_sha256': candidate_hash, 'reference_sha256': reference_hash,
            'required_levels': sorted(set(required)), 'counts': counts, 'missing_levels': missing_levels,
            'matched': matched, 'differences': differences}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--reference', required=True, type=Path)
    parser.add_argument('--period', required=True)
    parser.add_argument('--unit', required=True, help='本批结算最小单位，例如 0.01')
    parser.add_argument('--raw-role-atol', default='0', help='仅用于角色原值的绝对容差')
    parser.add_argument('--require', choices=LEVELS, nargs='+', required=True)
    parser.add_argument('--out', type=Path, help='保存至新的本地 JSON 文件，默认打印到标准输出')
    args = parser.parse_args(argv)
    try:
        result = compare(args.candidate, args.reference, args.period, args.require,
                         decimal_text(args.unit), decimal_text(args.raw_role_atol))
    except (ValueError, OSError) as exc:
        result = {'status': 'INVALID', 'error': str(exc)}
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        try:
            with args.out.open('x', encoding='utf-8') as stream:
                stream.write(serialized + '\n')
        except OSError as exc:
            parser.exit(2, f'无法保存本次报告 {args.out}: {exc}\n')
        print(result['status'], args.out)
    else:
        print(serialized)
    return {'MATCH': 0, 'DIFFERENT': 1, 'INVALID': 2}[result['status']]


if __name__ == '__main__':
    raise SystemExit(main())
