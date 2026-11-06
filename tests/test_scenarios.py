"""Phase A infrastructure tests; no finance contracts or business rules."""
import shutil

import pytest

from bonus_agent.scenarios import PACKS, ScenarioError, list_scenarios, load_pack, require_settlement


@pytest.mark.parametrize("scenario", ["sales_commission", "eng_dev_bonus", "annual_bonus"])
def test_placeholder_schema_and_settlement_gate(scenario):
    pack = load_pack(scenario)
    assert pack.metadata.scenario == scenario
    assert pack.rule_package.status == "pending_finance"
    with pytest.raises(ScenarioError, match="pending_finance.*published rules missing"):
        require_settlement(scenario)


@pytest.mark.parametrize("scenario", [None, "", "universal-auto", "../annual_bonus", "unknown"])
def test_no_default_or_inferred_scenario(scenario):
    with pytest.raises(ScenarioError):
        require_settlement(scenario)


def test_invalid_and_missing_components(tmp_path):
    shutil.copytree(PACKS / "annual_bonus", tmp_path / "annual_bonus")
    rule = tmp_path / "annual_bonus" / "rule_package.yaml"
    rule.write_text("status: published\ntodo: TODO\n")
    with pytest.raises(ScenarioError, match="Invalid scenario pack"):
        load_pack("annual_bonus", root=tmp_path)
    rule.unlink()
    with pytest.raises(ScenarioError, match="Cannot load"):
        load_pack("annual_bonus", root=tmp_path)


def test_confirm_blocks_before_creating_artifacts(tmp_path):
    from bonus_agent.case import confirm, ConfirmError
    for scenario in (None, *list_scenarios()):
        with pytest.raises(ConfirmError):
            confirm(tmp_path, "finance", scenario=scenario)
    assert not list(tmp_path.iterdir())


def test_template_api_cannot_bypass_gate(tmp_path):
    from bonus_core.template import Template
    for scenario in (None, "annual_bonus"):
        template = Template(tmp_path, {"scenario": scenario})
        with pytest.raises(ScenarioError):
            template.run([])
        with pytest.raises(ScenarioError):
            template.write({}, tmp_path / "output")
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("cmd", ["auto", "confirm"])
def test_agent_cli_requires_scenario(cmd):
    from bonus_agent.cli import main
    args = [cmd, "--out", "unused"] + (["input.xlsx"] if cmd == "auto" else ["--by", "finance"])
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2


@pytest.mark.parametrize("cmd", ["run", "run-template"])
def test_core_cli_requires_and_rejects_pending_scenario(cmd, tmp_path):
    from bonus_core.cli import main
    args = [cmd, "--out", str(tmp_path / "output"), "input.xlsx"]
    args += ["--template", "unused"] if cmd == "run-template" else ["--rules", "unused", "--mapping", "unused"]
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2
    assert main(args + ["--scenario", "annual_bonus"]) == 1
    assert not (tmp_path / "output").exists()


def test_web_requires_explicit_scenario_and_blocks_publication(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from bonus_agent.web import create_app
    app = create_app(tmp_path, lambda: pytest.fail("must not call LLM"))
    store = app.state.store
    store.users.add("finance", "password-123", ["editor", "approver"])
    c = TestClient(app)
    c.post("/login", data={"username": "finance", "password": "password-123"})
    csrf = store.csrf(c.cookies.get("bonus_session"))
    def post(url, **data):
        return c.post(url, data={"csrf": csrf, **data}, follow_redirects=False)
    assert post("/cases", name="pending").status_code == 400
    assert post("/cases", name="pending", scenario="universal-auto").status_code == 400
    response = post("/cases", name="pending", scenario="annual_bonus")
    assert response.status_code == 303
    cid = response.headers["location"].split("/")[-1]
    assert store.meta(cid)["scenario"] == "annual_bonus"
    response = post(f"/cases/{cid}/confirm")
    assert "pending_finance" in c.get(response.headers["location"]).text
    assert post(f"/cases/{cid}/template").status_code == 400
    assert not (store.dir(cid) / "out" / "template").exists()
