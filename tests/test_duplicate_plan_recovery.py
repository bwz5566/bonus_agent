"""A duplicate task is resolved only by a verified reuse of its original target owner."""
import copy
import json

import pytest

from bonus_agent.agent import ToolError, Workspace
from bonus_agent.auto import build_inventory
from bonus_agent.llm import FakeLLM
from bonus_agent.recovery import RecoveryJournal

pytestmark = pytest.mark.evidence


def task(ident, target='/allocation', scheme='S'):
    return {'id': ident, 'scheme': scheme, 'target': target}


def setup_failure(tmp_path, *, existing=None, duplicate=None):
    ws = Workspace(FakeLLM([]), build_inventory([]))
    ws.drafts.update({'S': {}, 'T': {}})
    ws.plan_formula_tasks(existing or [task('T38')])
    args = {'tasks': duplicate or [task('T43', '/tables/roles'), task('T44')]}
    journal = RecoveryJournal(tmp_path / 'recovery.json', {'case': 'synthetic'})
    with pytest.raises(ToolError) as caught:
        ws.plan_formula_tasks(**args)
    result = '错误：' + str(caught.value)
    journal.failure('plan_formula_tasks', args, result)
    journal.observe_native_feedback('plan_formula_tasks', args, result, False)
    return ws, journal


def apply_plan(ws, journal, tasks, **options):
    args = {'tasks': tasks, **options}
    report = ws.plan_formula_tasks(**args)
    journal.observe_native_feedback('plan_formula_tasks', args, json.dumps(report, ensure_ascii=False), True)
    return report


def test_real_plan_failure_then_original_id_reuse_closes_only_duplicate_error(tmp_path):
    ws, journal = setup_failure(tmp_path)
    assert [r['objects'] for r in journal.state['native_repairs'].values()] == [['T44']]
    journal.observe_native_feedback('bind_facts', {'bindings': [{'name': 'unrelated'}]},
                                    '错误：parameter.binding：unrelated缺目标', False)
    journal.state['unconfirmed_reserved_tokens'] = 1234
    history = copy.deepcopy(journal.state['failures'])
    report = apply_plan(ws, journal, [task('T43', '/tables/roles'), task('T38')])
    assert report['status'] == 'PLANNED'
    assert next(row for row in report['validated_targets'] if row['id'] == 'T38') == {
        'id': 'T38', 'scheme': 'S', 'target': '/allocation', 'unique': True, 'reused': True}
    assert [r['code'] for r in journal.state['native_repairs'].values()] == ['parameter.binding']
    assert journal.state['failures'] == history
    assert journal.state['unconfirmed_reserved_tokens'] == 1234
    assert not ws.llm.calls


@pytest.mark.parametrize('repair,options', [
    ([task('T43', '/tables/roles')], {}),
    ([task('T38', scheme='T')], {}),
    ([task('T38', '/pool')], {}),
    ([task('T44')], {'replace': True}),
])
def test_other_success_or_replacing_original_id_cannot_clear_duplicate(tmp_path, repair, options):
    ws, journal = setup_failure(tmp_path)
    apply_plan(ws, journal, repair, **options)
    assert any(row['code'] == 'construction.plan' for row in journal.state['native_repairs'].values())


@pytest.mark.parametrize('change', [
    {'status': 'FAIL'}, {'validated_targets': []},
    {'validated_targets': [{'id': 'T38', 'scheme': 'S', 'target': '/allocation', 'unique': False, 'reused': True}]},
    {'validated_targets': [{'id': 'T38', 'scheme': 'S', 'target': '/allocation', 'unique': True, 'reused': False}]},
])
def test_success_boolean_without_unique_reuse_evidence_is_insufficient(tmp_path, change):
    ws, journal = setup_failure(tmp_path)
    args = {'tasks': [task('T38')]}
    report = {**ws.plan_formula_tasks(**args), **change}
    journal.observe_native_feedback('plan_formula_tasks', args, json.dumps(report), True)
    assert journal.state['native_repairs']


def test_legacy_saved_conflicts_resolve_after_fresh_verified_plan_not_old_summary(tmp_path):
    ws, journal = setup_failure(tmp_path)
    for item in journal.state['native_repairs'].values():
        item.pop('plan_conflicts')
    journal.save()
    resumed = RecoveryJournal(tmp_path / 'recovery.json', {'case': 'synthetic'})
    args = {'tasks': [task('T38')]}
    resumed.observe_native_feedback('plan_formula_tasks', args, '{"status":"PLANNED","passed":41}', True)
    assert resumed.state['native_repairs']
    apply_plan(ws, resumed, args['tasks'])
    assert not resumed.state['native_repairs']


def test_multiple_conflicts_are_resolved_independently(tmp_path):
    ws, journal = setup_failure(tmp_path, existing=[task('old_a'), task('old_b', '/pool')],
                                duplicate=[task('new_a'), task('new_b', '/pool')])
    apply_plan(ws, journal, [task('old_a')])
    pending = list(journal.state['native_repairs'].values())
    assert len(pending) == 1 and pending[0]['objects'] == ['new_b']
    assert [row['target'] for row in pending[0]['plan_conflicts']] == ['/pool']
    apply_plan(ws, journal, [task('old_b', '/pool')])
    assert not journal.state['native_repairs']


def test_canonical_pointer_identity_is_checked_before_issuing_unique_receipt(tmp_path):
    ws, journal = setup_failure(tmp_path, existing=[task('old', '/tables/a~2')],
                                duplicate=[task('new', '/tables/a~02')])
    apply_plan(ws, journal, [task('old', '/tables/a~02')])
    assert not journal.state['native_repairs']
