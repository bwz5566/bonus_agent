"""Confidence from actual construction evidence, scoped repairs and version changes."""
import pytest

from bonus_agent.construction import TaskBoard
from tests.test_staged_workflow import make_workspace


pytestmark = pytest.mark.evidence


def task(name, target, *deps):
    return {'id': name, 'scheme': 'S', 'target': target, 'depends_on': list(deps)}


def test_actual_data_revokes_before_data_pass_and_confidence_survives_only_current_version(tmp_path):
    ws = make_workspace(tmp_path)
    ws.plan_formula_tasks([task('rate', '/variables/first_amount'), task('fund', '/pool', 'rate')])
    value = {'scope': 'global', 'expr': 'single_value(amount, of: "projects")',
             'sources': ws.drafts['S']['pool']['sources'], 'confidence': 1}
    before = ws.submit_formula_task('rate', value=value)
    assert before['saved']
    dimensions = before['confidence']['dimensions']
    assert dimensions['execution']['state'] == dimensions['independent']['state'] == 'UNKNOWN'
    assert dimensions['rule_source']['state'] == 'VERIFIED'
    assert ws.prepare_data()['status'] == 'PASS'
    board = TaskBoard(ws)
    refreshed = board.refresh()
    assert refreshed['rate']['status'] == 'PENDING'
    assert refreshed['fund']['status'] == 'WAITING'
    assert refreshed['rate']['confidence_level'] == 'CONFLICT'
    assert refreshed['rate']['confidence']['dimensions']['execution']['state'] == 'CONFLICT'
    assert board.gate('basis')
    read, = ws.read_workspace('construction', name='rate')['items']
    assert read['confidence']['level'] == 'CONFLICT'
    assert read['indicator_findings'][0]['selected_rows'] == 2
    ws.set_rules(scheme='S', variables={'first_amount': {'scope': '项目', 'expr': 'amount'}})
    reset = board.statuses()['rate']
    assert reset['confidence_level'] != 'CONFLICT' and not reset.get('indicator_findings')
    assert reset['confidence']['dimensions']['rule_source']['state'] == 'UNKNOWN'
    changed, = ws.read_workspace('construction', name='rate')['items']
    assert changed['report_current'] is False and changed['report'] is None
    assert changed['previous_report']['status'] == 'FAIL'
    assert ws.check_formula_task('rate')['status'] == 'PASS'
    assert not ws.results and not ws.workflow['basis']


def test_failed_candidate_orders_direct_counterexample_and_only_affected_dependencies(tmp_path):
    def split(wb):
        wb['people']['D3'] = .25
        wb['people']['D4'] = .75

    ws = make_workspace(tmp_path, mutate_workbook=split)
    ws.prepare_data()
    ws.plan_formula_tasks([task('input', '/variables/source_share'),
                           task('factor', '/variables/per_project', 'input'),
                           task('other', '/variables/unrelated'), task('fund', '/pool')])
    ws.submit_formula_task('input', value={'scope': '项目', 'expr': 'amount'})
    ws.submit_formula_task('factor', value={'scope': '项目', 'expr': 'single_value(share, of: "people")'})
    failed = ws.submit_formula_task('fund', changes={'expr': 'amount * 0.1 * per_project'})
    assert not failed['saved'] and failed['execution']['repair']['task'] == 'factor'
    trace = failed['repair_order']
    assert [(r['task'], r['distance']) for r in trace['items']] == [('factor', 0), ('input', 1)]
    assert trace['items'][0]['level'] == 'CONFLICT'
    assert 'single_value.multiple' in trace['items'][0]['reason']
    assert TaskBoard(ws).statuses()['factor']['confidence_level'] != 'CONFLICT'
    assert not ws.workflow['basis'] and not ws.results


def test_direct_formula_syntax_error_has_a_current_counterexample(tmp_path):
    ws = make_workspace(tmp_path)
    ws.plan_formula_tasks([task('a', '/variables/output_value'), task('fund', '/pool', 'a')])
    failed = ws.submit_formula_task('a', value={'scope': '项目', 'expr': 'unknown_field'})
    assert not failed['saved']
    assert failed['confidence']['level'] == 'CONFLICT'
    assert 'name_unknown' in failed['confidence']['counterexamples'][0]
    assert failed['confidence']['dimensions']['independent']['state'] == 'UNKNOWN'


def test_actual_grain_lead_is_investigated_before_inputs_with_only_generic_gaps(tmp_path):
    ws = make_workspace(tmp_path)
    ws.prepare_data()
    ws.plan_formula_tasks([task('sum', '/variables/all_amount'), task('other', '/variables/other_amount'),
                           task('by_project', '/variables/scoped_amount', 'sum', 'other'),
                           task('fund', '/pool', 'by_project')])
    ws.set_rules(scheme='S', variables={
        'all_amount': {'scope': 'global', 'expr': 'sum(amount, of: "projects")'},
        'scoped_amount': {'scope': '项目', 'expr': 'all_amount'},
    })
    board = TaskBoard(ws)
    board.inspect_indicators('S')
    statuses = board.statuses()
    assert statuses['sum']['confidence']['dimensions']['execution']['state'] == 'VERIFIED'
    assert statuses['sum']['confidence']['dimensions']['grain']['state'] == 'PARTIAL'
    assert statuses['sum']['confidence_level'] != 'HIGH'
    ordered = board.repair_order('by_project', statuses)['items']
    assert [row['task'] for row in ordered] == ['by_project', 'sum', 'other']
    assert '全局聚合' in ordered[1]['reason']
