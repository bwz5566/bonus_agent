"""Expose the real negative policy without turning execution into approval."""
import copy
from decimal import Decimal

import pytest
from pydantic import ValidationError

from bonus_agent.advice_context import build_runtime_focus_context
from bonus_agent.agent import CHAT_SOURCE, ToolError
from bonus_agent.extract_rules import Extraction
from bonus_agent.recovery import fingerprint
from bonus_agent.tool_specs import TOOLS
from bonus_core.data.table import from_records
from bonus_core.dsl.models import Pool
from bonus_core.engine.preflight import preflight_basis
from bonus_core.engine.run import Engine
from tests.test_preflight_policy_evidence import negative_case
from tests.test_staged_workflow import make_workspace

pytestmark = pytest.mark.evidence


def test_public_pool_contract_matches_actual_extraction_and_engine_options():
    contract = next(t for t in TOOLS if t['name'] == 'set_rules')['parameters']['properties']['pool']
    actual = Pool.model_json_schema()['properties']['negative']
    extracted = Extraction.model_json_schema()['$defs']['Pool']['properties']['negative']
    public = contract['properties']['negative']
    assert public['enum'] == actual['enum'] == extracted['enum']
    assert public['default'] == actual['default'] == extracted['default'] == 'block'
    assert 'expr' in contract['required']
    with pytest.raises(ValidationError):
        Pool.model_validate({'negative': 'allow'})
    rs, records = negative_case()
    before = rs.model_dump()
    blocked = preflight_basis(rs, from_records(rs, records))
    assert rs.model_dump() == before and blocked['status'] == 'FAIL'
    policy = blocked['issues'][0]['policy']
    assert policy == {'target': '/pool/negative', 'actual_policy': 'block',
                      'default': 'block', 'supported_options': public['enum']}
    for option in public['enum']:
        candidate = rs.model_copy(deep=True)
        candidate.pool = Pool.model_validate({**before['pool'], 'negative': option})
        data = from_records(candidate, records)
        report = preflight_basis(candidate, data)
        assert report['status'] == ('PASS' if option == 'allow' else 'FAIL')
        if option == 'allow':
            basis = Engine.prepare_basis(candidate, data).calculate_basis()
            assert Decimal(basis['objects']['projects:202601-A']['amount']) == Decimal('-5')
            assert Decimal(basis['pool']) == Decimal('5')
            assert 'not_business_or_amount_approval' in report['execution_evidence']['policy_success_scope']


def test_existing_pool_task_changes_preserve_formula_and_verified_sources(tmp_path):
    ws = make_workspace(tmp_path)
    ws.prepare_data()
    ws.said = '本批退货冲销为负数时照算，保留负奖金。'
    ws.plan_formula_tasks([{'id': 'pool-task', 'scheme': 'S', 'target': '/pool',
                           'facts': ['提成率'], 'clauses': ['C1']}])
    before = copy.deepcopy(ws.drafts['S']['pool'])
    sources = [*before['sources'], {'doc': CHAT_SOURCE, 'quote': ws.said}]
    result = ws.submit_formula_task('pool-task', changes={'negative': 'allow', 'sources': sources})
    assert result['saved'] and result['status'] == 'PASS', result
    assert ws.drafts['S']['pool'] == {**before, 'negative': 'allow', 'sources': sources}
    assert not ws.results and not ws._flow().current('review:S') and not ws._flow().current('approved:S')
    assert not ws.llm.calls


def test_local_pool_repair_keeps_negative_policy_and_the_upstream_source(tmp_path):
    quote = '退货记录为负数时照算，保留负奖金。'

    def refund(book):
        book['projects']['B2'] = -100

    ws = make_workspace(tmp_path, mutate_workbook=refund, policy_tail='\n' + quote)
    ws.record_clauses([{'file': 'policy.md', 'quote': quote, 'point': '保留退货负奖金'}])
    clause = next(c.id for c in ws.clauses if c.quote == quote)
    assert ws.prepare_data()['status'] == 'PASS'
    ws.plan_formula_tasks([{'id': 'refund', 'scheme': 'S', 'target': '/variables/refund_amount', 'clauses': [clause]},
                           {'id': 'pool-task', 'scheme': 'S', 'target': '/pool'}])
    assert ws.submit_formula_task('refund', value={'scope': '项目', 'expr': 'amount'})['status'] == 'PASS'
    result = ws.submit_formula_task('pool-task', changes={'expr': 'refund_amount * 0.1'})
    assert result['status'] == 'FAIL' and not result['saved']
    repair = result['execution']['repair']
    assert repair['policy']['target'] == '/pool/negative'
    assert repair['policy']['actual_policy'] == 'block'
    assert repair['policy_sources'] == [{'id': clause, 'file': 'policy.md', 'quote': quote}]
    assert any('submit_formula_task' in instruction for instruction in repair['guidance'])
    assert ws._ruleset('S').pool.negative == 'block' and not ws.results


def test_focus_exposes_negative_policy_but_allow_does_not_approve_a_wrong_amount(tmp_path):
    ws = make_workspace(tmp_path)
    pool = copy.deepcopy(ws.drafts['S']['pool'])
    # Deliberately introduce an incorrect sign. An execution option must not
    # replace the independent review of the original positive-rate formula.
    pool['expr'] = '-amount * 0.1'
    ws.set_rules(scheme='S', pool=pool)
    assert ws.prepare_data()['status'] == 'PASS'
    assert ws.compile_formulas()['status'] == 'FAIL'
    packet = build_runtime_focus_context(ws)
    assert packet['failure']['policy']['actual_policy'] == 'block'
    assert packet['failure']['policy']['supported_options'] == ['block', 'allow']
    assert packet['failure']['repair'] == ws.workflow['formula_report']['report']['preflight']['S']['issues'][0]['repair']
    assert any('submit_formula_task' in step and 'sources' in step for step in packet['failure']['repair'])
    ws.set_rules(scheme='S', pool={**pool, 'negative': 'allow'})
    assert ws.compile_formulas()['status'] == 'PASS'
    ws.calculate_basis('S')
    assert ws.review_basis('S')['status'] == 'FAIL'
    assert not ws._flow().current('review:S')
    with pytest.raises(ToolError):
        ws._flow().approve('S', '财务', fingerprint(ws.workflow['basis']['S']))
    with pytest.raises(ToolError):
        ws.trial_run('S')
    assert not ws.results


def test_oversized_failure_still_delivers_policy_and_conditional_repair(tmp_path):
    from tests.test_runtime_focus_context import example
    ws, report = example(tmp_path)
    rs, records = negative_case()
    issue = preflight_basis(rs, from_records(rs, records))['issues'][0]
    original = report['preflight']['S']['issues'][0]
    original.update(policy=issue['policy'], repair=issue['repair'], message='x' * 20000)
    packet = build_runtime_focus_context(ws, report)
    assert 'message' not in packet['failure'] and packet['limits']['truncated']
    assert packet['failure']['policy'] == issue['policy']
    assert packet['failure']['repair'] == issue['repair']
