"""Real SDK/httpcore paths with an in-memory network backend; never access a socket."""
import json
import logging
import ssl

import openai
import pytest

httpcore2 = pytest.importorskip('httpcore2', reason='pre-send proof covers the installed httpcore2 transport')
httpx2 = pytest.importorskip('httpx2', reason='other SDK transports intentionally remain unknown')
if not issubclass(openai.DefaultHttpxClient, httpx2.Client):
    pytest.skip('this SDK uses a different, unverified transport', allow_module_level=True)

from bonus_agent.llm import LLMError, RunBudgetError, _PresendProbe
from bonus_agent.recovery import RunBudget
from tests.test_request_budget import model


def payload():
    body = json.dumps({'id': 'memory', 'object': 'chat.completion', 'created': 1, 'model': 'primary',
                      'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150},
                      'choices': [{'index': 0, 'finish_reason': 'stop',
                                   'message': {'role': 'assistant', 'content': '{}'}}]}).encode()
    return b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body


class Stream:
    def __init__(self, plan):
        self.plan, self.writes, self.closed = plan, [], False
        self.reads = list(plan.get('responses', [payload()]))

    def start_tls(self, ssl_context=None, server_hostname=None, timeout=None):
        if error := self.plan.get('tls_error'):
            self.close()  # real SyncStream closes a failed TLS upgrade
            raise httpcore2.ConnectError('TLS failed') from error
        return self

    def write(self, data, **kwargs):
        self.writes.append(data)
        if self.plan.get('write_error'):
            raise httpcore2.WriteError('partial write') from ConnectionResetError('reset while writing')

    def read(self, max_bytes, **kwargs):
        if self.plan.get('write_error'):
            raise httpcore2.ReadError('connection reset') from ConnectionResetError('reset after partial write')
        return self.reads.pop(0) if self.reads else b''

    def close(self):
        self.closed = True

    def get_extra_info(self, info):
        return None


class Backend:
    def __init__(self, plans, requests):
        self.plans, self.requests, self.streams, self.connects = list(plans), requests, [], []

    def connect_tcp(self, **kwargs):
        self.connects.append(kwargs)
        plan = self.plans.pop(0)
        if plan.get('malformed_trace'):
            self.requests[-1].extensions['trace']('connection.start_tls.failed', {'exception': None})
        if plan.get('http2_init_trace'):
            # Exercise httpcore's actual trace dispatch even where optional h2
            # is absent. Any HTTP/2 init event makes a later connect error unknown.
            from httpcore2._trace import Trace
            with Trace('send_connection_init', logging.getLogger('httpcore2.http2'), self.requests[-1]):
                pass
        if callback := plan.get('callback'):
            callback()
        if error := plan.get('connect_error'):
            raise httpcore2.ConnectError('connect failed') from error
        stream = Stream(plan)
        self.streams.append(stream)
        return stream

    def sleep(self, seconds):
        raise AssertionError('httpcore retries must remain disabled')


class Budget(RunBudget):
    def reserve(self, *args):
        result = super().reserve(*args)
        self.reservations = getattr(self, 'reservations', []) + [result]
        return result

    def settle(self, reservation, **kwargs):
        self.settlements = getattr(self, 'settlements', []) + [(reservation, kwargs)]
        return super().settle(reservation, **kwargs)


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    monkeypatch.setattr('bonus_agent.llm.time.sleep', lambda delay: None)


def install(monkeypatch, attempts, **transport_kwargs):
    """One fresh SDK/client/pool per application attempt; each plan can contain redirect hops."""
    created, plans = [], list(attempts)

    def factory(url, key, timeout):
        requests = []
        transport = httpx2.HTTPTransport(trust_env=False, retries=0, **transport_kwargs)
        backend = Backend(plans.pop(0), requests)
        transport._pool._network_backend = backend
        http = openai.DefaultHttpxClient(transport=transport, trust_env=False,
                                        event_hooks={'request': [requests.append]})
        sdk = openai.OpenAI(base_url=url, api_key=key, timeout=timeout, http_client=http, max_retries=2)
        created.append((sdk, http, backend))
        return sdk

    monkeypatch.setattr('bonus_agent.llm.openai_client', factory)
    return created


def bound():
    llm = model()
    return llm, Budget(0, 10000, 0).bind(llm)


@pytest.mark.parametrize('stage', ['connect', 'tls'])
def test_real_core_presend_failures_retry_with_fresh_reservations_and_clients(monkeypatch, stage):
    plan = {'connect_error': ConnectionResetError('temporary reset')} if stage == 'connect' else {
        'tls_error': ssl.SSLEOFError(8, 'temporary handshake EOF')}
    created = install(monkeypatch, [[plan], [plan], [{}]])
    llm, budget = bound()
    assert llm.chat([], max_tokens=100) == '{}'
    assert len(created) == 3 and all(http.is_closed for _, http, _ in created)
    assert budget.reservations == [1, 2, 3]
    assert [entry['sent'] for _, entry in budget.settlements] == [False, False, True]
    assert budget.snapshot()['actual_used_tokens'] == 150
    assert budget.snapshot()['unconfirmed_reserved_tokens'] == 0
    assert budget.snapshot()['inflight_reserved_tokens'] == 0
    assert llm.usage.calls == 1
    attempts = llm.last_call['request_attempts']
    assert [a['dispatch_status'] for a in attempts] == ['not_sent', 'not_sent', 'response']
    assert attempts[0]['transport_evidence']['failed_stage'] == 'connection.' + ('connect_tcp' if stage == 'connect' else 'start_tls')
    assert llm.last_call['first_request_error']['causes'][-1]['type'] in ('ConnectionResetError', 'SSLEOFError')
    assert 'request_error' not in llm.last_call
    assert all(req.headers['x-stainless-retry-count'] == '0' for _, _, backend in created for req in backend.requests)


def test_real_sdk_memory_transport_receives_hook_but_no_trace_is_not_proof(monkeypatch):
    seen, clients = [], []
    def handle(request):
        seen.append(request)
        assert callable(request.extensions['trace'])
        return httpx2.Response(200, content=payload().split(b'\r\n\r\n', 1)[1], headers={'content-type': 'application/json'})
    def factory(url, key, timeout):
        http = openai.DefaultHttpxClient(transport=httpx2.MockTransport(handle), trust_env=False)
        clients.append(http)
        return openai.OpenAI(base_url=url, api_key=key, timeout=timeout, http_client=http)
    monkeypatch.setattr('bonus_agent.llm.openai_client', factory)
    llm, budget = bound()
    assert llm.chat([], max_tokens=100) == '{}'
    assert len(seen) == 1 and clients[0].is_closed
    assert budget.snapshot()['actual_used_tokens'] == 150
    assert not llm.last_call['request_attempts'][0]['transport_evidence']['supported_transport']


def test_started_headers_are_unknown_even_when_write_fails_immediately(monkeypatch):
    created = install(monkeypatch, [[{'write_error': True}]])
    llm, budget = bound()
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    evidence = llm.last_call['request_attempts'][0]['transport_evidence']
    assert evidence['application_write_started'] and not evidence['not_sent']
    assert len(created) == 1 and created[0][1].is_closed
    assert budget.snapshot()['unconfirmed_reserved_tokens'] > 0 and budget.error


def test_no_trace_cannot_turn_connection_error_into_known_zero_usage(monkeypatch):
    clients = []
    def handle(request):
        raise httpx2.ConnectError('looks like pre-send', request=request) from ConnectionResetError('reset')
    def factory(url, key, timeout):
        http = openai.DefaultHttpxClient(transport=httpx2.MockTransport(handle), trust_env=False)
        clients.append(http)
        return openai.OpenAI(base_url=url, api_key=key, timeout=timeout, http_client=http)
    monkeypatch.setattr('bonus_agent.llm.openai_client', factory)
    llm, budget = bound()
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    assert len(clients) == 1 and clients[0].is_closed
    assert budget.snapshot()['unconfirmed_reserved_tokens'] > 0


@pytest.mark.parametrize('cause', [ssl.SSLCertVerificationError(1, 'certificate verify failed'),
                                  ssl.SSLError(1, 'TLS configuration error'), ValueError('configuration')])
def test_nontransient_tls_causes_do_not_retry_even_with_presend_proof(monkeypatch, cause):
    created = install(monkeypatch, [[{'tls_error': cause}]])
    llm, budget = bound()
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    assert len(created) == 1 and created[0][1].is_closed
    assert llm.last_call['request_attempts'][0]['transport_evidence']['not_sent']
    assert budget.snapshot()['unconfirmed_reserved_tokens'] == 0
    assert llm.last_call['first_request_error']['causes'][-1]['type'] == type(cause).__name__


def test_malformed_trace_fails_closed_without_overwriting_original_cause(monkeypatch):
    created = install(monkeypatch, [[{'malformed_trace': True, 'connect_error': ConnectionResetError('original reset')}]])
    llm, budget = bound()
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    assert len(created) == 1 and budget.snapshot()['unconfirmed_reserved_tokens'] > 0
    assert not llm.last_call['request_attempts'][0]['transport_evidence']['trace_valid']
    assert llm.last_call['request_error']['causes'][-1]['message'] == 'original reset'


def test_redirect_connection_failure_cannot_erase_the_initial_send(monkeypatch):
    redirect = b'HTTP/1.1 307 Temporary Redirect\r\nLocation: https://redirect.invalid/v1/chat/completions\r\nContent-Length: 0\r\n\r\n'
    created = install(monkeypatch, [[{'responses': [redirect]}, {'connect_error': ConnectionResetError('redirect reset')}]])
    llm, budget = bound()
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    evidence = llm.last_call['request_attempts'][0]['transport_evidence']
    assert not evidence['single_request'] and evidence['application_write_started']
    assert len(created) == 1 and budget.snapshot()['unconfirmed_reserved_tokens'] > 0


def test_proxy_certificate_failure_remains_unknown_and_never_retries(monkeypatch):
    connect = b'HTTP/1.1 200 Connection Established\r\n\r\n'
    created = install(monkeypatch, [[{'responses': [connect], 'tls_error': ssl.SSLCertVerificationError(1, 'certificate verification failed')}]],
                      proxy='http://proxy.invalid:8080')
    llm, budget = bound()
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    assert created[0][2].streams[0].writes[0].startswith(b'CONNECT ')
    assert llm.last_call['request_attempts'][0]['transport_evidence']['application_write_started']
    assert len(created) == 1 and budget.snapshot()['unconfirmed_reserved_tokens'] > 0


def test_http2_init_trace_prevents_retry_even_if_outer_failure_is_connection(monkeypatch):
    created = install(monkeypatch, [[{'http2_init_trace': True, 'connect_error': ConnectionResetError('reset')}]])
    llm, budget = bound()
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    assert llm.last_call['request_attempts'][0]['transport_evidence']['application_write_started']
    assert len(created) == 1 and budget.snapshot()['unconfirmed_reserved_tokens'] > 0


def test_http2_runtime_missing_dependency_or_write_failure_remains_unknown(monkeypatch):
    created = install(monkeypatch, [[{'write_error': True}]], http1=False, http2=True)
    llm, budget = bound()
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    # With h2 installed this is a real init write failure. Without it, the
    # protocol import fails first, which also cannot establish a pre-send proof.
    evidence = llm.last_call['request_attempts'][0]['transport_evidence']
    assert not evidence['not_sent']
    assert len(created) == 1 and budget.snapshot()['unconfirmed_reserved_tokens'] > 0
    assert created[0][1].is_closed


def test_three_attempt_limit_and_first_root_cause_are_retained(monkeypatch):
    created = install(monkeypatch, [[{'connect_error': ConnectionResetError(f'reset {n}')}] for n in range(3)])
    llm, budget = bound()
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    assert len(created) == 3 and budget.reservations == [1, 2, 3]
    assert all(http.is_closed for _, http, _ in created)
    assert budget.snapshot()['unconfirmed_reserved_tokens'] == 0
    assert llm.last_call['first_request_error']['causes'][-1]['message'] == 'reset 0'
    assert llm.last_call['request_error']['causes'][-1]['message'] == 'reset 2'


def test_unknown_second_attempt_stops_and_does_not_lose_first_failure(monkeypatch):
    created = install(monkeypatch, [[{'tls_error': ssl.SSLEOFError(8, 'first TLS EOF')}], [{'write_error': True}]])
    llm, budget = bound()
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    assert len(created) == 2 and budget.snapshot()['unconfirmed_reserved_tokens'] > 0
    uncertain = budget.snapshot()['unconfirmed_reserved_tokens']
    assert llm.last_call['first_request_error']['causes'][-1]['type'] == 'SSLEOFError'
    with pytest.raises(RunBudgetError):
        llm.chat([], max_tokens=1)
    assert len(created) == 2
    assert budget.snapshot()['unconfirmed_reserved_tokens'] == uncertain


def test_missing_usage_after_presend_retry_is_still_unknown(monkeypatch):
    body = json.loads(payload().split(b'\r\n\r\n', 1)[1])
    body.pop('usage')
    encoded = json.dumps(body).encode()
    answer = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ' + str(len(encoded)).encode() + b'\r\n\r\n' + encoded
    created = install(monkeypatch, [[{'tls_error': ssl.SSLEOFError(8, 'first EOF')}], [{'responses': [answer]}]])
    llm, budget = bound()
    with pytest.raises(RunBudgetError, match='InvalidUsage'):
        llm.chat([], max_tokens=100)
    assert len(created) == 2 and all(http.is_closed for _, http, _ in created)
    assert llm.usage.calls == 1 and budget.snapshot()['actual_used_tokens'] == 0
    assert budget.snapshot()['unconfirmed_reserved_tokens'] > 0
    assert llm.last_call['request_attempts'][-1]['request_error']['type'] == 'InvalidUsage'


def test_certificate_error_dominates_nested_transient_cause(monkeypatch):
    certificate = ssl.SSLCertVerificationError(1, 'verification failed')
    certificate.__cause__ = TimeoutError('transient-looking nested exception')
    created = install(monkeypatch, [[{'tls_error': certificate}]])
    llm, budget = bound()
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    assert len(created) == 1 and budget.snapshot()['unconfirmed_reserved_tokens'] == 0
    assert any(cause['type'] == 'SSLCertVerificationError' for cause in llm.last_call['first_request_error']['causes'])


def test_deadline_is_rechecked_before_retry_can_create_a_client(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('bonus_agent.llm.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('bonus_agent.llm.time.sleep', lambda delay: clock.__setitem__(0, clock[0] + delay))
    created = install(monkeypatch, [[{'connect_error': ConnectionResetError('reset'),
                                    'callback': lambda: clock.__setitem__(0, 1004.9)}]])
    llm = model(); budget = Budget(0, 10000, 5, started=1000).bind(llm)
    with pytest.raises(RunBudgetError, match='时间上限'):
        llm.chat([], max_tokens=100)
    assert len(created) == 1 and created[0][1].is_closed
    assert budget.reservations == [1]
    assert budget.snapshot()['unconfirmed_reserved_tokens'] == 0
    assert llm.last_call['first_request_error']['causes'][-1]['message'] == 'reset'


def test_tokens_are_reserved_again_before_retry(monkeypatch):
    llm, budget = bound()
    created = install(monkeypatch, [[{'connect_error': ConnectionResetError('reset'),
                                    'callback': lambda: setattr(budget, 'max_tokens', 1)}]])
    with pytest.raises(RunBudgetError, match='事前拦截'):
        llm.chat([], max_tokens=100)
    assert len(created) == 1 and budget.reservations == [1]
    assert budget.snapshot()['inflight_reserved_tokens'] == 0
    assert budget.snapshot()['unconfirmed_reserved_tokens'] == 0


def test_nonbudget_calls_do_not_stack_application_retries_on_sdk_retries(monkeypatch):
    created = install(monkeypatch, [[{'connect_error': ConnectionResetError('reset')} for _ in range(3)]])
    llm = model()
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    assert len(created) == 1 and len(created[0][2].connects) == 3
    assert created[0][1].is_closed and 'request_attempts' not in llm.last_call


def test_probe_failures_do_not_expose_trace_request_material():
    probe = _PresendProbe()
    probe.on_trace(None, {'authorization': 'secret', 'request': 'sensitive body'})
    assert not probe.valid
    assert 'secret' not in json.dumps(probe.summary()) and 'sensitive' not in json.dumps(probe.summary())
