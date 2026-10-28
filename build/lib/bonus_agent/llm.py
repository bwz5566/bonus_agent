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

DEFAULT_CONFIG = Path(__file__).parent / "config" / "models.yaml"      # 随安装包发布的默认配置
REPO_CONFIG = Path(__file__).parent.parent / "config"                   # 源码目录下的 config/（放 secrets.yaml）


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
    tool_mode: str = "native"         # native：OpenAI 兼容的工具调用；json：模型输出 JSON 动作（不支持工具调用时用）
    timeout: float = 300
    cache: str | None = None          # explicit：在消息上加 cache_control 标记（百炼显式缓存）；不写则不加
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
                   m.get("extra_body"), m.get("tool_mode", "native"), cache=m.get("cache"))

    def _usage(self, resp, t0: float):
        u = Usage(calls=1, latency_s=time.monotonic() - t0)
        if resp.usage:
            u.prompt_tokens, u.completion_tokens = resp.usage.prompt_tokens, resp.usage.completion_tokens
            details = getattr(resp.usage, "prompt_tokens_details", None)
            u.cached_tokens = (getattr(details, "cached_tokens", None) or 0) if details else 0
        self.usage += u

    def _prepare(self, messages: list[dict]) -> list[dict]:
        """去掉内部用的 _cache 标记；开启显式缓存时，把标记换成 cache_control（缓存到该消息为止的前缀）。"""
        out = []
        for m in messages:
            if "_cache" not in m:
                out.append(m)
                continue
            m = {k: v for k, v in m.items() if k != "_cache"}
            if self.cache == "explicit" and isinstance(m.get("content"), str) and m["content"]:
                m["content"] = [{"type": "text", "text": m["content"], "cache_control": {"type": "ephemeral"}}]
            out.append(m)
        return out

    def _client(self):
        from openai import OpenAI
        return OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=self.timeout)

    def chat(self, messages: list[dict], *, schema: dict | None = None, json_out: bool = False,
             temperature: float = 0.1, max_tokens: int = 8192) -> str:
        kwargs = dict(model=self.model, messages=self._prepare(messages), temperature=temperature, max_tokens=max_tokens)
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
        self._usage(resp, t0)
        return resp.choices[0].message.content or ""

    def chat_json(self, messages: list[dict], **kw):
        return parse_json(self.chat(messages, json_out=True, **kw))

    def chat_tools(self, messages: list[dict], tools: list[dict], *, temperature: float = 0.1,
                   max_tokens: int = 8192) -> dict:
        """工具调用。返回 {"content": 文本, "tool_calls": [{"id", "name", "arguments"(JSON 字符串)}]}。"""
        kwargs = dict(model=self.model, messages=self._prepare(messages), tools=tools, temperature=temperature,
                      max_tokens=max_tokens)
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body
        t0 = time.monotonic()
        try:
            resp = self._client().chat.completions.create(**kwargs)
        except Exception as e:
            raise LLMError(f"{self.name} 调用失败：{e}") from e
        self._usage(resp, t0)
        msg = resp.choices[0].message
        calls = [{"id": c.id, "name": c.function.name, "arguments": c.function.arguments or "{}"}
                 for c in (msg.tool_calls or [])]
        return {"content": msg.content or "", "tool_calls": calls}


class FakeLLM(LLM):
    """测试和演练用：按调用顺序返回预设回答，或由函数根据消息生成回答。"""

    def __init__(self, responder: Callable[[list[dict]], str] | list[str], name: str = "fake"):
        super().__init__(name, name, "", "")
        self._responder = responder
        self._i = 0
        self.calls: list[list[dict]] = []

    def _next(self, *args):
        if callable(self._responder):
            return self._responder(*args)
        out = self._responder[min(self._i, len(self._responder) - 1)]
        self._i += 1
        return out

    def chat(self, messages, **kw) -> str:
        self.calls.append(messages)
        self.usage += Usage(calls=1)
        return self._next(messages) if callable(self._responder) else self._next()

    def chat_tools(self, messages, tools, **kw) -> dict:
        """预设回答为 {"tool": 名称, "args": {...}}、它们的列表，或纯文本。"""
        self.calls.append(messages)
        self.usage += Usage(calls=1)
        out = self._next(messages, tools) if callable(self._responder) else self._next()
        if isinstance(out, str):
            return {"content": out, "tool_calls": []}
        items = out if isinstance(out, list) else [out]
        return {"content": "", "tool_calls": [
            {"id": f"call_{self.usage.calls}_{i}", "name": it["tool"],
             "arguments": json.dumps(it.get("args", {}), ensure_ascii=False)} for i, it in enumerate(items)]}
