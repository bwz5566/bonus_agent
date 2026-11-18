import copy
import pytest

from bonus_agent.agent import ToolError
from bonus_agent.recovery import fingerprint
from tests.test_staged_workflow import make_workspace, prepared, reviewed


def test_compile_reports_real_runtime_failure_before_issuing_receipt(tmp_path):
    ws = make_workspace(tmp_path)
    ws.drafts['S']['pool']['expr'] = 'amount * 0.1 / (amount - 100)'
    assert ws.prepare_data()['status'] == 'PASS'
    original = copy.deepcopy(ws.drafts)
    report = ws.compile_formulas()
    assert report['status'] == 'FAIL'
    assert report['preflight']['S']['status'] == 'FAIL'
    assert any('项目:A' in issue and 'data.xlsx' in issue for issue in report['issues'])
    assert not ws._flow().current('formulas')
    assert not ws.workflow['basis'] and not ws.results
    assert ws.drafts == original and not ws.llm.calls

    ws.drafts['S']['pool']['expr'] = 'amount * 0.1'
    report = ws.compile_formulas()
    assert report['status'] == 'PASS' and report['preflight']['S']['status'] == 'PASS'
    assert ws._flow().current('formulas')
    assert not ws.workflow['basis'] and not ws.results


def test_failed_recompile_revokes_same_version_pass_and_downstream_receipts(tmp_path, monkeypatch):
    ws = reviewed(make_workspace(tmp_path))
    flow = ws._flow()
    flow.approve('S', '财务', fingerprint(ws.workflow['basis']['S']))
    version = flow.version('basis')
    monkeypatch.setattr('bonus_core.engine.preflight.preflight_basis', lambda *a, **k: {
        'status': 'FAIL', 'issues': [{'code': 'basis_preflight_eval', 'message': 'current data evaluation failed',
                                   'coverage': [{'dataset': 'projects', 'visible_rows': 2, 'selected_rows': 0}]}]})
    result = flow.compile('basis')
    assert result['status'] == 'FAIL' and flow.version('basis') == version
    assert all(not flow.current(name) for name in ('formulas', 'basis:S', 'review:S', 'approved:S'))
    with pytest.raises(ToolError):
        flow.approve('S', '财务', fingerprint(ws.workflow['basis']['S']))
    details = ws.read_workspace(section='checks', name='preflight:S')
    assert 'selected_rows' in str(details) and 'projects' in str(details)
    assert 'details_call' in result['preflight']['S']


def test_compile_exception_also_revokes_old_formula_receipt(tmp_path, monkeypatch):
    ws = prepared(make_workspace(tmp_path))
    monkeypatch.setattr(ws, 'validate_parameters', lambda **kwargs: (_ for _ in ()).throw(ValueError('invalid old evidence')))
    with pytest.raises(ValueError, match='invalid old evidence'):
        ws.compile_formulas()
    assert not ws._flow().current('formulas')
