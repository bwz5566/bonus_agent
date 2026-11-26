import json

from bonus_agent import case
from tests.test_staged_workflow import make_workspace, save_staged_case
from tools.inspect_commission_indicators import inspect_case, main


def test_diagnostic_rebuilds_current_indicators_without_changing_case(tmp_path, capsysbinary):
    ws = make_workspace(tmp_path)
    ws.plan_formula_tasks([{'id': 'base', 'scheme': 'S', 'target': '/variables/base',
                            'expected_scope': '项目', 'clauses': ['C1']}])
    assert ws.submit_formula_task('base', value={'scope': '项目', 'expr': 'amount'})['saved']
    ws.set_rules(scheme='S', variables={'base': {'scope': 'global', 'expr': 'sum(amount, of: "projects")'}})
    out = tmp_path / 'out'
    save_staged_case(ws, out)
    files = list(out.iterdir())
    before = case.fingerprint(files)
    assert main([str(out)]) == 1
    report = json.loads(capsysbinary.readouterr().out.decode('utf-8'))
    assert report['status'] == 'FINDINGS'
    metric = next(row for row in report['indicators'] if row['task'] == 'base')
    assert metric['task_status'] == 'FAIL' and metric['confidence']['level'] == 'CONFLICT'
    assert metric['repair_order']['items'][0]['task'] == 'base'
    assert report['confidence_distribution']['CONFLICT'] >= 1
    assert case.fingerprint(list(out.iterdir())) == before


def test_rejects_missing_or_modified_original_by_name_and_skips_old_receipts(tmp_path):
    ws = make_workspace(tmp_path)
    out = tmp_path / 'out'
    save_staged_case(ws, out)
    policy = tmp_path / 'policy.md'
    policy.write_text('已改变原文', encoding='utf8')
    report = inspect_case(out)
    assert report['status'] == 'INPUT_INVALID' and 'policy.md' in str(report['issues'])
    assert 'indicators' not in report
    policy.unlink()
    report = inspect_case(out)
    assert report['status'] == 'INPUT_INVALID' and 'policy.md' in str(report['issues'])


def test_distinguishes_unplanned_case_from_missing_data(tmp_path):
    ws = make_workspace(tmp_path)
    out = tmp_path / 'out'
    save_staged_case(ws, out)
    assert inspect_case(out)['status'] == 'PLAN_UNAVAILABLE'
    ws.plan_formula_tasks([{'id': 'pool', 'scheme': 'S', 'target': '/pool'}])
    ws.datasets.clear()
    save_staged_case(ws, out)
    assert inspect_case(out)['status'] == 'DATA_UNAVAILABLE'
