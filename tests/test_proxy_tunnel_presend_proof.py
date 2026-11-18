"""Only a verified empty CONNECT -> failed target TLS permits a proxy retry."""
import ssl

import pytest

from bonus_agent.llm import LLMError, RunBudgetError, _PresendProbe
from tests.test_presend_transport_retry import bound, install, payload, httpcore2, no_retry_sleep, Budget, model, Stream


CONNECT = b'HTTP/1.1 200 Connection Established\r\n\r\n'
PROXY = 'http://proxy.invalid:8080'


def failed_plan(cause=None):
    return {'responses': [CONNECT], 'tls_error': cause or ssl.SSLEOFError(8, 'target handshake EOF')}


def test_real_sdk_proxy_failures_retry_then_count_one_success_with_fresh_clients(monkeypatch):
    created = install(monkeypatch, [[failed_plan()], [failed_plan()], [{'responses': [CONNECT, payload()]}]], proxy=PROXY)
    llm, budget = bound()
    assert llm.chat([], max_tokens=100) == '{}'
    assert len(created) == 3 and all(http.is_closed for _, http, _ in created)
    assert budget.reservations == [1, 2, 3]
    assert [value['sent'] for _, value in budget.settlements] == [False, False, True]
    assert budget.snapshot()['actual_used_tokens'] == 150 and llm.usage.calls == 1
    assert budget.snapshot()['unconfirmed_reserved_tokens'] == 0
    attempts = llm.last_call['request_attempts']
    assert [entry['dispatch_status'] for entry in attempts] == ['not_sent', 'not_sent', 'response']
    assert all(entry['transport_evidence']['proxy_tunnel_not_sent'] for entry in attempts[:2])
    for _, _, backend in created[:2]:
        writes = backend.streams[0].writes
        assert writes[0].startswith(b'CONNECT ') and not any(chunk.startswith(b'POST ') for chunk in writes)
    assert any(chunk.startswith(b'POST ') for chunk in created[-1][2].streams[0].writes)
    assert llm.last_call['first_request_error']['causes'][-1]['type'] == 'SSLEOFError'


def test_complete_proof_cannot_exceed_existing_three_attempt_limit(monkeypatch):
    created = install(monkeypatch, [[failed_plan()] for _ in range(3)], proxy=PROXY)
    llm, budget = bound()
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    assert len(created) == 3 and budget.reservations == [1, 2, 3]
    assert all(http.is_closed for _, http, _ in created)
    assert all(not value['sent'] for _, value in budget.settlements)
    assert budget.snapshot()['unconfirmed_reserved_tokens'] == 0


@pytest.mark.parametrize('damage', [
    'missing_event', 'extra_event', 'reordered_event', 'wrong_method', 'wrong_authority',
    'nonempty_body', 'request_identity', 'non_success_response', 'failure_identity', 'sdk_replay', 'http2_init',
])
def test_incomplete_or_ambiguous_real_proxy_trace_stays_unknown(monkeypatch, damage):
    original = _PresendProbe.on_trace
    changed = False

    def observe(self, event, info):
        nonlocal changed
        if damage == 'missing_event' and event == 'http11.send_request_body.complete':
            return
        if not changed and event == 'http11.send_request_headers.started':
            changed = True
            request = info['request']
            if damage in ('extra_event', 'http2_init', 'reordered_event'):
                injected = {'extra_event': 'unknown.private-data', 'http2_init': 'http2.send_connection_init.started',
                            'reordered_event': 'http11.send_request_body.started'}[damage]
                original(self, injected, {})
            elif damage in ('wrong_method', 'wrong_authority', 'nonempty_body'):
                url = request.url
                if damage == 'wrong_authority':
                    url = httpcore2.URL(scheme=url.scheme, host=url.host, port=url.port, target=b'wrong.invalid:443')
                other = httpcore2.Request(b'POST' if damage == 'wrong_method' else b'CONNECT', url,
                                         content=b'not empty' if damage == 'nonempty_body' else b'',
                                         extensions=request.extensions)
                info = {**info, 'request': other}  # do not mutate the actual packet
            elif damage == 'sdk_replay':
                # A second SDK request hook is never authorized to reset proof.
                self.on_request(created[0][2].requests[-1])
        if damage == 'request_identity' and event == 'http11.receive_response_headers.started':
            request = info['request']
            info = {**info, 'request': httpcore2.Request('CONNECT', request.url, extensions=request.extensions)}
        if damage == 'non_success_response' and event == 'http11.receive_response_headers.complete':
            value = info['return_value']
            info = {**info, 'return_value': (value[0], 407, value[2], value[3])}
        if damage == 'failure_identity' and event == 'proxy.start_tls.failed':
            info = {**info, 'exception': httpcore2.ConnectError('different failure')}
        return original(self, event, info)

    monkeypatch.setattr(_PresendProbe, 'on_trace', observe)
    created = install(monkeypatch, [[failed_plan()]], proxy=PROXY)
    llm, budget = bound()
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    assert len(created) == 1 and created[0][1].is_closed
    evidence = llm.last_call['request_attempts'][0]['transport_evidence']
    assert not evidence['not_sent'] and not evidence['proxy_tunnel_not_sent']
    assert budget.snapshot()['unconfirmed_reserved_tokens'] > 0 and budget.error


def test_target_post_started_is_unknown_even_when_prior_connect_succeeded(monkeypatch):
    original = _PresendProbe.on_trace

    def observe(self, event, info):
        if event == 'http11.send_request_headers.started' and info['request'].method == b'POST':
            created[0][2].streams[0].plan['write_error'] = True
        return original(self, event, info)

    monkeypatch.setattr(_PresendProbe, 'on_trace', observe)
    created = install(monkeypatch, [[{'responses': [CONNECT, payload()]}]], proxy=PROXY)
    llm, budget = bound()
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    assert any(chunk.startswith(b'POST ') for chunk in created[0][2].streams[0].writes)
    assert len(created) == 1 and budget.snapshot()['unconfirmed_reserved_tokens'] > 0
    assert not llm.last_call['request_attempts'][0]['transport_evidence']['not_sent']


@pytest.mark.parametrize('cause', [ssl.SSLCertVerificationError(1, 'certificate failure'),
                                  ssl.CertificateError('hostname mismatch')])
def test_proxy_certificate_errors_are_unknown_and_never_retried(monkeypatch, cause):
    created = install(monkeypatch, [[failed_plan(cause)]], proxy=PROXY)
    llm, budget = bound()
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    assert len(created) == 1 and budget.snapshot()['unconfirmed_reserved_tokens'] > 0
    assert not llm.last_call['request_attempts'][0]['transport_evidence']['proxy_tunnel_not_sent']


def test_generic_connection_error_with_no_transient_cause_does_not_retry(monkeypatch):
    created = install(monkeypatch, [[failed_plan(ValueError('configuration failure'))]], proxy=PROXY)
    llm, budget = bound()
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    assert len(created) == 1 and budget.snapshot()['unconfirmed_reserved_tokens'] == 0
    assert llm.last_call['request_attempts'][0]['transport_evidence']['proxy_tunnel_not_sent']


def test_actual_proxy_redirect_cannot_erase_the_original_post(monkeypatch):
    redirect = (b'HTTP/1.1 307 Temporary Redirect\r\nLocation: https://redirect.invalid/v1/chat/completions\r\n'
                b'Content-Length: 0\r\n\r\n')
    created = install(monkeypatch, [[{'responses': [CONNECT, redirect]}, failed_plan()]], proxy=PROXY)
    llm, budget = bound()
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    assert len(created) == 1 and len(created[0][2].requests) == 2
    assert any(chunk.startswith(b'POST ') for chunk in created[0][2].streams[0].writes)
    assert budget.snapshot()['unconfirmed_reserved_tokens'] > 0
    assert not llm.last_call['request_attempts'][0]['transport_evidence']['proxy_tunnel_not_sent']


def test_https_proxy_target_tls_is_outside_the_narrow_proof(monkeypatch):
    def start_tls(self, ssl_context=None, server_hostname=None, timeout=None):
        self.tls_calls = getattr(self, 'tls_calls', 0) + 1
        if self.tls_calls == 2:
            raise httpcore2.ConnectError('second TLS failed') from ssl.SSLEOFError(8, 'target TLS EOF')
        return self

    monkeypatch.setattr(Stream, 'start_tls', start_tls)
    created = install(monkeypatch, [[{'responses': [CONNECT]}]], proxy='https://proxy.invalid:443')
    llm, budget = bound()
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    assert created[0][2].streams[0].tls_calls == 2
    assert len(created) == 1 and budget.snapshot()['unconfirmed_reserved_tokens'] > 0


def test_selected_pool_with_internal_retry_configuration_is_not_proven(monkeypatch):
    created = install(monkeypatch, [[failed_plan()]], proxy=PROXY)
    original = _PresendProbe.install

    def configure(self, sdk):
        sdk._client._transport._pool._retries = 1
        original(self, sdk)

    monkeypatch.setattr(_PresendProbe, 'install', configure)
    llm, budget = bound()
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    evidence = llm.last_call['request_attempts'][0]['transport_evidence']
    assert not evidence['supported_transport'] and not evidence['proxy_tunnel_not_sent']
    assert len(created) == 1 and budget.snapshot()['unconfirmed_reserved_tokens'] > 0


def test_proxy_retry_rechecks_deadline_before_creating_another_client(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('bonus_agent.llm.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('bonus_agent.llm.time.sleep', lambda delay: clock.__setitem__(0, clock[0] + delay))
    plan = {**failed_plan(), 'callback': lambda: clock.__setitem__(0, 1004.9)}
    created = install(monkeypatch, [[plan]], proxy=PROXY)
    llm = model(); budget = Budget(0, 10000, 5, started=1000).bind(llm)
    with pytest.raises(RunBudgetError, match='时间上限'):
        llm.chat([], max_tokens=100)
    assert len(created) == 1 and budget.reservations == [1] and created[0][1].is_closed
    assert budget.snapshot()['unconfirmed_reserved_tokens'] == 0


def test_proxy_retry_requires_fresh_token_reservation(monkeypatch):
    llm, budget = bound()
    created = install(monkeypatch, [[{**failed_plan(), 'callback': lambda: setattr(budget, 'max_tokens', 1)}]], proxy=PROXY)
    with pytest.raises(RunBudgetError, match='事前拦截'):
        llm.chat([], max_tokens=100)
    assert len(created) == 1 and budget.reservations == [1]
    assert budget.snapshot()['inflight_reserved_tokens'] == 0
    assert budget.snapshot()['unconfirmed_reserved_tokens'] == 0
