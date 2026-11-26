"""大模型调用封装：统一走 OpenAI 兼容接口，云端 API 和本地 vLLM 只是配置不同。"""
from __future__ import annotations

import base64
import errno
import json
import logging
import math
import mimetypes
import os
import re
import ssl
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

DEFAULT_CONFIG = Path(__file__).parent / "config" / "models.yaml"      # 随安装包发布的默认配置
REPO_CONFIG = Path(__file__).parent.parent / "config"                   # 源码目录下的 config/（放 secrets.yaml）
BASIS_PART_REASONING = ContextVar('basis_part_reasoning', default=None)


def _first(paths) -> Path | None:
    return next((Path(p) for p in paths if p and Path(p).exists()), None)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    calls: int = 0
    cached_tokens: int = 0            # 输入中命中前缀缓存的部分（服务端返回才有）

    def __iadd__(self, o: "Usage"):
        self.prompt_tokens += o.prompt_tokens
        self.cached_tokens += o.cached_tokens
        self.completion_tokens += o.completion_tokens
        self.latency_s += o.latency_s
        self.calls += o.calls
        return self


class LLMError(RuntimeError):
    pass


class RunBudgetError(LLMError):
    """A shared job allowance stopped the request, not a provider/protocol failure."""


def _moonshot_quota_denial(base_url, exc):
    if base_url.rstrip('/') != 'https://api.moonshot.cn/v1' or getattr(exc, 'status_code', None) != 429:
        return False
    body = getattr(exc, 'body', None)
    error = body.get('error', body) if isinstance(body, dict) else None
    return isinstance(error, dict) and error.get('type') == 'exceeded_current_quota_error'


def _safe_error_text(value, secret=''):
    """Bound and redact exception text; callers do not separately attach raw requests/headers."""
    text = str(value)
    if secret:
        text = text.replace(secret, '[redacted]')
    text = re.sub(r'(?i)\bBearer\s+[^\s,;\"\'}]+', 'Bearer [redacted]', text)
    text = re.sub(r'''(?i)(\b(?:api[_-]?key|authorization|access[_-]?token|token)["']?\s*[:=]\s*["']?)[^\s,;"'}]+''',
                  r'\1[redacted]', text)
    text = re.sub(r'(?i)(https?://)[^/\s:@]+:[^/\s@]+@', r'\1[redacted]@', text)
    return text[:1600]


def _close_client(client, *, secret='', diagnostics=None, field='cleanup_error'):
    """Release owned transport without replacing request outcomes or their usage accounting."""
    if client is None:
        return
    try:
        close = getattr(client, 'close', None)
        if close is not None:
            close()
    except Exception as exc:
        detail = {'type': type(exc).__name__, 'message': _safe_error_text(exc, secret)}
        if diagnostics is not None:
            diagnostics[field] = detail
        logging.getLogger(__name__).warning('HTTP client cleanup failed: %s: %s', detail['type'], detail['message'])


def _request_error(exc, *, secret='', model='', sent=False, response_received=False, extra_causes=()):
    detail = {'type': type(exc).__name__, 'message': _safe_error_text(exc, secret),
              'model': model, 'transport_invoked': sent, 'response_received': response_received}
    causes, seen, cause = [], {id(exc)}, exc.__cause__ or exc.__context__
    while cause is not None and id(cause) not in seen and len(causes) < 4:
        seen.add(id(cause))
        causes.append({'type': type(cause).__name__, 'message': _safe_error_text(cause, secret)})
        cause = cause.__cause__ or cause.__context__
    # httpcore's pool may re-raise the same exception "from None" after tracing;
    # preserve only the causes captured synchronously from that exact failure.
    for cause in extra_causes:
        if id(cause) not in seen and len(causes) < 8:
            seen.add(id(cause))
            causes.append({'type': type(cause).__name__, 'message': _safe_error_text(cause, secret)})
    if causes:
        detail['causes'] = causes
    status = getattr(exc, 'status_code', None)
    if isinstance(status, int):
        detail['status_code'] = status
    request_id = getattr(exc, 'request_id', None)
    if request_id:
        detail['request_id'] = _safe_error_text(request_id, secret)[:160]
    return detail


def _exception_chain(exc):
    chain, seen = [], set()
    while exc is not None and id(exc) not in seen and len(chain) < 16:
        chain.append(exc)
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return chain


class _ProxyTunnelProof:
    """Proof for one empty CONNECT followed by a failed target TLS handshake.

    Restricted to the installed HTTP proxy implementation. Every extra event
    invalidates the proof; no request headers, address or body are exported.
    """
    EVENTS = (
        'connection.connect_tcp.started', 'connection.connect_tcp.complete',
        'http11.send_request_headers.started', 'http11.send_request_headers.complete',
        'http11.send_request_body.started', 'http11.send_request_body.complete',
        'http11.receive_response_headers.started', 'http11.receive_response_headers.complete',
        'proxy.start_tls.started', 'proxy.start_tls.failed',
    )

    def __init__(self, request, pool, callback):
        import httpcore2
        self.valid = (type(pool) is httpcore2.HTTPProxy and pool._retries == 0
                      and pool._proxy_url.scheme == b'http' and request.url.raw_scheme == b'https'
                      and request.method == 'POST' and not {'sni_hostname', 'target'} & request.extensions.keys())
        self.index = 0
        self.failed_exception = None
        self.failure_causes = ()
        self._proxy_origin = pool._proxy_url.origin
        self._host = request.url.raw_host
        self._authority = self._host + b':' + str(request.url.port or 443).encode('ascii')
        self._callback = callback
        self._connect = None

    def _empty_connect(self, request):
        import httpcore2
        from httpcore2._models import ByteStream
        return (type(request) is httpcore2.Request and request.method == b'CONNECT'
                and request.url.origin == self._proxy_origin and request.url.target == self._authority
                and request.extensions.get('trace') == self._callback
                and type(request.stream) is ByteStream and request.stream._content == b'')

    def observe(self, event, info):
        if not self.valid:
            return
        try:
            if self.index >= len(self.EVENTS) or event != self.EVENTS[self.index]:
                self.valid = False
                return
            if self.index == 0:
                self.valid = (info.get('host') == self._proxy_origin.host.decode('ascii')
                              and info.get('port') == self._proxy_origin.port)
            elif self.index == 2:
                self._connect = info.get('request')
                self.valid = self._empty_connect(self._connect)
            elif self.index in (4, 6):
                self.valid = info.get('request') is self._connect and self._empty_connect(self._connect)
            elif self.index == 7:
                response = info.get('return_value')
                self.valid = (type(response) is tuple and len(response) == 4
                              and response[0] in (b'HTTP/1.0', b'HTTP/1.1')
                              and type(response[1]) is int and 200 <= response[1] < 300)
            elif self.index == 8:
                context = info.get('ssl_context')
                self.valid = (info.get('server_hostname') == self._host.decode('ascii')
                              and isinstance(context, ssl.SSLContext) and context.check_hostname
                              and context.verify_mode == ssl.CERT_REQUIRED)
            elif self.index == 9:
                self.failed_exception = info.get('exception')
                self.valid = isinstance(self.failed_exception, BaseException)
                if self.valid:
                    self.failure_causes = tuple(_exception_chain(self.failed_exception))
            self.index += 1
        except Exception:
            self.valid = False

    def not_sent(self, exc):
        causes = [*_exception_chain(exc), *self.failure_causes]
        return bool(self.valid and self.index == len(self.EVENTS)
                    and any(cause is self.failed_exception for cause in _exception_chain(exc))
                    and not any(isinstance(cause, (ssl.SSLCertVerificationError, ssl.CertificateError)) for cause in causes))


class _PresendProbe:
    """Conservative proof for this attempt, never an inference from an error class.

    Only the installed httpx2/httpcore2 transport is covered. Any application
    write disables the direct-connection proof. A separate strict proof covers
    only empty CONNECT -> failed target TLS; HTTP/2 initialization, redirects,
    missing or malformed traces stay unknown. No request data enters diagnostics.
    """
    STAGES = {'connection.connect_tcp', 'connection.start_tls'}
    # Diagnostic-only names: accepting an event here grants no retry authority.
    TRACE_EVENTS = frozenset(f'{stage}.{transition}' for stage in (
        'connection.connect_tcp', 'connection.connect_unix_socket', 'connection.start_tls',
        'proxy.start_tls', 'http11.send_request_headers', 'http11.send_request_body',
        'http11.receive_response_headers', 'http11.receive_response_body', 'http11.response_closed',
        'http2.send_connection_init', 'http2.send_request_headers', 'http2.send_request_body',
        'http2.receive_response_headers', 'http2.receive_response_body',
        'http2.receive_remote_settings', 'http2.response_closed',
    ) for transition in ('started', 'complete', 'failed'))

    def __init__(self):
        self.installed = False
        self.supported = False
        self.valid = True
        self.requests = 0
        self.write_started = False
        self.active = None
        self.failed_stage = ''
        self.failed_exception = None
        self.failure_causes = ()
        self.failure_count = 0
        self.trace_events = []
        self.trace_events_truncated = False
        self._http = None
        self._tunnel = None

    def install(self, client):
        try:
            import httpx2
            import httpcore2
            http = getattr(client, '_client', None)
            if not isinstance(http, httpx2.Client):
                return
            transports = [http._transport, *http._mounts.values()]
            self.supported = all(t is None or (
                type(t) is httpx2.HTTPTransport and
                type(t._pool) in (httpcore2.ConnectionPool, httpcore2.HTTPProxy)
                and getattr(t._pool, '_retries', None) == 0
            ) for t in transports)
            http.event_hooks['request'].append(self.on_request)
            self._http = http
            self.installed = True
        except Exception:
            self.valid = False

    def on_request(self, request):
        try:
            self.requests += 1
            if self.requests != 1 or request.extensions.get('trace') is not None:
                self.valid = False  # redirects/auth replay must not reset earlier send evidence
                if self._tunnel is not None:
                    self._tunnel.valid = False
                return
            request.extensions['trace'] = self.on_trace
            if self.supported and self._http is not None:
                import httpx2
                import httpcore2
                transport = self._http._transport_for_url(request.url)
                if type(transport) is httpx2.HTTPTransport and type(transport._pool) is httpcore2.HTTPProxy:
                    self._tunnel = _ProxyTunnelProof(request, transport._pool, request.extensions['trace'])
        except Exception:
            self.valid = False
            if self._tunnel is not None:
                self._tunnel.valid = False

    def on_trace(self, event, info):
        try:
            if self._tunnel is not None:
                self._tunnel.observe(event, info)
            # Record before fail-closed branches so CONNECT -> proxy TLS errors
            # stay diagnosable. Never copy info or stringify unknown event data.
            self.trace_events.append(event if isinstance(event, str) and event in self.TRACE_EVENTS else 'unknown')
            if len(self.trace_events) > 32:
                del self.trace_events[16]  # first 16 + latest 16, bounded throughout the request
                self.trace_events_truncated = True
            stage, transition = event.rsplit('.', 1)
            if '.send_' in event:
                # started is before the write; even a failed/partial header write is unknown.
                self.write_started = True
            if stage not in self.STAGES or transition not in ('started', 'complete', 'failed'):
                self.valid = False
                return
            if self.failure_count:
                self.valid = False  # an internal retry or continuation is outside this proof
            if transition == 'started':
                if self.active is not None:
                    self.valid = False
                self.active = stage
            else:
                if self.active != stage:
                    self.valid = False
                self.active = None
                if transition == 'failed':
                    self.failure_count += 1
                    self.failed_stage = stage
                    self.failed_exception = info.get('exception')
                    if not isinstance(self.failed_exception, BaseException):
                        self.valid = False
                    else:
                        self.failure_causes = tuple(_exception_chain(self.failed_exception))
        except Exception:
            self.valid = False  # instrumentation must never replace the original outcome
            if self._tunnel is not None:
                self._tunnel.valid = False

    def not_sent(self, exc):
        direct = bool(self.valid and not self.write_started and self.active is None and self.failure_count == 1
                      and any(cause is self.failed_exception for cause in _exception_chain(exc)))
        tunnel = self._tunnel is not None and self._tunnel.not_sent(exc)
        return bool(self.installed and self.supported and self.requests == 1 and (direct or tunnel))

    def causes_for(self, exc):
        # A broken trace cannot authorize a retry, but its captured cause can
        # still explain the very same exception later stripped by the pool.
        if any(cause is self.failed_exception for cause in _exception_chain(exc)):
            return self.failure_causes
        if self._tunnel is not None and any(cause is self._tunnel.failed_exception for cause in _exception_chain(exc)):
            return self._tunnel.failure_causes
        return ()

    def retryable(self, exc):
        if not self.not_sent(exc):
            return False
        causes = [*_exception_chain(exc), *self.causes_for(exc)]
        if any(isinstance(cause, (ssl.SSLCertVerificationError, ssl.CertificateError)) for cause in causes):
            return False
        # A generic ConnectError/SSLError says nothing about whether the cause is transient.
        return any(isinstance(cause, (TimeoutError, ConnectionResetError, ConnectionAbortedError, ssl.SSLEOFError))
                   or (type(cause) is OSError and (
                       cause.errno in (errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED)
                       or getattr(cause, 'winerror', None) in (10060, 10054, 10053)))
                   for cause in causes)

    def summary(self, exc=None):
        return {'installed': self.installed, 'supported_transport': self.supported,
                'trace_valid': self.valid, 'single_request': self.requests == 1,
                'application_write_started': self.write_started, 'failed_stage': self.failed_stage,
                'failure_paired': self.failure_count == 1 and self.active is None,
                'not_sent': self.not_sent(exc) if exc is not None else False,
                'proxy_tunnel_not_sent': bool(exc is not None and self._tunnel is not None and self.requests == 1
                                              and self._tunnel.not_sent(exc)),
                'trace_events': list(self.trace_events), 'trace_events_truncated': self.trace_events_truncated}


THINK_BUDGET = 8192               # 百炼千问思考时每步思考 tokens 的上限
THINK_KEYS = ("enable_thinking", "thinking")      # 各家“思考开关”的参数名：千问 / vLLM 模板用 enable_thinking，DeepSeek 用 thinking


def think_switch(body: dict | None, top: bool = True) -> dict | None:
    """按配置里“关闭思考”的写法，推出“打开思考”的请求参数，不用给每个模型另写一份：
    百炼千问 {enable_thinking: false}、vLLM {chat_template_kwargs: {enable_thinking / thinking: false}}、
    DeepSeek {thinking: {type: disabled}}。找不到关闭开关（模型本身不思考，或已经一直在思考）返回 None。"""
    if not body:
        return None
    out, found = {}, False
    for k, v in body.items():
        if k in THINK_KEYS and v is False:
            out[k], found = True, True
            if top and k == "enable_thinking" and "thinking_budget" not in body:     # 百炼的写法：顺带限制一步的思考长度
                out["thinking_budget"] = THINK_BUDGET
        elif k == "thinking" and isinstance(v, dict) and v.get("type") == "disabled":
            out[k], found = {**v, "type": "enabled"}, True
        elif isinstance(v, dict):
            sub = think_switch(v, top=False)
            if sub is not None:
                out[k], found = {**v, **sub}, True
    return out if found else None


def _merge(a: dict | None, b: dict | None) -> dict:
    """合并请求参数，嵌套的字典（如 chat_template_kwargs）逐项合并，后者优先。"""
    out = dict(a or {})
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _reasoning(msg) -> str:
    """思考内容：vLLM、百炼、DeepSeek 放在 reasoning_content（新版 vLLM 叫 reasoning），没有就是空。"""
    for k in ("reasoning_content", "reasoning"):
        v = getattr(msg, k, None) or (getattr(msg, "model_extra", None) or {}).get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def load_config(path: str | Path | None = None, secrets: str | Path | None = None) -> dict:
    """读取模型配置，并合并 secrets.yaml（API Key、专属域名等，不入库）。

    模型配置依次找：参数、环境变量 BONUS_MODELS_CONFIG、当前目录 config/models.yaml、源码目录 config/models.yaml、
    安装包自带的默认配置。secrets.yaml 依次找：参数、环境变量 BONUS_SECRETS、模型配置同目录、当前目录 config/、
    源码目录 config/、用户目录 ~/.bonus-agent/。
    """
    explicit = path or os.environ.get("BONUS_MODELS_CONFIG")
    if explicit and not Path(explicit).exists():
        raise LLMError(f"找不到模型配置文件 {explicit}")
    path = Path(explicit) if explicit else _first([Path.cwd() / "config" / "models.yaml", REPO_CONFIG / "models.yaml",
                                                     DEFAULT_CONFIG])
    if path is None:
        raise LLMError(f"找不到模型配置文件（安装包应自带 {DEFAULT_CONFIG}），请用 BONUS_MODELS_CONFIG 指定")
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    explicit = secrets or os.environ.get("BONUS_SECRETS")
    if explicit and not Path(explicit).exists():
        raise LLMError(f"找不到密钥文件 {explicit}")
    sp = Path(explicit) if explicit else _first([path.with_name("secrets.yaml"), Path.cwd() / "config" / "secrets.yaml",
                                                   REPO_CONFIG / "secrets.yaml", Path.home() / ".bonus-agent" / "secrets.yaml"])
    if sp is not None:
        with open(sp, encoding="utf-8") as f:
            sec = yaml.safe_load(f) or {}
        for name, over in (sec.get("providers") or {}).items():
            if name not in cfg["providers"]:
                raise LLMError(f"{sp} 里的服务商 {name} 未在 {path} 中配置")
            cfg["providers"][name].update(over or {})
    return cfg


def resolve_models(spec: str | None, cfg: dict) -> list[str]:
    """把 "a,b"、分组名（candidates / open / reference ...）或空（= candidates）展开成模型名列表。"""
    groups = {"candidates": cfg["candidates"], **cfg.get("groups", {})}
    out: list[str] = []
    for part in (spec or "candidates").split(","):
        part = part.strip()
        names = groups.get(part, [part])
        out += [n for n in names if n not in out]
    unknown = [n for n in out if n not in cfg["models"]]
    if unknown:
        raise LLMError(f"未配置的模型或分组 {', '.join(unknown)}。分组：{', '.join(groups)}；模型：{', '.join(cfg['models'])}")
    return out


def provider_key(prov: dict) -> str:
    return os.environ.get(prov.get("api_key_env", ""), "") or prov.get("api_key") or "EMPTY"


def direct(base_url: str) -> bool:
    """内网地址（本机、局域网 IP）：直连，不走系统代理。Windows 上 httpx 会读系统代理设置，
    内网模型服务的请求被转给代理就连不上。"""
    import ipaddress
    from urllib.parse import urlparse
    host = urlparse(base_url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


MAX_RETRIES = 2


def openai_client(base_url: str, api_key: str, timeout: float):
    from openai import DefaultHttpxClient, OpenAI
    http = DefaultHttpxClient(trust_env=False) if direct(base_url) else None
    # Transport retries are bounded separately from business recovery; six retries can hide minutes of failure.
    try:
        return OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, http_client=http, max_retries=MAX_RETRIES)
    except BaseException:
        _close_client(http, secret=api_key)
        raise


def list_remote(provider: str, cfg: dict) -> list[str]:
    """用当前 Key 向服务商查询可调用的模型 ID（/models 接口）。"""
    prov = cfg["providers"][provider]
    client, key = None, provider_key(prov)
    try:
        client = openai_client(prov["base_url"], key, 60)
        return sorted(m.id for m in client.models.list())
    except Exception as e:
        raise LLMError(f"查询 {provider} 模型列表失败：{_safe_error_text(e, key)}") from e
    finally:
        _close_client(client, secret=key)


def image_part(data: bytes | str | Path, mime: str | None = None) -> dict:
    if not isinstance(data, bytes):
        p = Path(data)
        mime = mime or mimetypes.guess_type(p.name)[0] or "image/png"
        data = p.read_bytes()
    b64 = base64.b64encode(data).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{mime or 'image/png'};base64,{b64}"}}


def parse_json(text: str):
    """容错解析：去掉代码块围栏，取第一个完整的 JSON 对象。"""
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.S)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    if start < 0:
        raise LLMError("模型输出中没有 JSON 对象")
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(s[start:], start):
        if in_str:
            esc = (ch == "\\") and not esc
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start:i + 1])
    raise LLMError("模型输出的 JSON 不完整")


@dataclass
class LLM:
    name: str
    model: str
    base_url: str
    api_key: str = field(repr=False)
    json_mode: str = "json_object"
    extra_body: dict | None = None
    tool_mode: str = "native"         # native：OpenAI 兼容的工具调用；json：模型输出 JSON 动作（不支持工具调用时用）
    timeout: float = 300
    cache: str | None = None          # explicit：在消息上加 cache_control 标记（百炼显式缓存）；不写则不加
    tools_extra_body: dict | None = None   # 智能体工具调用时覆盖 extra_body（如打开思考模式），抽取等其他调用不受影响
    vision: bool = True               # 能否看图；不能看图时由 vision_llm 转写图片（没配置就不转写）
    vision_llm: "LLM | None" = None
    checker_llm: "LLM | None" = None  # 交叉验证（独立复读、独立算式）用的另一个模型；没配置就用自己
    # 深度思考（按需）：智能体出错、卡住时这一步临时打开思考，想清楚错在哪再动手；平时不开，省时间和 tokens。
    # think_body 是打开思考时追加的请求参数，默认从 extra_body 的关闭开关推出来（见 think_switch），None 表示这个模型不能切换
    think_body: dict | None = None
    think_max_tokens: int = 16384     # 思考也占输出 tokens，思考的那一步放宽
    think_timeout: float = 900        # 本地模型思考一步可能要几分钟
    think_echo: bool = False          # 下一步把这一步的思考内容带回去（DeepSeek 思考模式下工具调用要求带回）
    think_llm: "LLM | None" = None    # 深度思考的那一步改用的另一个（更强的）模型；平时仍用自己，省钱
    last_reasoning: str = ""          # 最近一次思考调用的思考内容
    usage: Usage = field(default_factory=Usage)
    checker_required: bool = False  # do not silently replace an explicitly required independent reviewer
    recovery_mode: str = "inline"   # advisor: isolated diagnosis; main tool loop stays on its primary model
    last_call: dict = field(default_factory=dict)
    workflow_mode: str = 'staged'
    max_input_tokens: int = 96000
    max_history_chars: int | None = None   # 对话历史上限（字符），见 models.yaml
    run_budget: object | None = field(default=None, repr=False, compare=False)
    tools_thinking: bool = False     # agent tool turns request thinking without consuming recovery/advisor attempts
    min_request_seconds: float = 0   # minimum job time remaining before a potentially billable request
    basis_review_split: bool = False  # independently generate bounded basis review sections
    basis_review_reasoning_effort: str | None = None
    repair_llm: "LLM | None" = None
    fixed_temperature: float | None = None
    json_with_thinking: bool = False
    fallback_api_key: str = field(default='', repr=False, compare=False)

    @classmethod
    def from_config(cls, name: str | None = None, config: dict | None = None, checker: str | None = None,
                    *, _auxiliary: bool = False, _path: tuple = ()) -> "LLM":
        """checker：交叉验证用的模型名（不写就看 models.yaml 里这个模型的 checker 项，都没有就自己验证）。"""
        cfg = config or load_config()
        name = name or cfg["default"]
        if name in _path:
            raise LLMError(f"模型路由循环：{' -> '.join((*_path, name))}")
        route_path = (*_path, name)
        if name not in cfg["models"]:
            raise LLMError(f"未配置的模型 {name}，可选：{', '.join(cfg['models'])}")
        m = cfg["models"][name]
        prov = cfg["providers"][m["provider"]]
        key = provider_key(prov)
        if prov.get('require_api_key') and (not str(key).strip() or key == 'EMPTY'):
            raise LLMError(f"模型 {name} 缺少 API Key。请设置 {prov['api_key_env']}，或填写 "
                           f"config/secrets.yaml 的 providers.{m['provider']}.api_key。")
        llm = cls(name, m["model"], prov["base_url"], key, m.get("json_mode", "json_object"),
                  m.get("extra_body"), m.get("tool_mode", "native"), cache=m.get("cache"),
                  tools_extra_body=m.get("tools_extra_body"), timeout=float(m.get("timeout", 300)))
        if m['provider'] == 'moonshot' and llm.base_url.rstrip('/') == 'https://api.moonshot.cn/v1':
            spare = os.environ.get(prov.get('fallback_api_key_env', ''), '') or prov.get('fallback_api_key')
            if isinstance(spare, str) and spare.strip() and spare != llm.api_key:
                llm.fallback_api_key = spare.strip()
        if (temperature := m.get('fixed_temperature')) is not None:
            if not isinstance(temperature, (int, float)) or isinstance(temperature, bool) or not 0 <= temperature <= 2:
                raise LLMError(f'模型 {name} 的 fixed_temperature 须为 0 至 2 的数值')
            llm.fixed_temperature = float(temperature)
        llm.json_with_thinking = m.get('json_with_thinking', False)
        t = m.get("think", "auto")      # auto：按关闭开关推出来；false：不切换；字典：打开思考时的请求参数
        if t == "auto":
            llm.think_body = think_switch(_merge(m.get("extra_body"), m.get("tools_extra_body")))
        elif isinstance(t, dict):
            llm.think_body = t
        elif t not in (False, None):
            raise LLMError(f"模型 {name} 的 think 应是 auto、false 或请求参数，得到 {t!r}")
        llm.think_max_tokens = int(m.get("think_max_tokens", llm.think_max_tokens))
        llm.think_timeout = float(m.get("think_timeout", max(llm.think_timeout, llm.timeout)))
        llm.think_echo = bool(m.get("think_echo", "thinking" in (llm.think_body or {})))
        tools_thinking = m.get('tools_thinking', False)
        if not isinstance(tools_thinking, bool):
            raise LLMError(f'模型 {name} 的 tools_thinking 须为 true 或 false')
        llm.tools_thinking = tools_thinking
        basis_review_split = m.get('basis_review_split', False)
        if not isinstance(basis_review_split, bool):
            raise LLMError(f'模型 {name} 的 basis_review_split 须为 true 或 false')
        llm.basis_review_split = basis_review_split
        effort = m.get('basis_review_reasoning_effort')
        if effort is not None and (not basis_review_split or effort not in ('low', 'high')
                                   or 'reasoning_effort' not in (llm.think_body or {})):
            raise LLMError(f'模型 {name} 的分段思考配置须启用分段，且模型已配置 reasoning_effort；可选 low 或 high')
        llm.basis_review_reasoning_effort = effort
        minimum = m.get('min_request_seconds', 0)
        try:
            minimum = float(minimum) if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) else math.nan
        except OverflowError:
            minimum = math.nan
        if not math.isfinite(minimum) or minimum < 0:
            raise LLMError(f'模型 {name} 的 min_request_seconds 须为有限非负数字')
        llm.min_request_seconds = minimum
        tm = m.get("think_model") if not _auxiliary else None
        if tm and tm != name:
            if tm not in cfg["models"]:
                raise LLMError(f"深度思考模型 {tm} 没有配置，可选：{', '.join(cfg['models'])}")
            llm.think_llm = cls.from_config(tm, cfg, _auxiliary=True, _path=route_path)
            llm.think_llm.usage = llm.usage            # 用量记在同一处
            llm.think_echo = llm.think_echo or llm.think_llm.think_echo
        if llm.tools_thinking and not llm.can_think:
            raise LLMError(f'模型 {name} 设置了 tools_thinking=true，但没有可用的 think 参数或 think_model')
        v = m.get("vision", True)       # true：自己看图；false：不能看图；模型名：用那个模型转写图片
        if isinstance(v, str):
            if v == name or cfg["models"].get(v, {}).get("vision", True) is not True:
                raise LLMError(f"模型 {name} 的 vision 应是另一个能看图的模型名，得到 {v}")
            # A text-only checker can delegate image transcription back to the primary model.
            llm.vision, llm.vision_llm = False, cls.from_config(v, cfg, _auxiliary=True)
            llm.vision_llm.usage = llm.usage          # 看图的用量记在同一处
        else:
            llm.vision = bool(v)
        c = (checker or m.get("checker")) if not _auxiliary else None
        llm.checker_required = bool(m.get("checker_required", False))
        llm.recovery_mode = m.get("recovery_mode", "inline")
        llm.workflow_mode = m.get('workflow_mode','staged')
        if llm.workflow_mode not in ('staged','legacy'):
            raise LLMError('workflow_mode须为staged或legacy')
        llm.max_input_tokens = int(m.get('max_input_tokens',96000))
        llm.max_history_chars = int(m.get('max_history_chars', 0)) or None   # 没写由智能体用默认值
        if c and c != name:
            if c not in cfg["models"]:
                raise LLMError(f"交叉验证模型 {c} 没有配置，可选：{', '.join(cfg['models'])}")
            llm.checker_llm = cls.from_config(c, cfg, _auxiliary=True, _path=route_path)
            llm.checker_llm.usage = llm.usage          # 用量记在同一处
        repair = m.get('repair_model') if not _auxiliary else None
        if repair:
            if not isinstance(repair, str) or repair == name or repair not in cfg['models']:
                raise LLMError(f'模型 {name} 的 repair_model 须引用另一个已配置模型')
            provider = cfg['providers'][cfg['models'][repair]['provider']]
            if provider_key(provider) != 'EMPTY':
                llm.repair_llm = cls.from_config(repair, cfg, _auxiliary=True, _path=route_path)
                llm.repair_llm.usage = llm.usage
        return llm

    @property
    def image_llm(self) -> "LLM | None":
        """用来看图的模型：自己能看图就是自己，否则是配置的 vision 模型，都没有返回 None。"""
        return self if self.vision else self.vision_llm

    def _usage(self, resp, t0: float):
        u = Usage(calls=1, latency_s=time.monotonic() - t0)
        usage = getattr(resp, 'usage', None)
        counts = [getattr(usage, key, None) for key in ('prompt_tokens', 'completion_tokens')]
        reported = all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in counts)
        estimate=self.__dict__.pop('_pending_context_estimate',None)
        if reported:
            u.prompt_tokens, u.completion_tokens = counts
            if estimate and u.prompt_tokens:
                self._context_token_ratio=max(getattr(self,'_context_token_ratio',0),u.prompt_tokens/estimate)
                self.last_call.update(estimated_input_tokens=estimate,actual_input_tokens=u.prompt_tokens)
            details = getattr(usage, "prompt_tokens_details", None)
            cached = getattr(details, 'cached_tokens', None)
            u.cached_tokens = min(cached, u.prompt_tokens) if isinstance(cached, int) and 0 <= cached else 0
        self.last_call['usage_reported'] = reported
        if self.run_budget is not None:
            self.run_budget.record_usage(self, u)
        else:
            self.usage += u
        return reported

    @property
    def can_think(self) -> bool:
        return self.think_body is not None or self.think_llm is not None

    def _prepare(self, messages: list[dict]) -> list[dict]:
        """去掉内部用的 _cache 标记；开启显式缓存时，把标记换成 cache_control（缓存到该消息为止的前缀）。
        思考内容（reasoning_content）只在要求带回的服务上保留，别的服务不认这个字段。"""
        out = []
        for m in messages:
            if "reasoning_content" in m and not (self.think_echo and m["reasoning_content"]):
                m = {k: v for k, v in m.items() if k != "reasoning_content"}
            if "_cache" not in m:
                out.append(m)
                continue
            m = {k: v for k, v in m.items() if k != "_cache"}
            if self.cache == "explicit" and isinstance(m.get("content"), str) and m["content"]:
                m["content"] = [{"type": "text", "text": m["content"], "cache_control": {"type": "ephemeral"}}]
            out.append(m)
        return out

    def _client(self, think: bool = False):
        timeout = self.think_timeout if think else self.timeout
        if self.run_budget is not None:
            timeout = self.run_budget.request_timeout(timeout)
        client = openai_client(self.base_url, self.api_key, timeout)
        if self.run_budget is not None:
            # A retry is another potentially billable request. SDK retries cannot reserve
            # tokens or re-evaluate the job deadline, so budgeted jobs disable them.
            try:
                client = client.with_options(max_retries=0, timeout=self.run_budget.request_timeout(timeout))
            except BaseException:
                _close_client(client, secret=self.api_key, diagnostics=self.last_call)
                raise
        return client

    def _send(self, kwargs, think):
        started = time.monotonic()
        attempts = None
        if self.run_budget is not None:
            attempts = self.last_call['request_attempts'] = []
        try:
            try:
                return self._send_current(kwargs, think, attempts)
            except LLMError as exc:
                if not self.fallback_api_key or not _moonshot_quota_denial(self.base_url, exc.__cause__):
                    raise
                self.api_key, self.fallback_api_key = self.fallback_api_key, ''
                self.last_call['credential_failover'] = {'provider': 'moonshot', 'reason': 'exceeded_current_quota_error'}
            return self._send_current(kwargs, think, attempts)
        finally:
            self.last_call['request_seconds'] = round(time.monotonic() - started, 3)

    def _send_current(self, kwargs, think, attempts):
        if attempts is None:
            return self._send_once(kwargs, think)
        for attempt in range(3):  # initial request plus at most two proven pre-send retries
            for field in ('error_type', 'error_message', 'request_error'):
                self.last_call.pop(field, None)
            try:
                return self._send_once(kwargs, think, probe=_PresendProbe(), attempts=attempts)
            except LLMError as exc:
                if not getattr(exc, '_presend_retry_safe', False) or attempt == 2:
                    raise
                self.last_call['presend_retries'] = attempt + 1
                try:
                    time.sleep(self.run_budget.request_timeout((0.2, 0.5)[attempt]))
                except RunBudgetError:
                    self.last_call.update(status='budget_blocked', budget=self.run_budget.snapshot())
                    raise

    def _send_once(self, kwargs, think, *, probe=None, attempts=None):
        budget, reservation = self.run_budget, None
        if budget is not None:
            from bonus_agent.context import estimate_tokens
            packet = {k: kwargs[k] for k in ('messages', 'tools', 'response_format', 'extra_body') if k in kwargs}
            baseline = estimate_tokens(packet)
            estimate = budget.estimate_input(self, baseline)
            output = max(int(kwargs.get('max_tokens', 0)),
                         int((kwargs.get('extra_body') or {}).get('max_tokens', 0)),
                         int((kwargs.get('extra_body') or {}).get('max_completion_tokens', 0)))
            try:
                reservation = budget.reserve(self, estimate, output)
            except RunBudgetError:
                self.last_call.update(status='budget_blocked', budget=budget.snapshot(),
                                      reserved_input_estimate=estimate, requested_output_tokens=output)
                raise
            self.last_call.update(budget=budget.snapshot(), reserved_input_estimate=estimate,
                                  budget_request_sequence=reservation,
                                  requested_output_tokens=output, transport_retries_limit=0)
        t0, sent, reported, response_received, failure = time.monotonic(), False, False, False, None
        client, request_exception, not_sent = None, None, False
        try:
            client = self._client(think)
            if probe is not None:
                probe.install(client)
            if budget is not None:
                budget.ensure_request_window(self)  # client setup may consume the remaining admission margin
            sent = True
            resp = client.chat.completions.create(**kwargs)
            response_received = True
            reported = self._usage(resp, t0)
            if budget is not None and reported:
                budget.observe_input(self, baseline, resp.usage.prompt_tokens)
            if not reported:
                usage = getattr(resp, 'usage', None)
                invalid = [key for key in ('prompt_tokens', 'completion_tokens')
                           if not isinstance(getattr(usage, key, None), int)
                           or isinstance(getattr(usage, key, None), bool) or getattr(usage, key) < 0]
                failure = {'type': 'InvalidUsage', 'message': '响应未提供完整有效 usage；缺失或非法字段：' + '、'.join(invalid),
                           'model': self.model, 'transport_invoked': sent, 'response_received': True,
                           'response_id': _safe_error_text(getattr(resp, 'id', ''), self.api_key)[:160], 'invalid_usage_fields': invalid}
                self.last_call.update(error_type=failure['type'], error_message=failure['message'], request_error=failure)
        except Exception as exc:
            request_exception = exc
            not_sent = probe.not_sent(exc) if probe is not None else False
            status = getattr(exc, 'status_code', None)
            response_received = (response_received or getattr(exc, 'response', None) is not None
                                 or isinstance(status, int) and 100 <= status <= 599)
            not_sent = not_sent and not response_received
            failure = _request_error(exc, secret=self.api_key, model=self.model,
                                     sent=sent, response_received=response_received,
                                     extra_causes=probe.causes_for(exc) if probe is not None else ())
            self.last_call.update(status='budget_blocked' if isinstance(exc, RunBudgetError) else 'failed',
                                  error_type=failure['type'], error_message=failure['message'], request_error=failure)
            if isinstance(exc, RunBudgetError):
                raise
            error = LLMError(f'{self.name} 调用失败（{failure["type"]}）：{failure["message"]}')
            error._presend_retry_safe = not_sent and probe.retryable(exc) if probe is not None else False
            raise error from exc
        finally:
            self.last_call['request_seconds'] = round(time.monotonic() - t0, 3)
            self.last_call['response_received'] = response_received
            _close_client(client, secret=self.api_key, diagnostics=self.last_call)
            if budget is not None and reservation is not None:
                if failure is not None:
                    failure['budget_request_sequence'] = reservation
                quota_denial = _moonshot_quota_denial(self.base_url, request_exception)
                if quota_denial:
                    failure['provider_rejection'] = 'exceeded_current_quota_error'
                budget.settle(reservation, reported=reported, sent=sent and not not_sent, failure=failure,
                              quota_denial=quota_denial)
                self.last_call['budget'] = budget.snapshot()
            if attempts is not None:
                diagnostic = {'attempt': len(attempts) + 1, 'budget_request_sequence': reservation,
                              'transport_invoked': sent, 'usage_reported': reported,
                              'request_seconds': self.last_call['request_seconds'],
                              'dispatch_status': 'response' if response_received else
                                                 'not_sent' if not_sent or not sent else 'unknown',
                              'transport_evidence': probe.summary(request_exception)}
                if failure is not None:
                    diagnostic['request_error'] = failure
                    self.last_call.setdefault('first_request_error', failure)
                attempts.append(diagnostic)
        if budget is not None:
            try:
                budget.after_response()
            except RunBudgetError:
                self.last_call.update(status='budget_blocked', budget=budget.snapshot())
                raise
        return resp

    def _think(self, think: bool, body: dict, max_tokens: int) -> tuple[bool, dict, int]:
        """这一步是否真的思考、请求参数、输出上限。模型不能切换思考时照常调用。"""
        if not (think and self.can_think):
            return False, body, max_tokens
        thinking_body = self.think_body
        scoped = BASIS_PART_REASONING.get()
        if scoped is not None and scoped[0] is self:
            thinking_body = {**(thinking_body or {}), 'reasoning_effort': scoped[1]}
        return True, _merge(body, thinking_body), max(max_tokens, self.think_max_tokens)

    def context_window(self) -> int | None:
        """模型服务的上下文长度（vLLM 在 /models 里返回 max_model_len）；查不到返回 None，不影响调用。"""
        if not hasattr(self, "_ctx"):
            self._ctx = None
            client = None
            try:
                timeout = self.run_budget.request_timeout(20) if self.run_budget is not None else 20
                client = openai_client(self.base_url, self.api_key, timeout)
                if self.run_budget is not None:
                    client = client.with_options(max_retries=0, timeout=self.run_budget.request_timeout(timeout))
                for m in client.models.list():
                    if m.id == self.model:
                        extra=getattr(m,'model_extra',None) or {}
                        n = getattr(m,'context_window',None) or extra.get('context_window') or getattr(m, "max_model_len", None) or extra.get("max_model_len")
                        self._max_output_tokens=getattr(m,'max_output_tokens',None) or extra.get('max_output_tokens')
                        self._ctx = int(n) if n else None
                if self.run_budget is not None:
                    self.run_budget.after_response()
            except RunBudgetError:
                raise
            except Exception:
                pass
            finally:
                _close_client(client, secret=self.api_key, diagnostics=self.last_call, field='context_cleanup_error')
        return self._ctx

    def chat(self, messages: list[dict], *, schema: dict | None = None, json_out: bool = False,
             temperature: float = 0.1, max_tokens: int = 8192, think: bool = False) -> str:
        """think：这一次打开思考（JSON 协议的智能体出错后用）。思考时不加 response_format：
        部分服务思考模式下不支持 JSON 输出，结果仍按容错方式解析。思考内容放在 last_reasoning。"""
        if think and self.think_llm is not None:
            if self.run_budget is not None:
                self.run_budget.bind(self)
            try:
                return self.think_llm.chat(messages, schema=schema, json_out=json_out, temperature=temperature,
                                           max_tokens=max_tokens, think=True)
            finally:
                self.last_reasoning = self.think_llm.last_reasoning
                self.last_call = self.think_llm.last_call
        think, body, max_tokens = self._think(think, dict(self.extra_body or {}), max_tokens)
        if getattr(self,'_max_output_tokens',None):max_tokens=min(max_tokens,int(self._max_output_tokens))
        if self.fixed_temperature is not None:
            temperature = self.fixed_temperature
        kwargs = dict(model=self.model, messages=self._prepare(messages), temperature=temperature, max_tokens=max_tokens)
        if body:
            kwargs["extra_body"] = body
        if (json_out or schema) and (not think or self.json_with_thinking):
            if schema and self.json_mode == "json_schema":
                kwargs["response_format"] = {"type": "json_schema", "json_schema": {"name": "output", "schema": schema}}
            else:
                kwargs["response_format"] = {"type": "json_object"}
        self.last_call = {'model': self.model, 'thinking': think, 'status': 'requesting',
                          'reasoning_effort': (body or {}).get('reasoning_effort'), 'transport_retries_limit': MAX_RETRIES}
        resp = self._send(kwargs, think)
        self._record_call(resp, think)
        self.last_reasoning = _reasoning(resp.choices[0].message) if think else ""
        return resp.choices[0].message.content or ""

    def chat_json(self, messages: list[dict], **kw):
        return parse_json(self.chat(messages, json_out=True, **kw))

    def _record_call(self, resp, think):
        reason = getattr(resp.choices[0], 'finish_reason', None)
        self.last_call = {**self.last_call, 'model': getattr(resp, 'model', self.model), 'thinking': think,
                          'response_id': getattr(resp, 'id', ''), 'request_id': getattr(resp, '_request_id', ''),
                          'finish_reason': reason, 'status': 'completed'}
        if reason in {'length', 'content_filter'}:
            raise LLMError(f'{self.name} 输出未完成（finish_reason={reason}），未执行工具或应用结果')

    def chat_tools(self, messages: list[dict], tools: list[dict], *, temperature: float = 0.1,
                   max_tokens: int = 8192, think: bool = False) -> dict:
        """工具调用。返回 {"content": 文本, "tool_calls": [{"id", "name", "arguments"(JSON 字符串)}], "reasoning": 思考内容}。
        think：这一步打开思考（模型不能切换时照常调用，reasoning 为空）；配了 think_model 时这一步交给那个模型。"""
        if think and self.think_llm is not None:
            if self.run_budget is not None:
                self.run_budget.bind(self)
            try:
                return self.think_llm.chat_tools(messages, tools, temperature=temperature, max_tokens=max_tokens, think=True)
            finally:
                self.last_reasoning = self.think_llm.last_reasoning
                self.last_call = self.think_llm.last_call
        think, body, max_tokens = self._think(think, _merge(self.extra_body, self.tools_extra_body), max_tokens)
        if getattr(self,'_max_output_tokens',None):max_tokens=min(max_tokens,int(self._max_output_tokens))
        if self.fixed_temperature is not None:
            temperature = self.fixed_temperature
        kwargs = dict(model=self.model, messages=self._prepare(messages), tools=tools, temperature=temperature,
                      max_tokens=max_tokens)
        if body:
            kwargs["extra_body"] = body
        self.last_call = {'model': self.model, 'thinking': think, 'status': 'requesting',
                          'reasoning_effort': (body or {}).get('reasoning_effort'), 'transport_retries_limit': MAX_RETRIES}
        resp = self._send(kwargs, think)
        self._record_call(resp, think)
        msg = resp.choices[0].message
        calls = [{"id": c.id, "name": c.function.name, "arguments": c.function.arguments or "{}"}
                 for c in (msg.tool_calls or [])]
        reply = {"content": msg.content or "", "tool_calls": calls, "reasoning": _reasoning(msg) if think else ""}
        # Gemini signs the original assistant turn, including the location of the signature on parallel calls.
        if any(isinstance(extra := getattr(part, 'extra_content', None), dict) and extra.get('google')
               for part in [msg, *(msg.tool_calls or [])]):
            reply['signed_message'] = msg.model_dump(mode='json', exclude_none=True, exclude_unset=True,
                                                    include={'role', 'content', 'tool_calls', 'extra_content'})
        return reply


class FakeLLM(LLM):
    """测试和演练用：按调用顺序返回预设回答，或由函数根据消息生成回答。"""

    def context_window(self) -> int | None:
        return getattr(self, "ctx", None)        # 测试里设 ctx 模拟模型服务的上下文长度

    def __init__(self, responder: Callable[[list[dict]], str] | list[str], name: str = "fake"):
        super().__init__(name, name, "", "")
        self.workflow_mode = 'legacy'  # Existing scripted replays use the old protocol; staged tests opt in explicitly.
        self._responder = responder
        self._i = 0
        self.calls: list[list[dict]] = []
        self.images: str | None = None      # 设置后，看图的调用一律返回它，不消耗预设回答
        self.reread: str | None = None      # 独立复读的回答（JSON 文本）；不设时复读结果为空
        self.calc: str | list[str] | None = None     # 独立算式的回答（几次改正时给列表，依次返回）；不设时为空
        self.source: str | None = None      # 提问前查原文的回答（JSON 文本）；不设时为“材料里没有”
        self.triage = None                  # 材料预筛的回答：JSON 文本，或按消息生成回答的函数；不设时一页都不折叠
        self.stuck: str | None = None       # 卡住时改写成给财务的问题（JSON 文本）；不设时用程序的通用问法

    def _next(self, *args):
        if callable(self._responder):
            return self._responder(*args)
        out = self._responder[min(self._i, len(self._responder) - 1)]
        self._i += 1
        return out

    def chat(self, messages, **kw) -> str:
        self.calls.append(messages)
        self.usage += Usage(calls=1)
        if str(messages[0].get("content", "")).startswith("【独立复读】") and \
                (self.reread is not None or not callable(self._responder)):
            return self.reread or '{"items": []}'    # 独立复读：不消耗预设回答（脚本里接下来是工具调用）
        if str(messages[0].get("content", "")).startswith("【卡住提问】") and \
                (self.stuck is not None or not callable(self._responder)):
            return self.stuck or '{"program_issue": false}'   # 不消耗预设回答
        if str(messages[0].get("content", "")).startswith("【材料预筛】"):
            if callable(self.triage):
                return self.triage(messages)
            return self.triage or '{"pages": []}'     # 材料预筛：不消耗预设回答
        if str(messages[0].get("content", "")).startswith("【查原文】") and \
                (self.source is not None or not callable(self._responder)):
            return self.source or '{"answerable": false}'
        if str(messages[0].get("content", "")).startswith("【独立算式】") and \
                (self.calc is not None or not callable(self._responder)):
            if isinstance(self.calc, list):
                return self.calc.pop(0) if len(self.calc) > 1 else self.calc[0]
            return self.calc or '{"schemes": {}}'

        if callable(self._responder):
            return self._responder(messages)
        has_image = any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"])
                        for m in messages)
        if has_image and (self.images is not None or not isinstance(self._responder[min(self._i, len(self._responder) - 1)], str)):
            return self.images or "（图片转写）"   # 智能体开工前转写图片；脚本里接下来是工具调用，不消耗脚本
        return self._next()

    def chat_tools(self, messages, tools, **kw) -> dict:
        """预设回答为 {"tool": 名称, "args": {...}}、它们的列表，或纯文本。"""
        self.calls.append(messages)
        self.usage += Usage(calls=1)
        out = self._next(messages, tools) if callable(self._responder) else self._next()
        if isinstance(out, str):
            return {"content": out, "tool_calls": []}
        items = out if isinstance(out, list) else [out]
        return {"content": items[0].get("thought", ""), "tool_calls": [     # 顶层 thought 当作模型写在正文里的思考
            {"id": f"call_{self.usage.calls}_{i}", "name": it["tool"],
             "arguments": json.dumps(it.get("args", {}), ensure_ascii=False)} for i, it in enumerate(items)]}
