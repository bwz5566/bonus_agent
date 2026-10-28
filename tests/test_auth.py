import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("sign", Path(__file__).parent.parent / "examples" / "auth" / "sign.py")
sign = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sign)


def test_signature_roundtrip_and_rejections():
    seen = set()

    def seen_nonce(app, n):
        if (app, n) in seen:
            return False
        seen.add((app, n))
        return True

    keys = {"hr-gateway": ["old-key", "new-key"]}
    kw = dict(secrets_of=lambda a: keys.get(a, []), seen_nonce=seen_nonce)
    body = b'{"amount": "1000000.00"}'
    h = sign.sign_headers("new-key", "post", "/api/v1/runs/1/adjustments", "b=2&a=1", body,
                          app_id="hr-gateway", user_id="u1", scopes="小组:G01")
    assert sign.verify(h, "POST", "/api/v1/runs/1/adjustments", "a=1&b=2", body, **kw)
    assert not sign.verify(h, "POST", "/api/v1/runs/1/adjustments", "a=1&b=2", body, **kw)  # 重放
    h2 = sign.sign_headers("old-key", "POST", "/x", "", body, app_id="hr-gateway", user_id="u1")
    assert not sign.verify(h2, "POST", "/x", "", b'{"amount": "9"}', **kw)          # 篡改请求体
    assert not sign.verify({**h2, "X-User-Id": "boss"}, "POST", "/x", "", body, **kw)  # 冒充用户
    assert not sign.verify(h2, "POST", "/x", "", body, now=int(h2["X-Timestamp"]) + 600, **kw)  # 过期
    assert sign.verify(h2, "POST", "/x", "", body, **kw)                             # 旧密钥轮换期有效
