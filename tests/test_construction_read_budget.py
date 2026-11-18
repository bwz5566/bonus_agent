import json

from bonus_agent.agent import run_agent
from bonus_agent.construction import TaskBoard
from bonus_agent.llm import FakeLLM
from bonus_agent.progress import ConstructionReadBudget
from tests.test_staged_workflow import make_workspace, save_staged_case


def test_missing_scheme_bounds_distinct_reads_and_resume(tmp_path):
    ws = make_workspace(tmp_path)
    TaskBoard(ws).adopt()
    ws.workflow['plan']['schemes'].append('第二方案')
    assert ws.prepare_data()['status'] == 'PASS'
    assert ws._flow().stage()[0] == 3
    out = tmp_path / 'case'
    save_staged_case(ws, out)
    reads = [{'tool': 'read_workspace', 'args': {'section': 'facts', 'name': f'查阅{i}'}}
             for i in range(ConstructionReadBudget.LIMIT)]
    script = reads[:3] + [{'tool': 'note', 'args': {'topic': '查询笔记', 'text': '待查'}}] + reads[3:]
    primary = FakeLLM(script)
    primary.workflow_mode = 'staged'
    run = run_agent(primary, ws.inv.files, out, max_steps=len(script) + 2)
    assert run.error.startswith('公式构建停滞'), run.error
    assert '第二方案::/pool' in run.error
    assert any('【当前缺失的构建任务】' in str(call) for call in primary.calls)
    assert json.loads((out / 'recovery.json').read_text(encoding='utf8'))['construction_reads']['reads'] == len(reads)
    resumed = FakeLLM([])
    resumed.workflow_mode = 'staged'
    assert run_agent(resumed, ws.inv.files, out).error == run.error
    assert not resumed.calls


def test_obligation_changes_only_for_a_new_executable_target():
    budget = ConstructionReadBudget({})
    row = {'output': '明细', 'scheme': '一', 'target': '/report/details',
           'task_target': '/report', 'kind': 'missing_task'}
    focus = budget.observe([row])
    renamed = dict(row, output='新名称', target='/report/totals')
    assert budget.observe([renamed], previous=focus['id'], reads=4)['warn']
    assert budget.observe(None) is None  # rules/data take priority temporarily
    assert budget.observe([renamed], previous=focus['id'], reads=4)['halt']
    component = dict(row, kind='missing_component')  # owner task has actually been created
    next_focus = budget.observe([component], previous=focus['id'], reads=1)
    assert next_focus['id'] != focus['id'] and not next_focus['warn']
    assert budget.state['reads'] == 0
    assert budget.observe([]) is None and not budget.state
