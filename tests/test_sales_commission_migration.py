"""Migrated quarter binding, product approval, and the settlement gate."""
import json
from pathlib import Path

import pytest
import yaml

from bonus_agent.case import ConfirmError, confirm
from bonus_agent.scenarios import (
    PACKS,
    ScenarioError,
    list_scenarios,
    load_pack,
    record_product_approval,
    require_settlement,
)
from bonus_agent.scenarios.binding import BindingError, compose
from bonus_agent.scenarios.settle import materialize_template, settle_bound

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "evals" / "cases" / "syn_acceptance" / "inputs" / "03_业务输入.xlsx"


def _approval(tmp_path, **kwargs):
    return record_product_approval("sales_commission", "product-owner", approval_out=tmp_path / "approval.json", **kwargs)


def test_contract_fields_are_a_subset_of_existing_materials():
    origin = yaml.safe_load((ROOT / "evals/cases/syn_acceptance/datasets.yaml").read_text(encoding="utf-8"))
    pack = load_pack("sales_commission")
    assert set(pack.input_contract.excluded_datasets) == {"里程碑", "历史已发"}
    assert "perf_fy" in pack.input_contract.excluded_fields["小组"]
    for name, fields in pack.input_contract.datasets.items():
        assert name in origin
        assert set(fields) <= set(origin[name]["fields"])
        assert "seniority" not in fields and "gross_margin" not in fields
    assert "difficulty" in pack.input_contract.datasets["项目"]
    assert pack.rule_package.finance_overlay is None
    assert pack.rule_package.separate_pack_todo[0].pack_id == "process_bonus"
    assert "process_bonus" not in list_scenarios()
    assert {item.role for item in pack.metadata.migration.origins} >= {"scheme", "shared_library", "datasets", "mapping", "golden"}


def test_binding_selects_quarter_only_and_matches_reference(tmp_path):
    pack = load_pack("sales_commission")
    bound = compose(pack)
    text = bound.ruleset.model_dump_json()
    for excluded in ("里程碑", "历史已发", "perf_fy", "scheme_paid", "R_DIFF"):
        assert excluded not in text
    assert bound.ruleset.name.startswith("QUARTER")
    assert [item["name"] for item in bound.revisions] == ["R1", "R2"]
    approval = _approval(tmp_path)
    assert approval["finance_signed"] is False and approval["binding_fingerprint"] == bound.fingerprint
    from evals.cases.syn_acceptance import run_case
    results = settle_bound("sales_commission", [DATA], tmp_path / "result.xlsx", tmp_path / "approval.json")
    reference = json.loads((run_case.HERE / "reference.json").read_text(encoding="utf-8"))
    assert run_case.diff_reference({"QUARTER": results}, {"QUARTER": reference["QUARTER"]}) == []
    assert json.loads((tmp_path / "result.settlement.json").read_text(encoding="utf-8"))["finance_signed"] is False


def test_product_approval_is_required_and_fingerprint_is_checked(tmp_path):
    with pytest.raises(ScenarioError, match="product written approval required"):
        require_settlement("sales_commission")
    approval = _approval(tmp_path)
    assert require_settlement("sales_commission", tmp_path / "approval.json").metadata.status == "migrated_draft"
    approval["binding_fingerprint"] = "0" * 64
    (tmp_path / "stale.json").write_text(json.dumps(approval), encoding="utf-8")
    with pytest.raises(ScenarioError, match="does not match the current migrated binding"):
        require_settlement("sales_commission", tmp_path / "stale.json")
    approval["statement"] = "Finance has published this pack."
    approval["binding_fingerprint"] = load_pack("sales_commission").metadata and compose(load_pack("sales_commission")).fingerprint
    (tmp_path / "claim.json").write_text(json.dumps(approval), encoding="utf-8")
    with pytest.raises(ScenarioError, match="Invalid product approval"):
        require_settlement("sales_commission", tmp_path / "claim.json")
    with pytest.raises(BindingError, match="migrated_draft"):
        record_product_approval("annual_bonus", "product-owner", approval_out=tmp_path / "annual.json")
    with pytest.raises(BindingError, match="migrated_draft"):
        record_product_approval("eng_dev_bonus", "product-owner", approval_out=tmp_path / "eng.json")


def test_structured_state_is_the_authority(tmp_path, monkeypatch):
    monkeypatch.setenv("BONUS_AGENT_STATE_DIR", str(tmp_path / "platform"))
    record_product_approval("sales_commission", "product-owner", out=tmp_path / "case")
    assert require_settlement("sales_commission").metadata.scenario == "sales_commission"
    copied = json.loads((tmp_path / "case" / "product_approval.json").read_text(encoding="utf-8"))
    copied["decision_id"] = None
    (tmp_path / "file-only.json").write_text(json.dumps(copied), encoding="utf-8")
    with pytest.raises(ScenarioError, match="structured state"):
        require_settlement("sales_commission", tmp_path / "file-only.json")


def test_confirm_does_not_freeze_agent_drafts(tmp_path):
    _approval(tmp_path, out=tmp_path)
    with pytest.raises(ConfirmError, match="does not freeze an agent round"):
        confirm(tmp_path, "finance", scenario="sales_commission")
    assert not (tmp_path / "template").exists()


def test_formal_run_uses_the_pack_binding(tmp_path):
    from bonus_core.cli import main
    out = tmp_path / "result.xlsx"
    assert main(["run", "--scenario", "sales_commission", "--from-pack", "--out", str(out), str(DATA)]) == 1
    assert not out.exists()
    _approval(tmp_path)
    assert main(["run", "--scenario", "sales_commission", "--from-pack", "--rules", "unused", "--mapping", "unused",
                 "--product-approval", str(tmp_path / "approval.json"), "--out", str(out), str(DATA)]) == 1
    assert not out.exists()
    assert main(["run", "--scenario", "sales_commission", "--from-pack", "--product-approval",
                 str(tmp_path / "approval.json"), "--out", str(out), str(DATA)]) == 0
    assert out.exists() and (tmp_path / "result_R2.xlsx").exists()


def test_template_settlement_requires_the_same_binding(tmp_path):
    from bonus_core.template import Template
    approval = _approval(tmp_path)
    dest = materialize_template("sales_commission", tmp_path / "template", approval)
    results, _ = Template.load(dest).run([DATA])
    assert set(results) == {"QUARTER"} and set(results["QUARTER"]) == {"R0", "R1", "R2"}
    (dest / "rules.yaml").write_text("name: other\n", encoding="utf-8")
    with pytest.raises(Exception):
        Template.load(dest).run([DATA])


def test_exploration_does_not_call_the_settlement_gate(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr("bonus_agent.scenarios.require_settlement", lambda *args, **kwargs: called.append(args))
    monkeypatch.setattr("bonus_agent.cli._agent", lambda *args, **kwargs: 0)
    from bonus_agent.cli import main
    assert main(["auto", "--scenario", "sales_commission", "--out", str(tmp_path), "notes.xlsx"]) == 0
    assert called == []


def test_unknown_pack_fields_still_fail(tmp_path):
    shutil_copy = __import__("shutil").copytree
    shutil_copy(PACKS / "annual_bonus", tmp_path / "annual_bonus")
    (tmp_path / "annual_bonus" / "pack.yaml").write_text(
        "schema_version: 1\nscenario: annual_bonus\nstatus: published\ntodo: TODO\n", encoding="utf-8")
    with pytest.raises(ScenarioError, match="Invalid scenario pack"):
        load_pack("annual_bonus", root=tmp_path)
    (tmp_path / "annual_bonus" / "pack.yaml").write_text(
        "schema_version: 1\nscenario: annual_bonus\nstatus: pending_finance\ntodo: TODO\nextra: 1\n", encoding="utf-8")
    with pytest.raises(ScenarioError, match="Invalid scenario pack"):
        load_pack("annual_bonus", root=tmp_path)


def test_web_records_approval_and_settles_without_calling_the_model(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from bonus_agent.web import create_app
    app = create_app(tmp_path, lambda: pytest.fail("must not call LLM"))
    store = app.state.store
    store.users.add("owner", "password-123", ["editor", "approver"], "product-owner")
    client = TestClient(app)
    client.post("/login", data={"username": "owner", "password": "password-123"})
    csrf = store.csrf(client.cookies.get("bonus_session"))

    def post(url, **data):
        return client.post(url, data={"csrf": csrf, **data}, follow_redirects=False)

    created = post("/cases", name="quarter", scenario="sales_commission")
    assert created.status_code == 303
    cid = created.headers["location"].split("/")[-1]
    page = client.get(f"/cases/{cid}").text
    assert "migrated_draft" in page and "记录产品书面批准" in page
    assert "pending_finance" not in page
    blocked = post(f"/cases/{cid}/settle")
    assert blocked.status_code == 400
    assert post(f"/cases/{cid}/product-approval").status_code == 303
    approved = client.get(f"/cases/{cid}").text
    assert "已记录产品书面批准" in approved and "不是财务签发" in approved
    with DATA.open("rb") as handle:
        response = client.post(f"/cases/{cid}/settle", data={"csrf": csrf},
                               files={"files": ("03_业务输入.xlsx", handle, "application/vnd.ms-excel")},
                               follow_redirects=False)
    assert response.status_code == 303
    done = client.get(response.headers["location"]).text
    assert "已按迁入规则结算" in done
    runs = list((store.dir(cid) / "runs").glob("*/result.xlsx"))
    assert len(runs) == 1
