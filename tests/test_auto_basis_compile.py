"""Ready basis tasks advance to deterministic preflight without another model decision."""
import copy
import json

import pytest

from bonus_agent import case
from bonus_agent.agent import Workspace, ToolError, run_agent
from bonus_agent.auto import build_inventory
from bonus_agent.construction import TaskBoard
from bonus_agent.context import task_card
from bonus_agent.llm import FakeLLM
from bonus_agent.workflow import Workflow
from tests.test_staged_workflow import make_workspace, save_staged_case, _staged_llm


pytestmark = pytest.mark.evidence


def ready_workspace(tmp_path, *, unbound=False, missing_weight=False):
    ws = make_workspace(tmp_path)
    if missing_weight:
        ws.drafts['S']['allocation'][-1].pop('weight')
    assert ws.prepare_data()['status'] == 'PASS'
    TaskBoard(ws).adopt()
    assert not TaskBoard(ws).gate('basis')
    if unbound:
        ws.facts.append(ws.facts[0].model_copy(deep=True, update={'name': '待绑定提成率', 'targets': []}))
    return ws


def count_compilations(monkeypatch):
    calls, original = [], Workflow._compile
    def counted(self, phase):
        calls.append(phase)
        return original(self, phase)
    monkeypatch.setattr(Workflow, '_compile', counted)
    return calls


def test_first_ready_basis_compiles_once_without_downstream_weight_or_model(tmp_path, monkeypatch):
    ws = ready_workspace(tmp_path, missing_weight=True)
    calls = count_compilations(monkeypatch)
    assert 'formulas' not in ws.workflow['receipts']
    assert ws._flow().recheck(initial_compile=True) == [('compile_formulas', True, [])]
    assert ws._flow().stage() == (4, 'S')
    assert ws._flow().recheck(initial_compile=True) == []
    assert calls == ['basis']
    assert not ws.llm.calls and not ws.llm.checker_llm.calls
    assert not ws.workflow['basis'] and not ws.results
    assert not any(n.startswith(('review:', 'approved:')) or n == 'allocation_formulas'
                   for n in ws.workflow['receipts'])


@pytest.mark.parametrize('gap', ['pending', 'coverage', 'structure'])
def test_incomplete_or_invalid_task_inputs_do_not_trigger_initial_compile(tmp_path, monkeypatch, gap):
    ws = ready_workspace(tmp_path)
    board = TaskBoard(ws)
    pool = next(n for n, spec in board.state['tasks'].items() if spec['target'] == '/pool')
    if gap == 'pending':
        board.state['receipts'].pop(pool)
    elif gap == 'coverage':
        board.state['tasks'].pop(pool)
    else:
        ws.datasets['people'].key = None
        ws.datasets['people'].link = 'pid'
    calls = count_compilations(monkeypatch)
    checked = ws._flow().recheck(initial_compile=True)
    if gap == 'structure':
        assert checked and checked[0][:2] == ('prepare_data', False)
        assert any('实体表' in issue for issue in checked[0][2])
    else:
        assert checked == []
    assert calls == []
    assert not ws._flow().current('formulas')


def test_current_failure_is_visible_cached_and_survives_workspace_restore(tmp_path, monkeypatch):
    ws = ready_workspace(tmp_path, unbound=True)
    calls = count_compilations(monkeypatch)
    checked = ws._flow().recheck(initial_compile=True)
    assert checked[0][:2] == ('compile_formulas', False)
    assert any('待绑定提成率' in issue for issue in checked[0][2])
    card = json.loads(task_card(ws))
    assert '待绑定提成率' in card['workflow']['next']
    assert card['workflow']['construction']['next'] != 'compile_formulas'
    assert card['workflow']['construction']['compile_failure']['status'] == 'FAIL'
    for _ in range(3):
        assert ws._flow().recheck(initial_compile=True) == []
        assert ws.compile_formulas()['cached'] is True
        ws.read_workspace('checks')
    clone = Workspace(FakeLLM([]), ws.inv)
    case.restore(clone, copy.deepcopy(case.snapshot(ws)))
    assert clone._flow().recheck(initial_compile=True) == []
    assert '待绑定提成率' in clone._flow().hint()
    assert calls == ['basis']


def test_binding_change_allows_one_new_check_then_stops_at_amount_stage(tmp_path, monkeypatch):
    ws = ready_workspace(tmp_path, unbound=True)
    calls = count_compilations(monkeypatch)
    assert ws._flow().recheck(initial_compile=True)[0][1] is False
    ws.bind_facts([{'name': '待绑定提成率', 'targets': list(ws.facts[0].targets)}])
    assert ws._flow().recheck(initial_compile=True) == [('compile_formulas', True, [])]
    assert ws._flow().recheck(initial_compile=True) == []
    assert calls == ['basis', 'basis']
    assert ws._flow().stage() == (4, 'S')


def test_early_manual_failure_is_invalidated_by_the_missing_local_pass(tmp_path, monkeypatch):
    ws = ready_workspace(tmp_path)
    board = TaskBoard(ws)
    pool = next(n for n, spec in board.state['tasks'].items() if spec['target'] == '/pool')
    board.state['receipts'].pop(pool)
    calls = count_compilations(monkeypatch)
    report = ws.compile_formulas()
    assert report['status'] == 'FAIL'
    assert any('construction.task' in issue for issue in report['issues'])
    assert board.check(pool)['status'] == 'PASS'
    assert ws._flow().recheck(initial_compile=True) == [('compile_formulas', True, [])]
    assert calls == ['basis', 'basis']


def test_final_local_pass_compiles_before_the_next_model_request(tmp_path, monkeypatch):
    ws = ready_workspace(tmp_path, unbound=True)
    board = TaskBoard(ws)
    pool = next(n for n, spec in board.state['tasks'].items() if spec['target'] == '/pool')
    board.state['receipts'].pop(pool)
    out = tmp_path / 'out'
    save_staged_case(ws, out)
    calls = count_compilations(monkeypatch)
    primary_calls = []
    def script(messages, *unused):
        primary_calls.append(messages)
        if len(primary_calls) == 1:
            return {'tool': 'check_formula_task', 'args': {'task': pool}}
        assert calls == ['basis']
        assert '待绑定提成率' in json.dumps(messages, ensure_ascii=False)
        return {'tool': 'read_workspace', 'args': {'section': 'checks'}}
    llm = _staged_llm(ws, script)
    ar = run_agent(llm, ws.inv.files, out, max_steps=2)
    automatic = [s for s in ar.steps if s.tool == '（自动复核）']
    assert len(automatic) == 1 and not automatic[0].ok
    assert automatic[0].args == {'tool': 'compile_formulas', 'automatic': True, 'phase': 'basis'}
    assert json.loads(automatic[0].result)['phase'] == 'basis'
    assert all(s.tool != '（进入新阶段）' for s in ar.steps)
    assert len(primary_calls) == 2 and not llm.checker_llm.calls
    assert calls == ['basis']


def test_resume_runs_ready_compile_before_first_model_request(tmp_path, monkeypatch):
    ws = ready_workspace(tmp_path, unbound=True)
    out = tmp_path / 'out'
    save_staged_case(ws, out)
    calls = count_compilations(monkeypatch)
    def script(messages, *unused):
        assert calls == ['basis']
        assert '待绑定提成率' in json.dumps(messages, ensure_ascii=False)
        return {'tool': 'read_workspace', 'args': {'section': 'checks'}}
    ar = run_agent(_staged_llm(ws, script), ws.inv.files, out, max_steps=1)
    assert ar.steps[0].tool == '（自动复核）'
    assert not ar.steps[0].ok and calls == ['basis']


def test_new_material_cannot_bypass_the_inventory_gate(tmp_path, monkeypatch):
    ws = ready_workspace(tmp_path)
    saved = copy.deepcopy(case.snapshot(ws))
    extra = tmp_path / '补充说明.md'
    extra.write_text('新增规则须重新核对。', encoding='utf8')
    clone = Workspace(FakeLLM([]), build_inventory([*ws.inv.files, extra]))
    case.restore(clone, saved)
    calls = count_compilations(monkeypatch)
    assert clone._flow().stage()[0] == 0
    assert clone._flow().recheck(initial_compile=True) == []
    assert not calls


def test_live_source_change_revokes_prior_compilation_before_returning_error(tmp_path):
    ws = ready_workspace(tmp_path)
    assert ws._flow().recheck(initial_compile=True)[0][1] is True
    policy = tmp_path / 'policy.md'
    policy.write_text(policy.read_text(encoding='utf8') + '\n新增范围要求。', encoding='utf8')
    with pytest.raises(ToolError, match='变化|改变|重新|修改'):
        ws.compile_formulas()
    assert not ws._flow().current('formulas')
