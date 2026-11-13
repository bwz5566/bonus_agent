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


MAX_RETRIES = 6


def openai_client(base_url: str, api_key: str, timeout: float):
    from openai import DefaultHttpxClient, OpenAI
    http = DefaultHttpxClient(trust_env=False) if direct(base_url) else None
    # 网关偶发 502 / 503、连接中断时多重试几次（SDK 自带退避，约 0.5～8 秒一次）：几小时的回归不该因为一次抖动丢掉整次运行
    return OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, http_client=http, max_retries=MAX_RETRIES)


def list_remote(provider: str, cfg: dict) -> list[str]:
    """用当前 Key 向服务商查询可调用的模型 ID（/models 接口）。"""
    prov = cfg["providers"][provider]
    try:
        client = openai_client(prov["base_url"], provider_key(prov), 60)
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
        llm = cls(name, m["model"], prov["base_url"], provider_key(prov), m.get("json_mode", "json_object"),
                  m.get("extra_body"), m.get("tool_mode", "native"), cache=m.get("cache"),
                  tools_extra_body=m.get("tools_extra_body"), timeout=float(m.get("timeout", 300)))
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
        tm = m.get("think_model") if not _auxiliary else None
        if tm and tm != name:
            if tm not in cfg["models"]:
                raise LLMError(f"深度思考模型 {tm} 没有配置，可选：{', '.join(cfg['models'])}")
            llm.think_llm = cls.from_config(tm, cfg, _auxiliary=True, _path=route_path)
            llm.think_llm.usage = llm.usage            # 用量记在同一处
            llm.think_echo = llm.think_echo or llm.think_llm.think_echo
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
        if c and c != name:
            if c not in cfg["models"]:
                raise LLMError(f"交叉验证模型 {c} 没有配置，可选：{', '.join(cfg['models'])}")
            llm.checker_llm = cls.from_config(c, cfg, _auxiliary=True, _path=route_path)
            llm.checker_llm.usage = llm.usage          # 用量记在同一处
        return llm

    @property
    def image_llm(self) -> "LLM | None":
        """用来看图的模型：自己能看图就是自己，否则是配置的 vision 模型，都没有返回 None。"""
        return self if self.vision else self.vision_llm

    def _usage(self, resp, t0: float):
        u = Usage(calls=1, latency_s=time.monotonic() - t0)
        if resp.usage:
            u.prompt_tokens, u.completion_tokens = resp.usage.prompt_tokens, resp.usage.completion_tokens
            details = getattr(resp.usage, "prompt_tokens_details", None)
            u.cached_tokens = (getattr(details, "cached_tokens", None) or 0) if details else 0
        self.usage += u

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
        return openai_client(self.base_url, self.api_key, self.think_timeout if think else self.timeout)

    def _think(self, think: bool, body: dict, max_tokens: int) -> tuple[bool, dict, int]:
        """这一步是否真的思考、请求参数、输出上限。模型不能切换思考时照常调用。"""
        if not (think and self.can_think):
            return False, body, max_tokens
        return True, _merge(body, self.think_body), max(max_tokens, self.think_max_tokens)

    def context_window(self) -> int | None:
        """模型服务的上下文长度（vLLM 在 /models 里返回 max_model_len）；查不到返回 None，不影响调用。"""
        if not hasattr(self, "_ctx"):
            self._ctx = None
            try:
                for m in openai_client(self.base_url, self.api_key, 20).models.list():
                    if m.id == self.model:
                        n = getattr(m, "max_model_len", None) or (getattr(m, "model_extra", None) or {}).get("max_model_len")
                        self._ctx = int(n) if n else None
            except Exception:
                pass
        return self._ctx

    def chat(self, messages: list[dict], *, schema: dict | None = None, json_out: bool = False,
             temperature: float = 0.1, max_tokens: int = 8192, think: bool = False) -> str:
        """think：这一次打开思考（JSON 协议的智能体出错后用）。思考时不加 response_format：
        部分服务思考模式下不支持 JSON 输出，结果仍按容错方式解析。思考内容放在 last_reasoning。"""
        if think and self.think_llm is not None:
            out = self.think_llm.chat(messages, schema=schema, json_out=json_out, temperature=temperature,
                                      max_tokens=max_tokens, think=True)
            self.last_reasoning = self.think_llm.last_reasoning
            return out
        think, body, max_tokens = self._think(think, dict(self.extra_body or {}), max_tokens)
        kwargs = dict(model=self.model, messages=self._prepare(messages), temperature=temperature, max_tokens=max_tokens)
        if body:
            kwargs["extra_body"] = body
        if (json_out or schema) and not think:
            if schema and self.json_mode == "json_schema":
                kwargs["response_format"] = {"type": "json_schema", "json_schema": {"name": "output", "schema": schema}}
            else:
                kwargs["response_format"] = {"type": "json_object"}
        t0 = time.monotonic()
        try:
            resp = self._client(think).chat.completions.create(**kwargs)
        except Exception as e:  # 网络、鉴权、限流等
            raise LLMError(f"{self.name} 调用失败：{e}") from e
        self._usage(resp, t0)
        self.last_reasoning = _reasoning(resp.choices[0].message) if think else ""
        return resp.choices[0].message.content or ""

    def chat_json(self, messages: list[dict], **kw):
        return parse_json(self.chat(messages, json_out=True, **kw))

    def chat_tools(self, messages: list[dict], tools: list[dict], *, temperature: float = 0.1,
                   max_tokens: int = 8192, think: bool = False) -> dict:
        """工具调用。返回 {"content": 文本, "tool_calls": [{"id", "name", "arguments"(JSON 字符串)}], "reasoning": 思考内容}。
        think：这一步打开思考（模型不能切换时照常调用，reasoning 为空）；配了 think_model 时这一步交给那个模型。"""
        if think and self.think_llm is not None:
            return self.think_llm.chat_tools(messages, tools, temperature=temperature, max_tokens=max_tokens, think=True)
        think, body, max_tokens = self._think(think, _merge(self.extra_body, self.tools_extra_body), max_tokens)
        kwargs = dict(model=self.model, messages=self._prepare(messages), tools=tools, temperature=temperature,
                      max_tokens=max_tokens)
        if body:
            kwargs["extra_body"] = body
        t0 = time.monotonic()
        try:
            resp = self._client(think).chat.completions.create(**kwargs)
        except Exception as e:
            raise LLMError(f"{self.name} 调用失败：{e}") from e
        self._usage(resp, t0)
        msg = resp.choices[0].message
        calls = [{"id": c.id, "name": c.function.name, "arguments": c.function.arguments or "{}"}
                 for c in (msg.tool_calls or [])]
        return {"content": msg.content or "", "tool_calls": calls, "reasoning": _reasoning(msg) if think else ""}


class FakeLLM(LLM):
    """测试和演练用：按调用顺序返回预设回答，或由函数根据消息生成回答。"""

    def context_window(self) -> int | None:
        return getattr(self, "ctx", None)        # 测试里设 ctx 模拟模型服务的上下文长度

    def __init__(self, responder: Callable[[list[dict]], str] | list[str], name: str = "fake"):
        super().__init__(name, name, "", "")
        self._responder = responder
        self._i = 0
        self.calls: list[list[dict]] = []
        self.images: str | None = None      # 设置后，看图的调用一律返回它，不消耗预设回答
        self.reread: str | None = None      # 独立复读的回答（JSON 文本）；不设时复读结果为空
        self.calc: str | list[str] | None = None     # 独立算式的回答（几次改正时给列表，依次返回）；不设时为空
        self.source: str | None = None      # 提问前查原文的回答（JSON 文本）；不设时为“材料里没有”
        self.triage = None                  # 材料预筛的回答：JSON 文本，或按消息生成回答的函数；不设时一页都不折叠

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
