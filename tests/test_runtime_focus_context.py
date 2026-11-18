"""Primary repair context is grounded, bounded and does not refill read budgets."""
import copy
import json
from types import SimpleNamespace

import pytest

from bonus_agent.advice_context import MAX_CONTEXT_CHARS, build_runtime_focus_context
from bonus_agent.agent import ToolError, run_agent
from test_agent_tools_thinking import Recorder
from test_t3_fixes import _ws, _sales


def example(tmp_path):
    (tmp_path / '旧规则.txt').write_text('旧规则的适用范围。\n欠款时冻结整笔提成。\n需要结合本批说明。', encoding='utf-8')
    ws = _ws(tmp_path, '本批暂扣规则。\n先扣库存暂扣。\n欠款项目正常分配为0。\n欠款暂扣是剩余部分。\n分别报告。')
    _sales(ws)
    new = [{'doc': '规则.txt', 'quote': '欠款项目正常分配为0。'}]
    old = [{'doc': '旧规则.txt', 'quote': '欠款时冻结整笔提成。'}]
    ws.drafts['S'] = {'pool': {'scope': '销售', 'expr': 'net', 'sources': new}, 'allocation': [],
        'variables': {
            'net': {'scope': '销售', 'expr': 'ready - held', 'sources': new},
            'ready': {'scope': '销售', 'expr': 'if(flag, 0, gross)', 'sources': new},
            'held': {'scope': '销售', 'expr': 'if(flag, gross, 0)', 'sources': old},
            'flag': {'scope': '销售', 'expr': 'true'},
            'gross': {'scope': '销售', 'expr': 'amt * 0.1'},
        }}
    issue = {'code': 'pool_negative', 'node_id': '销售:A', 'rule_item': 'pool',
             'target': '/pool', 'message': 'negative', 'evidence_kind': 'policy_failure'}
    report = {'status': 'FAIL', 'phase': 'basis', 'issues': ['negative'],
              'preflight': {'S': {'status': 'FAIL', 'issues': [issue]}}}
    tasks = {ident: {'id': ident, 'scheme': 'S', 'target': path} for ident, path in [
        ('T1', '/pool'), ('T2', '/variables/net'), ('T3', '/variables/ready'), ('T4', '/variables/held')]}
    ws.workflow = {'enabled': True, 'formula_report': {'report': report, 'phase_version': 'v1'},
                   'construction': {'tasks': tasks, 'receipts': {key: {'status': 'PASS'} for key in tasks}}}
    ws._flow = lambda: SimpleNamespace(compile_feedback=lambda phase: report if phase == 'basis' else None)
    return ws, report


def test_pool_dependencies_existing_tasks_and_both_sources_arrive_without_tool_reads(tmp_path):
    ws, report = example(tmp_path)
    before = copy.deepcopy((ws.drafts, ws.workflow, ws.doc_reads, ws.questions, ws.notes))
    packet = build_runtime_focus_context(ws, report)
    nodes = {n['target']: n for n in packet['current_nodes']}
    assert nodes['S::/pool']['task_ids'] == ['T1']
    assert nodes['S::/variables/net']['task_ids'] == ['T2']
    assert nodes['S::/variables/ready']['task_ids'] == ['T3']
    assert nodes['S::/variables/held']['task_ids'] == ['T4']
    assert nodes['S::/variables/net']['definition']['expr'] == 'ready - held'
    excerpts = {x['file']: x['text'] for x in packet['source_excerpts']}
    assert '先扣库存暂扣。' in excerpts['规则.txt'] and '欠款暂扣是剩余部分。' in excerpts['规则.txt']
    assert '欠款时冻结整笔提成。' in excerpts['旧规则.txt'] and '需要结合本批说明。' in excerpts['旧规则.txt']
    assert all(x['declared_reference_only'] for x in packet['source_references'])
    assert all(x['verified_in_current_material'] and x['not_a_read_receipt'] for x in packet['source_excerpts'])
    assert (ws.drafts, ws.workflow, ws.doc_reads, ws.questions, ws.notes) == before
    assert not ws.llm.calls and len(json.dumps(packet, ensure_ascii=False)) <= MAX_CONTEXT_CHARS


def test_report_balance_total_parts_and_ancestor_report_task_are_included(tmp_path):
    ws, report = example(tmp_path)
    ws.drafts['S']['report'] = {'balances': [{'name': 'split', 'level': '销售', 'total': 'gross', 'parts': ['ready', 'held']}]}
    ws.workflow['construction']['tasks']['T9'] = {'scheme': 'S', 'target': '/report'}
    issue = report['preflight']['S']['issues'][0]
    issue.update(code='report_unbalanced', target='/report/balances', rule_item='report.balances')
    packet = build_runtime_focus_context(ws, report)
    nodes = {n['target']: n for n in packet['current_nodes']}
    assert nodes['S::/report/balances']['task_ids'] == ['T9']
    assert {'S::/variables/gross', 'S::/variables/ready', 'S::/variables/held'} <= nodes.keys()


def test_checks_use_real_id_and_expand_predicate_without_unrelated_checks(tmp_path):
    ws, report = example(tmp_path)
    ws.drafts['S']['checks'] = [{'id': 'wanted', 'expr': 'gross > 0'}, {'id': 'other', 'expr': 'unrelated > 0'}]
    report['preflight']['S']['issues'][0].update(code='check_failed', target='/checks', rule_item='checks.wanted')
    packet = build_runtime_focus_context(ws, report)
    assert packet['current_nodes'][0]['definition'] == [{'id': 'wanted', 'expr': 'gross > 0'}]
    assert any(n['target'] == 'S::/variables/gross' for n in packet['current_nodes'])


def test_declared_page_cannot_be_verified_by_identical_quote_on_other_page(tmp_path):
    ws, report = example(tmp_path)
    for value in [ws.drafts['S']['pool'], *ws.drafts['S']['variables'].values()]:
        value.pop('sources', None)
    ws.drafts['S']['pool']['sources'] = [{'doc': '规则.txt', 'page': 2, 'quote': '欠款项目正常分配为0。'}]
    ws._page_raw = lambda name: {1: '欠款项目正常分配为0。', 2: '别的规定。'}
    packet = build_runtime_focus_context(ws, report)
    assert not packet['source_excerpts']
    assert packet['source_references'][0]['declared_reference_only']
    assert any(x.get('source_ref') == 1 for x in packet['omitted'])


def test_stale_or_passed_report_never_emits_old_failure_and_reading_does_not_change_identity(tmp_path):
    ws, report = example(tmp_path)
    packet = build_runtime_focus_context(ws, report)
    ws.read_document('规则.txt')
    assert build_runtime_focus_context(ws, report)['fingerprint'] == packet['fingerprint']
    stale = copy.deepcopy(report); stale['issues'] = ['stale']
    assert build_runtime_focus_context(ws, stale) is None
    report['preflight']['S']['status'] = 'PASS'
    assert build_runtime_focus_context(ws, report) is None
    (tmp_path / '规则.txt').write_text('源文件已变。', encoding='utf-8')
    with pytest.raises(ToolError):
        build_runtime_focus_context(ws, report)


def test_cycles_and_oversized_components_are_bounded_without_formula_truncation(tmp_path):
    ws, report = example(tmp_path)
    ws.drafts['S']['variables']['gross']['expr'] = 'net'
    ws.drafts['S']['variables']['held']['expr'] = '1+' * 6000 + '1'
    packet = build_runtime_focus_context(ws, report)
    assert len(packet['current_nodes']) <= 8 and packet['limits']['truncated']
    assert len(json.dumps(packet, ensure_ascii=False)) <= MAX_CONTEXT_CHARS
    assert not any(n['target'] == 'S::/variables/held' for n in packet['current_nodes'])


@pytest.mark.parametrize('mode', ['native', 'json'])
def test_primary_loop_has_one_focus_packet_per_request_and_still_stops_at_eight_reads(tmp_path, mode):
    from tests.test_staged_workflow import make_workspace, save_staged_case
    ws = make_workspace(tmp_path)
    pool = copy.deepcopy(ws.drafts['S']['pool']); pool['expr'] = '-amount * 0.1'
    ws.set_rules(scheme='S', pool=pool)
    assert ws.prepare_data()['status'] == 'PASS'
    out = tmp_path / 'out'; save_staged_case(ws, out)
    script = [{'tool': 'read_workspace', 'args': {'section': 'rules', 'name': name}} for name in
              ['/pool', '/allocation', '/report', '/report/totals', '/report/details', '/report/people', '/report/distributions', '/pool/expr']]
    llm = Recorder(script, mode); llm.workflow_mode = 'staged'
    result = run_agent(llm, ws.inv.files, out, max_steps=15, deep=0, reflect=0)
    assert '读取 8/8 次' in result.error
    assert len(llm.requests) == 8
    fingerprints = []
    for messages, _ in llm.requests:
        packets = [m['content'] for m in messages if isinstance(m.get('content'), str)
                   and m['content'].startswith('【当前运行失败聚焦包；')]
        assert len(packets) == 1
        packet = json.loads(packets[0].split('】', 1)[1])
        fingerprints.append(packet['fingerprint'])
        assert packet['current_nodes'][0]['target'] == 'S::/pool'
    assert len(set(fingerprints)) == 1
    assert not any(step.tool == '（局部诊断）' for step in result.steps)


def test_cached_packet_checks_source_change_before_next_model_request(tmp_path):
    from tests.test_staged_workflow import make_workspace, save_staged_case
    ws = make_workspace(tmp_path)
    pool = copy.deepcopy(ws.drafts['S']['pool']); pool['expr'] = '-amount * 0.1'
    ws.set_rules(scheme='S', pool=pool); ws.prepare_data()
    out = tmp_path / 'out'; save_staged_case(ws, out)
    llm = Recorder([{'tool': 'read_workspace', 'args': {'section': 'rules', 'name': '/pool'}}])
    llm.workflow_mode = 'staged'
    def on_step(n, step):
        if step.tool == 'read_workspace':
            (tmp_path / 'policy.md').write_text('Changed source after first request.', encoding='utf-8')
    result = run_agent(llm, ws.inv.files, out, max_steps=4, deep=0, reflect=0, on_step=on_step)
    assert len(llm.requests) == 1 and '材料' in result.error
