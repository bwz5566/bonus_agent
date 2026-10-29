"""内网网页服务：财务在网页上看问题、填答复、传补充材料，提交后自动跑下一轮；确认结果、按模板计算、下载结果。

私有化部署在内网，默认只监听本机（127.0.0.1），对外请放在 HTTPS 反向代理后面。

数据目录：
  users.yaml                      用户（口令只存 PBKDF2 哈希）和角色
  secret.key                      表单防伪的密钥（首次启动自动生成）
  cases/<编号>/meta.json          案卷名称、创建人
  cases/<编号>/materials/         本笔奖金的材料（规则文档、Excel 数据）
  cases/<编号>/expected/          期望结果（可选，只用来事后核对，不交给智能体）
  cases/<编号>/out/               案卷：case.json、questions.yaml、每轮结果和 history/、确认后的 template/
  cases/<编号>/runs/<时间>/        按模板计算的数据和结果
  cases/<编号>/audit.jsonl        操作记录：谁、什么时候、做了什么（含下载）

角色：editor 传材料、答复问题、运行下一轮、按模板计算；approver 确认结果。所有登录用户都能查看。
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
from typing import Callable
from urllib.parse import quote

import yaml

from bonus_agent import case as cs
from bonus_agent.parsing.docs import PARSERS

ROLES = {"editor": "传材料、答复问题、运行、按模板计算", "approver": "确认结果"}
COOKIE = "bonus_session"
SESSION_SECONDS = 8 * 3600       # 登录 8 小时后要重新登录
MAX_FAILURES, LOCK_SECONDS = 5, 300   # 同一用户名 5 分钟内输错 5 次，暂停登录
ROLE_NAMES = {"editor": "经办", "approver": "审批"}
DETAIL_NAMES = {"name": "名称", "file": "文件", "new": "从头开始", "round": "轮次", "status": "状态", "error": "错误",
                "question": "问题", "answer": "答复", "note": "说明", "reason": "原因", "run": "批次", "pools": "奖金池",
                "data": "数据", "sha256": "sha256", "size": "字节", "template": "模板"}
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

    def add(self, name: str, password: str, roles: list[str], display: str = "") -> None:
        bad = [r for r in roles if r not in ROLES]
        if bad:
            raise ValueError(f"没有角色 {bad}，可选：{list(ROLES)}")
        if not re.fullmatch(r"[\w.\-@]{1,64}", name):
            raise ValueError("用户名只能用字母（含中文）、数字、下划线、点、减号、@")
        if len(password) < 8:
            raise ValueError("口令至少 8 位")
        users = self.load()
        users[name] = {"password": hash_password(password), "roles": roles, "display": display or name}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(users, allow_unicode=True, sort_keys=True), encoding="utf-8")
        self.path.chmod(0o600)

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

    def create(self, name: str, by: str) -> str:
        cid = secrets.token_hex(6)
        d = self.root / "cases" / cid
        for sub in ("materials", "expected", "out", "runs"):
            (d / sub).mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"name": name, "created": now(), "created_by": by}, ensure_ascii=False),
                                     encoding="utf-8")
        self.audit(cid, by, "新建案卷", name=name)
        return cid

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


class Runner:
    """每个案卷同时只跑一轮；全局最多同时跑 max_parallel 轮（大模型调用量大）。"""

    def __init__(self, store: Store, llm_factory: Callable[[], object], max_parallel: int = 2, max_steps: int = 80,
                 max_tokens: int = 2_000_000):
        self.store, self.llm_factory = store, llm_factory
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.slots = threading.Semaphore(max_parallel)
        self.max_steps, self.max_tokens = max_steps, max_tokens

    def running(self, cid: str) -> bool:
        j = self.jobs.get(cid)
        return j is not None and j.status == "running"

    def start(self, cid: str, user: str, new: bool = False, message: str | None = None) -> Job:
        with self.lock:
            if self.running(cid):
                raise RuntimeError("这个案卷正在运行，请等本轮结束")
            job = self.jobs[cid] = Job(cid, user)
        if message:
            self.store.chat_add(cid, "user", user, message)
            self.store.audit(cid, user, "对话", text=message[:200])
        else:
            self.store.audit(cid, user, "运行下一轮", new=new)
        job.thread = threading.Thread(target=self._run, args=(job, new, message), daemon=True)
        job.thread.start()
        return job

    def _run(self, job: Job, new: bool, message: str | None = None) -> None:
        from bonus_agent.agent import run_agent
        d = self.store.dir(job.cid)
        try:
            with self.slots:
                llm = self.llm_factory()
                ar = run_agent(llm, [d / "materials"], d / "out", resume=not new, max_steps=self.max_steps,
                               max_tokens=self.max_tokens, message=message, by=job.by,
                               chat=self.store.chat(job.cid)[:-1] if message else None,
                               on_step=lambda n, s: job.steps.append(f"[{n:02d}] {s.tool} {'✓' if s.ok else '✗'} "
                                                                      f"{(s.result.splitlines() or [''])[0][:160]}"))
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
            job.message = {"OK": "已出结果，请核对", "NEEDS_CONFIRMATION": f"有 {len(ar.pending)} 个问题等答复，本轮不出结果"
                           }.get(ar.status, f"未完成：{ar.error}")
            if message:
                made = bool(ar.outputs)          # 对话里改了东西才会生成新的一轮
                note = (f"\n\n（已按修改生成第 {ar.round} 轮：{job.message}）" if made else "")
                self.store.chat_add(job.cid, "agent", "智能体", (ar.reply or f"（没有回复：{ar.error or '未完成'}）") + note)
                if not made:
                    job.result, job.message = "", "已回复"
            if ar.outputs or not message:
                self.store.audit(job.cid, job.by, "本轮结束", round=ar.round, status=ar.status, error=ar.error)
        except Exception as e:  # 失败要如实显示，不能当作完成
            job.status, job.message = "failed", f"运行失败：{e}"
            if message:
                self.store.chat_add(job.cid, "agent", "智能体", f"（运行失败：{e}）")
            self.store.audit(job.cid, job.by, "本轮失败", error=str(e))


# ---------------- 问题答复 ----------------
def load_questions(out: Path) -> list[dict]:
    p = out / "questions.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or []) if p.exists() else []


def save_answers(out: Path, form: dict[str, str], user: str) -> list[str]:
    """把网页表单的答复写进 questions.yaml（记录答复人和时间）。返回本次答复的问题编号。"""
    qs = load_questions(out)
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
    if done:
        (out / "questions.yaml").write_text(QUESTIONS_HEAD + yaml.safe_dump(qs, allow_unicode=True, sort_keys=False),
                                            encoding="utf-8")
    return done


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
.q{border-top:1px solid var(--line);padding:10px 0}.q:first-of-type{border-top:0}.opt{display:block;margin:2px 0}
pre{white-space:pre-wrap;background:var(--bg);padding:8px;border-radius:6px;max-height:320px;overflow:auto;margin:0}
.wrap{overflow-x:auto}.chat{max-height:480px;overflow-y:auto;margin-bottom:10px}
.msg{margin:8px 0;display:flex;flex-direction:column}.msg.user{align-items:flex-end}
.bubble{white-space:pre-wrap;padding:8px 12px;border-radius:10px;max-width:85%;background:var(--bg);border:1px solid var(--line)}
.msg.user .bubble{background:var(--acc);color:#fff;border-color:var(--acc)}
"""
STATUS = {"OK": ("已出结果，待财务核对", "ok"), "NEEDS_CONFIRMATION": ("等财务答复，本轮不出结果", "warn"),
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
    label, cls = STATUS.get(r.get("status"), (r.get("status"), "mut"))
    conf = " · <span class='ok'>已确认</span>" if r.get("confirmed") else ""
    return f"第 {r['n']} 轮：<span class='{cls}'>{e(label)}</span>{conf}"


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


def create_app(root: str | Path, llm_factory: Callable[[], object], *, max_parallel: int = 2, model_name: str = "",
               max_steps: int = 80, max_tokens: int = 2_000_000):
    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

    store = Store(Path(root))
    runner = Runner(store, llm_factory, max_parallel, max_steps, max_tokens)
    app = FastAPI(title="奖金计算", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.runner = store, runner
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

    @app.get("/", response_class=HTMLResponse)
    def index(u: dict = Depends(user)):
        rows = "".join(f"<tr><td><a href='/cases/{cid}'>{e(m['name'])}</a></td><td>{status_text(c)}</td>"
                       f"<td class='mut'>{e(m['created_by'])} · {e(m['created'][:16])}</td></tr>" for cid, m, c in store.cases())
        new = form("/cases", "<input type='text' name='name' placeholder='如 2026 年 Q3 季度奖' required> "
                             "<p><button>新建案卷</button></p>", u["csrf"]) if "editor" in u["roles"] else ""
        body = (f"<h1>案卷</h1><section><div class='wrap'><table><tr><th>名称</th><th>状态</th><th>创建</th></tr>{rows}</table></div>"
                + ("" if rows else "<p class='mut'>还没有案卷</p>") + "</section>"
                + (f"<section><h2>新建</h2>{new}</section>" if new else ""))
        return page("案卷", body, u)

    @app.post("/cases")
    async def create(request: Request, u: dict = Depends(user)):
        need(u, "editor")
        f = await posted(request, u)
        name = str(f.get("name", "")).strip()[:100]
        if not name:
            raise HTTPException(400, "请填写名称")
        return back(store.create(name, u["display"]))

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
        last = (case or {}).get("rounds", [{}])[-1] if case else {}
        info = f"<p>{status_text(case)}</p>"
        if last.get("summary"):
            info += f"<p class='mut'>方案概述：{e(last['summary'])}</p>"
        if last.get("error"):
            info += f"<p class='bad'>{e(last['error'])}</p>"
        if model_name:
            info += f"<p class='mut'>模型：{e(model_name)}</p>"
        check = out / "expected_check.txt"
        if check.exists() and not busy:
            info += f"<p class='mut'>与期望结果核对：</p><pre>{e(check.read_text(encoding='utf-8'))}</pre>"
        if job:
            cls = "warn" if job.status == "running" else "bad" if job.status == "failed" else STATUS.get(job.result, ("", "mut"))[1]
            head = "正在运行……" if job.status == "running" else job.message
            info += (f"<p class='{cls}'>{e(head)}（{e(job.by)}，{e(job.started[11:16])} 开始）</p>"
                     + (f"<pre>{e(chr(10).join(job.steps[-15:]))}</pre>" if job.steps else ""))
        if editor and not busy:
            info += form(f"/cases/{cid}/run", "<p><button>运行下一轮</button> <label class='mut'><input type='checkbox' "
                                              "name='new' value='1'> 从头开始（不接着上一轮）</label></p>", csrf)
        parts.append(f"<section><h2>状态</h2>{info}</section>")
        msgs = "".join(f"<div class='msg {m['role']}'><div class='mut'>{e(m['user'])} · {e(m['at'][5:16].replace('T', ' '))}</div>"
                       f"<div class='bubble'>{e(m['text'])}</div></div>" for m in store.chat(cid)[-40:])
        if busy and job and job.steps:
            msgs += "<div class='msg agent'><div class='bubble mut'>正在处理……</div></div>"
        box = form(f"/cases/{cid}/chat", "<textarea name='text' rows='3' placeholder='直接问结果（如：E001 的季度奖怎么算的？），"
                                         "或者让智能体修改（如：华东区域系数改为 1.05）' required></textarea>"
                                         "<p><button>发送</button> <span class='mut'>改了规则会自动重新试算，生成新的一轮；"
                                         "只是提问不会生成新的一轮</span></p>", csrf) if editor and not busy else ""
        parts.append(f"<section><h2>对话</h2><div class='chat'>{msgs or '<p class=mut>还没有对话</p>'}</div>{box}</section>")
        # 问题
        qs = load_questions(out)
        if qs:
            items = []
            for q in qs:
                state = (f"<span class='mut'>已撤回：{e(q['withdrawn'])}</span>" if q.get("withdrawn") else
                         f"<span class='ok'>已答复：{e(q['answer'])}</span>" + (f"<span class='mut'>（{e(q.get('answered_by'))} "
                                                                             f"{e(str(q.get('answered_at', ''))[:16])}）</span>"
                                                                             if q.get("answered_by") else "")
                         if q.get("answer") not in (None, "") else
                         "<span class='warn'>等待答复</span>" if q.get("blocking", True) else "<span class='mut'>提示</span>")
                opts = ""
                if editor and not q.get("withdrawn"):
                    opts = "".join(f"<label class='opt'><input type='radio' name='ans_{e(q['id'])}' value='{i}'> {e(o)}</label>"
                                   for i, o in enumerate(q.get("options") or []))
                    opts += (f"<label class='opt'><input type='radio' name='ans_{e(q['id'])}' value='{OTHER}'> 其他（在下面写明）</label>"
                             f"<input type='text' name='text_{e(q['id'])}' placeholder='补充说明或其他答复'>")
                sug = q.get("suggested")
                hint = (f"<div class='mut'>智能体建议（仅供参考，不会被采用）：{e(q['options'][sug])}</div>"
                        if isinstance(sug, int) and 0 <= sug < len(q.get("options") or []) else "")
                items.append(f"<div class='q'><b>{e(q['id'])}</b> <span class='mut'>第 {e(q.get('round', 1))} 轮</span> {state}"
                             f"<div>{e(q['text'])}</div>{hint}{opts}</div>")
            body = "".join(items)
            if editor and not busy:
                body = form(f"/cases/{cid}/answers", body + "<p><button>保存答复</button> "
                                                           "<span class='mut'>保存后点“运行下一轮”</span></p>", csrf)
            parts.append(f"<section><h2>问题</h2>{body}</section>")
        # 材料
        up = form(f"/cases/{cid}/materials", "<p><input type='file' name='files' multiple> <button>上传</button></p>",
                  csrf, upload=True) if editor and not busy else ""
        parts.append(f"<section><h2>材料</h2>{files_table(d / 'materials', f'/cases/{cid}/dl/materials', csrf, f'/cases/{cid}/materials/delete' if editor and not busy else None)}{up}"
                     "<p class='mut'>规则文档（Word、PPT、图片）和 Excel 数据都放这里。补充材料、修改后的数据直接上传（同名覆盖），下一轮会识别出新增、修改、删除的文件。</p></section>")
        exp = form(f"/cases/{cid}/expected", "<p><input type='file' name='files'> <button>上传期望结果</button></p>",
                   csrf, upload=True) if editor and not busy else ""
        parts.append(f"<section><h2>期望结果（可选）</h2>{files_table(d / 'expected', f'/cases/{cid}/dl/expected', csrf, f'/cases/{cid}/expected/delete' if editor and not busy else None)}{exp}"
                     "<p class='mut'>只在算完后逐个对象核对（写入 expected_check.txt），不会交给智能体。</p></section>")
        # 结果
        top = [p for p in sorted(out.glob("*")) if p.is_file() and p.name != cs.CASE_FILE]
        res = "".join(f"<li><a href='/cases/{cid}/dl/out/{e(p.name)}'>{e(p.name)}</a></li>" for p in top)
        hist = sorted((out / "history").glob("*")) if (out / "history").exists() else []
        res = (f"<ul>{res}</ul>" if res else "<p class='mut'>无</p>") + "".join(
            f"<details><summary>{e(h.name)}</summary>{files_table(h, f'/cases/{cid}/dl/out/history/{h.name}')}</details>" for h in hist)
        parts.append(f"<section><h2>本轮输出</h2>{res}</section>")
        # 确认
        conf = (case or {}).get("confirmed")
        cbody = (f"<p class='ok'>已确认：第 {e(conf['round'])} 轮，{e(conf['by'])}，{e(conf['at'][:16])}"
                 + (f"，{e(conf['note'])}" if conf.get("note") else "") + "</p>") if conf else "<p class='mut'>尚未确认</p>"
        if approver and not busy and last.get("status") == "OK" and not last.get("confirmed"):
            cbody += form(f"/cases/{cid}/confirm", "<p><input type='text' name='note' placeholder='确认说明（可选）'></p>"
                                                   f"<p><button>确认第 {e(last['n'])} 轮结果并冻结为模板</button></p>", csrf)
        cbody += "<p class='mut'>确认前会核对输出文件没被改过，并按模板复算、与该轮结果逐分一致。确认后每期换数据直接按模板计算，不再调用大模型。</p>"
        parts.append(f"<section><h2>确认</h2>{cbody}</section>")
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
        d = case_dir(cid)
        idle(cid)
        f = await posted(request, u)
        files = [x for x in f.getlist("files") if getattr(x, "filename", "")]
        if not files:
            raise HTTPException(400, "没有选择文件")
        if single:
            files = files[:1]
            for old in (d / area).glob("*"):
                old.unlink()
        saved = []
        for x in files:
            try:
                name = safe_name(x.filename)
            except ValueError as err:
                raise HTTPException(400, str(err)) from None
            data = await x.read(MAX_UPLOAD + 1)
            if len(data) > MAX_UPLOAD:
                raise HTTPException(413, f"{name} 超过 {MAX_UPLOAD // 1024 // 1024} MB")
            (d / area / name).write_bytes(data)
            saved.append(name)
            store.audit(cid, u["display"], "上传" + ("期望结果" if area == "expected" else "材料"), file=name,
                        sha256=hashlib.sha256(data).hexdigest(), size=len(data))
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
        p.unlink()
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
        need(u, "editor")
        d = case_dir(cid)
        idle(cid)
        f = await posted(request, u)
        done = save_answers(d / "out", {k: str(v) for k, v in f.items()}, u["display"])
        for qid in done:
            q = next(q for q in load_questions(d / "out") if q["id"] == qid)
            store.audit(cid, u["display"], "答复问题", question=qid, answer=q["answer"])
        return back(cid, f"已保存 {len(done)} 个答复，点“运行下一轮”继续" if done else "没有新的答复")

    @app.post("/cases/{cid}/run")
    async def run(cid: str, request: Request, u: dict = Depends(user)):
        need(u, "editor")
        d = case_dir(cid)
        f = await posted(request, u)
        if not any((d / "materials").iterdir()):
            return back(cid, "还没有材料，请先上传")
        try:
            runner.start(cid, u["display"], new=f.get("new") == "1")
        except RuntimeError as err:
            raise HTTPException(409, str(err)) from None
        return back(cid)

    @app.post("/cases/{cid}/chat")
    async def chat(cid: str, request: Request, u: dict = Depends(user)):
        need(u, "editor")
        case_dir(cid)
        f = await posted(request, u)
        text = str(f.get("text", "")).strip()[:4000]
        if not text:
            return back(cid)
        try:
            runner.start(cid, u["display"], message=text)
        except RuntimeError as err:
            raise HTTPException(409, str(err)) from None
        return back(cid)

    @app.post("/cases/{cid}/confirm")
    async def confirm(cid: str, request: Request, u: dict = Depends(user)):
        need(u, "approver")
        d = case_dir(cid)
        idle(cid)
        f = await posted(request, u)
        note = str(f.get("note", "")).strip()[:500]
        try:
            tpl = cs.confirm(d / "out", by=u["display"], note=note)
        except cs.ConfirmError as err:
            store.audit(cid, u["display"], "确认被拒绝", reason=str(err))
            return back(cid, f"不能确认：{err}")
        store.audit(cid, u["display"], "确认结果", round=cs.load_case(d / "out")["confirmed"]["round"], note=note,
                    template=str(tpl.relative_to(d)))
        return back(cid, "已确认并冻结为模板")

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
        store.audit(cid, u["display"], "下载", file=f"{area}/{rel}")
        return FileResponse(p, filename=p.name)

    return app


def serve(root: str, host: str = "127.0.0.1", port: int = 8080, model: str | None = None, **kw) -> None:
    import uvicorn

    from bonus_agent.llm import LLM, load_config
    cfg = load_config()
    name = LLM.from_config(model, cfg).name          # 启动时就核对模型配置
    users = Store(Path(root)).users.load()
    if not users:
        raise SystemExit(f"还没有用户，先运行：bonus-agent user add --data {root} 用户名 --roles editor,approver")
    print(f"数据目录：{Path(root).resolve()}；用户：{'、'.join(users)}；模型：{name}；浏览器打开 http://{host}:{port}",
          flush=True)
    app = create_app(root, lambda: LLM.from_config(model, cfg), model_name=name, **kw)
    uvicorn.run(app, host=host, port=port)
