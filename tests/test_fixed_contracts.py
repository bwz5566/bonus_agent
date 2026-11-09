"""Business date, cross-case batch and approval/catalog regression tests."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json
from pathlib import Path
import shutil

import openpyxl
import pytest
import yaml
from fastapi.testclient import TestClient

from test_fixed_scenario_execution import fixture_package
from bonus_agent.scenarios.releases import publish, ReleaseError
from bonus_agent.scenarios.execution import run_fixed, task_key
from bonus_agent.scenarios.batches import Registry, BatchError
from bonus_agent.scenarios.periods import PeriodError
from bonus_agent.scenarios.approval import ApprovalError, record_product_approval, resolve_product_approval
from bonus_agent.scenarios import load_pack
from bonus_agent.web import create_app
from bonus_core.engine.run import Engine


@pytest.fixture
def package(tmp_path):
    source = fixture_package(tmp_path / "source")
    releases = tmp_path / "releases"
    return source, releases


def approve(source, releases):
    publish(source, releases, "finance")
    registry = Registry(releases)
    return registry, registry.register("annual_bonus", "v1", "2026-Q3", "测试公司", "ANNUAL-001", "finance")["key"]


def update_spec(source, change):
    path = source / "release.yaml"
    spec = yaml.safe_load(path.read_text())
    change(spec)
    path.write_text(yaml.safe_dump(spec,allow_unicode=True,sort_keys=False))


def input_date(source, tmp_path, value):
    wb = openpyxl.load_workbook(source / "valid.xlsx")
    wb["销售"]["C2"] = value
    path = tmp_path / "renamed.xlsx"
    wb.save(path)
    return path


@pytest.mark.parametrize("value", [date(2026,6,30), date(2026,10,1), None])
def test_period_rejects_outside_dates_and_missing_dates_before_amount_engine(package, tmp_path, monkeypatch, value):
    source, releases = package
    _, batch = approve(source,releases)
    data = input_date(source,tmp_path,value)
    monkeypatch.setattr(Engine,"compute",lambda *a,**k: pytest.fail("period must block before money"))
    with pytest.raises(PeriodError):
        run_fixed("annual_bonus","v1","2026-Q3",[data],tmp_path/"case/runs",releases=releases,business_batch=batch)
    assert not list((tmp_path/"case").rglob("result.xlsx"))


def test_period_boundaries_master_data_exemption_and_approved_filter_are_audited(package, tmp_path):
    source, releases = package
    update_spec(source,lambda s:s["period_contracts"]["销售"].update(action="filter",reason="财务批准：仅纳入当期业务"))
    _,batch=approve(source,releases)
    data=input_date(source,tmp_path,date(2026,10,1))
    run=run_fixed("annual_bonus","v1","2026-Q3",[data],tmp_path/"case/runs",releases=releases,business_batch=batch)
    assert run.result.nodes["个人:E1"].final == "40.00"
    report=json.loads((run.directory/"run.json").read_text())["period_validation"]
    assert report["tables"]["员工"]["kept_count"] == 2
    assert report["tables"]["销售"]["excluded"][0]["values"]["amount"] == "600"
    workbook=openpyxl.load_workbook(run.directory/"result.xlsx",data_only=True)
    assert workbook["跨期排除明细"].max_row == 2
    assert "期间校验" in workbook.sheetnames


@pytest.mark.parametrize("mode", ["as_of","window"])
def test_historical_and_cumulative_dates_follow_finance_contract(package,tmp_path,mode):
    source,releases=package
    def change(s):
        policy=s["period_contracts"]["销售"]
        policy["mode"]=mode
        if mode == "window":
            policy["windows"]={p:{"start":"2026-01-01","end_exclusive":w["end_exclusive"]} for p,w in s["period_windows"].items()}
    update_spec(source,change)
    _,batch=approve(source,releases)
    data=input_date(source,tmp_path,date(2026,1,1))
    run=run_fixed("annual_bonus","v1","2026-Q3",[data],tmp_path/"case/runs",releases=releases,business_batch=batch)
    assert run.result.nodes["个人:E1"].final == "100.00"


@pytest.mark.parametrize("fault", ["missing_table","unknown_field","missing_window","unapproved_filter"])
def test_incomplete_date_contract_cannot_be_published(package,fault):
    source,releases=package
    def change(s):
        if fault=="missing_table": del s["period_contracts"]["员工"]
        if fault=="unknown_field": s["period_contracts"]["销售"]["field"]="not_a_field"
        if fault=="missing_window": del s["period_windows"]["2026-Q3"]
        if fault=="unapproved_filter": s["period_contracts"]["销售"]["action"]="filter"
    update_spec(source,change)
    with pytest.raises((ReleaseError,PeriodError)):
        publish(source,releases,"finance")
    assert not (releases/"annual_bonus/v1").exists()


def test_publication_executes_period_negative_case_and_refuses_cross_period_gold(package,tmp_path):
    source,releases=package
    mixed=input_date(source,tmp_path,date(2026,10,1))
    shutil.copyfile(mixed,source/"mixed.xlsx")
    update_spec(source,lambda s:s["cases"].append({"id":"cross_period","period":"2026-Q3","data":["mixed.xlsx"],"error_code":"period_contract"}))
    publish(source,releases,"finance")
    other=fixture_package(tmp_path/"badgold",version="v2")
    shutil.copyfile(mixed,other/"valid.xlsx")
    with pytest.raises(ReleaseError,match="非预期失败"):
        publish(other,releases,"finance")


def test_business_batch_blocks_cross_case_rename_and_version_bypass(package,tmp_path,monkeypatch):
    source,releases=package
    registry,batch=approve(source,releases)
    first=run_fixed("annual_bonus","v1","2026-Q3",[source/"valid.xlsx"],tmp_path/"case-a/runs",releases=releases,business_batch=batch)
    other=fixture_package(tmp_path/"v2",version="v2")
    publish(other,releases,"finance")
    renamed=tmp_path/"newname.xlsx"; shutil.copyfile(source/"valid.xlsx",renamed)
    assert registry.register("annual_bonus","v2","2026-Q3","测试公司","ANNUAL-001","finance")["key"] == batch
    monkeypatch.setattr(Engine,"compute",lambda *a,**k:pytest.fail("duplicate case must not calculate"))
    with pytest.raises(BatchError,match="batch_duplicate"):
        run_fixed("annual_bonus","v2","2026-Q3",[renamed],tmp_path/"case-b/runs",releases=releases,business_batch=batch)
    assert run_fixed("annual_bonus","v1","2026-Q3",[source/"valid.xlsx"],tmp_path/"case-a/runs",releases=releases,business_batch=batch).cached
    assert first.result.nodes["个人:E2"].final == "200.00"


def test_batch_registration_and_binding_are_atomic_across_instances(package,tmp_path):
    source,releases=package
    registry,batch=approve(source,releases)
    def bind(i):
        try:
            Registry(releases).bind(batch,tmp_path/f"case-{i}/runs")
            return "bound"
        except BatchError as exc:
            return exc.code
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(bind,range(2))) == ["batch_duplicate","bound"]
    owner=registry.get(batch)["owner"]
    token=registry.begin(batch,owner,"revision-one")
    with pytest.raises(BatchError,match="batch_busy"):
        Registry(releases).begin(batch,owner,"revision-two")
    registry.release(batch,token)


def test_confirmation_is_single_and_revisions_stay_in_original_batch(package,tmp_path):
    source,releases=package
    registry,batch=approve(source,releases); runs=tmp_path/"case/runs"
    first=run_fixed("annual_bonus","v1","2026-Q3",[source/"valid.xlsx"],runs,releases=releases,business_batch=batch)
    workbook=openpyxl.load_workbook(source/"valid.xlsx");workbook["销售"]["B2"]=700
    revised=tmp_path/"revised.xlsx";workbook.save(revised)
    second=run_fixed("annual_bonus","v1","2026-Q3",[revised],runs,releases=releases,business_batch=batch)
    assert second.task_id != first.task_id and second.result.nodes["个人:E1"].final=="110.00"
    with pytest.raises(BatchError,match="batch_revision_changed"):
        registry.confirm(batch,runs,first.task_id,{"task_id":first.task_id})
    record={"task_id":second.task_id,"by":"finance"}
    assert registry.confirm(batch,runs,second.task_id,record)==record
    assert registry.confirm(batch,runs,second.task_id,{"by":"other"})==record
    with pytest.raises(BatchError,match="batch_confirmed"):
        run_fixed("annual_bonus","v1","2026-Q3",[source/"valid.xlsx"],runs,releases=releases,business_batch=batch)


def test_unregistered_or_wrong_period_batch_cannot_run(package,tmp_path,monkeypatch):
    source,releases=package;_,batch=approve(source,releases)
    monkeypatch.setattr(Engine,"compute",lambda *a,**k:pytest.fail("batch must block first"))
    with pytest.raises(BatchError,match="batch_missing"):
        run_fixed("annual_bonus","v1","2026-Q3",[source/"valid.xlsx"],tmp_path/"runs",releases=releases)
    with pytest.raises(ReleaseError,match="不一致"):
        task_key("annual_bonus","v1","2026-Q4",[source/"valid.xlsx"],releases=releases,business_batch=batch)


def test_approval_file_requires_matching_structured_decision(tmp_path,monkeypatch):
    monkeypatch.delenv("BONUS_AGENT_STATE_DIR",raising=False)
    approval=record_product_approval("sales_commission","product-owner")
    pack=load_pack("sales_commission")
    assert resolve_product_approval(pack,approval)
    monkeypatch.setenv("BONUS_AGENT_STATE_DIR",str(tmp_path/"state"))
    monkeypatch.setenv("BONUS_AGENT_STATE_SCOPE","finance-test")
    with pytest.raises(ApprovalError,match="structured decision") as exc:
        resolve_product_approval(pack,approval)
    assert exc.value.code=="approval_decision_missing" and "finance-test" in str(exc.value)
    approved=record_product_approval("sales_commission","product-owner")
    assert resolve_product_approval(pack,approved)["decision_id"]==approved["decision_id"]
    approval["binding_fingerprint"]="a"*64
    with pytest.raises(ApprovalError) as exc:
        resolve_product_approval(pack,approval)
    assert exc.value.code=="approval_binding_mismatch"


@pytest.mark.parametrize("exploration", [False,True])
@pytest.mark.parametrize("fault", ["metadata","published"])
def test_bad_scenario_isolated_while_other_scenarios_stay_available(package,tmp_path,monkeypatch,exploration,fault):
    import bonus_agent.scenarios as scenarios
    source,releases=package;publish(source,releases,"finance")
    good=fixture_package(tmp_path/"good","eng_dev_bonus");publish(good,releases,"finance")
    shutil.copytree(scenarios.PACKS,tmp_path/"packs")
    monkeypatch.setattr(scenarios,"PACKS",tmp_path/"packs")
    if fault=="metadata": (scenarios.PACKS/"annual_bonus/pack.yaml").write_text("scenario: [broken")
    else: (releases/"annual_bonus/v1/publication.json").write_text("broken")
    app=create_app(tmp_path/"web",lambda:None,releases=releases,exploration=exploration)
    app.state.store.users.add("finance","password-123",["editor","approver"])
    client=TestClient(app,raise_server_exceptions=False)
    client.post("/login",data={"username":"finance","password":"password-123"},follow_redirects=False)
    response=client.get("/")
    assert response.status_code==200 and "配置异常" in response.text
    assert ("value='eng_dev_bonus'" if exploration else "eng_dev_bonus@v1") in response.text
    assert "annual_bonus@v1" not in response.text
    if not exploration:
        csrf=app.state.store.csrf(client.cookies.get("bonus_session"))
        def post(path,**body):
            return client.post(path,data={"csrf":csrf,**body},follow_redirects=False)
        assert post("/batches",binding="eng_dev_bonus@v1",entity="测试公司",period="2026-Q3",batch_id="DEV-001").status_code==303
        batch=Registry(releases).list()[0]["key"]
        fields=dict(name="healthy",scenario="eng_dev_bonus",rule_version="eng_dev_bonus@v1",period="2026-Q3",business_batch=batch)
        created=post("/cases",**fields)
        assert created.status_code==303
        assert post("/cases",**fields).status_code==400
        cid=created.headers["location"].split("/")[-1]
        with (good/"valid.xlsx").open("rb") as handle:
            assert client.post(f"/cases/{cid}/materials",data={"csrf":csrf},files={"files":("valid.xlsx",handle)},follow_redirects=False).status_code==303
        assert post(f"/cases/{cid}/run").status_code==303
        app.state.runner.jobs[cid].thread.join(20)
        assert app.state.runner.jobs[cid].result=="OK"


def test_web_revision_and_confirmation_use_shared_batch_authority(package,tmp_path):
    source,releases=package;registry,batch=approve(source,releases)
    other=fixture_package(tmp_path/"v2",version="v2");publish(other,releases,"finance")
    app=create_app(tmp_path/"web",lambda:pytest.fail("fixed runs do not need model"),releases=releases)
    app.state.store.users.add("editor","password-123",["editor"])
    app.state.store.users.add("approver","password-123",["editor","approver"])
    client=TestClient(app)
    def login(name):
        client.post("/login",data={"username":name,"password":"password-123"},follow_redirects=False)
    def post(path,**body):
        csrf=app.state.store.csrf(client.cookies.get("bonus_session"))
        return client.post(path,data={"csrf":csrf,**body},follow_redirects=False)
    login("editor")
    assert post("/batches",binding="annual_bonus@v1",entity="测试公司",period="2026-Q3",batch_id="UNAUTHORIZED").status_code==403
    created=post("/cases",name="batch",scenario="annual_bonus",rule_version="annual_bonus@v1",period="2026-Q3",business_batch=batch)
    cid=created.headers["location"].split("/")[-1]
    with (source/"valid.xlsx").open("rb") as handle:
        client.post(f"/cases/{cid}/materials",data={"csrf":app.state.store.csrf(client.cookies.get("bonus_session"))},files={"files":("valid.xlsx",handle)},follow_redirects=False)
    def run():
        assert post(f"/cases/{cid}/run").status_code==303
        app.state.runner.jobs[cid].thread.join(20)
        assert app.state.runner.jobs[cid].result=="OK"
    run()
    old=json.loads((app.state.store.dir(cid)/"out/fixed_run.json").read_text())["task_id"]
    assert post(f"/cases/{cid}/rule-version",version="v2").status_code==303
    login("approver")
    assert post(f"/cases/{cid}/confirm-fixed").status_code==409
    run()
    assert post(f"/cases/{cid}/confirm-fixed").status_code==303
    record=json.loads(registry.get(batch)["confirmation"])
    assert record["task_id"]!=old
    assert post(f"/cases/{cid}/rule-version",version="v1").status_code==409


def test_cli_batch_registration_and_cross_entry_dedup(package,tmp_path,capsys,monkeypatch):
    from bonus_agent import cli
    from bonus_core.cli import main as core_main
    source,releases=package;publish(source,releases,"finance")
    monkeypatch.setattr(cli,"load_config",lambda:pytest.fail("no model/config for business registration and calculation"))
    assert cli.main(["register-batch","--scenario","annual_bonus","--rule-version","v1","--period","2026-Q3",
        "--entity","测试公司","--batch-id","CLI-001","--by","finance","--release-dir",str(releases)])==0
    key=capsys.readouterr().out.strip()
    common=["--scenario","annual_bonus","--rule-version","v1","--period","2026-Q3","--release-dir",str(releases),"--business-batch",key]
    args=["auto",*common,"--out",str(tmp_path/"case"),str(source/"valid.xlsx")]
    assert cli.main(args)==cli.main(args)==0
    assert core_main(["run",*common,"--out",str(tmp_path/"other.xlsx"),str(source/"valid.xlsx")])==1
    assert "batch_duplicate" in capsys.readouterr().out
