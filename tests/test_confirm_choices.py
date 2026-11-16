"""确认时询问是否录入模板、录入哪些人工调整；模板和处理中的目录写明请勿修改；同名材料直接拒绝。"""
import shutil

import pytest
import yaml

from bonus_agent import review as rv
from bonus_agent.agent import run_agent
from bonus_agent.case import (PROCESSING_NOTICE, TEMPLATE_NOTICE, ConfirmError, confirm, confirm_options, fingerprint,
                              load_case)
from bonus_agent.llm import FakeLLM
from bonus_core.data.files import DuplicateFileName
from bonus_core.template import Template
from tests.test_syn_acceptance import _script, run_case


@pytest.fixture
def case_dir(tmp_path):
    out = tmp_path / "案卷"
    seen = []
    ar = run_agent(FakeLLM(_script()), [run_case.XLSX], out,
                   on_step=lambda n, s: seen.append((out / PROCESSING_NOTICE).exists()))
    assert ar.status == "OK"
    assert seen and all(seen)                                   # 处理期间一直有“请勿修改”提示
    assert not (out / PROCESSING_NOTICE).exists()               # 结束后删除
    rv.mark(out, {r["key"]: (rv.OK, "") for r in rv.load(out)}, "财务甲")
    return out


def test_confirm_asks_which_adjustments_to_save_and_template_says_do_not_edit(case_dir):
    opts = confirm_options(case_dir)
    assert not opts["has_template"] and any("首次录入" in x for x in opts["rule_changes"])
    assert {r["key"]: r["required_by"] for r in opts["revisions"]} == {"QUARTER/R1": [], "QUARTER/R2": ["ANNUAL"]}
    assert all(r["new"] and r["adjustments"] for r in opts["revisions"]) and "是否把本轮结果录入模板" in opts["ask"]
    with pytest.raises(ConfirmError, match="修订：QUARTER/R1。.*逐个选择"):  # R2 被 ANNUAL 引用，不算可选
        confirm(case_dir, "财务甲")                              # 不默认把人工调整带进以后每一期
    with pytest.raises(ConfirmError, match="没有这些修订"):
        confirm(case_dir, "财务甲", revisions=["QUARTER/R9"])
    tpl = confirm(case_dir, "财务甲", revisions=[])            # 只录入必须的 R2
    m = yaml.safe_load((tpl / "manifest.yaml").read_text(encoding="utf-8"))
    quarter = next(s for s in m["schemes"] if s["name"] == "QUARTER")
    assert [r["name"] for r in quarter["revisions"]] == ["R2"]
    assert m["confirmed"]["revisions"] == ["QUARTER/R2"] and m["confirmed"]["revisions_not_saved"] == ["QUARTER/R1"]
    assert m["confirmed"]["revisions_required"] == {"QUARTER/R2": ["ANNUAL"]}
    assert not (tpl / "adjust_QUARTER_R1.yaml").exists()
    results, _ = Template.load(tpl).run([run_case.XLSX])
    assert set(results["QUARTER"]) == {"R0", "R2"}
    # 请勿修改的提醒：目录说明 + 每个规则文件开头
    assert "请勿修改" in (tpl / TEMPLATE_NOTICE).read_text(encoding="utf-8")
    for name in [quarter["rules"], m["mapping"], "manifest.yaml"]:
        assert (tpl / name).read_text(encoding="utf-8").startswith("# 已确认冻结的计算模板")
    assert load_case(case_dir)["confirmed"]["revisions"] == ["QUARTER/R2"]
    # 下一次确认时，已录入的修订不再标为本轮新增
    assert {r["key"]: r["new"] for r in confirm_options(case_dir)["revisions"]} == {"QUARTER/R1": True, "QUARTER/R2": False}


def test_confirm_result_only_keeps_existing_template(case_dir):
    tpl = confirm(case_dir, "财务甲", revisions=[])
    before = (tpl / "manifest.yaml").read_text(encoding="utf-8")
    assert confirm(case_dir, "财务乙", "只确认", save_template=False) is None
    assert (tpl / "manifest.yaml").read_text(encoding="utf-8") == before   # 原模板不动
    conf = load_case(case_dir)["confirmed"]
    assert conf["by"] == "财务乙" and conf["template"] is None


def test_same_file_name_in_two_folders_is_rejected(tmp_path):
    a, b = tmp_path / "一月" / "数据.xlsx", tmp_path / "二月" / "数据.xlsx"
    for p in (a, b):
        p.parent.mkdir()
        shutil.copy(run_case.XLSX, p)
    with pytest.raises(DuplicateFileName, match="数据.xlsx.*改名"):
        fingerprint([a, b])
    assert list(fingerprint([a, a])) == ["数据.xlsx"]           # 同一个文件给两次不算
    with pytest.raises(DuplicateFileName):
        run_agent(FakeLLM([]), [a, b], tmp_path / "out")
    assert not (tmp_path / "out" / PROCESSING_NOTICE).exists()
