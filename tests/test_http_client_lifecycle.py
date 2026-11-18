"""Owned transports close on every outcome without changing accounting or retry policy."""
import importlib
import json
from types import SimpleNamespace as NS

import openai
import pytest

from bonus_agent.llm import LLMError, RunBudgetError, list_remote, openai_client
from bonus_agent.recovery import RunBudget
from tests.test_request_budget import Transport, model, response
from tests.test_request_error_diagnostics import connection_failure


class OwnedTransport(Transport):
    def __init__(self, responses=None, *, close_error=None, options_error=None, models_error=None):
        super().__init__(responses)
        self.close_count = 0
        self.close_error, self.options_error, self.models_error = close_error, options_error, models_error
        self.models = NS(list=self.list_models)

    def close(self):
        self.close_count += 1
        if self.close_error:
            raise self.close_error

    def with_options(self, **kwargs):
        if self.options_error:
            raise self.options_error
        return super().with_options(**kwargs)

    def list_models(self):
        if self.models_error:
            raise self.models_error
        return [NS(id='primary', context_window=96000)]


def install(monkeypatch, client):
    monkeypatch.setattr('bonus_agent.llm.openai_client', lambda *args: client)


@pytest.mark.parametrize('kind', ['chat', 'chat_tools'])
def test_successful_generation_closes_owned_transport(monkeypatch, kind):
    client = OwnedTransport(); install(monkeypatch, client)
    llm = model(); budget = RunBudget(0, 10000, 0).bind(llm)
    getattr(llm, kind)([], **({'tools': []} if kind == 'chat_tools' else {}), max_tokens=100)
    assert client.close_count == 1 and len(client.requests) == 1
    assert client.options[0]['max_retries'] == 0
    assert budget.snapshot()['actual_used_tokens'] == 150
    assert not budget.snapshot()['unconfirmed_reserved_tokens'] and not budget.error


def test_cleanup_failure_does_not_turn_known_success_into_unknown_usage(monkeypatch, caplog):
    client = OwnedTransport(close_error=OSError('Authorization: Bearer not-a-key'))
    install(monkeypatch, client)
    llm = model(); budget = RunBudget(0, 10000, 0).bind(llm)
    assert llm.chat([], max_tokens=100) == '{}'
    assert client.close_count == 1 and not budget.error
    assert budget.snapshot()['actual_used_tokens'] == 150
    assert budget.snapshot()['unconfirmed_reserved_tokens'] == 0
    assert llm.last_call['cleanup_error']['type'] == 'OSError'
    assert 'not-a-key' not in json.dumps(llm.last_call) + caplog.text


def test_request_and_cleanup_failure_keep_original_cause_and_no_retry(monkeypatch, caplog):
    client = OwnedTransport([connection_failure()], close_error=OSError('api_key=not-a-key'))
    install(monkeypatch, client)
    llm = model(); budget = RunBudget(0, 10000, 0).bind(llm)
    with pytest.raises(LLMError, match='APIConnectionError'):
        llm.chat([], max_tokens=100)
    assert client.close_count == 1 and len(client.requests) == 1
    assert budget.snapshot()['request_failure']['causes'][-1]['type'] == 'SSLCertVerificationError'
    assert budget.snapshot()['unconfirmed_reserved_tokens'] > 0
    assert llm.last_call['cleanup_error']['type'] == 'OSError'
    assert 'not-a-key' not in json.dumps(llm.last_call) + caplog.text
    with pytest.raises(RunBudgetError):
        llm.chat([], max_tokens=100)
    assert len(client.requests) == client.close_count == 1


def test_missing_usage_stays_unknown_even_when_cleanup_also_fails(monkeypatch):
    client = OwnedTransport([response(reported=False)], close_error=OSError('close failed'))
    install(monkeypatch, client)
    llm = model(); budget = RunBudget(0, 10000, 0).bind(llm)
    with pytest.raises(RunBudgetError, match='InvalidUsage'):
        llm.chat([], max_tokens=100)
    assert client.close_count == 1 and len(client.requests) == 1
    assert budget.snapshot()['request_failure']['type'] == 'InvalidUsage'
    assert budget.snapshot()['unconfirmed_reserved_tokens'] > 0


def test_options_failure_closes_original_before_send_without_unknown_charge(monkeypatch):
    original = ValueError('invalid options')
    client = OwnedTransport(options_error=original, close_error=OSError('close failed'))
    install(monkeypatch, client)
    llm = model(); budget = RunBudget(0, 10000, 0).bind(llm)
    with pytest.raises(LLMError, match='invalid options') as error:
        llm.chat([], max_tokens=100)
    assert error.value.__cause__ is original
    assert client.close_count == 1 and not client.requests
    assert budget.snapshot()['unconfirmed_reserved_tokens'] == 0
    assert budget.snapshot()['inflight_reserved_tokens'] == 0
    assert llm.last_call['request_error']['transport_invoked'] is False


@pytest.mark.parametrize('failure', [None, 'models', 'options'])
def test_context_metadata_closes_on_success_or_failure_and_keeps_cache(monkeypatch, failure):
    client = OwnedTransport(models_error=ValueError('metadata failed') if failure == 'models' else None,
                            options_error=ValueError('options failed') if failure == 'options' else None,
                            close_error=OSError('api_key=not-a-key'))
    install(monkeypatch, client)
    llm = model(); del llm._ctx
    RunBudget(0, 10000, 0).bind(llm)
    expected = None if failure else 96000
    assert llm.context_window() == expected
    assert llm.context_window() == expected  # Cached metadata does not create another client.
    assert client.close_count == 1
    assert 'not-a-key' not in json.dumps(llm.last_call)
    assert llm.last_call['context_cleanup_error']['type'] == 'OSError'


@pytest.mark.parametrize('failed', [False, True])
def test_remote_list_always_closes_without_cleanup_masking_result(monkeypatch, caplog, failed):
    original = ValueError('metadata original') if failed else None
    client = OwnedTransport(models_error=original, close_error=OSError('api_key=not-a-key'))
    install(monkeypatch, client)
    cfg = {'providers': {'test': {'base_url': 'https://example.invalid', 'api_key': 'not-a-key'}}}
    if failed:
        with pytest.raises(LLMError, match='metadata original') as error:
            list_remote('test', cfg)
        assert error.value.__cause__ is original
    else:
        assert list_remote('test', cfg) == ['primary']
    assert client.close_count == 1 and 'not-a-key' not in caplog.text


def test_sdk_constructor_failure_releases_explicit_local_http_client(monkeypatch):
    client = OwnedTransport()
    monkeypatch.setattr(openai, 'DefaultHttpxClient', lambda **kwargs: client)
    def fail(**kwargs):
        raise ValueError('constructor failed')
    monkeypatch.setattr(openai, 'OpenAI', fail)
    with pytest.raises(ValueError, match='constructor failed'):
        openai_client('http://127.0.0.1/v1', 'not-a-key', 7)
    assert client.close_count == 1


def test_actual_sdk_options_share_transport_and_new_parameters_are_respected(monkeypatch):
    # Use the SDK's installed HTTP library (httpx or httpx2), with a memory-only transport.
    http = importlib.import_module(openai.DefaultHttpxClient.__mro__[1].__module__.split('.')[0])
    created = []
    def factory(url, key, timeout):
        def handle(request):
            return http.Response(200, json={'id': 'mock', 'object': 'chat.completion', 'created': 1,
                'model': 'primary', 'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150},
                'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '{}'}}]})
        transport = http.Client(transport=http.MockTransport(handle))
        client = openai.OpenAI(base_url=url, api_key=key, timeout=timeout, http_client=transport, max_retries=2)
        created.append((url, key, timeout, transport))
        return client
    monkeypatch.setattr('bonus_agent.llm.openai_client', factory)
    llm = model(); RunBudget(0, 10000, 0).bind(llm)
    assert llm.chat([], max_tokens=100) == '{}'
    llm.base_url, llm.api_key, llm.timeout = 'https://second.invalid/v1', 'second-key', 17
    assert llm.chat([], max_tokens=100) == '{}'
    assert len(created) == 2 and all(item[3].is_closed for item in created)
    assert created[1][:3] == (llm.base_url, llm.api_key, llm.timeout)
