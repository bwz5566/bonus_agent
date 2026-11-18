"""A basis projection must not turn an omitted scope into an explicit decision."""
import openpyxl
import pytest

from bonus_agent.agent import Workspace
from bonus_agent.auto import build_inventory
from bonus_agent.llm import FakeLLM


pytestmark = pytest.mark.evidence


def workspace(tmp_path, *, explicit_global):
    policy = tmp_path / 'policy.md'
    quote = '每个项目使用全公司销售合计金额的10%作为奖金，按名单比例分配。'
    policy.write_text(quote, encoding='utf-8')
    data = tmp_path / 'data.xlsx'
    wb = openpyxl.Workbook()
    projects = wb.active
    projects.title = 'projects'
    projects.append(['pid', 'amount'])
    projects.append(['A', 100])
    projects.append(['B', 200])
    people = wb.create_sheet('people')
    people.append(['rid', 'pid', 'person', 'share'])
    people.append(['1', 'A', '甲', 1])
    people.append(['2', 'B', '乙', 1])
    wb.save(data)
    wb.close()
    ws = Workspace(FakeLLM([]), build_inventory([policy, data]))
    ws.workflow['enabled'] = True
    ws.plan_calculation('按明确的全公司基数逐项目计奖', ['项目底稿', '人员金额'],
                        {policy.name: 'rule', data.name: 'data'}, ['S'])
    ws.record_facts([{'name': '提成率', 'file': policy.name, 'quote': '销售合计金额的10%', 'value': '10%'}])
    ws.record_clauses([{'file': policy.name, 'quote': quote, 'point': '每个项目使用全公司销售合计金额乘提成率'}])
    field = lambda column, kind: {'column': column, 'type': kind}
    ws.define_dataset('projects', data.name, 'projects',
                      {'pid': field('pid', 'id'), 'amount': field('amount', 'money')}, key='pid')
    ws.define_dataset('people', data.name, 'people',
                      {'rid': field('rid', 'id'), 'pid': field('pid', 'id'),
                       'person': field('person', 'text'), 'share': field('share', 'ratio')}, key='rid')
    sources = [{'doc': policy.name, 'quote': quote}]
    company_sales = {'expr': 'sum(amount, of: "projects")', 'sources': sources}
    if explicit_global:
        company_sales['scope'] = 'global'
    ws.set_rules(scheme='S', variables={'company_sales': company_sales},
                 pool={'scope': '项目', 'expr': 'company_sales * 0.1', 'sources': sources},
                 allocation=[{'level': '项目', 'key': 'pid'},
                             {'level': '参与', 'key': 'rid', 'parent_key': 'pid', 'weight': 'share'}],
                 report={'people': 'person',
                         'details': [{'name': '项目奖金', 'level': '项目',
                                      'expr': 'company_sales * 0.1', 'sources': sources}],
                         'totals': [{'name': '奖金合计', 'level': 'global',
                                     'expr': 'sum(amount, of: "projects") * 0.1 * count(of: "projects")',
                                     'sources': sources}]})
    ws.bind_facts([{'name': '提成率', 'targets': [
        'S::/pool/expr', 'S::/report/details/0/expr', 'S::/report/totals/0/expr']}])
    ws.plan_formula_tasks([
        {'id': 'sales', 'scheme': 'S', 'target': '/variables/company_sales'},
        {'id': 'pool', 'scheme': 'S', 'target': '/pool', 'depends_on': ['sales'],
         'facts': ['提成率'], 'clauses': ['C1']},
        {'id': 'allocation', 'scheme': 'S', 'target': '/allocation'},
        {'id': 'report', 'scheme': 'S', 'target': '/report', 'depends_on': ['sales'],
         'facts': ['提成率'], 'clauses': ['C1']},
    ])
    assert ws.prepare_data()['status'] == 'PASS'
    assert ws.check_formula_task('sales')['status'] == 'PASS'
    return ws


def test_omitted_global_scope_blocks_local_basis_task_and_basis_compile(tmp_path):
    ws = workspace(tmp_path, explicit_global=False)
    assert 'scope' not in ws.drafts['S']['variables']['company_sales']
    local = ws.check_formula_task('pool')
    assert local['status'] == 'FAIL', local
    assert any('global_in_level' in issue for issue in local['issues']), local
    ws.check_formula_task('report')
    result = ws.compile_formulas()
    full = ws.workflow['formula_report']['report']
    assert result['status'] == 'FAIL', result
    assert any('global_in_level' in issue for issue in full['issues']), full
    assert not ws._flow().current('formulas')
    assert not ws.workflow['basis'] and not ws.results and not ws.llm.calls


def test_material_backed_explicit_global_scope_remains_allowed(tmp_path):
    ws = workspace(tmp_path, explicit_global=True)
    for task in ('pool', 'report'):
        result = ws.check_formula_task(task)
        assert result['status'] == 'PASS', result
    compiled = ws.compile_formulas()
    assert compiled['status'] == 'PASS', compiled
    ws.calculate_basis('S')
    basis = ws.workflow['basis']['S']
    assert basis['pool'] == '60.00'
    assert {key: value['amount'] for key, value in basis['objects'].items()} == {
        '项目:A': '30.00', '项目:B': '30.00'}
    assert not ws.llm.calls
