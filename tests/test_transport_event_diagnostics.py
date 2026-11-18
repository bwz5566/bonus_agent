"""Transport event names explain interruption stages without expanding retry proof."""
import json
import ssl

import pytest

from bonus_agent.llm import LLMError, _PresendProbe
from tests.test_presend_transport_retry import bound, install


def test_real_installed_proxy_certificate_failure_is_visible_but_still_unknown(monkeypatch):
    connect = b'HTTP/1.1 200 Connection Established\r\n\r\n'
    created = install(monkeypatch, [[{'responses': [connect], 'tls_error': ssl.SSLCertVerificationError(1, 'certificate verification failed')}]],
                      proxy='http://private-proxy.invalid:8080')
    llm, budget = bound()
    with pytest.raises(LLMError):
        llm.chat([], max_tokens=100)
    assert len(created) == 1 and created[0][1].is_closed
    writes = created[0][2].streams[0].writes
    assert writes[0].startswith(b'CONNECT ') and not any(write.startswith(b'POST ') for write in writes)
    attempts = llm.last_call['request_attempts']
    assert len(attempts) == 1 and attempts[0]['dispatch_status'] == 'unknown'
    evidence = attempts[0]['transport_evidence']
    events = evidence['trace_events']
    assert events.index('http11.send_request_headers.started') < events.index('proxy.start_tls.started')
    assert events[-1] == 'proxy.start_tls.failed'
    assert evidence['application_write_started'] and not evidence['trace_valid'] and not evidence['not_sent']
    assert evidence['failed_stage'] == ''  # the original proof logic is intentionally unchanged
    assert not evidence['trace_events_truncated']
    assert budget.snapshot()['unconfirmed_reserved_tokens'] > 0 and budget.error
    assert 'private-proxy' not in json.dumps(evidence) and 'CONNECT ' not in json.dumps(evidence)


def test_event_storage_retains_first_and_last_sixteen_only():
    probe = _PresendProbe()
    events = sorted(probe.TRACE_EVENTS) * 5
    for event in events:
        probe.on_trace(event, {})
        assert len(probe.trace_events) <= 32
    summary = probe.summary()
    assert summary['trace_events'] == events[:16] + events[-16:]
    assert summary['trace_events_truncated']
    summary['trace_events'].clear()
    assert len(probe.trace_events) == 32  # a returned diagnostic is not live probe state


def test_unknown_names_and_trace_info_are_not_logged():
    class PrivateInfo(dict):
        def __repr__(self):
            raise AssertionError('diagnostics must not serialize trace info')

    probe = _PresendProbe()
    secret = 'api-key-private-host-and-body'
    probe.on_trace('arbitrary.event.' + secret, PrivateInfo(headers=secret, host=secret, body=secret))
    probe.on_trace('proxy.start_tls.failed', PrivateInfo(headers=secret, exception=ssl.SSLEOFError(secret)))
    probe.on_trace({'secret': secret}, PrivateInfo(body=secret))
    summary = probe.summary()
    assert summary['trace_events'] == ['unknown', 'proxy.start_tls.failed', 'unknown']
    assert secret not in json.dumps(summary)
    assert not summary['trace_valid'] and not summary['not_sent']


def test_recorded_names_do_not_expand_existing_presend_proof():
    assert _PresendProbe.STAGES == {'connection.connect_tcp', 'connection.start_tls'}
    failure = ssl.SSLEOFError(8, 'handshake EOF')
    direct = _PresendProbe()
    direct.installed = direct.supported = True
    direct.requests = 1
    direct.on_trace('connection.start_tls.started', {})
    direct.on_trace('connection.start_tls.failed', {'exception': failure})
    assert direct.not_sent(failure) and direct.retryable(failure)
    proxy = _PresendProbe()
    proxy.installed = proxy.supported = True
    proxy.requests = 1
    proxy.on_trace('proxy.start_tls.started', {})
    proxy.on_trace('proxy.start_tls.failed', {'exception': failure})
    assert not proxy.not_sent(failure) and not proxy.retryable(failure)
