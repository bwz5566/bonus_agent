"""Report references must exist, retain exact evidence, and expose partial rejection."""
import copy
import json

import pytest

from bonus_agent.agent import ToolError, run_agent
from bonus_agent.clauses import Clause, check_where, known_refs, refs, report_elements
from bonus_agent.llm import FakeLLM
from tests.test_staged_workflow import make_workspace, save_staged_case


def report_draft():
    return {'report': {
        'distributions': [{'name': 'funding', 'level': 'global', 'total': 'funded',
                           'pending': ['Unassigned'], 'rounding': 'after_sum'}],
        'pending': [{'name': 'Unassigned', 'level': 'Project', 'expr': 'reserved'}],
        'balances': [],
    }}


def test_report_sections_named_items_and_exact_fields_share_one_resolver():
    draft = report_draft()
    elements = report_elements(draft)
    known = known_refs({'S': draft}, {})
    assert set(elements) <= known
    for path in ('report.distributions', 'report.distributions.0.pending',
                 'report.distributions.funding.rounding', 'report.pending.Unassigned.expr'):
        assert check_where(path, known, set()) is None
    assert elements['report.distributions.funding.rounding'] == 'after_sum'
    assert elements['report.pending.Unassigned.expr'] == 'reserved'
    for invalid in ('report.balances', 'report.distributions.pending',
                    'report.distributions.3.pending', 'report.pending.Missing'):
        assert check_where(invalid, known, set())


def test_pending_references_do_not_invent_pending_definitions_or_accept_builtin_excuses():
    draft = report_draft()
    del draft['report']['pending']
    known = known_refs({'S': draft}, {})
    assert 'report.distributions.0.pending' in known
    assert 'report.pending' not in known
    message = check_where('report.pending、引擎自带：由程序处理', known, set())
    assert message and message.startswith('report.pending 不存在')
    assert 'report.distributions' in message


def test_duplicate_named_items_require_exact_indices():
    draft = report_draft()
    draft['report']['pending'].append(copy.deepcopy(draft['report']['pending'][0]))
    known = known_refs({'S': draft}, {})
    assert 'report.pending.Unassigned' not in known
    assert {'report.pending.0', 'report.pending.1'} <= known


def test_nested_explanatory_parentheses_do_not_turn_valid_paths_into_missing_references():
    where = 'report.distributions（客户AI01(IVG)，按实际制度计算）、pool(仅求和)'
    assert refs(where) == ['report.distributions', 'pool']
    assert check_where(where, {'report.distributions', 'pool'}, set()) is None


def test_partial_coverage_keeps_success_and_returns_machine_readable_failure(tmp_path):
    ws = make_workspace(tmp_path)
    ws.clauses.append(Clause(id='C2', point='待分配', file='policy.md', quote='按名单比例分配'))
    result = ws.cover_clauses({'C1': 'report.distributions', 'C2': 'report.pending'})
    assert result['status'] == 'FAIL' and result['applied'] == ['C1']
    assert result['rejected'][0]['id'] == 'C2' and result['remaining_clauses'] == ['C2']
    assert 'report.pending' in result['issues'][0]
    assert ws.clauses[0].where == 'report.distributions' and ws.clauses[1].where == ''
    assert ws.clauses[0].checked and 'report.distributions' in result['review']
    assert '不代表业务口径' in result['scope']
    assert not ws.results and not ws.workflow.get('basis')


def test_failed_replacement_preserves_prior_valid_binding(tmp_path):
    ws = make_workspace(tmp_path)
    before = ws.clauses[0].model_dump()
    with pytest.raises(ToolError, match='report.pending'):
        ws.cover_clauses({'C1': 'report.pending'})
    assert ws.clauses[0].model_dump() == before


def test_report_child_binding_tracks_actual_content_and_disappearing_items(tmp_path):
    ws = make_workspace(tmp_path)
    ws.drafts['S']['report'].update(report_draft()['report'])
    result = ws.cover_clauses({'C1': 'report.pending.Unassigned.expr'})
    assert result['status'] == 'PASS'
    before = ws.clauses[0].checked
    assert 'reserved' in before
    ws.drafts['S']['report']['pending'][0]['expr'] = 'other_reserved'
    assert ws._clause_sig(ws.clauses[0], ws._element_texts()) != before
    ws.drafts['S']['report']['pending'] = []
    assert any('report.pending.Unassigned.expr' in item for item in ws._clause_gaps(set()))


def test_staged_tool_trace_does_not_mark_partial_coverage_as_success(tmp_path):
    ws = make_workspace(tmp_path)
    ws.clauses.append(Clause(id='C2', point='待分配', file='policy.md', quote='按名单比例分配'))
    out = tmp_path / 'case'
    save_staged_case(ws, out)
    llm = FakeLLM([{'tool': 'cover_clauses', 'args': {
        'items': {'C1': 'report.distributions', 'C2': 'report.pending'},
    }}])
    llm.workflow_mode = 'staged'
    result = run_agent(llm, ws.inv.files, out, max_steps=1)
    step = next(s for s in result.steps if s.tool == 'cover_clauses')
    assert not step.ok
    payload, _ = json.JSONDecoder().raw_decode(step.result)
    assert payload['status'] == 'FAIL' and payload['applied'] == ['C1']
    assert result.ws.clauses[0].where == 'report.distributions'
    assert not result.results
