"""A first real compiler diagnosis opens repair once, without renewing budgets."""
import json

import pytest

from bonus_agent.agent import Step
from bonus_agent.construction import TaskBoard
from bonus_agent.progress import ProgressWatch, discover
from bonus_agent.recovery import RecoveryJournal
from tests.test_staged_workflow import make_workspace


def compile_step(report, *, automatic=False):
    return Step(
        '（自动复核）' if automatic else 'compile_formulas',
        {'tool': 'compile_formulas', 'automatic': True, 'phase': report.get('phase')} if automatic else {},
        json.dumps(report, ensure_ascii=False), report.get('status') == 'PASS',
    )


def unbound_workspace(tmp_path):
    ws = make_workspace(tmp_path)
    assert ws.prepare_data()['status'] == 'PASS'
    TaskBoard(ws).adopt()
    assert all(row['status'] == 'PASS' for row in TaskBoard(ws).statuses().values())
    # A restored, source-backed fact can be missing its binding even when the
    # previously checked component tasks all pass, as in the T18 resume.
    ws.facts.append(ws.facts[0].model_copy(deep=True, update={'name': '待绑定提成率', 'targets': []}))
    return ws


@pytest.mark.evidence
def test_first_real_compile_failure_is_not_killed_by_previous_read_stall(tmp_path):
    ws = unbound_workspace(tmp_path)
    watch = ProgressWatch({'compile_formulas'}, 12)
    report = ws.compile_formulas()
    assert report['status'] == 'FAIL'
    assert any('待绑定提成率' in issue for issue in report['issues']), report
    assert not ws._flow().current('formulas')
    seen = set()
    assert watch.observe({'compile_formulas'}, discovery=discover(compile_step(report), seen)) == 0
    # Repeating a failed compile is still a failure, and cannot buy another
    # diagnostic allowance by changing tool mode or wrapping it as automatic.
    same = ws.compile_formulas()
    assert not discover(compile_step(same, automatic=True), seen)
    assert watch.observe({'compile_formulas'}) == 1
    assert not ws.workflow.get('basis')
    assert not ws.llm.calls


@pytest.mark.evidence
def test_description_rewrite_does_not_mint_another_diagnostic_allowance(tmp_path):
    ws = unbound_workspace(tmp_path)
    seen = set()
    assert discover(compile_step(ws.compile_formulas()), seen)
    for number in range(3):
        ws.drafts['S']['pool']['desc'] = f'改写说明{number}'
        report = ws.compile_formulas()
        assert report['status'] == 'FAIL'
        assert not discover(compile_step(report), seen)
    # Even differently worded/new error text is not proof that an old blocker
    # was fixed. Real reduction is handled by the frontier, not this allowance.
    rewritten = {'phase': 'basis', 'status': 'FAIL', 'issues': ['换一种说法描述未绑定参数']}
    assert not discover(compile_step(rewritten), seen)


def test_diagnostic_credit_and_failure_budget_survive_same_context_resume(tmp_path):
    path = tmp_path / 'recovery.json'
    journal = RecoveryJournal(path, {'inputs': 'same', 'code': 'same'})
    report = {'phase': 'basis', 'status': 'FAIL', 'issues': ['parameter.binding: rate 未绑定']}
    step = compile_step(report)
    assert journal.failure(step.tool, step.args, step.result) == 1
    assert journal.claim_advice('binding')
    seen = set()
    assert discover(step, seen)
    journal.state['progress'] = {'previous': ['compile_formulas'], 'stagnant': 0, 'reads': sorted(seen)}
    journal.save()
    resumed = RecoveryJournal(path, {'inputs': 'same', 'code': 'same'})
    saved = resumed.state['progress']
    watch = ProgressWatch(set(saved['previous']), saved['stagnant'])
    assert not discover(compile_step(report, automatic=True), set(saved['reads']))
    assert watch.observe({'compile_formulas'}) == 1
    assert not resumed.claim_advice('binding')
    assert resumed.failure(step.tool, step.args, step.result) == 2
    assert resumed.failure(step.tool, step.args, step.result) == 3
    assert '累计失败 3 次' in resumed.error
    # A diagnostic is never permission to clear an existing hard recovery stop.
    assert not discover(step, set(saved['reads']))
    assert resumed.error


@pytest.mark.parametrize('report', [
    {'phase': 'basis', 'status': 'PASS', 'issues': []},
    {'phase': 'basis', 'status': 'DEFERRED', 'issues': ['等待依赖']},
    {'phase': 'basis', 'status': 'NOT_RUN', 'issues': ['compile_formulas']},
    {'phase': 'basis', 'status': 'FAIL', 'issues': []},
    {'phase': 'basis', 'status': 'FAIL', 'issues': ['  ', {}]},
    {'status': 'FAIL', 'issues': ['无有效编译阶段']},
])
def test_status_or_empty_report_is_not_diagnostic_progress(report):
    seen = set()
    assert not discover(compile_step(report), seen)
    assert not discover(compile_step(report, automatic=True), seen)
    assert not seen


def test_cached_queries_and_noncompiler_failures_cannot_claim_diagnostic_credit():
    report = {'section': 'checks', 'phase': 'basis', 'status': 'FAIL', 'issues': ['parameter.binding']}
    seen = set()
    for tool in ('read_workspace', 'query_dataset', 'set_rules', '（局部诊断）'):
        for query in ('rate', 'threshold', 'new spelling'):
            step = Step(tool, {'query': query}, json.dumps(report), False)
            assert not discover(step, seen)
    assert not discover(Step('compile_formulas', {}, '错误：参数不是合法 JSON', False), seen)
    assert not seen


def test_compiler_phases_have_a_finite_shared_manual_automatic_allowance():
    seen = set()
    for phase in ('basis', 'all'):
        step = compile_step({'phase': phase, 'status': 'FAIL', 'issues': [{'message': '具体校验失败'}]}, automatic=True)
        step.result += '\n（提醒：请填写thought。）'
        assert discover(step, seen)
        assert not discover(step, seen)
        assert not discover(compile_step({'phase': phase, 'status': 'FAIL', 'issues': ['另一种错误描述']}), seen)
    assert len(seen) == 2


def checks_step(items, **extra):
    args = {'section': 'checks', 'offset': extra.get('offset', 0), 'query': extra.pop('query', '')}
    return Step('read_workspace', args, json.dumps({'section': 'checks', 'items': items, **extra}, ensure_ascii=False), True)


@pytest.mark.evidence
def test_real_checks_pages_are_discovered_before_a_first_repair(tmp_path):
    ws = unbound_workspace(tmp_path)
    for name in ('待绑定阈值', '待绑定缺省值'):
        ws.facts.append(ws.facts[0].model_copy(deep=True, update={'name': name, 'targets': []}))
    report = ws.compile_formulas()
    assert report['status'] == 'FAIL'
    seen = set()
    assert discover(compile_step(report), seen)
    # The first-compile allowance was already consumed in the earlier round.
    assert not discover(compile_step(report, automatic=True), seen)
    watch = ProgressWatch({'compile_formulas'}, 1)
    pages = []
    offset = 0
    while offset is not None:
        page = ws.read_workspace('checks', offset=offset, limit=1)
        step = Step('read_workspace', {'section': 'checks', 'offset': offset, 'limit': 1},
                    json.dumps(page, ensure_ascii=False), True)
        pages.append(step)
        assert discover(step, seen), page
        assert watch.observe({'compile_formulas'}, discovery=True) == 0
        offset = page['next_offset']
    assert len(pages) >= 3
    # Reading all the concrete errors does not mark a single one fixed.
    assert not ws._flow().current('formulas')
    assert not ws.workflow.get('basis')
    for step in reversed(pages):
        assert not discover(step, seen)
        watch.observe({'compile_formulas'})
    assert watch.stagnant >= 3
    assert not ws.llm.calls


def test_checks_discovery_persists_by_record_across_paging_query_and_prose_changes(tmp_path):
    journal = RecoveryJournal(tmp_path / 'recovery.json', {'input': 'same'})
    seen = set()
    first = [{'kind': 'issue', 'index': 0, 'message': 'policy.pptx::P2.T1.R2.C2.L1：parameter.unit，原文需核对'},
             {'kind': 'issue', 'index': 1, 'message': 'C14「最初条款说明」：variables.rate 不存在'}]
    second = [{'kind': 'issue', 'index': 2, 'message': 'policy.pptx::P3.T1.R2.C2.L1：缺少参数绑定'}]
    assert discover(checks_step(first), seen)
    assert discover(checks_step(second, offset=2), seen)
    journal.state['progress'] = {'reads': sorted(seen), 'previous': ['compile_formulas'], 'stagnant': 0}
    journal.save()
    resumed = RecoveryJournal(journal.path, {'input': 'same'})
    restored = set(resumed.state['progress']['reads'])
    rewritten = [
        {'kind': 'issue', 'index': 10, 'message': 'C14「重写条款说明」：另一种修复建议'},
        {'kind': 'issue', 'index': 11, 'message': 'policy.pptx::P2.T1.R2.C2.L1：改写说明，候选和数值已改变'},
        {'kind': 'issue', 'index': 12, 'message': 'policy.pptx::P3.T1.R2.C2.L1：修改后的长说明'},
    ]
    assert not discover(checks_step(rewritten, query='另一种查询', offset=10, version='edited-description'), restored)
    assert not discover(checks_step(first + second, offset=0, version='another'), restored)


def test_legacy_heading_or_unknown_message_rewrite_cannot_mint_new_credits():
    seen = set()
    initial = [{'kind': 'issue', 'index': 0, 'message': '参数名：参数需要绑定'},
               {'kind': 'issue', 'index': 1, 'message': '无结构化标识的检查说明'}]
    assert discover(checks_step(initial), seen)
    for n in range(4):
        rewritten = [{'kind': 'issue', 'index': 0, 'message': f'另一种描述{n}：新的措辞'},
                     {'kind': 'issue', 'index': 1, 'message': f'重写说明{n}'}]
        assert not discover(checks_step(rewritten, query=str(n), version=n), seen)
    # Moving an already read parameter to another slot is also just a replay.
    assert not discover(checks_step([{'kind': 'issue', 'index': 40, 'message': '参数名：换个修复建议'}]), seen)


def test_structured_diagnostics_track_real_new_locations_not_formula_prose():
    seen = set()
    issue = {'kind': 'preflight', 'index': 0, 'code': 'basis_preflight_eval',
             'where': 'Project:A/base', 'node_id': 'Project:A', 'rule_item': 'base',
             'message': 'empty rows', 'formula': 'min(amount, of: "sales")'}
    assert discover(checks_step([issue]), seen)
    assert not discover(checks_step([{**issue, 'index': 20, 'message': 'rewritten', 'formula': 'edited prose'}], version='v2'), seen)
    assert discover(checks_step([{**issue, 'where': 'Project:B/base', 'node_id': 'Project:B'}]), seen)
    assert not discover(checks_step([issue], query='different'), seen)


@pytest.mark.parametrize('status', ['STALE', 'NOT_RUN', 'ERROR', 'DEFERRED'])
def test_unavailable_checks_are_not_read_progress(status):
    seen = set()
    assert not discover(checks_step([{'kind': 'issue', 'index': 0, 'message': '参数：错误'}], status=status), seen)
    assert not seen


def test_empty_or_fragment_offsets_do_not_count_as_new_diagnostic_records():
    seen = set()
    assert not discover(checks_step([], offset=1), seen)
    for n in range(5):
        page = checks_step([], offset=n, item_fragment={'text': 'x' * 2000, 'offset': n * 1000,
                                                       'next_detail_offset': (n + 1) * 1000})
        assert not discover(page, seen)
    assert not seen
