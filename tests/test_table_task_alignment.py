import copy
from decimal import Decimal

import pytest

from bonus_agent.agent import ToolError
from bonus_agent.construction import TaskBoard
from tests.test_staged_workflow import make_workspace


def table_plan(ws):
    ws.plan_formula_tasks([
        {'id': 'rates', 'scheme': 'S', 'target': '/tables/rate~1A', 'facts': ['提成率']},
        {'id': 'pool', 'scheme': 'S', 'target': '/pool', 'depends_on': ['rates']},
    ])


@pytest.mark.parametrize('unknown_region', [False, True])
def test_monthly_amount_tiers_follow_new_dates_and_physical_rate_tables(tmp_path, unknown_region):
    from datetime import date

    from pptx import Presentation
    from pptx.util import Inches

    from bonus_agent.auto import build_inventory
    from bonus_agent.case import fingerprint
    from bonus_agent.rule_package import prepare_formula_inputs
    from bonus_core.data.period import Period
    from bonus_core.engine.run import Engine

    basis = '阶梯提成按项目的合同基数计算。'
    mode = '按区域对应超额累进阶梯比例计算'
    negative = '计算基数为负数时按阶梯照算，不记为0。'
    age = '销售单月份与首发日期的月份对比，月数大于12个月，判定为老项目，否则为新项目'
    quotes = (basis, mode, negative, age)

    def source_rows(wb):
        sheet = wb['projects']
        sheet['B2'], sheet['B3'] = 200, 600
        sheet['C1'], sheet['C2'] = '区域', '海运-欧洲一组'
        sheet['C3'] = '海运-未知组' if unknown_region else '海运-欧洲二组'
        dates = wb.create_sheet('发货台账')
        dates.append(['项目编码', '月份', '首发日期'])
        dates.append(['A', '202705', date(2026, 5, 31)])
        dates.append(['B', '202705', date(2026, 4, 30)])

    ws = make_workspace(tmp_path, mutate_workbook=source_rows, policy_tail='\n\n' + '\n\n'.join(quotes))
    ws.period = Period.parse('2027-05')
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    slide.shapes.add_textbox(Inches(0), Inches(0), Inches(7), Inches(1)).text = '海运事业部\n销售提成总包计算 (欧洲)'
    table = slide.shapes.add_table(4, 3, Inches(0), Inches(1), Inches(7), Inches(2)).table
    for row, cells in enumerate([['销售额阶梯', '新项目', '老客户（旧项目）'],
                                ['600元以上', '4%', '2%'], ['200-600元', '3%', '1.5%'], ['200元以下', '2%', '1%']]):
        for col, value in enumerate(cells):
            table.cell(row, col).text = value
    file = tmp_path / 'rates.pptx'
    deck.save(file)
    inputs = [*ws.inv.files, file]
    ws.inv = build_inventory(inputs)
    ws._source_version = fingerprint(inputs)
    plan = ws.workflow['plan']
    ws.plan_calculation(plan['summary'], plan['outputs'], {**plan['files'], file.name: 'rule'}, ['S'])
    ws.record_clauses([{'file': 'policy.md', 'point': q, 'quote': q} for q in quotes])
    ws.define_dataset('projects', 'data.xlsx', 'projects', {
        'pid': {'column': 'pid', 'type': 'id'}, 'amount': {'column': 'amount', 'type': 'money'},
        'team': {'column': '区域', 'type': 'text'}}, key='pid')
    ws.define_dataset('ship_info', 'data.xlsx', '发货台账', {
        'pid': {'column': '项目编码', 'type': 'id'}, 'mth': {'column': '月份', 'type': 'text'},
        'sent': {'column': '首发日期', 'type': 'date'}}, link='pid', period='mth')
    ws.plan_formula_tasks([
        {'id': 'base', 'scheme': 'S', 'target': '/variables/合同基数', 'clauses': ['C1'], 'expected_scope': '项目'},
        {'id': 'tier', 'scheme': 'S', 'target': '/variables/阶梯提成', 'clauses': ['C2'], 'depends_on': ['base']},
        {'id': 'pool', 'scheme': 'S', 'target': '/pool', 'depends_on': ['tier']},
    ])
    assert ws.prepare_data()['status'] == 'PASS'
    value = {'scope': '项目', 'expr': 'amount', 'sources': [{'doc': 'policy.md', 'quote': ws.clauses[0].quote}]}
    assert ws.submit_formula_task('base', value=value)['saved']
    age_report = prepare_formula_inputs(ws)
    assert age_report['saved'] and age_report['task'] == 'tier:age', age_report
    tier_report = prepare_formula_inputs(ws)
    if unknown_region:
        assert not tier_report['saved'] and '海运-未知组' in tier_report['issues'][0]
        assert '阶梯提成' not in ws._ruleset('S').variables
        return
    assert tier_report['saved'] and len(tier_report['tier_inputs']) == 2, tier_report
    rs = ws._ruleset('S')
    rule = rs.model_copy(update={'pool': rs.pool.model_copy(update={'expr': '阶梯提成'})})
    data, _ = ws._data(rule)
    engine = Engine.prepare_basis(rule, data, period=ws.period)
    actual = {node.attrs['pid']: (engine.ev.variable('新老项目', node).v, engine.ev.variable('阶梯提成', node).v)
              for node in engine.by_level['项目']}
    assert actual == {'A': ('新项目', Decimal(4)), 'B': ('老项目', Decimal(8))}


def test_physical_fee_columns_are_compiled_with_downstream_classification_and_negative_policy(tmp_path):
    from pptx import Presentation
    from pptx.util import Inches

    from bonus_agent.auto import build_inventory
    from bonus_agent.case import fingerprint
    from bonus_agent.rule_package import prepare_formula_inputs
    from bonus_core.engine.evaluator import tier_amount

    mode = '合同奖金按超额累进阶梯计算。'
    negative = '计算基数为负数时按阶梯照算，不记为0。'
    classification = '项目编号 A 判定为老项目，否则为新项目。'
    ws = make_workspace(tmp_path, policy_tail=f'\n\n{mode}\n\n{negative}\n\n{classification}')
    file = tmp_path / 'bands.pptx'
    ppt = Presentation()
    slide = ppt.slides.add_slide(ppt.slide_layouts[6])
    title = '合同阶梯费率'
    slide.shapes.add_textbox(Inches(0), Inches(0), Inches(5), Inches(1)).text = title
    table = slide.shapes.add_table(4, 3, Inches(0), Inches(1), Inches(5), Inches(2)).table
    for r, values in enumerate([['合同额', '新签', '续签'], ['600元以上', '4%', '2%'],
                                 ['200-600元', '3%', '1.5%'], ['200元以下', '2%', '1%']]):
        for c, value in enumerate(values):
            table.cell(r, c).text = value
    ppt.save(file)
    inputs = [*ws.inv.files, file]
    ws.inv = build_inventory(inputs)
    ws._source_version = fingerprint(inputs)
    plan = ws.workflow['plan']
    ws.plan_calculation(plan['summary'], plan['outputs'], {**plan['files'], file.name: 'rule'}, ['S'])
    ws.record_clauses([{'file': 'policy.md', 'point': text, 'quote': text} for text in (mode, negative, classification)])
    data = ws.record_parameter_cells(file.name, 1, 1, applies_to=title,
                                     cells=[{'row': row, 'column': col} for row in (2, 3, 4) for col in (2, 3)])
    assert data['status'] == 'PASS'
    facts = [row['name'] for row in data['facts']]
    ws.plan_formula_tasks([
        {'id': 'table', 'scheme': 'S', 'target': '/tables/contract_tier', 'clauses': ['C2'], 'facts': facts},
        {'id': 'class', 'scheme': 'S', 'target': '/variables/existing_customer', 'clauses': ['C4']},
        {'id': 'fee', 'scheme': 'S', 'target': '/variables/contract_fee', 'clauses': ['C2', 'C3'],
         'depends_on': ['table', 'class']},
        {'id': 'pool', 'scheme': 'S', 'target': '/pool', 'depends_on': ['fee']},
    ])
    assert ws.prepare_data()['status'] == 'PASS'
    prepared = prepare_formula_inputs(ws)
    assert prepared['saved'] and len(prepared['checked']) == 2, prepared
    board = TaskBoard(ws)
    assert all(board.statuses()[t['task']]['status'] == 'PASS' for t in prepared['checked'])
    new, old = [ws._ruleset('S').tables[t['target'].split('/')[-1]] for t in prepared['checked']]
    assert (tier_amount(new, Decimal(-50)), tier_amount(old, Decimal(-50))) == (Decimal(-1), Decimal('-.5'))
    assert (tier_amount(new, Decimal(600)), tier_amount(old, Decimal(600))) == (Decimal(16), Decimal(8))
    assert (tier_amount(new, Decimal(700)), tier_amount(old, Decimal(700))) == (Decimal(20), Decimal(10))
    assert ws.submit_formula_task('class', value={'scope': '项目', 'expr': 'pid == "A"',
        'sources': [{'doc': 'policy.md', 'quote': classification}]})['status'] == 'PASS'
    values = [t['target'].split('/')[-1] for t in prepared['checked']]
    new_call, old_call = [f'tier({name}, amount)' for name in values]
    for expr in (new_call, f'if(amount > 0, {old_call}, {new_call})'):
        check = ws.submit_formula_task('fee', value={'scope': '项目', 'expr': expr})
        assert not check['saved'] and any('algorithm.tier_dispatch' in i for i in check['issues'])
    passed = ws.submit_formula_task('fee', value={'scope': '项目',
                                    'expr': f'if(existing_customer, {old_call}, {new_call})',
                                    'sources': [{'doc': 'policy.md', 'quote': q} for q in (mode, negative)]})
    assert passed['status'] == 'PASS', passed


def test_misdirected_source_rate_cannot_write_an_unplanned_table(tmp_path):
    ws = make_workspace(tmp_path)
    table_plan(ws)
    rows = [{'key': 'sale', 'value': '提成率'}]
    drafts = copy.deepcopy(ws.drafts)
    bindings = list(ws.facts[0].targets)
    with pytest.raises(ToolError, match='construction.table_target') as failure:
        ws.compile_table('other', rows, scheme='S', kind='lookup')
    assert 'rates' in str(failure.value) and '/tables/rate~1A' in str(failure.value)
    assert ws.drafts == drafts and ws.facts[0].targets == bindings
    ws.record_facts([{'name': 'second_source', 'file': 'policy.md', 'quote': '销售金额的10%', 'value': '10%'}])
    ws.compile_table('independent', [{'key': 'sale', 'value': 'second_source'}], scheme='S', kind='lookup')
    ws.compile_table('rate/A', rows, scheme='S', kind='lookup')
    assert ws.check_formula_task('rates')['status'] == 'PASS'

    # Explicitly planned tables can legitimately use the same source rate.
    ws.plan_formula_tasks([{'id': 'second', 'scheme': 'S', 'target': '/tables/other', 'facts': ['提成率']}])
    ws.compile_table('other', rows, scheme='S', kind='lookup')
    assert ws.check_formula_task('second')['status'] == 'PASS'


def test_unbound_equal_rate_in_another_cell_requires_recompilation(tmp_path):
    ws = make_workspace(tmp_path)
    table_plan(ws)
    ws.set_rules(scheme='S', tables={'rate/A': {'kind': 'lookup', 'mapping': {'a': '0.1', 'b': '0.1'}}})
    fact = ws.facts[0]
    ws.bind_facts([{'name': fact.name, 'targets': [*fact.targets, 'S::/tables/rate~1A/mapping/a']}])
    report = ws.check_formula_task('rates')
    assert report['status'] == 'FAIL'
    assert any('/tables/rate~1A/mapping/b' in issue for issue in report['issues'])
    assert report['next'].startswith('compile_table') and 'rate/A' in report['next']
    ws.compile_table('rate/A', [{'key': k, 'value': fact.name} for k in ('a', 'b')], scheme='S', kind='lookup')
    assert ws.check_formula_task('rates')['status'] == 'PASS'


def test_table_waits_for_its_applicability_variable_before_checking_again(tmp_path):
    ws = make_workspace(tmp_path)
    table_plan(ws)
    ws.plan_formula_tasks([{'id': 'region', 'scheme': 'S', 'target': '/variables/eligible'}])
    ws.compile_table('rate/A', [{'key': 'sale', 'value': '提成率'}], scheme='S', kind='lookup',
                     applies_when='eligible == 1')
    board = TaskBoard(ws)
    assert board.statuses()['rates']['status'] == 'WAITING'
    assert 'region' in board.required('basis')
    assert ws.check_formula_task('rates')['issues'] == ["construction.dependency：先检查依赖任务['region']"]
    ws.set_rules(scheme='S', variables={'eligible': {'expr': 'amount > 0', 'scope': '项目'}})
    assert ws.check_formula_task('region')['status'] == 'PASS'
    assert ws.check_formula_task('rates')['status'] == 'PASS'
    ws.set_rules(scheme='S', variables={'eligible': {'expr': 'amount < 0', 'scope': '项目'}})
    assert board.statuses()['rates']['status'] == 'WAITING'
