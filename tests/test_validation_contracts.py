"""Adversarial validation tests: real gates, no global fixture bypasses."""
import copy
from decimal import Decimal as D
from types import SimpleNamespace as NS

import openpyxl
import pytest
from hypothesis import given, settings, strategies as st

from bonus_agent.agent import Workspace, ToolError
from bonus_agent.auto import build_inventory
from bonus_agent.facts import Fact, binding_errors
from bonus_agent.llm import FakeLLM
from bonus_agent.reread import _disagree, calc_gaps
from bonus_agent.validation import Status, compare_values, result_integrity
from bonus_core.data.period import Period
from bonus_core.data.table import from_records
from bonus_core.dsl.models import RuleSet
from bonus_core.engine.issues import EngineError
from bonus_core.engine.run import Engine

pytestmark = [pytest.mark.verify, pytest.mark.reread, pytest.mark.evidence]


def calculate(expr="amount", records=None, tables=None, report=None, period="2026-09"):
    rs = RuleSet.model_validate({"name": "test", "version": "1", "datasets": {
        "项目": {"key": "pid", "fields": {"pid": {"type": "id"}, "amount": {"type": "number"},
            "budget": {"type": "number"}, "region": {"type": "text"}}}},
        "tables": tables or {}, "pool": {"expr": expr, "scope": "项目", "negative": "allow"},
        "allocation": [{"level": "项目", "key": "pid"}], "report": report})
    data = from_records(rs, {"项目": records or [{"pid": "A/B|01", "amount": 100, "budget": 0, "region": "A"}]})
    return rs, Engine(rs, data, period=Period.parse(period)).compute()


def test_slashes_and_pipes_preserve_pool_coverage():
    rs, res = calculate(records=[{"pid": key, "amount": 100, "budget": 0, "region": "A"}
                                 for key in ["01", "A/B", "C|D", "X/Y:Z/0"]])
    ws = NS(results={"S": {"R0": res}}, _ruleset=lambda _: rs)
    got = Workspace._pool_values(ws, "S")
    assert set(got) == set(res.nodes)-{"全部"}
    assert all(v == 100 for v in got.values())
    del res.traces['项目:A/B/奖金池']
    with pytest.raises(ToolError, match="追踪不完整"):
        Workspace._pool_values(ws, "S")


@pytest.mark.parametrize('expected,actual', [({'A': D(1)}, {}), ({'A': D(1)}, {'B': D(1)}), ({}, {})])
def test_no_comparison_is_not_a_pass(expected, actual):
    assert compare_values('coverage', expected, actual).status == Status.INCOMPLETE


def test_independent_execution_error_is_not_no_difference():
    def crash(*_):
        raise RuntimeError('broken reference')
    ws = NS(results={'S': {}}, untested=set(), _independent_pool=crash)
    with pytest.raises(ValueError, match='执行失败'):
        _disagree(ws, 'S', {})
    ws._independent_pool = lambda *_: {'A': D(1)}
    ws._pool_values = lambda *_: {'B': D(1)}
    with pytest.raises(ValueError, match='集合'):
        _disagree(ws, 'S', {})


@pytest.mark.parametrize('state', [{'error': 'offline'}, {'calc': {'unsettled': {'S': ['奖金池']}}}, {}])
def test_reference_error_unsettled_and_missing_scheme_block(state):
    ws = NS(schemes=['S'], results={'S': {}}, untested=set(), _merged=lambda _: {'pool': {'expr': '1'}},
            _ruleset=lambda _: NS(payout=[]))
    assert calc_gaps(ws, state)


def test_same_rate_in_another_region_is_not_a_binding():
    f = Fact(name='A rate', value='0.3%', file='rules.md', quote='A 0.3%', targets=['tables.A.steps.0.1'])
    wrong = {'S': {'tables': {'A': {'steps': [[0, '0.005']]}, 'B': {'steps': [[0, '0.003']]}}}}
    assert binding_errors([f], wrong)
    wrong['S']['tables']['A']['steps'][0][1] = '0.003'
    assert binding_errors([f], wrong) == []


def test_pointer_binding_handles_lookup_keys_with_punctuation():
    f = Fact(name='rate', value='5%', file='rules.md', quote='5%', targets=['/tables/A~1B/mapping/X.Y~1Z'])
    assert not binding_errors([f], {'S': {'tables': {'A/B': {'mapping': {'X.Y/Z': '.05'}}}}})


def test_final_coefficient_default_does_not_become_full_achievement():
    table = {'R': {'kind': 'tier', 'mode': 'whole', 'steps': [[0, '.5'], [1, '1.05']]}}
    _, r = calculate('amount * tier_ratio(R, amount, budget, 1)', tables=table)
    assert r.pool == '100.00'
    _, r = calculate('amount * tier_ratio(R, amount, budget, 1)',
                     records=[{'pid': 'x', 'amount': 100, 'budget': 100, 'region': 'A'}], tables=table)
    assert r.pool == '105.00'
    _, r = calculate('amount * tier_ratio(R, amount, budget, 1, missing: true)', tables=table)
    assert r.pool == '100.00'


@pytest.mark.parametrize('period,expected', [('2026-01', '1.00'), ('2026-09', '9.00'), ('2027-12', '12.00')])
def test_month_selection_changes_with_period(period, expected):
    _, r = calculate('period_value(1,2,3,4,5,6,7,8,9,10,11,12)', period=period)
    assert r.pool == expected


def test_parameter_table_applicability_is_enforced():
    table = {'R': {'kind': 'tier', 'mode': 'whole', 'steps': [[0, '.05']], 'applies_when': 'region == "B"'}}
    with pytest.raises(EngineError, match='适用条件'):
        calculate('tier(R, amount)', tables=table)


def test_material_backed_balance_catches_missing_component():
    source = [{'doc': 'rules.md', 'quote': '总额=正常+暂扣'}]
    report = {'balances': [{'name': 'money', 'level': '项目', 'total': 'amount',
                            'parts': ['amount * 0.8', 'amount * 0.2'], 'sources': source}]}
    _, r = calculate(report=report)
    assert result_integrity(r).ok
    report['balances'][0]['parts'][1] = 'amount * 0.1'
    with pytest.raises(EngineError, match='不闭合'):
        calculate(report=report)


def test_balance_must_not_silently_skip_invalid_scope():
    report = {'balances': [{'name': 'money', 'level': 'missing', 'total': 'amount', 'parts': ['amount']}]}
    with pytest.raises(EngineError):
        calculate(report=report)


def test_result_conservation_detects_corrupted_person_total():
    _, r = calculate()
    r.people = [{'name': 'x', 'final': '99.00'}]
    assert not result_integrity(r).ok


def material_workspace(tmp_path):
    p = tmp_path / 'rules.md'
    p.write_text('A类首档费率5%，第二档费率2%，第二档下限100万元。', encoding='utf8')
    return Workspace(FakeLLM([]), build_inventory([p]))


def test_compile_from_facts_and_reject_wrong_slot_and_stale_source(tmp_path):
    ws = material_workspace(tmp_path)
    ws.record_facts([{'name': n, 'value': v, 'file': 'rules.md', 'quote': q} for n,v,q in [
        ('first', '5%', '首档费率5%'), ('second', '2%', '第二档费率2%'), ('limit', '100万元', '第二档下限100万元')]])
    ws.compile_table('A', [{'value': 'first'}, {'lower': 'limit', 'value': 'second'}], scheme='S')
    assert ws.drafts['S']['tables']['A']['steps'] == [['0', '0.05'], ['1000000', '0.02']]
    assert ws._binding_gaps() == []
    ws.drafts['S']['tables']['A']['steps'][0][1] = '0.02'
    assert ws._binding_gaps()
    ws.drafts['S']['tables']['A']['steps'][0][1] = '0.05'
    (tmp_path/'rules.md').write_text('changed source', encoding='utf8')
    assert any('版本变化' in g for g in ws._binding_gaps())


def test_guarded_repair_isolated_then_applied_after_full_validation(tmp_path):
    from test_param_tables import case, rules
    from bonus_agent import case as cs
    ws = case(tmp_path/'m')
    rules(ws, 'amount * rate * 2')
    spec = {'tables': {}, 'helpers': {'oc': {'scope': '订单', 'expr': 'round(amount * rate, 2)'},
                                     'pay': {'scope': '个人', 'expr': 'round(sum(oc, of: "订单") * coef, 2)'}}, 'pool': 'pay'}
    ws.reread = {'items': [], 'error': '', 'calc': {'schemes': {'月度': spec}}}
    before = copy.deepcopy(ws.drafts)
    result = ws.preview_repair({'variables': {'oc': {'scope': '订单', 'expr': 'round(amount * rate, 2)'}}}, '规则原文没有额外乘2')
    candidate = result['candidate']
    assert ws.drafts == before and ws.results['月度']['R0'].pool == '164.00'
    with pytest.raises(ToolError, match='尚未通过'):
        ws.apply_repair(candidate)
    for node in ['个人:001', '个人:002']:
        ws.verify_repair(candidate, node, 'round(sum(round(amount * rate, 2), of: "订单") * coef, 2)', '原文按品类费率及计奖系数计算')
    assert ws.apply_repair(candidate)['status'] == 'case_validated'
    assert ws.results['月度']['R0'].pool == '82.00'
    assert cs.snapshot(ws)['repairs'][candidate]['status'] == 'case_validated'
    with pytest.raises(ToolError):
        ws.apply_repair(candidate)


def test_repair_cannot_change_checks_or_inputs(tmp_path):
    ws = material_workspace(tmp_path)
    for parts in [{'checks': []}, {'patches': []}, {'datasets': {}}, {'answers': []}]:
        with pytest.raises(ToolError):
            ws.preview_repair(parts, 'bypass', scheme='S')


@settings(max_examples=30, deadline=None)
@given(st.lists(st.integers(min_value=-100000, max_value=100000), min_size=1, max_size=10))
def test_reordering_rows_does_not_change_money(values):
    rows = [{'pid': str(i), 'amount': v, 'budget': 0, 'region': 'A'} for i,v in enumerate(values)]
    _, left = calculate('round(amount * 0.03, 2)', records=rows)
    _, right = calculate('round(amount * 0.03, 2)', records=list(reversed(rows)))
    assert {k:n.final for k,n in left.nodes.items()} == {k:n.final for k,n in right.nodes.items()}
    assert D(left.pool) == sum((D(v)*D('0.03') for v in values), D(0))


def test_changed_materials_require_a_new_workspace(tmp_path):
    from test_param_tables import case, rules
    ws = case(tmp_path/'m')
    rules(ws, 'amount * rate')
    book = next((tmp_path/'m').glob('*.xlsx'))
    wb = openpyxl.load_workbook(book)
    wb.worksheets[1].cell(2,4).value = 99
    wb.save(book)
    with pytest.raises(ToolError, match='版本不一致'):
        ws.trial_run()


def test_unvalidated_experiences_never_enter_scenario_memory(tmp_path):
    from bonus_agent.scenarios.memory import ScenarioMemory
    memory = ScenarioMemory(tmp_path/'memory.yaml')
    report = {'status':'PASS','fingerprint':'input-and-rule-version'}
    records = {'bad': {'status':'proposed','reason':'guess','report':report},
               'good': {'status':'case_validated','reason':'原文要求最终系数默认值，不是比率默认值','report':report}}
    assert memory.add_repairs(records, version='v1') == 1
    assert memory.items()[0]['status'] == 'case_validated'
    assert '仅原案验证' in memory.brief()


def test_pending_component_is_explicit_and_not_paid():
    report={'people':'region', 'pending':[{'name':'待分配角色','level':'项目','expr':'amount * 0.2',
                                         'sources':[{'doc':'rules.md','quote':'另有20%待分配'}]}]}
    _, result = calculate('amount * 0.8', report=report)
    assert result_integrity(result).ok
    assert result.people[-1]['pending'] is True
    assert result.people[-1]['final'] == '20.00'
    assert result.people[-1]['paid'] == '0.00'


def test_population_can_follow_effective_detail_without_ghost_entities(monkeypatch):
    from bonus_core.data.population import materialize
    rs=RuleSet.model_validate({'name':'test','version':'1','datasets':{
        '对象':{'key':'eid','population_from':'有效明细','fields':{'eid':{'type':'id'}}},
        '有效明细':{'link':'eid','fields':{'eid':{'type':'id'},'amount':{'type':'number'}}}},
        'pool':{'expr':'sum(amount, of: "有效明细")','scope':'对象'},
        'allocation':[{'level':'对象','key':'eid'}]})
    data=from_records(rs,{'对象':[{'eid':'A'},{'eid':'B'}], '有效明细':[{'eid':'A','amount':100}]})
    assert len(materialize(rs,data)['对象'].rows)==1
    result=Engine(rs,data).compute()
    assert set(result.nodes)=={'全部','对象:A'}
    changed=from_records(rs,{'对象':[{'eid':'A'},{'eid':'B'}], '有效明细':[{'eid':'B','amount':100}]})
    monkeypatch.setattr('bonus_core.data.patches.apply_patches',lambda *_:(changed,[]))
    assert set(Engine(rs,data).compute().nodes)=={'全部','对象:B'}


def test_frozen_holdout_rejects_candidate_without_exposing_answers(tmp_path):
    import json
    from test_param_tables import case, rules
    ws=case(tmp_path/'m')
    rules(ws,'amount * rate * 2')
    ws.reread={'items':[], 'calc':{'schemes':{'月度':{
        'helpers':{'oc':{'scope':'订单','expr':'round(amount * rate, 2)'},
                   'pay':{'scope':'个人','expr':'round(sum(oc, of: "订单") * coef, 2)'}},'pool':'pay'}}}}
    book=openpyxl.Workbook();s=book.active;s.append(['工号','期望应得']);s.append(['001',7654321]);s.append(['002',52])
    expected=tmp_path/'expected.xlsx';book.save(expected)
    ws.repair_regressions=[{'id':'private-case','files':[str(next((tmp_path/'m').glob('*.xlsx')))],'expected':str(expected),'period':None}]
    result=ws.preview_repair({'variables':{'oc':{'scope':'订单','expr':'round(amount * rate, 2)'}}},'按原文费率计算')
    check=result['report']['checks'][-1]
    assert check['id']=='repair.holdouts' and check['status']=='FAIL'
    assert '7654321' not in json.dumps(result)
    with pytest.raises(ToolError):
        ws.apply_repair(result['candidate'])


@pytest.mark.parametrize('value',['NaN','Infinity','-Infinity',float('nan')])
def test_nonfinite_amounts_are_rejected(value):
    from bonus_core.data.table import convert, ConvertError
    from bonus_core.dsl.models import FieldDef, to_decimal
    with pytest.raises(ConvertError):
        convert(value, FieldDef(type='money', unit='元'))
    with pytest.raises(ValueError):
        to_decimal(value)


def test_real_finish_requires_evidence_full_reference_and_samples(tmp_path, monkeypatch):
    """Exercise all production finish gates without the unrelated-test bypass fixtures."""
    import json
    from test_param_tables import case, rules
    ws=case(tmp_path/'m')
    quote='订单提成 = 合同额 × 品类提成率，提成率直接读取品类费率表；个人提成 = 订单提成合计 × 计奖系数（读人员表）。'
    ws.record_facts([{'name':'口径','value':'订单提成 = 合同额 × 品类提成率','file':'01_规则.md','quote':quote}])
    rules(ws,'amount * rate')
    ws.record_clauses([{'point':'按原文读取费率与计奖系数','file':'01_规则.md','quote':quote}])
    ws.cover_clauses({'C1':'variables.oc、variables.pay、pool'})
    ws.llm.calc=json.dumps({'schemes':{'月度':{'pool':'v_pay','helpers':{
        'v_order':{'scope':'订单','expr':'round(amount * rate, 2)'},
        'v_pay':{'scope':'个人','expr':'round(sum(v_order, of: "订单") * coef, 2)'}}}}})
    for node in ['个人:001','个人:002']:
        ws.verify(node,'round(sum(round(amount * rate, 2), of: "订单") * coef, 2)',quote)
    assert ws.finish('已核对',review=False)=='已完成'
    assert ws.validation_report()['status']=='PASS'
    monkeypatch.setattr(ws,'_independent_pool',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('checker error')))
    with pytest.raises(ToolError, match='执行失败'):
        ws.finish('不能放行',review=False)


def test_middle_amount_branch_is_selected_even_when_extrema_miss_it():
    tables={name:{'kind':'tier','mode':'whole','steps':[[0,rate]]} for name,rate in [('TA','.01'),('TB','.02'),('TC','.03')]}
    rs,res=calculate('if(region == "C", tier(TC, amount), if(region == "B", tier(TB, amount), tier(TA, amount)))',
        records=[{'pid':k,'amount':a,'budget':0,'region':k} for k,a in [('A',10000),('B',100),('C',1)]],tables=tables)
    ws=Workspace(FakeLLM([]),build_inventory([]))
    ws.datasets=rs.datasets
    ws.drafts={'S':{k:rs.model_dump(mode='json')[k] for k in ['tables','pool','allocation']}}
    ws.results={'S':{'R0':res}}
    assert {n for n,_,_ in ws._verify_targets('S')}=={'项目:A','项目:B','项目:C'}


def test_auxiliary_branch_coverage_follows_structured_dependencies():
    from bonus_agent.validation import covered_nodes
    from bonus_core.engine.evaluator import Trace
    result=NS(nodes={'对象:A/B':object()},traces={
        '辅助:X/Y/v':Trace('辅助:X/Y/v','v','1','1','1',node_id='辅助:X/Y'),
        '对象:A/B/v':Trace('对象:A/B/v','v','other','1','1',inputs=('辅助:X/Y/v',),node_id='对象:A/B')})
    assert covered_nodes(result,['辅助:X/Y'])=={'对象:A/B'}


def test_verification_identity_cannot_collide_on_delimiters():
    from bonus_agent.agent import _verify_key
    from bonus_agent import case
    assert _verify_key('S|X','N','应得') != _verify_key('S','X|N','应得')
    ws=Workspace(FakeLLM([]),build_inventory([]))
    snapshot={'drafts':{'S':{}},'verified':{'S|对象:A|B':{'expr':'1','amount':'应得'}}}
    case.restore(ws,snapshot)
    assert _verify_key('S','对象:A|B','应得') in ws.verified
    snapshot['drafts']['S|对象:A']={}
    case.restore(ws,snapshot)
    assert not ws.verified


def test_frozen_prior_reference_is_checked_even_if_sampling_changes():
    from bonus_agent.repair import _report
    from bonus_agent.agent import _verify_key
    reference={'expr':'1','basis':'source'}
    original=NS(verified={_verify_key('S','old-node','实发'):reference},
                results={'S':{'R0':NS(nodes={'old-node':object()},payout_rows=lambda:[]) }},repair_regressions=[])
    candidate=NS(validation_report=lambda:{'status':'PASS','checks':[]},
                 _verify_mismatch=lambda *args:'reference no longer matches')
    report=_report(original,candidate)
    assert report['status']=='FAIL'
    assert report['checks'][0]['id']=='repair.frozen_references'


def test_nonblocking_question_does_not_waive_numeric_failure(monkeypatch):
    from bonus_agent import reread
    from bonus_agent.extract_rules import Question
    ws=Workspace(FakeLLM([]),build_inventory([]))
    ws.questions=[Question(id='q1',text='方案S奖金池按哪种计算',related='复读 奖金池 S',blocking=False)]
    monkeypatch.setattr(reread,'run',lambda _: {})
    monkeypatch.setattr(reread,'compare',lambda *args: [])
    monkeypatch.setattr(reread,'calc_gaps',lambda *args: [('复读 奖金池 S','amounts differ')])
    assert ws._reread_gaps({'复读 奖金池 S'})
    ws.questions[0].answer='已确认'
    assert ws._reread_gaps({'复读 奖金池 S'})
