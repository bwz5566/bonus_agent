import copy
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from bonus_agent import case
from bonus_agent.agent import Workspace, run_agent
from bonus_agent.auto import build_inventory
from bonus_agent.llm import FakeLLM
from bonus_agent.scenarios import Column, InputTable
from bonus_core.data.period import Period


def workbook(path, sheet, rows):
    book = Workbook()
    book.active.title = sheet
    for row in rows:
        book.active.append(row)
    book.save(path)
    book.close()
    return path


def workspace(tmp_path):
    main = workbook(tmp_path / 'amounts.xlsx', '已生效业务', [
        ['登记号', '金额'], ['K01', 50], ['K02', 70],
    ])
    source = workbook(tmp_path / 'dates.xlsx', '确认记录', [
        ['登记号', '确认日'], ['K01', '2025-01-03'],
    ])
    optional = workbook(tmp_path / 'archive.xlsx', '归档摘要', [['附注'], ['留档']])
    files = {main.name: 'data', source.name: 'data', optional.name: 'reference'}
    ws = Workspace(FakeLLM([]), build_inventory([main, source, optional]))
    ws.workflow['enabled'] = True
    ws.plan_calculation('按本批业务核算', ['底稿'], files, ['S'])
    ws.define_dataset('业务', main.name, '已生效业务',
                      {'key_id': {'column': '登记号', 'type': 'id'},
                       'money': {'column': '金额', 'type': 'money'}}, key='key_id')
    return ws, files


def map_confirmation(ws):
    ws.define_dataset('核对', 'dates.xlsx', '确认记录',
                      {'key_id': {'column': '登记号', 'type': 'id'},
                       'effective': {'column': '确认日', 'type': 'date'}}, link='key_id')


def collection_workspace(tmp_path, *, unrelated=False, include_history=False, ambiguous=False, reference_amount=None):
    labels = ['发生月', '业务编号', '大类', '回款金额(本币)']
    rows = [['2028-07', 'A', '甲区', 100], ['2028-07', 'B', '乙区', 0], ['2028-07', 'C', '甲区', -10]]
    if include_history:
        rows.append(['2028-01', 'A', '甲区', 50])
    if ambiguous:
        labels.append('回款金额(原币)')
        rows = [[*row, row[-1] or 8] for row in rows]
    source = workbook(tmp_path / 'records.xlsx', '登记', [labels, *rows])
    targets = [['大类', '月度目标'], ['甲区', 500], ['乙区', 600], *([['无关区', 700]] if unrelated else [])]
    if reference_amount:
        targets = [[*row, reference_amount if i == 0 else 0] for i, row in enumerate(targets)]
    reference = workbook(tmp_path / 'plan.xlsx', '目标', targets)
    policy = tmp_path / '口径.md'
    zero = '回款金额等于０的记录不计入当期核算。'
    absent = '参考表里有、但主表中没有的金额，不参与核算。'
    policy.write_text(f'# 口径\n\n{zero}\n\n{absent}', encoding='utf-8')
    ws = Workspace(FakeLLM([]), build_inventory([source, reference, policy]))
    ws.workflow['enabled'] = True
    ws.period = Period.parse('2028-07')
    ws.plan_calculation('按有效明细核算', ['底稿'], {source.name: 'data', reference.name: 'data', policy.name: 'rule'}, ['S'])
    fields = {'month': {'column': '发生月', 'type': 'text'}, 'txn': {'column': 'txn', 'type': 'id'}}
    combined = {'txn': ['发生月', '业务编号']}
    ws.define_dataset('当期对象', source.name, '登记', fields, key='txn', period='month', combine=combined, distinct=True)
    ws.define_dataset('按月的回款', source.name, '登记', {**fields, 'amount': {'column': '回款金额(本币)', 'type': 'money'}},
                      link='txn', period='month', window='year_to_date', combine=combined)
    ws.define_dataset('大类实体', source.name, '登记', {'zone': {'column': '大类', 'type': 'text'}}, key='zone', distinct=True)
    target_fields = {'zone': {'column': '大类', 'type': 'text'}, 'limit_sum': {'column': '月度目标', 'type': 'money'}}
    if reference_amount:
        target_fields['ref_amount'] = {'column': reference_amount, 'type': 'money'}
    ws.define_dataset('目标数', reference.name, '目标', target_fields, link='zone')
    return ws, policy, zero, absent


def test_data_receipt_uses_verified_nonzero_rule_and_exact_reference_scope(tmp_path):
    ws, policy, zero, absent = collection_workspace(tmp_path)
    assert ws.prepare_data()['counts']['当期对象'] == 3
    ws.record_clauses([{'point': quote, 'file': policy.name, 'quote': quote} for quote in (zero, absent)])
    assert not ws._flow().current('data')
    result = ws.prepare_data()
    assert result['status'] == 'PASS', result['issues']
    assert result['counts'] == {'当期对象': 2, '按月的回款': 2, '大类实体': 1, '目标数': 2}
    assert ws.datasets['按月的回款'].window == 'period'
    assert ws.datasets['目标数'].unmatched == 'ignore'
    assert ws.mapping['目标数'].unmatched_basis['quote'] == absent
    assert ws._values('按月的回款', 'amount') == ['100', '-10']
    assert not ws.prepare_data()['scope_repairs']


@pytest.mark.parametrize('header,ready', [('求和项:回款金额(本币)', True), ('回款金额(本币)', False)])
def test_row_scope_identifies_raw_source_with_linked_pivot_reference(tmp_path, header, ready):
    ws, policy, zero, absent = collection_workspace(tmp_path, reference_amount=header)
    if not ready:
        ws.update_dataset('按月的回款', {'window': 'period'})
    ws.record_clauses([{'point': q, 'file': policy.name, 'quote': q} for q in (zero, absent)])
    result = ws.prepare_data()
    if ready:
        assert result['status'] == 'PASS' and result['counts']['当期对象'] == 2
        assert not ws.mapping['目标数'].filter and result['counts']['目标数'] == 2
    else:
        assert any('尚未唯一绑定原始数值列' in issue for issue in result['issues'])
        assert not ws.mapping['按月的回款'].filter


@pytest.mark.parametrize('problem', ['unrelated', 'include_history', 'ambiguous'])
def test_data_scope_cannot_discard_unverified_rows_to_get_a_pass(tmp_path, problem):
    ws, policy, zero, absent = collection_workspace(tmp_path, **{problem: True})
    ws.record_clauses([{'point': quote, 'file': policy.name, 'quote': quote} for quote in (zero, absent)])
    result = ws.prepare_data()
    assert result['status'] == 'FAIL'
    assert not ws._flow().current('data')
    if problem == 'unrelated':
        assert ws.datasets['目标数'].unmatched == 'block'
    elif problem == 'include_history':
        assert ws.datasets['按月的回款'].window == 'year_to_date'
    else:
        assert any('尚未唯一绑定原始数值列' in issue for issue in result['issues'])
        assert not ws.mapping['按月的回款'].filter


def test_data_receipt_rejects_declared_but_unmapped_source(tmp_path):
    ws, _ = workspace(tmp_path)
    failure = ws.prepare_data()
    assert failure['status'] == 'FAIL'
    assert not ws._flow().current('data')
    gap = next(item for item in failure['source_coverage'] if item['name'] == 'dates.xlsx')
    assert gap['status'] == 'UNMAPPED'
    assert gap['next_call']['args']['file'] == 'dates.xlsx'
    assert gap['scopes'][0]['keys'] == 2
    assert 'archive.xlsx' not in str(failure['source_coverage'])
    map_confirmation(ws)
    ready = ws.prepare_data()
    assert ready['status'] == 'PASS'
    coverage = next(item for item in ready['join_coverage'] if item['dataset'] == '核对')
    assert (coverage['matched_keys'], coverage['missing_keys']) == (1, 1)
    assert coverage['missing_examples'][0]['key'] == 'K02'


def test_resume_checks_input_contract_even_when_workbook_is_marked_reference(tmp_path):
    ws, files = workspace(tmp_path)
    ws.plan_calculation('只映射业务表', ['底稿'], {**files, 'dates.xlsx': 'reference'}, ['S'])
    ws.workflow['receipts']['data'] = {
        'version': ws._flow().version('data'), 'kind': 'data', 'payload': {}, 'at': 'old',
    }
    original = case.snapshot(ws)
    case.save_case(tmp_path / 'out', {'workspace': original, 'rounds': []})
    provider = FakeLLM([])
    provider.workflow_mode = 'staged'
    scenario = SimpleNamespace(files=[], outputs='1. 原始列：登记号、确认日。', brief='', regressions=[],
                               inputs=[InputTable(name='确认记录', columns=[Column(name='确认日')])])
    run = run_agent(provider, ws.inv.files, tmp_path / 'out', scenario=scenario, max_steps=0)
    report = run.ws.workflow['data_report']['report']
    assert report['status'] == 'FAIL' and not run.ws._flow().current('data')
    assert {(item['kind'], item['name'], item['status']) for item in report['source_coverage']} >= {
        ('column', '确认日', 'UNMAPPED'), ('table', '确认记录', 'UNMAPPED'),
    }
    assert not run.ws.llm.calls
    map_confirmation(run.ws)
    assert run.ws.prepare_data()['status'] == 'PASS'
    for document in (copy.deepcopy(case.load_case(tmp_path / 'out')['workspace']), case.snapshot(run.ws)):
        resumed = Workspace(FakeLLM([]), ws.inv)
        case.restore(resumed, document)
        assert resumed.workflow.get('source_contract') == run.ws.workflow['source_contract']


def test_unusable_population_returns_a_failed_data_report(tmp_path):
    ws, _ = workspace(tmp_path)
    map_confirmation(ws)
    ws.update_dataset('业务', {'population_from': '没有定义的表'})
    report = ws.prepare_data()
    assert report['status'] == 'FAIL'
    assert any('population_from' in issue for issue in report['issues'])
    assert not ws._flow().current('data')


def test_formula_cannot_commit_newly_required_empty_source_column(tmp_path):
    from tests.test_staged_workflow import make_workspace

    ws = make_workspace(tmp_path, lambda book: setattr(book['projects']['C1'], 'value', '预算'))
    ws.update_dataset('projects', {'fields': {'budget': {'column': '预算', 'type': 'money'}}})
    assert ws.prepare_data()['status'] == 'PASS'
    ws.plan_formula_tasks([{'id': 'budget', 'scheme': 'S', 'target': '/variables/budget_current'}])
    report = ws.submit_formula_task('budget', value={'scope': '项目', 'expr': 'budget'})
    assert report['status'] == 'FAIL' and not report['saved']
    assert 'value_empty' in ' '.join(report['issues'])
    assert 'budget_current' not in ws.drafts['S'].get('variables', {})


def test_monthly_formula_requires_cells_only_in_its_table_and_period(tmp_path):
    from bonus_core.data.period import Period
    from tests.test_staged_workflow import make_workspace

    months = [f'm{month:02}' for month in range(1, 13)]

    def monthly_workbooks(book):
        for sheet, current in [('区域数据', 1000), ('另一张表', None)]:
            table = book.create_sheet(sheet)
            table.append(['序号', *months])
            table.append(['X', *([None] * 8), current, None, None, None])

    ws = make_workspace(tmp_path, monthly_workbooks)
    ws.period = Period.parse('2026-09')
    for sheet in ('区域数据', '另一张表'):
        ws.define_dataset(sheet, 'data.xlsx', sheet, {
            **{month: {'column': month, 'type': 'money'} for month in months},
            'label': {'column': '序号', 'type': 'id'},
        })
    assert ws.prepare_data()['status'] == 'PASS'
    ws.plan_formula_tasks([{'id': 'monthly', 'scheme': 'S', 'target': '/variables/current_money'}])
    proposal = {'scope': 'global', 'expr': f'sum(period_value({", ".join(months)}), of: "区域数据")'}
    assert ws.submit_formula_task('monthly', value=proposal)['status'] == 'PASS'
    ws.period = Period.parse('2026-08')
    report = ws.prepare_data()
    assert report['status'] == 'FAIL'
    assert any('区域数据' in issue and 'm08' in issue for issue in report['issues'])
    assert all('另一张表' not in issue for issue in report['issues'])
    # Once explicitly owned by distribution, this input is evaluated at that phase.
    report_def = copy.deepcopy(ws.drafts['S']['report'])
    report_def['pending'] = [{'name': '待分配预算', 'level': 'global', 'expr': 'current_money',
                              'sources': ws.drafts['S']['pool']['sources']}]
    ws.set_rules(scheme='S', report=report_def)
    assert ws.prepare_data()['status'] == 'PASS'
