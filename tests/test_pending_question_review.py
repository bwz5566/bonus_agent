"""A source-reviewed withdrawal never becomes a fabricated financial answer."""
import copy
import json

import pytest

from bonus_agent.agent import ToolError
from bonus_agent.extract_rules import Question
from bonus_agent.llm import FakeLLM, LLMError
from test_t3_fixes import _ws, _sales


RULE = '本期只核算本期销售单中的项目；全年明细用于历史统计，不把历史项目当成本期对象。'
EVIDENCE = [{'file': '规则.txt', 'quote': RULE}]


def setup(tmp_path, response):
    ws = _ws(tmp_path, RULE)
    ws.questions = [Question(id='q1', text='全年项目没有本期日期，是否全部视为新项目？')]
    ws.llm.checker_llm = FakeLLM([response] if isinstance(response, str) else response, name='independent')
    ws.workflow['receipts'] = {'approved:S': {'unchanged': True}}
    return ws


def opinion(**changes):
    return json.dumps({'decision': 'withdraw', 'kind': 'false_premise',
                       'reason': '原文只核算本期项目，问题却要求给全部历史项目补本期属性。',
                       'evidence': EVIDENCE, **changes}, ensure_ascii=False)


@pytest.mark.parametrize('resolve', [True, False])
def test_question_raised_from_disputed_amount_gets_one_source_review(tmp_path, resolve):
    ws = setup(tmp_path, opinion() if resolve else opinion(decision='keep', kind='unresolved'))
    ws.workflow.update(enabled=True, references={'S': {'spec_state': 'amount_difference'}})
    ws.questions.clear()
    ws._source_skip = True
    receipts = copy.deepcopy(ws.workflow['receipts'])
    text = '全年项目没有本期日期，是否全部视为新项目？'
    result = ws.ask_question(text, related='S.variables.当期项目', evidence=EVIDENCE)
    q = ws.questions[0]
    assert bool(q.withdrawn) is resolve and bool(ws.pending) is not resolve
    assert not q.answer and not q.answered_by and ws.workflow['receipts'] == receipts
    assert len(ws.llm.checker_llm.calls) == 1
    again = ws.ask_question(text, related=q.related, evidence=EVIDENCE)
    assert len(ws.questions) == 1 and len(ws.llm.checker_llm.calls) == 1
    assert q.id in result and q.id in again


def test_withdrawal_keeps_original_question_and_never_writes_answer_or_approval(tmp_path):
    ws = setup(tmp_path, opinion())
    receipts = copy.deepcopy(ws.workflow['receipts'])
    before = ws.questions[0].model_dump()
    out = ws.review_question('q1', '请核对全年与本期范围是否混淆', EVIDENCE)
    assert out['status'] == 'WITHDRAWN' and not ws.pending
    q = ws.questions[0]
    assert q.answer is None and not q.answered_by and not q.answered_at
    assert q.text == before['text'] and q.withdrawn
    assert ws.workflow['receipts'] == receipts
    audit = ws.workflow['question_reviews']['q1']
    assert audit['question'] == before and audit['verified_evidence'][0]['ok']
    assert ws.review_question('q1', '再次询问', EVIDENCE)['cached']
    assert len(ws.llm.checker_llm.calls) == 1


@pytest.mark.parametrize('bad', [
    'not json', opinion(evidence=[{'file': '规则.txt', 'quote': '所有历史项目都可以忽略不用核算'}]),
    opinion(kind='financial_approval'), opinion(evidence=[]), opinion(reason=''),
    opinion(evidence=[{}]), opinion(decision='approve'),
])
def test_invalid_review_keeps_pending_and_cannot_resample_by_rephrasing(tmp_path, bad):
    ws = setup(tmp_path, bad)
    out = ws.review_question('q1', '可能有误', EVIDENCE)
    assert out['status'] == 'KEEP' and ws.pending and not ws.questions[0].withdrawn
    again = ws.review_question('q1', '换个理由再问', EVIDENCE)
    assert again['cached'] and len(ws.llm.checker_llm.calls) == 1


def test_independent_disagreement_is_cached_but_changed_source_can_be_reviewed(tmp_path):
    ws = setup(tmp_path, opinion(decision='keep', kind='unresolved', reason='尚不能确定具体核算对象'))
    assert ws.review_question('q1', '请核对', EVIDENCE)['status'] == 'KEEP'
    assert ws.review_question('q1', '再核对一次', EVIDENCE)['cached']
    assert len(ws.llm.checker_llm.calls) == 1
    ws.said = '新增原文：本期销售单是完整的核算对象清单。'
    assert not ws.review_question('q1', '已有新增原文', EVIDENCE)['cached']
    assert len(ws.llm.checker_llm.calls) == 2 and ws.pending


@pytest.mark.parametrize('field,value', [('answer', '按新项目'), ('answered_by', '财务')])
def test_answered_question_is_protected_before_model_call(tmp_path, field, value):
    ws = setup(tmp_path, opinion())
    setattr(ws.questions[0], field, value)
    with pytest.raises(ToolError, match='不能通过复核撤回'):
        ws.review_question('q1', '请撤回', EVIDENCE)
    assert not ws.llm.checker_llm.calls


def test_unverified_proposal_and_unavailable_checker_cannot_close_question(tmp_path):
    ws = setup(tmp_path, opinion())
    with pytest.raises(ToolError, match='原文核对'):
        ws.review_question('q1', '不重要', [{'file': '规则.txt', 'quote': '缺失日期一律当新项目'}])
    assert not ws.llm.checker_llm.calls
    ws.llm.checker_llm = None
    with pytest.raises(ToolError, match='不可用'):
        ws.review_question('q1', '请核对', EVIDENCE)
    assert ws.pending and not ws.llm.calls


def test_failed_checker_never_falls_back_or_discards_pending_question(tmp_path):
    def failure(_):
        raise LLMError('transport result unknown')
    ws = setup(tmp_path, failure)
    result = ws.review_question('q1', '请核对', EVIDENCE)
    assert result['status'] == 'KEEP' and 'unknown' in result['reason']
    assert ws.pending and not ws.llm.calls
    assert ws.review_question('q1', '再试一次', EVIDENCE)['cached']
    assert len(ws.llm.checker_llm.calls) == 1


def test_changed_source_reopens_withdrawal_without_creating_answer(tmp_path):
    ws = setup(tmp_path, opinion())
    assert ws.review_question('q1', '请核对', EVIDENCE)['status'] == 'WITHDRAWN'
    ws.said = '新增要求：另列历史项目金额，需要确认具体范围。'
    assert ws.pending and not ws.questions[0].withdrawn and ws.questions[0].answer is None
    assert ws.workflow['question_reviews']['q1']['invalidated_withdrawal']
    ws.llm.checker_llm._responder = [opinion(decision='keep', kind='unresolved', reason='新增要求需要财务明确')]
    assert ws.review_question('q1', '已有新材料', EVIDENCE)['status'] == 'KEEP'
    assert len(ws.llm.checker_llm.calls) == 2
    assert ws.workflow['question_review_history']['q1'][0]['invalidated_withdrawal']


def test_formula_or_description_changes_do_not_buy_another_opinion(tmp_path):
    ws = setup(tmp_path, opinion(decision='keep', kind='unresolved', reason='原文不足'))
    _sales(ws)
    ws.set_rules(pool={'expr': 'sum(amt) * 0.1'}, allocation=[{'level': '行', 'key': 'rid', 'weight': 'amt'}])
    ws.review_question('q1', '请核对', EVIDENCE)
    scheme = ws.schemes[0]
    ws.drafts[scheme]['pool']['expr'] = 'sum(amt) * 0.2'
    ws.datasets['销售'].desc = '换了一个描述'
    assert ws.review_question('q1', '重试', EVIDENCE)['cached']
    assert len(ws.llm.checker_llm.calls) == 1


def test_complete_rule_tail_is_present_and_overlong_quotes_are_rejected(tmp_path):
    ws = setup(tmp_path, opinion(decision='keep', kind='unresolved', reason='文末有相反条款'))
    ws.llm.checker_llm.max_input_tokens = 120000
    tail = '重要限定：历史项目是否另行补发尚未确认，须先由财务批准。'
    (tmp_path / '规则.txt').write_text(RULE + '\n' + '说明文字。' * 12500 + '\n' + tail, encoding='utf8')
    # Inventory is refreshed as a normal run would do after source changes.
    from bonus_agent.auto import build_inventory, collect_files
    ws.inv = build_inventory(collect_files([tmp_path]))
    from bonus_agent import case
    ws._source_version = case.fingerprint(ws.inv.files)
    assert ws.review_question('q1', '请核对完整来源', EVIDENCE)['status'] == 'KEEP'
    assert tail in ws.llm.checker_llm.calls[0][-1]['content']
    with pytest.raises(ToolError, match='300'):
        ws.review_question('q1', '请核对', [{'file': '规则.txt', 'quote': '说明文字。' * 61}])


def test_checker_configuration_change_invalidates_old_withdrawal(tmp_path):
    ws = setup(tmp_path, opinion())
    ws.review_question('q1', '请核对', EVIDENCE)
    ws.llm.checker_llm.extra_body = {'new_policy': True}
    assert ws.pending and not ws.questions[0].withdrawn


def test_midrun_material_change_cannot_certify_stale_inventory(tmp_path):
    ws = setup(tmp_path, opinion())
    (tmp_path / '规则.txt').write_text('旧规则已作废，当前范围需要重新确认。', encoding='utf8')
    with pytest.raises(ToolError, match='材料|文件'):
        ws.review_question('q1', '旧依据', EVIDENCE)
    assert not ws.llm.checker_llm.calls and ws.pending and not ws.questions[0].withdrawn
