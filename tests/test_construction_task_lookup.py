"""An unknown task ID must point to existing plans without mutating the case."""
import copy

import pytest

from bonus_agent import case
from bonus_agent.agent import ToolError
from bonus_agent.construction import TaskBoard
from tests.test_staged_workflow import make_workspace


@pytest.mark.parametrize('action', ['check', 'submit'])
def test_component_name_error_returns_existing_task_id_without_duplicate_plan(tmp_path, action):
    ws = make_workspace(tmp_path)
    ws.plan_formula_tasks([{'id': 'step17', 'scheme': 'S', 'target': '/variables/coef'}])
    before = copy.deepcopy(case.snapshot(ws))
    with pytest.raises(ToolError) as caught:
        if action == 'check':
            ws.check_formula_task('coef')
        else:
            ws.submit_formula_task('coef', {'expr': '1'})
    message = str(caught.value)
    assert 'step17' in message and '/variables/coef' in message and 'next_call' in message
    assert '未自动选择' in message
    assert case.snapshot(ws) == before


def test_same_component_in_multiple_schemes_returns_choices_and_keeps_both_unchanged(tmp_path):
    ws = make_workspace(tmp_path)
    ws.workflow['plan']['schemes'].append('Other')
    ws.plan_formula_tasks([{'id': 'one', 'scheme': 'S', 'target': '/variables/a~1b'},
                           {'id': 'two', 'scheme': 'Other', 'target': '/variables/a~1b'}])
    before = copy.deepcopy(case.snapshot(ws))
    with pytest.raises(ToolError) as caught:
        TaskBoard(ws).task_spec('a/b')
    assert 'one' in str(caught.value) and 'two' in str(caught.value)
    with pytest.raises(ToolError) as scoped:
        TaskBoard(ws).task_spec('S::/variables/a~1b')
    assert 'one' in str(scoped.value) and '"id": "two"' not in str(scoped.value)
    assert case.snapshot(ws) == before


def test_unknown_component_suggests_lookup_before_a_new_plan(tmp_path):
    ws = make_workspace(tmp_path)
    with pytest.raises(ToolError, match='确无该目标时才'):
        TaskBoard(ws).task_spec('absent')
