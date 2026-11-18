"""Transport failures remain explainable after conservative unknown-usage settlement."""
import json
import ssl

import httpx
import openai
import pytest

from bonus_agent.agent import Workspace, run_agent
from bonus_agent.llm import LLMError, RunBudgetError
from bonus_agent.recovery import RunBudget
from tests.test_request_budget import model, response, transport


def connection_failure(secret='not-a-key'):
    request = httpx.Request('POST', 'https://example.invalid/v1/chat/completions')
    tls = ssl.SSLCertVerificationError('certificate verify failed')
    connection = httpx.ConnectError('TLS handshake failed', request=request)
    connection.__cause__ = tls
    error = openai.APIConnectionError(request=request, message=f'Connection error; Authorization: Bearer {secret}')
    error.__cause__ = connection
    return error


def test_original_connection_cause_survives_settlement_and_blocked_fallback(monkeypatch):
    client = transport(monkeypatch, [connection_failure()])
    llm = model()
    budget = RunBudget(0, 100000, 0).bind(llm)
    with pytest.raises(LLMError, match='APIConnectionError'):
        llm.chat([], max_tokens=100)
    failure = budget.snapshot()['request_failure']
    assert failure['type'] == 'APIConnectionError'
    assert failure['transport_invoked'] is True and failure['response_received'] is False
    assert [cause['type'] for cause in failure['causes']] == ['ConnectError', 'SSLCertVerificationError']
    assert failure['budget_request_sequence'] == 1
    assert 'APIConnectionError' in budget.error and 'certificate verify failed' in budget.error
    assert 'not-a-key' not in json.dumps(llm.last_call)
    assert budget.snapshot()['unconfirmed_reserved_tokens'] > 0
    with pytest.raises(RunBudgetError):
        llm.chat([], max_tokens=1)
    assert budget.snapshot()['request_failure'] == failure
    assert llm.last_call['budget']['request_failure'] == failure
    assert len(client.requests) == 1


def test_main_loop_final_error_and_persisted_case_keep_original_network_cause(tmp_path, monkeypatch):
    client = transport(monkeypatch, [connection_failure()])
    llm = model()
    monkeypatch.setattr(Workspace, 'triage_all', lambda self: None)
    policy = tmp_path / 'policy.md'
    policy.write_text('按原文计算。', encoding='utf8')
    out = tmp_path / 'out'
    result = run_agent(llm, [policy], out, max_steps=3, max_tokens=200000,
                       run_budget=RunBudget(0, 200000, 0))
    assert '用量未确认' in result.error
    assert 'APIConnectionError' in result.error and 'certificate verify failed' in result.error
    assert len(client.requests) == 1  # No retry or native-to-JSON network fallback.
    saved = json.loads((out / 'case.json').read_text(encoding='utf-8'))['rounds'][-1]
    assert saved['error'] == result.error
    assert saved['last_request']['request_error']['type'] == 'APIConnectionError'
    assert saved['usage']['budget']['request_failure']['causes'][-1]['type'] == 'SSLCertVerificationError'
    assert 'not-a-key' not in json.dumps(saved)


def test_http_failure_preserves_status_request_id_and_redacts_credentials(monkeypatch):
    request = httpx.Request('POST', 'https://example.invalid/v1/chat/completions')
    error = openai.APIStatusError(
        "Service unavailable; {'api_key': 'other-secret'}; token=access-secret; https://user:proxy-secret@example.invalid",
        response=httpx.Response(503, request=request, headers={'x-request-id': 'req-503'}), body=None)
    client = transport(monkeypatch, [error])
    llm = model()
    budget = RunBudget(0, 10000, 0).bind(llm)
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    detail = budget.snapshot()['request_failure']
    assert detail['status_code'] == 503 and detail['request_id'] == 'req-503'
    assert detail['response_received'] is True
    assert llm.last_call['response_received'] is True
    for secret in ('other-secret', 'access-secret', 'proxy-secret'):
        assert secret not in json.dumps(detail)
    assert len(client.requests) == 1 and budget.error


def test_status_only_provider_error_still_records_an_http_response(monkeypatch):
    class ProviderError(Exception):
        status_code = 429
    transport(monkeypatch, [ProviderError('rate limited')])
    llm = model()
    budget = RunBudget(0, 10000, 0).bind(llm)
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    detail = budget.snapshot()['request_failure']
    assert detail['status_code'] == 429 and detail['response_received'] is True
    assert budget.snapshot()['unconfirmed_reserved_tokens'] > 0


@pytest.mark.parametrize('item,invalid', [
    (response(reported=False), ['prompt_tokens', 'completion_tokens']),
    (response(prompt=100, completion=None), ['completion_tokens']),
])
def test_missing_usage_is_distinguished_from_transport_failure(monkeypatch, item, invalid):
    client = transport(monkeypatch, [item])
    llm = model()
    budget = RunBudget(0, 10000, 0).bind(llm)
    with pytest.raises(RunBudgetError, match='InvalidUsage'):
        llm.chat_tools([], [], max_tokens=100)
    detail = budget.snapshot()['request_failure']
    assert detail['type'] == 'InvalidUsage' and detail['response_received'] is True
    assert detail['invalid_usage_fields'] == invalid and detail['response_id'] == 'mock-response'
    assert budget.snapshot()['unconfirmed_reserved_tokens'] > 0
    with pytest.raises(RunBudgetError):
        llm.chat([], max_tokens=1)
    assert len(client.requests) == 1


def test_diagnostic_snapshot_cannot_overwrite_preserved_original_failure(monkeypatch):
    transport(monkeypatch, [connection_failure()])
    llm = model()
    budget = RunBudget(0, 10000, 0).bind(llm)
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    snapshot = budget.snapshot()
    snapshot['request_failure']['causes'].clear()
    snapshot['request_failure']['message'] = 'different error'
    assert budget.snapshot()['request_failure']['causes']
    assert budget.snapshot()['request_failure']['message'] != 'different error'
