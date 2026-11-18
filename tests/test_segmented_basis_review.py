"""Review independently in bounded calls and persist only complete, blind evidence."""

import json

import pytest

from bonus_agent import case
from bonus_agent.agent import ToolError, Workspace
from bonus_agent.llm import FakeLLM, LLMError
from tests.test_staged_workflow import make_workspace, population_spec, prepared


def review_workspace(tmp_path):
    def change_sales(workbook):
        workbook['projects']['B2'] = 654321.91

    ws = make_workspace(tmp_path, mutate_workbook=change_sales)
    ws.said = '财务确认：本批先按项目计算。'
    ws.notes['参考答案'] = 'PRIVATE_ANSWER_SENTINEL'
    ws.drafts['S']['pool']['expr'] = '((amount) * (0.1))'
    ws.drafts['S']['report']['details'][0]['desc'] = 'PRIVATE_REPORT_DESCRIPTION'
    prepared(ws)
    ws.calculate_basis('S')
    return ws


def responses():
    scopes = population_spec()
    return [
        {'populations': {key: scopes[key] for key in ('项目', 'rows:projects')}, 'unclear': []},
        {'pool': 'amount / 10', 'helpers': {}, 'tables': {}, 'unclear': []},
        {'details': {'项目奖': 'amount / 10'},
         'totals': {'项目奖合计': 'sum(amount, of: "projects") / 10'}, 'unclear': []},
    ]


def attach_checker(ws, declarations):
    checker = FakeLLM([json.dumps(item, ensure_ascii=False) for item in declarations], name='independent')
    checker.basis_review_split = True
    ws.llm.checker_llm = checker
    return checker


def test_restore_retries_only_incomplete_report_without_production_values(tmp_path):
    ws = review_workspace(tmp_path)
    population, pool, reports = responses()
    bad_report = {**reports, 'totals': {}}
    checker = attach_checker(ws, [population, pool, bad_report])
    with pytest.raises(ToolError):
        ws.review_basis('S')
    assert len(checker.calls) == 3 and not ws._flow().current('review:S')

    clone = Workspace(FakeLLM([]), ws.inv)
    case.restore(clone, json.loads(json.dumps(case.snapshot(ws), ensure_ascii=False)))
    clone.llm.checker_required = True
    resumed_checker = attach_checker(clone, [reports])
    result = clone.review_basis('S')
    assert result['status'] == 'PASS' and clone._flow().current('review:S')
    assert not clone._flow().current('approved:S')
    assert clone.review_basis('S')['cached'] and len(resumed_checker.calls) == 1

    submitted = checker.calls + resumed_checker.calls
    total = ws.workflow['basis']['S']['pool']
    assert total == '65452.19'
    for request in submitted:
        text = '\n'.join(msg['content'] for msg in request)
        assert '((amount) * (0.1))' not in text
        assert 'PRIVATE_ANSWER_SENTINEL' not in text and 'PRIVATE_REPORT_DESCRIPTION' not in text
        assert total not in text


def test_partially_invalid_populations_resume_only_remaining_label(tmp_path):
    ws = review_workspace(tmp_path)
    population, pool, reports = responses()
    rows = population['populations']['rows:projects']
    bad = {**population, 'populations': {**population['populations'], 'rows:projects': {**rows, 'period': 'PRIVATE_BAD_VALUE'}}}
    first = attach_checker(ws, [bad])
    with pytest.raises(ToolError, match='period_field'):
        ws.review_basis('S')
    assert len(first.calls) == 1 and not ws._flow().current('review:S')
    clone = Workspace(FakeLLM([]), ws.inv)
    case.restore(clone, json.loads(json.dumps(case.snapshot(ws), ensure_ascii=False)))
    clone.llm.checker_required = True
    resumed = attach_checker(clone, [{'populations': {'rows:projects': rows}, 'unclear': []}, pool, reports])
    assert clone.review_basis('S')['status'] == 'PASS'
    request = resumed.calls[0]
    contract = json.loads(request[1]['content'].split('\n本段输入：')[-1])
    assert set(contract['output_template']) == set(contract['population_requirements']) == {'rows:projects'}
    assert 'period_field' in request[-1]['content'] and 'PRIVATE_BAD_VALUE' not in str(request)
    assert '项目' in clone.workflow['references']['S']['spec']['populations']


def test_report_cannot_override_independent_pool_or_exceed_retry_limit(tmp_path):
    ws = review_workspace(tmp_path)
    population, pool, reports = responses()
    bad = {**reports, 'pool': '0'}
    checker = attach_checker(ws, [population, pool, bad, bad])
    for _ in range(3):
        with pytest.raises(ToolError):
            ws.review_basis('S')
    assert len(checker.calls) == 4 and not ws._flow().current('review:S')
    assert not ws.results


def test_valid_segmented_disagreement_stays_frozen(tmp_path):
    ws = review_workspace(tmp_path)
    population, pool, reports = responses()
    checker = attach_checker(ws, [population, {**pool, 'pool': 'amount / 5'}, reports])
    assert ws.review_basis('S')['status'] == 'FAIL'
    first_calls = len(checker.calls)
    # This text is intentionally excluded from independent evidence. It cannot
    # invalidate the reviewer response or buy a chance at a friendlier one.
    ws.drafts['S']['report']['details'][0]['desc'] = '只修改内部展示说明'
    prepared(ws)
    ws.calculate_basis('S')
    repeated = ws.review_basis('S')
    assert repeated['status'] == 'FAIL' and repeated['cached']
    assert len(checker.calls) == first_calls and not ws._flow().current('review:S')


def test_unclear_population_blocks_future_slices_and_repeat_queries(tmp_path):
    ws = review_workspace(tmp_path)
    checker = attach_checker(ws, [{'unclear': ['原文未指定销售退货的业务截止期。']}])
    for _ in range(2):
        with pytest.raises(ToolError, match='截止期'):
            ws.review_basis('S')
    assert len(checker.calls) == 1 and not ws._flow().current('review:S')
    assert not ws.questions and not ws.results


def test_switching_from_monolithic_to_split_cannot_discard_independent_disagreement(tmp_path):
    ws = review_workspace(tmp_path)
    population, pool, reports = responses()
    different = {**population, **pool, **reports, 'pool': 'amount / 5'}
    checker = attach_checker(ws, [different])
    checker.basis_review_split = False
    assert ws.review_basis('S')['status'] == 'FAIL'
    checker.basis_review_split = True
    review = ws.review_basis('S')
    assert review['status'] == 'FAIL' and review['cached']
    assert len(checker.calls) == 1 and not ws._flow().current('review:S')


@pytest.mark.parametrize('other_invalid', [False, True])
def test_population_counterexample_stops_before_paid_formula_slices(tmp_path, other_invalid):
    ws = review_workspace(tmp_path)
    population, _, _ = responses()
    population['populations']['项目']['filter'] = {'pid': ['A']}
    if other_invalid:
        population['populations']['rows:projects']['window'] = 'invalid'
    checker = attach_checker(ws, [population])
    for _ in range(2):
        result = ws.review_basis('S')
        assert result['status'] == 'FAIL'
        rows = result['population']['checks']
        assert next(r['extra'] for r in rows if r['scope'] == '项目') == ['B']
    assert len(checker.calls) == 1 and not ws._flow().current('review:S')


def test_segment_failure_does_not_fall_back_to_primary(tmp_path):
    ws = review_workspace(tmp_path)
    ws.llm.checker_required = False
    checker = attach_checker(ws, [])

    def unavailable(messages):
        raise LLMError('independent review unavailable')

    checker._responder = unavailable
    ws.llm._responder = lambda messages: pytest.fail('unexpected primary fallback')
    assert ws.review_basis('S')['status'] == 'ERROR'
    assert len(checker.calls) == 1 and not ws.llm.calls and not ws._flow().current('review:S')
