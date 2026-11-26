import json
from decimal import Decimal

import pytest

from tools.compare_commission_facts import compare, main


PERIOD = '2026-10'


def fact(level, key, amount):
    return {'period': PERIOD, 'level': level, 'key': [key], 'metric': '应分',
            'amount': amount, 'source': '示例表!D4'}


def write_ledger(path, rows):
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
    return path


def check(tmp_path, actual, expected, *, levels=('person',), tolerance='0'):
    left = write_ledger(tmp_path / 'actual.jsonl', actual)
    right = write_ledger(tmp_path / 'expected.jsonl', expected)
    return compare(left, right, PERIOD, levels, Decimal('0.01'), Decimal(tolerance))


def test_equal_total_with_swapped_people_detected_and_order_irrelevant(tmp_path):
    total = fact('total', '本期', '100.00')
    a, b = fact('person', '001', '30.00'), fact('person', '002', '70.00')
    assert check(tmp_path, [a, b, total], [total, b, a])['matched'] == 3
    result = check(tmp_path, [total, dict(a, amount='70'), dict(b, amount='30')], [a, b, total])
    assert result['status'] == 'DIFFERENT' and result['matched'] == 1
    assert [r['key'] for r in result['differences']] == [('001',), ('002',)]
    assert [Decimal(r['candidate_minus_reference']) for r in result['differences']] == [40, -40]


def test_missing_zero_extra_role_and_required_level(tmp_path):
    zero, person = fact('project', '项目甲', '0'), fact('person', '甲', '-2.00')
    result = check(tmp_path, [person, fact('role_raw', '新增角色', '0')], [zero, person],
                   levels=['person', 'pending'])
    assert {r['reason'] for r in result['differences']} == {'missing', 'extra'}
    assert result['missing_levels'] == {'candidate': ['pending'], 'reference': ['pending']}
    assert check(tmp_path, [person], [person], levels=['person', 'project'])['status'] == 'DIFFERENT'


def test_role_precision_has_no_effect_on_settled_money(tmp_path):
    role, person = fact('role_raw', '甲', '1.23456789'), fact('person', '甲', '1.23')
    candidate = [dict(role, amount='1.23456790'), dict(person, amount='1.24')]
    strict = check(tmp_path, candidate, [role, person])
    assert len(strict['differences']) == 2
    relaxed = check(tmp_path, candidate, [role, person], tolerance='0.000001')
    assert [r['level'] for r in relaxed['differences']] == ['person']
    with pytest.raises(ValueError, match='结算单位'):
        check(tmp_path, candidate, [role, person], tolerance='0.01')


@pytest.mark.parametrize('change', [
    {'amount': 'NaN'}, {'amount': '1.001'}, {'amount': 1.0}, {'source': ''},
    {'key': []}, {'period': '2026-11'},
])
def test_invalid_facts_are_visible(tmp_path, change):
    row = fact('person', '甲', '1')
    with pytest.raises(ValueError, match='actual.jsonl:1'):
        check(tmp_path, [dict(row, **change)], [row])


def test_cli_exit_codes_and_distinct_evidence(tmp_path):
    row = fact('person', '甲', '1.00')
    left = write_ledger(tmp_path / 'actual', [row])
    right = write_ledger(tmp_path / 'expected', [row])
    args = ['--candidate', str(left), '--reference', str(right), '--period', PERIOD,
            '--unit', '0.01', '--require', 'person']
    out = tmp_path / 'report.json'
    assert main([*args, '--out', str(out)]) == 0
    assert json.loads(out.read_text(encoding='utf-8'))['status'] == 'MATCH'
    with pytest.raises(SystemExit) as exit_info:
        main([*args, '--out', str(out)])
    assert exit_info.value.code == 2
    write_ledger(left, [dict(row, amount='1.01')])
    assert main(args) == 1
    write_ledger(left, [row, row])
    assert main(args) == 2
    left.write_text(json.dumps(row)[:-1] + ', "amount": "1"}\n', encoding='utf-8')
    assert main(args) == 2
    write_ledger(left, [])
    assert main(args) == 2
    with pytest.raises(ValueError, match='同一文件'):
        compare(right, right, PERIOD, ['person'], Decimal('0.01'), Decimal(0))
