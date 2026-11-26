import copy
import json
from decimal import Decimal as D

import pytest
from pptx import Presentation
from pptx.util import Inches

from bonus_agent.agent import Workspace, ToolError
from bonus_agent.auto import build_inventory
from bonus_agent.llm import FakeLLM
from bonus_core.dsl.models import RuleSet, TierTable
from bonus_core.dsl.validator import validate
from bonus_core.engine.evaluator import tier_rate, tier_amount, EvalError
from bonus_core.dsl.excel_formula import Params, Formula

pytestmark = pytest.mark.evidence


def workspace(tmp_path, labels=None, percent_values=False, row_label=None):
    labels = labels or ['<30% (含)', '30-65% (含)', '65-90%', '90% (含)+']
    prs=Presentation();slide=prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_textbox(Inches(0),Inches(0),Inches(7),Inches(1)).text='销量完成率系数表'
    table=slide.shapes.add_table(2,len(labels)+1, Inches(0),Inches(1), Inches(8),Inches(3)).table
    table.cell(1,0).text=row_label or ('指标系数(%)' if percent_values else '指标系数')
    values=['60','90','100','120'] if percent_values else ['0.6','0.9','1.0','1.2']
    for col,(label,value) in enumerate(zip(labels, values),1):
        table.cell(0,col).text=label;table.cell(1,col).text=value
    path=tmp_path/'rules.pptx';prs.save(path)
    ws=Workspace(FakeLLM([]),build_inventory([path]))
    args={'name':'coefficient','scheme':'S','applies_to':'销量完成率系数表',
          'cells_from':{'file':path.name,'page':1,'table':1},
          'rows':[{'range':f'R1C{n}','value':f'R2C{n}'} for n in range(2,len(labels)+2)]}
    return ws,args


def test_complete_registered_row_can_supply_missing_table_but_partial_row_cannot(tmp_path):
    from types import SimpleNamespace

    from bonus_agent.coefficient import registered_coefficient_call

    ws, args = workspace(tmp_path, row_label='本事业部销售任务')
    source = args['cells_from']
    subject = SimpleNamespace(quote='事业部达成比例｜对应月份，事业部，按销售额及预算取系数')
    other = SimpleNamespace(quote='客户达成比例｜对应月份，客户，按销售额及预算取系数')
    cells = [{'row': 2, 'column': col} for col in range(2, 6)]
    ws.record_parameter_cells(**source, cells=cells[:-1], applies_to=args['applies_to'])
    assert registered_coefficient_call(ws, subject, args['name'], args['scheme']) is None
    ws.record_parameter_cells(**source, cells=cells[-1:], applies_to=args['applies_to'])
    assert registered_coefficient_call(ws, other, args['name'], args['scheme']) is None
    call = registered_coefficient_call(ws, subject, args['name'], args['scheme'])
    assert call == args
    assert ws.compile_coefficient_table(**call)['status'] == 'PASS'
    assert not ws._binding_gaps()


def test_scope_error_returns_exact_title_from_selected_series(tmp_path):
    ws, args = workspace(tmp_path, row_label='东区系数')
    with pytest.raises(ToolError, match='source.scope') as failed:
        ws.compile_coefficient_table(**{**args, 'applies_to': '模型改写的区域名称'})
    retry = json.loads(str(failed.value).split('next_call=', 1)[1])
    assert retry['tool'] == 'compile_coefficient_table'
    assert retry['args'] == {**args, 'applies_to': '东区系数'}
    assert not ws.facts
    assert ws.compile_coefficient_table(**retry['args'])['status'] == 'PASS'


@pytest.mark.parametrize('raw_series', [False, True])
def test_checked_regional_series_bind_optional_metric_before_model_call(tmp_path, raw_series):
    import openpyxl

    from bonus_agent.construction import TaskBoard
    from bonus_agent.rule_package import prepare_formula_inputs
    from bonus_core.engine.run import Engine

    policy = tmp_path / '口径.md'
    exact = '按项目准确匹配验收率，查不到验收率时，匹配系数指定0.75'
    if not raw_series:
        exact = '验收率 | ' + exact
    empty = '验收率单元格是空的，也算查不到，系数为 0.75。有验收率结果的，低于最低档一律取最低档系数。'
    quotes = [exact, empty]
    if raw_series:
        quotes += ['特别组项目的验收系数用特别组的验收系数表。',
                   '当区域=东区，客户简称2不等于甲，归类区域为特别组。']
    policy.write_text('\n'.join(quotes), encoding='utf-8')
    excel = tmp_path / '业务明细.xlsx'
    book = openpyxl.Workbook()
    projects = book.active
    projects.title = '对象'
    projects.append(['项目编号', '区域', '客户简称'])
    examples = [('P1', '东区', '甲', None), ('P2', '东区', '甲', 0), ('P3', '西区', '甲', .6),
                ('P4', '西区', '甲', -.1), ('P5', '西区', '甲', None)]
    if raw_series:
        examples.append(('P6', '东区', '乙', .6))
    for key, region, customer, _ in examples:
        projects.append([key, region, customer])
    rates = book.create_sheet('指标')
    rates.append(['项目编号', '验收率'])
    for key, _, _, rate in examples:
        if key != 'P5':
            rates.append([key, rate])
    book.save(excel)
    book.close()

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    matrix = [('实际验收率范围', '东区系数', '西区系数'),
              ('<50% (含)', '0.8', '0.9'), ('50%以上(不含)', '1.2', '1.3')]
    if raw_series:
        matrix = [(*row, extra) for row, extra in zip(matrix, ('特别组系数', '0.95', '1.6'))]
    table = slide.shapes.add_table(3, len(matrix[0]), Inches(0), Inches(1), Inches(8), Inches(3)).table
    for r, values in enumerate(matrix):
        for c, value in enumerate(values):
            table.cell(r, c).text = value
    ppt = tmp_path / '制度.pptx'
    prs.save(ppt)
    ws = Workspace(FakeLLM([]), build_inventory([policy, excel, ppt]))
    ws.workflow['enabled'] = True
    ws.plan_calculation('按项目核算验收系数', ['金额'], {policy.name: 'rule', ppt.name: 'rule', excel.name: 'data'}, ['S'])
    ws.record_clauses([{'file': policy.name, 'quote': q, 'point': q} for q in quotes])
    key = {'record_key': {'column': '项目编号', 'type': 'id'}}
    ws.define_dataset('项目', excel.name, '对象', {**key, 'territory': {'column': '区域', 'type': 'text'},
                      'short_name': {'column': '客户简称', 'type': 'text'}}, key='record_key')
    ws.define_dataset('验收记录', excel.name, '指标', {**key, 'inspection': {'column': '验收率', 'type': 'ratio'}}, link='record_key')
    ws.plan_formula_tasks([{'id': 'factor', 'scheme': 'S', 'target': '/variables/quality_factor',
                            'clauses': ['C1', 'C2', 'C3'] if raw_series else ['C1', 'C2']},
                           {'id': 'pool', 'scheme': 'S', 'target': '/pool', 'depends_on': ['factor']}])
    if not raw_series:
        for name, column, label in [('east', 2, '东区系数'), ('west', 3, '西区系数')]:
            rows = [{'range': f'R{r}C1', 'value': f'R{r}C{column}'} for r in (2, 3)]
            assert ws.compile_coefficient_table(name, rows, {'file': ppt.name, 'page': 1, 'table': 1},
                                                label, scheme='S')['status'] == 'PASS'
    assert ws.prepare_data()['status'] == 'PASS'
    checked = prepare_formula_inputs(ws)
    assert checked and checked['saved'], checked
    task = TaskBoard(ws).statuses()['factor']
    assert task['status'] == 'PASS' and len(task['depends_on']) == (4 if raw_series else 2)
    rules = ws._ruleset('S')
    rules = rules.model_copy(update={'pool': rules.pool.model_copy(update={'scope': '项目', 'expr': 'quality_factor'})})
    data, _ = ws._data(rules)
    engine = Engine.prepare_basis(rules, data)
    values = {k: engine.ev.evaluate('quality_factor', n, n.id, 'variables.quality_factor')
              for k, n in engine.ev.index['项目'].items()}
    expected = dict(zip(('P1', 'P2', 'P3', 'P4', 'P5'), map(D, ('.75', '.8', '1.3', '.9', '.75'))))
    if raw_series:
        expected['P6'] = D('1.6')
    assert values == expected
    assert not ws.llm.calls


def test_compile_all_source_endpoints_and_exact_mixed_boundaries(tmp_path):
    ws,args=workspace(tmp_path)
    assert ws.compile_coefficient_table(**args)['status']=='PASS'
    table=ws._ruleset('S').tables['coefficient']
    assert table.step_inclusive==[True,False,False,True]
    for amount,expected in [('-9','.6'),('0','.6'),('.3','.6'),('.30001','.9'),
                            ('.65','.9'),('.65001','1'),('.89999','1'),('.9','1.2'),('1.7','1.2')]:
        assert tier_rate(table,D(amount))==D(expected)
    assert not ws._binding_gaps()
    with pytest.raises(EvalError,match='tier_rate'):
        tier_amount(table,D('.4'))
    ws.set_rules(scheme='S',pool={'expr':'tier(coefficient, 0.4)'})
    assert any(i.code=='fn_table' and 'tier_rate' in i.message for i in validate(ws._ruleset('S')))
    assert not ws.llm.calls


@pytest.mark.parametrize('layout', ['keyed', 'linked', 'unbound', 'raw_series', 'duplicate', 'wrong_series'])
def test_monthly_group_preflight_separates_valid_ratio_from_missing_budget(tmp_path, layout):
    import openpyxl

    from bonus_agent.construction import TaskBoard
    from bonus_agent.rule_package import prepare_formula_inputs
    from bonus_core.data.period import Period
    from bonus_core.engine.run import Engine

    combined = layout in ('unbound', 'raw_series', 'duplicate', 'wrong_series')
    _, coefficient = workspace(tmp_path, row_label='客户销售任务' if layout == 'wrong_series' else '事业部销售任务')
    ppt = tmp_path / coefficient['cells_from']['file']
    headings = '事业部简称与事业部名称' if combined else '事业部'
    quotes = [
        f'对应月份，{headings}，求和销售额/预算数，查不到预算时，匹配系数指定0.95',
        '当月实际销售额（销售金额，本位币）合计 ÷ 当月预算。',
        '查不到预算、当月预算为 0 时，该项系数指定为 0.95。',
        '参考数据里有、但销售单里没有的事业部或金额，不参与核算。',
    ]
    policy = tmp_path / '依据.md'
    policy.write_text('\n'.join(quotes), encoding='utf-8')
    excel = tmp_path / '业务流水.xlsx'
    book = openpyxl.Workbook()
    sales = book.active
    sales.title = '流水'
    sales_header, budget_header = ('事业部简称', '事业部名称') if combined else ('事业部', '事业部')
    sales.append(['业务编号', '期间', sales_header, '销售金额(本位币)'])
    for idx, (group, amount) in enumerate(zip('ABCDE', (80, 0, 42, 31, 18)), 1):
        sales.append([str(idx), 202704, group, amount])
    sales.append(['6', 202703, 'A', 900])
    budget = book.create_sheet('预算')
    budget.append([budget_header, *[f'{m}月金额' for m in range(1, 13)]])
    for group, amount in [('A', 80), ('B', 40), ('D', 0), ('E', None), ('F', 123)]:
        label = 'Ａ' if combined and group == 'A' else group
        budget.append([label, 999, 999, 999, amount, *([999] * 8)])
    if layout == 'duplicate':
        budget.append(['A', 999, 999, 999, 35, *([999] * 8)])
    if layout == 'linked':
        unrelated = book.create_sheet('历史索引')
        unrelated.append(['事业部', '索引字段'])
        for group in 'ABDEF':
            unrelated.append([group, '有效'])
    book.save(excel)
    book.close()
    ws = Workspace(FakeLLM([]), build_inventory([policy, excel, ppt]))
    ws.workflow['enabled'] = True
    ws.period = Period.parse('2027-04')
    ws.plan_calculation('按事业部达成计算', ['资金'], {policy.name: 'rule', ppt.name: 'rule', excel.name: 'data'}, ['S'])
    ws.record_clauses([{'file': policy.name, 'quote': quote, 'point': quote} for quote in quotes])
    identity = {'ref': {'column': '业务编号', 'type': 'id'}, 'posting': {'column': '期间', 'type': 'text'}}
    team = {'team': {'column': sales_header, 'type': 'text'}}
    ws.define_dataset('accounts', excel.name, '流水', {**identity, **team}, key='ref', period='posting')
    ws.define_dataset('posted', excel.name, '流水', {**identity, **team, 'income': {
        'column': '销售金额(本位币)', 'type': 'money', 'unit': '元'}}, link='ref', period='posting')
    months = {f'value_{m}': {'column': f'{m}月金额', 'type': 'money', 'unit': '元'} for m in range(1, 13)}
    reference = {'budget_team': {'column': budget_header, 'type': 'text'}, **months}
    if layout == 'linked':
        ws.define_dataset('warehouse_index', excel.name, '历史索引', {'budget_team': team['team']}, key='budget_team')
        ws.define_dataset('targets', excel.name, '预算', reference, link='budget_team')
    elif layout == 'keyed':
        ws.define_dataset('targets', excel.name, '预算', reference, key='budget_team')
    else:
        ws.define_dataset('targets', excel.name, '预算', reference)
    requirements = {'data_requirements': [{'dataset': 'targets', 'fields': list(reference), 'link': 'budget_team'}]} if layout == 'linked' else {}
    factors = ([{'id': 'factor', 'scheme': 'S', 'target': '/variables/factor', 'title': '事业部达成系数',
                 'clauses': ['C1', 'C2', 'C3']}] if combined else [
        {'id': 'ratio', 'scheme': 'S', 'target': '/variables/achievement', 'clauses': ['C1', 'C2', 'C3'], 'expected_scope': '事业部',
         **requirements},
        {'id': 'factor', 'scheme': 'S', 'target': '/variables/factor', 'clauses': ['C3'], 'expected_scope': '事业部',
         'depends_on': ['table', 'ratio']},
    ])
    tables = [] if layout == 'raw_series' else [{'id': 'table', 'scheme': 'S', 'target': '/tables/coefficient', 'clauses': ['C3']}]
    ws.plan_formula_tasks([
        *tables, *factors,
        {'id': 'pool', 'scheme': 'S', 'target': '/pool', 'depends_on': ['factor']},
    ])
    if layout == 'raw_series':
        got = ws.record_parameter_cells(**coefficient['cells_from'], applies_to=coefficient['applies_to'],
                                        cells=[{'row': 2, 'column': int(row['value'].split('C')[1])}
                                               for row in coefficient['rows']])
        assert got['status'] == 'PASS'
    else:
        assert ws.compile_coefficient_table(**coefficient)['status'] == 'PASS'
    prepared = ws.prepare_data()
    assert prepared['status'] == 'PASS', prepared.get('issues')
    result = prepare_formula_inputs(ws)
    if layout in ('duplicate', 'wrong_series'):
        assert result is None or not result['saved']
        if layout == 'duplicate':
            assert result is not None and '多条预算记录' in result['issues'][0]
        assert ws.datasets['targets'].link is None and 'factor' not in ws._ruleset('S').variables
        return
    assert result['saved'], result['issues']
    board = TaskBoard(ws)
    assert board.statuses()['factor']['status'] == 'PASS'
    if layout == 'linked':
        assert ws.datasets['warehouse_index'].key == 'budget_team'
        assert ws.datasets['targets'].link == ws.datasets['group_team'].key == 'team'
    elif layout in ('unbound', 'raw_series'):
        assert ws.datasets['targets'].link == 'team'
        assert ws.mapping['targets'].columns['team'] == budget_header
        linked_table = 'factor_intervals' if layout == 'raw_series' else 'table'
        assert board.task_spec('factor')['depends_on'] == [linked_table]
        assert board.statuses()[linked_table]['status'] == 'PASS'
    rs = ws._ruleset('S')
    scope = rs.variables['factor'].scope
    rs = rs.model_copy(update={'pool': rs.pool.model_copy(update={'scope': scope, 'expr': 'factor'})})
    data, _ = ws._data(rs)
    engine = Engine.prepare_basis(rs, data, period=ws.period)
    actual = {key: engine.ev.evaluate('factor', node, node.id, 'variables.factor')
              for key, node in engine.ev.index[scope].items()}
    assert actual == dict(zip('ABCDE', map(D, ('1.2', '.6', '.95', '.95', '.95'))))
    assert not ws.llm.calls


@pytest.mark.parametrize('complete', [True, False])
def test_planned_coefficient_requires_every_physical_band(tmp_path, complete):
    from bonus_agent.coefficient import prepare_planned_coefficient
    from bonus_agent.construction import TaskBoard

    ws, args = workspace(tmp_path, row_label='本季度出货系数')
    source = args['cells_from']
    ws.workflow.update(enabled=True, plan={'schemes': ['S']})
    selected = range(2, 6 if complete else 5)
    evidence = ws.record_parameter_cells(**source, applies_to=args['applies_to'],
                                          cells=[{'row': 2, 'column': col} for col in selected])
    ws.plan_formula_tasks([{'id': 'coef', 'scheme': 'S', 'target': '/tables/coefficient',
                            'facts': [row['name'] for row in evidence['facts']]}])
    board = TaskBoard(ws)
    result = prepare_planned_coefficient(board, board.task_spec('coef'))
    if complete:
        assert result['status'] == 'PASS' and result['saved']
        assert tier_rate(ws._ruleset('S').tables['coefficient'], D('.30')) == D('.6')
    else:
        assert result is None and 'coefficient' not in ws._ruleset('S').tables


def test_shared_boundary_evidence_survives_second_table_and_idempotent_recompile(tmp_path):
    ws,args=workspace(tmp_path)
    ws.compile_coefficient_table(**args)
    ws.compile_coefficient_table(**{**args,'name':'second'})
    assert not ws._binding_gaps()
    before=copy.deepcopy([f.model_dump() for f in ws.facts])
    drafts=copy.deepcopy(ws.drafts)
    ws.compile_coefficient_table(**args)
    assert [f.model_dump() for f in ws.facts]==before
    assert ws.drafts==drafts


def test_wrong_coefficient_task_rolls_back_staged_cell_registration(tmp_path):
    ws, args = workspace(tmp_path)
    ws.workflow.update(enabled=True, plan={'schemes': ['S']})
    record = ws.record_parameter_cells('rules.pptx', 1, 1, [{'row': 2, 'column': 2}], args['applies_to'])
    ws.plan_formula_tasks([{'id': 'factor', 'scheme': 'S', 'target': '/tables/coefficient',
                            'facts': [record['facts'][0]['name']]}])
    before = copy.deepcopy(ws.facts)
    with pytest.raises(ToolError, match='construction.table_target'):
        ws.compile_coefficient_table(**{**args, 'name': 'other'})
    assert ws.facts == before and 'other' not in ws._merged('S').get('tables', {})
    assert ws.compile_coefficient_table(**args)['status'] == 'PASS'
    assert ws.check_formula_task('factor')['status'] == 'PASS'


def test_explicit_value_unit_header_is_used_without_borrowing_input_percentages(tmp_path):
    ws,args=workspace(tmp_path,percent_values=True)
    ws.compile_coefficient_table(**args)
    table=ws._ruleset('S').tables['coefficient']
    assert tier_rate(table,D('.4'))==D('.9')
    assert not ws._binding_gaps()


def test_inclusion_annotation_attached_to_lower_endpoint_is_not_upper_inclusion(tmp_path):
    ws,args=workspace(tmp_path,['<7%','7% (含)-13%','13% (含)-19%','19%(含)+'])
    ws.compile_coefficient_table(**args)
    table=ws._ruleset('S').tables['coefficient']
    assert table.step_inclusive==[True,True,True,True]
    for value,expected in [('.06999','.6'),('.07','.9'),('.13','1'),('.19','1.2')]:
        assert tier_rate(table,D(value))==D(expected)


def test_legacy_failure_returns_executable_source_based_repair(tmp_path):
    ws,args=workspace(tmp_path)
    legacy={'name':'coefficient','scheme':'S','mode':'whole','cells_from':args['cells_from'],
            'algorithm_basis':{'file':'rules.pptx','page':1,'quote':'销量完成率系数表'},
            'rows':[{'lower':'-1' if n==2 else f'R1C{n}','value':f'R2C{n}'} for n in range(2,6)]}
    before=copy.deepcopy(ws.drafts)
    with pytest.raises(ToolError,match='next_call=') as failure:
        ws.compile_table(**legacy)
    assert ws.drafts==before and not ws.facts
    action=json.loads(str(failure.value).split('next_call=',1)[1])
    assert action['tool']=='compile_coefficient_table'
    assert getattr(ws,action['tool'])(**action['args'])['status']=='PASS'
    assert not ws._binding_gaps()


def test_task_routes_explicit_coefficient_intent_and_keeps_amount_algorithm_gate(tmp_path):
    ws,args=workspace(tmp_path)
    ws.plan_calculation('核对原文区间系数',['系数表'],{'rules.pptx':'rule'},['S'])
    ws.plan_formula_tasks([{'id':'coef','scheme':'S','target':'/tables/coefficient','title':'指标区间系数表'},
                           {'id':'pool','scheme':'S','target':'/pool','depends_on':['coef']}])
    from bonus_agent.construction import TaskBoard
    assert 'compile_coefficient_table' in TaskBoard(ws).summary()['next']
    ws.compile_coefficient_table(**args)
    assert ws.check_formula_task('coef')['status']=='PASS'
    # Relabeling a table cannot bypass an explicit source requirement for marginal money.
    p=tmp_path/'money.md';p.write_text('奖金必须按超额累进计算。',encoding='utf-8')
    other=Workspace(FakeLLM([]),build_inventory([*ws.inv.files,p]))
    other.record_clauses([{'file':p.name,'point':'超额累进金额','quote':'奖金必须按超额累进计算'}])
    other.set_rules(scheme='S',tables=ws.drafts['S']['tables'],pool={'expr':'tier_rate(coefficient, 0.4) * 100'})
    other.cover_clauses({'C1':'pool'})
    from bonus_agent.semantics import semantic_gates
    assert any('algorithm.scope' in issue for issue in semantic_gates(other))


def test_formula_cannot_replace_a_planned_coefficient_with_its_raw_input(tmp_path):
    ws, args = workspace(tmp_path)
    ws.plan_calculation('按区间核算系数', ['系数'], {'rules.pptx': 'rule'}, ['S'])
    ws.plan_formula_tasks([
        {'id': 'table', 'scheme': 'S', 'target': '/tables/coefficient'},
        {'id': 'band', 'scheme': 'S', 'target': '/variables/selected_band', 'depends_on': ['table']},
        {'id': 'factor', 'scheme': 'S', 'target': '/variables/achieved_rate', 'depends_on': ['table', 'band']},
        {'id': 'fund', 'scheme': 'S', 'target': '/pool', 'depends_on': ['factor']},
    ])
    assert ws.compile_coefficient_table(**args)['status'] == 'PASS'
    assert ws.check_formula_task('table')['status'] == 'PASS'
    assert ws.submit_formula_task('band', value={'expr': 'tier_rate(coefficient, 1)'})['saved']
    valid = ws.submit_formula_task('factor', value={'expr': 'selected_band'})
    assert valid['status'] == 'PASS' and valid['saved']

    invalid = ws.submit_formula_task('factor', changes={'expr': '1'})
    assert invalid['status'] == 'FAIL' and not invalid['saved']
    assert any('construction.coefficient_input' in message and 'coefficient' in message
               for message in invalid['issues'])
    assert ws._ruleset('S').variables['achieved_rate'].expr == 'selected_band'


def test_ambiguous_or_wrong_source_never_partially_registers(tmp_path):
    ws,args=workspace(tmp_path,['<30%','30-65%','65%以上'])
    with pytest.raises(ToolError,match='等于0.65'):
        ws.compile_coefficient_table(**args)
    assert not ws.facts and not ws.drafts
    ws,args=workspace(tmp_path)
    args['rows'][0]['range']='R900C2'
    with pytest.raises(ToolError,match='source.cell'):
        ws.compile_coefficient_table(**args)
    assert not ws.facts and not ws.drafts
    ws,args=workspace(tmp_path)
    args['applies_to']='另一事业部'
    with pytest.raises(ToolError,match='不在'):
        ws.compile_coefficient_table(**args)
    assert not ws.facts and not ws.drafts


def test_malformed_boundary_metadata_and_legacy_behavior():
    with pytest.raises(ValueError,match='逐档对应'):
        TierTable(mode='whole',steps=[(0,1),(1,2)],step_inclusive=[True])
    with pytest.raises(ValueError,match='超额累进'):
        TierTable(mode='excess',purpose='coefficient',steps=[(0,1)])
    legacy=TierTable(mode='whole',steps=[(0,'.1'),(10,'.2')],lower_inclusive=False)
    assert tier_rate(legacy,D(10))==D('.1')
    assert tier_rate(legacy,D(0))==0


def test_exported_formula_keeps_each_original_boundary(tmp_path):
    ws,args=workspace(tmp_path);ws.compile_coefficient_table(**args)
    rs=ws._ruleset('S');tables=Params();tables.place(rs,{'coefficient'})
    from bonus_core.dsl.parser import parse
    formula=Formula(rs,tables,{}).tr(parse('tier_rate(coefficient, 0.9)'))
    # The final boundary includes equality; the earlier boundaries exclude it.
    assert '0.9>=' in formula and '0.9>' in formula
    assert rs.model_dump(mode='json')['tables']['coefficient']['step_inclusive']==[True,False,False,True]
