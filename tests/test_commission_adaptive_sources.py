"""Counterexamples for the fixed chain's source and policy compiler."""
from decimal import Decimal as D

import pytest
from hypothesis import given, strategies as st
from openpyxl import Workbook

from bonus_agent.commission_calculation import _steps, compile_hold_policy
from bonus_agent.commission_sources import Catalog, ChainError
from bonus_core.engine.commission import bracket


def test_unknown_sheet_name_and_moved_header_are_visible(tmp_path):
    book = Workbook()
    sheet = book.active
    sheet.title = 'Transactions'
    sheet.append(['Quarterly export'])
    for _ in range(6):
        sheet.append([None])
    sheet.append(['period', 'account_id', 'net_revenue'])
    sheet.append(['202601', '00042', 123])
    path = tmp_path / 'unknown.xlsx'
    book.save(path)
    book.close()
    c = Catalog([path])
    previews = c.profiles(('销售金额', '销售单'))['previews']
    assert len(previews) == 1
    assert any(x['row'] == 8 and x['cells']['B'] == 'account_id'
               for x in previews[0]['header_candidates'])


def test_csv_preserves_physical_rows_and_leading_zero(tmp_path):
    path = tmp_path / 'raw.csv'
    path.write_text('Period,ID,Amount\n202601,00042,12.30\n', encoding='utf8')
    c = Catalog([path])
    s, = c.sheets.values()
    spec = {'sheet': s.id, 'header_row': 1, 'columns': {'id': 'B', 'amount': 'C'}}
    record, where, _ = next(s.records(spec, ('id', 'amount')))
    assert record == {'id': '00042', 'amount': '12.30'}
    assert where.endswith('!2')


def test_spreadsheet_parameter_table_is_addressable(tmp_path):
    book = Workbook()
    sheet = book.active
    sheet.title = 'Rate schedule'
    sheet.append(['Sales band', 'Rate'])
    sheet.append(['100万以下', '2%'])
    sheet.append(['100万以上', '3%'])
    path = tmp_path / 'rate.xlsx'
    book.save(path)
    book.close()
    c = Catalog([path])
    table = next(t for t in c.tables.values() if t.file == path.name)
    assert table.cell(2, 2) == '2%'
    assert 'B2' in table.location(2, 2)


def test_word_parameter_table_is_addressable(tmp_path):
    docx = pytest.importorskip('docx')
    doc = docx.Document()
    doc.add_heading('Rate schedule', 1)
    table = doc.add_table(rows=3, cols=2)
    for row, values in zip(table.rows, [('Sales band', 'Rate'), ('100万以下', '2%'), ('100万以上', '3%')]):
        for cell, value in zip(row.cells, values):
            cell.text = value
    path = tmp_path / 'rate.docx'
    doc.save(path)
    c = Catalog([path])
    table = next(t for t in c.tables.values() if t.file == path.name)
    assert table.cell(2, 2) == '2%'


def test_native_table_without_header_keeps_first_rule_row(tmp_path):
    from bonus_agent.adaptive_rules import rule_units
    docx = pytest.importorskip('docx')
    doc = docx.Document()
    for rows in ([('表名', '字段', '唯一键'), ('订单', '订单号、金额', '订单号')],
                 [('标准组', '25'), ('高等级', '35')],
                 [('标准组', '按实际确认名单'), ('其他组', '不得自动分配')]):
        table = doc.add_table(rows=len(rows), cols=len(rows[0]))
        for row, values in zip(table.rows, rows):
            for cell, value in zip(row.cells, values):
                cell.text = value
    path = tmp_path / 'different-rule-layout.docx'
    doc.save(path)
    c = Catalog([path])
    units = rule_units(c, {'sheets': {}, 'unreadable': {}})
    by_text = {v['text']: v for v in units.values() if v.get('table')}
    assert by_text['表名 | 字段 | 唯一键']['structural_header']
    assert not by_text['标准组 | 25'].get('structural_header')
    assert not by_text['标准组 | 按实际确认名单'].get('structural_header')


def test_structured_json_and_tsv_are_real_data_sources(tmp_path):
    a=tmp_path/'data.json';a.write_text('{"orders":[{"id":"0001","amount":100},{"id":"0002","amount":200}]}',encoding='utf8')
    b=tmp_path/'data.tsv';b.write_text('id\tamount\n0003\t300\n',encoding='utf8')
    c=Catalog([a,b]);assert len(c.sheets)==2
    assert c.sheets['S1'].rows[1]==('0001',100) and c.sheets['S2'].rows[1]==('0003','300')


def test_unsupported_material_is_not_silently_dropped_in_strict_chain(tmp_path):
    from bonus_agent.auto import collect_files
    (tmp_path/'known.csv').write_text('a,b\n1,2',encoding='utf8');(tmp_path/'unknown.xls').write_bytes(b'old format')
    with pytest.raises(ValueError,match='unknown.xls'):collect_files([tmp_path],strict_formats=True)


@pytest.mark.parametrize('labels', [
    ['100万以下', '200-300万', '300万以上'],
    ['100万以下', '80-300万', '300万以上'],
    ['100万以下', '100-300万'],
])
def test_uncovered_or_overlapping_intervals_are_rejected(labels):
    with pytest.raises(ChainError):
        _steps([(x, D('.01'), {}) for x in labels], percent=False,
               includes=[True] * len(labels), where='new schedule')


def test_mixed_units_are_scaled_per_endpoint():
    steps, _ = _steps([('5000万以下', D('.01'), {}), ('5000万-1亿', D('.02'), {}), ('1亿以上', D('.03'), {})],
                     percent=False, includes=[True, True, True], where='new schedule')
    assert [x[0] for x in steps] == [D(0), D(50000000), D(100000000)]
    assert bracket(D(75000000), steps)[1] == D('.02')


@given(a=st.integers(min_value=1,max_value=5000), gap=st.integers(min_value=1,max_value=5000))
def test_interval_gap_is_not_silently_filled(a, gap):
    b = a + gap
    labels = [f'{a}万以下', f'{b}万以上']
    with pytest.raises(ChainError):
        _steps([(x, D('.01'), {}) for x in labels], percent=False, includes=[True, True], where='generated')


def test_v1_unrepresented_factor_must_not_compile():
    cat = Catalog([])
    texts = {'total':'提成总额=阶梯提成×区域系数×客户系数×毛利系数×回款及时率系数',
             'hold':'库存、逾期的扣减金额相加', 'freeze':'匹配欠款清单冻结',
             'project_round':'项目可分配金额保留到分'}
    evidence = {}
    for key, text in texts.items():
        cat._block('new_rules.md', key, text)
        sid = next(reversed(cat.blocks))
        evidence[key] = {'id':sid, 'quote':text}
    policy = {'hold_method':'additive', 'freeze_source':'list', 'project_round_digits':2, 'evidence':evidence}
    with pytest.raises(ChainError, match='总额|公式'):
        compile_hold_policy(cat, policy, set(cat.blocks))
