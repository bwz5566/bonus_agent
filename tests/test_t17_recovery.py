"""T17 regressions: binding lifecycle, typed evidence, bounded diagnostics and pre-amount progress."""
import copy
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bonus_agent import facts as fx
from bonus_agent.agent import Step, ToolError, _deep_reason, run_agent
from bonus_agent.context import workspace_view
from bonus_agent.llm import FakeLLM
from bonus_agent.lint import compile_warnings, static_gates
from bonus_agent.source_cells import parameter_cell
from bonus_core.dsl.models import RuleSet
from bonus_core.engine.evaluator import Evaluator
from bonus_core.engine.tree import Node, ROOT_LEVEL
from tests.test_staged_workflow import make_workspace, prepared
from tests.test_review_fixes_round5 import _tier_ppt

pytestmark = pytest.mark.evidence  # Do not use the global fixture that bypasses real parameter checks.


def test_deleting_objects_detaches_only_their_bindings(tmp_path):
    ws = make_workspace(tmp_path)
    ws.set_rules(scheme='S', variables={'old': {'expr': '0.1'}, 'older': {'expr': '0.1'}})
    f = ws.facts[0]
    f.targets += ['S::/variables/old/expr', 'S::/variables/older/expr', 'other::/variables/old/expr']
    ws.set_rules(scheme='S', variables={'old': None})
    assert 'S::/variables/old/expr' not in f.targets
    assert 'S::/variables/older/expr' in f.targets
    assert 'other::/variables/old/expr' in f.targets
    assert f.value == '10%'


def test_unqualified_bindings_survive_in_other_schemes():
    f = fx.Fact(name='rate', value='10%', file='policy', quote='10%', targets=['/variables/rate/expr'])
    drafts = {'A': {'variables': {}}, 'B': {'variables': {'rate': {'expr': '0.1'}}}}
    fx.detach_targets([f], 'A', {('variables', 'rate')}, drafts)
    assert f.targets == ['B::/variables/rate/expr']


def test_old_orphan_cleanup_requires_a_valid_surviving_binding(tmp_path):
    ws = make_workspace(tmp_path)
    f = ws.facts[0]
    f.targets += ['S::/tables/deleted/steps/0/1', 'S::/report/details/99/expr']
    report = ws.validate_parameters()
    assert report['repaired'] and not any('/tables/deleted/' in x for x in f.targets)
    assert 'S::/report/details/99/expr' in f.targets  # an invalid index in an existing object must still fail
    f.targets = ['S::/tables/only_missing/steps/0/1']
    assert not ws.validate_parameters()['repaired']
    assert f.targets  # never erase a parameter's only binding to pretend success


def test_rule_write_and_bindings_commit_together(tmp_path):
    ws = make_workspace(tmp_path)
    before = copy.deepcopy(ws.drafts)
    facts = [f.model_dump() for f in ws.facts]
    with pytest.raises(ToolError, match='绑定未生效'):
        ws.set_rules(scheme='S', variables={'rate': {'expr': '0.2'}},
                     bindings=[{'name': '提成率', 'targets': ['S::/variables/rate/expr']}])
    assert ws.drafts == before and [f.model_dump() for f in ws.facts] == facts
    ws.set_rules(scheme='S', variables={'rate': {'expr': '0.1'}},
                 bindings=[{'name': '提成率', 'targets': ws.facts[0].targets + ['S::/variables/rate/expr']}])
    assert 'S::/variables/rate/expr' in ws.facts[0].targets
    assert not ws._binding_gaps()
    assert ws.set_rules(scheme='S', bindings=[{'name': '提成率', 'targets': ws.facts[0].targets}]).startswith('no-op')


def test_bad_tier_order_never_creates_rule_or_bindings(tmp_path):
    ws, filename = _tier_ppt(tmp_path)
    ws.record_parameter_cells(filename, 1, 1, [{'row': r, 'column': c} for r in (2, 3, 4) for c in (1, 2, 3)], applies_to='北区')
    drafts, facts = copy.deepcopy(ws.drafts), [f.model_dump() for f in ws.facts]
    with pytest.raises(ToolError, match='parameter.order'):
        ws.compile_table('bad', [{'lower': 'R2C2', 'value': 'R2C3'}, {'lower': 'R3C2', 'value': 'R3C3'}],
                         cells_from={'file': filename, 'page': 1, 'table': 1})
    assert ws.drafts == drafts and [f.model_dump() for f in ws.facts] == facts


def test_compiled_interval_cell_is_reused_without_resetting_its_projection(tmp_path):
    ws, filename = _tier_ppt(tmp_path)
    ws.record_parameter_cells(filename, 1, 1, [{'row': r, 'column': c} for r in (2, 3, 4) for c in (1, 2)], applies_to='北区')
    ws.compile_table('rate', [{'value':'R4C2'}, {'lower':'R3C1','value':'R3C2'}, {'lower':'R2C1','value':'R2C2'}],
                     cells_from={'file':filename,'page':1,'table':1})
    before = [f.model_dump() for f in ws.facts]
    reply = ws.record_parameter_cells(filename, 1, 1, [{'row':3,'column':1}], applies_to='北区')
    assert reply['facts'][0]['reused'] and reply['facts'][0]['numeric'] == '10000000'
    assert [f.model_dump() for f in ws.facts] == before


@pytest.mark.parametrize('text,lower,upper', [
    ('15%-20%（含15%）', '0.15', '0.20'), ('5% (含) -10%', '0.05', '0.10'),
    ('5-10%（含5%）', '0.05', '0.10'), ('200-500万', '2000000', '5000000'),
    ('25 – 50% (含)', '0.25', '0.50'),
])
def test_annotated_bands_keep_full_source_and_exact_endpoints(tmp_path, text, lower, upper):
    assert fx.interval(text) == (Decimal(lower), Decimal(upper))
    from pptx import Presentation
    from pptx.util import Inches
    path = tmp_path / 'p.pptx'; prs = Presentation(); slide = prs.slides.add_slide(prs.slide_layouts[6])
    table = slide.shapes.add_table(2, 2, Inches(0), Inches(0), Inches(5), Inches(2)).table
    table.cell(1, 0).text = text; prs.save(path)
    cell = parameter_cell(path, 1, 1, 2, 1)
    assert cell['value'] == text and text in cell['quote']
    assert fx.check_fact(fx.Fact(name='band', file=path.name, quote=text, value=text), {path.name: fx.norm(text)}) is None


def test_inclusion_annotation_does_not_hide_an_unrelated_value():
    assert fx.interval('5-10%（含7%）') is None


@pytest.mark.parametrize('quote,value,valid', [('计算基数=金额/汇率*(汇率-0.05)', '0.05', True),
    ('费用=-0.05', '0.05', False), ('费用=-0.05', '-0.05', True), ('净额=收入-10%', '10%', True),
    ('亏损-0.05元', '0.05元', False)])
def test_formula_operator_is_not_a_negative_literal(quote,value,valid):
    f = fx.Fact(name='parameter', value=value, file='policy', quote=quote)
    assert (fx.check_fact(f, {'policy':fx.norm(quote)}) is None) is valid


def test_old_invalid_literal_can_be_corrected_but_valid_conflicting_values_cannot(tmp_path):
    from bonus_agent.agent import Workspace
    from bonus_agent.auto import build_inventory
    p=tmp_path/'source.md'; p.write_text('净额=收入-0.05；可选费率10%或20%。',encoding='utf8')
    ws=Workspace(FakeLLM([]),build_inventory([p]))
    ws.record_facts([{'name':'扣减额','file':p.name,'quote':'净额=收入-0.05','value':'0.05'}])
    ws.facts[0].value='-0.05'  # saved by the previous signed-token reader
    ws.facts[0].targets=['S::/variables/wrong/expr']
    assert any('parameter.source_literal' in g for g in ws._binding_gaps())
    ws.record_facts([{'name':'扣减额','file':p.name,'quote':'净额=收入-0.05','value':'0.05'}])
    assert ws.facts[0].value=='0.05' and not ws.facts[0].targets
    ws.record_facts([{'name':'费率','file':p.name,'quote':'可选费率10%或20%','value':'10%'}])
    with pytest.raises(ToolError,match='多个不同值'):
        ws.record_facts([{'name':'费率','file':p.name,'quote':'可选费率10%或20%','value':'20%'}])


def test_unit_change_needs_scoped_source_and_is_not_guessed(tmp_path):
    ws = make_workspace(tmp_path)
    p = tmp_path / 'units.md'; p.write_text('乙表全部数字按百分数。甲表全部数字按百分数。甲表分成比例25。', encoding='utf8')
    # Use a fresh inventory so all unit evidence has immutable source fingerprints.
    from bonus_agent.agent import Workspace
    from bonus_agent.auto import build_inventory
    ws = Workspace(FakeLLM([]), build_inventory([p]))
    ws.record_facts([{'name': '甲表比例', 'file': p.name, 'value': '25', 'quote': '甲表分成比例25', 'context': '甲表'}])
    ws.set_rules(scheme='S', variables={'rate': {'expr': '0.25'}})
    assert 'parameter.unit' in '\n'.join(ws._binding_gaps())
    spec = {'name': '甲表比例', 'targets': ['S::/variables/rate/expr'], 'unit': '%'}
    with pytest.raises(ToolError, match='parameter.unit'):
        ws.bind_facts([{**spec, 'unit_basis': {'file': p.name, 'quote': '乙表全部数字按百分数。'}}])
    ws.bind_facts([{**spec, 'unit_basis': {'file': p.name, 'quote': '甲表全部数字按百分数。'}}])
    assert fx.numeric(ws.facts[0]) == Decimal('0.25') and not ws._binding_gaps()
    assert ws.facts[0].value == '25' and ws.facts[0].unit_basis['source_hash']
    assert not ws.llm.calls


def test_compile_prioritizes_structure_and_retains_all_diagnostics(tmp_path):
    ws = prepared(make_workspace(tmp_path))
    pool = ws.drafts['S']['pool']
    ws.set_rules(scheme='S', variables={'broken': {'scope': '项目', 'expr': 'missing_field'}},
                 pool={**pool, 'expr': pool['expr'] + ' + broken'})
    for n in range(80):
        ws.facts.append(ws.facts[0].model_copy(update={'name': f'pending{n}', 'targets': []}))
    result = ws.compile_formulas()
    assert result['status'] == 'FAIL' and result['issue_count'] > 80
    assert 'missing_field' in result['issues'][0]
    assert len(json.dumps(result, ensure_ascii=False)) < 6000
    seen = []
    args = {'section': 'checks', 'limit': 6}
    while True:
        page = ws.read_workspace(**args)
        seen += [i['message'] for i in page['items'] if i['kind'] == 'issue']
        if not page['next_call']: break
        args = page['next_call']['args']
    assert seen == ws.workflow['formula_report']['report']['issues']
    ws.set_rules(scheme='S', variables={'broken': None}, pool=pool)
    stale = ws.read_workspace('checks')
    assert stale['status'] == 'STALE' and stale['next_call']['tool'] == 'compile_formulas'


def test_long_tool_feedback_is_complete_valid_json_and_pageable_without_rerunning(tmp_path):
    from bonus_agent.context import tool_result
    ws = make_workspace(tmp_path)
    original = {'status':'PARTIAL', 'facts':[{'name':f'cell{i}','value':'25','quote':'原文'*120} for i in range(80)],
                'failed':['未登记下界，按坐标补充']}
    shown = json.loads(tool_result(ws, 'record_parameter_cells', original))
    assert shown['status'] == 'PARTIAL' and shown['counts']['facts'] == 80
    args = shown['next_call']['args']; recovered = []
    while True:
        page = ws.read_workspace(**args)
        recovered += page['items']
        if not page['next_call']: break
        args = page['next_call']['args']
    assert [i['value'] for i in recovered if i['field']=='facts'] == original['facts']
    assert [i['value'] for i in recovered if i['field']=='failed'] == original['failed']


def test_fragment_size_uses_escaped_envelope_and_invalid_offset_has_recovery():
    ws = SimpleNamespace(schemes=['S'], _merged=lambda s: {'payload': ('\\"\n'*9000)}, _scheme=lambda s:s)
    args = {'section': 'rules'}; parts = []
    while True:
        page = workspace_view(ws, **args)
        assert len(json.dumps(page, ensure_ascii=False)) < 6000
        parts.append(page['item_fragment']['text'])
        if not page['next_call']: break
        args = page['next_call']['args']
    assert json.loads(''.join(parts))['rule'] == ws._merged('S')
    invalid = workspace_view(ws, 'rules', detail_offset=999999)
    assert invalid['status'] == 'INVALID_OFFSET' and invalid['next_call']['args']['detail_offset'] == 0


@pytest.mark.parametrize('a,b,expected', [('2024-02-29', '2025-08-01',18), ('2024-08-31','2026-03-01',19), ('2026-03-31','2026-02-28',-1)])
def test_calendar_month_difference_does_not_depend_on_day(a,b,expected):
    rs = RuleSet.model_validate({'name':'test','version':'1','datasets':{},'allocation':[],'pool':{'expr':'0'}})
    ev = Evaluator(rs, Node(ROOT_LEVEL, ROOT_LEVEL, None), {})
    assert ev.evaluate(f'months_between("{a}", "{b}")', ev.root, 'test','test') == expected


def test_wrapped_month_count_warns_and_month_day_mismatch_blocks():
    rs = RuleSet.model_validate({'name':'test','version':'1','datasets':{'entries':{'period':'date', 'fields':{'date':{'type':'date'}}}},
        'allocation':[], 'pool':{'expr':'mean'}, 'variables':{
            'mean':{'expr':'count_distinct(month(date), of: "entries")'},
            'age':{'expr':'if(days_between("2024-01-01", "2026-01-01") > 548, 1, 0)', 'desc':'月份相差大于18个月'}}})
    assert any('最多为1' in w for w in compile_warnings(rs))
    assert any('日期量纲' in k for k,_ in static_gates(rs))


def test_pre_amount_frontier_tracks_actual_repair_not_unrelated_writes(tmp_path):
    ws = prepared(make_workspace(tmp_path))
    ws.facts[0].targets.append('S::/tables/gone/steps/0/1')
    before = ws.blocker_frontier()
    assert before and not ws.results
    ws.notes['unrelated'] = '写了笔记'
    assert ws.blocker_frontier() == before
    ws.validate_parameters()
    assert not ws.blocker_frontier()
    ws.facts[0].targets = []
    before = ws.blocker_frontier()
    ws.set_rules(scheme='S', variables={'unrelated':{'expr':'0.1'}})
    assert ws.blocker_frontier() == before  # changing a numeric candidate suggestion is not resolution


def test_legacy_pre_amount_report_and_source_cache_remain_usable(tmp_path, monkeypatch):
    from bonus_agent.agent import CHAT_SOURCE
    ws = prepared(make_workspace(tmp_path))
    ws.workflow.pop('formula_report')  # old cases had only a data report
    ws.facts[0].targets = []
    assert ws.blocker_frontier()
    ws.__dict__.pop('_source_corpus_cache', None)
    original = ws._lines
    calls = []
    monkeypatch.setattr(ws, '_lines', lambda name: calls.append(name) or original(name))
    ws._corpus(); n = len(calls)
    ws._corpus()
    assert len(calls) == n
    ws.said = '新增财务确认：本表按百分数。'
    assert CHAT_SOURCE in ws._corpus() and len(calls) > n


def test_native_errors_and_saved_drafts_do_not_call_optional_pro(tmp_path):
    ws = make_workspace(tmp_path)
    for tool, result in [('compile_table','错误：parameter.ref：R1C1未登记'),
                         ('define_dataset','错误：combine 的列不在表头'),
                         ('set_rules','草稿已保存，校验未通过；尚不能计算金额。')]:
        assert _deep_reason([Step(tool, {}, result, False)], 0, ws, {tool:1}) == ''


def test_advisor_packet_remains_valid_json_with_tool_contracts():
    from bonus_agent.recovery import local_advice
    seen = []
    fake = FakeLLM(lambda messages: seen.append(messages) or json.dumps({
        'diagnosis':'error', 'next_action':'read_workspace', 'evidence_needed':'source', 'acceptance':'check'}))
    local_advice(fake, 'problem', [Step('set_rules', {'huge':'\\"'*50000}, 'bad', False)], [], 'inspect')
    packet = json.loads(seen[0][-1]['content'])
    assert len(seen[0][-1]['content']) <= 18000
    assert packet['tool_contracts'] and packet['constraints'] and packet['recent_is_excerpt']


def test_runner_marks_saved_invalid_rules_as_failed_validation(tmp_path):
    p = tmp_path/'policy.md'; p.write_text('奖金按规则计算',encoding='utf8')
    ar = run_agent(FakeLLM([{'tool':'set_rules','args':{'variables':{'x':{'expr':'missing_field'}}}}]),
                   [p], tmp_path/'out', max_steps=1, reflect=0)
    step = next(s for s in ar.steps if s.tool == 'set_rules')
    assert not step.ok and step.result.startswith('草稿已保存，校验未通过')
    assert ar.ws.drafts and not any(s.deep for s in ar.steps)


def test_partial_native_registration_is_not_reported_as_a_complete_pass(tmp_path):
    ws, filename = _tier_ppt(tmp_path)
    script = [{'tool':'record_parameter_cells','args':{'file':filename,'page':1,'table':1,'applies_to':'北区',
                'cells':[{'row':2,'column':2},{'row':1,'column':1}]}}]
    ar = run_agent(FakeLLM(script), [tmp_path/filename], tmp_path/'out', max_steps=1, reflect=0)
    step = next(s for s in ar.steps if s.tool=='record_parameter_cells')
    assert not step.ok and len(ar.ws.facts)==1 and not any(s.deep for s in ar.steps)
