"""Optional diagnosis must carry current evidence beyond the recent tool window."""
import copy
import json
from types import SimpleNamespace

import pytest

from bonus_agent.advice_context import MAX_CONTEXT_CHARS, build_advice_context
from bonus_agent.agent import Step, ToolError
from bonus_agent.llm import FakeLLM
from bonus_agent.recovery import local_advice
from test_t3_fixes import _ws, _sales


RULE = ('本期仅核算销售明细中的项目。\n\n'
        '历史明细用于客户全年累计，不能依据本期对象范围删除历史行。\n\n'
        '首发日期用于新老项目判断。')


def example(tmp_path):
    ws = _ws(tmp_path, RULE)
    _sales(ws)
    ws.drafts['S'] = {
        'variables': {'age': {'scope': '销售', 'expr': 'min(amt, of: "销售")',
                              'sources': [{'doc': '规则.txt', 'quote': '首发日期用于新老项目判断。'}]}},
        'pool': {'scope': '销售', 'expr': 'age'}, 'allocation': [],
    }
    issue = {'code': 'basis_preflight_eval', 'message': 'empty', 'where': '销售:OLD/age',
             'node_id': '销售:OLD', 'rule_item': 'age', 'formula': 'min(amt, of: "销售")',
             'dependency_chain': [{'rule_item': 'age'}, {'rule_item': 'pool'}],
             'coverage': [{'dataset': '销售', 'visible_rows': 0, 'selected_rows': 0}]}
    scope = {'status': 'OBSERVATION', 'comparison_count': 1, 'omitted': 0,
             'comparisons': [{'parent': '销售', 'detail': '销售', 'parent_keys': 8, 'detail_keys': 2,
                              'hypothetical_population_from': {'other_details_losing_parent': []}}],
             'notes': ['数量差异不是批准删除对象的依据']}
    report = {'status': 'FAIL', 'phase': 'basis', 'static_checks_complete': True,
              'issues': ['方案「S」基础求值预检：empty'],
              'preflight': {'S': {'status': 'FAIL', 'issues': [issue], 'scope_context': scope}}}
    ws.workflow = {'enabled': True, 'formula_report': {'report': report, 'phase_version': 'current'}}
    ws._flow = lambda: SimpleNamespace(compile_feedback=lambda phase: report if phase == 'basis' else None)
    old = [Step('read_document', {'file': '规则.txt'}, ws.read_document('规则.txt'), True),
           Step('read_workspace', {'section': 'rules', 'name': 'S::/variables/age'}, '{"old":true}', True)]
    recent = [Step('query_dataset', {'dataset': '销售', 'expr': f'count(of: "销售") + {i}'},
                   json.dumps({'rows': 8, 'value': str(8 + i), 'scope': '原表统计，不批准范围'}), True)
              for i in range(4)]
    return ws, report, old + recent


def test_current_failure_original_policy_and_older_reads_survive_recent_window(tmp_path):
    ws, report, steps = example(tmp_path)
    before = copy.deepcopy((ws.drafts, ws.datasets, ws.mapping, ws.workflow, ws.questions))
    packet = build_advice_context(ws, steps, report)
    assert packet['status'] == 'CURRENT'
    assert packet['current_failure'][0]['diagnostic'] == report['preflight']['S']['issues'][0]
    assert packet['scope_observations'][0]['comparison']['parent_keys'] == 8
    text = json.dumps(packet, ensure_ascii=False)
    assert '本期仅核算销售明细中的项目' in text and '客户全年累计' in text
    assert any(x['tool'] == 'read_document' and x['step'] == 1 for x in packet['read_receipts'])
    assert any(x.get('target') == 'S::/variables/age' for x in packet['current_nodes'])
    assert len(text) <= MAX_CONTEXT_CHARS
    assert (ws.drafts, ws.datasets, ws.mapping, ws.workflow, ws.questions) == before
    assert not ws.llm.calls


def test_static_guard_gets_real_table_shared_helper_and_original_evidence(tmp_path):
    ws, report, steps = example(tmp_path)
    ws.drafts['S']['tables'] = {'rate': {'kind': 'tier', 'steps': [[0, '.1']],
                                       'applies_when': 'age > unknown',
                                       'sources': [{'doc': '规则.txt', 'quote': '首发日期用于新老项目判断。'}]}}
    report.update(preflight={}, issues=['[阻断] name_unknown: 无法识别的名字 unknown (tables.rate.applies_when)'])
    packet = build_advice_context(ws, steps, report)
    nodes = {x['target']: x for x in packet['current_nodes'] if 'target' in x}
    assert nodes['S::/tables/rate']['definition']['applies_when'] == 'age > unknown'
    assert 'S::/variables/age' in nodes
    assert packet['source_excerpts']
    assert not packet['scope_observations']


def test_stale_report_or_new_source_never_claims_current_evidence(tmp_path):
    ws, report, steps = example(tmp_path)
    stale = copy.deepcopy(report)
    stale['issues'] = ['stale issue']
    packet = build_advice_context(ws, steps, stale)
    assert packet['status'] == 'NO_CURRENT_REPORT' and not packet['current_nodes']
    (tmp_path / '规则.txt').write_text('新规则，旧内容已作废。', encoding='utf8')
    with pytest.raises(ToolError, match='材料在本轮运行期间发生变化'):
        build_advice_context(ws, steps, report)
    assert not ws.llm.calls


def test_unread_source_is_explicit_without_inventing_missing_file_or_quoting_it(tmp_path):
    ws, report, steps = example(tmp_path)
    packet = build_advice_context(ws, steps[1:], report)
    coverage = packet['source_coverage'][0]
    assert coverage['loaded_material'] and not coverage['successful_read_recorded']
    assert not packet['source_excerpts']
    assert '不代表缺文件' in coverage['meaning']


def test_pinned_folded_page_is_not_claimed_as_previously_seen(tmp_path):
    ws, report, steps = example(tmp_path)
    ws.pinned = {'规则.txt'}
    visible, hidden = '本期仅核算销售明细中的项目。', '未展示的限定：首发日期缺失须先补数据。'
    ws._render_doc = lambda doc, folded=False: visible + ('\n\n' + hidden if folded else '')
    packet = build_advice_context(ws, steps[1:], report)
    texts = [item['text'] for item in packet['source_excerpts']]
    assert visible in texts and hidden not in texts


def test_oversized_diagnostic_and_definition_are_omitted_as_whole_objects(tmp_path):
    ws, report, steps = example(tmp_path)
    report['preflight']['S']['issues'][0]['message'] = 'diagnostic' * 3000
    ws.drafts['S']['variables']['age']['desc'] = 'large' * 4000
    packet = build_advice_context(ws, steps, report)
    assert packet['limits']['truncated'] and packet['omitted']
    assert not packet['current_failure']
    assert any(x.get('read_call', {}).get('args', {}).get('name') == 'preflight:S' for x in packet['omitted'])
    assert not any('large' in json.dumps(x) for x in packet['current_nodes'])
    assert len(json.dumps(packet, ensure_ascii=False)) <= MAX_CONTEXT_CHARS


def test_adviser_receives_context_mapping_contract_and_no_partial_json(tmp_path):
    ws, report, steps = example(tmp_path)
    context = build_advice_context(ws, steps, report)
    large = Step('read_workspace', {'section': 'rules', 'name': 'S::/variables/age'},
                 json.dumps({'payload': 'x' * 3000}), True)
    steps.append(large)
    captured = []
    llm = FakeLLM(lambda messages: captured.append(messages) or json.dumps({
        'diagnosis': '依据范围原文', 'next_action': '按证据调整映射',
        'evidence_needed': '完整键对照', 'acceptance': '重新检查历史范围'}))
    local_advice(llm, 'runtime scope failure', steps, [], '检查映射', context=context)
    packet = json.loads(captured[0][-1]['content'])
    assert packet['evidence_context'] == context
    contract = next(x for x in packet['tool_contracts'] if x['name'] == 'update_dataset')
    assert 'population_from' in contract['schema']['properties']['changes']['properties']
    assert isinstance(packet['recent'][-1]['result'], dict) and packet['recent'][-1]['result']['omitted']
    assert '不得把永久保留该反例' in captured[0][0]['content']
    assert len(captured[0][-1]['content']) <= 18000


def test_old_related_node_is_structured_or_explicitly_omitted():
    captured = []
    llm = FakeLLM(lambda messages: captured.append(json.loads(messages[-1]['content'])) or json.dumps({
        'diagnosis': 'missing', 'next_action': 'read', 'evidence_needed': 'source', 'acceptance': 'validate'}))
    old = Step('read_workspace', {'section': 'variables', 'scheme': 'S', 'name': 'factor'},
               json.dumps({'formula': 'x' * 3000}), True)
    failed = Step('bind_facts', {'bindings': [{'name': 'default', 'targets': ['S::/variables/factor/expr']}]},
                  'parameter.binding: missing', False)
    local_advice(llm, failed.result, [old, failed], [], 'read source')
    excerpt = captured[0]['related_nodes'][0]['result_excerpt']
    assert isinstance(excerpt, dict) and excerpt['omitted']
