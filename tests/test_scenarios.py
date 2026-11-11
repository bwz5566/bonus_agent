"""场景：场景包查找、输入模板检查、场景记忆、编制时交给智能体的场景信息、发布规则版本（含验收）、按版本计算，以及网页上的三种用法。
所有数据都是仓库里的合成样例（evals/cases/syn_acceptance），不含任何公司的规则。"""
import json
import shutil
import sys
from pathlib import Path

import openpyxl
import pytest
import yaml

from bonus_agent.scenarios import BUILTIN, Registry, ScenarioError, load_scenario
from bonus_agent.scenarios import releases as rl
from bonus_agent.scenarios.context import prepare
from bonus_agent.scenarios.inputs import check_inputs, template_text
from bonus_agent.scenarios.memory import ScenarioMemory, answer_text

CASE = Path(__file__).parent.parent / "evals" / "cases" / "syn_acceptance"
sys.path.insert(0, str(CASE))
import run_case  # noqa: E402

EXPECTED = CASE / "expected" / "05_期望奖金结果.xlsx"

INPUTS = [
    {"name": "员工", "columns": [{"name": "employee_id", "aliases": ["工号"]}, {"name": "group_id"},
                                {"name": "join_date", "type": "date"}]},
    {"name": "销售明细", "aliases": ["销售"], "columns": [
        {"name": "sales_id", "nonblank": True}, {"name": "sales_amount", "type": "number"},
        {"name": "cost_amount", "type": "number"}]},
    {"name": "预算", "required": False, "columns": [{"name": "budget", "type": "number"}]},
]


def make_pack(root: Path, sid="demo_quarter", acceptance=True, **extra) -> Path:
    d = root / sid
    (d / "acceptance").mkdir(parents=True)
    spec = {"id": sid, "name": "季度奖金（示例）", "description": "合成样例的季度、过程、年度奖金", "inputs": INPUTS, **extra}
    if acceptance:
        shutil.copy(run_case.XLSX, d / "acceptance" / "业务输入.xlsx")
        shutil.copy(EXPECTED, d / "acceptance" / "期望结果.xlsx")
        spec["acceptance"] = [{"id": "样例", "data": ["acceptance/业务输入.xlsx"], "expected": "acceptance/期望结果.xlsx"}]
    (d / "scenario.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")
    (d / "guide.md").write_text("# 示例场景说明\n\n季度奖按销售净额计提。\n", encoding="utf-8")
    return d


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.delenv("BONUS_SCENARIO_DIRS", raising=False)
    monkeypatch.delenv("BONUS_SCENARIO_HOME", raising=False)
    make_pack(tmp_path / "scen")
    return Registry([tmp_path / "scen"])


# ---------------- 场景包 ----------------
def test_builtin_skeletons_have_no_rules():
    ids = {sc.id for sc in Registry(home="/nonexistent").list()}
    assert {"sales_commission", "eng_dev_bonus", "annual_bonus"} <= ids
    for d in BUILTIN.iterdir():
        sc = load_scenario(d)
        assert not sc.guide_text() and not sc.inputs and not sc.acceptance     # 仓库里不放公司的规则


def test_local_pack_overrides_builtin_and_home_is_local(tmp_path, monkeypatch):
    monkeypatch.delenv("BONUS_SCENARIO_DIRS", raising=False)
    monkeypatch.delenv("BONUS_SCENARIO_HOME", raising=False)
    make_pack(tmp_path / "scen", "sales_commission", acceptance=False)
    reg = Registry([tmp_path / "scen"])
    sc = reg.get("sales_commission")
    assert sc.dir == (tmp_path / "scen" / "sales_commission").resolve() and sc.inputs
    assert reg.home == tmp_path / "scen" and reg.data_dir("sales_commission") == tmp_path / "scen" / "sales_commission"
    monkeypatch.setenv("BONUS_SCENARIO_DIRS", str(tmp_path / "env"))
    make_pack(tmp_path / "env", "sales_commission", acceptance=False, name="环境变量里的")
    assert Registry([tmp_path / "scen"]).get("sales_commission").name == "环境变量里的"     # 环境变量的目录最先


def test_bad_packs_and_ids_are_reported(tmp_path, reg):
    with pytest.raises(ScenarioError, match="没有场景 nope"):
        reg.get("nope")
    with pytest.raises(ScenarioError):
        reg.get("../etc")
    d = tmp_path / "scen" / "wrong"
    d.mkdir()
    (d / "scenario.yaml").write_text("id: other\nname: x\n", encoding="utf-8")
    with pytest.raises(ScenarioError, match="目录名"):
        reg.get("wrong")
    (d / "scenario.yaml").write_text("id: wrong\nname: x\nformula: 1\n", encoding="utf-8")
    with pytest.raises(ScenarioError, match="formula"):          # 不认识的字段直接报错
        reg.get("wrong")
    sc = make_pack(tmp_path / "s2", "with_bad_case", acceptance=False)
    assert reg.get("demo_quarter").file("acceptance/业务输入.xlsx").exists()
    with pytest.raises(ScenarioError, match="越界"):
        load_scenario(sc).file("../../x")


# ---------------- 输入模板 ----------------
def _book(path: Path, sheets: dict[str, list[list]]) -> Path:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    wb.save(path)
    return path


def test_input_template_passes_on_conforming_data(reg):
    rep = check_inputs(reg.get("demo_quarter"), [run_case.XLSX])
    assert rep.ok, rep.text()
    assert set(rep.found) == {"员工", "销售明细"} and "预算" not in rep.found
    assert any("可以不提供" in f.text for f in rep.findings)          # 可选表没给：只提醒
    assert any("不在输入模板里" in f.text for f in rep.findings)
    assert "列：employee_id（文字）" in template_text(reg.get("demo_quarter"))


def test_input_template_reports_missing_tables_columns_and_bad_values(tmp_path, reg):
    f = _book(tmp_path / "本期.xlsx", {
        "人员": [["工号", "group_id"], ["E1", "G1"], ["E2", "G1"]],                          # 按列名认出“员工”；缺 join_date
        "销售": [["sales_id", "sales_amount", "cost_amount"], ["S1", "1,000", 3], [None, "待定", 2], ["S3", 5, 1]],
    })
    rep = check_inputs(reg.get("demo_quarter"), [f])
    text = rep.text()
    assert not rep.ok
    assert "员工：缺少列「join_date」" in text
    assert "sales_amount」应为数字，有 1 行不是：第 3 行「待定」" in text     # 千分位逗号算数字
    assert "sales_id」必须填写，有 1 行为空（第 3 行）" in text
    assert "人员" in rep.found["员工"] and "employee_id" not in text.split("缺少列")[-1]   # 别名“工号”认得


def test_input_template_missing_required_table(tmp_path, reg):
    f = _book(tmp_path / "x.xlsx", {"别的": [["a", "b"], [1, 2]]})
    rep = check_inputs(reg.get("demo_quarter"), [f])
    assert [x.table for x in rep.errors] == ["员工", "销售明细"]


# ---------------- 场景记忆 ----------------
def test_memory_keeps_latest_answer_per_question(tmp_path):
    mem = ScenarioMemory(tmp_path / "memory.yaml")
    qs = [{"id": "q1", "text": "汇率用哪天的？", "options": ["月末", "月初"], "answer": 0},
          {"id": "q2", "text": "负数怎么办？", "answer": "照常冲减"},
          {"id": "q3", "text": "撤回的", "answer": "x", "withdrawn": "不需要了"},
          {"id": "q4", "text": "没答的", "answer": None}]
    assert mem.add_questions(qs, version="v1", source="案卷甲", by="财务甲") == 2
    assert [x["answer"] for x in mem.items()] == ["月末", "照常冲减"]
    mem.add_questions([{"text": "汇率用哪天的？ ", "answer": "月初"}], version="v2")
    items = mem.items()
    assert len(items) == 2 and items[0]["answer"] == "月初" and items[0]["version"] == "v2"
    assert "答：月初（v2）" in mem.brief()
    assert mem.remove([items[0]["id"]]) == 1 and len(mem.items()) == 1
    assert answer_text({"answer": "1", "options": ["a", "b"]}) == "b"


# ---------------- 编制：交给智能体的场景信息 ----------------
def test_prepare_stages_guide_as_material_and_briefs_agent(tmp_path, reg):
    m = tmp_path / "材料"
    m.mkdir()
    shutil.copy(run_case.XLSX, m / "业务输入.xlsx")
    reg.memory("demo_quarter").add_questions([{"text": "季度奖按什么口径？", "answer": "按销售净额"}], version="v1")
    s = prepare(reg, "demo_quarter", [m])
    assert s.files and s.files[0].name == "场景说明（季度奖金（示例））.md" and "季度奖按销售净额计提" in s.files[0].read_text(encoding="utf-8")
    assert "【场景】季度奖金（示例）" in s.brief and "【输入模板】" in s.brief and "全部符合" not in s.brief
    assert "按输入模板检查：0 个错误" in s.brief and "【场景记忆】" in s.brief and "按销售净额" in s.brief
    assert "【场景记忆】" not in prepare(reg, "demo_quarter", [m], use_memory=False).brief


def test_agent_reads_scenario_guide_and_brief(tmp_path, reg):
    from bonus_agent.agent import run_agent
    from bonus_agent.llm import FakeLLM
    m = tmp_path / "材料"
    m.mkdir()
    shutil.copy(run_case.XLSX, m / "业务输入.xlsx")
    llm = FakeLLM([json.dumps({"tool": "finish", "args": {"summary": "x"}})])
    llm.tool_mode = "json"
    run_agent(llm, [m], tmp_path / "out", scenario=prepare(reg, "demo_quarter", [m]), max_steps=2)
    task = next(x["content"] for x in llm.calls[0] if x["role"] == "user")
    assert "【场景】季度奖金（示例）" in task and "场景说明（季度奖金（示例））.md" in task and "季度奖按销售净额计提" in task


# ---------------- 发布与计算 ----------------
def confirmed_case(tmp_path) -> Path:
    from bonus_agent import review as rv
    from bonus_agent.agent import run_agent
    from bonus_agent.case import confirm
    from bonus_agent.llm import FakeLLM
    from tests.test_syn_acceptance import _script
    out = tmp_path / "案卷"
    ar = run_agent(FakeLLM(_script()), [run_case.XLSX], out)
    assert ar.status == "OK"
    rv.mark(out, {r["key"]: (rv.OK, "") for r in rv.load(out)}, "财务甲")
    confirm(out, "财务甲", "样例")
    return out


def test_publish_runs_acceptance_then_compute_reuses_version(tmp_path, reg):
    out = confirmed_case(tmp_path)
    qs = [{"id": "q1", "text": "过程奖按里程碑验收日期算吗？", "answer": "是"}]
    rel = rl.publish(reg, "demo_quarter", out / "template", "财务乙", "首版", source="案卷", questions=qs)
    assert rel.version == "v1" and rel.info["acceptance"][0]["ok"]
    assert (reg.data_dir("demo_quarter") / "releases" / "v1" / "release.yaml").exists()
    assert reg.memory("demo_quarter").items()[0]["answer"] == "是"
    v2 = rl.publish(reg, "demo_quarter", out / "template", "财务乙")
    assert v2.version == "v2"     # 只增不覆盖
    imp = v2.info["impact"][0]     # 和上一版比：同一套规则，验收数据上一分钱都不变
    assert imp["against"] == "v1" and imp["人员"]["changed"] == 0 and imp["对象"]["changed"] == 0 and imp["对象"]["total"] > 0
    assert [r.version for r in reg.releases("demo_quarter")] == ["v1", "v2"]
    # 计算：换了文件名的本期数据，按 v1 算（不调用大模型），与参考实现一致
    nxt = tmp_path / "本期" / "业务输入_10月.xlsx"
    nxt.parent.mkdir()
    shutil.copy(run_case.XLSX, nxt)
    c = rl.compute(reg, "demo_quarter", [nxt], tmp_path / "结果", "v1")
    ref = json.loads((CASE / "reference.json").read_text(encoding="utf-8"))
    assert run_case.diff_reference(c.results, ref) == [] and c.release.version == "v1"
    assert (tmp_path / "结果" / "run.json").exists() and "0 个错误" in (tmp_path / "结果" / "输入检查.txt").read_text(encoding="utf-8")
    assert rl.compute(reg, "demo_quarter", [nxt], tmp_path / "结果2").release.version == "v2"   # 不写版本用最新
    # 发布后的版本被改过：拒绝计算
    rules = next((reg.data_dir("demo_quarter") / "releases" / "v1").glob("rules_QUARTER.yaml"))
    rules.write_text("# 改过\n" + rules.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(Exception, match="被修改过"):
        rl.compute(reg, "demo_quarter", [nxt], tmp_path / "结果3", "v1")


def test_compute_blocks_on_input_errors(tmp_path, reg):
    out = confirmed_case(tmp_path)
    rl.publish(reg, "demo_quarter", out / "template", "财务乙")
    bad = _book(tmp_path / "坏.xlsx", {"员工": [["employee_id", "employee_name"], ["E1", "甲"]]})
    with pytest.raises(rl.InputError, match="缺少列「group_id」"):
        rl.compute(reg, "demo_quarter", [bad], tmp_path / "r")
    assert "错误" in (tmp_path / "r" / "输入检查.txt").read_text(encoding="utf-8")
    assert not list((tmp_path / "r").glob("result*.xlsx"))


def test_publish_refused_when_acceptance_fails(tmp_path, reg):
    out = confirmed_case(tmp_path)
    exp = reg.get("demo_quarter").file("acceptance/期望结果.xlsx")
    wb = openpyxl.load_workbook(exp)
    ws = wb["个人结果"]
    assert ws.cell(5, 8).value == "应得金额"
    ws.cell(6, 8).value = round(ws.cell(6, 8).value + 0.01, 2)       # 期望结果改掉一分钱
    wb.save(exp)
    with pytest.raises(rl.PublishError, match="验收没通过"):
        rl.publish(reg, "demo_quarter", out / "template", "财务乙")
    assert reg.releases("demo_quarter") == []
    with pytest.raises(ScenarioError, match="还没有发布"):
        rl.compute(reg, "demo_quarter", [run_case.XLSX], tmp_path / "r")


def test_cli_scenarios_publish_compute(tmp_path, reg, capsys):
    from bonus_agent.cli import main
    out = confirmed_case(tmp_path)
    sdir = str(tmp_path / "scen")
    assert main(["publish", "--scenario", "demo_quarter", "--out", str(out), "--by", "财务乙", "--scenario-dir", sdir]) == 0
    assert main(["scenarios", "--scenario-dir", sdir]) == 0
    assert "季度奖金（示例）（demo_quarter）：v1" in capsys.readouterr().out
    assert main(["compute", "--scenario", "demo_quarter", "--out", str(tmp_path / "r"), "--scenario-dir", sdir, str(run_case.XLSX)]) == 0
    assert list((tmp_path / "r").glob("result_*.xlsx"))
    assert main(["compute", "--scenario", "demo_quarter", "--version", "v9", "--out", str(tmp_path / "r2"),
                 "--scenario-dir", sdir, str(run_case.XLSX)]) == 1


# ---------------- 网页 ----------------
def test_web_scenario_author_publish_and_compute(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from bonus_agent.llm import FakeLLM
    from bonus_agent.web import create_app
    from tests.test_web import PW, post
    monkeypatch.delenv("BONUS_SCENARIO_DIRS", raising=False)
    monkeypatch.delenv("BONUS_SCENARIO_HOME", raising=False)
    data = tmp_path / "data"
    make_pack(data / "scenarios")
    app = create_app(data, lambda: FakeLLM([]))
    store = app.state.store
    store.users.add("alice", PW, ["editor"], "张会计")
    store.users.add("bob", PW, ["approver"], "李经理")
    c = TestClient(app)
    c.sids = {}
    # 计算模式要有已发布的版本
    assert post(c, store, "/cases", {"name": "10月", "scenario": "demo_quarter", "mode": "compute"}).status_code == 400
    r = post(c, store, "/cases", {"name": "9月编制", "scenario": "demo_quarter", "mode": "author", "memory": "1"})
    cid = r.headers["location"].split("/")[-1]
    assert store.meta(cid)["scenario"] == "demo_quarter" and store.meta(cid)["memory"] is True
    # 编制：用合成样例的确认案卷代替智能体的几轮（智能体流程在其他测试里）
    src = confirmed_case(tmp_path)
    shutil.rmtree(store.dir(cid) / "out")
    shutil.copytree(src, store.dir(cid) / "out")
    page = c.get(f"/cases/{cid}").text
    assert "季度奖金（示例） · 编制" in page and "发布规则版本" in page and "发布为场景的新规则版本" not in page   # 经办看不到发布按钮
    r = post(c, store, f"/cases/{cid}/publish", {"note": "9 月起"}, user="bob")
    assert r.status_code == 303 and "v1" in r.headers["location"], r.headers["location"]
    assert "已发布为规则版本 v1" in c.get(f"/cases/{cid}").text
    from urllib.parse import unquote
    assert "已经发布过" in unquote(post(c, store, f"/cases/{cid}/publish", user="bob").headers["location"])
    assert [x.version for x in app.state.registry.releases("demo_quarter")] == ["v1"]
    # 计算模式：新案卷、传本期数据、按 v1 算
    r = post(c, store, "/cases", {"name": "10月", "scenario": "demo_quarter", "mode": "compute"})
    cid2 = r.headers["location"].split("/")[-1]
    page = c.get(f"/cases/{cid2}").text
    assert "检查并计算" in page and "运行下一轮" not in page
    with pytest.raises(RuntimeError, match="不运行智能体"):                  # 计算案卷不运行智能体
        app.state.runner.start(cid2, "张会计")
    assert post(c, store, f"/cases/{cid2}/chat", {"text": "改一下系数"}).status_code == 409
    r = post(c, store, f"/cases/{cid2}/compute", {"version": "v1"},
             files=[("files", ("业务输入_10月.xlsx", run_case.XLSX.read_bytes()))])
    assert r.status_code == 303 and "已按 v1 算出" in unquote(r.headers["location"])
    page = c.get(f"/cases/{cid2}").text
    assert "按 v1 算完" in page and "result_QUARTER" in page
    r = post(c, store, f"/cases/{cid2}/compute", {"version": "v1"},
             files=[("files", ("坏.xlsx", _book(tmp_path / "坏.xlsx", {"员工": [["employee_id", "employee_name"], ["E1", "甲"]]}).read_bytes()))])
    assert "没有算出结果" in unquote(r.headers["location"])
    # 场景页：说明、模板、版本、记忆
    page = c.get("/scenarios/demo_quarter").text
    assert "季度奖按销售净额计提" in page and "v1" in page and "通过 样例" in page
    assert "demo_quarter" in c.get("/").text
    # 老案卷（没有场景）照常打开
    old = store.create("老案卷", "张会计")
    assert c.get(f"/cases/{old}").status_code == 200 and "通用" in c.get("/").text
