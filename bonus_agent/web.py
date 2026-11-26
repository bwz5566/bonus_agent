"""内网网页服务：财务在网页上看问题、填答复、传补充材料，提交后自动跑下一轮；确认结果、按模板计算、下载结果。

私有化部署在内网，默认只监听本机（127.0.0.1），对外请放在 HTTPS 反向代理后面。

数据目录：
  users.yaml                      用户（口令只存 PBKDF2 哈希）和角色
  secret.key                      表单防伪的密钥（首次启动自动生成）
  scenarios/<场景>/               本机场景包（scenario.yaml、guide.md 场景说明、输入模板、验收用例），
                                  以及发布的规则版本 releases/v1…、场景记忆 memory.yaml
  cases/<编号>/meta.json          案卷名称、创建人、场景和模式（编制 / 计算）
  cases/<编号>/materials/         本笔奖金的材料（规则文档、Excel 数据）
  cases/<编号>/expected/          期望结果（可选，只用来事后核对，不交给智能体）
  cases/<编号>/out/               案卷：case.json、questions.yaml、每轮结果和 history/、确认后的 template/
  cases/<编号>/runs/<时间>/        按模板计算的数据和结果
  cases/<编号>/audit.jsonl        操作记录：谁、什么时候、做了什么（含下载）

角色：editor 传材料、答复问题、运行下一轮、按模板计算；approver 确认结果、发布规则版本。所有登录用户都能查看。

场景的三种用法：编制（智能体起草规则，财务答复、确认后发布成场景的规则版本）、计算（选已发布的规则版本，换本期数据直接算，
不调用大模型）、对话（对结果提问）。没有场景的老案卷按通用流程照常使用。
"""

# 不写 from __future__ import annotations：FastAPI 要在运行时解析 create_app 里局部导入的 Request 等类型
import hashlib
import hmac
import html
import json
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote

import yaml

from bonus_agent import case as cs
from bonus_agent import review as rv
from bonus_agent import commission_revision as cr
from bonus_agent.failure import diagnosis_items, explain as explain_failure, retryable, to_text
from bonus_agent.parsing.docs import PARSERS

ROLES = {"editor": "传材料、答复问题、运行、按模板计算", "approver": "确认结果、发布规则版本"}
MODES = {"author": "编制", "compute": "计算"}
PH = "如 2026-09；季度 2026Q3；年 2026；起止 2026-07~2026-09"     # 核算期间的写法提示
COOKIE = "bonus_session"
SESSION_SECONDS = 8 * 3600       # 登录 8 小时后要重新登录
MAX_FAILURES, LOCK_SECONDS = 5, 300   # 同一用户名 5 分钟内输错 5 次，暂停登录
ROLE_NAMES = {"editor": "经办", "approver": "审批"}
DETAIL_NAMES = {"name": "名称", "file": "文件", "new": "从头开始", "round": "轮次", "status": "状态", "error": "错误",
                "question": "问题", "answer": "答复", "note": "说明", "reason": "原因", "run": "批次", "pools": "奖金池",
                "data": "数据", "sha256": "sha256", "size": "字节", "template": "模板",
                "scenario": "场景", "mode": "模式", "version": "规则版本", "removed": "删除条数", "period": "核算期间"}
MAX_UPLOAD = 50 * 1024 * 1024
CID = re.compile(r"^[0-9a-f]{12}$")
OTHER = "__other__"
QUESTIONS_HEAD = ("# 财务答复：在 answer 里填选项序号（从 0 开始）或直接写文字；有补充材料放进材料文件夹，\n"
                  "# 然后用同一个输出目录再运行一次，智能体会从这一轮接着做（不用 --answers）\n")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------- 用户 ----------------
def hash_password(pw: str, iterations: int = 240_000) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${h.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        algo, it, salt, h = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    got = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
    return hmac.compare_digest(got.hex(), h)


class Users:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        return (yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}) if self.path.exists() else {}

    def _save(self, users: dict) -> None:
        from bonus_agent.infra.atomic import write_text
        write_text(self.path, yaml.safe_dump(users, allow_unicode=True, sort_keys=True))
        self.path.chmod(0o600)

    def add(self, name: str, password: str, roles: list[str], display: str = "") -> None:
        bad = [r for r in roles if r not in ROLES]
        if bad:
            raise ValueError(f"没有角色 {bad}，可选：{list(ROLES)}")
        if not re.fullmatch(r"[\w.\-@]{1,64}", name):
            raise ValueError("用户名只能用字母（含中文）、数字、下划线、点、减号、@")
        if len(password) < 8:
            raise ValueError("口令至少 8 位")
        with cs.case_lock(self.path.parent / '.account-admin'):
            users = self.load()
            users[name] = {"password": hash_password(password), "roles": roles, "display": display or name}
            self._save(users)

    def single(self, name: str) -> Path | None:
        """Keep one existing login, preserving its password and audit identity.

        Server requests reload the active file, so removed accounts' sessions
        cease to work immediately. The backup is not an active account store.
        """
        with cs.case_lock(self.path.parent / '.account-admin'):
            users=self.load()
            if name not in users or not isinstance(users[name],dict) or not users[name].get('password'):
                raise ValueError(f'要保留的现有账号不存在：{name}')
            retained={**users[name], 'roles': list(ROLES)}
            target={name:retained}
            if users==target:return None
            backup=self.path.with_name(f'.users-before-single-{datetime.now().strftime("%Y%m%d-%H%M%S")}-{secrets.token_hex(3)}.yaml')
            shutil.copy2(self.path,backup);backup.chmod(0o600)
            self._save(target)
            if self.load()!=target:
                from bonus_agent.infra.atomic import write_text
                write_text(self.path,backup.read_text(encoding='utf8'))
                raise ValueError('单账号配置回读失败，已恢复原配置。')
            return backup

    def check(self, name: str, password: str) -> dict | None:
        u = self.load().get(name)
        if not u or not check_password(password, u.get("password", "")):
            return None
        return {"name": name, "display": u.get("display") or name, "roles": list(u.get("roles") or [])}


# ---------------- 案卷存储 ----------------
class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        (self.root / "cases").mkdir(parents=True, exist_ok=True)
        key = self.root / "secret.key"
        if not key.exists():
            key.write_text(secrets.token_hex(32), encoding="utf-8")
            key.chmod(0o600)
        self.secret = key.read_text(encoding="utf-8").strip().encode()
        self.users = Users(self.root / "users.yaml")

    def dir(self, cid: str) -> Path:
        d = self.root / "cases" / cid
        if not CID.match(cid) or not d.is_dir():
            raise KeyError(cid)
        return d

    def create(self, name: str, by: str, scenario: str | None = None, mode: str = "author", memory: bool = True,
               period: str | None = None) -> str:
        """scenario 为空：通用流程（不分场景）。mode：author 编制（智能体起草规则）/ compute 计算（按已发布的规则版本）。"""
        if mode not in MODES or (mode == "compute" and not scenario):
            raise ValueError("计算模式要选场景")
        cid = secrets.token_hex(6)
        d = self.root / "cases" / cid
        for sub in ("materials", "expected", "out", "runs"):
            (d / sub).mkdir(parents=True)
        meta = {"name": name, "created": now(), "created_by": by,
                **({"scenario": scenario, "mode": mode, "memory": memory} if scenario else {}),
                **({"period": period} if period else {})}
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        self.audit(cid, by, "新建案卷", name=name, **({"scenario": scenario, "mode": MODES[mode]} if scenario else {}))
        return cid

    def save_meta(self, cid: str, meta: dict) -> None:
        (self.dir(cid) / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    def meta(self, cid: str) -> dict:
        return json.loads((self.dir(cid) / "meta.json").read_text(encoding="utf-8"))

    def cases(self) -> list[tuple[str, dict, dict | None]]:
        out = []
        for d in sorted((self.root / "cases").iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if CID.match(d.name) and (d / "meta.json").exists():
                out.append((d.name, self.meta(d.name), cs.load_case(d / "out")))
        return out

    def audit(self, cid: str, user: str, action: str, **detail) -> None:
        with (self.root / "cases" / cid / "audit.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": now(), "user": user, "action": action, **detail}, ensure_ascii=False) + "\n")

    def audit_log(self, cid: str) -> list[dict]:
        p = self.dir(cid) / "audit.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x] if p.exists() else []

    def chat(self, cid: str) -> list[dict]:
        p = self.dir(cid) / "chat.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x] if p.exists() else []

    def chat_add(self, cid: str, role: str, user: str, text: str) -> None:
        with (self.root / "cases" / cid / "chat.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": now(), "role": role, "user": user, "text": text}, ensure_ascii=False) + "\n")

    def csrf(self, user: str) -> str:
        return hmac.new(self.secret, f"csrf:{user}".encode(), hashlib.sha256).hexdigest()[:32]


def _unfinished(out: Path) -> bool:
    """还没有完成过的案卷，或最近一轮失败 / 中断：财务发的文字应当作为说明接着处理，而不是只回答问题。"""
    case = cs.load_case(out)
    rounds = (case or {}).get("rounds") or []
    return not rounds or rounds[-1].get("status") in (None, "FAILED")


def safe_name(name: str) -> str:
    """上传文件名只取文件名部分，拒绝隐藏文件、临时文件和不支持的格式。"""
    n = Path(str(name).replace("\\", "/")).name.strip()
    if not n or n.startswith((".", "~$")) or n in ("..",):
        raise ValueError(f"文件名不合法：{name}")
    if Path(n).suffix.lower() not in PARSERS:
        raise ValueError(f"不支持的格式：{n}（支持 {' '.join(sorted(PARSERS))}）")
    return n


def inside(base: Path, rel: str) -> Path:
    p = (base / rel).resolve()
    if not p.is_relative_to(base.resolve()) or not p.is_file():
        raise KeyError(rel)
    return p


# ---------------- 后台运行 ----------------
@dataclass
class Job:
    cid: str
    by: str
    started: str = field(default_factory=now)
    status: str = "running"         # running / done / failed
    result: str = ""                # 本轮状态：OK / NEEDS_CONFIRMATION / FAILED
    message: str = ""
    steps: list[str] = field(default_factory=list)
    thread: threading.Thread | None = None
    cancel: bool = False            # 页面上点了「停止」：下一步开始前停下


class Runner:
    """每个案卷同时只跑一轮；全局最多同时跑 max_parallel 轮（大模型调用量大）。"""

    def __init__(self, store: Store, llm_factory: Callable[[], object], max_parallel: int = 2, max_steps: int = 150,
                 max_tokens: int = 4_000_000, auto_continue: int = 2, registry=None, *,
                 unlimited_case_tokens: Iterable[str] = ()):
        from bonus_agent.scenarios import Registry
        self.store, self.llm_factory = store, llm_factory
        self.registry = registry or Registry([store.root / "scenarios"])
        self.auto_continue = auto_continue      # 步数用完时的接续上限；卡住不接续，tokens/时间按整个作业累计
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.slots = threading.Semaphore(max_parallel)
        self.max_steps, self.max_tokens = max_steps, max_tokens
        self.unlimited_case_tokens = frozenset(unlimited_case_tokens)
        if any(not CID.fullmatch(cid) for cid in self.unlimited_case_tokens):
            raise ValueError("--unlimited-case-tokens 须填写 12 位案卷编号")

    def token_limit(self, cid: str) -> int | None:
        return None if cid in self.unlimited_case_tokens else self.max_tokens

    def running(self, cid: str) -> bool:
        j = self.jobs.get(cid)
        return j is not None and j.status == "running"

    def start(self, cid: str, user: str, new: bool = False, message: str | None = None,
              instruction: str | None = None) -> Job:
        """message：对话（提问或修改）；instruction：随上传材料附的说明，按正常一轮处理。"""
        if self.store.meta(cid).get("mode") == "compute":
            raise RuntimeError("计算模式的案卷按已发布的规则版本计算，不运行智能体")
        with self.lock:
            if self.running(cid):
                raise RuntimeError("这个案卷正在运行，请等本轮结束")
            job = self.jobs[cid] = Job(cid, user)
        if message:
            self.store.chat_add(cid, "user", user, message)
            self.store.audit(cid, user, "对话", text=message[:200])
        else:
            self.store.audit(cid, user, "运行下一轮", new=new)
        job.thread = threading.Thread(target=self._run, args=(job, new, message, instruction), daemon=True)
        job.thread.start()
        return job

    @staticmethod
    def _step(job: Job, out: Path, n: int, s) -> None:
        cost = (f" | {getattr(s, 'at', '')} 模型 {getattr(s, 'model_secs', 0):.0f}s 工具 {getattr(s, 'secs', 0):.1f}s "
                f"tokens {getattr(s, 'tokens_in', 0)}/{getattr(s, 'tokens_out', 0)}" + (" 深度思考" if getattr(s, "deep", False) else "")
                if getattr(s, "at", "") else "")
        line = f"[{n:02d}] {s.tool} {'✓' if s.ok else '✗'}{cost} {(s.result.splitlines() or [''])[0][:160]}"
        job.steps.append(line)
        try:     # 逐步落盘：强制关掉服务时也留下过程，便于排查
            with open(out / "running_steps.txt", "a" if n > 1 else "w", encoding="utf-8") as f:
                f.write(f"{line}\n    思考：{(getattr(s, 'thought', '') or '（没写）')[:600]}\n"
                        f"    行动：{s.tool} {json.dumps(s.args, ensure_ascii=False)[:600]}\n    观察：{s.result[:600]}\n")
        except OSError:
            pass

    def _run(self, job: Job, new: bool, message: str | None = None, instruction: str | None = None) -> None:
        from bonus_agent.agent import run_agent
        from bonus_agent.recovery import RunBudget

        d = self.store.dir(job.cid)
        try:
            meta = self.store.meta(job.cid)
            scenario = None
            if meta.get("scenario"):          # 编制模式：带上场景说明、输入模板检查和场景记忆
                from bonus_agent.scenarios.context import prepare
                scenario = prepare(self.registry, meta["scenario"], [d / "materials"], use_memory=meta.get("memory", True),
                                   period=meta.get("period"))
            budget_dir = d / ".model_budget"
            with self.slots, cs.case_lock(budget_dir):
                llm = self.llm_factory()
                max_tokens = self.token_limit(job.cid)
                budget = RunBudget.for_case(budget_dir / "usage.json", d / "out",
                                            llm.usage.prompt_tokens + llm.usage.completion_tokens, max_tokens)
                if error := budget.check(llm):
                    raise RuntimeError(error)
                ar = run_agent(llm, [d / "materials"], d / "out", resume=not new, max_steps=self.max_steps,
                               max_tokens=max_tokens, message=message, by=job.by,
                               chat=self.store.chat(job.cid)[:-1] if message else self.store.chat(job.cid),
                               on_step=lambda n, s: self._step(job, d / "out", n, s), should_stop=lambda: job.cancel,
                               instruction=instruction, scenario=scenario, period=meta.get("period"), run_budget=budget)
                auto = 0
                # 仅步数用完允许续跑；重复失败和无进展是终止条件，不能在外层清零重试。
                while not message and not instruction and auto < self.auto_continue and retryable(ar.error) and not job.cancel:
                    auto += 1
                    self.store.audit(job.cid, job.by, "本轮结束", round=ar.round, status=ar.status, error=ar.error)
                    self.store.chat_add(job.cid, "agent", "智能体",
                                        f"第 {ar.round} 轮没做完（{to_text(ar.error).splitlines()[0]}），"
                                        f"程序自动接着做第 {ar.round + 1} 轮（自动接续第 {auto} 次，最多 {self.auto_continue} 次）。")
                    self.store.audit(job.cid, job.by, "自动接续", round=ar.round + 1)
                    ar = run_agent(llm, [d / "materials"], d / "out", resume=True, max_steps=self.max_steps,
                                   max_tokens=max_tokens, by=job.by, chat=self.store.chat(job.cid),
                                   on_step=lambda n, s: self._step(job, d / "out", n, s), should_stop=lambda: job.cancel,
                                   scenario=scenario, period=meta.get("period"), run_budget=budget)
                budget.persist()
                expected = sorted((d / "expected").glob("*.xlsx"))
                if expected and ar.results:
                    from bonus_agent.compare import compare_expected, report
                    lines = report(compare_expected(ar.results, expected[0]))
                    text = "\n".join(lines) + "\n"
                    (d / "out" / "expected_check.txt").write_text(text, encoding="utf-8")
                    hist = d / "out" / "history" / f"第{ar.round}轮"     # 每轮的核对结果随该轮存档
                    if ar.outputs and hist.is_dir():
                        (hist / "expected_check.txt").write_text(text, encoding="utf-8")
            job.status, job.result = "done", ar.status
            job.message = {"OK": "已出结果，请核对", "NEEDS_CONFIRMATION": f"有 {len(ar.pending)} 个问题等答复，本轮不出结果",
                           "AWAITING_TOTAL_CONFIRMATION":"总额已独立复核，等待财务确认后继续人员分配",
                           "AWAITING_REVIEW":"独立复核未完成，已保留工作区；恢复复核后才能继续"
                           }.get(ar.status, f"未完成：{ar.error}")
            if message:
                made = bool(ar.outputs)          # 对话里改了东西才会生成新的一轮
                note = (f"\n\n（已按修改生成第 {ar.round} 轮" + ("" if ar.error else f"：{job.message}") + "）" if made else "")
                self.store.chat_add(job.cid, "agent", "智能体", (ar.reply or (f"（没有完成）{to_text(ar.error, diagnosis=ar.failure_context)}" if ar.error else f"（已处理：{ar.ws.summary or job.message}）")) + note)
                if not made:
                    job.result, job.message = "", "已回复"
            else:                                # 每轮的结果都在对话里交代：没完成先说原因，再说需要什么帮助
                self.store.chat_add(job.cid, "agent", "智能体", round_text(ar))
            if ar.outputs or not message:
                self.store.audit(job.cid, job.by, "本轮结束", round=ar.round, status=ar.status, error=ar.error)
        except Exception as e:  # 失败要如实显示，不能当作完成
            job.status, job.message = "failed", f"运行失败：{e}"
            self.store.chat_add(job.cid, "agent", "智能体", f"这一轮（{job.started[11:16]} 开始）中断了。\n{to_text(f'{type(e).__name__}: {e}')}")
            self.store.audit(job.cid, job.by, "本轮失败", error=str(e))


OUT_GROUPS = ("给财务核对", "留档：计算口径（确认后作为模板，每期复用）", "排查用（开发看）")


def output_kind(name: str) -> tuple[str, str]:
    """输出文件属于哪一类、有什么用。"""
    if name == "总额预核算.xlsx":
        return OUT_GROUPS[0], "预核算总额、逐对象金额和公式来源"
    if name == '修订差异.xlsx':
        return OUT_GROUPS[0], '新旧总额、逐项目指标、人员、规则及财务反馈处理结论'
    if name == 'commission_revisions.json':
        return OUT_GROUPS[1], '财务反馈历史与规则、人员修订记录'
    if name == "result.xlsx":
        return OUT_GROUPS[0], "奖金结果（有多个方案时是总览）"
    if name.startswith("result_") and name.endswith(".xlsx"):
        return OUT_GROUPS[0], f"方案 {name[7:-5]} 的逐人结果和计算过程"
    if name == "口径单.json":
        return OUT_GROUPS[0], "口径单：规则逐条翻译成大白话，在本页「口径单」里逐条确认"
    if name == "口径确认.json":
        return OUT_GROUPS[1], "口径单的确认记录（谁、何时、确认或有误）"
    if name.startswith("待确认问题"):
        return OUT_GROUPS[0], "需要你答复的问题"
    if name.startswith("rules"):
        return OUT_GROUPS[1], "计算规则（参数、公式、分配层级）"
    if name == "mapping.yaml":
        return OUT_GROUPS[1], "数据表和 Excel 列的对应关系"
    if name == "questions.yaml":
        return OUT_GROUPS[1], "问题和你的答复记录"
    if name.startswith("adjust"):
        return OUT_GROUPS[1], "人工调整记录"
    return OUT_GROUPS[2], {"agent_log.json": "智能体每一步的完整记录", "running_steps.txt": "运行过程（逐步落盘）",
                           "results.json": "全部金额明细（应得、暂缓、抵扣、超发、实发），可用程序逐分核对",
                           "expected_check.txt": "和期望结果的核对"}.get(name, "")


def round_text(ar) -> str:
    """每轮结束时在对话里的交代。财务的说明里带了提问时，智能体的回答放在最前面（没算完也显示）。"""
    from bonus_agent.agent import ANSWER_NOTE
    answer = (getattr(getattr(ar, "ws", None), "notes", None) or {}).get(ANSWER_NOTE, "")
    text = _round_status(ar) + usage_text(getattr(ar, "usage", None))
    return f"{answer}\n\n{text}" if answer else text


def usage_text(u: dict | None) -> str:
    """一轮的用时和用量，附在每轮的交代后面。"""
    if not u:
        return ""
    budget = u.get("budget") or {}
    case_usage = ""
    if budget.get("scope") == "case":
        case_usage = f"\n案卷累计已报告用量 {budget['actual_used_tokens']:,} tokens。"
        if budget["unconfirmed_reserved_tokens"]:
            case_usage += f"另有 {budget['unconfirmed_reserved_tokens']:,} tokens 用量待确认。"
        if budget["remaining_tokens"] is not None:
            case_usage += f"剩余额度 {budget['remaining_tokens']:,} tokens。"
    return (f"\n（本轮用时 {u.get('secs', 0) // 60} 分 {u.get('secs', 0) % 60} 秒，{u.get('steps', 0)} 步"
            + (f"，其中深度思考 {u['deep_steps']} 步" if u.get("deep_steps") else "")
            + f"；调用模型 {u.get('calls', 0)} 次，输入 {u.get('prompt_tokens', 0):,} tokens"
            + (f"（其中命中缓存 {u['cached_tokens']:,}）" if u.get("cached_tokens") else "")
            + f"，输出 {u.get('completion_tokens', 0):,} tokens）" + case_usage)


def _round_status(ar) -> str:
    if ar.error:
        return f"第 {ar.round} 轮没做完。{to_text(ar.error, diagnosis=getattr(ar, 'failure_context', None))}"
    if ar.status == "NEEDS_CONFIRMATION":
        qs = [q for q in ar.pending][:8]
        def one(q):    # ar.pending 是 dict（model_dump）；原来按属性取，对话里显示成整段 dict
            get = q.get if isinstance(q, dict) else (lambda k, d=None: getattr(q, k, d))
            opts = get("options") or []
            return (f"- {get('id', '')} {get('text', q)}"
                    + ''.join(f'\n  {label} {value}' for label, value in diagnosis_items(get('diagnosis')))
                    + "".join(f"\n  {chr(65 + i)}. {o}" for i, o in enumerate(opts[:6])))
        return (f"第 {ar.round} 轮暂停：有 {len(ar.pending)} 个问题需要你回答，回答前不出结果。\n"
                + "\n".join(one(q) for q in qs)
                + "\n怎么回答：在下方「问题」里选一项（选“其他”时写明怎么处理），点「保存答复」再点「运行下一轮」；"
                  "也可以直接在对话框回复，如“Q3 选 A”或“Q3：同一简称下的编码合并计算”。回答后从停下的地方接着做。")
    if ar.status == 'AWAITING_TOTAL_CONFIRMATION':
        return f'第 {ar.round} 轮已完成基础金额和总额独立复核。请核对「总额预核算」并确认，之后继续人员分配；尚未生成正式人员结果。'
    if ar.status == 'AWAITING_REVIEW':
        reason=getattr(ar.ws,'workflow',{}).get('review_error',{}).get('error','')
        return f'第 {ar.round} 轮必要独立复核未完成，已保留工作区，尚未出正式金额。\n{reason}'
    return f"第 {ar.round} 轮已出结果，请核对。" + (f"\n{ar.ws.summary}" if getattr(ar.ws, "summary", "") else "")


# ---------------- 问题答复 ----------------
def load_questions(out: Path) -> list[dict]:
    p = out / "questions.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or []) if p.exists() else []


def render_diagnosis(details) -> str:
    items = diagnosis_items(details)
    return ('<dl>' + ''.join(f'<dt><b>{e(label)}</b></dt><dd>{e(value)}</dd>' for label, value in items) + '</dl>') if items else ''


def _answer_updates(qs, form, user):
    done = []
    for q in qs:
        if q.get("withdrawn"):
            continue
        pick = (form.get(f"ans_{q['id']}") or "").strip()
        text = (form.get(f"text_{q['id']}") or "").strip()
        if pick == OTHER or (not pick and text):
            ans = text
        elif pick.isdigit() and int(pick) < len(q.get("options") or []):
            ans = q["options"][int(pick)] + (f"；补充说明：{text}" if text else "")
        else:
            continue
        if not ans or ans == q.get("answer"):
            continue
        q.update(answer=ans, answered_by=user, answered_at=now())
        done.append(q["id"])
    return done


def save_answers(out: Path, form: dict[str, str], user: str) -> list[str]:
    """把网页表单的答复写进 questions.yaml（记录答复人和时间）。返回本次答复的问题编号。"""
    qs = load_questions(out)
    done = _answer_updates(qs,form,user)
    if done:
        (out / "questions.yaml").write_text(QUESTIONS_HEAD + yaml.safe_dump(qs, allow_unicode=True, sort_keys=False),
                                            encoding="utf-8")
    return done


def norm_period(text) -> str | None:
    """表单里的核算期间 -> 规范写法（如 2026-09、2026Q3）；空返回 None，看不懂抛 ValueError。"""
    from bonus_core.data.period import Period
    p = Period.parse(str(text or ""))
    return p.label if p else None


# ---------------- 页面 ----------------
CSS = """
:root{--bg:#f6f7f9;--fg:#1d2330;--mut:#667085;--card:#fff;--line:#dfe3ea;--acc:#2554c7;--ok:#137a3e;--warn:#a15c00;--bad:#b42318}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--fg:#e6e9ef;--mut:#98a2b3;--card:#1c2027;--line:#2c323c;--acc:#7aa2ff;--ok:#5cc98a;--warn:#f0b35a;--bad:#ff8a80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 system-ui,"PingFang SC","Microsoft YaHei",sans-serif}
header{padding:12px 16px;border-bottom:1px solid var(--line);background:var(--card);display:flex;justify-content:space-between;gap:12px}
header a{color:var(--fg);text-decoration:none;font-weight:600}main{max-width:1080px;margin:0 auto;padding:16px}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:0 0 14px}
h1{font-size:20px;margin:4px 0 12px}h2{font-size:16px;margin:0 0 10px}a{color:var(--acc)}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{color:var(--mut);font-weight:500}td:not(:last-child),th{white-space:nowrap}.mut{color:var(--mut)}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
button{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:6px 14px;cursor:pointer;font:inherit}
button.link{background:none;color:var(--bad);padding:0}header button.link{color:var(--acc)}input[type=text],input[type=password],textarea{width:100%;padding:6px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
.fail{border:1px solid var(--bad);border-radius:8px;padding:8px 12px;margin:8px 0} .q{border-top:1px solid var(--line);padding:10px 0}.q:first-of-type{border-top:0}.opt{display:block;margin:2px 0}
pre{white-space:pre-wrap;background:var(--bg);padding:8px;border-radius:6px;max-height:320px;overflow:auto;margin:0}
.wrap{overflow-x:auto}.chat{max-height:480px;overflow-y:auto;margin-bottom:10px}
.msg{margin:8px 0;display:flex;flex-direction:column}.msg.user{align-items:flex-end}
.bubble{white-space:pre-wrap;padding:8px 12px;border-radius:10px;max-width:85%;background:var(--bg);border:1px solid var(--line)}
.msg.user .bubble{background:var(--acc);color:#fff;border-color:var(--acc)}
"""
STATUS = {"OK": ("已出结果，待财务核对", "ok"), "NEEDS_CONFIRMATION": ("等财务答复，本轮不出结果", "warn"),
          "AWAITING_TOTAL_CONFIRMATION": ("总额已独立复核，等待财务确认后继续人员分配", "warn"),
          "REVISION_PENDING": ("已登记财务异议，旧版交付停止，等待修订", "warn"),
          "APPROVED": ("本版本总额已确认，等待人员核验", "warn"),
          "FAILED": ("未完成", "bad")}


def e(v) -> str:
    return html.escape("" if v is None else str(v))


def detail(a: dict) -> str:
    def v(x):
        return json.dumps(x, ensure_ascii=False) if isinstance(x, (dict, list)) else "是" if x is True else "否" if x is False else str(x)
    items = [(k, x) for k, x in a.items() if k not in ("at", "user", "action", "sha256", "size") and x not in ("", None)]
    return "；".join(f"{DETAIL_NAMES.get(k, k)}：{v(x)}" for k, x in items)[:240]


def page(title: str, body: str, user: dict | None, refresh: bool = False) -> str:
    who = "" if user is None else (
        f"<span class='mut'>{e(user['display'])}（{e('、'.join(ROLE_NAMES.get(r, r) for r in user['roles']) or '只读')}）"
        f" {form('/logout', '<button class=link>退出</button>', user['csrf'], inline=True)}</span>")
    return ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            + ("<meta http-equiv='refresh' content='3'>" if refresh else "")
            + f"<title>{e(title)}</title><style>{CSS}</style></head><body>"
            f"<header><a href='/'>奖金计算</a>{who}</header>"
            f"<main>{body}</main></body></html>")


def form(action: str, inner: str, csrf: str, upload: bool = False, inline: bool = False) -> str:
    enc = " enctype='multipart/form-data'" if upload else ""
    enc += " style='display:inline'" if inline else ""
    return f"<form method='post' action='{e(action)}'{enc}><input type='hidden' name='csrf' value='{csrf}'>{inner}</form>"


def status_text(case: dict | None) -> str:
    if not case or not case.get("rounds"):
        return "<span class='mut'>还没有运行</span>"
    r = case["rounds"][-1]
    status = (cr.state_of(case) or {}).get('status') or r.get('status')
    label, cls = STATUS.get(status, (status, "mut"))
    conf = " · <span class='ok'>已确认</span>" if r.get("confirmed") and status=='OK' else ""
    return f"第 {r['n']} 轮：<span class='{cls}'>{e(label)}</span>{conf}"


def pending_basis_confirmations(case: dict, out=None):
    from bonus_agent.recovery import fingerprint

    flow = case.get('workspace', {}).get('workflow', {})
    if fixed := flow.get('fixed_commission'):
        from bonus_agent.commission_chain import SCHEME
        if out:
            try:cr.validate_current(out,case)
            except cs.ConfirmError:return
        basis=fixed.get('basis_view');digest=fixed.get('basis_digest')
        if (fixed.get('status')=='AWAITING_TOTAL_CONFIRMATION' and basis and digest
                and fixed.get('receipts',{}).get('basis_review',{}).get('status')=='PASS'
                and not cr.approval_matches(fixed) and fingerprint(basis)==digest):
            yield SCHEME,basis,cr.receipt_token(fixed)
        return
    if not flow.get('enabled'):
        return
    latest = (case.get('rounds') or [{}])[-1]
    view_version = flow.get('view_basis_version', flow.get('view_version'))
    saved_wait = flow.get('waiting') if latest.get('status') == 'AWAITING_TOTAL_CONFIRMATION' else None
    receipts = flow.get('receipts', {})
    for name, basis in flow.get('basis', {}).items():
        review = receipts.get('review:' + name, {})
        version = review.get('version')
        if not version or (version != view_version and name != saved_wait):
            continue
        current = receipts.get('basis:' + name, {})
        payload = review.get('payload', {})
        digest = fingerprint(basis)
        if version != view_version and 'view_basis_version' in flow:
            # Funding receipts use a scheme-scoped version. The independent opinion also binds
            # the global view and exact amounts, including for snapshots saved before this UI change.
            reference = flow.get('references', {}).get(name, {})
            opinion = reference.get('report', {})
            if (reference.get('version') != view_version or reference.get('spec_state') != 'valid'
                    or opinion.get('status') != 'PASS' or opinion.get('basis_digest') != digest):
                continue
        if (current.get('version') != version or current.get('payload', {}).get('digest') != digest
                or payload.get('status') != 'PASS' or payload.get('basis_digest') != digest):
            continue
        approved = receipts.get('approved:' + name, {})
        if approved.get('version') == version and approved.get('payload', {}).get('basis_digest') == digest:
            continue
        yield name, basis, digest


def basis_confirmation_section(cid, out, case, csrf, approver, busy, current_issue=''):
    """Always make the next financial action and the reason for blocking visible."""
    blocks=[]
    for name,basis,digest in pending_basis_confirmations(case or {},out):
        amounts='<br>'.join(f'{e(k)}：{e(v)} 元' for k,v in {'奖金池':basis['pool'],**basis['totals']}.items())
        block=f'<p><b>{e(name)}</b>：{amounts}<br>核算对象 {len(basis["objects"])} 个。请同时核对逐对象底稿和金额组成。</p>'
        if approver and not busy:
            block+=form(f'/cases/{cid}/confirm-totals',f'<input type="hidden" name="scheme" value="{e(name)}">'
                        f'<input type="hidden" name="digest" value="{e(digest)}">'
                        '<button>确认本版本总额并继续人员分配</button>',csrf)
        else:
            reason='本轮仍在保存，结束后即可确认。' if busy else '当前登录账号没有审批权限，请使用具有审批权限的账号确认。经办账号无需重复运行下一轮。'
            block+='<p><button type="button" disabled>确认本版本总额并继续人员分配</button></p>'+f'<p class="warn">{e(reason)}</p>'
        blocks.append(block)
    fixed=cr.state_of(case) or {}
    if not blocks and fixed.get('status')=='AWAITING_TOTAL_CONFIRMATION':
        reason=current_issue or '本版项目复核或金额凭据尚未达到确认条件，请查看当前状态和节点回执。'
        blocks.append('<p class="warn">'+e(reason)+'</p><p><button type="button" disabled>确认本版本总额并继续人员分配</button></p>')
    if not blocks:return ''
    return '<section id="basis-confirmation"><h2>总额预核算确认：下一步操作</h2>'+''.join(blocks)+'</section>'


def files_table(base: Path, href: str, csrf: str | None = None, delete: str | None = None) -> str:
    fs = sorted(p for p in base.rglob("*") if p.is_file()) if base.exists() else []
    if not fs:
        return "<p class='mut'>无</p>"
    rows = []
    for p in fs:
        rel = p.relative_to(base).as_posix()
        act = form(delete, f"<input type='hidden' name='name' value='{e(rel)}'><button class='link'>删除</button>", csrf) \
            if delete else ""
        rows.append(f"<tr><td><a href='{e(href)}/{e(rel)}'>{e(rel)}</a></td><td class='mut'>{p.stat().st_size:,} 字节</td>"
                    f"<td class='mut'>{datetime.fromtimestamp(p.stat().st_mtime).strftime('%Y-%m-%d %H:%M')}</td><td>{act}</td></tr>")
    return "<div class='wrap'><table>" + "".join(rows) + "</table></div>"


def commission_feedback_section(cid, out, case, csrf, editor, approver, busy):
    state=cr.state_of(case)
    if not state:return ''
    ledger=cr.load(out);rev=cr.active(ledger)
    body='<p>任何异议先登记成新版本，再按责任节点复核。历史审批和旧底稿保留；不会按期望金额反推规则。</p>'
    if rev:
        body+=f"<p><b>当前修订 {e(rev['id'])}</b> · {e(STATUS.get(rev['status'],(rev['status'],''))[0])}；原对照第 {e(rev['base_round'])} 轮；"+('项目总额需要重新确认。' if rev['basis_changed'] else '仅人员/报表范围，项目确认必须完全一致才能沿用。')+'</p>'
        for r in rev['requests']:
            body+=f"<p><b>{e(cr.KINDS.get(r['kind'],r['kind']))}</b> · {e(r['created_by'])}：{e(r['description'])}<br><span class='mut'>责任环节：{e(cr.OWNERS.get(r['owner'],r['owner']))}</span></p>"
        body+=f"<p><a href='/cases/{cid}/dl/out/revisions/{e(rev['baseline'])}/baseline.json'>修订前核算及审批快照</a></p>"
        if (out/'revision_diff.json').is_file():
            try:
                diff=json.loads((out/'revision_diff.json').read_text(encoding='utf8'))
                if diff.get('revision')==rev['id']:
                    body+=f"<p><a href='/cases/{cid}/dl/out/修订差异.xlsx'>下载本版修订差异</a> · 项目变化 {e(diff['projects']['changed'])} 个；逐条结论请查看报告。</p>"
            except (ValueError,OSError):pass
    if ledger.get('revisions') or ledger.get('scheduled'):
        records=[f"<li>{e(x['id'])} · {e(x['created_at'])} · {e(x['status'])} · 第 {e(x['base_round'])} 轮</li>" for x in reversed(ledger['revisions'])]
        records.extend(f"<li>已登记适用版本 {e(x['rule']['label'])}：{e(x['rule']['value'])}（{e(x['rule']['from'])} 起；不影响未适用期间）</li>" for x in ledger.get('scheduled',[]))
        body+='<details><summary>历次财务反馈及已登记规则</summary><ul>'+''.join(records)+'</ul></details>'
    if busy or not (editor or approver):return '<section><h2>财务反馈与版本修订</h2>'+body+'</section>'
    version=f"<input type='hidden' name='base_version' value='{e(cr.edit_token(case,ledger))}'>"
    action=f'/cases/{cid}/commission-feedback'
    buttons="<p><button name='run' value='1'>登记并开始复核</button> <button name='run' value='0'>仅登记，稍后运行</button></p>"
    options=''.join(f"<option value='{e(k)}'>{e(v)}</option>" for k,v in cr.OWNERS.items() if k!='settlement')
    dissent=version+"<input type='hidden' name='kind' value='discrepancy'>"+"<p>范围：<select name='stage'><option value='unknown'>自动定位（总额/项目）</option><option value='basis'>项目/总额</option><option value='settlement'>人员分配</option><option value='output'>报表回读</option></select></p>"+f"<p>已知环节（可不选）：<select name='owner'><option value=''>自动定位</option>{options}</select></p>"+"<p>项目四键（可不填，多个换行）：<textarea name='project_keys' rows='2'></textarea></p>"+"<textarea name='description' rows='3' required placeholder='具体金额/指标有误或疑义；说明实际看到什么、期望核对什么。'></textarea>"+"<input type='text' name='observed' placeholder='现值或反例（可选）'><input type='text' name='evidence' placeholder='材料文件、行列、条款依据（可选）'>"+buttons
    body+='<details open><summary><b>总额有误 / 撤回确认 / 人员金额有误</b></summary>'+form(action,dissent,csrf)+'</details>'
    if approver:
        available=[]
        for key,(node,path,_,label,typ) in cr.FIELDS.items():
            try:old=cr._get(state['specs'][node],path)
            except KeyError:continue
            choices='；可选 '+ '/'.join(f'{v}（{cr.VALUE_LABELS.get(v,v)}）' for v in typ) if isinstance(typ,tuple) else ''
            available.append(f"<option value='{e(key)}'>{e(label)}（当前 {e(old)}{e(choices)}）</option>")
        if available:
            fields=version+"<input type='hidden' name='kind' value='rule_change'>"+f"<p>被替代的规则：<select name='field'>{''.join(available)}</select></p>"+"<p>明确新值：<input name='value' type='text' required placeholder='例如 24、multiplicative、after_sum'></p>"+"<p>适用范围：<select name='scope'><option value='this_case'>仅本案</option><option value='this_period'>指定月份的场景</option><option value='future_periods'>从指定月份开始</option><option value='scenario'>场景指定有效区间</option></select></p>"+"<p>生效月份（跨案必填）：<input name='effective_from' type='month'> 终止月份（可选）：<input name='effective_to' type='month'></p>"+"<textarea name='description' rows='3' required placeholder='新规则、替代原因及适用范围；此操作是正式财务确认，不是期望答案。'></textarea>"+"<input name='evidence' type='text' placeholder='制度文件/会议/财务确认依据'>"+buttons
            body+='<details><summary><b>调整业务规则及登记适用版本</b></summary>'+form(action,fields,csrf)+'</details>'
        projects=(state.get('basis_view') or {}).get('projects',{})
        if projects:
            choices=''.join(f"<option value='{e(p)}'>{e(p)} · {e(v['alloc'])} 元</option>" for p,v in projects.items())
            roles=''.join(f"<option>{e(r)}</option>" for r in cr.ROLES)
            recipients=version+"<input type='hidden' name='kind' value='recipient_change'>"+f"<p>项目：<select name='project_keys'>{choices}</select></p><p>角色：<select name='role'>{roles}</select></p>"+"<input type='text' name='shares' required placeholder='例如 张三70%，李四30%'><textarea name='description' rows='2' required placeholder='本项目本角色实际名单变更原因；未指定的剩余份额保留待分配。'></textarea>"+buttons
            body+='<details><summary><b>仅修订指定项目角色的人员份额</b></summary>'+form(action,recipients,csrf)+'</details>'
    body+='<p class="mut">更正材料使用原上传入口，程序自动登记失效范围。新公式超出当前可复用算子时，不会擅自猜测，请提供正式材料后核对扩展需求。</p>'
    return '<section><h2>财务反馈与版本修订</h2>'+body+'</section>'


def review_section(cid: str, out: Path, csrf: str, can_mark: bool) -> str:
    """口径单：按方案、类别分组，每行可确认或标有误；可疑的行（程序检查标出的）不随整节确认。"""
    rows, marks = rv.load(out), rv.load_marks(out)
    st = rv.status(out)
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        groups.setdefault((r["scheme"], r["section"]), []).append(r)
    blocks = []
    for (scheme, section), rs in groups.items():
        items, todo = [], 0
        for r in rs:
            mk = marks.get(r["key"], {})
            state = mk.get("status")
            todo += state != rv.OK
            badge = (f"<span class='ok'>已确认（{e(mk.get('by'))}）</span>" if state == rv.OK else
                     f"<span class='bad'>有误：{e(mk.get('note'))}（{e(mk.get('by'))}）"
                     + ("，已交给智能体" if mk.get("sent") else "") + "</span>" if state == rv.WRONG else
                     "<span class='warn'>待确认</span>")
            ctl = (f"<div><label class='opt'><input type='radio' name='st_{r['key']}' value='{rv.OK}'> 确认</label>"
                   f"<label class='opt'><input type='radio' name='st_{r['key']}' value='{rv.WRONG}'> 有误</label>"
                   f"<input type='text' name='note_{r['key']}' placeholder='有误时写明哪里不对'></div>") if can_mark else ""
            items.append(f"<div class='q'><b>{e(r['id'])}</b> {e(r['item'])} {badge}"
                         + (f"<div class='bad'>需要重点看：{e(r['flag'])}</div>" if r["flag"] else "")
                         + f"<pre>{e(r['reading'])}</pre>"
                         + (f"<div class='mut'>代入示例：{e(r['example'])}</div>" if r["example"] else "")
                         + (f"<details><summary class='mut'>材料原文</summary><pre>{e(r['source'])}</pre></details>"
                            if r["source"] else "<div class='mut'>没有对应的材料原文，请核对依据</div>")
                         + ctl + "</div>")
        bulk = (f"<p><button name='bulk' value='{e(scheme)}|{e(section)}'>本节其余全部确认</button> "
                "<span class='mut'>标了“需要重点看”的行不会随整节确认</span></p>") if can_mark and todo else ""
        blocks.append(f"<details{' open' if todo else ''}><summary>{e(scheme)} · {e(section)}（{len(rs)} 行，"
                      f"{'待确认 ' + str(todo) + ' 行' if todo else '已全部确认'}）</summary>{''.join(items)}{bulk}</details>")
    head = (f"<p>规则逐条翻译成的大白话（程序按规则机械翻译，和实际计算一致）。已确认 {st['confirmed']} / {st['total']} 行"
            + (f"，<span class='bad'>{len(st['wrong'])} 行有误</span>" if st["wrong"] else "") + "。"
            "看“程序的理解”是否符合制度；有误的写明哪里不对，点“运行下一轮”交给智能体改正。规则改动后，内容变了的行要重新确认。</p>")
    body = "".join(blocks)
    if can_mark:
        body = form(f"/cases/{cid}/review", body + "<p><button>保存确认</button></p>", csrf)
    return f"<section><h2>口径单</h2>{head}{body}</section>"


def create_app(root: str | Path, llm_factory: Callable[[], object], *, max_parallel: int = 2, model_name: str = "",
               max_steps: int = 150, max_tokens: int = 4_000_000, scenario_dirs: list[str | Path] | None = None,
               unlimited_case_tokens: Iterable[str] = ()):
    """scenario_dirs：另外的场景目录（如财务维护的共享目录）；数据目录下的 scenarios/ 总会用上，规则版本和记忆默认写在那里。"""
    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

    store = Store(Path(root))
    from bonus_agent.scenarios import Registry, ScenarioError
    from bonus_agent.scenarios import releases as rl
    registry = Registry([store.root / "scenarios", *(scenario_dirs or [])])
    runner = Runner(store, llm_factory, max_parallel, max_steps, max_tokens, registry=registry,
                    unlimited_case_tokens=unlimited_case_tokens)
    app = FastAPI(title="奖金计算", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.runner, app.state.registry = store, runner, registry
    sessions: dict[str, tuple[str, float]] = {}            # 会话号 -> (用户名, 到期时间)；服务重启后要重新登录
    failures: dict[str, list[float]] = {}                   # 用户名 -> 最近登录失败的时间
    app.state.sessions = sessions

    @app.middleware("http")
    async def headers(request, call_next):
        resp = await call_next(request)
        resp.headers.update({"X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                             "Cache-Control": "no-store",
                             "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; form-action 'self'"})
        return resp

    def user(request: Request) -> dict:
        """登录状态放在服务端会话里，浏览器只拿到随机会话号（HttpOnly Cookie）；角色每次从 users.yaml 读，改了立即生效。"""
        sid = request.cookies.get(COOKIE, "")
        name, exp = sessions.get(sid, ("", 0.0))
        info = store.users.load().get(name) if exp > time.time() else None
        if info is None:
            sessions.pop(sid, None)
            nxt = request.url.path if request.method == "GET" else "/"
            raise HTTPException(303, "请先登录", headers={"Location": f"/login?next={quote(nxt)}"})
        return {"name": name, "display": info.get("display") or name, "roles": list(info.get("roles") or []),
                "sid": sid, "csrf": store.csrf(sid)}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        nxt = request.query_params.get("next", "/")
        msg = request.query_params.get("msg", "")
        body = ("<section style='max-width:360px;margin:40px auto'><h1>登录</h1>"
                + (f"<p class='bad'>{e(msg)}</p>" if msg else "")
                + f"<form method='post' action='/login'><input type='hidden' name='next' value='{e(nxt)}'>"
                "<p><input type='text' name='username' placeholder='用户名' autocomplete='username' required></p>"
                "<p><input type='password' name='password' placeholder='口令' autocomplete='current-password' required></p>"
                "<p><button>登录</button></p></form></section>")
        return page("登录", body, None)

    @app.post("/login")
    async def login(request: Request):
        f = await request.form()
        name, pw = str(f.get("username", "")).strip(), str(f.get("password", ""))
        nxt = str(f.get("next") or "/")
        nxt = nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"
        recent = [t for t in failures.get(name, []) if t > time.time() - LOCK_SECONDS]
        if len(recent) >= MAX_FAILURES:
            return RedirectResponse(f"/login?next={quote(nxt)}&msg={quote('失败次数太多，请 5 分钟后再试')}", status_code=303)
        u = store.users.check(name, pw)
        if u is None:       # 具体原因只打在服务端控制台，不告诉浏览器
            failures[name] = recent + [time.time()]
            known = name in store.users.load()
            print(f"登录失败：{'口令不对' if known else '没有这个用户'}（用户名「{name}」，数据目录 {store.root.resolve()}）",
                  flush=True)
            return RedirectResponse(f"/login?next={quote(nxt)}&msg={quote('用户名或口令不对')}", status_code=303)
        failures.pop(name, None)
        sid = secrets.token_urlsafe(32)
        sessions[sid] = (name, time.time() + SESSION_SECONDS)
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(COOKIE, sid, max_age=SESSION_SECONDS, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https")
        return resp

    @app.post("/logout")
    async def logout(request: Request, u: dict = Depends(user)):
        await posted(request, u)
        sessions.pop(u["sid"], None)
        resp = RedirectResponse("/login?msg=" + quote("已退出"), status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    async def posted(request: Request, u: dict) -> dict:
        """读表单并核对防伪码（防止别的网页借浏览器里的登录状态提交）。"""
        f = await request.form()
        if not hmac.compare_digest(str(f.get("csrf", "")), u["csrf"]):
            raise HTTPException(403, "表单已失效，请刷新页面后重试")
        return f

    def need(u: dict, role: str):
        if role not in u["roles"]:
            raise HTTPException(403, f"需要角色 {role}（{ROLES[role]}）")

    def case_dir(cid: str) -> Path:
        try:
            return store.dir(cid)
        except KeyError:
            raise HTTPException(404, "没有这个案卷") from None

    def idle(cid: str):
        if runner.running(cid):
            raise HTTPException(409, "这个案卷正在运行，请等本轮结束")

    def back(cid: str, msg: str = "") -> RedirectResponse:
        return RedirectResponse(f"/cases/{cid}" + (f"?msg={msg}" if msg else ""), status_code=303)

    def scenarios() -> tuple[list, str]:
        try:
            return registry.list(), ""
        except ScenarioError as err:          # 某个场景包写错了：照样能打开案卷，页面上说明
            return [], str(err)

    def scenario_label(m: dict) -> str:
        sid = m.get("scenario")
        if not sid:
            return "通用"
        try:
            name = registry.get(sid).name
        except ScenarioError:
            name = sid
        return f"{name} · {MODES.get(m.get('mode', 'author'), '')}"

    @app.get("/", response_class=HTMLResponse)
    def index(u: dict = Depends(user)):
        rows = "".join(f"<tr><td><a href='/cases/{cid}'>{e(m['name'])}</a></td><td>{e(scenario_label(m))}</td>"
                       f"<td>{status_text(c) if m.get('mode') != 'compute' else '<span class=mut>计算</span>'}</td>"
                       f"<td class='mut'>{e(m['created_by'])} · {e(m['created'][:16])}</td></tr>" for cid, m, c in store.cases())
        scs, bad = scenarios()
        vers = {sc.id: registry.releases(sc.id) for sc in scs}
        srows = "".join(f"<tr><td><a href='/scenarios/{e(sc.id)}'>{e(sc.name)}</a></td>"
                        f"<td>{e(vers[sc.id][-1].version + f'（共 {len(vers[sc.id])} 个）') if vers[sc.id] else '<span class=mut>还没有发布</span>'}</td>"
                        f"<td class='mut'>{e(sc.description)}</td></tr>" for sc in scs)
        options = "".join(f"<option value='{e(sc.id)}'>{e(sc.name)}</option>" for sc in scs)
        new = form("/cases", "<p><input type='text' name='name' placeholder='如 2026 年 9 月提成' required></p>"
                             "<p>场景 <select name='scenario'>" + options + "<option value=''>通用（不分场景）</option></select> "
                             "<label><input type='radio' name='mode' value='author' checked> 编制：智能体读材料起草规则</label> "
                             "<label><input type='radio' name='mode' value='compute'> 计算：按已发布的规则版本直接算</label></p>"
                             f"<p>核算期间 <input type='text' name='period' placeholder='{PH}' style='max-width:22em'> "
                             "<span class='mut'>编制时按期间筛选数据；计算时每次另选。不写则不按期间筛选</span></p>"
                             "<p><label class='mut'><input type='checkbox' name='memory' value='1' checked> 编制时参考场景记忆"
                             "（以前确认过的口径；评测时可取消）</label></p>"
                             "<p><button>新建案卷</button></p>", u["csrf"]) if "editor" in u["roles"] else ""
        body = (f"<h1>案卷</h1><section><div class='wrap'><table><tr><th>名称</th><th>场景</th><th>状态</th><th>创建</th></tr>{rows}</table></div>"
                + ("" if rows else "<p class='mut'>还没有案卷</p>") + "</section>"
                + f"<section><h2>场景</h2>" + (f"<p class='bad'>{e(bad)}</p>" if bad else "")
                + f"<div class='wrap'><table><tr><th>场景</th><th>最新规则版本</th><th>说明</th></tr>{srows}</table></div>"
                  f"<p class='mut'>场景包放在 {e(store.root / 'scenarios')}（每个场景一个文件夹：scenario.yaml、guide.md 场景说明）</p></section>"
                + (f"<section><h2>新建</h2>{new}</section>" if new else ""))
        return page("案卷", body, u)

    @app.post("/cases")
    async def create(request: Request, u: dict = Depends(user)):
        need(u, "editor")
        f = await posted(request, u)
        name = str(f.get("name", "")).strip()[:100]
        if not name:
            raise HTTPException(400, "请填写名称")
        sid, mode = str(f.get("scenario", "")).strip() or None, str(f.get("mode", "author"))
        try:
            period = norm_period(f.get("period"))
            if sid:
                registry.get(sid)
                if mode == "compute":
                    rl.get_release(registry, sid)
            cid = store.create(name, u["display"], sid, mode, memory=f.get("memory") == "1", period=period)
        except (ScenarioError, ValueError) as err:
            raise HTTPException(400, str(err)) from None
        return back(cid)

    def compute_sections(cid: str, m: dict, u: dict) -> list[str]:
        """计算模式的案卷：选规则版本、传本期数据、按输入模板检查后直接算（不调用大模型），历次计算的结果。"""
        d, csrf = case_dir(cid), u["csrf"]
        sid = m["scenario"]
        try:
            sc, rels = registry.get(sid), registry.releases(sid)
        except ScenarioError as err:
            return [f"<section class='warn'>{e(err)}</section>"]
        head = (f"<p>场景：<a href='/scenarios/{e(sid)}'>{e(sc.name)}</a> · 计算模式（按已发布的规则版本直接算，不调用大模型）</p>"
                + ("" if rels else "<p class='bad'>这个场景还没有发布的规则版本，先新建“编制”案卷起草并发布</p>"))
        if rels and "editor" in u["roles"]:
            opts = "".join(f"<option value='{e(r.version)}'{' selected' if r is rels[-1] else ''}>{e(r.summary)}</option>"
                           for r in reversed(rels))
            head += form(f"/cases/{cid}/compute", f"<p>规则版本 <select name='version'>{opts}</select> "
                                                  f"核算期间 <input type='text' name='period' value='{e(m.get('period') or '')}' "
                                                  f"placeholder='{PH}' style='max-width:18em' required></p>"
                                                  "<p><input type='file' name='files' multiple required> <button>检查并计算</button></p>"
                                                  "<p class='mut'>先按输入模板检查本期数据（表、列、数字格式），有错误不计算；"
                                                  "通过后按所选版本计算，结果可下载。</p>", csrf, upload=True)
        runs = sorted((d / "runs").glob("*"), reverse=True)
        body = ""
        for r in runs:
            info = json.loads((r / "run.json").read_text(encoding="utf-8")) if (r / "run.json").exists() else {}
            check = (r / "输入检查.txt").read_text(encoding="utf-8") if (r / "输入检查.txt").exists() else ""
            err = (r / "错误.txt").read_text(encoding="utf-8") if (r / "错误.txt").exists() else ""
            state = (f"<span class='ok'>按 {e(info.get('version'))} 算完" + (f"，期间 {e(info['period'])}" if info.get("period") else "")
                     + "</span>" if info else "<span class='bad'>没有算出结果</span>")
            body += (f"<details{' open' if r == runs[0] else ''}><summary>{e(r.name)} {state}</summary>"
                     + (f"<pre>{e(err)}</pre>" if err else "") + (f"<pre>{e(check)}</pre>" if check else "")
                     + files_table(r, f"/cases/{cid}/dl/runs/{r.name}") + "</details>")
        log = store.audit_log(cid)[-30:][::-1]
        rows = "".join(f"<tr><td class='mut'>{e(a['at'][5:16].replace('T', ' '))}</td><td>{e(a['user'])}</td><td>{e(a['action'])}</td>"
                       f"<td class='mut'>{e(detail(a))}</td></tr>" for a in log)
        return [f"<section><h2>计算</h2>{head}</section>",
                f"<section><h2>计算结果</h2>{body or '<p class=mut>还没有计算</p>'}</section>",
                f"<section><h2>操作记录</h2><div class='wrap'><table>{rows}</table></div></section>"]

    @app.get("/cases/{cid}", response_class=HTMLResponse)
    def view(cid: str, request: Request, u: dict = Depends(user)):
        d = case_dir(cid)
        m, case, out = store.meta(cid), cs.load_case(d / "out"), d / "out"
        csrf, job = u["csrf"], runner.jobs.get(cid)
        editor, approver, busy = "editor" in u["roles"], "approver" in u["roles"], runner.running(cid)
        msg = request.query_params.get("msg", "")
        parts = [f"<h1>{e(m['name'])}</h1>"]
        if msg:
            parts.append(f"<section class='warn'>{e(msg)}</section>")
        if busy:
            parts.append("<section class='warn'>智能体正在处理中，请勿修改材料或案卷目录里的文件；"
                         "需要补充或更正材料，请等本轮结束后再上传。</section>")
        if m.get("mode") == "compute":
            return page(m["name"], "".join(parts + compute_sections(cid, m, u)), u)
        if m.get("scenario"):
            parts.append(f"<p class='mut'>场景：<a href='/scenarios/{e(m['scenario'])}'>{e(scenario_label(m))}</a>"
                         + ("" if m.get("memory", True) else "（不参考场景记忆）") + "</p>")
        ptext = f"核算期间：{m['period']}" if m.get("period") else "核算期间：未指定（不按期间筛选）"
        if editor and not busy:
            parts.append(form(f"/cases/{cid}/period", f"<p class='mut'>{e(ptext)} <input type='text' name='period' "
                                                       f"value='{e(m.get('period') or '')}' placeholder='{PH}' style='max-width:18em'> "
                                                       "<button>改期间</button>（下一轮起生效）</p>", csrf, inline=True))
        else:
            parts.append(f"<p class='mut'>{e(ptext)}</p>")
        last = (case or {}).get("rounds", [{}])[-1] if case else {}
        fixed_saved=cr.state_of(case)
        revision=cr.active(cr.load(out)) if fixed_saved else None
        current_issue=''
        if fixed_saved:
            try:cr.validate_current(out,case,final=last.get('status')=='OK')
            except cs.ConfirmError as err:current_issue=str(err)
            if not current_issue and fixed_saved.get('status')=='AWAITING_TOTAL_CONFIRMATION' and not list(pending_basis_confirmations(case)):
                current_issue='本版总额复核回执或金额凭据不完整，需要重新核验后确认。'
        info = f"<p>{status_text(case)}</p>"
        if current_issue:info+=f"<p class='warn'>{e(current_issue)} 历史文件可查看，不是当前有效交付。</p>"
        if last.get("summary"):
            info += f"<p class='mut'>方案概述：{e(last['summary'])}</p>"
        failed = (job.status == "failed" if job and not busy else False) or (last.get("status") == "FAILED" and not busy)
        if last.get("error") or (job and job.status == "failed" and not busy):
            error = (job.message if job and job.status == "failed" else "") or last.get("error", "")
            saved = last.get('failure_context') if error == last.get('error') else None
            x = explain_failure(error, workspace=(case or {}).get('workspace'), diagnosis=saved)
            info += (f"<div class='fail'><p class='bad'><b>{e(x['title'])}</b></p>{render_diagnosis(x['diagnosis'])}"
                     + f"<p>{e(x['advice'])}</p>"
                     + (form(f"/cases/{cid}/run", "<p><button>重试</button> <span class='mut'>不补充信息，从上一轮做到的地方接着做"
                                                 "（网络断了、步数或用量用完、手动停止后用）。需要补充说明或材料时，"
                                                 "在下方对话框写清楚后点「发送」</span></p>", csrf) if editor and failed else "")
                     + f"<details><summary class='mut'>详细信息（给开发排查）</summary><pre>{e(x['detail'])}</pre></details></div>")
        if model_name:
            info += f"<p class='mut'>模型：{e(model_name)}</p>"
        if cid in runner.unlimited_case_tokens:
            info += "<p class='mut'>本案卷当前不设累计 token 上限，用量继续记账。</p>"
        check = out / "expected_check.txt"
        if check.exists() and not busy and not current_issue:
            info += f"<p class='mut'>与期望结果核对：</p><pre>{e(check.read_text(encoding='utf-8'))}</pre>"
        if job:
            cls = "warn" if job.status == "running" else "bad" if job.status == "failed" else STATUS.get(job.result, ("", "mut"))[1]
            head = ("正在运行……" if job.status == "running" else
                    "本轮未完成（原因和处理办法见上方）" if job.status == "failed" or job.result == "FAILED" else job.message)
            info += (f"<p class='{cls}'>{e(head)}（{e(job.by)}，{e(job.started[11:16])} 开始）</p>"
                     # 最新的一步放在最上面：页面每 3 秒刷新一次会回到顶部，倒序才能一眼看到最新进展
                     + (f"<p class='mut'>最近 {min(len(job.steps), 15)} 步，最新的在最上面：</p>"
                        f"<pre>{e(chr(10).join(reversed(job.steps[-15:])))}</pre>" if job.steps else ""))
        if editor and busy and job and job.status == "running":
            info += form(f"/cases/{cid}/stop", "<p><button>停止</button> <span class='mut'>当前这一步做完后停下，已做的部分会保存</span></p>"
                         if not job.cancel else "<p class='mut'>正在停止……</p>", csrf)
        if editor and not busy:
            if fixed_saved and fixed_saved.get('status')=='AWAITING_TOTAL_CONFIRMATION' and not current_issue:
                info+="<p class='mut'>本版总额已完成复核，下一步是审批确认；金额有误才发起财务修订。</p>"
            elif fixed_saved and fixed_saved.get('status')=='AWAITING_TOTAL_CONFIRMATION' and current_issue:
                info+=form(f'/cases/{cid}/run','<p><button>重新核验总额（保留已核验节点）</button></p>',csrf)
            else:
                info += form(f"/cases/{cid}/run", "<p><button>运行下一轮</button> <label class='mut'><input type='checkbox' "
                                                  "name='new' value='1'> 从头开始（不接着上一轮）</label></p>", csrf)
        parts.append(f"<section><h2>状态</h2>{info}</section>")
        # Approval is the primary next action, not buried under revision forms,
        # logs and historical downloads. Viewers can see exactly why it is disabled.
        parts.append(basis_confirmation_section(cid,out,case,csrf,approver,busy,current_issue))
        # Fixed-chain progress uses actual per-node receipts, not number of
        # generic tool calls. A running CLI acceptance writes the same atomic
        # checkpoint as the web worker, so it can also be inspected here.
        fixed=fixed_saved
        checkpoint=out/'commission_checkpoint.json'
        if checkpoint.is_file():
            try:
                live=json.loads(checkpoint.read_text(encoding='utf-8'))
                if (not revision or live.get('revision_id')==revision['id']) and (not fixed or live.get('checkpoint_at',0)>=fixed.get('checkpoint_at',0)):fixed=live
            except (OSError,ValueError):pass
        if fixed:
            names=[('inventory','来源盘点'),('scope_mapping','项目四键/首发/毛利/欠款'),('reference_mapping','历史/预算/库存/逾期数据'),
                   ('project_policy','计算基数/新老/阶梯'),('coefficient_policy','各考核系数'),('hold_policy','提成总额/暂扣/项目舍入'),
                   ('basis','项目基础金额'),('basis_review','独立项目复核'),('confirm_basis','本版总额确认'),('roster','人员名单及角色'),
                   ('settlement','逐角色与人员汇总'),('settlement_review','人员独立复核'),('export','正式输出回读')]
            observed={x.get('node'):x for x in fixed.get('events',[])}
            lines=[]
            for node,label in names:
                r=fixed.get('node_receipts',{}).get(node) or ({} if revision and node in revision['affected'] else fixed.get('receipts',{}).get(node) or observed.get(node,{}))
                passed=cr.approval_matches(fixed) if node=='confirm_basis' else r.get('status')=='PASS'
                text='已核验' if passed else '有反例' if r.get('status')=='FAIL' else '等待执行/确认'
                lines.append(f"<tr><td>{e(label)}</td><td class='{'ok' if passed else 'warn'}'>{e(text)}</td><td>{e(r.get('reviewer') or r.get('model') or '程序')}</td></tr>")
            parts.append("<section><h2>固定提成核算节点</h2><p class='mut'>节点通过代表已保存本版本验收回执；实际财务金额仍须独立核对。</p>"
                         "<div class='wrap'><table><thead><tr><th>节点</th><th>状态</th><th>处理者</th></tr></thead><tbody>"+''.join(lines)+"</tbody></table></div>"
                         +(f"<p class='bad'>{e(fixed['error'])}</p>" if fixed.get('status')=='FAILED' and fixed.get('error') else '')+"</section>")
        if fixed_saved:parts.append(commission_feedback_section(cid,out,case,csrf,editor,approver,busy))
        msgs = "".join(f"<div class='msg {m['role']}'><div class='mut'>{e(m['user'])} · {e(m['at'][5:16].replace('T', ' '))}</div>"
                       f"<div class='bubble'>{e(m['text'])}</div></div>" for m in store.chat(cid)[-40:])
        if busy and job and job.steps:
            msgs += "<div class='msg agent'><div class='bubble mut'>正在处理……</div></div>"
        box = form(f"/cases/{cid}/chat", "<textarea name='text' rows='3' placeholder='直接问结果（如：E001 的季度奖怎么算的？），"
                                         "让智能体修改（如：华东区域系数改为 1.05），或者附上材料并说明'></textarea>"
                                         "<p><input type='file' name='files' multiple> <label class='mut'><input type='checkbox' "
                                         "name='expected' value='1'> 附件是期望结果（只用于核对，不交给智能体）</label></p>"
                                         "<p><button>发送</button> <span class='mut'>附上材料（规则文档、Excel、PDF，同名覆盖）会按新材料处理一轮；"
                                         "上一轮没完成时，发的文字作为说明，智能体带着它接着做；已完成时，提问直接回答，"
                                         "要求修改会改完重新试算</span></p>", csrf, upload=True) \
            if editor and not busy else ""
        mats = files_table(d / "materials", f"/cases/{cid}/dl/materials", csrf, f"/cases/{cid}/materials/delete" if editor and not busy else None)
        exps = files_table(d / "expected", f"/cases/{cid}/dl/expected", csrf, f"/cases/{cid}/expected/delete" if editor and not busy else None)
        n_mat = sum(1 for p in (d / "materials").rglob("*") if p.is_file())
        files = (f"<details><summary class='mut'>已有材料 {n_mat} 个（查看、下载、删除）</summary>{mats}</details>"
                 f"<details><summary class='mut'>期望结果（只用于核对）</summary>{exps}</details>")
        parts.append(f"<section><h2>对话</h2><div class='chat'>{msgs or '<p class=mut>还没有对话。先附上材料发送，智能体就开始处理</p>'}</div>"
                     f"{box}{files}</section>")
        # 问题
        qs = load_questions(out)
        if fixed_saved:
            # Current persisted withdrawal states take precedence over an old
            # questions.yaml that predates the financial revision.
            withdrawn={q['id']:q.get('withdrawn') for q in fixed_saved.get('questions',[]) if q.get('withdrawn')}
            for q in qs:
                if q['id'] in withdrawn:q['withdrawn']=withdrawn[q['id']]
        if qs:
            items = []
            workflow = (case or {}).get('workspace', {}).get('workflow', {})
            for q in qs:
                effect = workflow.get('batch_question_effects', {}).get(q['id'], {})
                scope = workflow.get('applicability_questions', {}).get(q['id'], {})
                inactive = (effect.get('version') and effect['version'] == workflow.get('view_basis_version')
                            and effect.get('question') == scope.get('question') and effect.get('scheme') == scope.get('scheme')
                            and last.get('period') == m.get('period') and q['id'] not in last.get('pending', ())
                            and not q.get('answer') and not q.get('withdrawn'))
                state = (f"<span class='mut'>已撤回：{e(q['withdrawn'])}</span>" if q.get("withdrawn") else
                         f"<span class='ok'>已答复：{e(q['answer'])}</span>" + (f"<span class='mut'>（{e(q.get('answered_by'))} "
                                                                             f"{e(str(q.get('answered_at', ''))[:16])}）</span>"
                                                                             if q.get("answered_by") else "")
                         if q.get("answer") not in (None, "") else
                         f"<span class='ok'>第 {e(last.get('n'))} 轮已核验，本期未触发</span>" if inactive else
                         "<span class='warn'>等待答复</span>" if q.get("blocking", True) else "<span class='mut'>提示</span>")
                opts = ""
                if (editor or (fixed_saved and approver)) and not q.get("withdrawn") and not inactive:
                    opts = "".join(f"<label class='opt'><input type='radio' name='ans_{e(q['id'])}' value='{i}'> {e(o)}</label>"
                                   for i, o in enumerate(q.get("options") or []))
                    opts += (f"<label class='opt'><input type='radio' name='ans_{e(q['id'])}' value='{OTHER}'> 其他（在下面写明）</label>"
                             f"<input type='text' name='text_{e(q['id'])}' placeholder='补充说明或其他答复'>")
                sug = q.get("suggested")
                hint = (f"<div class='mut'>智能体建议（仅供参考，不会被采用）：{e(q['options'][sug])}</div>"
                        if isinstance(sug, int) and 0 <= sug < len(q.get("options") or []) else "")
                ev = q.get("evidence") or []
                if ev:
                    hint += ("<div class='mut'>依据（逐字核对原文；没核对上的请以材料为准）：<ul>" + "".join(
                        f"<li>《{e(x.get('file', ''))}》「{e(x.get('quote', ''))}」"
                        + ("" if x.get("ok") else " <span class='warn'>原文里没核对上</span>") + "</li>" for x in ev)
                        + "</ul></div>")
                elif hint:
                    hint += "<div class='warn'>智能体没有给出建议的依据，请对照材料判断</div>"
                if inactive:
                    hint += f"<div class='mut'>{e(effect.get('evidence', {}).get('reason', ''))}</div>"
                items.append(f"<div class='q'><b>{e(q['id'])}</b> <span class='mut'>第 {e(q.get('round', 1))} 轮</span> {state}"
                             f"<div>{e(q['text'])}</div>{render_diagnosis(q.get('diagnosis'))}{hint}{opts}</div>")
            body = "".join(items)
            if (editor or (fixed_saved and approver)) and not busy:
                body = form(f"/cases/{cid}/answers", body + "<p><button>保存答复</button> "
                                                           "<span class='mut'>保存后点“运行下一轮”</span></p>", csrf)
            parts.append(f"<section><h2>问题</h2>{body}</section>")
        # 口径单
        if (out / rv.FILE).exists():
            parts.append(review_section(cid, out, csrf, approver and not busy))
        # 结果
        reports=[];preferred_report=None
        if fixed_saved:
            from bonus_agent.commission_report import reports_for_case
            reports=reports_for_case(out,case)
            preferred_report=next((entry for entry in reports if entry['current'] and not current_issue),None)
        top = [p for p in sorted(out.glob("*")) if p.is_file() and p.name != cs.CASE_FILE]
        if fixed_saved:
            last_files={v[0] for v in last.get('outputs',{}).values()} if not revision or last.get('revision_id')==revision['id'] else set()
            if current_issue:last_files-=cr.CURRENT_FINANCIAL_FILES
            top=[p for p in top if p.name in last_files or p.name==cr.FILE]
        groups: dict[str, list[str]] = {}
        for p in top:
            g, note = output_kind(p.name)
            if p.name=='result.xlsx' and preferred_report:
                groups.setdefault(g,[]).append(f"<li><b><a href='/cases/{cid}/dl/out/{e(preferred_report['relative'])}'>新版核算底稿和汇总.xlsx</a></b> "
                    f"<span class='ok'>当前已核验金额的新版报表</span> · <a href='/cases/{cid}/dl/out/result.xlsx'>原已核验报表（留档）</a></li>")
            else:
                groups.setdefault(g, []).append(f"<li><a href='/cases/{cid}/dl/out/{e(p.name)}'>{e(p.name)}</a> "
                                                f"<span class='mut'>{e(note)}</span></li>")
        res = "".join((f"<p><b>{e(g)}</b></p><ul>{''.join(groups[g])}</ul>" if g == OUT_GROUPS[0] else
                       f"<details><summary>{e(g)}（{len(groups[g])} 个）</summary><ul>{''.join(groups[g])}</ul></details>")
                      for g in OUT_GROUPS if g in groups)
        hist = sorted((out / "history").glob("*")) if (out / "history").exists() else []
        res = (res or "<p class='mut'>无</p>") + (
            "<details><summary>历次存档（每轮的全部文件）</summary>" + "".join(
                f"<details><summary>{e(h.name)}</summary>{files_table(h, f'/cases/{cid}/dl/out/history/{h.name}')}</details>"
                for h in hist) + "</details>" if hist else "")
        parts.append(f"<section><h2>本轮输出</h2>{res}</section>")
        if fixed_saved:
            report_body=''
            for entry in reports:
                r=entry['metadata'];current=entry['current'] and not current_issue
                label='当前核验金额对应的报表' if current else '历史报表（不是当前核验金额）'
                report_body+=f"<p><b>{e(label)}</b> · 第 {e(r['source_round'])} 轮<br><a href='/cases/{cid}/dl/out/{e(entry['relative'])}'>下载新版核算底稿和汇总（{e(r['format'])}）</a> · {e(r['created_at'])}<br><span class='mut'>逐项目及人员金额已从原件重新复算；不产生新的财务审批。</span></p>"
            if (editor or approver) and not busy and not current_issue and last.get('status')=='OK':
                report_body+=form(f'/cases/{cid}/reexport','<p><button>按当前已核验结果重新导出（不调用大模型）</button> <span class="mut">仅报表结构修订，保留原结果和审批。</span></p>',csrf)
            if report_body:parts.append('<section><h2>新版报表 / 历史导出修订</h2>'+report_body+'</section>')
        # 确认
        conf = (case or {}).get("confirmed")
        cbody = (f"<p class='{'ok' if not current_issue and conf['round']==last.get('n') else 'mut'}'>{'已确认' if not current_issue and conf['round']==last.get('n') else '历史已确认（不是本版确认）'}：第 {e(conf['round'])} 轮，{e(conf['by'])}，{e(conf['at'][:16])}"
                 + (f"，{e(conf['note'])}" if conf.get("note") else "") + "</p>") if conf else "<p class='mut'>尚未确认</p>"
        if approver and not busy and not current_issue and last.get("status") == "OK" and not last.get("confirmed"):
            opts = cs.confirm_options(out)
            ask = ("<p><b>录入模板的规则" + ("（相对已有模板的改动）" if opts["has_template"] else "") + "：</b><br>"
                   + "<br>".join(e(x) for x in opts["rule_changes"] or ["规则没有改动"]) + "</p>")
            if opts["revisions"]:
                ask += "<p><b>人工调整修订</b>（勾选的录入模板，以后每期按模板计算都会自动带上；不勾选的只在本轮结果里）：</p>"
                ask += "".join(
                    f"<p><label><input type='checkbox' name='revision' value='{e(r['key'])}'"
                    + (" checked disabled" if r["required_by"] else "") + f"> {e(r['key'])}"
                    + ("（本轮新增）" if r["new"] else "")
                    + (f"（方案 {e('、'.join(r['required_by']))} 引用了它的结果，必须录入）" if r["required_by"] else "") + "：" + e("；".join(f"{x['node']} {x['amount']}（{x['reason']}）"
                                                                    for x in r["adjustments"][:5]))
                    + ("……" if len(r["adjustments"]) > 5 else "") + "</label></p>" for r in opts["revisions"])
            fixed=(case or {}).get('workspace',{}).get('workflow',{}).get('fixed_commission')
            if fixed:
                cbody += form(f"/cases/{cid}/confirm", "<p>固定提成链路已逐节点验收。后续每期重新绑定材料，不使用旧版自由编制模板。</p>"
                              "<p><input type='text' name='note' placeholder='确认说明（可选）'></p>"
                              f"<button name='save' value='result'>确认第 {e(last['n'])} 轮核算结果</button>",csrf)
            else:
                cbody += form(f"/cases/{cid}/confirm", ask + "<p><input type='text' name='note' placeholder='确认说明（可选）'></p>"
                              f"<p><button name='save' value='template'>确认第 {e(last['n'])} 轮结果并录入模板</button> "
                              "<button name='save' value='result'>只确认结果，不录入模板</button></p>", csrf)
        if (out / rv.FILE).exists():
            st = rv.status(out)
            cbody += (f"<p class='{'ok' if not st['pending'] else 'warn'}'>口径单：已确认 {st['confirmed']} / {st['total']} 行"
                      + (f"，{len(st['wrong'])} 行有误" if st["wrong"] else "") + "（全部确认后才能确认结果）</p>")
        cbody += ("<p class='mut'>固定提成确认绑定本版规则、对象、金额及来源。以后更换规则或材料须重新绑定并核验。</p>" if fixed_saved else "<p class='mut'>确认前会核对口径单已逐条确认、输出文件没被改过，并按模板复算、与该轮结果逐分一致。确认后每期换数据直接按模板计算，不再调用大模型。</p>")
        parts.append(f"<section><h2>确认</h2>{cbody}</section>")
        if m.get("scenario") and conf and (out / "template" / "manifest.yaml").exists():
            pub = [x for x in m.get("published") or [] if x.get("confirmed_at") == conf.get("at")]
            if pub:
                pbody = f"<p class='ok'>已发布为规则版本 {e(pub[-1]['version'])}（{e(pub[-1]['by'])}，{e(pub[-1]['at'][:16])}）</p>"
            else:
                pbody = "<p class='mut'>还没有发布。发布后，这个场景以后每期新建“计算”案卷、换数据直接按这个版本算；" \
                        "本案卷里已答复的问题会记为场景记忆，下次编制时参考。</p>"
                if approver and not busy:
                    pbody += form(f"/cases/{cid}/publish", "<p><input type='text' name='note' placeholder='版本说明（可选，如 2026 年 9 月起适用）'></p>"
                                                           "<p><button>发布为场景的新规则版本</button></p>", csrf)
            parts.append(f"<section><h2>发布规则版本</h2>{pbody}</section>")
        if (out / "template" / "manifest.yaml").exists():
            tbody = form(f"/cases/{cid}/template", "<p><input type='file' name='files' multiple> <button>按模板计算</button></p>",
                         csrf, upload=True) if editor else ""
            runs = sorted((d / "runs").glob("*"), reverse=True)
            tbody += "".join(f"<details><summary>{e(r.name)}</summary>{files_table(r, f'/cases/{cid}/dl/runs/{r.name}')}</details>"
                             for r in runs)
            parts.append(f"<section><h2>按模板计算（本期数据）</h2>{tbody}</section>")
        log = store.audit_log(cid)[-30:][::-1]
        rows = "".join(f"<tr><td class='mut'>{e(a['at'][5:16].replace('T', ' '))}</td><td>{e(a['user'])}</td><td>{e(a['action'])}</td>"
                       f"<td class='mut'>{e(detail(a))}</td></tr>" for a in log)
        parts.append(f"<section><h2>操作记录</h2><div class='wrap'><table>{rows}</table></div></section>")
        return page(m["name"], "".join(parts), u, refresh=busy)

    async def _upload(request: Request, u: dict, cid: str, area: str, single: bool = False) -> list[str]:
        case_dir(cid)
        idle(cid)
        f = await posted(request, u)
        return await _save(cid, u, [x for x in f.getlist("files") if getattr(x, "filename", "")], area, single)

    async def _save(cid: str, u: dict, files: list, area: str, single: bool = False, description: str = '') -> list[str]:
        d = case_dir(cid)
        if not files:
            raise HTTPException(400, "没有选择文件")
        if single:
            files = files[:1]
        got = []          # 先把每个文件都读完、校验过，再动磁盘：有一个被拒，原有文件和其他新文件都不变
        for x in files:
            try:
                name = safe_name(x.filename)
            except ValueError as err:
                raise HTTPException(400, str(err)) from None
            data = await x.read(MAX_UPLOAD + 1)
            if len(data) > MAX_UPLOAD:
                raise HTTPException(413, f"{name} 超过 {MAX_UPLOAD // 1024 // 1024} MB")
            got.append((name, data))
        saved = []
        try:
            with cs.case_lock(d/'out'):
                obj=cs.load_case(d/'out')
                if area=='materials' and cr.state_of(obj):
                    changed=[d/area/name for name,data in got if not (d/area/name).is_file() or cs.sha256(d/area/name)!=hashlib.sha256(data).hexdigest()]
                    if changed or description:
                        cr.submit_locked(d/'out',obj,by=u['display'],kind='material_correction' if changed else 'discrepancy',
                                         description='上传材料：'+'、'.join(name for name,_ in got)+(f'；{description}' if description else ''),source_changes=changed)
                if single:
                    for old in (d / area).glob("*"):
                        old.unlink()
                for name, data in got:
                    (d / area / name).write_bytes(data)
                    saved.append(name)
                    store.audit(cid, u["display"], "上传" + ("期望结果" if area == "expected" else "材料"), file=name,
                                sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        except (cr.RevisionError,cs.CaseBusyError) as err:raise HTTPException(409,str(err)) from None
        return saved

    @app.post("/cases/{cid}/materials")
    async def upload(cid: str, request: Request, u: dict = Depends(user)):
        need(u, "editor")
        names = await _upload(request, u, cid, "materials")
        return back(cid, f"已上传 {len(names)} 个文件，点“运行下一轮”处理")

    @app.post("/cases/{cid}/expected")
    async def upload_expected(cid: str, request: Request, u: dict = Depends(user)):
        need(u, "editor")
        await _upload(request, u, cid, "expected", single=True)
        return back(cid)

    async def _delete(cid: str, request: Request, u: dict, area: str):
        need(u, "editor")
        d = case_dir(cid)
        idle(cid)
        f = await posted(request, u)
        try:
            p = inside(d / area, str(f.get("name", "")))
        except KeyError:
            raise HTTPException(404, "没有这个文件") from None
        try:
            with cs.case_lock(d/'out'):
                obj=cs.load_case(d/'out')
                if area=='materials' and cr.state_of(obj):
                    cr.submit_locked(d/'out',obj,by=u['display'],kind='material_correction',description=f'删除原始材料 {p.name}',source_changes=[p])
                p.unlink()
        except (cr.RevisionError,cs.CaseBusyError) as err:raise HTTPException(409,str(err)) from None
        store.audit(cid, u["display"], "删除" + ("期望结果" if area == "expected" else "材料"), file=p.name)
        return back(cid)

    @app.post("/cases/{cid}/materials/delete")
    async def delete(cid: str, request: Request, u: dict = Depends(user)):
        return await _delete(cid, request, u, "materials")

    @app.post("/cases/{cid}/expected/delete")
    async def delete_expected(cid: str, request: Request, u: dict = Depends(user)):
        return await _delete(cid, request, u, "expected")

    @app.post("/cases/{cid}/answers")
    async def answers(cid: str, request: Request, u: dict = Depends(user)):
        d = case_dir(cid)
        idle(cid)
        f = await posted(request, u)
        out=d/'out'
        try:
            with cs.case_lock(out):
                obj=cs.load_case(out);fixed=cr.state_of(obj)
                if fixed:
                    if not {'editor','approver'}.intersection(u['roles']):raise HTTPException(403,'需要财务经办或审批权限')
                    qs=load_questions(out)
                    persisted={q['id']:q for q in fixed.get('questions',[])}
                    for q in qs:
                        if persisted.get(q['id'],{}).get('withdrawn'):q['withdrawn']=persisted[q['id']]['withdrawn']
                    before={q['id']:q.get('answer') for q in qs}
                    done=_answer_updates(qs,{k:str(v) for k,v in f.items()},u['display'])
                    if any(before.get(qid) for qid in done) and 'approver' not in u['roles']:
                        raise HTTPException(403,'更正已经确认过的财务答复需要审批权限')
                    if done:
                        from bonus_agent.infra.atomic import write_text
                        write_text(out/'questions.yaml',QUESTIONS_HEAD+yaml.safe_dump(qs,allow_unicode=True,sort_keys=False))
                else:
                    need(u,'editor')
                    done=save_answers(out,{k:str(v) for k,v in f.items()},u['display'])
        except cs.CaseBusyError as err:raise HTTPException(409,str(err)) from None
        for qid in done:
            q = next(q for q in load_questions(d / "out") if q["id"] == qid)
            store.audit(cid, u["display"], "答复问题", question=qid, answer=q["answer"])
        return back(cid, f"已保存 {len(done)} 个答复，点“运行下一轮”继续" if done else "没有新的答复")

    @app.post("/cases/{cid}/review")
    async def review_marks(cid: str, request: Request, u: dict = Depends(user)):
        need(u, "approver")
        d = case_dir(cid)
        idle(cid)
        f = await posted(request, u)
        out = d / "out"
        rows = rv.load(out)
        decisions = {r["key"]: (str(f.get(f"st_{r['key']}")), str(f.get(f"note_{r['key']}", "")))
                     for r in rows if f.get(f"st_{r['key']}") in (rv.OK, rv.WRONG)}
        bulk = str(f.get("bulk", ""))
        if bulk:                      # 整节确认：可疑的行（程序检查标出的）不随整节确认，要单独确认
            marks = rv.load_marks(out)
            for r in rows:
                if f"{r['scheme']}|{r['section']}" == bulk and not r["flag"] and r["key"] not in decisions \
                        and marks.get(r["key"], {}).get("status") != rv.WRONG:
                    decisions[r["key"]] = (rv.OK, "")
        errs = rv.mark(out, decisions, u["display"])
        n_ok = sum(1 for st, _ in decisions.values() if st == rv.OK)
        n_bad = sum(1 for st, _ in decisions.values() if st == rv.WRONG) - len(errs)
        if decisions:
            store.audit(cid, u["display"], "口径单确认", confirmed=n_ok, wrong=n_bad, section=bulk or "")
        return back(cid, "；".join(errs) if errs else f"已记录：确认 {n_ok} 行" + (f"，有误 {n_bad} 行（点“运行下一轮”交给智能体改正）"
                                                                            if n_bad else ""))

    @app.post("/cases/{cid}/run")
    async def run(cid: str, request: Request, u: dict = Depends(user)):
        need(u, "editor")
        d = case_dir(cid)
        f = await posted(request, u)
        if not any((d / "materials").iterdir()):
            return back(cid, "还没有材料，请先上传")
        fix = rv.instruction(d / "out") if f.get("new") != "1" else ""
        try:
            if fix:                   # 口径单上标了有误：作为修改要求交给智能体，改完重新试算（start 会把它记进对话，这里不再重复记）
                runner.start(cid, u["display"], message=fix)
                rv.mark_sent(d / "out")
                return back(cid)
            runner.start(cid, u["display"], new=f.get("new") == "1")
        except RuntimeError as err:
            raise HTTPException(409, str(err)) from None
        return back(cid)

    @app.post("/cases/{cid}/stop")
    async def stop(cid: str, request: Request, u: dict = Depends(user)):
        need(u, "editor")
        case_dir(cid)
        await posted(request, u)
        job = runner.jobs.get(cid)
        if job and runner.running(cid):
            job.cancel = True
            store.audit(cid, u["display"], "停止运行")
        return back(cid)

    @app.post("/cases/{cid}/chat")
    async def chat(cid: str, request: Request, u: dict = Depends(user)):
        need(u, "editor")
        case_dir(cid)
        f = await posted(request, u)
        text = str(f.get("text", "")).strip()[:20000]
        files = [x for x in f.getlist("files") if getattr(x, "filename", "")]
        if not text and not files:
            return back(cid)
        idle(cid)
        out=case_dir(cid)/'out';obj=cs.load_case(out);fixed=cr.state_of(obj)
        try:
            if files and f.get("expected") == "1":       # 期望结果：只登记，不交给智能体
                names = await _save(cid, u, files, "expected", single=True)
                store.chat_add(cid, "user", u["display"], f"上传了期望结果：{'、'.join(names)}（只用于核对，不交给智能体）")
                if text and not fixed:
                    runner.start(cid, u["display"], message=text)
                elif text and fixed:
                    store.chat_add(cid,'user',u['display'],text)
                    store.chat_add(cid,'agent','智能体','期望结果只用于独立核对，不用于更改计算规则。请通过「财务反馈与版本修订」登记具体异议；原计算未改变。')
            elif files:                                  # 材料：附言作为说明，按新材料处理一轮
                names = await _save(cid, u, files, "materials",description=text if fixed else '')
                store.chat_add(cid, "user", u["display"], f"上传了材料：{'、'.join(names)}" + (f"\n{text}" if text else ""))
                runner.start(cid, u["display"], instruction=None if fixed else text or None)
            elif fixed:
                store.chat_add(cid,'user',u['display'],text)
                if cr.is_explanation(text):
                    reply=cr.explanation(out,obj,text)
                    store.chat_add(cid,'agent','智能体',reply)
                    return back(cid,'已解释本版计算，不启动新轮次')
                if cr.needs_structured_change(text):
                    store.chat_add(cid,'agent','智能体','识别到正式规则/分配变更请求。请在「调整业务规则及登记适用版本」或「仅修订指定项目角色的人员份额」选择原规则、新值及范围；由财务明确确认后才改变计算。若只是对原规则执行有异议，请使用金额异议入口。')
                    return back(cid,'新规则或份额需要明确范围和财务确认；未静默改变金额')
                if re.match(r'^q[-\w]+\s*[:：选]',text,re.I):
                    return back(cid,'请在本页具体问题处保存答复，随后运行下一轮；不将答复误登记为新的异议。')
                rev=cr.submit(out,by=u['display'],description=text,stage=cr.feedback_stage(text))
                store.audit(cid,u['display'],'登记财务反馈',revision=rev['id'],owners=rev['owners'])
                runner.start(cid,u['display'])
            elif _unfinished(case_dir(cid) / "out"):      # 上一轮没完成：文字作为说明，接着做这一轮
                store.chat_add(cid, "user", u["display"], text)
                runner.start(cid, u["display"], instruction=text)
            else:
                runner.start(cid, u["display"], message=text)
        except (RuntimeError,cr.RevisionError) as err:
            raise HTTPException(409, str(err)) from None
        return back(cid)

    @app.post('/cases/{cid}/commission-feedback')
    async def commission_feedback(cid: str, request: Request, u: dict = Depends(user)):
        if not {'editor','approver'}.intersection(u['roles']):raise HTTPException(403,'需要财务经办或审批权限')
        d=case_dir(cid);idle(cid);f=await posted(request,u)
        kind=str(f.get('kind','discrepancy'))
        meta=store.meta(cid)
        rule_store=registry.data_dir(meta['scenario'])/'commission_rules'/'versions.json' if meta.get('scenario') else None
        try:
            revision=cr.submit(d/'out',by=u['display'],kind=kind,stage=str(f.get('stage','unknown')),
                owner=str(f.get('owner','')),description=str(f.get('description',''))[:20000],
                project_keys=str(f.get('project_keys','')).splitlines(),observed=str(f.get('observed',''))[:2000],
                evidence=str(f.get('evidence',''))[:4000],field=str(f.get('field','')),value=str(f.get('value',''))[:200],
                scope=str(f.get('scope','this_case')),effective_from=str(f.get('effective_from','')),
                effective_to=str(f.get('effective_to','')),role=str(f.get('role','')),shares=str(f.get('shares',''))[:8000],
                rule_store=rule_store,approver='approver' in u['roles'],base_version=str(f.get('base_version','')))
        except (cr.RevisionError,cs.CaseBusyError) as err:return back(cid,f'未登记：{err}')
        store.audit(cid,u['display'],'登记财务修订',revision=revision['id'],kind=kind,affected=revision['affected'])
        if revision['status']=='SCHEDULED':return back(cid,'已登记规则生效版本；不改变不在适用范围内的本期结果')
        store.chat_add(cid,'user',u['display'],'财务修订：'+str(f.get('description','')))
        if str(f.get('run','0'))=='1':
            try:runner.start(cid,u['display'])
            except RuntimeError as err:return back(cid,f'反馈已保存，暂不能启动：{err}')
            return back(cid,'已保存财务反馈，开始按责任节点核验；历史结果不变')
        return back(cid,'已登记修订，旧版交付停止；点击运行下一轮执行')

    @app.post('/cases/{cid}/confirm-totals')
    async def confirm_totals(cid: str, request: Request, u: dict = Depends(user)):
        need(u, 'approver')
        d = case_dir(cid)
        idle(cid)
        f = await posted(request, u)
        try:
            result = cs.confirm_totals(d / 'out', str(f.get('scheme', '')), str(f.get('digest', '')), u['display'])
        except cs.ConfirmError as err:
            store.audit(cid, u['display'], '总额确认被拒绝', reason=str(err))
            return back(cid, f'不能确认：{err}')
        if result.get('already_confirmed'):
            return back(cid, '本版总额已确认，请查看当前核算进度')
        store.audit(cid, u['display'], '确认核算总额', scheme=result['scheme'], digest=result['digest'])
        runner.start(cid, u['display'])
        return back(cid, '总额已确认，继续计算人员金额')

    @app.post("/cases/{cid}/confirm")
    async def confirm(cid: str, request: Request, u: dict = Depends(user)):
        need(u, "approver")
        d = case_dir(cid)
        idle(cid)
        f = await posted(request, u)
        note = str(f.get("note", "")).strip()[:500]
        save = str(f.get("save", "template")) != "result"
        picked = [str(x) for x in f.getlist("revision")] if hasattr(f, "getlist") else []
        try:   # 页面上没勾选就是都不录入：选择已经摆在表单里了
            tpl = cs.confirm(d / "out", by=u["display"], note=note, save_template=save, revisions=picked)
        except (cs.CaseBusyError, cs.ConfirmError) as err:
            store.audit(cid, u["display"], "确认被拒绝", reason=str(err))
            return back(cid, f"不能确认：{err}")
        store.audit(cid, u["display"], "确认结果", round=cs.load_case(d / "out")["confirmed"]["round"], note=note,
                    template=str(tpl.relative_to(d)) if tpl else "未录入", revisions=picked if tpl else [])
        return back(cid, "已确认并录入模板" if tpl else "已确认结果，未录入模板")

    @app.post('/cases/{cid}/reexport')
    async def reexport(cid: str, request: Request, u: dict = Depends(user)):
        if not {'editor','approver'}.intersection(u['roles']):raise HTTPException(403,'需要财务核算或审批权限')
        d=case_dir(cid);idle(cid);await posted(request,u)
        from bonus_agent.commission_report import reexport_fixed_case
        try:workbook,meta=reexport_fixed_case(d/'out',u['display'])
        except (cs.ConfirmError,cs.CaseBusyError,ValueError,OSError) as exc:return back(cid,f'重新导出失败：{exc}')
        store.audit(cid,u['display'],'仅报表重新导出',report=meta['id'],source_round=meta['source_round'],sha256=meta['workbook_sha256'])
        return back(cid,'已生成新版报表，原项目、角色及人员金额完全不变；在「新版报表」下载。')

    @app.post("/cases/{cid}/template")
    async def template(cid: str, request: Request, u: dict = Depends(user)):
        from bonus_core.template import Template
        need(u, "editor")
        d = case_dir(cid)
        run_dir = d / "runs" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2))
        (run_dir / "data").mkdir(parents=True)
        f = await posted(request, u)
        saved = []
        for x in [x for x in f.getlist("files") if getattr(x, "filename", "")]:
            try:
                name = safe_name(x.filename)
            except ValueError as err:
                shutil.rmtree(run_dir)
                raise HTTPException(400, str(err)) from None
            data = await x.read(MAX_UPLOAD + 1)
            if len(data) > MAX_UPLOAD:
                shutil.rmtree(run_dir)
                raise HTTPException(413, f"{name} 超过 {MAX_UPLOAD // 1024 // 1024} MB")
            (run_dir / "data" / name).write_bytes(data)
            saved.append((name, hashlib.sha256(data).hexdigest()))
        if not saved:
            shutil.rmtree(run_dir)
            return back(cid, "没有选择数据文件")
        try:
            tpl = Template.load(d / "out" / "template")
            results, _ = tpl.run([run_dir / "data" / n for n, _ in saved])
            written = tpl.write(results, run_dir)
        except Exception as err:  # 模板被改、数据缺列、校验不通过等，如实显示
            (run_dir / "错误.txt").write_text(str(err), encoding="utf-8")
            store.audit(cid, u["display"], "按模板计算失败", run=run_dir.name, error=str(err)[:500])
            return back(cid, f"按模板计算失败：{err}")
        store.audit(cid, u["display"], "按模板计算", run=run_dir.name, data=dict(saved),
                    pools={f"{n}/{r}": res.pool for _, (n, r, res) in written})
        return back(cid, f"已算出 {len(written)} 个结果，见“按模板计算”的 {run_dir.name}")

    @app.post("/cases/{cid}/period")
    async def set_period(cid: str, request: Request, u: dict = Depends(user)):
        need(u, "editor")
        case_dir(cid)
        idle(cid)
        f = await posted(request, u)
        try:
            period = norm_period(f.get("period"))
        except ValueError as err:
            return back(cid, str(err))
        m = store.meta(cid)
        if period!=m.get('period'):
            out=case_dir(cid)/'out'
            try:
                with cs.case_lock(out):
                    obj=cs.load_case(out)
                    if cr.state_of(obj):
                        from bonus_agent.commission_chain import one_month
                        one_month(period)
                        cr.submit_locked(out,obj,by=u['display'],kind='material_correction',description=f"核算期间由 {m.get('period')} 改为 {period}")
            except (ValueError,cs.CaseBusyError) as err:return back(cid,f'不能修改本案卷期间：{err}')
        if period:
            m["period"] = period
        else:
            m.pop("period", None)
        store.save_meta(cid, m)
        store.audit(cid, u["display"], "修改核算期间", period=period or "不指定")
        return back(cid, f"核算期间改为 {period or '不指定'}，下一轮起生效")

    @app.post("/cases/{cid}/publish")
    async def publish(cid: str, request: Request, u: dict = Depends(user)):
        need(u, "approver")
        d = case_dir(cid)
        idle(cid)
        f = await posted(request, u)
        m, case = store.meta(cid), cs.load_case(d / "out") or {}
        conf = case.get("confirmed")
        if not m.get("scenario") or not conf:
            return back(cid, "只有属于某个场景、已经确认的案卷才能发布")
        if conf.get("template", "") is None:
            return back(cid, "最近一次确认只确认了结果、没有录入模板，不能发布")
        if any(x.get("confirmed_at") == conf.get("at") for x in m.get("published") or []):
            return back(cid, "这次确认的结果已经发布过了")
        note = str(f.get("note", "")).strip()[:300]
        try:
            rel = rl.publish(registry, m["scenario"], d / "out" / "template", u["display"], note,
                             source=f"案卷「{m['name']}」（{cid}）第 {conf['round']} 轮", questions=load_questions(d / "out"),
                             repairs=(case.get("workspace") or {}).get("repairs"))
        except ScenarioError as err:
            store.audit(cid, u["display"], "发布被拒绝", reason=str(err)[:500])
            return back(cid, f"不能发布：{err}")
        m.setdefault("published", []).append({"version": rel.version, "by": u["display"], "at": now(), "confirmed_at": conf.get("at")})
        store.save_meta(cid, m)
        store.audit(cid, u["display"], "发布规则版本", scenario=m["scenario"], version=rel.version, note=note)
        return back(cid, f"已发布为规则版本 {rel.version}")

    @app.post("/cases/{cid}/compute")
    async def compute(cid: str, request: Request, u: dict = Depends(user)):
        need(u, "editor")
        d = case_dir(cid)
        m = store.meta(cid)
        if m.get("mode") != "compute":
            raise HTTPException(400, "只有计算模式的案卷能这样算")
        f = await posted(request, u)
        run_dir = d / "runs" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2))
        saved = await _save_run(run_dir, f)
        if not saved:
            return back(cid, "没有选择数据文件")
        version = str(f.get("version", "")).strip() or None
        try:
            period = norm_period(f.get("period"))
            c = rl.compute(registry, m["scenario"], [run_dir / "data" / n for n, _ in saved], run_dir, version, period)
        except Exception as err:   # 输入不符合模板、版本被改、数据缺列等，如实显示
            (run_dir / "错误.txt").write_text(str(err), encoding="utf-8")
            store.audit(cid, u["display"], "计算失败", version=version or "", run=run_dir.name, error=str(err)[:500])
            return back(cid, f"没有算出结果：{str(err).splitlines()[0]}")
        store.audit(cid, u["display"], "按规则版本计算", version=c.release.version, period=str(c.period or ""), run=run_dir.name, data=dict(saved),
                    pools={f"{n}/{r}": res.pool for n, revs in c.results.items() for r, res in revs.items()})
        return back(cid, f"已按 {c.release.version} 算出 {len(c.written)} 个结果")

    async def _save_run(run_dir: Path, f) -> list[tuple[str, str]]:
        (run_dir / "data").mkdir(parents=True)
        saved = []
        for x in [x for x in f.getlist("files") if getattr(x, "filename", "")]:
            try:
                name = safe_name(x.filename)
            except ValueError as err:
                shutil.rmtree(run_dir)
                raise HTTPException(400, str(err)) from None
            data = await x.read(MAX_UPLOAD + 1)
            if len(data) > MAX_UPLOAD:
                shutil.rmtree(run_dir)
                raise HTTPException(413, f"{name} 超过 {MAX_UPLOAD // 1024 // 1024} MB")
            (run_dir / "data" / name).write_bytes(data)
            saved.append((name, hashlib.sha256(data).hexdigest()))
        if not saved:
            shutil.rmtree(run_dir)
        return saved

    @app.get("/scenarios/{sid}", response_class=HTMLResponse)
    def scenario_page(sid: str, u: dict = Depends(user)):
        from bonus_agent.scenarios.inputs import template_text
        try:
            sc = registry.get(sid)
            guide = sc.guide_text()
        except ScenarioError as err:
            raise HTTPException(404, str(err)) from None
        rels = registry.releases(sid)
        vrows = "".join(
            f"<tr><td>{e(r.version)}</td><td>{e(r.info.get('published_by', ''))}</td><td class='mut'>{e(str(r.info.get('published_at', ''))[:16])}</td>"
            f"<td>{e(r.info.get('note', ''))}</td><td class='mut'>{e(r.info.get('source', ''))}</td>"
            f"<td class='mut'>{e('；'.join(('通过 ' if a['ok'] else '未通过 ') + a['id'] for a in r.info.get('acceptance') or []) or '无验收用例')}</td></tr>"
            for r in reversed(rels))
        mem = registry.memory(sid).items()
        approver = "approver" in u["roles"]
        mrows = "".join(f"<div class='q'>" + (f"<label><input type='checkbox' name='del' value='{e(x['id'])}'> 删除</label> " if approver else "")
                        + f"<b>问：</b>{e(x['question'])}<div><b>答：</b>{e(x['answer'])} "
                        f"<span class='mut'>（{e(x.get('version', ''))}，{e(x.get('by', ''))}）</span></div></div>" for x in mem)
        if mem and approver:
            mrows = form(f"/scenarios/{sid}/memory/delete", mrows + "<p><button>删除选中的记忆</button></p>", u["csrf"])
        body = (f"<h1>{e(sc.name)}</h1><section><p>{e(sc.description)}</p><p class='mut'>场景包：{e(sc.dir)}</p>"
                f"<p class='mut'>规则版本和记忆：{e(registry.data_dir(sid))}</p></section>"
                f"<section><h2>场景说明</h2>" + (f"<pre>{e(guide)}</pre>" if guide else "<p class='mut'>还没有场景说明（guide.md）</p>") + "</section>"
                f"<section><h2>输入模板</h2>" + (f"<pre>{e(template_text(sc))}</pre>" if sc.inputs else "<p class='mut'>还没有输入模板，计算前不做格式检查</p>") + "</section>"
                f"<section><h2>规则版本</h2>" + (f"<div class='wrap'><table><tr><th>版本</th><th>发布人</th><th>时间</th><th>说明</th><th>来源</th><th>验收</th></tr>{vrows}</table></div>"
                                              if rels else "<p class='mut'>还没有发布</p>") + "</section>"
                f"<section><h2>场景记忆</h2><p class='mut'>发布规则版本时记下的、财务确认过的口径，编制时交给智能体参考。</p>"
                + (mrows or "<p class='mut'>还没有</p>") + "</section>")
        return page(sc.name, body, u)

    @app.post("/scenarios/{sid}/memory/delete")
    async def memory_delete(sid: str, request: Request, u: dict = Depends(user)):
        need(u, "approver")
        f = await posted(request, u)
        try:
            registry.get(sid)
        except ScenarioError as err:
            raise HTTPException(404, str(err)) from None
        registry.memory(sid).remove([str(x) for x in f.getlist("del")])
        return RedirectResponse(f"/scenarios/{sid}", status_code=303)

    @app.get("/cases/{cid}/dl/{area}/{rel:path}")
    def download(cid: str, area: str, rel: str, u: dict = Depends(user)):
        d = case_dir(cid)
        if area not in ("out", "materials", "expected", "runs"):
            raise HTTPException(404)
        try:
            p = inside(d / area, rel)
        except KeyError:
            raise HTTPException(404, "没有这个文件") from None
        if p.name == cs.CASE_FILE:
            raise HTTPException(404)
        download_name=p.name
        if area=='out' and p.name=='result.xlsx' and p.is_relative_to((d/'out'/'report_versions').resolve()):
            from bonus_agent.commission_report import read_report
            try:read_report(p)
            except (cs.ConfirmError,ValueError,OSError) as err:raise HTTPException(409,str(err)) from None
            download_name='新版核算底稿.xlsx'
        if area=='out' and p.parent==d/'out' and p.name in cr.CURRENT_FINANCIAL_FILES:
            obj=cs.load_case(d/'out')
            if cr.state_of(obj):
                try:
                    cr.validate_current(d/'out',obj,final=p.name in ('result.xlsx','engine_results.json','results.json','commission_details.json','expected_check.txt'))
                    rnd=obj['rounds'][-1]
                    if p.name!='expected_check.txt' and not any(name==p.name and cs.sha256(p)==digest for name,digest in rnd.get('outputs',{}).values()):
                        raise cs.ConfirmError('该文件不属于当前核验轮次。')
                except cs.ConfirmError as err:raise HTTPException(409,str(err)+'；原文件请从历次存档查看。') from None
        store.audit(cid, u["display"], "下载", file=f"{area}/{rel}")
        return FileResponse(p, filename=download_name)

    return app


def serve(root: str, host: str = "127.0.0.1", port: int = 8080, model: str | None = None, checker: str | None = None,
          **kw) -> None:
    import uvicorn

    from bonus_agent.llm import LLM, load_config
    cfg = load_config()
    llm = LLM.from_config(model, cfg, checker)       # 启动时就核对模型配置
    name = llm.name + (f"（交叉验证：{llm.checker_llm.name}）" if llm.checker_llm else "")
    users = Store(Path(root)).users.load()
    if not users:
        raise SystemExit(f"还没有用户，先运行：bonus-agent user add --data {root} 用户名 --roles editor,approver")
    print(f"数据目录：{Path(root).resolve()}；用户：{'、'.join(users)}；模型：{name}；浏览器打开 http://{host}:{port}",
          flush=True)
    app = create_app(root, lambda: LLM.from_config(model, cfg, checker), model_name=name, **kw)
    uvicorn.run(app, host=host, port=port)
