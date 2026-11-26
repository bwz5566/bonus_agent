import json
from types import SimpleNamespace

import pytest
import yaml

from bonus_agent.agent import run_agent
from bonus_agent.construction import TaskBoard
from bonus_agent.llm import DEFAULT_CONFIG, FakeLLM, LLM
from bonus_agent.model_repair import REPAIR_SCHEMA, _packet
from bonus_agent.recovery import RunBudget
from tests.test_staged_workflow import make_workspace, save_staged_case


@pytest.mark.parametrize('valid', [True, False])
def test_construction_failure_routes_once_then_returns_to_primary(tmp_path, valid):
    ws = make_workspace(tmp_path)
    quote = ws.clauses[0].quote
    ws.plan_formula_tasks([
        {'id': 'source', 'scheme': 'S', 'target': '/variables/sales_basis', 'expected_scope': '项目', 'clauses': ['C1']},
        {'id': 'later', 'scheme': 'S', 'target': '/variables/later_check', 'depends_on': ['source']},
        {'id': 'pool', 'scheme': 'S', 'target': '/pool', 'depends_on': ['later']},
    ])
    assert ws.prepare_data()['status'] == 'PASS'
    out = tmp_path / 'case'
    save_staged_case(ws, out)
    failed = {'tool': 'submit_formula_task', 'args': {'task': 'source', 'value': {
        'scope': '项目', 'expr': 'undefined_field', 'sources': [{'doc': 'policy.md', 'quote': quote}],
    }}}
    later = {'tool': 'read_workspace', 'args': {'section': 'construction', 'name': 'later'}}
    main = FakeLLM([failed, later, failed], name='deepseek-flash')
    main.workflow_mode = 'staged'
    candidate = {'value': {'scope': '项目', 'expr': 'amount' if valid else 'other_missing_field',
                           'sources': [{'doc': 'policy.md', 'quote': quote}]}}
    repair = FakeLLM([json.dumps({
        'diagnosis': {'item': '项目销售额', 'identified': '规则按项目使用销售金额，当前式引用未注册字段。',
                      'missing': '项目数据映射中的金额字段。', 'impact': '项目销售额公式无法计算。'},
        'repair': {'formulas': [{'task': 'source', **candidate}]},
    }, ensure_ascii=False)], name='kimi-k3.1-pro')
    main.repair_llm = repair

    result = run_agent(main, ws.inv.files, out, max_steps=2 if valid else 3)
    assert [json.loads(call[-1]['content'])['failed_task']['id'] for call in repair.calls] == ['source']
    packet = json.loads(repair.calls[0][-1]['content'])
    assert 'undefined_field' in packet['failure'] and quote in {c['quote'] for c in packet['verified_clauses']}
    assert packet['scope_source'] == {'scope': '项目', 'dataset': 'projects', 'key': 'pid'}
    assert any(d['dataset'] == 'projects' and d['fields']['amount']['column'] == 'amount' for d in packet['datasets'])
    assert '- sum:' in packet['dsl_functions'] and '- tier_ratio:' in packet['dsl_functions']
    model_steps = [s for s in result.steps if s.args.get('tool') == 'model_repair']
    assert len(model_steps) == 1 and model_steps[0].ok is valid
    assert model_steps[0].args['model'] == 'kimi-k3.1-pro'
    assert any(s.args.get('name') == 'later' for s in result.steps[result.steps.index(model_steps[0]) + 1:])
    assert (TaskBoard(result.ws).statuses()['source']['status'] == 'PASS') is valid
    assert ('sales_basis' in result.ws._ruleset('S').variables) is valid


@pytest.mark.parametrize('valid', [True, False])
def test_parameter_table_failure_gets_a_verified_kimi_handoff(tmp_path, valid):
    ws = make_workspace(tmp_path)
    ws.plan_formula_tasks([
        {'id': 'rate', 'scheme': 'S', 'target': '/tables/rate', 'facts': ['提成率']},
        {'id': 'pool', 'scheme': 'S', 'target': '/pool', 'depends_on': ['rate']},
    ])
    ws.set_rules(scheme='S', tables={'rate': {'kind': 'lookup', 'mapping': {'A': '0.1', 'B': '0.1'}}})
    ws.bind_facts([{'name': '提成率', 'targets': ['S::/tables/rate/mapping/A']}])
    assert ws.prepare_data()['status'] == 'PASS'
    out = tmp_path / 'case'
    save_staged_case(ws, out)
    failed = {'tool': 'submit_formula_task', 'args': {'task': 'rate', 'changes': {'applies_when': ''}}}
    read = {'tool': 'read_workspace', 'args': {'section': 'construction', 'name': 'pool'}}
    main = FakeLLM([failed, read], name='deepseek-flash')
    main.workflow_mode = 'staged'
    main.recovery_mode = 'supervisor'
    binding = {'name': '提成率', 'targets': ['S::/tables/rate/mapping/A', 'S::/tables/rate/mapping/B']}
    change = {'applies_when': ''} if valid else {'mapping': {'A': '0.1', 'B': '0.2'}}
    main.repair_llm = FakeLLM([json.dumps({
        'diagnosis': {'item': '费率表', 'identified': '原文提成率为10%，B项尚无绑定。',
                      'missing': 'B项应补相同条款的绑定。', 'impact': '当前费率表未通过验证。'},
        'repair': {'formulas': [{'task': 'rate', 'changes': change, 'bindings': [binding]}],
                   'datasets': [], 'tiers': []},
    }, ensure_ascii=False)], name='kimi-k3.1-pro')
    result = run_agent(main, ws.inv.files, out, max_steps=2)
    handoff = next(s for s in result.steps if s.args.get('tool') == 'model_repair')
    assert handoff.ok == valid and handoff.args['task'] == 'rate'
    assert len(main.repair_llm.calls) == 1
    packet = json.loads(main.repair_llm.calls[0][-1]['content'])
    assert packet['failed_task']['target'] == '/tables/rate'
    assert any(p['name'] == '提成率' and p['file'] == 'policy.md' for p in packet['registered_parameters'])
    assert any(s.args.get('name') == 'pool' for s in result.steps[result.steps.index(handoff) + 1:])
    assert (TaskBoard(result.ws).statuses()['rate']['status'] == 'PASS') == valid


@pytest.mark.parametrize('preserve_distribution', [True, False])
def test_kimi_report_repair_uses_named_outputs_and_preserves_financial_phase(tmp_path, preserve_distribution):
    import copy

    from tests.test_output_obligations import output_map, update_plan

    ws = make_workspace(tmp_path)
    targets = {**output_map(), '项目底稿': ['S::/report/details/原始销售额']}
    update_plan(ws, ['S'], targets)
    ws.plan_formula_tasks([
        {'id': 'pool', 'scheme': 'S', 'target': '/pool', 'clauses': ['C1']},
        {'id': 'deliver', 'scheme': 'S', 'target': '/report', 'depends_on': ['pool']},
    ])
    assert ws.prepare_data()['status'] == 'PASS'
    assert ws.check_formula_task('pool')['status'] == 'PASS'
    report = copy.deepcopy(ws.drafts['S']['report'])
    failed = {'tool': 'submit_formula_task', 'args': {'task': 'deliver', 'changes': {'totals': report['totals']}}}
    details = [*report['details'], {'name': '原始销售额', 'level': '项目', 'expr': 'amount',
                                  'sources': [{'doc': 'policy.md', 'quote': ws.clauses[0].quote}]}]
    changes = {'details': details} if preserve_distribution else {'details': details, 'people': 'other_field'}
    main = FakeLLM([failed], name='deepseek-flash')
    main.workflow_mode = 'staged'
    main.repair_llm = FakeLLM([json.dumps({
        'diagnosis': {'item': '项目底稿', 'identified': '项目原始销售额字段已映射。',
                      'missing': '缺少名为原始销售额的项目明细行。', 'impact': '项目底稿未完成。'},
        'repair': {'formulas': [{'task': 'deliver', 'changes': changes}], 'datasets': [], 'tiers': []},
    }, ensure_ascii=False)], name='kimi-k3.1-pro')
    out = tmp_path / 'case'
    save_staged_case(ws, out)
    ar = run_agent(main, ws.inv.files, out, max_steps=1)
    handoff = next(s for s in ar.steps if s.args.get('tool') == 'model_repair')
    assert handoff.ok == preserve_distribution
    packet = json.loads(main.repair_llm.calls[0][-1]['content'])
    assert packet['report_phase'] == 'basis'
    assert [row['target'] for row in packet['required_outputs']] == ['/report/details/原始销售额']
    assert ar.ws.drafts['S']['report']['people'] == report['people']
    assert not ar.ws._flow().current('approved:S') and not ar.ws.results
    assert (TaskBoard(ar.ws).statuses()['deliver']['status'] == 'PASS') == preserve_distribution


def test_kimi_protocol_and_budget_are_bound_to_the_repair_route(monkeypatch):
    monkeypatch.setenv('MOONSHOT_API_KEY', 'test-repair-access')
    primary = LLM.from_config('deepseek-flash', yaml.safe_load(DEFAULT_CONFIG.read_text(encoding='utf-8')))
    repair = primary.repair_llm
    budget = RunBudget(0, 20_000).bind(primary)
    assert repair.model == 'kimi-k3.1-pro' and repair.run_budget is budget
    assert primary.usage is repair.usage and primary.checker_llm.model == 'deepseek-v4-pro'

    def reply(kwargs, think):
        assert think and kwargs['temperature'] == 1.0
        assert kwargs['extra_body'] == {'reasoning_effort': 'high'}
        assert kwargs['response_format']['type'] == 'json_schema'
        assert kwargs['response_format']['json_schema']['schema'] == REPAIR_SCHEMA
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='{"ready":true}'))])

    monkeypatch.setattr(repair, '_send', reply)
    assert repair.chat_json([{'role': 'user', 'content': 'Return JSON.'}], schema=REPAIR_SCHEMA, think=True) == {'ready': True}


def test_unsaved_formula_shares_only_verified_sources_and_can_bind_the_right_parameter(tmp_path):
    quote = '销售金额超过37元时按原额计入。'
    ws = make_workspace(tmp_path, policy_tail='\n\n' + quote)
    ws.record_clauses([{'file': 'policy.md', 'point': '金额门槛', 'quote': quote}])
    ws.record_facts([{'name': '金额门槛', 'file': 'policy.md', 'quote': quote, 'value': '37元'}])
    ws.plan_formula_tasks([{'id': 'amount', 'scheme': 'S', 'target': '/variables/filtered_amount',
                            'clauses': ['C1'], 'expected_scope': '项目'}])
    assert ws.prepare_data()['status'] == 'PASS'
    board = TaskBoard(ws)
    value = {'scope': '项目', 'expr': 'if(amount > 37, amount, 0)', 'sources': [
        {'doc': 'policy.md', 'quote': quote}, {'doc': 'policy.md', 'quote': '次月结算前先乘0.2。'}]}
    packet = _packet(ws, board, board.task_spec('amount'), board.statuses(with_confidence=False),
                     'construction.constant', {'task': 'amount', 'value': value})
    assert packet['value'] is None and packet['failed_attempt']['expr'] == value['expr']
    assert packet['failed_attempt']['sources'] == [value['sources'][0]]
    assert [c['id'] for c in packet['verified_clauses']] == ['C2', 'C1']
    assert [(f['name'], f['value']) for f in packet['registered_parameters']] == [('金额门槛', '37元')]

    without_source = {**value, 'sources': []}
    assert not ws.submit_formula_task('amount', value=without_source)['saved']
    report = ws.submit_formula_task('amount', value={**value, 'sources': [value['sources'][0]]})
    assert report['saved'], report
    fact = next(f for f in ws.facts if f.name == '金额门槛')
    assert fact.targets == ['S::/variables/filtered_amount/expr']
