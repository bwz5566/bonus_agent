"""Independent review retries repair invalid declarations, never resample disagreements."""
import copy
import json

import pytest

from bonus_agent.agent import ToolError
from bonus_agent.llm import FakeLLM, LLMError
from tests.test_staged_workflow import make_workspace, prepared


def setup_review(tmp_path):
    ws = prepared(make_workspace(tmp_path))
    ws.calculate_basis('S')
    good = json.loads(ws.llm.checker_llm._responder[0])
    return ws, good


def invalid_population(good):
    bad = copy.deepcopy(good)
    for item in bad['populations'].values():
        item.pop('filter')
    return bad


def test_invalid_population_gets_its_own_spec_and_diagnostic_with_same_attempt_budget(tmp_path):
    ws, good = setup_review(tmp_path)
    bad = invalid_population(good)
    checker = FakeLLM([json.dumps(bad), json.dumps(good)], name='checker')
    ws.llm.checker_llm = checker
    with pytest.raises(ToolError, match='filter'):
        ws.review_basis('S')
    first = copy.deepcopy(ws.workflow['references']['S'])
    assert first['spec_state'] == 'invalid' and first['spec'] == bad
    assert not ws._flow().current('review:S')
    assert ws.review_basis('S')['status'] == 'PASS'
    assert len(checker.calls) == 2 and list(ws.workflow['attempts'].values()) == [2]
    assert checker.calls[0] == checker.calls[1][:-2]
    assert json.loads(checker.calls[1][-2]['content']) == bad
    assert first['diagnostic'] in checker.calls[1][-1]['content']
    assert first['input_signature'] == ws.workflow['references']['S']['input_signature']
    assert ws.workflow['references']['S']['spec_state'] == 'valid'
    assert ws.review_basis('S')['cached'] and len(checker.calls) == 2


def test_two_invalid_attempts_still_block_a_third_model_call(tmp_path):
    ws, good = setup_review(tmp_path)
    checker = FakeLLM([json.dumps(invalid_population(good))])
    ws.llm.checker_llm = checker
    for _ in range(2):
        with pytest.raises(ToolError, match='filter'):
            ws.review_basis('S')
    with pytest.raises(ToolError, match='已达预算'):
        ws.review_basis('S')
    assert len(checker.calls) == 2 and list(ws.workflow['attempts'].values()) == [2]
    assert not ws._flow().current('review:S')


@pytest.mark.parametrize('bad', ['not json', '{"pool":'])
def test_invalid_json_is_repaired_with_original_output_and_parser_error(tmp_path, bad):
    ws, good = setup_review(tmp_path)
    checker = FakeLLM([bad, json.dumps(good)])
    ws.llm.checker_llm = checker
    with pytest.raises(ToolError, match='无法解析'):
        ws.review_basis('S')
    assert ws.review_basis('S')['status'] == 'PASS'
    assert checker.calls[1][-2]['content'] == bad
    assert 'JSON' in checker.calls[1][-1]['content']
    assert list(ws.workflow['attempts'].values()) == [2]


def test_invalid_independent_formula_gets_program_feedback(tmp_path):
    ws, good = setup_review(tmp_path)
    bad = copy.deepcopy(good)
    bad['pool'] = 'unknown_independent_field'
    checker = FakeLLM([json.dumps(bad), json.dumps(good)])
    ws.llm.checker_llm = checker
    with pytest.raises(ToolError, match='unknown_independent_field'):
        ws.review_basis('S')
    assert ws.review_basis('S')['status'] == 'PASS'
    assert 'unknown_independent_field' in checker.calls[1][-1]['content']


def test_valid_disagreement_is_frozen_without_requesting_a_more_favorable_formula(tmp_path):
    ws, good = setup_review(tmp_path)
    different = copy.deepcopy(good)
    different['pool'] = 'amount * 0.2'
    checker = FakeLLM([json.dumps(different), json.dumps(good)], name='independent')
    ws.llm.checker_llm = checker
    assert ws.review_basis('S')['status'] == 'FAIL'
    assert ws.workflow['references']['S']['spec_state'] == 'valid'
    second = ws.review_basis('S')
    assert second['status'] == 'FAIL' and second['cached']
    assert second['by'] == 'independent' and len(checker.calls) == 1


def test_switching_checker_changes_signature_and_records_the_model_that_ran(tmp_path):
    ws, good = setup_review(tmp_path)
    different = copy.deepcopy(good)
    different['pool'] = 'amount * 0.2'
    old = FakeLLM([json.dumps(different)], name='old-checker')
    ws.llm.checker_llm = old
    assert ws.review_basis('S')['by'] == 'old-checker'
    old_signature = ws.workflow['references']['S']['input_signature']
    new = FakeLLM([json.dumps(good)], name='new-checker')
    ws.llm.checker_llm = new
    result = ws.review_basis('S')
    assert result['status'] == 'PASS' and not result['cached']
    assert result['by'] == result['reviewer']['name'] == 'new-checker'
    assert len(old.calls) == len(new.calls) == 1
    assert old_signature != ws.workflow['references']['S']['input_signature']
    assert sorted(ws.workflow['attempts'].values()) == [1, 1]


def test_same_checker_thinking_change_invalidates_reference_cache(tmp_path):
    ws, good = setup_review(tmp_path)
    different = copy.deepcopy(good)
    different['pool'] = 'amount * 0.2'
    checker = FakeLLM([json.dumps(different), json.dumps(good)], name='checker')
    ws.llm.checker_llm = checker
    assert ws.review_basis('S')['status'] == 'FAIL'
    old_signature = ws.workflow['references']['S']['input_signature']
    checker.think_body = {'reasoning_effort': 'high'}
    assert ws.review_basis('S')['status'] == 'PASS'
    assert old_signature != ws.workflow['references']['S']['input_signature']
    assert len(checker.calls) == 2


def test_larger_review_output_retries_truncation_without_erasing_old_attempts(tmp_path):
    ws, good = setup_review(tmp_path)
    checker = FakeLLM(['{"pool":', '{"pool":', json.dumps(good)], name='checker')
    checker.think_body = {'thinking': {'type': 'enabled'}}
    checker.think_max_tokens = 8192
    ws.llm.checker_llm = checker
    for _ in range(2):
        with pytest.raises(ToolError, match='无法解析'):
            ws.review_basis('S')
    old_signature = ws.workflow['references']['S']['input_signature']
    with pytest.raises(ToolError, match='已达预算'):
        ws.review_basis('S')
    checker.think_max_tokens = 16384
    assert ws.review_basis('S')['status'] == 'PASS'
    assert old_signature != ws.workflow['references']['S']['input_signature']
    assert sorted(ws.workflow['attempts'].values()) == [1, 2]
    assert len(checker.calls) == 3
    assert len(checker.calls[2]) == len(checker.calls[0])
    assert ws.review_basis('S')['cached'] and len(checker.calls) == 3


def test_discovered_provider_metadata_does_not_resample_a_valid_disagreement(tmp_path):
    ws, good = setup_review(tmp_path)
    different = copy.deepcopy(good)
    different['pool'] = 'amount * 0.2'
    checker = FakeLLM([json.dumps(different), json.dumps(good)], name='checker')
    ws.llm.checker_llm = checker
    assert ws.review_basis('S')['status'] == 'FAIL'
    old_signature = ws.workflow['references']['S']['input_signature']
    checker._ctx, checker._max_output_tokens = 128000, 32000
    checker._context_token_ratio = 1.1
    again = ws.review_basis('S')
    assert again['status'] == 'FAIL' and again['cached']
    assert ws.workflow['references']['S']['input_signature'] == old_signature
    assert len(checker.calls) == 1


def test_new_checker_cannot_use_an_old_pass_fast_path(tmp_path):
    ws, good = setup_review(tmp_path)
    assert ws.review_basis('S')['status'] == 'PASS'
    assert ws._flow().current('review:S')
    different = copy.deepcopy(good)
    different['pool'] = 'amount * 0.2'
    new = FakeLLM([json.dumps(different)], name='new-checker')
    ws.llm.checker_llm = new
    result = ws.review_basis('S')
    assert result['status'] == 'FAIL' and result['by'] == 'new-checker'
    assert not ws._flow().current('review:S') and len(new.calls) == 1


def test_unclear_business_evidence_stays_frozen_until_input_changes(tmp_path):
    ws, good = setup_review(tmp_path)
    unresolved = copy.deepcopy(good)
    unresolved['unclear'] = ['Policy does not specify the required cutoff.']
    checker = FakeLLM([json.dumps(unresolved), json.dumps(good)])
    ws.llm.checker_llm = checker
    for _ in range(2):
        with pytest.raises(ToolError, match='未明确口径'):
            ws.review_basis('S')
    assert len(checker.calls) == 1
    assert ws.workflow['references']['S']['spec_state'] == 'unresolved'


def test_optional_fallback_and_its_cached_report_keep_actual_reviewer_identity(tmp_path):
    ws, good = setup_review(tmp_path)
    ws.llm.checker_required = False
    ws.llm.name = ws.llm.model = 'primary-fallback'
    ws.llm._responder = [json.dumps(good)]
    def unavailable(*args, **kwargs):
        raise LLMError('temporary checker outage')
    ws.llm.checker_llm.chat = unavailable
    result = ws.review_basis('S')
    assert result['status'] == 'PASS' and result['by'] == 'primary-fallback'
    assert 'temporary checker outage' in result['reviewer']['fallback_reason']
    cached = ws.review_basis('S')
    assert cached['cached'] and cached['by'] == 'primary-fallback'
    assert len(ws.llm.calls) == 1
