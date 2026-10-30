"""内网网页服务：登录、角色、防伪码、上传、答复、多轮运行、确认、按模板计算、下载和操作记录。"""
import json
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from bonus_agent.llm import FakeLLM  # noqa: E402
from bonus_agent.web import create_app, safe_name  # noqa: E402
from tests.test_conflicts import CLAUSES, DATASET, F5, F6, _materials, rules  # noqa: E402

PW = "password-123"


@pytest.fixture
def env(tmp_path):
    scripts = []
    app = create_app(tmp_path / "data", lambda: FakeLLM(scripts.pop(0)))
    store = app.state.store
    store.users.add("alice", PW, ["editor"], "张会计")
    store.users.add("bob", PW, ["approver"], "李经理")
    store.users.add("carol", PW, [])
    c = TestClient(app)
    c.sids = {}
    return app, store, scripts, c, _materials(tmp_path / "m")


def login(c, name, pw=PW):
    c.cookies.clear()
    return c.post("/login", data={"username": name, "password": pw}, follow_redirects=False)


def as_(c, name):
    """切换到某个用户（每个用户只登录一次，之后换回它的会话号）。"""
    if name not in c.sids:
        assert login(c, name).status_code == 303
        c.sids[name] = c.cookies.get("bonus_session")
    c.cookies.set("bonus_session", c.sids[name])
    return c


def post(c, store, url, data=None, files=None, user="alice"):
    as_(c, user)
    return c.post(url, data={"csrf": store.csrf(c.sids[user]), **(data or {})}, files=files, follow_redirects=False)


def wait(app, cid):
    app.state.runner.jobs[cid].thread.join(30)
    return app.state.runner.jobs[cid]


def test_web_rounds_confirm_and_template(env):
    app, store, scripts, c, m = env
    r = c.get("/cases/x", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/cases/x"     # 没登录先去登录页
    r = login(c, "alice", "wrong-password")
    assert "msg=" in r.headers["location"] and not c.cookies.get("bonus_session")
    assert as_(c, "carol").get("/").status_code == 200                       # 只读用户能看
    assert post(c, store, "/cases", {"name": "Q3"}, user="carol").status_code == 403
    as_(c, "alice")
    assert c.post("/cases", data={"name": "Q3", "csrf": "x"}).status_code == 403   # 防伪码不对
    r = post(c, store, "/cases", {"name": "2026 Q3 季度奖"})
    cid = r.headers["location"].split("/")[-1].split("?")[0]
    files = [("files", (p.name, p.read_bytes())) for p in sorted(m.iterdir())]
    assert post(c, store, f"/cases/{cid}/materials", files=files).status_code == 303
    assert post(c, store, f"/cases/{cid}/materials", files=[("files", ("a.exe", b"x"))]).status_code == 400
    # 第 1 轮：材料冲突，程序提问，不出结果
    scripts.append([{"tool": "record_facts", "args": {"facts": [F5, F6]}}, *CLAUSES, DATASET,
                    {"tool": "finish", "args": {"summary": "季度奖"}}])
    assert post(c, store, f"/cases/{cid}/run").status_code == 303
    job = wait(app, cid)
    assert job.status == "done" and "等答复" in job.message, job.message
    page = c.get(f"/cases/{cid}").text
    assert "等待答复" in page and "季度奖-计提比例" in page and "运行下一轮" in page
    r = post(c, store, f"/cases/{cid}/confirm", user="bob")      # 没有正式结果不能确认
    assert "不能确认" in c.get(r.headers["location"]).text
    as_(c, "alice")
    # 网页答复：选 6%，记录答复人
    post(c, store, f"/cases/{cid}/answers", {"ans_q1": "1"})
    q = yaml.safe_load((store.dir(cid) / "out" / "questions.yaml").read_text(encoding="utf-8"))
    assert q[0]["answer"].startswith("6%") and q[0]["answered_by"] == "张会计"
    # 第 2 轮：按答复写规则，出结果
    scripts.append([rules("6%"), {"tool": "trial_run"}, {"tool": "finish", "args": {"summary": "季度奖"}}])
    post(c, store, f"/cases/{cid}/run")
    job = wait(app, cid)
    assert "已出结果" in job.message, job.message
    page = c.get(f"/cases/{cid}").text
    assert "result.xlsx" in page and "第 2 轮" in page
    # 只有 approver 能确认
    assert post(c, store, f"/cases/{cid}/confirm").status_code == 403
    r = post(c, store, f"/cases/{cid}/confirm", {"note": "已核对"}, user="bob")
    assert "已确认" in c.get(r.headers["location"]).text
    assert (store.dir(cid) / "out" / "template" / "manifest.yaml").exists()
    # 按模板计算本期数据（不调用大模型）
    data = [("files", ("数据.xlsx", (m / "数据.xlsx").read_bytes()))]
    r = post(c, store, f"/cases/{cid}/template", files=data)
    assert "已算出 1 个结果" in c.get(r.headers["location"]).text
    run = next((store.dir(cid) / "runs").iterdir())
    out = next(run.glob("result_*.xlsx"))
    assert c.get(f"/cases/{cid}/dl/runs/{run.name}/{out.name}").status_code == 200
    # 越界路径、案卷内部状态不能下载
    assert c.get(f"/cases/{cid}/dl/out/..%2Fmeta.json").status_code == 404
    assert c.get(f"/cases/{cid}/dl/out/case.json").status_code == 404
    assert c.get("/cases/../../etc").status_code == 404
    acts = [a["action"] for a in store.audit_log(cid)]
    for want in ("新建案卷", "上传材料", "运行下一轮", "本轮结束", "答复问题", "确认结果", "按模板计算", "下载"):
        assert want in acts, (want, acts)
    ans = next(a for a in store.audit_log(cid) if a["action"] == "答复问题")
    assert ans["user"] == "张会计" and ans["question"] == "q1"


def test_running_case_is_locked(env):
    app, store, scripts, c, m = env
    import threading
    gate = threading.Event()

    def slow():
        gate.wait(10)
        return FakeLLM([{"tool": "finish", "args": {"summary": "x"}}])
    app.state.runner.llm_factory = slow
    cid = store.create("t", "x")
    (store.dir(cid) / "materials" / "数据.xlsx").write_bytes((m / "数据.xlsx").read_bytes())
    post(c, store, f"/cases/{cid}/run")
    assert post(c, store, f"/cases/{cid}/run").status_code == 409
    assert post(c, store, f"/cases/{cid}/answers", {"ans_q1": "0"}).status_code == 409
    files = [("files", ("x.docx", b"x"))]
    assert post(c, store, f"/cases/{cid}/materials", files=files).status_code == 409
    gate.set()
    assert wait(app, cid).status in ("done", "failed")


def test_safe_name_and_passwords(tmp_path):
    assert safe_name("../../etc/方案.docx") == "方案.docx" and safe_name("C:\\x\\数据.xlsx") == "数据.xlsx"
    for bad in (".env.docx", "~$tmp.docx", "run.sh", ""):
        with pytest.raises(ValueError):
            safe_name(bad)
    from bonus_agent.web import Users
    u = Users(tmp_path / "users.yaml")
    u.add("a", PW, ["editor"])
    assert PW not in (tmp_path / "users.yaml").read_text(encoding="utf-8")
    assert u.check("a", PW)["roles"] == ["editor"] and u.check("a", "x") is None and u.check("b", PW) is None
    with pytest.raises(ValueError):
        u.add("a", "short", ["editor"])
    with pytest.raises(ValueError):
        u.add("a", PW, ["root"])
    u.add("李经理", PW, ["approver"])                       # 中文用户名也可以
    assert u.check("李经理", PW)["roles"] == ["approver"]
    assert oct(Path(tmp_path / "users.yaml").stat().st_mode)[-3:] == "600"


def test_login_logout_and_lockout(env):
    app, store, scripts, c, m = env
    r = login(c, "alice")
    assert r.status_code == 303 and "httponly" in r.headers["set-cookie"].lower()
    assert "张会计" in c.get("/").text and "退出" in c.get("/").text
    sid = c.cookies.get("bonus_session")
    assert c.post("/logout", data={"csrf": "x"}, follow_redirects=False).status_code == 403   # 退出也要防伪码
    r = c.post("/logout", data={"csrf": store.csrf(sid)}, follow_redirects=False)
    assert r.status_code == 303 and sid not in app.state.sessions
    c.cookies.set("bonus_session", sid)                       # 旧会话号作废
    assert c.get("/", follow_redirects=False).status_code == 303
    # 外部跳转地址不接受
    r = c.post("/login", data={"username": "alice", "password": PW, "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    # 连续输错 5 次：正确口令也暂停登录
    for _ in range(5):
        login(c, "bob", "wrong-password")
    assert "msg=" in login(c, "bob").headers["location"] and not c.cookies.get("bonus_session")
    # 会话过期
    login(c, "carol")
    sid = c.cookies.get("bonus_session")
    app.state.sessions[sid] = ("carol", 0)
    assert c.get("/", follow_redirects=False).status_code == 303


def test_chat_window(env):
    app, store, scripts, c, m = env
    cid = store.create("对话", "张会计")
    for p in m.iterdir():
        (store.dir(cid) / "materials" / p.name).write_bytes(p.read_bytes())
    scripts.append([{"tool": "record_facts", "args": {"facts": [F5, F6]}}, *CLAUSES, DATASET,
                    {"tool": "finish", "args": {"summary": "季度奖"}}])
    post(c, store, f"/cases/{cid}/run")
    wait(app, cid)
    # 对话里答复问题并修改：生成第 2 轮
    scripts.append([{"tool": "answer_question", "args": {"id": "q1", "answer": "按 6%"}}, rules("6%"),
                    {"tool": "reply", "args": {"text": "已按 6% 重新计算"}}])
    assert post(c, store, f"/cases/{cid}/chat", {"text": "按 6%"}).status_code == 303
    assert "已出结果" in wait(app, cid).message
    # 只是提问：直接回复，不生成新的一轮
    scripts.append([{"tool": "reply", "args": {"text": "奖金池是销售额合计的 6%"}}])
    post(c, store, f"/cases/{cid}/chat", {"text": "奖金池怎么来的？"})
    assert wait(app, cid).message == "已回复"
    chat = store.chat(cid)
    assert [x["role"] for x in chat] == ["agent", "user", "agent", "user", "agent"]
    assert "第 1 轮暂停" in chat[0]["text"]          # 每轮结束都在对话里交代
    assert "已按修改生成第 2 轮" in chat[2]["text"] and chat[4]["text"] == "奖金池是销售额合计的 6%"
    page = c.get(f"/cases/{cid}").text
    assert "奖金池怎么来的？" in page and "发送" in page and "第 2 轮" in page
    assert post(c, store, f"/cases/{cid}/chat", {"text": "x"}, user="carol").status_code == 403
    assert any(a["action"] == "对话" for a in store.audit_log(cid))


def test_failure_explained_with_retry(env):
    app, store, scripts, c, m = env
    from bonus_agent.failure import explain
    x = explain("智能体卡住了，已停止以免继续消耗 tokens：trial_run 连续 3 次失败，最后一次：错误：方案「Q」试算失败："
                "- [阻断] eval_error: min 没有可计算的明细行（回款明细）")
    assert "「试算」" in x["title"] and "没有对应的明细行" in x["title"] and "重试" in x["advice"]
    assert "按 0 处理" in x["ask"] and "min 没有可计算的明细行" in x["where"]
    assert "欠费" in explain("Error code: 400 - Arrearage")["title"]
    x = explain("智能体卡住了，已停止以免继续消耗 tokens：verify 连续 3 次失败，最后一次：错误：验算式算不出来：计算失败："
                "变量 v_ew 属于「员工」层级，不能在「个人」层直接引用（需要聚合）（位置：个人:E007）")
    assert "算不出来" in x["title"] and "没对上" not in x["title"] and "不需要你回答" in x["title"]   # 写法问题，不问财务
    cid = store.create("失败", "张会计")
    (store.dir(cid) / "materials" / "数据.xlsx").write_bytes((m / "数据.xlsx").read_bytes())
    scripts.append([{"tool": "trial_run"}] * 3)          # 连续失败 3 次，自动停下；程序自动接着做 2 轮，仍卡住才交给财务
    post(c, store, f"/cases/{cid}/run")
    wait(app, cid)
    acts = [json.loads(x)["action"] for x in (store.dir(cid) / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert acts.count("自动接续") == 2 and acts.count("本轮结束") == 3
    assert sum("程序自动接着做" in m["text"] for m in store.chat(cid)) == 2
    page = c.get(f"/cases/{cid}").text
    assert "已自动停下" in page and ">重试<" in page and "详细信息" in page
    said = store.chat(cid)[-1]["text"]                 # 对话里先说原因，再说需要什么帮助、想了解什么
    assert said.index("已自动停下") < said.index("想请你确认：") < said.index("怎么继续：")
    assert "点「发送」" in said and "直接重试多半还会卡" in said and "原因：" not in said
    assert "程序内部出错" in explain("'list' object has no attribute 'items'")["title"]
    # 上一轮没完成时发文字：作为说明接着做这一轮（不是只回答问题的对话）
    scripts.append([{"tool": "finish", "args": {"summary": "x"}}])
    post(c, store, f"/cases/{cid}/chat", {"text": "没有回款的销售单按 0 处理"})
    wait(app, cid)
    audit = (store.dir(cid) / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert "运行下一轮" in audit[-2] and any(m["text"] == "没有回款的销售单按 0 处理" for m in store.chat(cid))


def test_upload_in_chat(env):
    app, store, scripts, c, m = env
    cid = store.create("附件", "张会计")
    files = [("files", (p.name, p.read_bytes())) for p in m.iterdir()]
    scripts.append([{"tool": "finish", "args": {"summary": "x"}}])
    assert post(c, store, f"/cases/{cid}/chat", {"text": "这是第一批材料"}, files=files).status_code == 303
    wait(app, cid)
    chat = store.chat(cid)
    assert chat[0]["text"].startswith("上传了材料：") and "这是第一批材料" in chat[0]["text"]
    assert sorted(p.name for p in (store.dir(cid) / "materials").iterdir()) == sorted(p.name for p in m.iterdir())
    post(c, store, f"/cases/{cid}/chat", {"expected": "1"}, files=[("files", ("期望.xlsx", (m / "数据.xlsx").read_bytes()))])
    assert "期望结果" in store.chat(cid)[-1]["text"] and (store.dir(cid) / "expected" / "期望.xlsx").exists()
    page = c.get(f"/cases/{cid}").text
    assert "<h2>材料</h2>" not in page and "已有材料" in page


def test_steps_written_while_running(env):
    app, store, scripts, c, m = env
    cid = store.create("过程", "张会计")
    (store.dir(cid) / "materials" / "数据.xlsx").write_bytes((m / "数据.xlsx").read_bytes())
    scripts.append([{"tool": "trial_run"}] * 3)
    post(c, store, f"/cases/{cid}/run")
    wait(app, cid)
    log = (store.dir(cid) / "out" / "running_steps.txt").read_text(encoding="utf-8")
    assert log.count("[0") == 3 and log.count("行动：trial_run") == 3 and "观察：" in log and "思考：" in log


def test_chat_text_is_citable_source(tmp_path):
    """财务在对话里贴的规则原文可以作为出处登记参数（file 写「财务对话」），没上传成文件也不会卡住。"""
    from bonus_agent.agent import CHAT_SOURCE, Workspace, _said
    from bonus_agent.auto import build_inventory
    from bonus_agent.llm import FakeLLM

    doc = tmp_path / "01_制度.md"
    doc.write_text("客服奖金按月发放。", encoding="utf-8")
    ws = Workspace(FakeLLM([]), build_inventory([doc]))
    ws.said = _said([{"role": "user", "text": "上传了期望结果：x.xlsx（只用于核对，不交给智能体）"},
                     {"role": "agent", "text": "好的"}], "在线客服300.00 800.00\n上限作用于用工折算后的最终金额", None)
    assert "期望结果" not in ws.said and "好的" not in ws.said
    out = ws.record_facts([{"name": "在线客服保障标准", "value": "300.00", "file": CHAT_SOURCE, "quote": "在线客服300.00"},
                           {"name": "在线客服上限", "value": "800.00", "file": "03_说明.pdf", "quote": "800.00"}])
    assert "已登记 1 条" in out and f"file 只能是：01_制度.md、{CHAT_SOURCE}" in out
    assert CHAT_SOURCE in ws.list_files() and "上限作用于用工折算后的最终金额" in ws.briefing()


def test_list_where_object_expected_is_tolerated(tmp_path):
    """模型把对象写成列表（[{"C1": "pool"}]、[{"name": "x", "expr": ...}]）时程序改回来；改不回来的报参数错，不让整轮崩掉。"""
    from bonus_agent.agent import _coerce_args
    assert _coerce_args("cover_clauses", {"items": [{"C1": "pool"}, {"C2": "tables.t"}]}) == {"items": {"C1": "pool", "C2": "tables.t"}}
    assert _coerce_args("cover_clauses", {"items": [{"id": "C1", "where": "pool"}]}) == {"items": {"C1": "pool"}}
    v = _coerce_args("set_rules", {"variables": [{"name": "n", "expr": "1", "scope": "个人"}]})["variables"]
    assert v == {"n": {"expr": "1", "scope": "个人"}}


def test_rejected_upload_keeps_existing_files(env, monkeypatch):
    """#8 新文件被拒（太大）时：原来的期望结果还在；一批材料里有一个被拒，整批都不写入。"""
    import bonus_agent.web as web
    app, store, scripts, c, m = env
    cid = store.create("上传", "张会计")
    post(c, store, f"/cases/{cid}/expected", files=[("files", ("期望.xlsx", b"old"))])
    monkeypatch.setattr(web, "MAX_UPLOAD", 10)
    r = post(c, store, f"/cases/{cid}/expected", files=[("files", ("新期望.xlsx", b"x" * 20))])
    assert r.status_code == 413
    assert [p.name for p in (store.dir(cid) / "expected").iterdir()] == ["期望.xlsx"]
    r = post(c, store, f"/cases/{cid}/materials", files=[("files", ("a.xlsx", b"ok")), ("files", ("b.xlsx", b"x" * 20))])
    assert r.status_code == 413 and not list((store.dir(cid) / "materials").glob("*.xlsx"))
