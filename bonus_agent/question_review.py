"""Audited source review of a possibly mistaken pending question, never an answer."""
from __future__ import annotations

import copy
import json

from bonus_agent.context import guard_messages
from bonus_agent.llm import LLMError, parse_json
from bonus_agent.recovery import fingerprint

REVIEW_OUTPUT_TOKENS = 16384
RETRY_OUTPUT_TOKENS = 32768


def _reviewer(checker):
    return {'name': getattr(checker, 'name', ''), 'model': getattr(checker, 'model', ''),
            'configuration': fingerprint({k: getattr(checker, k, None) for k in
                                          ('base_url', 'think_body', 'extra_body', 'json_mode')})}


def evidence_state(ws):
    from bonus_agent import case
    definitions = {}
    for name, ds in ws.datasets.items():
        data = ds.model_dump(mode='json', exclude={'desc'})
        for value in data.get('fields', {}).values():
            value.pop('desc', None)
        definitions[name] = data
    return fingerprint({'files': case.fingerprint(ws.inv.files), 'period': str(ws.period),
                        'datasets': definitions,
                        'mapping': {n: m.model_dump(mode='json') for n, m in ws.mapping.items()},
                        'said': ws.said, 'transcripts': ws.transcripts,
                        'answers': [(q.id, q.answer) for q in ws.questions if q.answer not in (None, '')],
                        'reviewer': _reviewer(getattr(ws.llm, 'checker_llm', None)), 'policy': 1})


def invalidate_changed_reviews(ws):
    """A source-backed withdrawal is not permanent after its evidence changes."""
    records = ws.workflow.get('question_reviews', {})
    candidates = [(q, records[q.id]) for q in ws.questions if q.id in records and q.withdrawn
                  and q.answer in (None, '') and records[q.id].get('result', {}).get('status') == 'WITHDRAWN']
    if not candidates:
        return
    current = evidence_state(ws)
    for q, record in candidates:
        if record.get('evidence_state') != current:
            record['invalidated_withdrawal'] = q.withdrawn
            record['result'] = {'status': 'KEEP', 'id': q.id, 'reason': '原文、数据范围或复核配置已变，旧撤回不再有效'}
            q.withdrawn = ''


def _quotes_complete(evidence):
    return (isinstance(evidence, list) and 1 <= len(evidence) <= 4
            and all(isinstance(e, dict) and isinstance(e.get('quote'), str)
                    and 8 <= len(e['quote'].strip()) <= 300 for e in evidence))


def _known_length(request):
    return (isinstance(request, dict) and request.get('response_received') is True
            and request.get('usage_reported') is True and request.get('finish_reason') == 'length')


def _retry_limit(ws, checker, old):
    """Only a protocol repair, never another draw after a completed opinion."""
    from bonus_agent.agent import ToolError
    if old.get('review') or old.get('outcome') == 'COMPLETED':
        raise ToolError('独立复核已有业务判断，不能通过技术重试重新抽取意见')
    if old.get('technical_retries', 0) >= 1:
        raise ToolError('同证据状态的独立复核已用完一次技术续试，保持等待')
    attempts = old.get('attempts')
    legacy = attempts is None and 'outcome' not in old
    if legacy:
        # Earlier versions persisted only this application-generated failure.
        # Its usage remains UNKNOWN: never infer accounting from error text.
        reason = old.get('result', {}).get('reason', '')
        previous = getattr(ws, '_resumed_from_round', None)
        if (not reason.startswith('独立问题复核未通过，保持等待：')
                or '输出未完成（finish_reason=length）' not in reason
                or not isinstance(previous, int) or isinstance(previous, bool)
                or not 0 < previous < ws.round):
            raise ToolError('旧复核没有可核对的请求元数据；仅真实恢复到新轮的旧length记录可显式修复一次协议，旧用量仍未知')
        limit = REVIEW_OUTPUT_TOKENS
        # Legacy chat requested 8192, but _think could raise that ceiling. The
        # original effective configuration is unknown; do not assume an upgrade
        # below even the current thinking allowance.
        prior_output = max(8192, getattr(checker, 'think_max_tokens', 0))
    else:
        request = attempts[-1].get('request', {}) if attempts else {}
        if old.get('outcome') != 'TECHNICAL' or not _known_length(request):
            raise ToolError('仅收到完整有效usage且明确finish_reason=length的未完成复核可技术续试；未知用量、超时或无效意见不可重抽')
        limit = RETRY_OUTPUT_TOKENS
        prior_output = request.get('requested_output_tokens', attempts[-1].get('output_tokens', 0))
    budget = getattr(checker, 'run_budget', None)
    if budget is None or budget.check(checker):
        raise ToolError('技术续试须有可用的本轮作业预算；不能返还旧请求用量或绕过预算停止')
    state = budget.snapshot()
    if state.get('remaining_tokens') is None or state['remaining_tokens'] <= 0:
        raise ToolError('技术续试须有明确且尚未耗尽的本轮token额度')
    cap = getattr(checker, '_max_output_tokens', None)
    effective = min(limit, int(cap)) if cap else limit
    if effective <= prior_output:
        raise ToolError('服务输出上限无法提高，重复同样长度的请求不是协议修复；保持等待')
    return limit, legacy


def review_question(ws, id: str, reason: str, evidence: list[dict], retry_technical: bool = False) -> dict:
    from bonus_agent.agent import ToolError
    from bonus_agent import reread
    from bonus_agent.scope_diagnostics import scope_diagnostics

    ws._check_source_version()
    invalidate_changed_reviews(ws)
    q = next((q for q in ws.questions if q.id == id), None)
    if q is None:
        raise ToolError(f'没有问题 {id}')
    if q.answer not in (None, '') or q.answered_by:
        raise ToolError('已有财务或材料答复的问题不能通过复核撤回，也不能更改其答复')
    if q.withdrawn:
        return {'status': 'WITHDRAWN', 'id': id, 'cached': True, 'reason': q.withdrawn}
    if not isinstance(reason, str) or not reason.strip():
        raise ToolError('须说明问题前提哪里可能有误；这只是待核对意见，不是财务答复')
    if not _quotes_complete(evidence):
        raise ToolError('须提供 1–4 条完整原文依据 evidence，每条 8–300 字；超长引文应选取完整相关句子，不能截断限定条件')
    verified, bad = ws._evidence(evidence)
    if bad or len(verified) != len(evidence) or any(not e.get('ok') for e in verified):
        raise ToolError('问题复核依据未通过原文核对：' + '；'.join(bad[:3]))
    checker = getattr(ws.llm, 'checker_llm', None)
    if checker is None or checker is ws.llm or getattr(ws, 'checker_down', ''):
        raise ToolError('独立问题复核模型不可用；保持等待，不由主模型自行撤回')

    material = reread.material(ws, full_documents=True)
    diagnostic = scope_diagnostics(ws)
    question = q.model_dump(mode='json')
    # Rewording the proposed reason/evidence must not buy another opinion.
    state = evidence_state(ws)
    signature = fingerprint({'question': question, 'evidence_state': state,
                             'material': material, 'scope': diagnostic, 'policy': 1})
    records = ws.workflow.setdefault('question_reviews', {})
    old = records.get(id, {})
    output_tokens, legacy_retry = REVIEW_OUTPUT_TOKENS, False
    initial_pending = False
    if not isinstance(retry_technical, bool):
        raise ToolError('retry_technical须为布尔值；不是业务口径或问题答复')
    if old.get('signature') == signature:
        budget = getattr(checker, 'run_budget', None)
        initial_pending = bool(old.get('initial_request_pending'))
        if initial_pending and retry_technical:
            raise ToolError('初次独立复核尚未发送，不属于技术续试；在可用作业预算下正常review_question即可')
        initial_ready = (initial_pending and budget is not None and not budget.check(checker)
                         and (budget.snapshot().get('remaining_tokens') or 0) > 0)
        if not retry_technical and not initial_ready:
            result = copy.deepcopy(old['result'])
            result['cached'] = True
            return result
        if retry_technical:
            output_tokens, legacy_retry = _retry_limit(ws, checker, old)
        # Even explicit repair cannot reframe the same question to seek another answer.
        reason, verified = old['proposed_reason'], copy.deepcopy(old['proposed_evidence'])
    elif retry_technical:
        raise ToolError('当前证据状态没有相同问题的可续试技术记录；新证据应正常复核，不能冒充旧请求续试')
    messages = [
        {'role': 'system', 'content':
         '【待答问题独立复核】核对主模型提出的问题是否包含错误前提，或原材料已经明确解决该问题。'
         '主模型的理由仅是待验证意见；按原文、真实程序范围诊断独立判断。'
         '不得代财务选择新口径、批准金额、推断缺失数据、默认排除记录或修改公式。'
         '对象覆盖数量不同只能证明映射事实，不能单独证明业务对象范围；须引用明确的原文。'
         '仅当原文明确、无冲突且足以排除提问前提时允许 withdraw；不能证明就 keep。'
         '空数据、缺少参数、未确认总额、多个合法理解或原文冲突均应 keep。'
         'withdraw 仅撤回错误问题，不代表计算已正确；后续数据与金额门禁仍须全部通过。'
         '只输出 JSON：{"decision":"withdraw|keep","kind":"false_premise|explicit_source|unresolved",'
         '"reason":"逐条说明原文如何推翻问题前提，或仍缺什么",'
         '"evidence":[{"file":"真实文件名","quote":"逐字原文"}]}。'
         'withdraw 必须含至少一条完整原文；不可把主模型的理由当成引用。'},
        {'role': 'user', 'content': material + '\n\n待复核问题：' + json.dumps(question, ensure_ascii=False)
         + '\n主模型待核对意见：' + reason[:3000]
         + '\n已核对引文：' + json.dumps(verified, ensure_ascii=False)
         + '\n程序范围观察（不等于业务批准）：' + json.dumps(diagnostic, ensure_ascii=False, default=str)},
    ]
    # Thinking shares the output allowance. Preserve the independent model and
    # full source packet; increase only this protocol's completion capacity.
    effective_output = max(output_tokens, getattr(checker, 'think_max_tokens', 0))
    if cap := getattr(checker, '_max_output_tokens', None):
        effective_output = min(effective_output, int(cap))
    guard_messages(messages, checker, output_tokens=effective_output)
    if retry_technical:
        # guard_messages may have just discovered the provider's output cap.
        _retry_limit(ws, checker, old)
    result = {'status': 'KEEP', 'id': id, 'reason': '独立复核尚未完成，仍等待原问题答复',
              'cached': False, 'by': checker.name, 'outcome': 'TECHNICAL'}
    record = {'signature': signature, 'evidence_state': state, 'question': question, 'proposed_reason': reason[:3000],
              'proposed_evidence': verified, 'result': result, 'outcome': 'TECHNICAL',
              'technical_retries': int(retry_technical),
              'attempts': copy.deepcopy(old.get('attempts', [])) if retry_technical else []}
    if initial_pending:
        record['budget_events'] = copy.deepcopy(old.get('budget_events', []))
    if legacy_retry:
        record['legacy_usage_status'] = 'UNKNOWN_NOT_REFUNDED'
        record['legacy_resumed_from_round'] = ws._resumed_from_round
    if old:
        ws.workflow.setdefault('question_review_history', {}).setdefault(id, []).append(copy.deepcopy(old))
    budget = getattr(checker, 'run_budget', None)
    attempt = {'round': ws.round, 'output_tokens': output_tokens, 'retry_technical': retry_technical,
               'legacy_protocol_repair': legacy_retry, 'status': 'REQUESTING',
               'budget_before': budget.snapshot() if budget is not None else None}
    record['attempts'].append(attempt)
    records[id] = record  # persist a failed/unknown request; do not resample it on resume
    checker.last_call = {}  # a pre-request failure must not inherit an unrelated successful call
    try:
        raw = checker.chat(messages, json_out=True, temperature=0, max_tokens=output_tokens, think=True)
        data = parse_json(raw)
        if not isinstance(data, dict) or data.get('decision') not in ('keep', 'withdraw'):
            raise ValueError('独立复核输出须包含 decision=keep/withdraw')
        record['review'] = copy.deepcopy(data)
        why = data.get('reason')
        if not isinstance(why, str) or not why.strip():
            raise ValueError('独立复核缺少判断依据')
        if data['decision'] == 'withdraw':
            if data.get('kind') not in ('false_premise', 'explicit_source'):
                raise ValueError('只能撤回已由原文证实的错误前提或已明确解决的问题')
            citations = data.get('evidence')
            if not _quotes_complete(citations):
                raise ValueError('独立撤回意见须含 1–4 条完整的 8–300 字原文证据，不可静默截断')
            proof, bad = ws._evidence(citations)
            if bad or len(proof) != len(citations) or any(not p.get('ok') for p in proof):
                raise ValueError('独立复核的引用无法核对原文：' + '；'.join(bad[:3]))
            q.withdrawn = f'独立原文复核撤回（{checker.name}）：{why.strip()}'
            record['verified_evidence'] = proof
            result.update(status='WITHDRAWN', reason=q.withdrawn, evidence=proof,
                          next='修复原文对应的数据范围或规则并重新核验；未写入财务答复或金额批准')
        else:
            result['reason'] = why.strip()
        record['outcome'] = result['outcome'] = 'COMPLETED'
    except LLMError as exc:
        result['reason'] = '独立问题复核未通过，保持等待：' + str(exc)
    except ValueError as exc:
        record['outcome'] = result['outcome'] = 'REJECTED'
        result['reason'] = '独立问题复核未通过，保持等待：' + str(exc)
    finally:
        attempt.update(status=record['outcome'], request=copy.deepcopy(getattr(checker, 'last_call', {})),
                       budget_after=budget.snapshot() if budget is not None else None)
    request = attempt['request']
    if (request.get('status') == 'budget_blocked'
            and request.get('request_attempts') == [] and not request.get('budget_request_sequence')
            and not request.get('response_received')):
        # reserve rejected this request before transport: preserve, rather than
        # spend, the sole retry opportunity. Sent/unknown requests never enter here.
        if retry_technical:
            retained = copy.deepcopy(old)
            retained.setdefault('technical_budget_blocks', []).append(copy.deepcopy(attempt))
            records[id] = retained
            result.update(technical_retry_not_sent=True, technical_retry_available=False,
                          next='本轮预算在发送前阻断；旧请求未退款。取得新的作业额度后可继续尚未发送的同一次技术续试')
        else:
            # No opinion was requested, so a fresh authorized allowance may
            # still send the initial request. Keep preflight events separately.
            record['attempts'].pop()
            record['initial_request_pending'] = True
            record.setdefault('budget_events', []).append(copy.deepcopy(attempt))
            result.update(initial_request_not_sent=True, technical_retry_available=False,
                          next='初次复核在发送前被预算阻断，尚无审核意见；取得新的作业额度后正常review_question，勿使用技术续试')
        return copy.deepcopy(result)
    if record['outcome'] == 'TECHNICAL':
        result['technical_retry_available'] = not retry_technical and _known_length(attempt['request'])
        if result['technical_retry_available']:
            result['next'] = '原复核未形成业务判断；可显式retry_technical=true增大输出额度续试一次，须重新预留本轮预算；仍未完成即停止'
    return copy.deepcopy(result)
