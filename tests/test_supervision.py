import json

import pytest

from bonus_agent import case
from bonus_agent.agent import ToolError, run_agent
from bonus_agent.construction import TaskBoard
from bonus_agent.llm import FakeLLM
from bonus_agent.progress import ConstructionReadBudget
from tests.test_staged_workflow import make_workspace, reviewed, save_staged_case


def planner_workspace(tmp_path):
    ws = make_workspace(tmp_path)
    ws.drafts.clear()
    ws.facts[0].targets = []
    for clause in ws.clauses:
        clause.where = ''
    assert ws.prepare_data()['status'] == 'PASS'
    assert ws._flow().stage()[0] == 3 and not TaskBoard(ws).state['tasks']
    return ws


def model(script, *, supervisor=None):
    primary = FakeLLM(script, name='deepseek-flash')
    primary.workflow_mode = 'staged'
    if supervisor:
        primary.recovery_mode = 'supervisor'
        primary.repair_llm = supervisor
    return primary


def supervisory_reply(tool, *, tasks=()):
    return json.dumps({
        'diagnosis': {'item': '当前算奖方案', 'identified': '工作区已有原文台账和当前数据检查状态。',
                      'missing': '当前阶段的必要操作尚未通过。', 'impact': '后续计算需要本阶段的通过回执。'},
        'next_tool': tool, 'guidance': '按当前材料完成缺失操作，并核对工具回执。', 'tasks': list(tasks),
    }, ensure_ascii=False)


def reads(count):
    sections = ['construction', 'variables', 'tables', 'mapping', 'facts', 'clauses', 'rules', 'checks']
    return [{'tool': 'read_workspace', 'args': {'section': section}} for section in sections[:count]]


@pytest.mark.parametrize('reject_supervisor', [False, True])
def test_saved_basis_gap_limits_next_action_and_preserves_existing_plan(tmp_path, reject_supervisor):
    ws = make_workspace(tmp_path, policy_tail='另附项目奖金汇总。')
    ws.plan_formula_tasks([{'id': 'fund', 'scheme': 'S', 'target': '/pool', 'clauses': ['C1']},
                           {'id': 'split', 'scheme': 'S', 'target': '/allocation'},
                           {'id': 'output', 'scheme': 'S', 'target': '/report', 'clauses': ['C1']}])
    ws.record_clauses([{'file': 'policy.md', 'quote': '另附项目奖金汇总', 'point': '列出资金汇总'}])
    assert ws.prepare_data()['status'] == 'PASS'
    for name in ('fund', 'output'):
        assert ws.check_formula_task(name)['status'] == 'PASS'
    premature = {'task': 'split', 'value': ws.drafts['S']['allocation']}
    early = ws.submit_formula_task(**premature)
    assert early['status'] == 'FAIL' and early['saved'] is False
    assert 'construction.phase' in early['issues'][0]
    assert TaskBoard(ws).statuses()['fund']['status'] == 'PASS'
    out = tmp_path / 'case'
    save_staged_case(ws, out)
    bad_plan = [{'id': 'output', 'scheme': 'S', 'target': '/report', 'clauses': ['C888']}]
    supervisor = FakeLLM([supervisory_reply('plan_formula_tasks', tasks=bad_plan)]) if reject_supervisor else None
    primary = model([{'tool': 'submit_formula_task', 'args': premature},
                     {'tool': 'plan_formula_tasks', 'args': {'tasks': [
                         {'id': 'output', 'scheme': 'S', 'target': '/report', 'clauses': ['C2']}]}}], supervisor=supervisor)
    run = run_agent(primary, ws.inv.files, out, max_steps=2)
    board = TaskBoard(run.ws)
    assert any(st.tool == 'submit_formula_task' and not st.ok for st in run.steps)
    assert board.task_spec('output')['clauses'] == ['C1', 'C2']
    assert run.ws.clauses[-1].where == 'report'
    assert run.ws._flow().current('formulas') and not run.ws._flow().current('approved:S')
    if reject_supervisor:
        step = next(st for st in run.steps if st.args.get('tool') == 'model_supervision')
        assert not step.ok and json.loads(step.result)['proposed_owners'] == bad_plan
        assert 'C888' in json.loads(step.result)['issues'][0]
        assert 'C888' in str(primary.calls[0])


def test_supervisor_gets_late_clause_and_its_owners_before_distribution(tmp_path):
    notes = [f'核算附表项目说明{i:02d}' for i in range(48)]
    ws = make_workspace(tmp_path, policy_tail='。'.join(notes))
    ws.record_clauses([{'file': 'policy.md', 'quote': quote, 'point': quote} for quote in notes])
    ws.cover_clauses({c.id: 'report' for c in ws.clauses[1:-1]})
    ws.plan_formula_tasks([{'id': 'funding', 'scheme': 'S', 'target': '/pool'},
                           {'id': 'output', 'scheme': 'S', 'target': '/report'}])
    assert ws.prepare_data()['status'] == 'PASS'
    for name in ('funding', 'output'):
        assert ws.check_formula_task(name)['status'] == 'PASS'
    last = ws.clauses[-1]
    out = tmp_path / 'case'
    save_staged_case(ws, out)
    kimi = FakeLLM([supervisory_reply('plan_formula_tasks', tasks=[
        {'id': 'output', 'scheme': 'S', 'target': '/report', 'clauses': [last.id]}])])
    primary = model([{'tool': 'calculate_basis', 'args': {'scheme': 'S'}}], supervisor=kimi)
    result = run_agent(primary, ws.inv.files, out, max_steps=1)
    packet = json.loads(kimi.calls[0][-1]['content'])
    assert packet['focus']['clauses'] == [{'id': last.id, 'file': last.file, 'quote': last.quote, 'point': last.point}]
    assert 'output' in {item['id'] for item in packet['focus']['tasks']}
    assert packet['allowed_tools'] == ['plan_formula_tasks']
    step = next(s for s in result.steps if s.args.get('tool') == 'model_supervision')
    assert step.ok and json.loads(step.result)['report']['assigned_clauses'] == [last.id]
    assert not TaskBoard(result.ws).unassigned_clauses()
    assert result.ws._flow().current('formulas') and not result.ws._flow().current('approved:S')


@pytest.mark.evidence
def test_future_parameter_owner_preserves_bindings_and_cannot_replace_existing_work(tmp_path):
    from bonus_agent.rule_package import plan_evidence_owners
    ws = make_workspace(tmp_path, policy_tail='人员分配保留候补份额5%。')
    ws.plan_formula_tasks([{'id': 'funding', 'scheme': 'S', 'target': '/pool', 'clauses': ['C1']},
                           {'id': 'split', 'scheme': 'S', 'target': '/allocation', 'depends_on': ['funding']},
                           {'id': 'output', 'scheme': 'S', 'target': '/report'}])
    ws.record_facts([{'name': '候补份额', 'file': 'policy.md', 'quote': '候补份额5%', 'value': '5%'}])
    assert ws.prepare_data()['status'] == 'PASS'
    for name in ('funding', 'output'):
        assert ws.check_formula_task(name)['status'] == 'PASS'
    assert ws.compile_formulas()['status'] == 'FAIL'
    gap = TaskBoard(ws).summary()['planning_gap']
    assert gap['kind'] == 'parameter_owners' and gap['facts'][0]['name'] == '候补份额'
    owner = {'id': 'split', 'scheme': 'S', 'target': '/allocation', 'facts': ['候补份额']}
    with pytest.raises(ToolError, match='保留原方案'):
        plan_evidence_owners(ws, gap, [{**owner, 'depends_on': []}])
    assert not TaskBoard(ws).task_spec('split')['facts']
    report = plan_evidence_owners(ws, gap, [owner])
    assert report['assigned_facts'] == ['候补份额']
    assert TaskBoard(ws).task_spec('split')['depends_on'] == ['funding']
    assert not next(f for f in ws.facts if f.name == '候补份额').targets
    assert ws.compile_formulas()['status'] == 'PASS'
    assert any('候补份额' in issue for issue in ws._binding_gaps())


def first_plan():
    return [
        {'id': 'basis_input', 'scheme': 'S', 'target': '/variables/project_income', 'clauses': ['C1']},
        {'id': 'funding', 'scheme': 'S', 'target': '/pool', 'depends_on': ['basis_input']},
        {'id': 'output', 'scheme': 'S', 'target': '/report', 'depends_on': ['funding']},
    ]


def test_kimi_repairs_empty_plan_at_warning_and_flash_executes_it(tmp_path):
    ws = planner_workspace(tmp_path)
    out = tmp_path / 'case'
    save_staged_case(ws, out)
    tasks = first_plan()
    tasks[0]['target'] = '/variables/amount'
    tasks[0]['expected_scope'] = ''
    tasks[1]['expected_scope'] = 'projects'
    kimi = FakeLLM([supervisory_reply('plan_formula_tasks', tasks=tasks)], name='kimi-k3.1-pro')
    resume = {'tool': 'submit_formula_task', 'args': {'task': 'basis_input', 'value': {'scope': 'projects', 'expr': 'amount'}}}
    primary = model([*reads(ConstructionReadBudget.WARN), resume], supervisor=kimi)

    run = run_agent(primary, ws.inv.files, out, max_steps=ConstructionReadBudget.WARN + 1)
    audit = [s for s in run.steps if s.args.get('tool') == 'model_supervision']
    assert len(kimi.calls) == len(audit) == 1
    assert audit[0].ok
    validated = json.loads(audit[0].result)['report']
    assert validated['omitted_scope_fields'] == ['basis_input', 'funding']
    assert validated['renamed_targets'] == [
        {'id': 'basis_input', 'from': '/variables/amount', 'to': '/variables/calculated_amount'},
    ]
    packet = json.loads(kimi.calls[0][-1]['content'])
    assert packet['stage']['name'] == '编译公式' and packet['focus']['target'] == '/pool'
    assert packet['receipts']['data'] is True
    assert packet['allowed_tools'] == ['plan_formula_tasks']
    assert any(s.tool == 'submit_formula_task' and s.ok for s in run.steps[run.steps.index(audit[0]) + 1:])
    assert TaskBoard(run.ws).statuses()['basis_input']['status'] == 'PASS'
    assert TaskBoard(run.ws).task_spec('funding')['depends_on'] == ['basis_input']
    assert run.ws._ruleset('S').variables['calculated_amount'].expr == 'amount'
    assert 'expected_scope' not in TaskBoard(run.ws).task_spec('funding')
    assert 'approved:S' not in run.ws.workflow['receipts'] and not run.ws.results


@pytest.mark.parametrize('valid', [True, False])
def test_supervision_resumes_a_persisted_planning_stop_once(tmp_path, valid):
    ws = planner_workspace(tmp_path)
    out = tmp_path / 'case'
    save_staged_case(ws, out)
    stopped = run_agent(model(reads(ConstructionReadBudget.LIMIT)), ws.inv.files, out)
    assert '8/8' in stopped.error and not TaskBoard(stopped.ws).state['tasks']
    old_checkpoint = case.load_case(out)

    tasks = first_plan() if valid else [{'id': 'unrelated', 'scheme': 'S', 'target': '/report'}]
    kimi = FakeLLM([supervisory_reply('plan_formula_tasks', tasks=tasks)], name='kimi-k3.1-pro')
    resume = {'tool': 'read_workspace', 'args': {'section': 'construction', 'name': 'funding'}}
    result = run_agent(model([resume], supervisor=kimi), ws.inv.files, out, max_steps=1)
    assert len(kimi.calls) == 1
    assert ('funding' in TaskBoard(result.ws).state['tasks']) is valid
    if valid:
        case.save_case(out, old_checkpoint)
        replay = run_agent(model([resume], supervisor=kimi), ws.inv.files, out, max_steps=1)
        assert any(s.args.get('replayed') and s.ok for s in replay.steps)
        assert 'funding' in TaskBoard(replay.ws).state['tasks'] and len(kimi.calls) == 1
    else:
        assert not TaskBoard(result.ws).state['tasks'] and '8/8' in result.error
        primary = model([], supervisor=kimi)
        assert '8/8' in run_agent(primary, ws.inv.files, out, max_steps=1).error
        assert len(kimi.calls) == 1 and not primary.calls


@pytest.mark.parametrize('empty_submission', [False, True])
def test_planning_read_timeout_returns_same_live_target_to_primary(tmp_path, monkeypatch, empty_submission):
    from httpx import ReadTimeout
    from bonus_agent.recovery import RunBudget
    from tests.test_request_budget import model as transport_model, transport

    ws = planner_workspace(tmp_path)
    out = tmp_path / 'case'
    save_staged_case(ws, out)
    supervisor = transport_model('supervisor')
    client = transport(monkeypatch, [ReadTimeout('no answer')])
    plan = {'tool': 'plan_formula_tasks', 'args': {'tasks': [] if empty_submission else first_plan()}}
    primary = model([*reads(ConstructionReadBudget.WARN), plan], supervisor=supervisor)
    budget = RunBudget(0, 300000)
    result = run_agent(primary, ws.inv.files, out, max_steps=ConstructionReadBudget.WARN + 1, run_budget=budget)
    event = next(s for s in result.steps if s.args.get('tool') == 'model_supervision')
    handoff = json.loads(event.result)
    assert handoff['status'] == 'DEFERRED' and not handoff['saved'] and not event.ok
    assert len(client.requests) == 1 and budget.snapshot()['unconfirmed_reserved_tokens'] > 0
    expected_id = 'auto:S:/pool' if empty_submission else 'funding'
    assert not budget.error and expected_id in TaskBoard(result.ws).state['tasks']
    assert any(s.tool == 'plan_formula_tasks' and s.ok for s in result.steps[result.steps.index(event)+1:])


def test_empty_plan_records_only_required_targets_and_cannot_erase_a_plan(tmp_path):
    ws = planner_workspace(tmp_path)
    expected = {(r['scheme'], r['task_target']) for r in TaskBoard(ws).output_obligations('basis')}
    report = ws.plan_formula_tasks([])
    assert report['saved'] and report['passed'] == 0 and ws._flow().current('data')
    assert {(r['scheme'], r['target']) for r in report['validated_targets']} == expected
    board = TaskBoard(ws)
    assert not board.state['receipts'] and not any(t['clauses'] or t['facts'] for t in board.state['tasks'].values())
    for replace in (False, True):
        with pytest.raises(ToolError, match='至少一项'):
            ws.plan_formula_tasks([], replace=replace)
        assert {(s['scheme'], s['target']) for s in board.state['tasks'].values()} == expected


def test_kimi_guides_material_planning_without_bypassing_tools(tmp_path):
    ws = make_workspace(tmp_path)
    template = ws.workflow['plan']
    plan = {k: template[k] for k in ('summary', 'outputs', 'files', 'schemes')}
    kimi = FakeLLM([supervisory_reply('plan_calculation')], name='kimi-k3.1-pro')
    primary = model([
        {'tool': 'plan_calculation', 'args': {**plan, 'files': {'不存在.md': 'rule'}}},
        [{'tool': 'set_rules', 'args': {'scheme': 'wrong', 'pool': {'expr': '0'}}},
         {'tool': 'plan_calculation', 'args': plan}],
        {'tool': 'plan_calculation', 'args': plan},
    ], supervisor=kimi)
    run = run_agent(primary, ws.inv.files, tmp_path / 'case', max_steps=3, deep=6)

    assert len(kimi.calls) == 1
    summary = json.loads(kimi.calls[0][-1]['content'])
    assert summary['stage']['name'] == '盘点材料'
    blocked = [s for s in run.steps if s.tool == 'set_rules']
    assert blocked and not blocked[0].ok and 'plan_calculation' in blocked[0].result
    assert 'wrong' not in run.ws.drafts
    assert run.ws.workflow['plan']['schemes'] == ['S']


def test_construction_guidance_allows_the_primary_to_plan_an_upstream_task(tmp_path, monkeypatch):
    import bonus_agent.agent as agent

    ws = planner_workspace(tmp_path)
    ws.plan_formula_tasks([
        {'id': 'funding', 'scheme': 'S', 'target': '/pool', 'clauses': ['C1']},
        {'id': 'output', 'scheme': 'S', 'target': '/report', 'depends_on': ['funding']},
    ])
    candidate = ws.submit_formula_task('funding', value={'expr': 'missing_total'})
    assert candidate['missing_components'] == [{'name': 'missing_total', 'task': None}]
    assert 'plan_formula_tasks' in candidate['next'] and not candidate['saved']
    out = tmp_path / 'case'
    save_staged_case(ws, out)
    monkeypatch.setattr(agent, 'QUIET_WARN', 1)
    kimi = FakeLLM([supervisory_reply('submit_formula_task')], name='kimi-k3.1-pro')
    upstream = {'tool': 'submit_formula_task', 'args': {'task': 'basis_input', 'value': {'scope': 'projects', 'expr': 'amount'}}}
    llm = model([*reads(1), [{'tool': 'plan_formula_tasks', 'args': {'tasks': first_plan()}}, upstream]], supervisor=kimi)

    result = run_agent(llm, ws.inv.files, out, max_steps=2)

    guidance = [s for s in result.steps if s.args.get('tool') == 'model_supervision']
    assert len(guidance) == 1 and json.loads(guidance[0].result)['status'] == 'GUIDED'
    assert any(s.tool == 'plan_formula_tasks' and s.ok for s in result.steps)
    board = TaskBoard(result.ws)
    assert board.task_spec('funding')['depends_on'] == ['basis_input']
    assert board.statuses()['basis_input']['status'] == 'PASS'


def test_stopped_pool_resumes_when_a_source_backed_amount_graph_is_saved(tmp_path, monkeypatch):
    from bonus_agent import funding_components
    from bonus_agent.recovery import _HALT_PREFIX
    from bonus_core.data.period import Period

    actual = '达成率按当月实际销售额合计除以当月预算。'
    product = '基础奖励×部门系数'
    withheld = '每笔交易先算「退货暂扣」=奖金总额×(1−退货系数)。'
    payable = '没有退货：实际可发 = 奖金总额 − 退货暂扣。'

    def postings(book):
        sheet = book['projects']
        sheet.cell(1, 3, '期间')
        for row in (2, 3):
            sheet.cell(row, 3, 202705)

    basis = '结算币属于USD，可奖励基数=销售金额/汇率*(汇率-0.05)，否则直接使用销售金额。'
    quotes = (actual, product, withheld, payable, basis, '基础奖励按项目的可奖励基数算。',
              '部门系数按当月实际销售额算，不用可奖励基数。')
    ws = make_workspace(tmp_path, mutate_workbook=postings, policy_tail='\n\n' + '\n\n'.join(quotes))
    ws.drafts.clear()
    ws.facts[0].targets = []
    ws.clauses[0].where = ''
    ws.period = Period.parse('2027-05')
    ws.record_clauses([{'file': 'policy.md', 'quote': quote, 'point': quote} for quote in quotes])
    ws.record_facts([{'name': '奖金总额公式', 'file': 'policy.md', 'quote': product, 'value': product}])
    ws.define_dataset('current_sales', 'data.xlsx', 'projects', {
        'pid': {'column': 'pid', 'type': 'id'}, 'amount': {'column': 'amount', 'type': 'money'},
        'posting': {'column': '期间', 'type': 'text'},
    }, link='pid', period='posting')
    ws.plan_formula_tasks([
        {'id': 'funding', 'scheme': 'S', 'target': '/pool', 'clauses': ['C2', 'C4', 'C5'], 'depends_on': ['monthly']},
        {'id': 'monthly', 'scheme': 'S', 'target': '/variables/monthly_total', 'clauses': ['C2'], 'expected_scope': 'projects'},
        {'id': 'output', 'scheme': 'S', 'target': '/report', 'depends_on': ['funding']},
    ])
    ws.set_rules(scheme='S', variables={'monthly_total': {'scope': 'projects', 'expr': 'sum(amount, of: "current_sales")',
                                                         'sources': [{'doc': 'policy.md', 'quote': actual}]}})
    assert ws.prepare_data()['status'] == 'PASS'
    out = tmp_path / 'case'
    save_staged_case(ws, out)
    with monkeypatch.context() as patch:
        patch.setattr(funding_components, 'prepare_funding_components', lambda ws: None)
        run_agent(model([]), ws.inv.files, out, period=str(ws.period), max_steps=0)
    journal = out / 'recovery.json'
    old = json.loads(journal.read_text(encoding='utf-8'))
    blocker = 'construction:funding:/pool'
    old['halted'] = _HALT_PREFIX + '连续 13 次检查没有消除既有问题；工具返回成功不代表有进展。' + blocker
    old['progress'] = {'previous': [blocker], 'stagnant': 12}
    journal.write_text(json.dumps(old, ensure_ascii=False), encoding='utf-8')

    llm = model(reads(1))
    result = run_agent(llm, ws.inv.files, out, period=str(ws.period), max_steps=1)

    step = next(s for s in result.steps if s.args.get('tool') == 'prepare_funding_components')
    assert step.ok and step.args['saved'] and len(llm.calls) == 1
    board = TaskBoard(result.ws)
    specs = board.state['tasks']
    owners = {s['target']: s['id'] for s in specs.values()}
    gross = owners['/variables/奖金总额']
    assert specs[gross]['depends_on'] == [owners['/variables/基础奖励'], owners['/variables/部门系数']]
    assert owners['/variables/可奖励基数'] in specs[owners['/variables/基础奖励']]['depends_on']
    assert owners['/variables/可奖励基数'] not in specs[owners['/variables/部门系数']]['depends_on']
    assert set(specs[owners['/variables/实际可发']]['depends_on']) == {gross, owners['/variables/退货暂扣']}
    assert board.statuses()['funding']['status'] == 'WAITING'
    assert not result.ws.results and not result.ws._flow().current('approved:S')
    archived = json.loads(journal.read_text(encoding='utf-8'))
    assert list(archived['resolved_construction_halts'].values())[0]['task'] == 'funding'


@pytest.mark.parametrize('separate_windows', [True, False])
def test_kimi_can_replace_only_incomplete_pool_dependencies(tmp_path, separate_windows):
    from bonus_core.data.period import Period

    period_rule = '达成率按当月实际销售额合计除以当月预算。'
    history_rule = '月平均出货按本年 1 月到本月的出货金额合计 ÷ 该客户实际有出货的月份数。'

    def postings(book):
        sheet = book['projects']
        sheet.cell(1, 3, '期间')
        for row in (2, 3):
            sheet.cell(row, 3, 202704)

    ws = make_workspace(tmp_path, mutate_workbook=postings, policy_tail=f'\n{period_rule}\n{history_rule}')
    ws.drafts.clear()
    ws.facts[0].targets = []
    ws.clauses[0].where = ''
    ws.period = Period.parse('2027-04')
    ws.record_clauses([{'file': 'policy.md', 'point': quote, 'quote': quote} for quote in (period_rule, history_rule)])
    ws.define_dataset('current_sales', 'data.xlsx', 'projects', {
        'pid': {'column': 'pid', 'type': 'id'}, 'amount': {'column': 'amount', 'type': 'money'},
        'posting': {'column': '期间', 'type': 'text'},
    }, link='pid', period='posting')
    monthly = {'dataset': 'current_sales', 'fields': ['posting', 'amount'], 'period_field': 'posting', 'window': 'period'}
    original = {'id': 'funding', 'scheme': 'S', 'target': '/pool', 'clauses': ['C2', 'C3'],
                'data_requirements': [{**monthly, 'window': 'year_to_date'}]}
    output = {'id': 'output', 'scheme': 'S', 'target': '/report', 'depends_on': ['funding']}
    ws.plan_formula_tasks([original, output])
    assert ws.prepare_data()['status'] == 'PASS'
    assert TaskBoard(ws).summary()['planning_gap']['kind'] == 'pool_inputs'
    rejected = ws.submit_formula_task('funding', value={'scope': 'global', 'expr': '0'})
    assert not rejected['saved'] and '拆入各自的前置变量任务' in rejected['issues'][0]
    with pytest.raises(ToolError, match='拆入各自的前置变量任务'):
        ws.set_rules(scheme='S', pool={'scope': 'global', 'expr': '0'})
    tasks = [
        {'id': 'funding', 'scheme': 'S', 'target': '/pool', 'depends_on': ['now', 'historic'],
         'data_requirements': [], 'clauses': []},
        {'id': 'now', 'scheme': 'S', 'target': '/variables/current_income', 'clauses': ['C2'], 'expected_scope': 'projects',
         'data_requirements': [monthly]},
        {'id': 'historic', 'scheme': 'S', 'target': '/variables/history_avg', 'clauses': ['C3'], 'expected_scope': 'projects',
         'data_requirements': [{**monthly, 'dataset': 'annual_sales' if separate_windows else 'current_sales', 'window': 'year_to_date'}]},
    ]
    kimi = FakeLLM([supervisory_reply('plan_formula_tasks', tasks=tasks)], name='kimi-k3.1-pro')
    out = tmp_path / 'case'
    save_staged_case(ws, out)
    ar = run_agent(model(reads(1), supervisor=kimi), ws.inv.files, out, max_steps=1, period='2027-04')
    step = next(st for st in ar.steps if st.args.get('tool') == 'model_supervision')
    assert step.ok == separate_windows and len(kimi.calls) == 1
    packet = json.loads(kimi.calls[0][-1]['content'])
    assert packet['focus']['task'] == 'funding' and packet['allowed_tools'] == ['plan_formula_tasks']
    board = TaskBoard(ar.ws)
    assert board.task_spec('funding')['clauses'] == ['C2', 'C3']
    assert board.task_spec('output')['depends_on'] == ['funding']
    if separate_windows:
        assert board.task_spec('funding')['depends_on'] == ['now', 'historic']
        assert not board.summary().get('planning_gap') and not board.state['receipts']
        assert '本轮没有进展' not in ar.error
    else:
        assert not board.task_spec('funding')['depends_on'] and 'historic' not in board.state['tasks']
        assert '要求期间范围 year_to_date，实际为 period' in step.result
        assert any('程序拒绝的依赖候选' in message.get('content', '') for message in ar.ws.llm.calls[0])
    assert not ar.ws.results and not ar.ws._flow().current('approved:S')


def test_supervision_does_not_act_during_financial_confirmation(tmp_path):
    ws = reviewed(make_workspace(tmp_path))
    out = tmp_path / 'case'
    save_staged_case(ws, out)
    kimi = FakeLLM([supervisory_reply('trial_run')], name='kimi-k3.1-pro')
    primary = model([], supervisor=kimi)
    primary.checker_llm = ws.llm.checker_llm
    primary.checker_required = True

    run = run_agent(primary, ws.inv.files, out)
    assert run.status == 'AWAITING_TOTAL_CONFIRMATION'
    assert not kimi.calls and not primary.calls
    assert not run.ws.results and 'approved:S' not in run.ws.workflow['receipts']
