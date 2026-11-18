"""Progress means a covered, stable compiler obligation was actually resolved."""
import copy
import json

import pytest

from bonus_agent.construction import TaskBoard
from bonus_agent.agent import ToolError
from bonus_agent.progress import ProgressWatch, compile_objects
from tests.test_staged_workflow import make_workspace


def report(issues, **extra):
    return {'status': 'FAIL' if issues else 'PASS', 'phase': 'basis', 'issues': issues,
            'static_checks_complete': True, 'preflight': {}, **extra}


def observe(watch, value, facts=('rate',), blockers=('compile_formulas',)):
    return watch.observe(set(blockers), compile_report=value, fact_names=facts)


def test_stable_objects_ignore_titles_error_wording_and_counts():
    before = report(['C14「旧标题」：variables.x 不存在', 'rate：parameter.unit，单位不明',
                     '[阻断] expression_type: 错误 (variables.x.expr)', '其他错误甲', '其他错误乙'])
    after = report(['C14「新标题」：clause.phase_reference，另一种说明', 'rate：parameter.binding，目标不对',
                    '[阻断] expression_type: 换了建议 (variables.x.expr)', '改写后只剩一条'])
    assert compile_objects(before, ['rate']) == compile_objects(after, ['rate'])
    watch = ProgressWatch()
    assert observe(watch, before) == 0
    for i in range(12):
        assert observe(watch, after) == i + 1
    assert watch.stagnant == 12


def test_covered_object_resolution_counts_even_when_total_error_count_does_not_fall():
    watch = ProgressWatch()
    observe(watch, report(['C1「规则一」：缺口', 'rate：缺口']))
    assert observe(watch, report(['C1「规则一」：改写', 'rate：另一个错误'])) == 1
    # C1 was covered and resolved, while a different real clause now fails.
    assert observe(watch, report(['C2「规则二」：缺口', 'rate：仍有缺口'])) == 0
    assert 'compile:basis:clause:C1' in watch.compile_state['resolved']


def test_unknown_prose_and_truncated_reports_cannot_release_known_obligations():
    watch = ProgressWatch()
    observe(watch, report(['rate：缺口', 'C1「规则」：缺口']))
    assert observe(watch, report(['rate：缺口'], issue_count=2)) == 1
    assert 'compile:basis:clause:C1' in watch.compile_state['objects']
    assert observe(watch, report(['workflow.gate：先准备数据'], static_checks_complete=False)) == 2
    assert 'compile:basis:fact:rate' in watch.compile_state['objects']
    assert observe(watch, None, blockers=()) == 3  # a stale report is not resolution


def test_same_objects_and_earned_resolution_survive_resume():
    watch = ProgressWatch()
    observe(watch, report(['rate：缺口', 'C1「规则」：缺口']))
    observe(watch, report(['rate：缺口']))
    for _ in range(3):
        observe(watch, report(['rate：改写后的缺口']))
    saved = json.loads(json.dumps({'previous': sorted(watch.previous), 'stagnant': watch.stagnant,
                                  'compile': watch.compile_state}))
    resumed = ProgressWatch(set(saved['previous']), saved['stagnant'], saved['compile'])
    assert observe(resumed, report(['rate：仍未修复'])) == 4
    # Reintroducing and then fixing C1 cannot repeatedly mint progress.
    assert observe(resumed, report(['rate：缺口', 'C1「换标题」：再次出现'])) == 5
    assert observe(resumed, report(['rate：缺口'])) == 6


def test_last_compiler_object_cannot_renew_credit_through_empty_frontier():
    watch = ProgressWatch()
    observe(watch, report(['rate：缺口']))
    assert observe(watch, report([]), blockers=()) == 0  # first genuine resolution
    for cycle in range(1, 4):
        assert observe(watch, report(['rate：换一种描述'])) == cycle
        assert observe(watch, report([]), blockers=()) == cycle
        # New stage work with no blockers does not consume or replenish credit.
        for _ in range(3):
            assert observe(watch, report([]), blockers=()) == cycle
    # A different, covered problem can still establish new real progress.
    assert observe(watch, report(['C2「新规则」：缺口'])) == 4
    assert observe(watch, report([]), blockers=()) == 0


def test_structured_code_and_target_identity_is_explicit():
    a = report([{'code': 'binding', 'target': '/variables/a', 'message': 'old'},
                {'code': 'binding', 'target': '/variables/b', 'message': 'old'}])
    b = report([{'code': 'binding', 'target': '/variables/a', 'message': 'new'}])
    assert len(compile_objects(a)) == 2
    watch = ProgressWatch()
    observe(watch, a)
    assert observe(watch, b) == 0


def test_canonical_contract_prefix_tracks_each_factor_without_hashing_counterexample():
    a = '方案「S」 variables.a：ratio_default.collision，反例分子100分母100'
    b = '方案「S」 variables.b：ratio_default.collision，反例分子50分母100'
    rewritten = '方案「S」 variables.a：ratio_default.collision，改写反例分子1分母1'
    assert compile_objects(report([a])) == compile_objects(report([rewritten]))
    watch = ProgressWatch()
    observe(watch, report([a, b]))
    assert observe(watch, report([rewritten, b])) == 1
    assert observe(watch, report([rewritten])) == 0
    assert observe(watch, report([])) == 0


def test_global_task_gate_preserves_each_actual_failed_task_identity():
    a = 'construction.task：T21未通过局部检查或已失效，调用check_formula_task'
    b = 'construction.task：T22未通过局部检查或已失效，调用check_formula_task'
    watch = ProgressWatch()
    observe(watch, report([a, b]))
    assert observe(watch, report([a, b])) == 1
    assert observe(watch, report([a])) == 0
    assert 'compile:basis:task:T22' in watch.compile_state['resolved']


@pytest.mark.evidence
def test_real_workflow_frontier_tracks_partial_binding_resolution_and_early_failure(tmp_path):
    ws = make_workspace(tmp_path)
    assert ws.prepare_data()['status'] == 'PASS'
    TaskBoard(ws).adopt()
    ws.facts += [ws.facts[0].model_copy(deep=True, update={'name': name, 'targets': []})
                 for name in ('待绑定甲', '待绑定乙')]
    assert ws.compile_formulas()['status'] == 'FAIL'
    full = ws.workflow['formula_report']['report']
    frontier = ws.blocker_frontier()
    assert {'compile:basis:fact:待绑定甲', 'compile:basis:fact:待绑定乙'} <= frontier
    watch = ProgressWatch()
    watch.observe(frontier, compile_report=full, fact_names=[f.name for f in ws.facts])
    watch.observe(frontier, compile_report=full, fact_names=[f.name for f in ws.facts])
    ws.bind_facts([{'name': '待绑定甲', 'targets': ['S::/pool/expr']}])
    assert ws.compile_formulas()['status'] == 'FAIL'
    assert watch.observe(ws.blocker_frontier(), compile_report=ws.workflow['formula_report']['report'],
                         fact_names=[f.name for f in ws.facts]) == 0
    ws.mapping['projects'].filter = {'amount': {'not': ['0']}}
    with pytest.raises(ToolError, match='data 尚未通过'):
        ws.compile_formulas()
    early = ws.workflow['formula_report']['report']
    assert early['static_checks_complete'] is False
    assert watch.observe(ws.blocker_frontier(), compile_report=early, fact_names=[f.name for f in ws.facts]) == 1
    assert 'compile:basis:fact:待绑定乙' in watch.compile_state['objects']
    assert not ws.llm.calls


@pytest.mark.evidence
def test_real_runtime_failure_is_not_resolved_by_new_static_gap_or_deferred_scheme(tmp_path):
    ws = make_workspace(tmp_path)
    ws.set_rules(scheme='S', pool={'scope': '项目', 'expr': 'amount * 0.1 / (amount - amount)',
                 'sources': ws.drafts['S']['pool']['sources']})
    assert ws.prepare_data()['status'] == 'PASS'
    assert ws.compile_formulas()['status'] == 'FAIL'
    failure = copy.deepcopy(ws.workflow['formula_report']['report'])
    assert failure['preflight']['S']['status'] == 'FAIL'
    watch = ProgressWatch()
    observe(watch, failure, ['提成率'])
    runtime = 'compile:basis:runtime:S'
    assert runtime in watch.compile_state['objects']
    ws.facts[0].targets = []
    assert ws.compile_formulas()['status'] == 'FAIL'
    hidden = ws.workflow['formula_report']['report']
    assert hidden['static_checks_complete'] and hidden['preflight'] == {}
    assert observe(watch, hidden, ['提成率']) == 1
    assert runtime in watch.compile_state['objects']
    assert observe(watch, report([], preflight={'S': {'status': 'DEFERRED'}, 'other': {'status': 'PASS'}})) == 0
    assert runtime in watch.compile_state['objects']  # static gaps cleared, runtime did not
    assert observe(watch, report([], preflight={'S': {'status': 'DEFERRED'}})) == 1
    assert observe(watch, report([], preflight={'S': {'status': 'PASS'}})) == 0
    assert runtime not in watch.compile_state['objects']
