"""服务间鉴权：HMAC-SHA256 签名头（与技术方案「API 规范 / 服务间鉴权」一致）。

调用方用 sign_headers() 生成请求头；本服务用 verify() 校验。nonce 去重由调用者传入的
seen_nonce 回调完成（生产用 Redis `SET key 1 NX EX 600`，返回 False 表示已见过）。
HTTP 头只能放 ASCII，X-User-Scopes 里的中文节点 id 先做 URL 编码，签名按实际发送的头值计算。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from typing import Callable
from urllib.parse import parse_qsl, urlencode

MAX_SKEW = 300  # 秒


def canonical(method: str, path: str, query: str, app_id: str, user_id: str, scopes: str,
              ts: str, nonce: str, body: bytes) -> str:
    q = urlencode(sorted(parse_qsl(query, keep_blank_values=True)))
    return "\n".join([method.upper(), path, q, app_id, user_id, scopes, ts, nonce,
                      hashlib.sha256(body).hexdigest()])


def signature(secret: str, text: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), text.encode(), hashlib.sha256).digest()).decode()


def sign_headers(secret: str, method: str, path: str, query: str, body: bytes, *,
                 app_id: str, user_id: str, scopes: str = "") -> dict[str, str]:
    ts, nonce = str(int(time.time())), secrets.token_hex(16)
    text = canonical(method, path, query, app_id, user_id, scopes, ts, nonce, body)
    return {"X-App-Id": app_id, "X-User-Id": user_id, "X-User-Scopes": scopes,
            "X-Timestamp": ts, "X-Nonce": nonce, "X-Signature": signature(secret, text)}


def verify(headers: dict[str, str], method: str, path: str, query: str, body: bytes, *,
           secrets_of: Callable[[str], list[str]], seen_nonce: Callable[[str, str], bool],
           now: float | None = None) -> bool:
    """secrets_of(app_id) 返回该调用方当前有效的密钥（轮换期可有新旧两把）。"""
    h = {k.lower(): v for k, v in headers.items()}
    try:
        app_id, user_id, ts, nonce, sig = (h["x-app-id"], h["x-user-id"], h["x-timestamp"],
                                           h["x-nonce"], h["x-signature"])
        ts_ok = abs((now or time.time()) - int(ts)) <= MAX_SKEW
    except (KeyError, ValueError):
        return False
    if not ts_ok or len(nonce) < 16:
        return False
    text = canonical(method, path, query, app_id, user_id, h.get("x-user-scopes", ""), ts, nonce, body)
    if not any(hmac.compare_digest(signature(s, text), sig) for s in secrets_of(app_id)):
        return False
    return seen_nonce(app_id, nonce)  # 签名通过后再占用 nonce，防止伪造请求耗尽 nonce
