"""Joint data and formula repairs through the formal tool entrypoint."""
import copy
import json

import pytest

from bonus_agent import case
from bonus_agent.construction import TaskBoard
from bonus_agent.contracts import ratio_default_gaps
from bonus_agent.rule_package import archived_tier_candidate
from bonus_core.dsl.models import Variable
from tests.test_staged_workflow import make_workspace


@pytest.mark.parametrize(('lookup_scope', 'lookup_source', 'blocked'), [
    ('department', 'returns', False),
    ('department', 'adjustments', True),
    ('company', 'returns', True),
])
def test_ratio_default_must_be_guarded_at_the_same_scope(tmp_path, lookup_scope, lookup_source, blocked):
    rs = make_workspace(tmp_path)._ruleset('S').model_copy(update={'variables': {
        'raw_ratio': Variable(scope='department', expr='if(count(of: "returns") == 0, 0, ratio(amount, 100))'),
        'factor': Variable(scope=lookup_scope, expr=f'if(count(of: "{lookup_source}") == 0, 1, tier_rate(rates, raw_ratio))'),
    }})

    assert [path for path, _ in ratio_default_gaps(rs)] == (['variables.factor'] if blocked else [])


def test_joint_mapping_and_dependent_formula_pass_or_rollback(tmp_path):
    ws = make_workspace(tmp_path)
    ws.prepare_data()
    ws.plan_formula_tasks([
        {'id': 'amount', 'scheme': 'S', 'target': '/variables/selected_amount', 'clauses': ['C1']},
        {'id': 'multiple', 'scheme': 'S', 'target': '/variables/doubled_amount', 'depends_on': ['amount'],
         'clauses': ['C1']},
        {'id': 'pool', 'scheme': 'S', 'target': '/pool', 'depends_on': ['multiple']},
    ])
    ws.set_rules(scheme='S', variables={
        'selected_amount': {'scope': 'global', 'expr': 'single_value(amount, of: "projects")'},
        'doubled_amount': {'scope': 'global', 'expr': 'selected_amount + selected_amount'},
    })
    board = TaskBoard(ws)
    receipts = ws.workflow['receipts']
    assert board.check('amount')['status'] == 'FAIL'
    before = copy.deepcopy(case.snapshot(ws))
    datasets = [{'operation': 'update', 'args': {
        'name': 'projects', 'changes': {'fields': {'amount_copy': {'column': ws.mapping['projects'].columns['amount'],
                                                               'type': 'money'}}}}}]
    formulas = [
        {'task': 'multiple', 'expected_scope': '项目',
         'changes': {'scope': '项目', 'expr': 'selected_amount + unknown_name'}},
        {'task': 'amount', 'expected_scope': '项目',
         'changes': {'scope': '项目', 'expr': 'amount_copy'}},
    ]
    failure = ws.submit_construction_repair('S', 'amount', formulas, datasets)
    assert failure['status'] == 'FAIL'
    assert failure['failed_task'] == 'multiple'
    assert 'unknown_name' in ' '.join(failure['issues'])
    assert case.snapshot(ws) == before
    assert 'amount_copy' not in ws.datasets['projects'].fields

    formulas[0]['changes']['expr'] = 'selected_amount + selected_amount'
    saved = ws.submit_construction_repair('S', 'amount', formulas, datasets)
    assert saved['status'] == 'PASS' and saved['saved']
    assert [check['task'] for check in saved['checked']] == ['amount', 'multiple']
    assert 'amount_copy' in ws.datasets['projects'].fields
    final = TaskBoard(ws).statuses()
    assert final['amount']['confidence']['dimensions']['execution']['state'] == 'VERIFIED'
    assert final['multiple']['confidence']['dimensions']['execution']['state'] == 'VERIFIED'
    assert 'approved:S' not in receipts and 'approved:S' not in ws.workflow['receipts']
    assert not ws.results


def test_table_guard_rejects_a_reached_wrong_table_before_saving_formula(tmp_path):
    def add_market(wb):
        sheet = wb['projects']
        sheet.cell(1, 3, '渠道')
        sheet.cell(2, 3, '直营')
        sheet.cell(3, 3, '分销')

    ws = make_workspace(tmp_path, mutate_workbook=add_market)
    ws.update_dataset('projects', {'fields': {'market': {'column': '渠道', 'type': 'text'}}})
    ws.plan_formula_tasks([
        {'id': 'direct', 'scheme': 'S', 'target': '/tables/direct_rate'},
        {'id': 'distribution', 'scheme': 'S', 'target': '/tables/distribution_rate'},
        {'id': 'routed', 'scheme': 'S', 'target': '/variables/routed_amount', 'clauses': ['C1']},
        {'id': 'pool', 'scheme': 'S', 'target': '/pool', 'depends_on': ['routed']},
    ])
    for table, market in [('direct_rate', '直营'), ('distribution_rate', '分销')]:
        ws.compile_table(table, [{'value': '提成率'}], scheme='S', mode='whole',
                         applies_when=f'market == "{market}"')
    assert ws.prepare_data()['status'] == 'PASS'
    board = TaskBoard(ws)
    assert board.check('direct')['status'] == 'PASS'
    assert board.check('distribution')['status'] == 'PASS'
    source = [{'doc': 'policy.md', 'quote': ws.clauses[0].quote}]
    wrong = {'scope': '项目', 'expr': 'tier(direct_rate, amount)', 'sources': source}
    before = case.snapshot(ws)
    report = ws.submit_formula_task('routed', value=wrong)
    assert report['status'] == 'FAIL' and not report['saved'], report
    repair = report['execution']['repair']
    assert repair['task'] == 'routed' and repair['node'] == '项目:B'
    guard = repair['table_applicability']
    assert guard['table'] == 'direct_rate'
    assert guard['evaluated'] == '"分销" == "直营"'
    assert guard['source'] == 'data.xlsx/projects!3'
    assert guard['field_evidence'][0]['source']['column'] == '渠道'
    assert case.snapshot(ws) == before

    correct = {**wrong, 'expr': 'if(market == "直营", tier(direct_rate, amount), tier(distribution_rate, amount))'}
    report = ws.submit_construction_repair('S', 'routed', [{'task': 'routed', 'value': correct}])
    assert report['status'] == 'PASS' and report['saved'], report
    status = TaskBoard(ws).statuses()['routed']
    assert status['confidence']['dimensions']['execution']['state'] == 'VERIFIED'


def history_workspace(tmp_path, conflicting=False, linked_policy=False, january=False, distinct_codes=False,
                      mixed_funding=False, status_filter=None):
    import openpyxl

    from bonus_agent.agent import Workspace
    from bonus_agent.auto import build_inventory
    from bonus_agent.llm import FakeLLM
    from bonus_core.data.period import Period

    quotes = [
        '月平均销售：该团队本年一月至核算月的销售金额合计 ÷ 该团队实际有销售的月份数。销售金额使用实际销售额（本位币）。',
        '基准口径 | 对应团队简称2，按团队月平均销售计算资金。',
        '销售金额为 0 的行不参与核算，也不算有销售的月份。',
        '参考数据里有、但销售单里没有的团队或金额，不参与核算，不影响结果。',
    ]
    if linked_policy:
        quotes[1] = '待发回款金额 VS 该团队月平均销售金额'
        quotes.append('对应团队简称2，待发回款金额/团队简称2的月平均金额，查不到待发回款金额时，匹配系数指定1')
    if mixed_funding:
        quotes.append('达成率按当月实际销售额（本位币）合计除以当月预算。')
    if status_filter and status_filter != 'unproven':
        quotes[2] = '销售金额为 0 的行（包括记账状态为“取消”的行）不参与核算，也不算有销售的月份。'
    policy = tmp_path / '规则.md'
    policy.write_text('\n\n'.join(quotes), encoding='utf-8')
    book = openpyxl.Workbook()
    current = book.active
    current.title = '当期明细'
    current.append(['发生月', '团队简称(B列)', '销售金额(本位币)', *(['编码'] if distinct_codes else [])])
    current_month = 202701 if january else 202706 if linked_policy else 202704
    first_amount, second_amount = (240, 75) if linked_policy else (200, 90)
    if distinct_codes:
        current.append([current_month, 'A', first_amount - 25, 'A1'])
        current.append([current_month, 'A', 25, 'A2'])
        current.append([current_month, 'B', second_amount, 'B1'])
    else:
        current.append([current_month, 'A', first_amount])
        current.append([current_month, 'B', second_amount])
    if linked_policy:
        current.append([202703, '仅历史', 150])
    if status_filter:
        column = current.max_column + 1
        current.cell(1, column, '记账状态')
        for row in range(2, current.max_row + 1):
            current.cell(row, column, '有效')
        current.append([current_month, 'A', 15 if status_filter == 'nonzero' else 0, '取消'])
    for name in ([] if january else ['过往流水', *(['另一历史汇总'] if conflicting else [])]):
        sheet = book.create_sheet(name)
        sheet.append(['发生月', '团队简称2', '求和项:销售金额(本位币)'])
        for row in [(202701, 'A', 100 if name == '过往流水' else 400), (202702, 'A', 0),
                    (current_month, 'A', first_amount), (202707, 'A', 999),
                    (202701, 'B', 30), (202702, 'B', 60), (current_month, 'B', second_amount)]:
            sheet.append(row)
    workbook = tmp_path / '记账汇总.xlsx'
    book.save(workbook)
    book.close()
    ws = Workspace(FakeLLM([]), build_inventory([policy, workbook]))
    ws.workflow['enabled'] = True
    ws.period = Period.parse(str(current_month))
    ws.plan_calculation('按团队核算', ['资金'], {policy.name: 'rule', workbook.name: 'data'}, ['S'])
    ws.record_clauses([{'file': policy.name, 'quote': quote, 'point': quote} for quote in quotes])
    if mixed_funding:
        ws.record_facts([{'name': '零金额判断', 'file': policy.name, 'quote': quotes[2], 'value': '0'}])
    keys = {'team_join': {'column': '团队简称(B列)', 'type': 'text'},
            'posting': {'column': '发生月', 'type': 'text'}}
    group_key = 'team_join'
    if distinct_codes:
        keys['code'] = {'column': '编码', 'type': 'id'}
        group_key = 'code'
    filters = ({'销售金额(本位币)': {'not': ['0']}, '记账状态': {'not': ['取消']}}
               if status_filter else None)
    ws.define_dataset('teams', workbook.name, current.title,
                      {'team_join': keys['team_join']} if linked_policy else keys, key=group_key, distinct=True,
                      period=None if linked_policy else 'posting', filter=filters)
    detail = {**keys, 'amount': {'column': '销售金额(本位币)', 'type': 'money', 'unit': '元'}}
    ws.define_dataset('posted', workbook.name, current.title, detail, link=group_key, period='posting', filter=filters)
    history_requirement = {}
    if distinct_codes:
        history_requirement = {'expected_scope': 'teams', 'data_requirements': [{
            'dataset': 'posted', 'fields': ['code', 'amount', 'posting'], 'window': 'year_to_date',
            'period_field': 'posting',
        }]}
    if linked_policy:
        ws.define_dataset('draft_history', workbook.name, current.title, detail, link='team_join',
                          period='posting', window='year_to_date', unmatched='ignore',
                          unmatched_basis={'file': policy.name, 'quote': quotes[3]})
        history_requirement = {'data_requirements': [{'dataset': 'draft_history', 'fields': list(detail),
                                                       'window': 'year_to_date', 'period_field': 'posting'}]}
    if mixed_funding:
        ws.plan_formula_tasks([
            {'id': 'fund', 'scheme': 'S', 'target': '/pool', 'clauses': ['C1', 'C2', f'C{len(quotes)}'],
             'data_requirements': [{'dataset': 'posted', 'fields': list(detail), 'window': 'year_to_date',
                                    'period_field': 'posting', 'link': group_key}]},
            {'id': 'delivery', 'scheme': 'S', 'target': '/report', 'depends_on': ['fund']},
        ])
    else:
        ws.plan_formula_tasks([
            {'id': 'average', 'scheme': 'S', 'target': '/variables/average_sales', 'clauses': ['C1'], **history_requirement},
            {'id': 'fund', 'scheme': 'S', 'target': '/pool', 'depends_on': ['average'], 'clauses': ['C2']},
        ])
    assert ws.prepare_data()['status'] == 'PASS'
    return ws, quotes[0]


@pytest.mark.parametrize('conflicting_history', [False, True])
def test_resume_mixed_funding_uses_verified_inputs_before_model(tmp_path, monkeypatch, conflicting_history):
    from bonus_agent import funding_plan
    from bonus_agent.agent import run_agent
    from bonus_agent.progress import ConstructionReadBudget
    from tests.test_staged_workflow import save_staged_case
    from tests.test_supervision import model, reads

    ws, _ = history_workspace(tmp_path, mixed_funding=True, conflicting=conflicting_history)
    assert ws._flow().stage()[0] == 3
    out = tmp_path / 'case'
    save_staged_case(ws, out)
    with monkeypatch.context() as patch:
        patch.setattr(funding_plan, 'prepare_pool_inputs', lambda ws: None)
        old = run_agent(model(reads(ConstructionReadBudget.LIMIT)), ws.inv.files, out, period=str(ws.period))
    assert '公式构建停滞' in old.error and not TaskBoard(old.ws).task_spec('fund')['depends_on']

    primary = model(reads(1))
    ar = run_agent(primary, ws.inv.files, out, max_steps=1, period=str(ws.period))
    board = TaskBoard(ar.ws)
    assert board.task_spec('delivery')['depends_on'] == ['fund']
    assert not ar.ws.results and not ar.ws._flow().current('approved:S')
    report = next(s for s in ar.steps if s.args.get('tool') == 'prepare_pool_inputs')
    if conflicting_history:
        assert not report.ok and '历史金额来源' in report.result
        assert funding_plan.pool_input_gap(board) and not board.task_spec('fund')['depends_on'] and not primary.calls
        assert not ar.ws._ruleset('S').variables
    else:
        assert report.ok and report.args.get('saved') and len(primary.calls) == 1
        assert not funding_plan.pool_input_gap(board) and '公式构建停滞' not in ar.error
        deps = board.task_spec('fund')['depends_on']
        assert len(deps) == 2 and all(board.statuses()[n]['status'] == 'PASS' for n in deps)
        expected = {'period': '当期明细', 'year_to_date': '过往流水'}
        assert {r['window']: ar.ws.mapping[r['dataset']].sheet for n in deps
                for r in board.task_spec(n)['data_requirements']} == expected


def test_first_month_history_needs_no_earlier_months(tmp_path):
    from bonus_agent.rule_package import prepare_formula_inputs

    ws, _ = history_workspace(tmp_path, january=True)
    report = prepare_formula_inputs(ws)
    assert report['status'] == 'PASS' and report['saved'], report
    assert report['data_input']['compared_group_months'] == 2
    assert report['data_input']['historical_group_months'] == 2


@pytest.mark.parametrize('status_filter', ['zero', 'nonzero', 'unproven'])
def test_history_accepts_only_proven_redundant_row_exclusions(tmp_path, status_filter):
    from decimal import Decimal
    from bonus_agent.rule_package import prepare_formula_inputs
    from bonus_core.engine.run import Engine

    ws, _ = history_workspace(tmp_path, status_filter=status_filter)
    report = prepare_formula_inputs(ws)
    if status_filter != 'zero':
        assert report['status'] == 'FAIL' and report['input_failures'][0]['code'] == 'history_filter_unresolved'
        assert 'average_sales_history' not in ws.datasets
        return
    assert report['status'] == 'PASS' and report['saved'], report
    rules = ws._ruleset('S')
    rule = rules.model_copy(update={'pool': rules.pool.model_copy(update={'scope': 'teams', 'expr': 'average_sales'})})
    data, _ = ws._data(rule)
    engine = Engine.prepare_basis(rule, data, period=ws.period)
    amounts = {key: engine.ev.variable('average_sales', node).v for key, node in engine.ev.index['teams'].items()}
    assert amounts == {'A': Decimal(150), 'B': Decimal(60)}
    assert ws.mapping['posted'].filter['记账状态'] == {'not': ['取消']}


@pytest.mark.parametrize('stale_failure', [False, True])
def test_ready_formula_input_is_prepared_with_an_independent_pending_task(tmp_path, stale_failure):
    from bonus_agent.rule_package import prepare_formula_inputs

    ws, _ = history_workspace(tmp_path)
    board = TaskBoard(ws)
    average, fund = (board.task_spec(name) for name in ('average', 'fund'))
    ws.plan_formula_tasks([
        {'id': 'other', 'scheme': 'S', 'target': '/variables/unrelated_adjustment', 'clauses': ['C3']},
        average,
        {**fund, 'depends_on': ['other', 'average']},
    ], replace=True)
    assert TaskBoard(ws).summary()['active']['id'] == 'other'
    if stale_failure:
        # Previous builds saved a failed speculative attempt. A new executable
        # or corrected input may now produce a verified result.
        TaskBoard(ws).state.setdefault('input_preflight', {})['average'] = {
            'version': 'previous-build', 'report': {'status': 'FAIL', 'saved': False},
        }

    report = prepare_formula_inputs(ws)

    board = TaskBoard(ws)
    assert report['task'] == 'average' and report['saved']
    assert board.statuses()['average']['status'] == 'PASS'
    assert board.statuses()['fund']['status'] == 'WAITING'


def test_background_input_failure_waits_until_its_task_is_scheduled(tmp_path):
    from bonus_agent.agent import Step
    from bonus_agent.progress import RepairReadBudget, repair_scope
    from bonus_agent.recovery import RecoveryJournal
    from bonus_agent.rule_package import prepare_formula_inputs

    ws, _ = history_workspace(tmp_path, conflicting=True)
    zero = ws.clauses[2]
    ws.record_facts([{'name': '零金额判断', 'file': zero.file, 'quote': zero.quote, 'value': '0'}])
    board = TaskBoard(ws)
    average, fund = (board.task_spec(name) for name in ('average', 'fund'))
    other = {'id': 'other', 'scheme': 'S', 'target': '/variables/other', 'clauses': ['C3']}
    fund = {**fund, 'depends_on': ['other', 'average']}
    ws.plan_formula_tasks([other, average, fund], replace=True)
    assert TaskBoard(ws).summary()['active']['id'] == 'other'
    assert prepare_formula_inputs(ws) is None
    pending = TaskBoard(ws).state['input_preflight']['average']['report']
    assert pending['status'] == 'FAIL'
    assert prepare_formula_inputs(ws) is None

    # Even a diagnostic retained from an earlier scheduling turn must not
    # spend the history task's allowance on the current independent task.
    journal = RecoveryJournal(None, {'test': 'scheduled input'})
    journal.observe_native_feedback('prepare_formula_inputs', {}, json.dumps(pending, ensure_ascii=False), False)
    budget = RepairReadBudget(journal.state.setdefault('repair_reads', {}))
    scope = repair_scope(ws)
    journal.set_repair_scope(scope)
    assert not journal.native_guidance
    reads = [Step('read_document', {'file': '规则.md', 'offset': i}, 'evidence', True)
             for i in range(RepairReadBudget.LIMIT)]
    assert not budget.observe(reads, journal.state['native_repairs'], None,
                              {'read_document'}, scope=scope)['active']

    ws.plan_formula_tasks([average, other, fund], replace=True)
    assert TaskBoard(ws).summary()['active']['id'] == 'average'
    report = prepare_formula_inputs(ws)
    assert report['status'] == 'FAIL' and report['task'] == 'average'
    assert prepare_formula_inputs(ws) is None
    scope = repair_scope(ws)
    journal.set_repair_scope(scope)
    assert journal.native_guidance
    exhausted = budget.observe(reads, journal.state['native_repairs'], None, {'read_document'}, scope=scope)
    assert exhausted['halt']
    journal.halt(exhausted['message'], repair_read=exhausted)
    ws.plan_formula_tasks([other, average, fund], replace=True)
    journal.set_repair_scope(repair_scope(ws))
    assert not journal.error
    ws.plan_formula_tasks([average, other, fund], replace=True)
    journal.set_repair_scope(repair_scope(ws))
    assert journal.error and '8/8' in journal.error


def test_history_policy_feedback_includes_existing_source_quotes_and_evidence_tools(tmp_path):
    from bonus_agent.agent import run_agent
    from bonus_agent.llm import FakeLLM
    from bonus_agent.rule_package import prepare_formula_inputs
    from tests.test_staged_workflow import save_staged_case

    ws, _ = history_workspace(tmp_path)
    group, exclude = ws.clauses[1], ws.clauses[3]
    ws.clauses = [c for c in ws.clauses if c.id not in (group.id, exclude.id)]
    zero = next(c for c in ws.clauses if c.id == 'C3')
    ws.record_facts([{'name': '零金额判断', 'file': zero.file, 'quote': zero.quote, 'value': '0'}])
    fund = TaskBoard(ws).task_spec('fund')
    ws.plan_formula_tasks([{**fund, 'clauses': []}])
    assert ws.prepare_data()['status'] == 'PASS'
    assert ws._flow().stage()[0] == 3
    report = prepare_formula_inputs(ws)
    failure = report['input_failures'][0]
    assert failure['code'] == 'history_policy_unresolved'
    candidates = failure['evidence_candidates']
    assert any(group.quote in row['quote'] and row['file'] == group.file for row in candidates['grouping'])
    assert any(exclude.quote in row['quote'] and row['file'] == exclude.file for row in candidates['exclusion'])

    calls = []
    def respond(messages, tools):
        calls.append(({t['function']['name'] for t in tools},
                      json.dumps(messages[-1], ensure_ascii=False)))
        if len(calls) == 1:
            return {'tool': 'record_clauses', 'args': {'clauses': [
                {'file': c.file, 'quote': c.quote, 'point': c.point} for c in (group, exclude)]}}
        return {'tool': 'plan_formula_tasks', 'args': {'tasks': [
            {'id': 'average', 'scheme': 'S', 'target': '/variables/average_sales',
             'clauses': ['C1', 'C4', 'C5']}]}}

    out = tmp_path / 'out'
    save_staged_case(ws, out)
    llm = FakeLLM(respond)
    llm.workflow_mode = 'staged'
    run = run_agent(llm, ws.inv.files, out, period=str(ws.period), max_steps=2, deep=0)
    assert calls and 'record_clauses' in calls[0][0]
    assert 'evidence_candidates' in calls[0][1] and group.quote in calls[0][1] and exclude.quote in calls[0][1]
    assert any(s.tool == 'record_clauses' and s.ok for s in run.steps)
    assert {group.quote, exclude.quote} <= {c.quote for c in run.ws.clauses}
    assert TaskBoard(run.ws).statuses()['average']['status'] == 'PASS'


def test_data_requirement_accepts_the_current_entity_primary_key(tmp_path):
    from bonus_agent.formula_inputs import input_failures

    ws, _ = history_workspace(tmp_path)
    requirement = {'dataset': 'teams', 'fields': ['team_join'], 'link': 'team_join'}
    spec = {'scheme': 'S', 'target': '/variables/own_key', 'clauses': [],
            'data_requirements': [requirement], 'expected_scope': 'teams'}
    assert not input_failures(ws, spec, {'scope': 'teams', 'expr': 'team_join'})
    assert input_failures(ws, {**spec, 'expected_scope': 'global'},
                          {'scope': 'global', 'expr': 'team_join'})[0]['code'] == 'link_mismatch'
    wrong = {**requirement, 'link': 'posting'}
    assert input_failures(ws, {**spec, 'data_requirements': [wrong]},
                          {'scope': 'teams', 'expr': 'team_join'})[0]['code'] == 'link_mismatch'


def test_verified_input_releases_its_saved_construction_halt(tmp_path, monkeypatch):
    from bonus_agent import rule_package
    from bonus_agent.agent import run_agent
    from bonus_agent.recovery import _HALT_PREFIX
    from tests.test_staged_workflow import save_staged_case
    from tests.test_supervision import model, reads

    ws, _ = history_workspace(tmp_path)
    zero = ws.clauses[2]
    ws.record_facts([{'name': '零金额规则', 'file': zero.file, 'quote': zero.quote, 'value': '0'}])
    assert ws._flow().stage()[0] == 3
    out = tmp_path / 'case'
    save_staged_case(ws, out)
    with monkeypatch.context() as patch:
        patch.setattr(rule_package, 'prepare_formula_inputs', lambda ws: None)
        run_agent(model([]), ws.inv.files, out, period=str(ws.period), max_steps=0)
    journal = out / 'recovery.json'
    old = json.loads(journal.read_text(encoding='utf-8'))
    blocker = 'construction:average:/variables/average_sales'
    old['halted'] = _HALT_PREFIX + '连续 13 次检查没有消除既有问题；工具返回成功不代表有进展。' + blocker
    old['progress'] = {'previous': [blocker], 'stagnant': 12}
    journal.write_text(json.dumps(old, ensure_ascii=False), encoding='utf-8')

    llm = model(reads(1))
    resumed = run_agent(llm, ws.inv.files, out, period=str(ws.period), max_steps=1)

    assert any(s.args.get('tool') == 'prepare_formula_inputs' and s.ok and s.args.get('saved') for s in resumed.steps)
    assert len(llm.calls) == 1 and TaskBoard(resumed.ws).statuses()['average']['status'] == 'PASS'
    archived = json.loads(journal.read_text(encoding='utf-8'))
    proof = next(iter(archived['resolved_construction_halts'].values()))
    assert set(proof['receipts']) == {blocker}
    assert not resumed.ws.results and not resumed.ws._flow().current('approved:S')


def test_data_preflight_finds_history_and_rejects_month_only_inputs(tmp_path):
    from decimal import Decimal

    from bonus_agent.rule_package import prepare_formula_inputs
    from bonus_core.engine.run import Engine

    ws, quote = history_workspace(tmp_path)
    for name, code in [('invented', 'dataset_missing'), ('posted', 'window_mismatch')]:
        report = ws.submit_formula_task('average', value={
            'scope': 'teams',
            'expr': f'ratio(sum(amount, of: "{name}"), count_distinct(month(posting), of: "{name}"))',
            'sources': [{'doc': '规则.md', 'quote': quote}],
        })
        assert not report['saved'] and report['input_failures'][0]['code'] == code
    assert not ws._ruleset('S').variables

    report = prepare_formula_inputs(ws)
    assert report['saved'] and report['data_input']['source']['sheet'] == '过往流水'
    assert TaskBoard(ws).statuses()['average']['status'] == 'PASS'
    assert ws.datasets['average_sales_history'].window == 'year_to_date'
    rules = ws._ruleset('S')
    rules = rules.model_copy(update={'pool': rules.pool.model_copy(update={'scope': 'teams', 'expr': 'average_sales'})})
    data, _ = ws._data(rules)
    engine = Engine.prepare_basis(rules, data, period=ws.period)
    amounts = {k: engine.ev.evaluate('average_sales', n, n.id, 'variables.average_sales')
               for k, n in engine.ev.index['teams'].items()}
    assert amounts == {'A': Decimal(150), 'B': Decimal(60)}
    assert not ws.llm.calls
    ws.update_dataset('average_sales_history', {'window': 'period'})
    board = TaskBoard(ws)
    assert board.statuses()['average']['status'] == 'PENDING'
    assert board.check('average')['input_failures'][0]['code'] == 'window_mismatch'


def test_history_creates_a_separate_evidenced_group_for_multiple_codes(tmp_path):
    from decimal import Decimal
    from bonus_agent.rule_package import prepare_formula_inputs
    from bonus_core.engine.run import Engine

    ws, _ = history_workspace(tmp_path, distinct_codes=True)
    report = prepare_formula_inputs(ws)
    assert report['status'] == 'PASS' and report['saved'], report
    rules = ws._ruleset('S')
    scope = rules.variables['average_sales'].scope
    assert ws.datasets['teams'].key == 'code' and ws.datasets[scope].key == 'team_join'
    assert ws.datasets['posted'].window == 'period'
    assert ws.datasets['average_sales_history'].link == 'team_join'
    assert ws.prepare_data()['status'] == 'PASS'
    rules = rules.model_copy(update={'pool': rules.pool.model_copy(update={'scope': scope, 'expr': 'average_sales'})})
    data, _ = ws._data(rules)
    engine = Engine.prepare_basis(rules, data, period=ws.period)
    amounts = {key: engine.ev.variable('average_sales', node).v for key, node in engine.ev.index[scope].items()}
    assert amounts == {'A': Decimal(150), 'B': Decimal(60)}


def test_joint_history_repair_commits_dataset_membership_atomically(tmp_path):
    from bonus_agent.indicator_history import history_average_input
    from bonus_agent.rule_package import prepare_formula_inputs

    ws, _ = history_workspace(tmp_path)
    old_membership = set(ws.datasets)
    ws.set_scheme_datasets('S', sorted(old_membership))
    assert ws.prepare_data()['status'] == 'PASS'
    board = TaskBoard(ws)
    proposal = history_average_input(board, board.task_spec('average'))['candidate_call']
    before = copy.deepcopy(case.snapshot(ws))
    rejected = copy.deepcopy(proposal)
    rejected['formulas'][0]['value']['expr'] += ' + unknown_field'
    failed = ws.submit_construction_repair(**rejected)
    assert not failed['saved'] and case.snapshot(ws) == before

    report = prepare_formula_inputs(ws)
    assert report['status'] == 'PASS' and report['saved'], report
    assert set(ws._scheme_datasets('S')) == old_membership | {'average_sales_history'}
    assert TaskBoard(ws).statuses()['average']['status'] == 'PASS'


def test_conflicting_history_inputs_are_reported_once_per_version(tmp_path):
    from bonus_agent.rule_package import prepare_formula_inputs

    ws, _ = history_workspace(tmp_path, conflicting=True)
    report = prepare_formula_inputs(ws)
    assert not report['saved'] and report['input_failures'][0]['code'] == 'history_source_unresolved'
    assert 'average_sales_history' not in ws.datasets and not ws._ruleset('S').variables
    assert prepare_formula_inputs(ws) is None
    summary = TaskBoard(ws).summary()
    assert summary['input_preflight']['input_failures'][0]['code'] == 'history_source_unresolved'


def test_history_uses_linked_source_policy_and_current_population(tmp_path):
    from decimal import Decimal

    from bonus_agent.rule_package import prepare_formula_inputs
    from bonus_core.engine.run import Engine

    ws, _ = history_workspace(tmp_path, linked_policy=True)
    report = prepare_formula_inputs(ws)
    assert report.get('saved'), report
    assert report['data_input']['source']['sheet'] == '过往流水'
    assert ws.datasets['teams'].period == 'posting'
    assert ws.mapping['draft_history'].sheet == '当期明细'
    rules = ws._ruleset('S')
    rules = rules.model_copy(update={'pool': rules.pool.model_copy(update={'scope': 'teams', 'expr': 'average_sales'})})
    data, _ = ws._data(rules)
    engine = Engine.prepare_basis(rules, data, period=ws.period)
    amounts = {key: engine.ev.variable('average_sales', node).v for key, node in engine.ev.index['teams'].items()}
    assert amounts == {'A': Decimal(170), 'B': Decimal(55)}
    assert not ws.llm.calls


def test_resume_reconnects_only_the_proposed_missing_tiers(tmp_path):
    folder = tmp_path / 'history' / '第4轮'
    folder.mkdir(parents=True)
    previous = {'scheme': 'S', 'task': 'base', 'tiers': [{'task': 'old_rate'}]}
    current = {'scheme': 'S', 'task': 'base', 'formulas': [{'task': 'base', 'changes': {'expr': 'old_rate'}}]}
    feedback = {'stage': 'formula', 'failed_task': 'base',
                'issues': ["construction.dependency：先检查依赖任务['old_rate']"]}
    log = {'error': '联合修复候选停止', 'steps': [
        {'tool': 'submit_construction_repair', 'ok': False, 'args': previous},
        {'tool': 'submit_construction_repair', 'ok': False, 'args': current, 'result': json.dumps(feedback)},
    ]}
    path = folder / 'agent_log.json'
    path.write_text(json.dumps(log), encoding='utf-8')
    last = {'n': 4, 'status': 'FAILED', 'error': log['error']}
    call = archived_tier_candidate(tmp_path, last)
    assert call['args'] == {**current, 'tiers': previous['tiers']}
    feedback['issues'] = ["construction.dependency：先检查依赖任务['different']"]
    log['steps'][-1]['result'] = json.dumps(feedback)
    path.write_text(json.dumps(log), encoding='utf-8')
    assert archived_tier_candidate(tmp_path, last) is None
