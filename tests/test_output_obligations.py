import copy

import pytest

from bonus_agent import case
from bonus_agent.agent import ToolError, Workspace
from bonus_agent.construction import TaskBoard
from bonus_agent.llm import FakeLLM
from tests.test_staged_workflow import make_workspace, prepared


def update_plan(ws, schemes, targets=None):
    p = ws.workflow['plan']
    return ws.plan_calculation(p['summary'], p['outputs'], p['files'], schemes, output_targets=targets)


def output_map(scheme='S'):
    return {'项目底稿': ['S::/report/details/项目奖'], '总额': ['S::/report/totals/项目奖合计'],
            '人员金额': [scheme + '::/report/people']}


def test_person_obligation_invalidates_basis_if_project_details_still_exist(tmp_path):
    ws = make_workspace(tmp_path)
    update_plan(ws, ['S'], output_map())
    prepared(ws)
    assert not TaskBoard(ws).output_obligations()
    report = copy.deepcopy(ws.drafts['S']['report'])
    report.pop('people')
    ws.set_rules(scheme='S', report=report)
    assert not ws._flow().current('formulas')
    assert TaskBoard(ws).output_obligations() == [
        {'output': '人员金额', 'scheme': 'S', 'target': '/report/people',
         'task_target': '/report', 'kind': 'missing_component'}]
    result = ws.compile_formulas()
    assert result['status'] == 'FAIL' and any('S::/report/people' in i for i in result['issues'])
    assert not ws.workflow.get('basis') and not ws.results


def test_missing_new_scheme_is_actionable_with_legacy_plan_and_with_named_outputs(tmp_path):
    ws = prepared(make_workspace(tmp_path))
    update_plan(ws, ['S', 'B'])
    board = TaskBoard(ws)
    missing = board.output_obligations()
    assert any(i['scheme'] == 'B' and i['task_target'] == '/pool' and i['kind'] == 'missing_task' for i in missing)
    update_plan(ws, ['S', 'B'], output_map('B'))
    row = next(i for i in board.output_obligations() if i['output'] == '人员金额')
    assert row == {'output': '人员金额', 'scheme': 'B', 'target': '/report/people',
                   'task_target': '/report', 'kind': 'missing_task'}
    clone = Workspace(FakeLLM([]), ws.inv)
    case.restore(clone, case.snapshot(ws))
    assert TaskBoard(clone).output_obligations() == board.output_obligations()
    assert 'B' not in clone.drafts and not clone.results


def test_replanning_preserves_promised_outputs_and_rejects_bad_mapping_atomically(tmp_path):
    ws = make_workspace(tmp_path)
    update_plan(ws, ['S'], output_map())
    original = copy.deepcopy(ws.workflow['plan'])
    update_plan(ws, ['S'])
    assert ws.workflow['plan'] == original
    for bad in ({'总额': ['S::/report/totals']}, {**output_map(), '总额': ['other::/report/totals']},
                {**output_map(), '人员金额': ['S::/report/people/label']}):
        with pytest.raises(ToolError, match='output_targets'):
            update_plan(ws, ['S'], bad)
        assert ws.workflow['plan'] == original


def test_named_report_definition_requires_unique_complete_row(tmp_path):
    ws = prepared(make_workspace(tmp_path))
    update_plan(ws, ['S'], output_map())
    details = ws.drafts['S']['report']['details']
    details.append(copy.deepcopy(details[0]))
    assert TaskBoard(ws).output_obligations()[0]['target'] == '/report/details/项目奖'
    details.pop()
    details[0]['expr'] = ''
    assert TaskBoard(ws).output_obligations()[0]['kind'] == 'missing_component'
