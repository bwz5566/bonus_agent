"""Exercise constructive progress, dependency invalidation and source semantics, without providers."""
import copy
import json
from decimal import Decimal

import pytest

from bonus_agent import case, facts as fx
from bonus_agent.agent import Workspace, Step, ToolError, run_agent
from bonus_agent.auto import build_inventory
from bonus_agent.construction import TaskBoard
from bonus_agent.context import task_card
from bonus_agent.llm import FakeLLM
from bonus_agent.progress import ProgressWatch, discover
from bonus_agent.semantics import semantic_gates
from tests.test_staged_workflow import make_workspace, prepared, reviewed, save_staged_case

pytestmark = pytest.mark.evidence


def tasks():
    return [{'id':'base','scheme':'S','target':'/variables/base','facts':['提成率'],'clauses':['C1']},
            {'id':'pool','scheme':'S','target':'/pool','depends_on':['base']},
            {'id':'allocation','scheme':'S','target':'/allocation','depends_on':['pool']},
            {'id':'report','scheme':'S','target':'/report','depends_on':['pool']}]


def test_formula_planner_gets_exact_source_names_and_can_repair_a_rejected_plan(tmp_path):
    ws = make_workspace(tmp_path)
    ws.drafts['S'].clear()
    ws.facts[0].targets = []
    assert ws.prepare_data()['status'] == 'PASS'
    out = tmp_path / 'out'
    save_staged_case(ws, out)
    requests = []

    def respond(messages, tools):
        requests.append((messages, {tool['function']['name'] for tool in tools}))
        entries = tasks()
        if len(requests) == 1:
            entries[0]['facts'] = ['项目提成率']
        elif len(requests) == 2:
            entries[0]['target'] = '/variables/amount'
        return {'tool': 'plan_formula_tasks', 'args': {'tasks': entries}}

    llm = FakeLLM(respond)
    llm.workflow_mode = 'staged'
    llm.recovery_mode = 'advisor'
    llm.think_llm = FakeLLM(['{}'])
    result = run_agent(llm, ws.inv.files, out, max_steps=3)
    failed, collision, saved = [s for s in result.steps if s.tool == 'plan_formula_tasks']
    assert not failed.ok and not collision.ok and saved.ok, [step.result for step in (failed, collision, saved)]
    assert 'base' in failed.result and '"项目提成率"' in failed.result and '"提成率"' in failed.result
    assert 'read_workspace' in failed.result
    assert 'construction.name_clash' in collision.result and '/variables/amount' in collision.result
    messages, tools = requests[0]
    card = json.loads(messages[-1]['content'].removeprefix('【当前任务卡】'))
    assert card['workflow']['construction']['planning_facts']['items'] == [
        {'name': '提成率', 'value': '10%', 'file': 'policy.md'},
    ]
    assert {'plan_formula_tasks', 'read_workspace'} <= tools and 'trial_run' not in tools
    assert 'compile_table' not in tools and 'submit_construction_repair' not in tools
    assert not llm.think_llm.calls
    assert TaskBoard(result.ws).task_spec('base')['facts'] == ['提成率']


def test_ready_formula_receives_complete_checked_inputs_while_stale_ones_are_excluded(tmp_path):
    ws = make_workspace(tmp_path)
    ws.prepare_data()
    ws.plan_formula_tasks([
        {'id': 'base', 'scheme': 'S', 'target': '/variables/amount_base'},
        {'id': 'apply', 'scheme': 'S', 'target': '/variables/derived', 'depends_on': ['base']},
        {'id': 'fund', 'scheme': 'S', 'target': '/pool', 'depends_on': ['apply']},
        {'id': 'report', 'scheme': 'S', 'target': '/report', 'depends_on': ['fund']},
    ])
    assert ws.submit_formula_task('base', value={'scope': '项目', 'expr': 'amount'})['saved']
    view = json.loads(task_card(ws))['workflow']['construction']
    assert view['active']['id'] == 'apply'
    item, = view['checked_inputs']['items']
    assert item['task'] == 'base' and item['target'] == '/variables/amount_base'
    assert item['definition']['expr'] == 'amount' and item['definition']['scope'] == '项目'
    ws.set_rules(scheme='S', variables={'amount_base': {'scope': '项目', 'expr': 'unknown_name'}})
    view = json.loads(task_card(ws))['workflow']['construction']
    assert view['active']['id'] == 'base' and not view['checked_inputs']['items']


def test_atomic_formula_requires_its_explicit_quote_to_exist(tmp_path):
    ws = make_workspace(tmp_path)
    ws.plan_formula_tasks([{'id': 'metric', 'scheme': 'S', 'target': '/variables/metric'}])
    ws.prepare_data()
    value = {'scope': '项目', 'expr': 'amount',
             'sources': [{'doc': 'policy.md', 'quote': '每个项目按回款金额核算'}]}
    bad = ws.submit_formula_task('metric', value=value)
    assert bad['status'] == 'FAIL' and not bad['saved']
    assert any('source.quote' in issue for issue in bad['issues'])
    assert TaskBoard(ws).value(TaskBoard(ws).task_spec('metric')) is None
    value['sources'][0]['quote'] = '每个项目按销售金额的10%计算奖金'
    good = ws.submit_formula_task('metric', value=value)
    assert good['saved'] and good['checked_rule_source']['state'] == 'VERIFIED'


def test_report_total_citation_is_checked_in_its_own_phase(tmp_path):
    ws = make_workspace(tmp_path)
    ws.plan_formula_tasks([{'id': root, 'scheme': 'S', 'target': '/' + root}
                           for root in ('pool', 'report', 'allocation')])
    ws.prepare_data()
    assert ws.check_formula_task('pool')['status'] == 'PASS'
    report = ws.drafts['S']['report']
    report['distributions'][0]['sources'] = [{'doc': 'policy.md', 'quote': '未提供的分配政策'}]
    # The distribution source must not block the foundation calculation.
    assert ws.check_formula_task('report')['status'] == 'PASS'
    candidate = copy.deepcopy(report)
    candidate['totals'][0]['sources'] = [{'doc': 'policy.md', 'quote': '不存在的合计公式'}]
    failed = ws.submit_formula_task('report', value=candidate)
    assert not failed['saved'] and any('source.quote' in issue for issue in failed['issues'])
    # The full, editable report is unchanged. The pending distribution phase
    # cannot be released by a successful basis-only check.
    assert report['totals'][0]['sources'][0]['quote'] == '每个项目按销售金额的10%计算奖金'


def test_construct_validate_and_invalidate_dependent_tasks(tmp_path):
    ws=make_workspace(tmp_path)
    ws.prepare_data()
    board=TaskBoard(ws)
    ws.plan_formula_tasks(tasks())
    assert 'base' in ws._flow().hint() and 'submit_formula_task' in ws._flow().hint()
    assert ws.check_formula_task('pool')['status']=='FAIL'
    assert ws.compile_formulas()['status']=='FAIL'
    f=ws.facts[0]
    ws.set_rules(scheme='S', variables={'base':{'scope':'项目','expr':'amount * 0.1'}},
                 bindings=[{'name':f.name,'targets':f.targets+['S::/variables/base/expr']}])
    assert ws.check_formula_task('base')['status']=='PASS'
    ws.set_rules(scheme='S',pool={'scope':'项目','expr':'base'},
                 bindings=[{'name':f.name,'targets':[t for t in ws.facts[0].targets if t != 'S::/pool/expr']}])
    for n in ('pool','allocation','report'):
        assert ws.check_formula_task(n)['status']=='PASS'
    assert ws.compile_formulas()['status']=='PASS'
    assert board.summary()['active'] is None
    ws.set_rules(scheme='S',variables={'base':{'scope':'项目','expr':'amount * 0.2'}})
    status=board.statuses()
    assert status['base']['status']=='PENDING' and status['pool']['status']=='WAITING'
    assert not ws._flow().current('formulas')
    assert ws.check_formula_task('base')['status']=='FAIL'
    assert not ws.llm.calls


def test_plan_cycle_and_runtime_cycle_never_approve_or_break_reading(tmp_path):
    ws=make_workspace(tmp_path); original=copy.deepcopy(ws.drafts)
    with pytest.raises(ToolError,match='construction.cycle'):
        ws.plan_formula_tasks([{'id':'a','scheme':'S','target':'/variables/a','depends_on':['b']},
                               {'id':'b','scheme':'S','target':'/variables/b','depends_on':['a']}])
    assert ws.drafts==original
    ws.plan_formula_tasks([{'id':'a','scheme':'S','target':'/variables/a'}, {'id':'b','scheme':'S','target':'/variables/b'}])
    ws.set_rules(scheme='S',variables={'a':{'expr':'b'},'b':{'expr':'a'}})
    assert ws.check_formula_task('a')['status']=='FAIL'
    assert ws.read_workspace('construction')['items']
    assert task_card(ws)


def test_formula_plan_rejects_same_name_table_and_variable_before_writing(tmp_path):
    ws = make_workspace(tmp_path)
    plan = [
        {'id': 'table', 'scheme': 'S', 'target': '/tables/rate'},
        {'id': 'value', 'scheme': 'S', 'target': '/variables/rate', 'depends_on': ['table']},
    ]
    with pytest.raises(ToolError, match=r'construction.name_clash.*rate'):
        ws.plan_formula_tasks(plan)
    assert not TaskBoard(ws).state['tasks']
    plan[1]['target'] = '/variables/applied_rate'
    assert ws.plan_formula_tasks(plan)['status'] == 'PLANNED'
    assert TaskBoard(ws).statuses()['value']['depends_on'] == ['table']


def test_basis_phase_waits_for_money_outputs_and_defers_recipient_outputs(tmp_path):
    ws = make_workspace(tmp_path)
    plan = ws.workflow['plan']
    ws.plan_calculation(plan['summary'], ['项目和人员结果'], plan['files'], plan['schemes'],
                        output_targets={'项目和人员结果': [
                            'S::/report/details/项目奖', 'S::/report/people', 'S::/report/pending',
                        ]})
    ws.plan_formula_tasks([{'id': root, 'scheme': 'S', 'target': '/' + root}
                           for root in ('pool', 'report', 'allocation')])
    ws.drafts['S']['report'].pop('people')
    board = TaskBoard(ws)
    assert board.output_obligations('basis') == []
    assert {gap['target'] for gap in board.output_obligations('all')} == {'/report/people', '/report/pending'}
    ws.drafts['S']['report'].pop('details')
    assert [gap['target'] for gap in board.output_obligations('basis')] == ['/report/details/项目奖']


def test_confirmed_work_prioritizes_recipient_dependencies_but_keeps_all_gates(tmp_path):
    ws = make_workspace(tmp_path)
    ws.plan_formula_tasks([
        {'id': 'other', 'scheme': 'S', 'target': '/tables/alternative'},
        {'id': 'roles', 'scheme': 'S', 'target': '/tables/role_rates'},
        {'id': 'pool', 'scheme': 'S', 'target': '/pool'},
        {'id': 'allocation', 'scheme': 'S', 'target': '/allocation', 'depends_on': ['roles']},
        {'id': 'report', 'scheme': 'S', 'target': '/report', 'depends_on': ['allocation']},
    ])
    ws.prepare_data()
    assert ws.check_formula_task('pool')['status'] == 'PASS'
    assert ws.check_formula_task('report')['status'] == 'PASS'
    reviewed(ws)
    flow = ws._flow()
    flow.approve('S', '财务', flow.state['receipts']['basis:S']['payload']['digest'])
    board = TaskBoard(ws)
    assert board.summary()['active']['id'] == 'roles'
    assert any('other' in issue for issue in board.gate('all'))


def test_unreferenced_task_check_does_not_add_a_basis_clause_binding(tmp_path):
    ws = make_workspace(tmp_path)
    ws.plan_formula_tasks([
        {'id': 'base', 'scheme': 'S', 'target': '/pool', 'clauses': ['C1']},
        {'id': 'alternate', 'scheme': 'S', 'target': '/variables/unused', 'clauses': ['C1']},
        {'id': 'people', 'scheme': 'S', 'target': '/allocation'},
        {'id': 'report', 'scheme': 'S', 'target': '/report'},
    ])
    assert ws.prepare_data()['status'] == 'PASS'
    assert ws.check_formula_task('base')['status'] == 'PASS'
    assert ws.check_formula_task('report')['status'] == 'PASS'
    reviewed(ws)
    flow = ws._flow()
    flow.approve('S', '财务', flow.state['receipts']['basis:S']['payload']['digest'])
    ws.set_rules(scheme='S', variables={'unused': {'scope': '项目', 'expr': 'amount'}})
    assert ws.check_formula_task('alternate')['status'] == 'PASS'
    assert ws.clauses[0].where == 'pool'
    assert flow.current('approved:S')


def test_after_sum_plan_keeps_funding_outputs_and_moves_recipient_outputs(tmp_path):
    from bonus_agent.context import _recipient_scheme_plan

    def add_roster(workbook):
        table = workbook.create_sheet('separate roster')
        table.append(['membership', 'recipient'])
        table.append(['r1', '甲'])

    ws = make_workspace(tmp_path, mutate_workbook=add_roster)
    flow = ws._flow()
    plan = flow.state['plan']
    flow.plan(plan['summary'], plan['outputs'], plan['files'], ['S'], output_targets={
        '项目底稿': ['S::/report/details/项目奖'],
        '总额': ['S::/report/totals'],
        '人员金额': ['S::/report/people', 'S::/report/pending'],
    })
    reviewed(ws)
    digest = flow.state['receipts']['basis:S']['payload']['digest']
    flow.approve('S', '财务', digest)
    plan = flow.state['plan']
    source = {'id': 'rounding', 'quote': '本批先按人员汇总，再四舍五入到两位。', 'file': 'policy.md'}
    assert not _recipient_scheme_plan(plan, 'S', [{**source, 'quote': '按项目确认总额'}])
    proposal = _recipient_scheme_plan(plan, 'S', [source])
    assert flow.plan(**proposal['call']['args'])['status'] == 'PASS'
    targets = flow.state['plan']['output_targets']
    assert targets['项目底稿'] == ['S::/report/details/项目奖'] and targets['总额'] == ['S::/report/totals']
    recipient = proposal['recipient_scheme']
    assert targets['人员金额'] == [recipient + '::/report/people', recipient + '::/report/pending']
    assert flow.current('basis:S') and flow.current('review:S') and flow.current('approved:S')
    assert not flow.current('allocation_formulas')
    ws.define_dataset('roster_detail', 'data.xlsx', 'separate roster',
                      {'member_id': {'column': 'membership', 'type': 'id'},
                       'recipient': {'column': 'recipient', 'type': 'text'}}, key='member_id')
    ws.set_rules(scheme=recipient, pool={'scope': 'roster_detail', 'expr': '0'},
                 allocation=[{'level': 'roster_detail', 'key': 'member_id'}])
    assert 'roster_detail' in ws._ruleset(recipient).datasets
    assert 'roster_detail' not in ws._ruleset('S').datasets
    assert flow.current('basis:S') and flow.current('review:S') and flow.current('approved:S')
    targets['项目底稿'] = ['S::/report/details/其他项目金额']
    assert not flow.current('approved:S')


def test_basis_report_card_uses_its_effective_inputs_and_the_planned_money_outputs(tmp_path):
    ws = make_workspace(tmp_path)
    plan = ws.workflow['plan']
    ws.plan_calculation(plan['summary'], ['跨对象明细与人员'], plan['files'], ['S'], output_targets={
        '跨对象明细与人员': ['S::/report/details/合同净额', 'S::/report/totals', 'S::/report/people']})
    ws.drafts['S'].pop('report')
    ws.prepare_data()
    ws.plan_formula_tasks([{'id':'source_amount','target':'/variables/source_amount','scheme':'S'},
                           {'id':'fund','target':'/pool','scheme':'S','depends_on':['source_amount']},
                           {'id':'pay','target':'/allocation','scheme':'S','depends_on':['fund']},
                           {'id':'print','target':'/report','scheme':'S','depends_on':['pay']}])
    assert ws.submit_formula_task('source_amount', value={'scope':'项目','expr':'amount'})['saved']
    assert ws.check_formula_task('fund')['status'] == 'PASS'
    view = json.loads(task_card(ws))['workflow']['construction']
    assert view['active']['id'] == 'print' and view['active']['depends_on'] == ['fund']
    brief = view['report_task']
    assert brief['phase'] == 'basis'
    assert [t['target'] for t in brief['outputs']] == ['/report/details/合同净额', '/report/totals']
    assert brief['deferred_inputs'] == ['pay']
    assert brief['scope_keys']['项目'] == 'pid'
    assert view['checked_inputs']['items'][0]['definition']['expr'] == 'amount * 0.1'
    assert [item['task'] for item in view['checked_inputs']['items']] == ['fund','source_amount']
    assert ws.workflow['construction']['tasks']['print']['depends_on'] == ['pay']
    row = {'name':'合同净额', 'level':'项目', 'expr':'amount * 0.1',
           'sources': ws.drafts['S']['pool']['sources']}
    incomplete = ws.submit_formula_task('print', value={'details': [{**row, 'stage':'distribution'}], 'totals':[row]})
    assert incomplete['status'] == 'FAIL' and any('/report/details/合同净额' in s for s in incomplete['issues'])
    assert ws.submit_formula_task('print', value={'details':[row], 'totals':[row]})['status'] == 'PASS'


def test_resumed_basis_report_request_uses_current_amounts_and_report_tool(tmp_path):
    ws = make_workspace(tmp_path)
    report = ws.drafts['S'].pop('report')
    for key in ('people','distributions'):
        report.pop(key)
    ws.prepare_data()
    ws.plan_formula_tasks([{'id':'fund','target':'/pool','scheme':'S'},
                           {'id':'pay','target':'/allocation','scheme':'S','depends_on':['fund']},
                           {'id':'print','target':'/report','scheme':'S','depends_on':['pay']}])
    assert ws.check_formula_task('fund')['status'] == 'PASS'
    out = tmp_path / 'out'
    save_staged_case(ws, out)
    requests = []

    def respond(messages, tools):
        requests.append((messages[0]['content'], {t['function']['name'] for t in tools}))
        return {'tool':'submit_formula_task','args':{'task':'print','value':report}}

    llm = FakeLLM(respond)
    llm.workflow_mode = 'staged'
    result = run_agent(llm, ws.inv.files, out, max_steps=1)
    prompt, tools = requests[0]
    assert 'report_task' in prompt and 'details' in prompt and 'totals' in prompt
    assert 'submit_formula_task' in tools and 'read_document' in tools and 'set_rules' not in tools
    assert len(requests) == 1 and any(s.tool == 'submit_formula_task' and s.ok for s in result.steps)
    assert TaskBoard(result.ws).state['reports']['print']['execution']['status'] == 'PASS'
    assert not result.ws.results and not result.ws.workflow['basis']


def test_invalid_basis_detail_fails_on_a_later_object_before_report_is_saved(tmp_path):
    def split(workbook):
        workbook['people']['D3'] = .25
        workbook['people']['D4'] = .75

    ws = make_workspace(tmp_path, mutate_workbook=split)
    assert ws.prepare_data()['status'] == 'PASS'
    ws.plan_formula_tasks([{'id':'fund','target':'/pool','scheme':'S'},
                           {'id':'print','target':'/report','scheme':'S','depends_on':['fund']}])
    assert ws.check_formula_task('fund')['status'] == 'PASS'
    assert ws.check_formula_task('print')['status'] == 'PASS'
    original = copy.deepcopy(ws._merged('S')['report'])
    valid = original['details'][0]
    bad = ws.submit_formula_task('print', changes={
        'details': [{**valid, 'expr':'amount * 0.1 * single_value(share, of: "people")'}]})
    assert not bad['saved'] and bad['status'] == 'FAIL'
    assert any('single_value.multiple' in issue and '项目:B' in issue for issue in bad['issues'])
    assert bad['execution']['repair']['task'] == 'print'
    assert bad['execution']['repair']['component'] == '/report/details'
    assert ws._merged('S')['report'] == original
    assert TaskBoard(ws).statuses()['print']['status'] == 'PASS'
    good = ws.submit_formula_task('print', changes={'details': [{**valid, 'expr':'amount * 0.1'}]})
    assert good['saved'] and good['execution']['status'] == 'PASS'


def test_pool_submission_executes_its_dependencies_on_later_objects_and_keeps_the_previous_pool(tmp_path):
    def split(workbook):
        workbook['people']['D3'] = .25
        workbook['people']['D4'] = .75

    ws = make_workspace(tmp_path, mutate_workbook=split)
    assert ws.prepare_data()['status'] == 'PASS'
    ws.plan_formula_tasks([{'id':'factor','scheme':'S','target':'/variables/customer_share'},
                           {'id':'fund','scheme':'S','target':'/pool'}])
    assert ws.submit_formula_task('factor', value={
        'scope':'项目','expr':'single_value(share, of: "people")'})['status'] == 'PASS'
    original = copy.deepcopy(ws._merged('S')['pool'])
    invalid = ws.submit_formula_task('fund', changes={'expr':'amount * 0.1 * customer_share'})
    assert invalid['status'] == 'FAIL' and not invalid['saved']
    assert any('single_value.multiple' in issue and '项目:B' in issue for issue in invalid['issues'])
    assert any('factor' in issue for issue in invalid['issues'])
    assert invalid['execution']['repair']['task'] == 'factor'
    assert invalid['execution']['repair']['scope_keys']['项目'] == 'pid'
    assert invalid['next'] == invalid['execution']['repair']['next']
    assert ws._merged('S')['pool'] == original
    # A material condition may legitimately make the variable partial outside its callers.
    valid = ws.submit_formula_task('fund', changes={
        'expr':'if(pid == "A", amount * 0.1 * customer_share, amount * 0.1)'})
    assert valid['status'] == 'PASS' and valid['saved']
    assert not ws.results and not ws.workflow['basis']


def test_failed_pool_recheck_revokes_even_a_same_version_receipt(tmp_path, monkeypatch):
    ws = make_workspace(tmp_path)
    ws.prepare_data()
    ws.plan_formula_tasks([{'id': 'fund', 'scheme': 'S', 'target': '/pool'}])
    assert ws.check_formula_task('fund')['status'] == 'PASS'
    monkeypatch.setattr('bonus_core.engine.preflight.preflight_basis', lambda *args: {
        'status': 'FAIL', 'issues': [{'message': 'current execution failed', 'target': '/pool'}]})
    assert ws.check_formula_task('fund')['status'] == 'FAIL'
    assert TaskBoard(ws).statuses()['fund']['status'] == 'PENDING'


def test_pool_repair_distinguishes_iterated_rows_from_the_linked_value_source(tmp_path):
    def workbook(book):
        project_rows = book['projects']
        project_rows['C1'] = 'Group'
        project_rows['C2'] = 'East'
        project_rows['C3'] = 'West'
        targets = book.create_sheet('group_targets')
        targets.append(['Group', 'Target'])
        targets.append(['East', 10])
        targets.append(['West', 12])
        targets.append(['Inactive', 15])

    ws = make_workspace(tmp_path, mutate_workbook=workbook)
    ws.update_dataset('projects', {'fields': {'group_code': {'column': 'Group', 'type': 'id'}}})
    ws.define_dataset('targets', 'data.xlsx', 'group_targets', {
        'group_code': {'column': 'Group', 'type': 'id'},
        'target_amount': {'column': 'Target', 'type': 'money'},
    }, key='group_code')
    assert ws.prepare_data()['status'] == 'PASS'
    ws.plan_formula_tasks([{'id': 'target', 'scheme': 'S', 'target': '/variables/selected_target'},
                           {'id': 'fund', 'scheme': 'S', 'target': '/pool'}])
    wrong = {'scope': 'global', 'expr': 'single_value(target_amount, of: "projects")'}
    rejected = ws.submit_formula_task('target', value=wrong)
    assert rejected['status'] == 'FAIL' and not rejected['saved']
    proposed = {'expr': 'amount * 0.1 + selected_target'}
    # Old case files may already contain this formula. The pool preflight must still
    # distinguish the rows it iterated from the linked cells supplying their values.
    ws.set_rules(scheme='S', variables={'selected_target': wrong},
                 pool={**ws._merged('S')['pool'], **proposed})
    board = TaskBoard(ws)
    execution, issues = board.basis_execution(board.task_spec('fund'), ws._ruleset('S'))
    assert issues and execution['status'] == 'FAIL'
    observation, = execution['repair']['aggregate_evidence']
    assert observation['dataset'] == 'projects' and observation['selected_rows'] == 2
    assert [r['aggregate_source'] for r in observation['competing_values']] == [
        'data.xlsx/projects!2', 'data.xlsx/projects!3']
    assert [r['sources'] for r in observation['competing_values']] == [
        [{'dataset': 'targets', 'source': 'data.xlsx/group_targets!2'}],
        [{'dataset': 'targets', 'source': 'data.xlsx/group_targets!3'}],
    ]
    group, = observation['source_scopes']
    assert group['scope'] == 'targets' and group['key'] == 'group_code'
    assert (group['pool_objects_reaching_scope'], group['pool_objects']) == (2, 2)
    assert (group['scopes_with_aggregate_rows'], group['scope_objects']) == (2, 3)
    assert group['mapping_call']['args']['name'] == 'targets'
    assert ws.submit_formula_task('target', changes={
        'scope': 'targets', 'expr': 'target_amount'})['status'] == 'PASS'
    assert ws.submit_formula_task('fund', changes=proposed)['status'] == 'PASS'
    assert not ws.results and not ws.workflow['basis']


@pytest.mark.parametrize('split_quote', [False, True])
def test_quoted_identifier_mismatch_blocks_formula_until_raw_value_matches(tmp_path, monkeypatch, split_quote):
    policy = '当渠道为AC01(PH）时按该销售规则'
    quote = f'渠道规则 {policy}' if split_quote else policy

    def add_channel(workbook):
        workbook['projects']['C1'] = '渠道标识'
        workbook['projects']['C2'] = 'AC01(PH）'
        workbook['projects']['C3'] = 'B'
        source = workbook.create_sheet('规则')
        source.append(['标题', '规则'] if split_quote else ['规则'])
        source.append(['渠道规则', policy] if split_quote else [policy])

    ws = make_workspace(tmp_path, add_channel)
    ws.update_dataset('projects', {'fields': {'channel': {'column': '渠道标识', 'type': 'id'}}})
    assert ws.prepare_data()['status'] == 'PASS'
    ws.plan_formula_tasks([{'id': 'channel', 'scheme': 'S', 'target': '/variables/channel_money'}])
    proposal = {'scope': '项目', 'expr': 'if(channel == "AC01(PH)", amount, 0)',
                'sources': [{'doc': 'data.xlsx', 'quote': quote}]}
    failure = ws.submit_formula_task('channel', value=proposal)
    assert failure['status'] == 'FAIL' and not failure['saved']
    assert any('construction.identity_literal' in i and 'AC01(PH）' in i for i in failure['issues'])
    assert 'channel_money' not in ws.drafts['S'].get('variables', {})
    from bonus_agent.semantics import ground_identity_literals
    from bonus_core.dsl.models import Variable
    rules = ws._ruleset('S')
    proposed = rules.model_copy(update={'variables': {'channel_money': Variable.model_validate(proposal)}})
    fixed, evidence = ground_identity_literals(ws, proposed, 'channel_money', ())
    assert fixed == 'if(channel == "AC01(PH）", amount, 0)'
    assert evidence[0]['quote'] == quote and evidence[0]['physical'] == 'AC01(PH）'
    with monkeypatch.context() as ambiguous:
        ambiguous.setattr(ws, '_values', lambda *args: ['AC01(PH）', 'ＡC01(PH)'])
        assert ground_identity_literals(ws, proposed, 'channel_money', ()) == (proposal['expr'], [])
    proposal['expr'] = 'if(channel == "AC01(PH）", amount, 0)'
    assert ws.submit_formula_task('channel', value=proposal)['status'] == 'PASS'


def test_root_task_clauses_have_real_content_and_changed_content_is_visible(tmp_path):
    ws=make_workspace(tmp_path)
    ws.record_clauses([{'file':'policy.md','point':'名单分配','quote':'按名单比例分配'}])
    ws.prepare_data()
    # Adopt the fixture's complete rules through a task for each existing component.
    plan=[p for p in tasks() if p['id']!='base'];plan[0]['depends_on']=[];plan[0]['clauses']=['C1']
    plan[1]['clauses']=['C2']
    ws.plan_formula_tasks(plan,replace=True)
    for name in ('pool','allocation','report'):
        assert ws.check_formula_task(name)['status']=='PASS'
    clause=next(c for c in ws.clauses if c.id=='C2')
    assert clause.where=='allocation' and clause.checked
    before=clause.checked
    ws.drafts['S']['allocation'][1]['weight']='1'
    assert ws._clause_sig(clause,ws._element_texts())!=before


def test_future_obligations_do_not_mask_current_task_progress(tmp_path):
    ws=make_workspace(tmp_path);ws.prepare_data();ws.plan_formula_tasks(tasks())
    assert ws.blocker_frontier()=={'construction:base:/variables/base'}
    seen=set(); watch=ProgressWatch()
    for n in range(18):
        step=Step('read_workspace', {'section':'clauses','offset':n},
                  json.dumps({'section':'clauses','items':[{'id':f'C{n}','point':'原文'}]}), True)
        assert discover(step,seen)
        assert watch.observe(ws.blocker_frontier(), discovery=True)==0
    for n in range(12):
        same=Step('read_workspace', {'section':'clauses','query':f'不同查询{n}'},
                  json.dumps({'section':'clauses','items':[{'id':'C1','point':'原文'}]}),True)
        assert not discover(same,seen)
        count=watch.observe(ws.blocker_frontier())
    assert count==12


def test_report_after_distribution_requires_a_planned_basis_before_it_is_ready(tmp_path):
    from bonus_agent.progress import repair_scope

    ws = make_workspace(tmp_path)
    ws.drafts['S'].pop('pool')
    ws.drafts['S'].pop('report')
    assert ws.prepare_data()['status'] == 'PASS'
    plan = [
        {'id': 'net', 'scheme': 'S', 'target': '/variables/net'},
        {'id': 'person', 'scheme': 'S', 'target': '/variables/person_weight'},
        {'id': 'divide', 'scheme': 'S', 'target': '/allocation', 'depends_on': ['net', 'person']},
        {'id': 'summary', 'scheme': 'S', 'target': '/report', 'depends_on': ['divide']},
    ]
    board = TaskBoard(ws)
    saved = ws.plan_formula_tasks(plan)
    assert saved['active'] is None
    assert 'plan_formula_tasks' in saved['next'] and 'S::/pool' in saved['next']
    assert {entry['id'] for entry in saved['planning_gap']['downstream_inputs']} == {'net', 'person'}
    assert ws.read_workspace('construction', name='summary')['items'][0]['planned_depends_on'] == ['divide']
    assert ws._flow().hint() == saved['next']
    assert board.statuses()['summary']['status'] == 'WAITING'
    assert '/pool' in ws.check_formula_task('summary')['issues'][0]
    assert 'net' not in repair_scope(ws)['deferred']
    assert ws.blocker_frontier() == {'construction.plan:S:/pool'}

    ws.plan_formula_tasks([{'id': 'fund', 'scheme': 'S', 'target': '/pool', 'depends_on': ['net']}])
    assert board.required('basis') == {'net', 'fund', 'summary'}
    assert board.statuses()['summary']['depends_on'] == ['fund']
    assert board.summary()['active']['id'] == 'net'
    assert {'person', 'divide'} <= set(repair_scope(ws)['deferred'])
    assert 'net' not in repair_scope(ws)['deferred']
    assert 'S' not in ws.workflow['basis'] and not ws.results

    with pytest.raises(ToolError, match=r'construction.cycle'):
        ws.plan_formula_tasks([{'id': 'fund', 'scheme': 'S', 'target': '/pool', 'depends_on': ['summary']}])
    assert board.statuses()['fund']['depends_on'] == ['net']


def test_missing_pool_is_scoped_to_its_scheme(tmp_path):
    ws = make_workspace(tmp_path)
    ws.workflow['plan']['schemes'].append('另一个方案')
    board = TaskBoard(ws)
    saved = ws.plan_formula_tasks([
        *tasks(),
        {'id': 'other_share', 'scheme': '另一个方案', 'target': '/variables/role_weight'},
        {'id': 'other_dist', 'scheme': '另一个方案', 'target': '/allocation', 'depends_on': ['other_share']},
        {'id': 'other_report', 'scheme': '另一个方案', 'target': '/report', 'depends_on': ['other_dist']},
    ])
    gap = saved['planning_gap']
    assert gap['scheme'] == '另一个方案' and [r['id'] for r in gap['downstream_inputs']] == ['other_share']
    assert board.statuses()['other_report'].get('missing_pool')
    ws.plan_formula_tasks([{'id': 'other_fund', 'scheme': '另一个方案', 'target': '/pool'}])
    assert board.statuses()['other_report']['depends_on'] == ['other_fund']
    assert board.statuses()['report']['depends_on'] == ['pool']


def test_parallel_required_receipt_counts_once_while_first_task_stays_pending(tmp_path):
    ws = make_workspace(tmp_path)
    assert ws.prepare_data()['status'] == 'PASS'
    ws.plan_formula_tasks([
        {'id': 'first', 'scheme': 'S', 'target': '/variables/first_input'},
        {'id': 'second', 'scheme': 'S', 'target': '/variables/second_input'},
        {'id': 'weight', 'scheme': 'S', 'target': '/variables/person_weight'},
        {'id': 'fund', 'scheme': 'S', 'target': '/pool', 'depends_on': ['first', 'second']},
        {'id': 'report', 'scheme': 'S', 'target': '/report', 'depends_on': ['fund']},
        {'id': 'divide', 'scheme': 'S', 'target': '/allocation', 'depends_on': ['fund', 'weight']},
    ])
    assert TaskBoard(ws).summary()['active']['id'] == 'first'
    watch = ProgressWatch()
    assert watch.observe_workspace(ws) == 0
    assert watch.observe_workspace(ws) == 1
    value = {'scope': '项目', 'expr': 'amount'}
    assert ws.submit_formula_task('weight', value=value)['saved']
    assert watch.observe_workspace(ws) == 2
    assert ws.submit_formula_task('second', value=value)['saved']
    assert TaskBoard(ws).summary()['active']['id'] == 'first'
    assert watch.observe_workspace(ws) == 0
    assert ws.check_formula_task('second')['status'] == 'PASS'
    resumed = ProgressWatch(set(watch.previous), watch.stagnant, json.loads(json.dumps(watch.compile_state)))
    assert resumed.observe_workspace(ws) == 1
    assert not ws.results


def test_finite_discovery_and_task_receipts_survive_resume(tmp_path):
    ws=prepared(make_workspace(tmp_path)); before=TaskBoard(ws).statuses()
    clone=Workspace(FakeLLM([]),ws.inv);case.restore(clone,case.snapshot(ws))
    assert TaskBoard(clone).statuses()==before
    seen=set(); step=Step('read_workspace',{},json.dumps({'section':'facts','items':[{'name':'rate'}]}),True)
    assert discover(step,seen)
    restored=set(json.loads(json.dumps(sorted(seen))))
    assert not discover(step,restored)
    spec=copy.deepcopy(TaskBoard(clone).state['tasks'])
    first=next(iter(spec.values())); first['title']='更新来源规划'
    clone.plan_formula_tasks([first])
    assert not clone._flow().current('formulas')


def test_scope_variants_do_not_merge_identifiers_or_hide_wrong_pages(tmp_path):
    assert fx.scope_norm('PI 事業部 銷售執行人')==fx.scope_norm('PI事业部销售执行人')
    assert fx.norm('事業部客户001')!=fx.norm('事业部客户001')
    from pptx import Presentation
    from pptx.util import Inches
    prs=Presentation()
    for caption in ('PI 事業部銷售提成總包分成比例','其他事业部销售执行人比例'):
        slide=prs.slides.add_slide(prs.slide_layouts[6])
        t=slide.shapes.add_table(2,2,Inches(0),Inches(0),Inches(6),Inches(2)).table
        t.cell(0,1).text='新项目';t.cell(1,0).text='销售執行人';t.cell(1,1).text='35%'
        slide.shapes.add_textbox(Inches(0),Inches(3),Inches(6),Inches(1)).text=caption
    path=tmp_path/'policy.pptx';prs.save(path)
    ws=Workspace(FakeLLM([]),build_inventory([path]))
    spec={'name':'PI事业部销售执行人比例','value':'35%','file':path.name,'page':1,
          'source_cell':{'table':1,'row':2,'column':2},'quote':'销售執行人\n新项目\n35%'}
    ws.record_facts([spec])
    assert len(ws.facts)==1
    with pytest.raises(ToolError,match='其他事业部'):
        ws.record_facts([{**spec,'name':'其他事业部销售执行人比例'}])


def mode_workspace(tmp_path):
    p=tmp_path/'policy.md';p.write_text('按超额累进计算提成。低档比例10%，高档比例20%，档位100元。',encoding='utf8')
    ws=Workspace(FakeLLM([]),build_inventory([p]))
    ws.record_facts([{'name':'low','value':'10%','file':p.name,'quote':'低档比例10%'},
                     {'name':'high','value':'20%','file':p.name,'quote':'高档比例20%'},
                     {'name':'band','value':'100元','file':p.name,'quote':'档位100元'}])
    ws.record_clauses([{'file':p.name,'point':'必须超额累进','quote':'按超额累进计算提成'}])
    return ws,{'file':p.name,'quote':'按超额累进计算提成'}


def test_explicit_algorithm_cannot_compile_as_whole_and_gate_follows_dependencies(tmp_path):
    ws,basis=mode_workspace(tmp_path)
    rows=[{'value':'low'},{'lower':'band','value':'high'}]
    with pytest.raises(ToolError,match='algorithm.mode'):
        ws.compile_table('rate',rows,scheme='S',mode='whole',algorithm_basis=basis)
    assert not ws.drafts
    ws.compile_table('rate',rows,scheme='S',algorithm_basis=basis)
    assert ws.drafts['S']['tables']['rate']['mode']=='excess'
    ws.set_rules(scheme='S', variables={'bonus':{'expr':'tier(rate, 150)'}}, pool={'expr':'bonus'})
    ws.cover_clauses({'C1':'pool'})
    assert not semantic_gates(ws)
    ws.drafts['S']['tables']['rate']['mode']='whole'
    assert any('algorithm.mode' in e for e in semantic_gates(ws))
    # Same source rates produce different money; the mode guard has a financial consequence.
    from bonus_core.engine.evaluator import tier_amount
    from bonus_core.dsl.models import TierTable
    assert tier_amount(TierTable(mode='excess',steps=[(0,'.1'),(100,'.2')]),Decimal(150))==20
    assert tier_amount(TierTable(mode='whole',steps=[(0,'.1'),(100,'.2')]),Decimal(150))==30


def test_explicit_negative_policy_is_checked_without_changing_the_default(tmp_path):
    ws,basis=mode_workspace(tmp_path)
    p=tmp_path/'negative.md';p.write_text('负数按阶梯照算，保留负奖金。',encoding='utf8')
    new=Workspace(FakeLLM([]),build_inventory([*ws.inv.files,p]))
    new.record_clauses([{'file':p.name,'point':'保留负值','quote':'负数按阶梯照算，保留负奖金'}])
    new.set_rules(scheme='S',tables={'rate':{'kind':'tier','mode':'excess','steps':[[0,'.1']]}},pool={'expr':'tier(rate, -10)'})
    new.cover_clauses({'C1':'pool'})
    assert any('algorithm.negative' in x for x in semantic_gates(new))
    new.drafts['S']['tables']['rate']['below_first']='first'
    assert not semantic_gates(new)
    from bonus_core.dsl.models import TierTable
    assert TierTable(mode='excess',steps=[(0,'.1')]).below_first=='zero'


def test_backtracking_keeps_actual_tool_history(tmp_path):
    ws=prepared(make_workspace(tmp_path));out=tmp_path/'out'
    case.save_case(out,{'rounds':[{'n':1,'at':'2026-11-17','files':case.fingerprint(ws.inv.files),'status':'FAILED'}],
                        'workspace':case.snapshot(ws)})
    fake=FakeLLM([{'tool':'read_workspace','args':{'section':'mapping'}},
        {'tool':'set_rules','args':{'scheme':'S','report':{'people':'unknown_person','totals':[]}}},
        {'tool':'note','args':{'topic':'end','text':'stop'}}])
    fake.workflow_mode='staged'
    ar=run_agent(fake,ws.inv.files,out,max_steps=3,reflect=0)
    assert any(s.tool=='（恢复当前任务）' for s in ar.steps)
    final_messages=fake.calls[-1]
    assert any(tc['function']['name']=='read_workspace' for m in final_messages
               for tc in m.get('tool_calls',[]))
