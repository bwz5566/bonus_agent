"""大模型调用封装：统一走 OpenAI 兼容接口，云端 API 和本地 vLLM 只是配置不同。"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "models.yaml"


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    calls: int = 0

    def __iadd__(self, o: "Usage"):
        self.prompt_tokens += o.prompt_tokens
        self.completion_tokens += o.completion_tokens
        self.latency_s += o.latency_s
        self.calls += o.calls
        return self


class LLMError(RuntimeError):
    pass


def load_config(path: str | Path | None = None, secrets: str | Path | None = None) -> dict:
    """读取模型配置，并合并同目录下的 secrets.yaml（API Key、专属域名等，不入库）。"""
    path = Path(path or os.environ.get("BONUS_MODELS_CONFIG") or DEFAULT_CONFIG)
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    sp = Path(secrets or os.environ.get("BONUS_SECRETS") or path.with_name("secrets.yaml"))
    if sp.exists():
        with open(sp, encoding="utf-8") as f:
            sec = yaml.safe_load(f) or {}
        for name, over in (sec.get("providers") or {}).items():
            if name not in cfg["providers"]:
                raise LLMError(f"{sp} 里的服务商 {name} 未在 {path} 中配置")
            cfg["providers"][name].update(over or {})
    return cfg


def resolve_models(spec: str | None, cfg: dict) -> list[str]:
    """把 "a,b"、分组名（如 next）或空（= candidates）展开成模型名列表。"""
    if not spec:
        return list(cfg["candidates"])
    out: list[str] = []
    for part in spec.split(","):
        part = part.strip()
        names = cfg.get("groups", {}).get(part, [part])
        out += [n for n in names if n not in out]
    unknown = [n for n in out if n not in cfg["models"]]
    if unknown:
        raise LLMError(f"未配置的模型 {', '.join(unknown)}，可选：{', '.join(cfg['models'])}")
    return out


def provider_key(prov: dict) -> str:
    return os.environ.get(prov.get("api_key_env", ""), "") or prov.get("api_key") or "EMPTY"


def list_remote(provider: str, cfg: dict) -> list[str]:
    """用当前 Key 向服务商查询可调用的模型 ID（/models 接口）。"""
    from openai import OpenAI
    prov = cfg["providers"][provider]
    try:
        client = OpenAI(base_url=prov["base_url"], api_key=provider_key(prov), timeout=60)
        return sorted(m.id for m in client.models.list())
    except Exception as e:
        raise LLMError(f"查询 {provider} 模型列表失败：{e}") from e


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
    api_key: str
    json_mode: str = "json_object"
    extra_body: dict | None = None
    timeout: float = 300
    usage: Usage = field(default_factory=Usage)

    @classmethod
    def from_config(cls, name: str | None = None, config: dict | None = None) -> "LLM":
        cfg = config or load_config()
        name = name or cfg["default"]
        if name not in cfg["models"]:
            raise LLMError(f"未配置的模型 {name}，可选：{', '.join(cfg['models'])}")
        m = cfg["models"][name]
        prov = cfg["providers"][m["provider"]]
        return cls(name, m["model"], prov["base_url"], provider_key(prov), m.get("json_mode", "json_object"),
                   m.get("extra_body"))

    def _client(self):
        from openai import OpenAI
        return OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=self.timeout)

    def chat(self, messages: list[dict], *, schema: dict | None = None, json_out: bool = False,
             temperature: float = 0.1, max_tokens: int = 8192) -> str:
        kwargs = dict(model=self.model, messages=messages, temperature=temperature, max_tokens=max_tokens)
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body
        if json_out or schema:
            if schema and self.json_mode == "json_schema":
                kwargs["response_format"] = {"type": "json_schema", "json_schema": {"name": "output", "schema": schema}}
            else:
                kwargs["response_format"] = {"type": "json_object"}
        t0 = time.monotonic()
        try:
            resp = self._client().chat.completions.create(**kwargs)
        except Exception as e:  # 网络、鉴权、限流等
            raise LLMError(f"{self.name} 调用失败：{e}") from e
        u = Usage(calls=1, latency_s=time.monotonic() - t0)
        if resp.usage:
            u.prompt_tokens, u.completion_tokens = resp.usage.prompt_tokens, resp.usage.completion_tokens
        self.usage += u
        return resp.choices[0].message.content or ""

    def chat_json(self, messages: list[dict], **kw):
        return parse_json(self.chat(messages, json_out=True, **kw))


class FakeLLM(LLM):
    """测试和演练用：按调用顺序返回预设回答，或由函数根据消息生成回答。"""

    def __init__(self, responder: Callable[[list[dict]], str] | list[str], name: str = "fake"):
        super().__init__(name, name, "", "")
        self._responder = responder
        self._i = 0
        self.calls: list[list[dict]] = []

    def chat(self, messages, **kw) -> str:
        self.calls.append(messages)
        self.usage += Usage(calls=1)
        if callable(self._responder):
            return self._responder(messages)
        out = self._responder[min(self._i, len(self._responder) - 1)]
        self._i += 1
        return out
