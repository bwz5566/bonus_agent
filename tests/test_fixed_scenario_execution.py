"""Production scenarios: independently specified money, release gates and recovery.

These are synthetic finance acceptance materials, not production bonus policies.
"""
import json
from pathlib import Path
import threading
import zipfile
import xml.etree.ElementTree as ET

import openpyxl
import pytest
import yaml

from bonus_agent.scenarios.releases import FormulaInputError, ReleaseError, publish, load_release, versions
from bonus_agent.scenarios.execution import run_fixed, Ledger, RunBusy
from bonus_core.engine.run import Engine


def fixture_package(root, scenario="annual_bonus", version="v1"):
    root.mkdir(parents=True, exist_ok=True)
    rules = {
        "name": "synthetic-finance-policy", "version": version,
        "datasets": {
            "员工": {"key": "emp", "fields": {"emp": {"type": "id"}, "prepaid": {"type": "money"}, "ready": {"type": "bool"}}},
            "销售": {"link": "emp", "fields": {"emp": {"type": "id"}, "amount": {"type": "money"}}}},
        "pool": {"scope": "个人", "expr": 'round(sum(amount, of: "销售") * 0.1, 2)'},
        "allocation": [{"level": "个人", "key": "emp"}],
        "payout": [{"id": "回访", "scope": "个人", "hold_ratio": "if(ready, 0, 0.25)"}],
        "offsets": [{"id": "预发", "scope": "个人", "expr": "prepaid"}]}
    mapping = {
        "员工": {"file": "valid.xlsx", "sheet": "员工", "header_rows": [1], "columns": {"emp": "工号", "prepaid": "预发", "ready": "回访"}},
        "销售": {"file": "valid.xlsx", "sheet": "销售", "header_rows": [1], "columns": {"emp": "工号", "amount": "销售额"}}}
    for name, body in (("rules.yaml", rules), ("mapping.yaml", mapping)):
        (root / name).write_text(yaml.safe_dump(body, allow_unicode=True, sort_keys=False), encoding="utf-8")
    for name, duplicate in (("valid.xlsx", False), ("duplicate.xlsx", True)):
        wb = openpyxl.Workbook(); emp = wb.active; emp.title = "员工"
        for row in [["工号", "预发", "回访"], ["E1", 150, False], ["E2", 50, True]]:
            emp.append(row)
        if duplicate:
            emp.append(["E1", 150, False])
        sales = wb.create_sheet("销售")
        for row in [["工号", "销售额"], ["E1", 600], ["E1", 400], ["E2", 2000]]:
            sales.append(row)
        wb.save(root / name)
    # Independently specified: E1 earned 100, held 25, cap 75, advance 150;
    # E2 earned 200, held 0, advance 50. Actual paid 0 + 150.
    def pay(final, held, cap, offset, deducted, paid, overpaid):
        return dict(zip(("final", "held", "cap", "offset", "deducted", "paid", "overpaid"),
                        map(str, (final, held, cap, offset, deducted, paid, overpaid))))
    expected = {"R0": {"nodes": {"全部": "300", "个人:E1": "100", "个人:E2": "200"},
                       "payouts": {"个人:E1": pay(100, 25, 75, 150, 75, 0, 75),
                                   "个人:E2": pay(200, 0, 200, 50, 50, 150, 0)}, "rollups": {}}}
    (root / "expected.json").write_text(json.dumps(expected), encoding="utf-8")
    spec = {"scenario": scenario, "version": version, "periods": ["2026-Q3", "2026-Q4"],
            "rules": "rules.yaml", "mapping": "mapping.yaml", "cases": [
                {"id": "money", "data": ["valid.xlsx"], "expected": "expected.json"},
                {"id": "duplicate", "data": ["duplicate.xlsx"], "error_code": "key_dup"}]}
    (root / "release.yaml").write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return root


@pytest.fixture
def released(tmp_path):
    source = fixture_package(tmp_path / "source")
    releases = tmp_path / "releases"
    publish(source, releases, "财务审批人")
    return source, releases


def test_publication_is_immutable_and_requires_independent_complete_answers(tmp_path):
    source = fixture_package(tmp_path / "source")
    expected = json.loads((source / "expected.json").read_text())
    del expected["R0"]["payouts"]["个人:E2"]
    (source / "expected.json").write_text(json.dumps(expected))
    with pytest.raises(ReleaseError, match="不完整"):
        publish(source, tmp_path / "releases", "财务")
    assert versions("annual_bonus", tmp_path / "releases") == []
    fixture_package(source)
    publish(source, tmp_path / "releases", "财务")
    with pytest.raises(ReleaseError, match="不可覆盖"):
        publish(source, tmp_path / "releases", "财务")
    directory, _, _ = load_release("annual_bonus", "v1", tmp_path / "releases")
    (directory / "rules.yaml").write_text("changed")
    with pytest.raises(ReleaseError, match="被修改"):
        load_release("annual_bonus", "v1", tmp_path / "releases")


@pytest.mark.parametrize("scenario", ["sales_commission", "eng_dev_bonus", "annual_bonus"])
def test_each_registered_scenario_can_receive_its_own_finance_release(tmp_path, scenario):
    source = fixture_package(tmp_path / "source", scenario)
    publish(source, tmp_path / "releases", "财务")
    run = run_fixed(scenario, "v1", "2026-Q3", [source / "valid.xlsx"], tmp_path / "batch", releases=tmp_path / "releases")
    assert run.result.payouts["个人:E1"].overpaid == "75.00"
    assert run.result.payouts["个人:E2"].paid == "150.00"


def test_same_version_name_in_two_scenarios_uses_different_approved_logic(tmp_path):
    annual = fixture_package(tmp_path / "annual", "annual_bonus")
    development = fixture_package(tmp_path / "development", "eng_dev_bonus")
    rule = yaml.safe_load((development / "rules.yaml").read_text())
    rule["pool"]["expr"] = 'round(sum(amount, of: "销售") * 0.2, 2)'
    (development / "rules.yaml").write_text(yaml.safe_dump(rule, allow_unicode=True, sort_keys=False), encoding="utf-8")
    expected = {"R0": {"nodes": {"全部": "600", "个人:E1": "200", "个人:E2": "400"}, "rollups": {},
        "payouts": {
            "个人:E1": {"final": "200", "held": "50", "cap": "150", "offset": "150", "deducted": "150", "paid": "0", "overpaid": "0"},
            "个人:E2": {"final": "400", "held": "0", "cap": "400", "offset": "50", "deducted": "50", "paid": "350", "overpaid": "0"}}}}
    (development / "expected.json").write_text(json.dumps(expected))
    releases = tmp_path / "releases"
    publish(annual, releases, "财务"); publish(development, releases, "财务")
    a = run_fixed("annual_bonus", "v1", "2026-Q3", [annual / "valid.xlsx"], tmp_path / "batch", releases=releases)
    d = run_fixed("eng_dev_bonus", "v1", "2026-Q3", [annual / "valid.xlsx"], tmp_path / "batch", releases=releases)
    assert a.result.payouts["个人:E2"].paid == "150.00"
    assert d.result.payouts["个人:E2"].paid == "350.00" and a.task_id != d.task_id


def test_duplicate_execution_reuses_result_and_separates_period_and_batch(released, tmp_path, monkeypatch):
    source, releases = released
    call = Engine.compute
    calls = []
    def spy(self, *args, **kwargs):
        calls.append(1)
        return call(self, *args, **kwargs)
    monkeypatch.setattr(Engine, "compute", spy)
    first = run_fixed("annual_bonus", "v1", "2026-Q3", [source / "valid.xlsx"], tmp_path / "batch-a", releases=releases)
    second = run_fixed("annual_bonus", "v1", "2026-Q3", [source / "valid.xlsx"], tmp_path / "batch-a", releases=releases)
    assert first.task_id == second.task_id and second.cached and len(calls) == 1
    run_fixed("annual_bonus", "v1", "2026-Q4", [source / "valid.xlsx"], tmp_path / "batch-a", releases=releases)
    run_fixed("annual_bonus", "v1", "2026-Q3", [source / "valid.xlsx"], tmp_path / "batch-b", releases=releases)
    assert len(calls) == 3
    (first.directory / "result.xlsx").write_bytes(b"tamper")
    with pytest.raises(ReleaseError, match="被修改"):
        run_fixed("annual_bonus", "v1", "2026-Q3", [source / "valid.xlsx"], tmp_path / "batch-a", releases=releases)
    assert len(calls) == 3


def test_export_failure_resumes_computed_checkpoint(released, tmp_path, monkeypatch):
    from bonus_agent.scenarios import execution
    source, releases = released
    original = execution.write_excel
    monkeypatch.setattr(execution, "write_excel", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        run_fixed("annual_bonus", "v1", "2026-Q3", [source / "valid.xlsx"], tmp_path / "batch", releases=releases)
    assert not list((tmp_path / "batch").rglob("result.xlsx"))
    monkeypatch.setattr(execution, "write_excel", original)
    monkeypatch.setattr(Engine, "compute", lambda *a, **k: pytest.fail("completed calculation must not repeat"))
    run = run_fixed("annual_bonus", "v1", "2026-Q3", [source / "valid.xlsx"], tmp_path / "batch", releases=releases)
    assert (run.directory / "result.xlsx").exists()


def test_active_lease_blocks_parallel_run_and_expired_lease_can_resume(tmp_path):
    ledger = Ledger(tmp_path)
    token, _ = ledger.claim("task")
    with pytest.raises(RunBusy):
        Ledger(tmp_path).claim("task")
    with ledger.connect() as db:
        db.execute("UPDATE tasks SET lease=0 WHERE id='task'")
    next_token, _ = Ledger(tmp_path).claim("task")
    assert token != next_token
    with pytest.raises(RunBusy):
        ledger.update("task", token, "completed")
    with pytest.raises(RunBusy):
        ledger.finish("task", token, lambda: pytest.fail("stale worker must not publish files"))
    ledger.finish("task", next_token, lambda: {"files": {}})
    assert ledger.claim("task") == (None, {"files": {}})


def test_parallel_submission_computes_only_once(released, tmp_path, monkeypatch):
    source, releases = released
    entered, resume = threading.Event(), threading.Event()
    original = Engine.compute
    completed, errors = [], []
    def slow(self, *a, **kw):
        entered.set()
        assert resume.wait(10)
        return original(self, *a, **kw)
    monkeypatch.setattr(Engine, "compute", slow)
    args = ("annual_bonus", "v1", "2026-Q3", [source / "valid.xlsx"], tmp_path / "batch")
    def first():
        try:
            completed.append(run_fixed(*args, releases=releases))
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=first); thread.start()
    assert entered.wait(10)
    try:
        with pytest.raises(RunBusy):
            run_fixed(*args, releases=releases)
    finally:
        resume.set(); thread.join(10)
    assert not thread.is_alive() and not errors and len(completed) == 1


def test_engine_upgrade_requires_reacceptance(released, monkeypatch):
    from bonus_agent.scenarios import execution
    _, releases = released
    monkeypatch.setattr(execution, "engine_code", lambda: "new-program")
    with pytest.raises(ReleaseError, match="程序已变更"):
        load_release("annual_bonus", "v1", releases)


def test_unpublished_and_wrong_period_do_not_compute(released, tmp_path, monkeypatch):
    source, releases = released
    monkeypatch.setattr(Engine, "compute", lambda *a, **k: pytest.fail("must block first"))
    for version, period in (("v2", "2026-Q3"), ("v1", "2027")):
        with pytest.raises(ReleaseError):
            run_fixed("annual_bonus", version, period, [source / "valid.xlsx"], tmp_path / "batch", releases=releases)


def test_fixed_format_does_not_guess_headers_or_identifiers(released, tmp_path):
    source, releases = released
    p = tmp_path / "valid.xlsx"
    wb = openpyxl.load_workbook(source / "valid.xlsx")
    wb["销售"]["A2"] = "e1"
    wb.save(p)
    with pytest.raises(ReleaseError, match="精确关联"):
        run_fixed("annual_bonus", "v1", "2026-Q3", [p], tmp_path / "batch", releases=releases)
    wb["销售"]["A1"] = "销售额"
    wb["销售"]["B1"] = "工号"
    wb.save(p)
    with pytest.raises(ReleaseError, match="表头"):
        run_fixed("annual_bonus", "v1", "2026-Q3", [p], tmp_path / "batch", releases=releases)


def test_assistant_cannot_write_or_loop_forever(released):
    from bonus_agent.llm import FakeLLM
    from bonus_agent.scenarios.assistant import answer
    _, releases = released
    llm = FakeLLM([{"tool": "set_rules", "args": {"pool": {"expr": "999999"}}}])
    assert "禁止工具 set_rules" in answer(llm, "annual_bonus", "v1", "把奖金加倍", releases=releases)
    assert len(llm.calls) == 1
    llm = FakeLLM([{"tool": "describe_scenario"}])
    assert "上限" in answer(llm, "annual_bonus", "v1", "规则是什么", releases=releases, max_steps=2)
    assert len(llm.calls) == 2


def formula_workbook(source, destination, *, cell="B2", cached="600", formula="9000"):
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, "w") as modified:
        for item in original.infolist():
            data = original.read(item.filename)
            if item.filename == "xl/worksheets/sheet2.xml":
                sheet = ET.fromstring(data)
                target = sheet.find(f'.//{{{ns}}}c[@r="{cell}"]')
                target.attrib.pop("t", None)
                for child in list(target):
                    target.remove(child)
                ET.SubElement(target, f"{{{ns}}}f").text = formula
                if cached is not None:
                    ET.SubElement(target, f"{{{ns}}}v").text = cached
                data = ET.tostring(sheet)
            modified.writestr(item, data)


@pytest.mark.parametrize("cached", ["600", "9000", None])
def test_formula_input_blocks_cached_fresh_and_missing_values_before_compute(released, tmp_path, monkeypatch, cached):
    source, releases = released
    data = tmp_path / "data.xlsx"
    formula_workbook(source / "valid.xlsx", data, cached=cached)
    monkeypatch.setattr(Engine, "compute", lambda *a, **kw: pytest.fail("never calculate using formula caches"))
    with pytest.raises(FormulaInputError, match="销售!B2") as exc:
        run_fixed("annual_bonus", "v1", "2026-Q3", [data], tmp_path / "batch", releases=releases)
    assert exc.value.code == "formula_input"
    assert not list((tmp_path / "batch").rglob("result.xlsx"))


def test_publication_rejects_formula_acceptance_and_accepts_explicit_formula_negative(tmp_path):
    source = fixture_package(tmp_path / "source")
    replacement = tmp_path / "replacement.xlsx"
    formula_workbook(source / "valid.xlsx", replacement)
    original = (source / "valid.xlsx").read_bytes()
    (source / "valid.xlsx").write_bytes(replacement.read_bytes())
    releases = tmp_path / "releases"
    with pytest.raises(ReleaseError, match="公式"):
        publish(source, releases, "finance")
    assert not (releases / "annual_bonus" / "v1").exists()
    (source / "valid.xlsx").write_bytes(original)
    (source / "formula.xlsx").write_bytes(replacement.read_bytes())
    spec = yaml.safe_load((source / "release.yaml").read_text())
    spec["cases"].append({"id": "stale_formula", "data": ["formula.xlsx"], "error_code": "formula_input"})
    (source / "release.yaml").write_text(yaml.safe_dump(spec))
    publish(source, releases, "finance")


@pytest.mark.parametrize("cell", ["A1", "B4"])
def test_formula_scan_includes_headers_and_hidden_rows(released, tmp_path, cell):
    source, releases = released
    hidden = tmp_path / "hidden.xlsx"
    workbook = openpyxl.load_workbook(source / "valid.xlsx")
    workbook["销售"].row_dimensions[4].hidden = True
    workbook["销售"].column_dimensions["B"].hidden = True
    workbook.save(hidden)
    data = tmp_path / "data.xlsx"
    formula_workbook(hidden, data, cell=cell)
    with pytest.raises(FormulaInputError, match=f"销售!{cell}"):
        run_fixed("annual_bonus", "v1", "2026-Q3", [data], tmp_path / "batch", releases=releases)


@pytest.mark.parametrize("mode", ["native", "json"])
@pytest.mark.parametrize("query", [False, True])
def test_assistant_renders_only_engine_evidence_never_model_amounts(released, tmp_path, mode, query):
    from bonus_agent.scenarios.assistant import answer
    source, releases = released
    result = run_fixed("annual_bonus", "v1", "2026-Q3", [source / "valid.xlsx"], tmp_path / "batch", releases=releases).result
    class Forged:
        tool_mode = mode
        calls = 0

        def chat_tools(self, *a, **kw):
            self.calls += 1
            return {"content": "本次实发999999元，已修改公式。", "tool_calls":
                    [{"id": "q", "name": "explain_object", "arguments": '{"node_id":"个人:E2"}'}]
                    if query and self.calls == 1 else []}

        def chat(self, *a, **kw):
            response = self.chat_tools()
            return json.dumps({"tool": "explain_object", "args": {"node_id": "个人:E2"}}
                              if response["tool_calls"] else {"answer": response["content"]})

    text = answer(Forged(), "annual_bonus", "v1", "E2发多少", releases=releases, result=result)
    assert "999999" not in text and "已修改公式" not in text
    if query:
        assert "个人:E2" in text and "本次实发：150.00 元" in text
        assert "应得：200.00 元" in text
    else:
        assert "未取得可核验" in text


def test_assistant_without_result_cannot_invent_amounts(released):
    from bonus_agent.llm import FakeLLM
    from bonus_agent.scenarios.assistant import answer
    _, releases = released
    text = answer(FakeLLM(["本次实发999999元"]), "annual_bonus", "v1", "发多少", releases=releases)
    assert "尚未计算" in text and "999999" not in text


def test_assistant_does_not_echo_forged_tool_names_or_query_parameters(released):
    from bonus_agent.llm import FakeLLM
    from bonus_agent.scenarios.assistant import answer
    _, releases = released
    llm = FakeLLM([{"tool": "本次实发999999元"}])
    text = answer(llm, "annual_bonus", "v1", "查金额", releases=releases)
    assert "未授权工具" in text and "999999" not in text


def test_production_web_uses_fixed_rules_and_read_only_chat(released, tmp_path):
    from fastapi.testclient import TestClient
    from bonus_agent.web import create_app
    from bonus_agent.llm import FakeLLM
    source, releases = released
    llm = FakeLLM([{"tool": "set_rules", "args": {"pool": {"expr": "999"}}}])
    app = create_app(tmp_path / "web", lambda: llm, releases=releases)
    store = app.state.store
    store.users.add("finance", "password-123", ["editor", "approver"])
    client = TestClient(app)
    client.post("/login", data={"username": "finance", "password": "password-123"})
    csrf = store.csrf(client.cookies.get("bonus_session"))
    def post(url, **data):
        return client.post(url, data={"csrf": csrf, **data}, follow_redirects=False)
    assert post("/cases", name="wrong", scenario="annual_bonus", rule_version="sales_commission@v1", period="2026-Q3").status_code == 400
    response = post("/cases", name="batch", scenario="annual_bonus", rule_version="annual_bonus@v1", period="2026-Q3")
    cid = response.headers["location"].split("/")[-1]
    with (source / "valid.xlsx").open("rb") as handle:
        client.post(f"/cases/{cid}/materials", data={"csrf": csrf}, files={"files": ("valid.xlsx", handle)}, follow_redirects=False)
    for attempt in range(2):
        assert post(f"/cases/{cid}/run").status_code == 303
        job = app.state.runner.jobs[cid]; job.thread.join(20)
        assert job.result == "OK", job.message
    assert not llm.calls  # monetary execution must never construct/call a model
    state = json.loads((store.dir(cid) / "out/fixed_run.json").read_text())
    assert state["cached"]
    assert "固定计算完成" in client.get("/").text
    assert post(f"/cases/{cid}/chat", text="改奖金池").status_code == 303
    app.state.runner.jobs[cid].thread.join(20)
    assert "禁止工具 set_rules" in store.chat(cid)[-1]["text"]
    assert not (store.dir(cid) / "out/case.json").exists()
    assert post(f"/cases/{cid}/confirm-fixed").status_code == 303
    assert (store.dir(cid) / "runs" / state["task_id"] / "confirmed.json").exists()
    assert post(f"/cases/{cid}/product-approval").status_code == 409
    assert post(f"/cases/{cid}/settle").status_code == 409
    assert post(f"/cases/{cid}/confirm").status_code == 409
    assert post(f"/cases/{cid}/template").status_code == 409
    assert post(f"/cases/{cid}/materials/delete", name="valid.xlsx").status_code == 303
    assert post(f"/cases/{cid}/confirm-fixed").status_code == 409


def test_web_honors_release_directory_environment(released, tmp_path, monkeypatch):
    from bonus_agent.web import create_app
    _, releases = released
    monkeypatch.setenv("BONUS_SCENARIO_RELEASES_DIR", str(releases))
    app = create_app(tmp_path / "web", lambda: pytest.fail("must not initialize a model"))
    assert app.state.runner.releases == releases.resolve()


def test_cli_fixed_computation_never_initializes_model(released, tmp_path, monkeypatch):
    from bonus_agent.cli import main
    from bonus_agent.llm import LLM
    source, releases = released
    monkeypatch.setattr(LLM, "from_config", lambda *a, **k: pytest.fail("no model for fixed calculation"))
    args = ["auto", "--scenario", "annual_bonus", "--rule-version", "v1", "--period", "2026-Q3",
            "--release-dir", str(releases), "--out", str(tmp_path / "batch"), str(source / "valid.xlsx")]
    assert main(args) == main(args) == 0
    assert main(["auto", "--scenario", "annual_bonus", "--out", str(tmp_path / "pending"), str(source / "valid.xlsx")]) == 1


@pytest.mark.parametrize("command", ["confirm", "approve-settlement"])
def test_legacy_approval_cannot_bypass_fixed_publication(command, tmp_path, monkeypatch):
    from bonus_agent.cli import main
    from bonus_agent import cli
    monkeypatch.setattr(cli, "load_config", lambda: pytest.fail("block before loading the legacy workflow"))
    assert main([command, "--scenario", "sales_commission", "--by", "finance",
                 "--out", str(tmp_path / "legacy")]) == 1
    assert not (tmp_path / "legacy").exists()
