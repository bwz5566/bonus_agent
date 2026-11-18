import copy

import pytest

from bonus_agent.agent import ToolError
from tests.test_staged_workflow import make_workspace


def test_duplicate_target_reports_existing_id_without_changing_plan(tmp_path):
    ws = make_workspace(tmp_path)
    ws.plan_formula_tasks([{'id': 'original', 'scheme': 'S', 'target': '/pool'}])
    old = copy.deepcopy(ws.workflow['construction'])
    with pytest.raises(ToolError) as error:
        ws.plan_formula_tasks([{'id': 'new_id', 'scheme': 'S', 'target': '/pool'}])
    assert 'original' in str(error.value) and 'new_id' in str(error.value)
    assert 'existing_ids' in str(error.value) and '/pool' in str(error.value)
    assert ws.workflow['construction'] == old


def test_task_index_omits_large_evidence_but_exact_read_preserves_it(tmp_path):
    ws = make_workspace(tmp_path)
    ws.plan_formula_tasks([{'id': 'pool', 'scheme': 'S', 'target': '/pool', 'facts': ['提成率'], 'clauses': ['C1']}])
    ws.check_formula_task('pool')
    brief = ws.read_workspace('construction', query='/pool')['items'][0]
    assert brief['id'] == 'pool' and brief['fact_count'] == 1 and brief['clause_count'] == 1
    assert 'facts' not in brief and 'report' not in brief
    detail = ws.read_workspace('construction', name='pool')['items'][0]
    assert detail['facts'] == ['提成率'] and detail['clauses'] == ['C1']
    assert detail['report']['status'] == 'PASS'
